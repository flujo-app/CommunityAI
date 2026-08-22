from __future__ import annotations

import asyncio
import itertools
import time
import uuid
from typing import AsyncIterator, Dict, List, Optional, Tuple

import torch
from hivemind.compression import deserialize_torch_tensor, serialize_torch_tensor
from hivemind.moe.client.remote_expert_worker import RemoteExpertWorker
from hivemind.p2p import P2P
from hivemind.proto import runtime_pb2
from hivemind.utils.asyncio import anext
from hivemind.utils.logging import get_logger
from hivemind.utils.serializer import MSGPackSerializer
from hivemind.utils.tensor_descr import BatchTensorDescriptor

from drift.client.config import ClientConfig
from drift.client.routing import RemoteSequenceManager, maybe_log_traceback
from drift.data_structures import CHAIN_DELIMITER, ModuleUID, RemoteSpanInfo, RPCInfo
from drift.server.handler import TransformerConnectionHandler
from drift.utils.misc import DUMMY, DUMMY_INT64, is_dummy
from drift.utils.packaging import pack_args_kwargs
from drift.utils.shared_kv import flatten_shared_kv, unflatten_shared_kv

logger = get_logger(__name__)


class _ServerInferenceSession:
    """
    An interface to a single multi-step *inference* session for a a set of blocks on a specific server.

    :note: This class is *not* fault-tolerant out of the box.
    """

    def __init__(
        self,
        config: ClientConfig,
        span: RemoteSpanInfo,
        uid: ModuleUID,
        rpc_info: RPCInfo,
        inputs_queue: asyncio.Queue,
        outputs_aiter: AsyncIterator,
        *,
        max_length: int,
        **metadata,
    ):
        self.config = config
        self.span, self.uid, self.rpc_info = span, uid, rpc_info
        self.num_blocks = uid.count(CHAIN_DELIMITER) + 1
        self._inputs_queue: asyncio.Queue[runtime_pb2.ExpertRequest] = inputs_queue
        self._outputs_stream: AsyncIterator[runtime_pb2.ExpertResponse] = outputs_aiter
        self.session_id = str(uuid.uuid4())
        self.session_metadata = dict(max_length=max_length, **metadata)
        self.stepped = False
        self.closed = False

        self._position = 0
        self.history = None  # Used in case of server failures to regenerate attention caches on new servers
        self.per_layer_history = None
        self.replay_prompts = None
        self._replay_target_position = None
        self.next_session = None

    @classmethod
    async def create(
        cls,
        config: ClientConfig,
        p2p: P2P,
        span: RemoteSpanInfo,
        uid: ModuleUID,
        rpc_info: RPCInfo,
        **metadata,
    ) -> _ServerInferenceSession:
        """Create a new session for a given remote module. This code is meant to be run inside RemoteExpertWorker"""
        stub = TransformerConnectionHandler.get_stub(p2p, span.peer_id)
        inputs_queue = asyncio.Queue()
        outputs_stream = await asyncio.wait_for(
            stub.rpc_inference(cls._read_inputs_from_queue(inputs_queue)),
            config.connect_timeout,
        )
        return cls(config, span, uid, rpc_info, inputs_queue, outputs_stream, **metadata)

    @staticmethod
    async def _read_inputs_from_queue(queue: asyncio.Queue, input_timeout: Optional[float] = None) -> AsyncIterator:
        while True:
            next_input_message = await asyncio.wait_for(queue.get(), input_timeout)
            yield next_input_message
            if not next_input_message.uid and not next_input_message.tensors:
                break  # this message means "done sending"

    @property
    def position(self):
        return self._position

    @property
    def needs_replay(self) -> bool:
        return self._replay_target_position is not None

    @position.setter
    def position(self, start_from_position: int):
        assert start_from_position <= self._position
        self._position = start_from_position
        if self.history is not None and self.history.shape[1] >= start_from_position:
            self.history = self.history[:, :start_from_position, :] if start_from_position > 0 else None
        if self.per_layer_history is not None and self.per_layer_history.shape[2] >= start_from_position:
            self.per_layer_history = (
                self.per_layer_history[:, :, :start_from_position, :] if start_from_position > 0 else None
            )

    def prepare_for_replay(
        self,
        *,
        history: Optional[torch.Tensor],
        per_layer_history: Optional[torch.Tensor],
        prompts: Optional[torch.Tensor],
        target_position: int,
    ) -> None:
        """Prepare a fresh server session to rebuild its attention cache from client-side history."""
        assert not self.stepped and self._position == 0
        assert target_position > 0
        self.history = history
        self.per_layer_history = per_layer_history
        self.replay_prompts = prompts
        self._replay_target_position = target_position

    def step(
        self,
        inputs: torch.Tensor,
        prompts: torch.Tensor,
        hypo_ids: torch.LongTensor,
        *,
        step_id: str,
        per_layer_inputs: Optional[torch.Tensor] = None,
        shared_kv_states: Optional[Dict[str, Tuple[torch.Tensor, torch.Tensor]]] = None,
    ) -> Tuple[torch.Tensor, Dict[str, Tuple[torch.Tensor, torch.Tensor]]]:
        """
        Inference step: send a chunk of input tensors and receive a chunk of outputs
        :prompts: optional DEEP prompts, added to a prefix of each layer's outputs,
          if specified, deep prompts should have shape [num_layers, batch_size, prefix_len, hid_size]
        :per_layer_inputs: optional Gemma 4 Per-Layer Embeddings for this span's blocks, sliced from
          the client's [num_layers, batch_size, seq_len, per_layer_dim] tensor; seq-aligned to `inputs`
        :shared_kv_states: optional Gemma 4 KV-sharing donor keys/values produced by upstream spans,
          fed to this span so its consumer layers can attend against a donor hosted on another server
        :returns: (this span's output hidden states, the donor keys/values this span newly produced)
        """
        if self.closed:
            raise Exception("Session is closed, cannot perform step")

        n_input_tokens = inputs.shape[1]
        if self.needs_replay:
            if not is_dummy(hypo_ids):
                raise NotImplementedError(
                    "Attention-cache replay after a worker failure is not implemented for beam search"
                )

            target_position = self._replay_target_position
            assert self._position == 0 and target_position is not None
            if self.history is None:
                self.history = inputs
            elif self.history.shape[1] < target_position:
                missing_tokens = target_position - self.history.shape[1]
                if missing_tokens > n_input_tokens:
                    raise RuntimeError(
                        f"Cannot restore span {self.span}: activation history is missing {missing_tokens} tokens, "
                        f"but the current step contains only {n_input_tokens}"
                    )
                self.history = torch.cat([self.history, inputs[:, -missing_tokens:]], dim=1)
            if self.history.shape[1] != target_position:
                raise RuntimeError(
                    f"Cannot restore span {self.span}: activation history has {self.history.shape[1]} tokens, "
                    f"expected {target_position}"
                )
            inputs = self.history
            n_input_tokens = target_position
        else:
            if self.history is None:
                self.history = inputs
            elif self.history.shape[1] == self._position:
                self.history = torch.cat([self.history, inputs[:, -n_input_tokens:]], dim=1)
            assert self.history.shape[1] == self._position + n_input_tokens, (
                f"Broken input cache: span={self.span} shape={self.history.shape} "
                f"position={self._position} n_input_tokens={n_input_tokens}"
            )

            if not self.stepped:
                inputs = self.history  # Pass full inputs including prefix
            else:
                inputs = inputs[:, -n_input_tokens:]  # No need to pass prefix further

        has_per_layer_inputs = per_layer_inputs is not None and not is_dummy(per_layer_inputs)
        if self.needs_replay:
            target_position = self._replay_target_position
            assert target_position is not None
            if self.per_layer_history is not None:
                if self.per_layer_history.shape[2] < target_position:
                    missing_tokens = target_position - self.per_layer_history.shape[2]
                    if not has_per_layer_inputs or missing_tokens > per_layer_inputs.shape[2]:
                        raise RuntimeError(
                            f"Cannot restore per-layer inputs for span {self.span}: missing {missing_tokens} tokens"
                        )
                    self.per_layer_history = torch.cat(
                        [self.per_layer_history, per_layer_inputs[:, :, -missing_tokens:, :]], dim=2
                    )
                if self.per_layer_history.shape[2] != target_position:
                    raise RuntimeError(
                        f"Cannot restore per-layer inputs for span {self.span}: history has "
                        f"{self.per_layer_history.shape[2]} tokens, expected {target_position}"
                    )
                per_layer_inputs = self.per_layer_history
                has_per_layer_inputs = True
            elif has_per_layer_inputs:
                if per_layer_inputs.shape[2] != target_position:
                    raise RuntimeError(
                        f"Cannot restore per-layer inputs for span {self.span}: received "
                        f"{per_layer_inputs.shape[2]} tokens, expected {target_position}"
                    )
                self.per_layer_history = per_layer_inputs
        elif has_per_layer_inputs:
            if self.per_layer_history is None:
                self.per_layer_history = per_layer_inputs
            elif self.per_layer_history.shape[2] == self._position:
                self.per_layer_history = torch.cat(
                    [self.per_layer_history, per_layer_inputs[:, :, -n_input_tokens:, :]], dim=2
                )
            assert self.per_layer_history.shape[2] == self._position + n_input_tokens, (
                f"Broken per-layer input cache: span={self.span} shape={self.per_layer_history.shape} "
                f"position={self._position} n_input_tokens={n_input_tokens}"
            )

        if self.needs_replay and self.replay_prompts is not None:
            prompts = self.replay_prompts
        elif not is_dummy(prompts):
            self.replay_prompts = prompts

        # serialize inputs and put them into the queue. Gemma 4 appends the per-layer inputs as a
        # fourth tensor and, when a KV-sharing donor lives upstream, its keys/values as further
        # tensors; other models keep the original 3-tensor layout for wire compatibility.
        has_shared_kv = bool(shared_kv_states)
        extra_tensors = []
        if has_per_layer_inputs:
            extra_tensors.append(per_layer_inputs)
        elif has_shared_kv:
            extra_tensors.append(DUMMY)  # keep per-layer-inputs in slot 4 so shared KV starts at slot 5
        shared_kv_keys: List[str] = []
        if has_shared_kv:
            shared_kv_keys, shared_kv_tensors = flatten_shared_kv(shared_kv_states)
            extra_tensors.extend(shared_kv_tensors)
        input_tensors, args_structure = pack_args_kwargs(inputs, prompts, hypo_ids, *extra_tensors)

        request_metadata = dict(session_id=self.session_id, step_id=step_id)
        if has_shared_kv:
            request_metadata["shared_kv_keys"] = shared_kv_keys
        if not self.stepped:
            request_metadata.update(self.session_metadata)
        if self._position is not None:
            request_metadata["start_from_position"] = self._position
        elif self.config.use_server_to_server:
            next_servers = self._collect_next_servers()
            if next_servers:
                request_metadata["next_servers"] = next_servers

        request_metadata["args_structure"] = args_structure

        # TODO: make possible to use different compression method for different tensors
        server_side_inference_schema, kwargs_schema = self.rpc_info["inference_schema"]
        compression = server_side_inference_schema[0].compression
        inference_schema = tuple(BatchTensorDescriptor.from_tensor(arg, compression) for arg in input_tensors)

        # TODO: create more explicit way to check servers schema and client's structure
        assert len(input_tensors) >= len(
            server_side_inference_schema
        ), "Hidden_state, prompts and hypo_ids tensors are necessary for an inference step"

        outputs_serialized = RemoteExpertWorker.run_coroutine(
            self._step(
                runtime_pb2.ExpertRequest(
                    uid=self.uid,
                    tensors=[
                        serialize_torch_tensor(tensor.to(proto.dtype), proto.compression)
                        for tensor, proto in zip(input_tensors, inference_schema)
                    ],
                    metadata=MSGPackSerializer.dumps(request_metadata),
                )
            )
        )
        outputs = list(map(deserialize_torch_tensor, outputs_serialized.tensors))
        assert (
            outputs[0].shape == inputs.shape
        ), f"output activation shape is different from input shape: {outputs[0].shape} != {inputs.shape}"

        # Trailing tensors (if any) are the KV-sharing donor keys/values this span produced; the
        # response metadata names the layer types, in the same flattened order.
        response_metadata = MSGPackSerializer.loads(outputs_serialized.metadata) if outputs_serialized.metadata else {}
        produced_keys = response_metadata.get("shared_kv_keys", [])
        produced_shared_kv = unflatten_shared_kv(produced_keys, outputs[1 : 1 + 2 * len(produced_keys)])

        self._position += n_input_tokens
        self._replay_target_position = None

        return outputs[0], produced_shared_kv

    def _collect_next_servers(self) -> List[Tuple[str, str, int, int]]:
        next_servers = []
        session = self.next_session
        while session is not None and session.stepped:
            next_servers.append(
                (session.span.peer_id.to_base58(), session.session_id, session.span.start, session.span.end)
            )
            session = session.next_session
        return next_servers

    async def _step(self, inputs_serialized: runtime_pb2.ExpertRequest) -> runtime_pb2.ExpertResponse:
        """Inference step on serialized data. This code is meant to be run inside RemoteExpertWorker"""
        await self._inputs_queue.put(inputs_serialized)
        self.stepped = True
        return await asyncio.wait_for(anext(self._outputs_stream), self.config.request_timeout)

    def close(self):
        """Finish a given inference session, close the underlying connection"""
        if self._outputs_stream is None:
            return  # already closed
        RemoteExpertWorker.run_coroutine(self._aclose_stream())
        self._outputs_stream = self._inputs_queue = None
        self.closed = True

    async def _aclose_stream(self):
        """Close the inference session. This code is meant to be run inside RemoteExpertWorker"""
        if self._outputs_stream is None:
            return  # already closed
        if self.stepped:
            await self._inputs_queue.put(runtime_pb2.ExpertRequest())  # empty request will trigger end of session
            try:
                await anext(self._outputs_stream)
            except StopAsyncIteration:
                pass

    def __del__(self):
        self.close()

    def __enter__(self):
        assert not self.closed
        return self

    def __exit__(self, *exc_details):
        self.close()


class InferenceSession:
    """
    An interface to a multi-step *inference* session for a sequence of remote transformer blocks
    """

    def __init__(self, sequence_manager: RemoteSequenceManager, max_length: int):
        self._sequence_manager = sequence_manager
        self._closed = False
        self._server_sessions = []
        self._position = 0
        self._max_length = max_length
        self.output_ids = None
        self.past_key_values = None

    @property
    def num_blocks(self) -> int:
        return len(self._sequence_manager)

    @property
    def position(self) -> int:
        return self._position

    @position.setter
    def position(self, start_from_position: int) -> None:
        self._position = start_from_position
        for session in self._server_sessions:
            assert isinstance(session, _ServerInferenceSession)
            session.position = start_from_position

    def _enter_server_sessions(self, chosen_spans: List[RemoteSpanInfo]) -> List[_ServerInferenceSession]:
        server_sessions = []
        try:
            for span in chosen_spans:
                span_uids = CHAIN_DELIMITER.join(self._sequence_manager.block_uids[span.start : span.end])
                metadata = self._sequence_manager.get_request_metadata("rpc_inference", span_uids, peer_id=span.peer_id)
                session = RemoteExpertWorker.run_coroutine(
                    _ServerInferenceSession.create(
                        self._sequence_manager.config,
                        self._sequence_manager.state.p2p,
                        span,
                        span_uids,
                        rpc_info=self._sequence_manager.rpc_info,
                        max_length=self._max_length,
                        **metadata,
                    )
                )
                server_sessions.append(session)
                session.__enter__()
            return server_sessions
        except:
            self._exit_server_sessions(server_sessions)
            raise

    def _exit_server_sessions(self, server_sessions: List[_ServerInferenceSession]) -> None:
        for session in reversed(server_sessions):
            try:
                session.__exit__(None, None, None)
            except Exception:
                logger.debug("Caught exception while closing connection to server:", exc_info=True)

    def __enter__(self) -> "InferenceSession":
        assert not self._closed and not self._server_sessions
        return self

    def step(
        self,
        inputs: torch.Tensor,
        prompts: Optional[torch.Tensor] = None,
        hypo_ids: Optional[torch.Tensor] = None,
        per_layer_inputs: Optional[torch.Tensor] = None,
    ) -> torch.Tensor:
        assert not self._closed
        if torch.is_grad_enabled():
            logger.warning("Running inference session with grad enabled. Gradients will *not* be propagated correctly.")

        if prompts is None or is_dummy(prompts):
            prompts = DUMMY
        else:
            assert prompts.ndim == 4, "deep prompts should have shape [num_blocks, batch_size, prefix_len, hid_size]"
            assert prompts.shape[0] == self.num_blocks
            assert prompts.shape[1] in (inputs.shape[0], 1)
            assert prompts.shape[2] <= inputs.shape[1]
            assert prompts.shape[3] == inputs.shape[2]

        if per_layer_inputs is None or is_dummy(per_layer_inputs):
            per_layer_inputs = DUMMY
        else:
            assert (
                per_layer_inputs.ndim == 4
            ), "per_layer_inputs should have shape [num_blocks, batch_size, seq_len, per_layer_dim]"
            assert per_layer_inputs.shape[0] == self.num_blocks
            assert per_layer_inputs.shape[1] in (inputs.shape[0], 1)
            assert per_layer_inputs.shape[2] == inputs.shape[1]

        if hypo_ids is None or is_dummy(hypo_ids):
            hypo_ids = DUMMY_INT64
        else:
            assert len(hypo_ids) == len(inputs)
            assert hypo_ids.dtype == torch.int64

        inputs_device = inputs.device
        inputs_dtype = inputs.dtype
        inputs = inputs.cpu()
        prompts = prompts.cpu()
        per_layer_inputs = per_layer_inputs.cpu()
        hypo_ids = hypo_ids.cpu()
        step_id = str(uuid.uuid4())

        n_input_tokens = inputs.shape[1]
        if self._position + n_input_tokens > self._max_length:
            raise ValueError(
                f"Maximum length exceeded: prefix {self._position} + current {n_input_tokens} exceeds pre-allocated maximum {self._max_length}"
            )

        # Gemma 4 KV sharing: donor keys/values produced by one span feed the consumer layers of
        # downstream spans. Accumulate them across spans within this step; each span emits only the
        # donors it newly produced (one donor per layer type in the whole model, so no re-emission).
        accumulated_shared_kv: Dict[str, Tuple[torch.Tensor, torch.Tensor]] = {}

        server_idx = 0
        block_idx = 0
        recovery_until: Optional[int] = None
        while block_idx < self.num_blocks:
            for attempt_no in itertools.count():
                logger.debug(f"Inference: block {block_idx}, attempt {attempt_no}")
                server_session = None
                try:
                    if not self._server_sessions or attempt_no >= 1:
                        replay_until = self._update_sequence(
                            server_idx,
                            block_idx,
                            attempt_no,
                            target_position=self._position + n_input_tokens,
                        )
                        if replay_until is not None:
                            recovery_until = (
                                replay_until if recovery_until is None else max(recovery_until, replay_until)
                            )

                    server_session = self._server_sessions[server_idx]
                    expected_position = 0 if server_session.needs_replay else self.position
                    assert (
                        server_session.position == expected_position
                    ), f"{server_session.position} and {expected_position}"
                    span_per_layer_inputs = (
                        per_layer_inputs
                        if is_dummy(per_layer_inputs)
                        else per_layer_inputs[server_session.span.start : server_session.span.end]
                    )
                    inputs, produced_shared_kv = server_session.step(
                        inputs,
                        prompts[server_session.span.start : server_session.span.end],
                        hypo_ids,
                        step_id=step_id,
                        per_layer_inputs=span_per_layer_inputs,
                        shared_kv_states=accumulated_shared_kv,
                    )
                    accumulated_shared_kv.update(produced_shared_kv)

                    server_idx += 1
                    block_idx = server_session.span.end
                    if recovery_until is not None and block_idx >= recovery_until:
                        assert block_idx == recovery_until
                        inputs = inputs[:, -n_input_tokens:]
                        recovery_until = None
                    self._sequence_manager.on_request_success(server_session.span.peer_id)
                    break
                except Exception as e:
                    if isinstance(e, NotImplementedError):
                        raise
                    self._sequence_manager.on_request_failure(
                        server_session.span.peer_id if server_session is not None else None
                    )
                    if attempt_no + 1 == self._sequence_manager.config.max_retries:
                        raise
                    delay = self._sequence_manager.get_retry_delay(attempt_no)
                    logger.warning(
                        f"Caught exception when running inference via {server_session.span if server_session is not None else None} "
                        f"(retry in {delay:.0f} sec): {repr(e)}"
                    )
                    maybe_log_traceback(e)
                    time.sleep(delay)

        self._position += n_input_tokens
        outputs = inputs[:, -n_input_tokens:]
        outputs = outputs.to(device=inputs_device, dtype=inputs_dtype)
        return outputs

    def _update_sequence(
        self, server_idx: int, block_idx: int, attempt_no: int, *, target_position: int
    ) -> Optional[int]:
        # If there is a failed server session, this code closes it
        n_prev_spans = len(self._server_sessions)
        previous_session = self._server_sessions[server_idx] if server_idx < n_prev_spans else None
        previous_span = previous_session.span if previous_session is not None else None
        self._exit_server_sessions(self._server_sessions[server_idx : server_idx + 1])

        update_end = previous_span.end if previous_span is not None else self.num_blocks
        if attempt_no >= 1:
            logger.debug(
                f"Due to a server failure, remote attention caches "
                f"from block {block_idx} to {update_end} will be regenerated"
            )

        updated_spans = self._sequence_manager.make_sequence(
            block_idx, update_end, mode="min_latency", cache_tokens_needed=self._max_length
        )
        # make_sequence() could return a longer sequence
        updated_spans[-1].end = min(updated_spans[-1].end, update_end)
        updated_sessions = self._enter_server_sessions(updated_spans)
        logger.debug(f"Found path from block {block_idx} to {update_end} via {len(updated_spans)} servers")

        # If there is a failed span, this code replaces it, otherwise it just adds new ones
        replay_until = None
        if previous_session is not None and previous_session.history is not None and target_position > 0:
            assert previous_span is not None
            history_tokens = previous_session.history.shape[1]
            if not self._position <= history_tokens <= target_position:
                raise RuntimeError(
                    f"Cannot restore failed span {previous_span}: activation history has {history_tokens} tokens, "
                    f"expected between {self._position} and {target_position}"
                )

            try:
                for updated_index, updated_session in enumerate(updated_sessions):
                    assert (
                        previous_span.start
                        <= updated_session.span.start
                        < updated_session.span.end
                        <= previous_span.end
                    )
                    relative_start = updated_session.span.start - previous_span.start
                    relative_end = updated_session.span.end - previous_span.start
                    per_layer_history = (
                        previous_session.per_layer_history[relative_start:relative_end]
                        if previous_session.per_layer_history is not None
                        else None
                    )
                    replay_prompts = (
                        previous_session.replay_prompts[relative_start:relative_end]
                        if previous_session.replay_prompts is not None
                        else None
                    )
                    updated_session.prepare_for_replay(
                        history=previous_session.history if updated_index == 0 else None,
                        per_layer_history=per_layer_history,
                        prompts=replay_prompts,
                        target_position=target_position,
                    )
            except Exception:
                self._exit_server_sessions(updated_sessions)
                raise
            replay_until = update_end
            logger.info(
                f"Replaying {target_position} cached activation tokens through replacement blocks "
                f"{block_idx}:{update_end}"
            )
        self._server_sessions[server_idx : server_idx + 1] = updated_sessions

        # Update links to the next server session for direct server-to-server communication via rpc_push()
        for i in range(max(server_idx - 1, 0), min(server_idx + len(updated_spans), len(self._server_sessions) - 1)):
            self._server_sessions[i].next_session = self._server_sessions[i + 1]

        return replay_until

    def close(self, *exc_details):
        """Finish a given inference session, close the underlying connection"""
        if not self._closed:
            self._exit_server_sessions(self._server_sessions)
            self._server_sessions.clear()
            self._closed = True

    def __exit__(self, *exc_details):
        self.close(*exc_details)

    def __del__(self):
        self.close()

    @property
    def last_token_id(self) -> Optional[torch.Tensor]:  # Backward compatibility with DRIFT-LLM < 2.1.0
        return self.output_ids[:, -1:] if self.output_ids is not None else None

    @last_token_id.setter
    def last_token_id(self, value: torch.Tensor):  # Backward compatibility with DRIFT-LLM < 2.1.0
        if self.output_ids is None:
            raise RuntimeError("Can't override `last_token_id` since the session has not stepped yet")
        self.output_ids[:, -1:] = value
