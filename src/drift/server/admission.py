"""Bounded, privacy-safe admission controls for public manifested workers."""

from __future__ import annotations

import contextlib
import hashlib
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable, MutableMapping, Optional

DEFAULT_MAX_ACTIVE_SESSIONS = 8
DEFAULT_MAX_ACTIVE_SESSIONS_PER_PEER = 1
DEFAULT_GLOBAL_SESSION_RATE = 2.0
DEFAULT_GLOBAL_SESSION_BURST = 4
DEFAULT_PEER_SESSION_RATE = 0.25
DEFAULT_PEER_SESSION_BURST = 1
DEFAULT_MAX_TRACKED_PEERS = 512
DEFAULT_TRACKED_PEER_TTL = 300.0
DEFAULT_MAX_PENDING_PUSHES = 4
PUBLIC_OVERLOAD_MESSAGE = "public worker admission is temporarily unavailable"

_MAX_ACTIVE_SESSIONS = 128
_MAX_SESSION_BURST = 1024
_MAX_SESSION_RATE = 1024.0
_MAX_TRACKED_PEERS = 65_536
_MAX_TRACKED_PEER_TTL = 86_400.0
_MAX_PENDING_PUSHES = 64
_MAX_SESSION_ID_BYTES = 256
_LOCK_ACQUIRE_TIMEOUT = 1.0

_GLOBAL_KEY = "global"
_HEALTH_KEY = "health"
_PENDING_PUSHES_KEY = "pending_pushes"
_PEER_PREFIX = "peer:"
_SESSION_PREFIX = "session:"


class AdmissionRejected(RuntimeError):
    """A request was rejected before allocating public-worker compute resources."""


@dataclass(frozen=True)
class AdmissionPolicy:
    """Finite public-worker limits shared by every connection handler."""

    max_active_sessions: int = DEFAULT_MAX_ACTIVE_SESSIONS
    max_active_sessions_per_peer: int = DEFAULT_MAX_ACTIVE_SESSIONS_PER_PEER
    global_session_rate: float = DEFAULT_GLOBAL_SESSION_RATE
    global_session_burst: int = DEFAULT_GLOBAL_SESSION_BURST
    peer_session_rate: float = DEFAULT_PEER_SESSION_RATE
    peer_session_burst: int = DEFAULT_PEER_SESSION_BURST
    max_tracked_peers: int = DEFAULT_MAX_TRACKED_PEERS
    tracked_peer_ttl: float = DEFAULT_TRACKED_PEER_TTL
    max_pending_pushes: int = DEFAULT_MAX_PENDING_PUSHES
    allow_training_rpcs: bool = False

    def __post_init__(self) -> None:
        integer_bounds = {
            "max_active_sessions": _MAX_ACTIVE_SESSIONS,
            "max_active_sessions_per_peer": _MAX_ACTIVE_SESSIONS,
            "global_session_burst": _MAX_SESSION_BURST,
            "peer_session_burst": _MAX_SESSION_BURST,
            "max_tracked_peers": _MAX_TRACKED_PEERS,
            "max_pending_pushes": _MAX_PENDING_PUSHES,
        }
        for name, upper_bound in integer_bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper_bound:
                raise ValueError(f"{name} must be an integer between 1 and {upper_bound}")
        number_bounds = {
            "global_session_rate": _MAX_SESSION_RATE,
            "peer_session_rate": _MAX_SESSION_RATE,
            "tracked_peer_ttl": _MAX_TRACKED_PEER_TTL,
        }
        for name, upper_bound in number_bounds.items():
            value = getattr(self, name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not 0 < value <= upper_bound
            ):
                raise ValueError(f"{name} must be a finite number between 0 and {upper_bound}")
        if not isinstance(self.allow_training_rpcs, bool):
            raise ValueError("allow_training_rpcs must be a boolean")
        if self.max_active_sessions_per_peer > self.max_active_sessions:
            raise ValueError("max_active_sessions_per_peer cannot exceed max_active_sessions")
        if self.max_tracked_peers < self.max_active_sessions:
            raise ValueError("max_tracked_peers cannot be less than max_active_sessions")
        refill_seconds = self.peer_session_burst / self.peer_session_rate
        if self.tracked_peer_ttl < refill_seconds:
            raise ValueError("tracked_peer_ttl must cover a complete per-peer token-bucket refill")

    @property
    def push_queue_capacity(self) -> int:
        """Bound each transport queue by the shared aggregate push ceiling."""

        return self.max_pending_pushes


class AdmissionLease:
    """Idempotently release one admitted inference session."""

    def __init__(self, state: "AdmissionState", peer_key: str):
        self._state = state
        self._peer_key = peer_key
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._state._release(self._peer_key)

    def __enter__(self) -> "AdmissionLease":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:  # noqa: ANN001
        self.release()


class AdmissionState:
    """Atomic admission state backed by local or multiprocessing-manager primitives."""

    def __init__(
        self,
        policy: AdmissionPolicy,
        records: MutableMapping,
        lock,
        *,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.policy = policy
        self._records = records
        self._lock = lock
        self._clock = clock
        now = self._now()
        with self._locked():
            if _HEALTH_KEY not in self._records:
                self._records[_HEALTH_KEY] = True
            if _GLOBAL_KEY not in self._records:
                self._records[_GLOBAL_KEY] = (
                    0,
                    float(policy.global_session_burst),
                    now,
                    0,
                    0,
                    True,
                )
            if _PENDING_PUSHES_KEY not in self._records:
                self._records[_PENDING_PUSHES_KEY] = 0

    @classmethod
    def local(cls, policy: AdmissionPolicy, *, clock: Optional[Callable[[], float]] = None) -> "AdmissionState":
        return cls(policy, {}, threading.RLock(), clock=clock)

    @classmethod
    def shared(cls, policy: AdmissionPolicy, manager) -> "AdmissionState":  # noqa: ANN001
        return cls(policy, manager.dict(), manager.RLock())

    @contextlib.contextmanager
    def _locked(self):
        """Acquire shared state for a finite interval so manager faults cannot wedge a worker."""

        try:
            acquired = self._lock.acquire(timeout=_LOCK_ACQUIRE_TIMEOUT)
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc
        if not acquired:
            raise AdmissionRejected("public worker admission state is unavailable")
        try:
            yield
        finally:
            try:
                self._lock.release()
            except Exception as exc:
                raise AdmissionRejected("public worker admission state is unavailable") from exc

    def _now(self) -> float:
        value = time.monotonic() if self._clock is None else self._clock()
        if not isinstance(value, (int, float)) or not math.isfinite(value):
            raise AdmissionRejected("public worker admission state is unavailable")
        return float(value)

    @staticmethod
    def _peer_key(peer_id: object) -> str:
        identity = str(peer_id).encode("utf-8", errors="strict")
        return _PEER_PREFIX + hashlib.sha256(identity).hexdigest()

    @staticmethod
    def _session_key(session_id: object) -> str:
        if not isinstance(session_id, str):
            raise AdmissionRejected("public worker session identity is invalid")
        try:
            identity = session_id.encode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise AdmissionRejected("public worker session identity is invalid") from exc
        if not 1 <= len(identity) <= _MAX_SESSION_ID_BYTES:
            raise AdmissionRejected("public worker session identity is invalid")
        return _SESSION_PREFIX + hashlib.sha256(identity).hexdigest()

    @classmethod
    def validate_session_id(cls, session_id: object) -> None:
        cls._session_key(session_id)

    @staticmethod
    def create_session_token() -> str:
        return secrets.token_hex(16)

    @staticmethod
    def _validated_route(record) -> tuple[int, str]:  # noqa: ANN001
        if not isinstance(record, tuple) or len(record) != 2:
            raise AdmissionRejected("public worker admission state is unavailable")
        owner, route_token = record
        if isinstance(owner, bool) or not isinstance(owner, int) or owner < 0:
            raise AdmissionRejected("public worker admission state is unavailable")
        if (
            not isinstance(route_token, str)
            or len(route_token) != 32
            or any(character not in "0123456789abcdef" for character in route_token)
        ):
            raise AdmissionRejected("public worker admission state is unavailable")
        return owner, route_token

    def _is_healthy_locked(self) -> bool:
        record = self._records.get(_GLOBAL_KEY)
        return (
            self._records.get(_HEALTH_KEY) is True
            and isinstance(record, tuple)
            and len(record) == 6
            and record[-1] is True
        )

    def _mark_unhealthy_locked(self) -> None:
        self._records[_HEALTH_KEY] = False
        record = self._records.get(_GLOBAL_KEY)
        if isinstance(record, tuple) and len(record) == 6:
            active, tokens, updated, accepted, rejected, _ = record
            self._records[_GLOBAL_KEY] = (active, tokens, updated, accepted, rejected, False)
        else:
            self._records[_GLOBAL_KEY] = (0, 0.0, 0.0, 0, 0, False)

    @staticmethod
    def _refill(tokens: float, updated_at: float, now: float, rate: float, burst: int) -> float:
        if now < updated_at:
            raise AdmissionRejected("public worker admission state is unavailable")
        return min(float(burst), tokens + (now - updated_at) * rate)

    def _reject(self, global_record, *, now: float) -> None:  # noqa: ANN001
        active, tokens, _, accepted, rejected, healthy = global_record
        self._records[_GLOBAL_KEY] = (active, tokens, now, accepted, rejected + 1, healthy)

    def _prune_inactive_peers(self, now: float) -> None:
        candidates = []
        for key, record in tuple(self._records.items()):
            if not isinstance(key, str) or not key.startswith(_PEER_PREFIX):
                continue
            active, _, _, last_seen = record
            if active == 0 and now - last_seen >= self.policy.tracked_peer_ttl:
                candidates.append((last_seen, key))
        for _, key in sorted(candidates):
            if self.tracked_peer_count < self.policy.max_tracked_peers:
                break
            self._records.pop(key, None)

    @property
    def tracked_peer_count(self) -> int:
        return sum(1 for key in self._records.keys() if isinstance(key, str) and key.startswith(_PEER_PREFIX))

    def acquire(self, peer_id: object) -> AdmissionLease:
        peer_key = self._peer_key(peer_id)
        try:
            now = self._now()
            with self._locked():
                global_record = self._records[_GLOBAL_KEY]
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
                global_active, global_tokens, global_updated, accepted, rejected, healthy = global_record
                global_tokens = self._refill(
                    global_tokens,
                    global_updated,
                    now,
                    self.policy.global_session_rate,
                    self.policy.global_session_burst,
                )
                global_record = (global_active, global_tokens, now, accepted, rejected, healthy)

                peer_record = self._records.get(peer_key)
                if peer_record is None:
                    self._prune_inactive_peers(now)
                    if self.tracked_peer_count >= self.policy.max_tracked_peers:
                        self._reject(global_record, now=now)
                        raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)
                    peer_record = (0, float(self.policy.peer_session_burst), now, now)
                peer_active, peer_tokens, peer_updated, _ = peer_record
                peer_tokens = self._refill(
                    peer_tokens,
                    peer_updated,
                    now,
                    self.policy.peer_session_rate,
                    self.policy.peer_session_burst,
                )

                if global_active >= self.policy.max_active_sessions:
                    self._reject(global_record, now=now)
                    raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)
                if peer_active >= self.policy.max_active_sessions_per_peer:
                    self._reject(global_record, now=now)
                    raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)
                if global_tokens < 1.0 or peer_tokens < 1.0:
                    self._reject(global_record, now=now)
                    raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)

                self._records[_GLOBAL_KEY] = (
                    global_active + 1,
                    global_tokens - 1.0,
                    now,
                    accepted + 1,
                    rejected,
                    True,
                )
                self._records[peer_key] = (peer_active + 1, peer_tokens - 1.0, now, now)
            return AdmissionLease(self, peer_key)
        except AdmissionRejected:
            raise
        except Exception as exc:
            self.mark_unhealthy()
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def _release(self, peer_key: str) -> None:
        try:
            now = self._now()
            with self._locked():
                global_active, tokens, updated, accepted, rejected, healthy = self._records[_GLOBAL_KEY]
                peer_record = self._records.get(peer_key)
                if global_active < 1 or peer_record is None or peer_record[0] < 1:
                    self._records[_GLOBAL_KEY] = (
                        max(0, global_active),
                        tokens,
                        updated,
                        accepted,
                        rejected,
                        False,
                    )
                    return
                peer_active, peer_tokens, peer_updated, _ = peer_record
                self._records[_GLOBAL_KEY] = (
                    global_active - 1,
                    tokens,
                    updated,
                    accepted,
                    rejected,
                    healthy,
                )
                self._records[peer_key] = (peer_active - 1, peer_tokens, peer_updated, now)
        except Exception:
            self.mark_unhealthy()

    def require_healthy(self) -> None:
        try:
            if self._records.get(_HEALTH_KEY) is not True:
                raise AdmissionRejected("public worker admission state is unavailable")
            with self._locked():
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def mark_unhealthy(self) -> None:
        # Poison outside the quota lock first. A handler that times out releasing a lease
        # must make health fail closed even when another process still owns that lock.
        try:
            self._records[_HEALTH_KEY] = False
        except Exception:
            return
        try:
            with self._locked():
                self._mark_unhealthy_locked()
        except Exception:
            pass

    def register_session(
        self,
        session_id: object,
        handler_index: int,
        route_token: Optional[str] = None,
    ) -> tuple[str, str]:
        """Atomically bind an opaque session identity and generation to one handler."""

        session_key = self._session_key(session_id)
        route_token = self.create_session_token() if route_token is None else route_token
        self._validated_route((handler_index, route_token))
        try:
            with self._locked():
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
                if session_key in self._records:
                    raise AdmissionRejected("public worker session identity is already active")
                if self.active_session_routes >= self.policy.max_active_sessions:
                    raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)
                self._records[session_key] = (handler_index, route_token)
            return session_key, route_token
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def unregister_session(self, session_key: str, handler_index: int, route_token: str) -> None:
        """Remove one exact route generation; corruption poisons admission until restart."""

        try:
            with self._locked():
                if self._records.get(session_key) != (handler_index, route_token):
                    self._mark_unhealthy_locked()
                    return
                self._records.pop(session_key, None)
        except Exception:
            self.mark_unhealthy()

    def resolve_session(self, session_id: object) -> Optional[int]:
        session_key = self._session_key(session_id)
        try:
            with self._locked():
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
                route = self._records.get(session_key)
                if route is None:
                    return None
                try:
                    owner, _ = self._validated_route(route)
                except AdmissionRejected:
                    self._mark_unhealthy_locked()
                    raise
                return owner
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def _reserve_push_locked(self) -> None:
        pending = self._records.get(_PENDING_PUSHES_KEY)
        if isinstance(pending, bool) or not isinstance(pending, int) or pending < 0:
            self._mark_unhealthy_locked()
            raise AdmissionRejected("public worker admission state is unavailable")
        if pending >= self.policy.max_pending_pushes:
            raise AdmissionRejected(PUBLIC_OVERLOAD_MESSAGE)
        self._records[_PENDING_PUSHES_KEY] = pending + 1

    def reserve_outbound_push(self) -> None:
        """Reserve one shared slot before creating an outbound activation RPC task."""

        try:
            with self._locked():
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
                self._reserve_push_locked()
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def reserve_push(self, session_id: object) -> tuple[int, str]:
        """Reserve one aggregate push slot and return the exact session route."""

        session_key = self._session_key(session_id)
        try:
            with self._locked():
                if not self._is_healthy_locked():
                    raise AdmissionRejected("public worker admission state is unavailable")
                route = self._records.get(session_key)
                if route is None:
                    raise AdmissionRejected("public worker push target is unavailable")
                try:
                    owner, route_token = self._validated_route(route)
                except AdmissionRejected:
                    self._mark_unhealthy_locked()
                    raise
                self._reserve_push_locked()
                return owner, route_token
        except AdmissionRejected:
            raise
        except Exception as exc:
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def release_push(self) -> None:
        try:
            with self._locked():
                pending = self._records.get(_PENDING_PUSHES_KEY)
                if isinstance(pending, bool) or not isinstance(pending, int) or pending < 1:
                    self._mark_unhealthy_locked()
                    return
                self._records[_PENDING_PUSHES_KEY] = pending - 1
        except Exception:
            self.mark_unhealthy()

    @property
    def active_session_routes(self) -> int:
        return sum(1 for key in self._records.keys() if isinstance(key, str) and key.startswith(_SESSION_PREFIX))

    def require_training_allowed(self) -> None:
        if not self.policy.allow_training_rpcs:
            raise AdmissionRejected("training RPCs are disabled on manifested public workers")

    def snapshot(self) -> dict:
        """Return aggregate counters only; never expose peer identities or per-peer utilization."""
        try:
            return self._snapshot()
        except AdmissionRejected:
            raise
        except Exception as exc:
            self.mark_unhealthy()
            raise AdmissionRejected("public worker admission state is unavailable") from exc

    def _snapshot(self) -> dict:
        with self._locked():
            active, _, _, accepted, rejected, _ = self._records[_GLOBAL_KEY]
            healthy = self._is_healthy_locked()
            pending_pushes = self._records.get(_PENDING_PUSHES_KEY)
            if isinstance(pending_pushes, bool) or not isinstance(pending_pushes, int) or pending_pushes < 0:
                self._mark_unhealthy_locked()
                raise AdmissionRejected("public worker admission state is unavailable")
            return {
                "active_sessions": active,
                "tracked_peers": self.tracked_peer_count,
                "active_session_routes": self.active_session_routes,
                "pending_pushes": pending_pushes,
                "accepted_sessions": accepted,
                "rejected_sessions": rejected,
                "healthy": healthy,
            }
