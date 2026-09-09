"""Authenticated discovery and text-only inference over the public peer transport.

Consumers never construct a tokenizer, tensor model or artifact downloader here.
Text peers own the input/output weights and route generation through block peers.
"""

import asyncio
import contextlib
import json
import logging
import time
import uuid
from typing import AsyncIterator

from hivemind import DHT
from hivemind.p2p import P2PContext, PeerID, ServicerBase
from hivemind.proto import runtime_pb2

from drift.protocol_identity import TRANSPORT_SECURITY, ProtocolSecurityError, SignedRecord, _validate_lifetime

MAX_REQUEST_BYTES = 128 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
ANNOUNCEMENT_TTL = 40
logger = logging.getLogger(__name__)


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


class TextPeerClient:
    """One lightweight consumer. Retry another peer only before receiving answer text."""

    def __init__(self, manifest, *, initial_peers, revocations=None, request_timeout=30, total_timeout=900, dht=None):
        self.manifest = manifest
        self.revocations = revocations
        self.request_timeout = max(15, min(request_timeout, 120))
        self.total_timeout = min(total_timeout, 900)
        self.dht = (
            dht
            if dht is not None
            else DHT(initial_peers=list(initial_peers), client_mode=True, start=True, tls=True, startup_timeout=30)
        )
        self._owns_dht = dht is None

    async def stream(self, body, *, chat):
        request_id = uuid.uuid4().hex
        payload = encode({"request_id": request_id, "chat": chat, "body": body}, MAX_REQUEST_BYTES)
        peers = await asyncio.to_thread(discover_text_peers, self.dht, self.manifest, revocations=self.revocations)
        if not peers:
            raise TextPeerUnavailable("No community peer is ready to answer yet. Please try again shortly.")
        p2p = await self.dht.replicate_p2p()
        deadline = time.monotonic() + self.total_timeout
        try:
            for candidate in peers[:3]:
                emitted, complete, received = False, False, 0
                stub = TextPeerProtocol.get_stub(p2p, PeerID.from_base58(candidate["peer_id"]))
                responses = None
                try:
                    responses = await asyncio.wait_for(
                        stub.rpc_generate(runtime_pb2.ExpertRequest(uid=self.manifest.digest_id, metadata=payload)),
                        self.request_timeout,
                    )
                    while True:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("Community answer exceeded its time limit")
                        response = await asyncio.wait_for(anext(responses), min(self.request_timeout, remaining))
                        received += len(response.metadata)
                        if response.tensors or received > MAX_RESPONSE_BYTES:
                            raise ValueError("Invalid community response")
                        frame = decode(response.metadata)
                        if frame.get("manifest_digest") != self.manifest.digest_id:
                            raise ProtocolSecurityError("Community response model mismatch")
                        if frame.get("type") == "error":
                            if frame.get("code") == "invalid_request":
                                raise ValueError(str(frame.get("message", "Invalid request"))[:256])
                            raise TextPeerUnavailable(str(frame.get("message", "Community peer unavailable"))[:256])
                        if frame.get("type") == "delta":
                            if not isinstance(frame.get("text"), str):
                                raise ValueError("Invalid community text")
                            emitted = emitted or bool(frame["text"])
                        elif frame.get("type") == "done":
                            usage = frame.get("usage", {})
                            if (
                                frame.get("finish_reason") not in ("stop", "length")
                                or not isinstance(usage, dict)
                                or any(
                                    type(usage.get(k)) is not int or not 0 <= usage[k] <= 1048576
                                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                                )
                            ):
                                raise ValueError("Invalid community usage")
                            complete = True
                        elif frame.get("type") != "heartbeat":
                            raise ValueError("Invalid community response type")
                        yield frame
                        if complete:
                            return
                except (ValueError, ProtocolSecurityError):
                    raise
                except Exception as exc:
                    if emitted:
                        raise TextPeerUnavailable(
                            "The community connection stopped during the answer. Please retry."
                        ) from exc
                finally:
                    if not complete:
                        try:
                            # Use the same stream transport as generation, including
                            # on desktop daemons that do not support unary handlers.
                            async with asyncio.timeout(3):
                                cancelled = await stub.rpc_cancel(
                                    runtime_pb2.ExpertRequest(
                                        uid=self.manifest.digest_id, metadata=encode({"request_id": request_id})
                                    )
                                )
                                try:
                                    await anext(cancelled)
                                finally:
                                    await cancelled.aclose()
                        except Exception as exc:
                            logger.warning("Could not cancel community generation: %s", exc)
                    if responses is not None:
                        with contextlib.suppress(Exception):
                            await responses.aclose()
            raise TextPeerUnavailable("Community peers are busy or unreachable. Please try again shortly.")
        finally:
            await p2p.shutdown()

    def close(self):
        if self._owns_dht and self.dht.is_alive():
            self.dht.shutdown()
