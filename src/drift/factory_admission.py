"""Host-owned ordinary Factory admission; no authentication or TEE verifier.

The embedding host must authenticate the ORIGINAL ingress record independently,
before operational credentials/DB access, and supply fresh authority and route
readers. Caller fields, HTTP bearer authentication and these callbacks do not
establish that provenance. Protected dispatch is deliberately unavailable.
"""

import hashlib
import inspect
import json
import math
import re
from dataclasses import dataclass, field
from typing import Callable


class RequestAdmissionDenied(RuntimeError):
    def __init__(self):
        super().__init__("Original request admission denied")


def synchronous_result(value):
    """Reject deferred callback work without advancing a generator/coroutine."""
    if inspect.isawaitable(value) or inspect.isgenerator(value) or inspect.isasyncgen(value):
        if inspect.iscoroutine(value) or inspect.isgenerator(value):
            value.close()
        raise RequestAdmissionDenied()
    return value


def require_authority(callback):
    try:
        if synchronous_result(callback()) is not None:
            raise RequestAdmissionDenied()
    except Exception:
        raise RequestAdmissionDenied() from None


def request_body_digest(body: dict) -> str:
    """Digest the validated API body, including defaults, excluding null fields."""
    if type(body) is not dict:
        raise RequestAdmissionDenied()
    try:
        wire = json.dumps(body, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(wire.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        raise RequestAdmissionDenied() from None


@dataclass(frozen=True)
class FactoryAdmission:
    request_id: str
    body_digest: str
    manifest_digest: str
    num_blocks: int
    confidentiality_class: str
    class_version: int
    authority: Callable[[], None] = field(repr=False, compare=False)
    route_snapshot: Callable[[], dict] = field(repr=False, compare=False)
    claim_dispatch: Callable[[], None] = field(repr=False, compare=False)
    max_route_age: float = 5.0

    def require_current(self, body: dict) -> None:
        # Missing/unknown/protected versions or classes fail before host readers.
        if (
            type(self.request_id) is not str
            or re.fullmatch(r"[0-9a-f]{32}", self.request_id) is None
            or type(self.class_version) is not int
            or self.class_version != 1
            or self.confidentiality_class != "ordinary"
            or type(self.manifest_digest) is not str
            or re.fullmatch(r"sha256:[0-9a-f]{64}", self.manifest_digest) is None
            or type(self.body_digest) is not str
            or re.fullmatch(r"[0-9a-f]{64}", self.body_digest) is None
            or type(self.num_blocks) is not int
            or not 0 < self.num_blocks <= 4096
            or type(self.max_route_age) not in (int, float)
            or not math.isfinite(self.max_route_age)
            or not 0 < self.max_route_age <= 30
            or not callable(self.authority)
            or not callable(self.route_snapshot)
            or not callable(self.claim_dispatch)
            or body.get("model") != self.manifest_digest
            or request_body_digest(body) != self.body_digest
        ):
            raise RequestAdmissionDenied()
        require_authority(self.authority)
        try:
            route = synchronous_result(self.route_snapshot())
            if type(route) is not dict:
                raise RequestAdmissionDenied()
            age = route.get("last_updated_age")
            replicas = route.get("replica_counts")
            if (
                route.get("manifest_digest") != self.manifest_digest
                or route.get("status") != "complete"
                or type(route.get("covered_blocks")) is not int
                or route["covered_blocks"] != self.num_blocks
                or type(route.get("total_blocks")) is not int
                or route["total_blocks"] != self.num_blocks
                or route.get("missing_blocks") != []
                or route.get("chat_ready") is not True
                or type(route.get("peer_count")) is not int
                or route["peer_count"] < 1
                or type(age) not in (int, float)
                or not math.isfinite(age)
                or not 0 <= age <= self.max_route_age
                or type(replicas) is not list
                or len(replicas) != self.num_blocks
                or any(type(count) is not int or count < 1 for count in replicas)
            ):
                raise RequestAdmissionDenied()
        except Exception:
            raise RequestAdmissionDenied() from None
