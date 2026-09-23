"""Bounded provider protocol contracts; no backend or qualification authority.

The exact-model profiles in this module are deliberately unavailable.  A
``qualification_id`` is only a reference to evidence validated elsewhere; it
is never proof of hardware, model execution, licensing, or backend support.
Likewise, accepting a terminal frame validates protocol shape only.  It cannot
stop a backend or prove that an external process actually stopped.
"""

from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Callable, Mapping

DEEPSEEK_V41_FLASH = "deepseek-ai/DeepSeek-V4.1-Flash"
GLM_53 = "zai-org/GLM-5.3"
REQUIRED_MODEL_IDS = frozenset({DEEPSEEK_V41_FLASH, GLM_53})

MAX_IDENTIFIER_BYTES = 192
MAX_REQUEST_BYTES = 1 << 20
MAX_EVENT_BYTES = 1 << 20
MAX_EVENTS = 4096
MAX_UNITS = 2**63 - 1
MAX_MONOTONIC = 2**53

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class ContractCode(str, Enum):
    MALFORMED = "malformed"
    IDENTITY_MISMATCH = "identity_mismatch"
    SEQUENCE = "sequence"
    CLOCK = "clock"
    DEADLINE = "deadline"
    LIMIT = "limit"
    CANCELLED = "cancelled"
    TERMINAL = "terminal"


class ProviderContractError(ValueError):
    def __init__(self, code: ContractCode):
        self.code = code
        super().__init__(code.value)


def _require(condition: bool, code: ContractCode = ContractCode.MALFORMED) -> None:
    if not condition:
        raise ProviderContractError(code)


def _identifier(value: object) -> bool:
    if type(value) is not str or not value:
        return False
    try:
        if len(value.encode("utf-8")) > MAX_IDENTIFIER_BYTES:
            return False
    except UnicodeEncodeError:
        return False
    return all(0x21 <= ord(character) <= 0x7E for character in value)


def _monotonic(value: object) -> bool:
    return type(value) is float and math.isfinite(value) and 0 <= value <= MAX_MONOTONIC


def _uint(value: object, maximum: int = MAX_UNITS) -> bool:
    return type(value) is int and 0 <= value <= maximum


class Availability(str, Enum):
    UNAVAILABLE = "unavailable"
    AVAILABLE = "available"


class Feature(str, Enum):
    JSON_SCHEMA = "json_schema"
    LOGPROBS = "logprobs"
    TOOLS = "tools"


class EventKind(str, Enum):
    STARTED = "started"
    OUTPUT = "output"
    COMPLETED = "completed"
    REFUSED = "refused"
    FAILED = "failed"
    STOPPED = "stopped"


class RefusalCode(str, Enum):
    PROFILE_UNAVAILABLE = "profile_unavailable"
    UNSUPPORTED_FEATURE = "unsupported_feature"


class StopReason(str, Enum):
    CANCELLED = "cancelled"
    DEADLINE = "deadline"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class ProviderIdentity:
    provider_id: str
    instance_id: str

    def __post_init__(self) -> None:
        _require(_identifier(self.provider_id) and _identifier(self.instance_id))


@dataclass(frozen=True)
class ProviderProfile:
    profile_id: str
    model_id: str
    availability: Availability
    supported_features: frozenset[Feature] = frozenset()
    qualification_id: str | None = None

    def __post_init__(self) -> None:
        _require(_identifier(self.profile_id) and _identifier(self.model_id))
        _require(type(self.availability) is Availability and type(self.supported_features) is frozenset)
        _require(all(type(feature) is Feature for feature in self.supported_features))
        if self.availability is Availability.UNAVAILABLE:
            _require(not self.supported_features and self.qualification_id is None)
        else:
            _require(type(self.qualification_id) is str and _HEX64.fullmatch(self.qualification_id) is not None)
            _require(self.profile_id.startswith("test/") and self.model_id.startswith("test/"))
        # This source checkpoint must not turn either real target into a claimed
        # qualified profile through a caller-supplied availability flag.
        _require(self.model_id not in REQUIRED_MODEL_IDS or self.availability is Availability.UNAVAILABLE)


@dataclass(frozen=True)
class InferenceLimits:
    max_input_units: int
    max_output_units: int
    max_events: int
    max_output_bytes: int

    def __post_init__(self) -> None:
        _require(_uint(self.max_input_units) and _uint(self.max_output_units))
        _require(_uint(self.max_events, MAX_EVENTS) and self.max_events > 0)
        _require(_uint(self.max_output_bytes, MAX_EVENT_BYTES))


@dataclass(frozen=True)
class InferenceRequest:
    identity: ProviderIdentity
    profile_id: str
    model_id: str
    request_id: str
    attempt_id: str
    issued_at: float
    deadline: float
    prompt: str = field(repr=False)
    limits: InferenceLimits
    features: frozenset[Feature] = frozenset()

    def __post_init__(self) -> None:
        _require(type(self.identity) is ProviderIdentity and type(self.limits) is InferenceLimits)
        _require(_identifier(self.profile_id) and _identifier(self.model_id))
        _require(type(self.request_id) is str and _HEX32.fullmatch(self.request_id) is not None)
        _require(type(self.attempt_id) is str and _HEX32.fullmatch(self.attempt_id) is not None)
        _require(_monotonic(self.issued_at) and _monotonic(self.deadline) and self.deadline > self.issued_at)
        _require(type(self.prompt) is str)
        try:
            prompt_bytes = self.prompt.encode("utf-8")
        except (AttributeError, UnicodeEncodeError):
            _require(False)
        _require(len(prompt_bytes) <= MAX_REQUEST_BYTES)
        _require(type(self.features) is frozenset and all(type(feature) is Feature for feature in self.features))


@dataclass(frozen=True)
class Usage:
    input_units: int
    output_units: int
    total_units: int

    def __post_init__(self) -> None:
        _require(all(_uint(value) for value in (self.input_units, self.output_units, self.total_units)))
        _require(self.total_units == self.input_units + self.output_units)


@dataclass(frozen=True)
class Refusal:
    code: RefusalCode
    unsupported_features: tuple[Feature, ...] = ()

    def __post_init__(self) -> None:
        _require(type(self.code) is RefusalCode and type(self.unsupported_features) is tuple)
        _require(all(type(feature) is Feature for feature in self.unsupported_features))
        _require(
            tuple(sorted(set(self.unsupported_features), key=lambda item: item.value)) == self.unsupported_features
        )
        _require((self.code is RefusalCode.UNSUPPORTED_FEATURE) == bool(self.unsupported_features))


@dataclass(frozen=True)
class ProviderEvent:
    identity: ProviderIdentity
    profile_id: str
    model_id: str
    request_id: str
    attempt_id: str
    sequence: int
    kind: EventKind
    text: str | None = field(default=None, repr=False)
    usage: Usage | None = None
    refusal: Refusal | None = None
    failure_code: str | None = None
    stop_reason: StopReason | None = None

    def __post_init__(self) -> None:
        _require(type(self.identity) is ProviderIdentity)
        _require(_identifier(self.profile_id) and _identifier(self.model_id))
        _require(type(self.request_id) is str and _HEX32.fullmatch(self.request_id) is not None)
        _require(type(self.attempt_id) is str and _HEX32.fullmatch(self.attempt_id) is not None)
        _require(_uint(self.sequence, MAX_EVENTS - 1) and type(self.kind) is EventKind)
        present = (
            self.text is not None,
            self.usage is not None,
            self.refusal is not None,
            self.failure_code is not None,
            self.stop_reason is not None,
        )
        expected = {
            EventKind.STARTED: (False, False, False, False, False),
            EventKind.OUTPUT: (True, False, False, False, False),
            EventKind.COMPLETED: (False, True, False, False, False),
            EventKind.REFUSED: (False, False, True, False, False),
            EventKind.FAILED: (False, False, False, True, False),
            EventKind.STOPPED: (False, False, False, False, True),
        }[self.kind]
        _require(present == expected)
        if self.text is not None:
            _require(type(self.text) is str)
            try:
                raw = self.text.encode("utf-8")
            except (AttributeError, UnicodeEncodeError):
                _require(False)
            _require(0 < len(raw) <= MAX_EVENT_BYTES)
        if self.usage is not None:
            _require(type(self.usage) is Usage)
        if self.refusal is not None:
            _require(type(self.refusal) is Refusal)
        if self.failure_code is not None:
            _require(type(self.failure_code) is str and _CODE.fullmatch(self.failure_code) is not None)
        if self.stop_reason is not None:
            _require(type(self.stop_reason) is StopReason)


@dataclass(frozen=True)
class AcceptedEvent:
    event: ProviderEvent
    accepted_at: float

    def __post_init__(self) -> None:
        _require(type(self.event) is ProviderEvent and _monotonic(self.accepted_at))


@dataclass(frozen=True)
class CancellationRequest:
    identity: ProviderIdentity
    request_id: str
    attempt_id: str
    reason: StopReason
    requested_at: float
    after_sequence: int

    def __post_init__(self) -> None:
        _require(type(self.identity) is ProviderIdentity)
        _require(type(self.request_id) is str and _HEX32.fullmatch(self.request_id) is not None)
        _require(type(self.attempt_id) is str and _HEX32.fullmatch(self.attempt_id) is not None)
        _require(type(self.reason) is StopReason and _monotonic(self.requested_at))
        _require(_uint(self.after_sequence, MAX_EVENTS - 1))


@dataclass(frozen=True)
class StreamCheckpoint:
    events: tuple[AcceptedEvent, ...]
    cancellation: CancellationRequest | None

    def __post_init__(self) -> None:
        _require(type(self.events) is tuple and all(type(event) is AcceptedEvent for event in self.events))
        _require(self.cancellation is None or type(self.cancellation) is CancellationRequest)
        _require(len(self.events) <= MAX_EVENTS)


def refusal_for(profile: ProviderProfile, request: InferenceRequest) -> Refusal | None:
    _require(type(profile) is ProviderProfile and type(request) is InferenceRequest)
    _require(
        profile.profile_id == request.profile_id and profile.model_id == request.model_id,
        ContractCode.IDENTITY_MISMATCH,
    )
    if profile.availability is Availability.UNAVAILABLE:
        return Refusal(RefusalCode.PROFILE_UNAVAILABLE)
    unsupported = tuple(sorted(request.features - profile.supported_features, key=lambda item: item.value))
    return Refusal(RefusalCode.UNSUPPORTED_FEATURE, unsupported) if unsupported else None


class ProviderStreamValidator:
    """Stateful protocol validator using only a trusted local monotonic clock."""

    _TERMINAL = frozenset({EventKind.COMPLETED, EventKind.REFUSED, EventKind.FAILED, EventKind.STOPPED})

    def __init__(
        self,
        profile: ProviderProfile,
        request: InferenceRequest,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        _require(type(profile) is ProviderProfile and type(request) is InferenceRequest and callable(clock))
        _require(
            profile.profile_id == request.profile_id and profile.model_id == request.model_id,
            ContractCode.IDENTITY_MISMATCH,
        )
        self._lock = threading.RLock()
        self._in_clock = False
        self.profile, self.request, self._clock = profile, request, clock
        self._required_refusal = refusal_for(profile, request)
        self._events: list[AcceptedEvent] = []
        self._cancellation: CancellationRequest | None = None
        self._terminal: ProviderEvent | None = None
        self._started = False
        self._output_bytes = 0
        self._last_clock = request.issued_at

    @property
    def terminal(self) -> ProviderEvent | None:
        with self._lock:
            return self._terminal

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancellation is not None

    @property
    def terminal_reported(self) -> bool:
        with self._lock:
            return self._terminal is not None

    @property
    def stop_reported(self) -> bool:
        """Whether a STOPPED frame was reported, not proof an external process stopped."""
        with self._lock:
            return self._terminal is not None and self._terminal.kind is EventKind.STOPPED

    def _now(self) -> float:
        with self._lock:
            # The clock is trusted for time, not as a reentrant transition
            # callback. RLock makes ordinary nested internal use safe while this
            # explicit flag prevents a clock from changing stream state.
            _require(not self._in_clock, ContractCode.CLOCK)
            self._in_clock = True
            try:
                now = self._clock()
            finally:
                self._in_clock = False
            _require(_monotonic(now) and now >= self._last_clock, ContractCode.CLOCK)
            self._last_clock = now
            return now

    def _identity(self, event: ProviderEvent) -> None:
        _require(
            event.identity == self.request.identity
            and event.profile_id == self.request.profile_id
            and event.model_id == self.request.model_id
            and event.request_id == self.request.request_id
            and event.attempt_id == self.request.attempt_id,
            ContractCode.IDENTITY_MISMATCH,
        )

    def accept(self, event: ProviderEvent) -> AcceptedEvent:
        with self._lock:
            return self._accept_at(event, self._now())

    def _accept_at(self, event: ProviderEvent, accepted_at: float) -> AcceptedEvent:
        with self._lock:
            _require(not self._in_clock, ContractCode.CLOCK)
            _require(type(event) is ProviderEvent and _monotonic(accepted_at))
            _require(accepted_at >= self.request.issued_at, ContractCode.CLOCK)
            _require(self._terminal is None, ContractCode.TERMINAL)
            self._identity(event)
            _require(event.sequence == len(self._events), ContractCode.SEQUENCE)
            _require(event.sequence < self.request.limits.max_events, ContractCode.LIMIT)
            # max_events is a total bound. A nonterminal may not consume the
            # final slot and strand a stream without a bounded terminal frame.
            _require(
                event.kind in self._TERMINAL or event.sequence + 1 < self.request.limits.max_events,
                ContractCode.LIMIT,
            )
            if self._events:
                _require(accepted_at >= self._events[-1].accepted_at, ContractCode.CLOCK)

            if self._cancellation is not None:
                _require(event.kind is EventKind.STOPPED, ContractCode.CANCELLED)
                _require(event.stop_reason is self._cancellation.reason, ContractCode.CANCELLED)
                _require(event.sequence == self._cancellation.after_sequence, ContractCode.SEQUENCE)
                _require(accepted_at >= self._cancellation.requested_at, ContractCode.CLOCK)
            else:
                _require(accepted_at < self.request.deadline, ContractCode.DEADLINE)
                if self._required_refusal is not None:
                    _require(
                        event.kind is EventKind.REFUSED and event.refusal == self._required_refusal,
                        ContractCode.MALFORMED,
                    )
                else:
                    _require(event.kind is not EventKind.REFUSED, ContractCode.MALFORMED)
                    if event.kind is EventKind.STARTED:
                        _require(not self._started and not self._events)
                        self._started = True
                    elif event.kind is EventKind.OUTPUT:
                        _require(self._started)
                        output_bytes = self._output_bytes + len(event.text.encode("utf-8"))
                        _require(output_bytes <= self.request.limits.max_output_bytes, ContractCode.LIMIT)
                        self._output_bytes = output_bytes
                    elif event.kind is EventKind.COMPLETED:
                        _require(self._started)
                        usage = event.usage
                        _require(usage.input_units <= self.request.limits.max_input_units, ContractCode.LIMIT)
                        _require(usage.output_units <= self.request.limits.max_output_units, ContractCode.LIMIT)
                    elif event.kind is EventKind.FAILED:
                        pass  # Terminal startup/runtime failure; no success or usage claim.
                    else:
                        _require(False, ContractCode.MALFORMED)

            accepted = AcceptedEvent(event, accepted_at)
            self._events.append(accepted)
            self._last_clock = max(self._last_clock, accepted_at)
            if event.kind in self._TERMINAL:
                self._terminal = event
            return accepted

    def _record_cancel(self, reason: StopReason, requested_at: float) -> CancellationRequest:
        _require(not self._in_clock, ContractCode.CLOCK)
        _require(type(reason) is StopReason and _monotonic(requested_at))
        _require(self._terminal is None, ContractCode.TERMINAL)
        if self._cancellation is not None:
            _require(self._cancellation.reason is reason, ContractCode.CANCELLED)
            return self._cancellation
        cancellation = CancellationRequest(
            self.request.identity,
            self.request.request_id,
            self.request.attempt_id,
            reason,
            requested_at,
            len(self._events),
        )
        self._cancellation = cancellation
        return cancellation

    def request_cancel(self, reason: StopReason = StopReason.CANCELLED) -> CancellationRequest:
        """Record caller cancellation; system reasons have dedicated checked paths."""
        with self._lock:
            _require(not self._in_clock, ContractCode.CLOCK)
            _require(type(reason) is StopReason and reason is StopReason.CANCELLED)
            if self._cancellation is not None:
                _require(self._cancellation.reason is reason, ContractCode.CANCELLED)
                return self._cancellation
            _require(self._terminal is None, ContractCode.TERMINAL)
            return self._record_cancel(reason, self._now())

    def enforce_deadline(self) -> CancellationRequest | None:
        with self._lock:
            if self._terminal is not None:
                return None
            now = self._now()
            if now < self.request.deadline:
                return None
            if self._cancellation is not None:
                return self._cancellation
            return self._record_cancel(StopReason.DEADLINE, now)

    def checkpoint(self) -> StreamCheckpoint:
        with self._lock:
            return StreamCheckpoint(tuple(self._events), self._cancellation)

    @classmethod
    def recover(
        cls,
        profile: ProviderProfile,
        request: InferenceRequest,
        checkpoint: StreamCheckpoint,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> "ProviderStreamValidator":
        _require(type(checkpoint) is StreamCheckpoint)
        owner = cls(profile, request, clock=clock)
        cancellation = checkpoint.cancellation
        if cancellation is not None:
            _require(
                cancellation.identity == request.identity
                and cancellation.request_id == request.request_id
                and cancellation.attempt_id == request.attempt_id,
                ContractCode.IDENTITY_MISMATCH,
            )
        with owner._lock:
            for accepted in checkpoint.events:
                if cancellation is not None and accepted.event.sequence == cancellation.after_sequence:
                    _require(cancellation.requested_at >= owner._last_clock, ContractCode.CLOCK)
                    _require(cancellation.requested_at <= accepted.accepted_at, ContractCode.CLOCK)
                    owner._cancellation = cancellation
                    owner._last_clock = cancellation.requested_at
                owner._accept_at(accepted.event, accepted.accepted_at)
            if cancellation is not None and owner._cancellation is None:
                _require(cancellation.after_sequence == len(checkpoint.events), ContractCode.SEQUENCE)
                _require(owner._terminal is None, ContractCode.TERMINAL)
                _require(cancellation.requested_at >= owner._last_clock, ContractCode.CLOCK)
                owner._cancellation = cancellation
                owner._last_clock = cancellation.requested_at
            if owner._terminal is None and owner._cancellation is None:
                now = owner._now()
                reason = StopReason.DEADLINE if now >= request.deadline else StopReason.RECOVERY
                owner._record_cancel(reason, now)
        return owner


REQUIRED_PROFILES: Mapping[str, ProviderProfile] = MappingProxyType(
    {
        model_id: ProviderProfile(
            profile_id=model_id,
            model_id=model_id,
            availability=Availability.UNAVAILABLE,
        )
        for model_id in sorted(REQUIRED_MODEL_IDS)
    }
)


__all__ = [
    "AcceptedEvent",
    "Availability",
    "CancellationRequest",
    "ContractCode",
    "DEEPSEEK_V41_FLASH",
    "EventKind",
    "Feature",
    "GLM_53",
    "InferenceLimits",
    "InferenceRequest",
    "ProviderContractError",
    "ProviderEvent",
    "ProviderIdentity",
    "ProviderProfile",
    "ProviderStreamValidator",
    "REQUIRED_MODEL_IDS",
    "REQUIRED_PROFILES",
    "Refusal",
    "RefusalCode",
    "StopReason",
    "StreamCheckpoint",
    "Usage",
    "refusal_for",
]
