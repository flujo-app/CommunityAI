from __future__ import annotations

import asyncio
import contextlib
import math
import multiprocessing as mp
import queue
import sys
from enum import Enum
from itertools import chain
from typing import Any, AsyncIterator, Dict, Iterable, List, Optional, Sequence, Tuple

import torch
from async_timeout import timeout
from hivemind import DHT
from hivemind.compression import deserialize_tensor_stream, deserialize_torch_tensor, serialize_torch_tensor
from hivemind.moe.server.connection_handler import ConnectionHandler
from hivemind.p2p import P2PContext, PeerID
from hivemind.p2p.p2p_daemon import DEFAULT_MAX_MSG_SIZE
from hivemind.proto import runtime_pb2
from hivemind.utils.asyncio import amap_in_executor, anext
from hivemind.utils.logging import get_logger
from hivemind.utils.nested import nested_flatten, nested_pack
from hivemind.utils.serializer import MSGPackSerializer
from hivemind.utils.streaming import split_for_streaming

import drift
from drift.data_structures import CHAIN_DELIMITER, UID_DELIMITER, Handle, ModuleUID
from drift.protocol_identity import TRANSPORT_SECURITY
from drift.server.admission import PUBLIC_OVERLOAD_MESSAGE, AdmissionRejected, AdmissionState
from drift.server.backend import TransformerBackend
from drift.server.block_functions import iterate_rpc_inference, run_rpc_backward, run_rpc_forward
from drift.server.task_prioritizer import DummyTaskPrioritizer, TaskPrioritizerBase
from drift.utils.convert_block import QuantType

logger = get_logger(__name__)


# Fix pickling protobufs, see https://stackoverflow.com/a/74873028
sys.modules["runtime_pb2"] = runtime_pb2


CACHE_TOKENS_AVAILABLE = "cache_tokens_available"
MAX_INFERENCE_METADATA_BYTES = 64 * 1024
MAX_PUSH_METADATA_BYTES = 64 * 1024


class Event(Enum):
    NEW_SESSION = 0
    END_SESSION = 1
    PUSH = 2
    SHUTDOWN = 3


class TransformerConnectionHandler(ConnectionHandler):
    """Handles three request types: forward, backward and forward-incremental (inference)"""

    module_backends: Dict[ModuleUID, TransformerBackend]

    def __init__(
        self,
        dht: DHT,
        module_backends: Dict[str, TransformerBackend],
        *,
        adapters: Optional[Sequence[str]],
        dht_prefix: str,
        handler_event_queues: Sequence[mp.Queue],
        handler_index: int,
        inference_max_length: int,
        request_timeout: float,
        session_timeout: float,
        step_timeout: float,
        manifest_digest: Optional[str] = None,
        identity_key_id: Optional[str] = None,
        admission_state: Optional[AdmissionState] = None,
        task_prioritizer: TaskPrioritizerBase = DummyTaskPrioritizer(),
        quant_type: QuantType,
    ):
        super().__init__(dht, module_backends)
        for module_backend in self.module_backends.values():
            assert isinstance(module_backend, TransformerBackend)
        self.dht_prefix = dht_prefix
        self.adapters = adapters
        self._handler_event_queues = handler_event_queues
        self._handler_index = handler_index
        self._own_event_queue = handler_event_queues[handler_index]
        self._listener_task: Optional[asyncio.Task] = None
        self._session_queues: Dict[str, asyncio.Queue] = {}
        self._session_handlers: Dict[str, int] = {}
        self._session_tokens: Dict[str, str] = {}
        self._admission_state = admission_state
        self._max_pending_pushes = 0 if admission_state is None else admission_state.policy.max_pending_pushes

        self.inference_max_length = inference_max_length
        self.request_timeout = request_timeout
        self.session_timeout, self.step_timeout = session_timeout, step_timeout
        self.manifest_digest = manifest_digest
        self.identity_key_id = identity_key_id
        self._prioritizer = task_prioritizer
        self.quant_type = quant_type

    def _require_training_allowed(self) -> None:
        if self._admission_state is not None:
            self._admission_state.require_training_allowed()

    def _require_bounded_inference_request(self, request: runtime_pb2.ExpertRequest) -> None:
        if self._admission_state is not None and (
            len(request.metadata) > MAX_INFERENCE_METADATA_BYTES or request.ByteSize() > DEFAULT_MAX_MSG_SIZE
        ):
            raise AdmissionRejected("public worker inference request is too large")

    def _check_manifest_digest(self, metadata: Dict[str, Any]) -> None:
        """Require manifested clients and servers to agree before executing model blocks."""
        actual = metadata.get("manifest_digest")
        if self.manifest_digest is not None and actual != self.manifest_digest:
            raise ValueError(
                f"Manifest digest mismatch: client sent {actual!r}, server requires {self.manifest_digest!r}"
            )
        if self.manifest_digest is None and actual is not None:
            raise ValueError(f"Manifest digest mismatch: client requires {actual!r}, server is in legacy mode")

    async def add_p2p_handlers(self, *args, **kwargs) -> None:
        if self._admission_state is not None:
            # A public handler must never wait for a multiprocessing Queue feeder to flush into a
            # saturated/stopped sibling while this container is shutting down.
            for event_queue in self._handler_event_queues:
                event_queue.cancel_join_thread()
        if self._listener_task is None:
            # Start listening to our own event queue before we accept any requests
            self._listener_task = asyncio.create_task(self._listen_to_event_queue())
        await super().add_p2p_handlers(*args, **kwargs)

    def shutdown(self):
        if self.is_alive():
            self._outer_pipe.send("_shutdown")
            if self._admission_state is None:
                self._own_event_queue.put((Event.SHUTDOWN, None, None))
            else:
                # This queue contains only bounded activation pushes in public mode. Make finite
                # room for the sentinel so a saturated peer cannot deadlock worker shutdown.
                signalled = False
                for _ in range(3):
                    try:
                        self._own_event_queue.put((Event.SHUTDOWN, None, None), timeout=0.05)
                        signalled = True
                        break
                    except queue.Full:
                        try:
                            discarded_event, _, _ = self._own_event_queue.get(timeout=0.05)
                            if discarded_event == Event.PUSH:
                                self._admission_state.release_push()
                        except queue.Empty:
                            pass
                if not signalled:
                    logger.warning("Could not signal the bounded public-worker push listener")
            self.join(self.shutdown_timeout)
            if self.is_alive():
                logger.warning(f"{self.__class__.__name__} failed to shut down gracefully, sending SIGTERM")
                # On Windows (thread-mode) terminate() is a no-op; the join above is the best effort.
                try:
                    self.terminate()
                except Exception:
                    pass

    async def _gather_inputs(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> Tuple[str, List[torch.Tensor], Dict]:
        block_uid, metadata = None, None

        def _unpack(req: runtime_pb2.ExpertRequest) -> Iterable[runtime_pb2.Tensor]:
            nonlocal block_uid, metadata

            if block_uid is None:
                block_uid = req.uid
            elif block_uid != req.uid:
                raise ValueError("Block uids differ in one request")

            if metadata is None:
                metadata = MSGPackSerializer.loads(req.metadata) if req.metadata else {}

            return req.tensors

        tensors_stream = amap_in_executor(_unpack, requests)
        inputs = await deserialize_tensor_stream(tensors_stream)
        assert isinstance(block_uid, str) and isinstance(metadata, dict)
        return block_uid, inputs, metadata

    async def rpc_inference(
        self,
        requests: AsyncIterator[runtime_pb2.ExpertRequest],
        context: P2PContext,
    ) -> AsyncIterator[runtime_pb2.ExpertResponse]:
        """Compute a single step of inference using attention cache; update attention cache accordingly."""
        async with timeout(self.session_timeout):
            # Count the stream before waiting for its first message. Otherwise a peer can hold
            # unlimited idle input streams without consuming an active-session lease.
            lease = self._admission_state.acquire(context.remote_id) if self._admission_state is not None else None
            try:
                request = await asyncio.wait_for(anext(requests), self.step_timeout)
            except asyncio.TimeoutError:
                if lease is not None:
                    lease.release()
                self._log_request("rpc_inference.open", None, context, warning="timed out")
                return
            except BaseException:
                if lease is not None:
                    lease.release()
                raise

            requested_uids = None
            try:
                self._require_bounded_inference_request(request)
                requested_uids = self._check_uids(request.uid)
                self._log_request("rpc_inference.open", requested_uids, context)
                metadata = MSGPackSerializer.loads(request.metadata) if request.metadata else {}
                if not isinstance(metadata, dict):
                    raise AdmissionRejected("public worker inference metadata is invalid")
                self._check_manifest_digest(metadata)
                requested_backends = tuple(self.module_backends[uid] for uid in requested_uids)
                max_length = metadata.get("max_length")
                points = metadata.get("points", 0)
                session_id = metadata.get("session_id")
                raw_alloc_timeout = metadata.get("alloc_timeout", 0.0)
                try:
                    alloc_timeout = float(raw_alloc_timeout)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise AdmissionRejected("public worker inference metadata is invalid") from exc
                args_structure = metadata.get("args_structure")
                if not requested_uids:
                    raise ValueError("User must specify at least one block for inference, but got none")
                if isinstance(max_length, bool) or not isinstance(max_length, int):
                    raise AdmissionRejected("public worker inference metadata is invalid")
                try:
                    points_finite = math.isfinite(float(points))
                except (TypeError, ValueError, OverflowError):
                    points_finite = False
                if isinstance(points, bool) or not isinstance(points, (float, int)) or not points_finite:
                    raise AdmissionRejected("public worker inference metadata is invalid")
                if not math.isfinite(alloc_timeout) or not 0 <= alloc_timeout <= self.session_timeout:
                    raise AdmissionRejected("public worker inference metadata is invalid")
                if not 0 <= max_length <= self.inference_max_length:
                    raise ValueError(
                        f"Cannot allocate KV cache for {max_length} tokens, max = {self.inference_max_length}"
                    )

                batch_size = request.tensors[0].size[0] if request.tensors else 1

                session_context = (
                    self._managed_session(session_id) if session_id is not None else contextlib.nullcontext()
                )
                with session_context:
                    async with self._allocate_cache(
                        requested_backends, batch_size=batch_size, max_length=max_length, timeout=alloc_timeout
                    ) as cache_handles:
                        background_tasks = set()
                        try:
                            async for output_tensors, can_push, step_metadata, response_metadata in iterate_rpc_inference(
                                requested_uids=requested_uids,
                                requested_backends=requested_backends,
                                active_adapter=self._get_active_adapter(metadata),
                                input_iterator=self._iterate_inference_steps(
                                    request, requests, session_id, requested_uids, context
                                ),
                                cache_handles=cache_handles,
                                max_length=max_length,
                                prioritizer=self._prioritizer,
                                points=points,
                                quant_type=self.quant_type,
                                args_structure=args_structure,
                            ):
                                if can_push:
                                    task = self._create_output_push_task(
                                        request,
                                        output_tensors[0],
                                        step_metadata,
                                    )
                                    if task is not None:
                                        background_tasks.add(task)
                                        task.add_done_callback(background_tasks.discard)
                                response = runtime_pb2.ExpertResponse(tensors=output_tensors)
                                if response_metadata:  # Gemma 4 KV-sharing donor keys/values for downstream spans
                                    response.metadata = MSGPackSerializer.dumps(response_metadata)
                                yield response
                        finally:
                            for task in tuple(background_tasks):
                                task.cancel()
                            if background_tasks:
                                await asyncio.gather(*background_tasks, return_exceptions=True)

            finally:
                if lease is not None:
                    lease.release()
                self._log_request("rpc_inference.close", requested_uids, context)

    @contextlib.contextmanager
    def _managed_session(self, session_id: str):
        route_token = None
        if self._admission_state is not None:
            self._admission_state.validate_session_id(session_id)
            route_token = self._admission_state.create_session_token()
        elif not isinstance(session_id, str):
            raise AdmissionRejected("session identity is invalid")
        if session_id in self._session_queues:
            raise AdmissionRejected("public worker session identity is already active")

        session_key = None
        notified_handlers = []
        local_queue = asyncio.Queue(maxsize=self._max_pending_pushes)
        registered_locally = False
        try:
            self._session_queues[session_id] = local_queue
            self._session_handlers[session_id] = self._handler_index
            if route_token is not None:
                self._session_tokens[session_id] = route_token
            registered_locally = True
            if self._admission_state is not None:
                # Publish only after the exact local route generation is ready.
                session_key, registered_token = self._admission_state.register_session(
                    session_id,
                    self._handler_index,
                    route_token,
                )
                assert registered_token == route_token

            # Legacy/private servers retain their historical cross-handler routing. Manifested
            # public workers use the atomic shared registry instead, so activation saturation can
            # never starve route cleanup.
            if self._admission_state is None:
                for other_index, other_queue in enumerate(self._handler_event_queues):
                    if other_index != self._handler_index:
                        other_queue.put((Event.NEW_SESSION, session_id, self._handler_index))
                        notified_handlers.append(other_queue)
            yield
        finally:
            # Unpublish first so no new cross-handler push can target a closing local queue.
            if self._admission_state is not None and session_key is not None and route_token is not None:
                self._admission_state.unregister_session(session_key, self._handler_index, route_token)
            elif self._admission_state is None:
                for other_queue in notified_handlers:
                    other_queue.put((Event.END_SESSION, session_id, self._handler_index))

            if registered_locally:
                self._session_queues.pop(session_id, None)
                self._session_handlers.pop(session_id, None)
                self._session_tokens.pop(session_id, None)
                while True:
                    try:
                        queued_request = local_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if queued_request is not None and self._admission_state is not None:
                        self._admission_state.release_push()
                local_queue.put_nowait(None)

    def _put_into_session_queue(
        self,
        session_id: str,
        request: runtime_pb2.ExpertRequest,
        *,
        handler_index: Optional[int] = None,
        route_token: Optional[str] = None,
    ) -> None:
        if handler_index is None:
            if self._admission_state is not None:
                handler_index, route_token = self._admission_state.reserve_push(session_id)
            else:
                handler_index = self._session_handlers.get(session_id)
        if handler_index is None:
            logger.debug("Ignored rpc_push to an unknown session")
            return

        try:
            if not 0 <= handler_index < len(self._handler_event_queues):
                if self._admission_state is not None:
                    self._admission_state.mark_unhealthy()
                raise AdmissionRejected("public worker admission state is unavailable")
            if self._admission_state is not None and route_token is None:
                self._admission_state.mark_unhealthy()
                raise AdmissionRejected("public worker admission state is unavailable")
            if handler_index == self._handler_index:
                session_queue = self._session_queues.get(session_id)
                if session_queue is None or (
                    self._admission_state is not None and self._session_tokens.get(session_id) != route_token
                ):
                    raise AdmissionRejected("public worker push target is unavailable")
                session_queue.put_nowait(request)
            else:
                payload = (route_token, request) if self._admission_state is not None else request
                self._handler_event_queues[handler_index].put_nowait((Event.PUSH, session_id, payload))
        except (AdmissionRejected, asyncio.QueueFull, queue.Full) as exc:
            if self._admission_state is not None:
                self._admission_state.release_push()
            if isinstance(exc, AdmissionRejected):
                raise
            raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE) from exc

    async def _get_from_session_queue(self, session_id: str) -> Optional[runtime_pb2.ExpertRequest]:
        session_queue = self._session_queues.get(session_id)
        if session_queue is None:
            raise AdmissionRejected("public worker push target is unavailable")
        request = await session_queue.get()
        if request is not None and self._admission_state is not None:
            self._admission_state.release_push()
        return request

    async def _listen_to_event_queue(self):
        loop = asyncio.get_event_loop()
        while True:
            try:
                event, session_id, payload = await loop.run_in_executor(None, self._own_event_queue.get)
                if event == Event.SHUTDOWN:
                    break
                if event == Event.PUSH:
                    maybe_session_queue = self._session_queues.get(session_id)
                    delivered = False
                    pushed_request = payload
                    if self._admission_state is not None:
                        try:
                            route_token, pushed_request = payload
                        except (TypeError, ValueError):
                            self._admission_state.mark_unhealthy()
                            route_token = None
                        if self._session_tokens.get(session_id) != route_token:
                            maybe_session_queue = None
                    if maybe_session_queue is not None:
                        try:
                            maybe_session_queue.put_nowait(pushed_request)
                            delivered = True
                        except asyncio.QueueFull:
                            logger.warning("Dropped a pushed activation at bounded queue capacity")
                    if not delivered and self._admission_state is not None:
                        self._admission_state.release_push()
                elif self._admission_state is None and event == Event.NEW_SESSION:
                    self._session_handlers[session_id] = payload  # index of the handler that owns that session
                elif self._admission_state is None and event == Event.END_SESSION:
                    self._session_handlers.pop(session_id, None)
                else:
                    if self._admission_state is not None:
                        self._admission_state.mark_unhealthy()
                    raise RuntimeError(f"Unexpected event: {event}")
            except Exception as e:
                logger.exception(e)

    async def _iterate_inference_steps(
        self,
        first_request: runtime_pb2.ExpertRequest,
        requests: AsyncIterator[runtime_pb2.ExpertRequest],
        session_id: Optional[str],
        requested_uids: Sequence[str],
        context: P2PContext,
    ) -> AsyncIterator[Tuple[runtime_pb2.ExpertRequest, dict]]:
        processed_step_ids = set()
        n_pushes = n_late_pushes = 0
        request = first_request
        anext_task = get_push_task = None
        try:
            with contextlib.nullcontext():
                while True:
                    self._require_bounded_inference_request(request)
                    if not request.tensors:  # the user ended the inference stream
                        return
                    metadata = MSGPackSerializer.loads(request.metadata) if request.metadata else {}
                    if not isinstance(metadata, dict):
                        raise AdmissionRejected("public worker inference metadata is invalid")
                    step_id = metadata.get("step_id")

                    pushed = metadata.get("pushed")
                    if pushed:
                        n_pushes += 1
                        self._log_request("rpc_inference.push", requested_uids, context, debug=f"session received push")

                    if step_id is None or step_id not in processed_step_ids:
                        yield request, metadata
                        if step_id is not None:
                            processed_step_ids.add(step_id)
                    elif pushed:
                        n_late_pushes += 1
                        self._log_request(
                            "rpc_inference.push",
                            requested_uids,
                            context,
                            warning=f"arrived late {n_late_pushes / n_pushes * 100:.1f}% of the time",
                        )

                    # Wait for the next request, coming either from the `requests` iterator or `push_queue`
                    if anext_task is None:
                        anext_task = asyncio.create_task(anext(requests))
                    if get_push_task is None:
                        if session_id is not None:
                            get_push_task = asyncio.create_task(self._get_from_session_queue(session_id))
                        else:
                            get_push_task = asyncio.create_task(asyncio.Event().wait())  # Dummy never-ending task
                    done, _ = await asyncio.wait(
                        [anext_task, get_push_task], timeout=self.step_timeout, return_when=asyncio.FIRST_COMPLETED
                    )

                    if anext_task in done:
                        request = await anext_task
                        anext_task = None
                    elif get_push_task in done:
                        request = await get_push_task
                        get_push_task = None
                    else:
                        self._log_request("rpc_inference.step", requested_uids, context, warning="timed out")
                        anext_task.cancel()
                        get_push_task.cancel()
                        return
        except Exception:
            logger.warning("rpc_inference._iterate_inference_steps() exception:", exc_info=True)
            raise
        finally:
            for task in (anext_task, get_push_task):
                if task is not None and not task.done():
                    task.cancel()

    async def rpc_push(self, request: runtime_pb2.ExpertRequest, context: P2PContext) -> runtime_pb2.ExpertResponse:
        """Directly push activation tensors from one server to another."""

        handler_index = None
        route_token = None
        if self._admission_state is not None:
            self._admission_state.require_healthy()
            if len(request.metadata) > MAX_PUSH_METADATA_BYTES or request.ByteSize() > DEFAULT_MAX_MSG_SIZE:
                raise AdmissionRejected("public worker push request is too large")

        metadata = MSGPackSerializer.loads(request.metadata)
        if not isinstance(metadata, dict):
            raise AdmissionRejected("public worker push metadata is invalid")
        session_id = metadata.get("session_id")
        if self._admission_state is not None:
            handler_index, route_token = self._admission_state.reserve_push(session_id)

        try:
            requested_uids = self._check_uids(request.uid)
            self._log_request("rpc_push", requested_uids, context, debug="bounded activation push")
        except Exception:
            if self._admission_state is not None:
                self._admission_state.release_push()
            raise

        self._put_into_session_queue(
            session_id,
            request,
            handler_index=handler_index,
            route_token=route_token,
        )
        return runtime_pb2.ExpertResponse()

    def _create_output_push_task(
        self,
        request: runtime_pb2.ExpertRequest,
        serialized_outputs: runtime_pb2.Tensor,
        metadata: dict,
    ) -> Optional[asyncio.Task]:
        reserved = self._admission_state is not None
        if reserved:
            try:
                self._admission_state.reserve_outbound_push()
            except AdmissionRejected:
                return None
        try:
            task = asyncio.create_task(self._push_outputs(request, serialized_outputs, metadata))
        except Exception:
            if reserved:
                self._admission_state.release_push()
            raise
        if reserved:
            task.add_done_callback(lambda completed: self._admission_state.release_push())
        return task

    async def _push_outputs(
        self, request: runtime_pb2.ExpertRequest, serialized_outputs: runtime_pb2.Tensor, metadata: dict
    ) -> None:
        try:
            next_servers = metadata.get("next_servers")
            if not next_servers:
                return

            next_peer_id, next_session_id, next_start, next_end = next_servers[0]
            next_peer_id = PeerID.from_base58(next_peer_id)
            next_uid = CHAIN_DELIMITER.join(f"{self.dht_prefix}{UID_DELIMITER}{i}" for i in range(next_start, next_end))

            # Sending hidden states serialized with output_schema to avoid double serialization
            next_tensors = [serialized_outputs] + request.tensors[1:]
            next_metadata = metadata.copy()
            next_metadata.update(session_id=next_session_id, next_servers=next_servers[1:], pushed=True)

            stub = self.get_stub(self._p2p, next_peer_id)
            await stub.rpc_push(
                runtime_pb2.ExpertRequest(
                    uid=next_uid,
                    tensors=next_tensors,
                    metadata=MSGPackSerializer.dumps(next_metadata),
                ),
                timeout=self.request_timeout,
            )
        except Exception:
            logger.debug("Failed to push outputs to a downstream worker", exc_info=True)

    async def rpc_forward(self, request: runtime_pb2.ExpertRequest, context: P2PContext) -> runtime_pb2.ExpertResponse:
        self._require_training_allowed()
        async with timeout(self.request_timeout):
            # Parse request and prepare backends
            flat_inputs = [deserialize_torch_tensor(tensor) for tensor in request.tensors]
            requested_uids = self._check_uids(request.uid)
            self._log_request("rpc_forward", requested_uids, context)

            requested_backends = tuple(self.module_backends[uid] for uid in requested_uids)
            metadata = MSGPackSerializer.loads(request.metadata) if request.metadata else {}
            self._check_manifest_digest(metadata)
            active_adapter = self._get_active_adapter(metadata)
            points = metadata.get("points", 0)
            args_structure = metadata.get("args_structure")
            assert isinstance(
                points, (float, int)
            ), f"rpc_forward should have number of points as number or None, got {points}"

            hidden_states = await run_rpc_forward(
                *flat_inputs,
                requested_backends=requested_backends,
                prioritizer=self._prioritizer,
                active_adapter=active_adapter,
                points=points,
                args_structure=args_structure,
            )
            return runtime_pb2.ExpertResponse(
                tensors=self._serialize_outputs(hidden_states, requested_backends, metadata)
            )

    async def rpc_forward_stream(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertRequest]:
        self._require_training_allowed()
        async with timeout(self.request_timeout):
            # Parse requests and prepare backends
            uid_str, flat_inputs, metadata = await self._gather_inputs(requests, context)
            self._check_manifest_digest(metadata)
            requested_uids = self._check_uids(uid_str)
            self._log_request("rpc_forward_stream", requested_uids, context)

            requested_backends = tuple(self.module_backends[uid] for uid in requested_uids)
            active_adapter = self._get_active_adapter(metadata)
            points = metadata.get("points", 0)
            args_structure = metadata.get("args_structure")
            assert isinstance(
                points, (float, int)
            ), f"rpc_forward_stream should have number of points as number or None, got {points}"

            hidden_states = await run_rpc_forward(
                *flat_inputs,
                requested_backends=requested_backends,
                prioritizer=self._prioritizer,
                active_adapter=active_adapter,
                points=points,
                args_structure=args_structure,
            )

            # Split the serialized_output for streaming and respond to client
            for tensor in self._serialize_outputs(hidden_states, requested_backends, metadata):
                for part in split_for_streaming(tensor, DEFAULT_MAX_MSG_SIZE):
                    yield runtime_pb2.ExpertResponse(tensors=[part])

    def _serialize_outputs(
        self,
        hidden_states: torch.Tensor,
        requested_backends: Sequence[TransformerBackend],
        metadata: Dict[str, Any],
    ) -> Sequence[runtime_pb2.Tensor]:
        """Serialize forward outputs using either outputs_schema or custom user-specified schema"""
        assert isinstance(hidden_states, torch.Tensor) and hidden_states.ndim == 3, "hidden_states must be a 3d tensor"
        outputs_schema = requested_backends[-1].outputs_schema

        if metadata.get("output_compression") is not None:
            assert isinstance(metadata["output_compression"], (list, tuple)), "output_compression must be a tuple/list"
            output_compression = tuple(metadata["output_compression"])
            assert all(isinstance(c, int) for c in output_compression), "output_compression must contain integers"
            assert len(output_compression) == 1, f"output_compression tuple should have 1 element"
        else:
            output_compression = tuple(tensor.compression for tensor in outputs_schema)

        return [
            serialize_torch_tensor(result.to(proto.dtype), compression, allow_inplace=True)
            for result, proto, compression in zip([hidden_states], outputs_schema, output_compression)
        ]

    async def rpc_backward(self, request: runtime_pb2.ExpertRequest, context: P2PContext) -> runtime_pb2.ExpertResponse:
        self._require_training_allowed()
        async with timeout(self.request_timeout):
            # Parse requests and prepare backends
            flat_tensors = [deserialize_torch_tensor(tensor) for tensor in request.tensors]
            requested_uids = self._check_uids(request.uid)
            self._log_request("rpc_backward", requested_uids, context)

            requested_backends = tuple(self.module_backends[uid] for uid in requested_uids)
            metadata = MSGPackSerializer.loads(request.metadata) if request.metadata else {}
            self._check_manifest_digest(metadata)
            active_adapter = self._get_active_adapter(metadata)
            points = metadata.get("points", 0)
            args_structure = metadata.get("args_structure")
            assert isinstance(
                points, (float, int)
            ), f"rpc_backward should have number of points as number or None, got {points}"

            grads = await run_rpc_backward(
                *flat_tensors,
                requested_backends=requested_backends,
                prioritizer=self._prioritizer,
                active_adapter=active_adapter,
                points=points,
                args_structure=args_structure,
            )

            return runtime_pb2.ExpertResponse(tensors=self._serialize_grads(grads, requested_backends, metadata))

    async def rpc_backward_stream(
        self, requests: AsyncIterator[runtime_pb2.ExpertRequest], context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertResponse]:
        self._require_training_allowed()
        async with timeout(self.request_timeout):
            uids_header, flat_tensors, metadata = await self._gather_inputs(requests, context)
            self._check_manifest_digest(metadata)
            requested_uids = self._check_uids(uids_header)
            self._log_request("rpc_backward_stream", requested_uids, context)

            requested_backends = tuple(self.module_backends[uid] for uid in requested_uids)
            active_adapter = self._get_active_adapter(metadata)
            points = metadata.get("points", 0)
            args_structure = metadata.get("args_structure")
            assert isinstance(
                points, (float, int)
            ), f"rpc_backward_stream should have number of points as number or None, got {points}"

            grads = await run_rpc_backward(
                *flat_tensors,
                requested_backends=requested_backends,
                prioritizer=self._prioritizer,
                active_adapter=active_adapter,
                points=points,
                args_structure=args_structure,
            )
            # Split the serialized_grad_inputs for streaming and respond
            for tensor in self._serialize_grads(grads, requested_backends, metadata):
                for part in split_for_streaming(tensor, DEFAULT_MAX_MSG_SIZE):
                    yield runtime_pb2.ExpertResponse(tensors=[part])

    def _get_active_adapter(self, metadata: dict) -> str:
        active_adapter = metadata.get("active_adapter", "")
        if active_adapter and (active_adapter not in self.adapters):
            raise KeyError(f"adapter {active_adapter} not found")
        return active_adapter

    def _serialize_grads(
        self,
        grads: Sequence[torch.Tensor],
        requested_backends: Sequence[TransformerBackend],
        metadata: Dict[str, Any],
    ) -> Sequence[runtime_pb2.Tensor]:
        """Serialize backward gradients w.r.t. inputs using either default schema or custom user-specified schema"""
        # Modify grad_inputs_schema to support grad_prompts
        assert len(requested_backends[0].args_schema) == 1 and len(grads) in (1, 2)  # TODO generalize
        flat_grads_schema = tuple(
            nested_flatten((requested_backends[0].args_schema * len(grads), requested_backends[0].kwargs_schema))
        )  # TODO generalize

        if metadata.get("output_compression") is not None:
            assert isinstance(metadata["output_compression"], (list, tuple)), "output_compression must be a tuple/list"
            output_compression = tuple(metadata["output_compression"])
            assert all(isinstance(c, int) for c in output_compression), "output_compression must contain integers"
            assert len(output_compression) == len(grads), f"output_compression should have {len(grads)} elements"
        else:
            output_compression = tuple(tensor.compression for tensor in flat_grads_schema)

        return [
            serialize_torch_tensor(result.to(proto.dtype), compression, allow_inplace=True)
            for result, proto, compression in zip(grads, flat_grads_schema, output_compression)
        ]

    def _check_uids(self, uids: str) -> Tuple[ModuleUID, ...]:
        """Check that the first request to rpc_inference is valid"""
        uids = (uids or "").split(CHAIN_DELIMITER)
        if not uids:
            raise RuntimeError("User did not provide any uids")
        for uid in uids:
            if uid not in self.module_backends:
                raise RuntimeError(f"Remote peer does not serve {uid}")
        return tuple(uids)

    @contextlib.asynccontextmanager
    async def _allocate_cache(
        self,
        backends: Sequence[TransformerBackend],
        *,
        batch_size: int,
        max_length: int,
        timeout: Optional[float],
    ) -> Sequence[Sequence[Handle]]:
        """
        Allocate memory cache for all transformer blocks, return cache handle
        :returns: a list of {len(backends)} elements, where i-th element is a tuple of cache handles for i-th backend
        """
        memory_cache = backends[0].memory_cache
        if memory_cache.paged:
            # Paged mode reserves no per-session cache up front: register one lazily-grown slot per
            # block and hand each backend its slot id as a single-element cache-handle tuple.
            async with memory_cache.allocate_paged_slots(len(backends), batch_size, timeout) as slot_ids:
                yield [(slot_id,) for slot_id in slot_ids]
            return
        descriptors = [backend.get_inference_cache_descriptors(batch_size, max_length) for backend in backends]
        async with memory_cache.allocate_cache(*chain(*descriptors), timeout=timeout) as handles:
            yield nested_pack(handles, descriptors)

    def _log_request(
        self,
        method: str,
        uids: Optional[Sequence[ModuleUID]],
        context: P2PContext,
        *,
        debug: Optional[str] = None,
        warning: Optional[str] = None,
    ) -> None:
        if uids is not None:
            friendly_uids = [uid.split(".")[-1] for uid in uids if "." in uid]
            friendly_uids = [int(uid) for uid in friendly_uids if uid.isdigit()]
            friendly_uids = f"{min(friendly_uids)}:{max(friendly_uids) + 1}" if friendly_uids else uids
        else:
            friendly_uids = "n/a"

        friendly_remote_id = (
            "authenticated" if self._admission_state is not None else "..." + str(context.remote_id)[-6:]
        )

        message = f"{method}(blocks={friendly_uids}, remote_peer={friendly_remote_id})"
        if warning is not None:
            logger.warning(f"{message}: {warning}")
        elif debug is not None:
            logger.debug(f"{message}: {debug}")
        else:
            logger.info(message)

    async def rpc_info(self, request: runtime_pb2.ExpertUID, context: P2PContext) -> runtime_pb2.ExpertInfo:
        """Return metadata about stored block uids and current load"""

        backend = self.module_backends[request.uid] if request.uid else next(iter(self.module_backends.values()))
        result = {
            "version": drift.__version__,
            "manifest_digest": self.manifest_digest,
            "server_peer_id": self.dht.peer_id.to_base58(),
            "identity_key_id": self.identity_key_id,
            "transport_security": TRANSPORT_SECURITY if self.manifest_digest is not None else None,
            "dht_client_mode": self.dht.client_mode,
            CACHE_TOKENS_AVAILABLE: backend.memory_cache.bytes_left // max(backend.cache_bytes_per_token.values()),
        }

        if request.uid:
            block_info = self.module_backends[request.uid].get_info()
            common_keys = set(result.keys()) & set(block_info.keys())
            if common_keys:
                raise RuntimeError(f"The block's rpc_info has keys reserved for the server's rpc_info: {common_keys}")
            result.update(block_info)

        return runtime_pb2.ExpertInfo(serialized_info=MSGPackSerializer.dumps(result))
