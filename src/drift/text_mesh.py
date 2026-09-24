"""Authenticated discovery and text-only inference over the public peer transport.

Consumers never construct a tokenizer, tensor model or artifact downloader here.
Text peers own the input/output weights and route generation through block peers.
"""

import asyncio
import json
import logging
import time
import uuid
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from hivemind.p2p import P2PContext, PeerID, ServicerBase
from hivemind.proto import runtime_pb2

from drift.inference_provider import ProviderContractError, Usage
from drift.protocol_identity import TRANSPORT_SECURITY, ProtocolSecurityError, SignedRecord, _validate_lifetime
from drift.text_request import RequestContext, RequestDeadlineExceeded, retain_request_task
from drift.utils.client_dht import create_client_dht

MAX_REQUEST_BYTES = 128 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 1024 * 1024
MAX_RESPONSE_FRAMES = 4096
CLEANUP_SECONDS = 3.0
SHUTDOWN_SECONDS = 3.0
ANNOUNCEMENT_TTL = 40
logger = logging.getLogger(__name__)
_T = TypeVar("_T")
_MISSING = object()


def _preserve_cleanup(_result) -> None:
    """Mark a resource release as owed even after observation stops."""


def encode(value, limit=MAX_FRAME_BYTES):
    data = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > limit:
        raise ValueError("Text request or response is too large")
    return data


def decode(data, limit=MAX_FRAME_BYTES):
    if len(data) > limit:
        raise ValueError("Text request or response is too large")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate field")
            result[key] = value
        return result

    def invalid(_value):
        raise ValueError("Non-finite value")

    result = json.loads(data, object_pairs_hook=pairs, parse_constant=invalid)
    if not isinstance(result, dict):
        raise ValueError("Expected a text protocol object")
    return result


def announcement_key(manifest):
    return f"{manifest.dht_prefix}.text-v1"


def create_text_announcement(manifest, identity, *, max_context_tokens, max_output_tokens, now=None):
    now = time.time() if now is None else now
    return SignedRecord.create(
        "text_peer_announcement",
        {
            "peer_id": str(identity.peer_id),
            "manifest_digest": manifest.digest,
            "dht_prefix": manifest.dht_prefix,
            "execution_profile": manifest.runtime.to_dict(),
            "transport_security": TRANSPORT_SECURITY,
            "protocol": "text-v1",
            "max_context_tokens": max_context_tokens,
            "max_output_tokens": max_output_tokens,
            "issued_at_ms": int(now * 1000),
            "expires_at_ms": int((now + ANNOUNCEMENT_TTL) * 1000),
            "sequence": time.time_ns(),
        },
        identity,
    )


def verify_text_announcement(source, manifest, peer_id, *, revocations=None, replay_guard=None, now=None):
    # Bound before RSA/JSON work, including already decoded DHT values.
    encode(source, 16 * 1024)
    record = SignedRecord.from_dict(source)
    record.verify(expected_kind="text_peer_announcement")
    p = record.payload
    if str(record.peer_id) != str(peer_id) or p.get("peer_id") != str(peer_id):
        raise ProtocolSecurityError("Text peer identity mismatch")
    expected = {
        "manifest_digest": manifest.digest,
        "dht_prefix": manifest.dht_prefix,
        "execution_profile": manifest.runtime.to_dict(),
        "protocol": "text-v1",
        "transport_security": TRANSPORT_SECURITY,
    }
    if any(p.get(key) != value for key, value in expected.items()):
        raise ProtocolSecurityError("Text peer model or protocol mismatch")
    for field in ("max_context_tokens", "max_output_tokens"):
        if type(p.get(field)) is not int or not 1 <= p[field] <= 262144:
            raise ProtocolSecurityError("Invalid text peer capacity")
    if type(p.get("sequence")) is not int or p["sequence"] < 0:
        raise ProtocolSecurityError("Invalid text peer sequence")
    _validate_lifetime(p, now=now)
    if revocations is not None:
        revocations.require_active(record.key_id)
    if replay_guard is not None:
        replay_guard.check(record)
    return record


def discover_text_peers(dht, manifest, *, revocations=None, replay_guard=None):
    found = dht.get(announcement_key(manifest), latest=True)
    values = getattr(found, "value", None)
    if not isinstance(values, dict) or len(values) > 256:
        return []
    peers = []
    for peer_id, wrapped in list(values.items())[:256]:
        try:
            record = verify_text_announcement(
                getattr(wrapped, "value", wrapped),
                manifest,
                peer_id,
                revocations=revocations,
                replay_guard=replay_guard,
            )
            peers.append(dict(record.payload))
        except (ValueError, TypeError, KeyError, ProtocolSecurityError):
            continue
    return sorted(peers, key=lambda p: p["peer_id"])


class TextPeerProtocol(ServicerBase):
    def __init__(self, manifest=None, engine=None):
        self.manifest, self.engine = manifest, engine

    async def rpc_generate(
        self, request: runtime_pb2.ExpertRequest, context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertResponse]:
        if request.uid != self.manifest.digest_id or request.tensors:
            raise ValueError("Text request model mismatch")
        body = decode(request.metadata, MAX_REQUEST_BYTES)
        frames = self.engine.stream(body, str(context.remote_id))
        try:
            async for frame in frames:
                yield runtime_pb2.ExpertResponse(metadata=encode(dict(frame, manifest_digest=self.manifest.digest_id)))
        finally:
            await frames.aclose()

    async def rpc_cancel(
        self, request: runtime_pb2.ExpertRequest, context: P2PContext
    ) -> AsyncIterator[runtime_pb2.ExpertResponse]:
        if request.uid != self.manifest.digest_id or request.tensors:
            raise ValueError("Text request model mismatch")
        body = decode(request.metadata, 1024)
        cancelled = self.engine.cancel(body.get("request_id"), str(context.remote_id))
        yield runtime_pb2.ExpertResponse(metadata=encode({"cancelled": cancelled}))


class TextPeerUnavailable(RuntimeError):
    pass


class TextPeerMalformedResponse(TextPeerUnavailable):
    """A peer response failed the authenticated legacy protocol checks."""


class _TextPeerRejectedRequest(ValueError):
    pass


class _RequestWork:
    """Keep one client's external work fenced until every owned task settles."""

    def __init__(self, gate):
        self._gate = gate
        self._loop = asyncio.get_running_loop()
        self._tasks = {}
        self._active = False
        self._sealed = False
        self._poisoned = False
        self._release_scheduled = False
        self._released = False

    def activate(self) -> None:
        if self._active:
            raise RuntimeError("Request work already activated")
        self._active = True

    def own(self, operation):
        task = asyncio.ensure_future(operation)
        if task in self._tasks:
            return task
        if self._released:
            raise RuntimeError("Request work already released")
        retain_request_task(task)
        self._tasks[task] = ["pending", None, False]
        task.add_done_callback(self._completed)
        return task

    def claim(self, task) -> None:
        self._dispose(task, "claimed", None)

    def abandon(self, task, handler=None, *, poison_on_failure=False) -> None:
        self._dispose(task, "abandoned", handler, poison_on_failure)

    def _dispose(self, task, state, handler, poison_on_failure=False) -> None:
        entry = self._tasks.get(task)
        if entry is None or entry[0] != "pending":
            raise RuntimeError("Invalid request task disposition")
        entry[:] = [state, handler, poison_on_failure]
        self._settle(task)

    def _completed(self, task) -> None:
        self._settle(task)

    def _settle(self, task) -> None:
        entry = self._tasks.get(task)
        if entry is None or entry[0] == "pending" or not task.done():
            return
        state, handler, poison_on_failure = entry
        if state == "abandoned":
            try:
                value = task.result()
            except BaseException:
                if poison_on_failure:
                    self.poison()
            else:
                if handler is not None:
                    try:
                        # The handler must synchronously own any descendant before
                        # this parent is removed, preventing a transient zero count.
                        handler(value)
                    except BaseException:
                        self.poison()
        del self._tasks[task]
        self._defer_release()

    def poison(self) -> None:
        self._poisoned = True

    def seal(self) -> None:
        if not self._active or self._sealed:
            raise RuntimeError("Invalid request work seal")
        self._sealed = True
        self._defer_release()

    def _defer_release(self) -> None:
        if self._release_scheduled:
            return
        self._release_scheduled = True
        self._loop.call_soon(self._maybe_release)

    def _maybe_release(self) -> None:
        self._release_scheduled = False
        if self._active and self._sealed and not self._tasks and not self._poisoned and not self._released:
            self._released = True
            self._gate.release()


class TextPeerClient:
    """One lightweight consumer. Retry another peer only before receiving answer text."""

    supports_request_context = True

    def __init__(self, manifest, *, initial_peers, revocations=None, request_timeout=30, total_timeout=900, dht=None):
        self.manifest = manifest
        self.revocations = revocations
        self.request_timeout = max(15, min(request_timeout, 120))
        self.total_timeout = min(total_timeout, 900)
        self.dht = (
            dht
            if dht is not None
            else create_client_dht(initial_peers=initial_peers, client_mode=True, tls=True, startup_timeout=30)
        )
        self._owns_dht = dht is None
        # This bounds local request-owned producers per client. Recreating a
        # client creates a new gate; this is not process-global stop evidence.
        self._request_gate = asyncio.Semaphore(1)

    def _track_cleanup(self, task, work=None):
        return retain_request_task(task) if work is None else work.own(task)

    async def _run_owned(
        self,
        context,
        work,
        operation,
        *,
        cap,
        preserve=False,
        on_abandoned=None,
    ):
        task = work.own(operation)
        try:
            result = await context.run(
                task,
                cap=cap,
                on_abandoned=_preserve_cleanup if preserve else None,
            )
        except BaseException:
            work.abandon(task, on_abandoned)
            raise
        work.claim(task)
        return result

    async def _bounded_cleanup(
        self,
        operation: Awaitable[_T],
        deadline: float,
        *,
        on_abandoned: Callable[[_T], None] | None = None,
        work=None,
        poison_on_failure=False,
    ):
        """Observe one cleanup operation without losing a cancellation-resistant task."""
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            if asyncio.isfuture(operation):
                task = self._track_cleanup(asyncio.ensure_future(operation), work)
                if work is not None:
                    if on_abandoned is None:
                        task.cancel()
                    work.abandon(task, on_abandoned, poison_on_failure=poison_on_failure)
                elif on_abandoned is not None:

                    def release_late(completed):
                        try:
                            on_abandoned(completed.result())
                        except BaseException:
                            pass

                    task.add_done_callback(release_late)
                else:
                    task.cancel()
            elif asyncio.iscoroutine(operation):
                operation.close()
            return _MISSING
        task = self._track_cleanup(asyncio.ensure_future(operation), work)

        def abandon() -> None:
            if work is not None:
                if on_abandoned is None:
                    task.cancel()
                work.abandon(task, on_abandoned, poison_on_failure=poison_on_failure)
            elif on_abandoned is not None:

                def release_late(completed):
                    try:
                        on_abandoned(completed.result())
                    except BaseException:
                        pass

                task.add_done_callback(release_late)
            else:
                task.cancel()

        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
        except BaseException:
            abandon()
            raise
        if done:
            if work is not None:
                work.claim(task)
            return task.result()
        abandon()
        return _MISSING

    def _schedule_bounded_cleanup(self, operation: Awaitable, seconds: float, work=None) -> None:
        async def cleanup():
            try:
                # Own the effect before calculating its observation deadline.
                owned = self._track_cleanup(asyncio.ensure_future(operation), work)
                deadline = asyncio.get_running_loop().time() + seconds
                await self._bounded_cleanup(
                    owned,
                    deadline,
                    on_abandoned=_preserve_cleanup,
                    work=work,
                    poison_on_failure=True,
                )
            except BaseException:
                if work is not None:
                    work.poison()

        try:
            task = self._track_cleanup(asyncio.create_task(cleanup()), work)
            if work is not None:
                work.abandon(task, poison_on_failure=True)
        except RuntimeError:
            if work is not None:
                work.poison()
            if asyncio.iscoroutine(operation):
                operation.close()

    def _abandon_stream(self, stream, work=None) -> None:
        try:
            operation = stream.aclose()
        except BaseException:
            if work is not None:
                work.poison()
            return
        self._schedule_bounded_cleanup(operation, CLEANUP_SECONDS, work)

    def _abandon_p2p(self, p2p, work=None) -> None:
        try:
            operation = p2p.shutdown()
        except BaseException:
            if work is not None:
                work.poison()
            return
        self._schedule_bounded_cleanup(operation, SHUTDOWN_SECONDS, work)

    def _schedule_close_after_task(self, stream, pending, work=None) -> None:
        async def close_when_idle():
            try:
                await pending
            except BaseException:
                pass
            await self._close_stream(
                stream,
                asyncio.get_running_loop().time() + CLEANUP_SECONDS,
                work,
            )

        task = self._track_cleanup(asyncio.create_task(close_when_idle()), work)
        if work is not None:
            work.abandon(task, poison_on_failure=True)

    async def _close_stream(self, stream, deadline: float, work=None) -> None:
        if stream is None:
            return
        try:
            # The close itself is owed. Only observation is bounded: timeout,
            # an expired budget, or caller cancellation must not cancel it.
            operation = asyncio.ensure_future(stream.aclose())
            await self._bounded_cleanup(
                operation,
                deadline,
                on_abandoned=_preserve_cleanup,
                work=work,
                poison_on_failure=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if work is not None:
                work.poison()
            logger.warning("Could not close community stream (%s)", type(exc).__name__)

    async def _close_after_pending(self, stream, pending, deadline: float, work=None) -> None:
        if stream is None:
            return
        if pending is not None and not pending.done():
            # RequestContext.run or _bounded_cleanup may already have delivered
            # cancellation. A second cancel can defeat a producer that is still
            # unwinding its first one and make resource release unobservable.
            remaining = deadline - asyncio.get_running_loop().time()
            try:
                done, _pending = await asyncio.wait({pending}, timeout=max(0, remaining))
            except BaseException:
                self._schedule_close_after_task(stream, pending, work)
                raise
            if not done:
                self._schedule_close_after_task(stream, pending, work)
                return
        if pending is not None:
            try:
                pending.result()
            except BaseException:
                pass
        await self._close_stream(stream, deadline, work)

    async def _cancel_attempt(self, stub, request_id: str, deadline: float, work=None) -> None:
        cancelled = None
        receipt_task = None
        try:
            cancelled = await self._bounded_cleanup(
                stub.rpc_cancel(
                    runtime_pb2.ExpertRequest(
                        uid=self.manifest.digest_id,
                        metadata=encode({"request_id": request_id}),
                    )
                ),
                deadline,
                on_abandoned=lambda stream: self._abandon_stream(stream, work),
                work=work,
            )
            if cancelled is _MISSING:
                return
            # A cancel response is only a transport receipt. It is deliberately
            # ignored and never presented as proof that execution stopped.
            receipt_task = self._track_cleanup(asyncio.ensure_future(anext(cancelled)), work)
            await self._bounded_cleanup(receipt_task, deadline, work=work)
        except (StopAsyncIteration, GeneratorExit):
            pass
        except Exception as exc:
            logger.warning("Could not cancel community generation (%s)", type(exc).__name__)
        finally:
            if cancelled not in (None, _MISSING):
                await self._close_after_pending(cancelled, receipt_task, deadline, work)

    async def _cleanup_attempt(self, stub, request_id: str, responses, response_task, work=None) -> None:
        deadline = asyncio.get_running_loop().time() + CLEANUP_SECONDS
        await asyncio.gather(
            self._cancel_attempt(stub, request_id, deadline, work),
            self._close_after_pending(responses, response_task, deadline, work),
            return_exceptions=True,
        )

    async def _shutdown(self, p2p, work=None) -> None:
        deadline = asyncio.get_running_loop().time() + SHUTDOWN_SECONDS
        try:
            operation = self._track_cleanup(asyncio.ensure_future(p2p.shutdown()), work)
            await self._bounded_cleanup(
                operation,
                deadline,
                on_abandoned=_preserve_cleanup,
                work=work,
                poison_on_failure=True,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if work is not None:
                work.poison()
            logger.warning("Could not close community transport (%s)", type(exc).__name__)

    @staticmethod
    def _requested_output_limit(body, chat):
        if type(body) is not dict:
            return None
        result = body.get("max_tokens")
        if chat and result is None:
            result = body.get("max_completion_tokens")
        return result if type(result) is int and result >= 0 else None

    def _validate_usage(self, frame, candidate, request_limit):
        usage = frame.get("usage")
        if frame.get("finish_reason") not in ("stop", "length") or type(usage) is not dict:
            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
        try:
            checked = Usage(
                input_units=usage.get("prompt_tokens"),
                output_units=usage.get("completion_tokens"),
                total_units=usage.get("total_tokens"),
            )
        except (ProviderContractError, TypeError):
            raise TextPeerMalformedResponse("The community peer sent an invalid response.") from None
        if (
            checked.output_units > candidate["max_output_tokens"]
            or checked.total_units > candidate["max_context_tokens"]
            or (request_limit is not None and checked.output_units > request_limit)
        ):
            raise TextPeerMalformedResponse("The community peer sent an invalid response.")

    async def stream(self, body, *, chat, context=None):
        if context is None:
            context = RequestContext.start(self.total_timeout)
        elif type(context) is not RequestContext:
            raise ValueError("Invalid request context")
        context.require_live()
        # Encoding before discovery is part of the same caller-owned deadline.
        encode({"request_id": "0" * 32, "chat": chat, "body": body}, MAX_REQUEST_BYTES)
        work = _RequestWork(self._request_gate)
        await context.acquire(self._request_gate)
        work.activate()
        p2p = None
        try:
            try:
                peers = await self._run_owned(
                    context,
                    work,
                    asyncio.to_thread(
                        discover_text_peers,
                        self.dht,
                        self.manifest,
                        revocations=self.revocations,
                    ),
                    cap=self.request_timeout,
                    preserve=True,
                    on_abandoned=_preserve_cleanup,
                )
            except (RequestDeadlineExceeded, asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:
                logger.warning("Community peer discovery failed (%s)", type(exc).__name__)
                raise TextPeerUnavailable("Could not discover a community peer. Please try again shortly.") from None
            if not peers:
                raise TextPeerUnavailable("No community peer is ready to answer yet. Please try again shortly.")
            try:
                p2p = await self._run_owned(
                    context,
                    work,
                    self.dht.replicate_p2p(),
                    cap=self.request_timeout,
                    preserve=True,
                    on_abandoned=lambda value: self._abandon_p2p(value, work),
                )
            except (RequestDeadlineExceeded, asyncio.CancelledError, GeneratorExit):
                raise
            except Exception as exc:
                logger.warning("Community peer transport failed (%s)", type(exc).__name__)
                raise TextPeerUnavailable("Could not connect to a community peer. Please try again shortly.") from None
            last_peer_error = None
            request_limit = self._requested_output_limit(body, chat)
            for candidate in peers[:3]:
                context.require_live()
                request_id = uuid.uuid4().hex
                payload = encode({"request_id": request_id, "chat": chat, "body": body}, MAX_REQUEST_BYTES)
                emitted, complete = False, False
                received = output_bytes = frame_count = 0
                stub = TextPeerProtocol.get_stub(p2p, PeerID.from_base58(candidate["peer_id"]))
                responses = None
                response_task = None
                try:
                    responses = await self._run_owned(
                        context,
                        work,
                        stub.rpc_generate(runtime_pb2.ExpertRequest(uid=self.manifest.digest_id, metadata=payload)),
                        cap=self.request_timeout,
                        preserve=True,
                        on_abandoned=lambda value: self._abandon_stream(value, work),
                    )
                    while True:
                        response_task = work.own(anext(responses))
                        response = await self._run_owned(
                            context,
                            work,
                            response_task,
                            cap=self.request_timeout,
                        )
                        response_task = None
                        if type(response.metadata) is not bytes:
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                        received += len(response.metadata)
                        frame_count += 1
                        if response.tensors or received > MAX_RESPONSE_BYTES or frame_count > MAX_RESPONSE_FRAMES:
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                        try:
                            frame = decode(response.metadata)
                        except (TypeError, ValueError, UnicodeError):
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.") from None
                        if frame.get("manifest_digest") != self.manifest.digest_id:
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                        kind = frame.get("type")
                        if kind == "error":
                            if type(frame.get("message")) is not str:
                                raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                            if frame.get("code") == "invalid_request":
                                raise _TextPeerRejectedRequest("The community peer rejected this request.")
                            if frame.get("code") == "busy":
                                raise TextPeerUnavailable("This community peer is busy")
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                        if kind == "delta":
                            if type(frame.get("text")) is not str:
                                raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                            try:
                                output_bytes += len(frame["text"].encode("utf-8"))
                            except UnicodeEncodeError:
                                raise TextPeerMalformedResponse(
                                    "The community peer sent an invalid response."
                                ) from None
                            if output_bytes > MAX_OUTPUT_BYTES:
                                raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                            emitted = emitted or bool(frame["text"])
                        elif kind == "done":
                            self._validate_usage(frame, candidate, request_limit)
                            complete = True
                        elif kind != "heartbeat":
                            raise TextPeerMalformedResponse("The community peer sent an invalid response.")
                        yield frame
                        if complete:
                            return
                except (RequestDeadlineExceeded, asyncio.CancelledError, GeneratorExit):
                    raise
                except TextPeerMalformedResponse:
                    raise
                except _TextPeerRejectedRequest:
                    raise
                except Exception as exc:
                    if emitted:
                        raise TextPeerUnavailable(
                            "The community connection stopped during the answer. Please retry."
                        ) from None
                    if isinstance(exc, TextPeerUnavailable):
                        last_peer_error = exc
                    else:
                        logger.warning("Community peer request failed (%s)", type(exc).__name__)
                finally:
                    if not complete:
                        await self._cleanup_attempt(stub, request_id, responses, response_task, work)
                    elif responses is not None:
                        await self._close_stream(
                            responses,
                            asyncio.get_running_loop().time() + CLEANUP_SECONDS,
                            work,
                        )
            if last_peer_error is not None:
                raise last_peer_error
            raise TextPeerUnavailable("Could not connect to a community peer. Please try again shortly.")
        finally:
            try:
                if p2p is not None:
                    await self._shutdown(p2p, work)
            finally:
                work.seal()

    def close(self):
        if self._owns_dht and self.dht.is_alive():
            self.dht.shutdown()
