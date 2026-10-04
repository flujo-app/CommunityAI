"""Opt-in Factory receiver observation; provenance belongs to the embedding host.

These values describe what this ASGI app received. A reverse proxy may have
changed the sender's method, path, headers, or body. Neither this observation
nor an HTTP bearer authenticates an Original request. The host must compare it
with a separately authenticated sender commitment and receiver generation.
"""

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

from drift.factory_admission import FactoryAdmission, RequestAdmissionDenied, request_body_digest

MAX_FACTORY_RAW_BODY_BYTES = 32_768
MAX_FACTORY_PROJECTED_HEADER_BYTES = 4096
MAX_FACTORY_TOTAL_HEADER_BYTES = 16_384
MAX_FACTORY_HEADER_COUNT = 64
_BODY_TOO_LARGE_RESPONSE = b'{"detail":"Factory ingress body too large"}'
_PROJECTED_HEADERS = frozenset(
    {
        "accept",
        "content-type",
        "http-referer",
        "openai-organization",
        "openai-project",
        "user-agent",
        "x-title",
        "x-stainless-arch",
        "x-stainless-lang",
        "x-stainless-os",
        "x-stainless-package-version",
        "x-stainless-retry-count",
        "x-stainless-runtime",
        "x-stainless-runtime-version",
    }
)


async def _send_body_too_large(send):
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(_BODY_TOO_LARGE_RESPONSE)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": _BODY_TOO_LARGE_RESPONSE})


class FactoryIngressTooLarge(RequestAdmissionDenied):
    pass


class FactoryIngressHeadersInvalid(RequestAdmissionDenied):
    pass


class FactoryBoundedBodyMiddleware:
    """Cap a v2 ASGI request before FastAPI/Pydantic can parse its body.

    ASGI delivers whole frames, so a single oversized frame is transiently
    owned by the server. This middleware never retains it and stops requesting
    frames at the first overflow. The deployment edge must bound frame size.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return await self.app(scope, receive, send)

        # A truthful Content-Length lets us refuse without asking ASGI for even
        # one body frame. A missing or false length is still caught below.
        for name, value in scope.get("headers", []):
            if type(name) is bytes and name.lower() == b"content-length" and type(value) is bytes and value.isdigit():
                digits = value.lstrip(b"0") or b"0"
                if len(digits) > 5 or int(digits) > MAX_FACTORY_RAW_BODY_BYTES:
                    await _send_body_too_large(send)
                    return

        chunks = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request" or type(message.get("body", b"")) is not bytes:
                raise RequestAdmissionDenied()
            chunk = message.get("body", b"")
            if len(chunk) > MAX_FACTORY_RAW_BODY_BYTES - size:
                await _send_body_too_large(send)
                return
            if chunk:
                chunks.append(chunk)
            size += len(chunk)
            if message.get("more_body") is not True:
                break

        bounded_body = b"".join(chunks)
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bounded_body, "more_body": False}
            return await receive()

        return await self.app(scope, replay, send)


@dataclass(frozen=True)
class HostVerifiedTransport:
    """Opaque host-issued identity after verification outside HTTP request fields.

    Construction is not verification. Only trusted host middleware may issue a
    value from a verified channel, and the Original adapter must check the
    principal, channel binding and receiver generation against its own record.
    """

    host_capability: object = field(repr=False, compare=False)
    principal_id: str
    channel_binding_sha256: str = field(repr=False)
    generation_sha256: str

    def __post_init__(self):
        if (
            type(self.host_capability) is not object
            or type(self.principal_id) is not str
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.principal_id) is None
            or type(self.channel_binding_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.channel_binding_sha256) is None
            or type(self.generation_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.generation_sha256) is None
        ):
            raise ValueError("host transport identity is malformed")


@dataclass(frozen=True)
class FactoryIngressObservationV2:
    """Bounded receiver-observed ASGI projection, not a sender-wire proof."""

    format: str
    schema_version: int
    method: str
    route: str
    headers: tuple[tuple[str, str], ...]
    raw_body: bytes = field(repr=False)
    raw_body_sha256: str
    normalized_body_sha256: str

    def digest(self) -> str:
        """Bind one receiver observation; the digest is not provenance."""
        projection = {
            "format": self.format,
            "schemaVersion": self.schema_version,
            "method": self.method,
            "route": self.route,
            "headers": self.headers,
            "rawBodyBytes": len(self.raw_body),
            "rawBodySha256": self.raw_body_sha256,
            "normalizedBodySha256": self.normalized_body_sha256,
        }
        wire = json.dumps(projection, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(wire.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class FactoryAdmissionV2:
    """Host's explicit v2 admission, bound to observation and transport.

    The host must authenticate the separate Original issuer and sender wire;
    these fields only prevent accidental reuse or downgrade of that decision.
    """

    admission: FactoryAdmission
    observation_sha256: str
    principal_id: str
    channel_binding_sha256: str
    generation_sha256: str

    def require_current(self, observation: FactoryIngressObservationV2, body: dict, transport: HostVerifiedTransport):
        if (
            type(self.admission) is not FactoryAdmission
            or type(self.observation_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.observation_sha256) is None
            or self.observation_sha256 != observation.digest()
            or self.principal_id != transport.principal_id
            or self.channel_binding_sha256 != transport.channel_binding_sha256
            or self.generation_sha256 != transport.generation_sha256
        ):
            raise RequestAdmissionDenied()
        self.admission.require_current(body)


@dataclass(frozen=True)
class FactoryReceiverProfileV2:
    """Host-owned opt-in bridge to authenticated Original admission.

    ``transport_verifier`` receives *only* trusted ASGI scope extensions, not
    request headers or bearer credentials. The host must set those extensions
    after verifying its channel and deployed generation. ``original_admission``
    authenticates the separate Original record, compares receiver-observed and
    sender-committed projections, and returns an explicit FactoryAdmissionV2.
    """

    generation_sha256: str
    host_capability: object = field(repr=False)
    transport_verifier: Callable[[Mapping[str, object]], HostVerifiedTransport] = field(repr=False)
    original_admission: Callable[[FactoryIngressObservationV2, HostVerifiedTransport], FactoryAdmissionV2] = field(
        repr=False
    )
    schema_version: int = 2

    def __post_init__(self):
        if (
            type(self.schema_version) is not int
            or self.schema_version != 2
            or type(self.host_capability) is not object
            or type(self.generation_sha256) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.generation_sha256) is None
            or not callable(self.transport_verifier)
            or not callable(self.original_admission)
        ):
            raise ValueError("Factory receiver v2 requires host verification and admission")


def observe_factory_ingress_v2(scope: dict, raw_body: bytes, normalized_body: dict, route: str):
    """Reject unbounded/ambiguous values before any trusted host callback."""
    if type(raw_body) is not bytes or len(raw_body) > MAX_FACTORY_RAW_BODY_BYTES:
        raise FactoryIngressTooLarge()
    root_path = scope.get("root_path", "")
    if type(root_path) is not str or re.fullmatch(r"(?:/[A-Za-z0-9._~-]+)*", root_path) is None:
        raise RequestAdmissionDenied()
    full_route = root_path + route
    if (
        scope.get("method") != "POST"
        or scope.get("path") != full_route
        or scope.get("raw_path") != full_route.encode("ascii")
        or scope.get("query_string", b"") != b""
    ):
        raise RequestAdmissionDenied()
    headers = scope.get("headers")
    if type(headers) is not list or len(headers) > MAX_FACTORY_HEADER_COUNT:
        raise FactoryIngressHeadersInvalid()
    projected = {}
    projected_bytes = 0
    total_header_bytes = 0
    for pair in headers:
        if type(pair) not in (tuple, list) or len(pair) != 2:
            raise FactoryIngressHeadersInvalid()
        name, value = pair
        if type(name) is not bytes or type(value) is not bytes or len(name) > 128 or len(value) > 4096:
            raise FactoryIngressHeadersInvalid()
        total_header_bytes += len(name) + len(value)
        if total_header_bytes > MAX_FACTORY_TOTAL_HEADER_BYTES:
            raise FactoryIngressHeadersInvalid()
        try:
            decoded_name = name.decode("ascii").lower()
        except UnicodeDecodeError:
            raise FactoryIngressHeadersInvalid() from None
        if re.fullmatch(r"[!#$%&'*+.^_`|~0-9a-z-]+", decoded_name) is None or b"\r" in value or b"\n" in value:
            raise FactoryIngressHeadersInvalid()
        if decoded_name not in _PROJECTED_HEADERS:
            continue
        if decoded_name in projected or len(value) > 1024:
            raise FactoryIngressHeadersInvalid()
        try:
            decoded_value = value.decode("ascii")
        except UnicodeDecodeError:
            raise FactoryIngressHeadersInvalid() from None
        if any(ord(character) < 32 or ord(character) > 126 for character in decoded_value):
            raise FactoryIngressHeadersInvalid()
        projected_bytes += len(name) + len(value)
        if projected_bytes > MAX_FACTORY_PROJECTED_HEADER_BYTES:
            raise FactoryIngressHeadersInvalid()
        projected[decoded_name] = decoded_value
    return FactoryIngressObservationV2(
        format="communityai-factory-asgi-ingress-observation",
        schema_version=2,
        method="POST",
        route=route,
        headers=tuple(sorted(projected.items())),
        raw_body=raw_body,
        raw_body_sha256=hashlib.sha256(raw_body).hexdigest(),
        normalized_body_sha256=request_body_digest(normalized_body),
    )
