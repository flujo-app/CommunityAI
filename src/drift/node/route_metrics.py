"""Privacy-bounded local observations from completed inference routes."""

from __future__ import annotations

import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from statistics import median_low
from typing import Any, Callable, Deque, Dict, Mapping, Optional, Sequence

ROUTE_OBSERVATION_SCHEMA_VERSION = 1
DEFAULT_ROUTE_OBSERVATION_WINDOW_SECONDS = 5 * 60
_DEMAND_BUCKETS = (0, 1, 2, 4, 8, 16, 32, 64)
_THROUGHPUT_MILLI_TPS_BUCKETS = (0, 250, 500, 1_000, 2_000, 4_000, 8_000, 16_000, 32_000, 64_000)
_OBSERVATION_FIELDS = {
    "schema_version",
    "manifest_digest",
    "window_seconds",
    "attempts_bucket",
    "successes_bucket",
    "useful_tokens_per_second_milli",
    "reliability_milli",
    "age_seconds_bucket",
}
_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class RouteUtilityObservation:
    """Strict, quantized signals suitable for local placement scoring."""

    manifest_digest: str
    window_seconds: int
    attempts_bucket: int
    successes_bucket: int
    useful_tokens_per_second_milli: int
    reliability_milli: int
    age_seconds_bucket: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": ROUTE_OBSERVATION_SCHEMA_VERSION,
            "manifest_digest": self.manifest_digest,
            "window_seconds": self.window_seconds,
            "attempts_bucket": self.attempts_bucket,
            "successes_bucket": self.successes_bucket,
            "useful_tokens_per_second_milli": self.useful_tokens_per_second_milli,
            "reliability_milli": self.reliability_milli,
            "age_seconds_bucket": self.age_seconds_bucket,
        }


@dataclass
class _RouteWindow:
    """Aggregate only; no request, user, content, identity, or per-event history."""

    window_number: int
    attempts: int = 0
    successes: int = 0
    useful_tokens: int = 0
    useful_duration_seconds: float = 0.0
    last_observed_at: float = 0.0


def _require_int(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
    return value


def _floor_bucket(value: int, buckets: tuple[int, ...]) -> int:
    return max(bucket for bucket in buckets if bucket <= value)


def validate_route_observation(
    source: Mapping[str, Any],
    *,
    expected_manifest_digest: str,
    maximum_age_seconds: float,
) -> RouteUtilityObservation:
    """Validate a complete aggregate; unknown or high-cardinality fields are forbidden."""

    if not isinstance(source, Mapping):
        raise ValueError("route observation must be an object")
    if set(source) != _OBSERVATION_FIELDS:
        raise ValueError("route observation has missing or unknown fields")
    if source["schema_version"] != ROUTE_OBSERVATION_SCHEMA_VERSION:
        raise ValueError("route observation schema version is unsupported")
    manifest_digest = source["manifest_digest"]
    if not isinstance(manifest_digest, str) or _DIGEST_PATTERN.fullmatch(manifest_digest) is None:
        raise ValueError("route observation manifest_digest is invalid")
    if manifest_digest != expected_manifest_digest:
        raise ValueError("route observation belongs to a different manifest")
    if (
        isinstance(maximum_age_seconds, bool)
        or not isinstance(maximum_age_seconds, (int, float))
        or not math.isfinite(maximum_age_seconds)
        or maximum_age_seconds <= 0
    ):
        raise ValueError("maximum_age_seconds must be finite and positive")

    window_seconds = _require_int(source["window_seconds"], "window_seconds", minimum=60, maximum=3600)
    attempts = _require_int(source["attempts_bucket"], "attempts_bucket", minimum=1, maximum=64)
    successes = _require_int(source["successes_bucket"], "successes_bucket", minimum=0, maximum=64)
    if attempts not in _DEMAND_BUCKETS or successes not in _DEMAND_BUCKETS or successes > attempts:
        raise ValueError("route observation count buckets are invalid")
    throughput = _require_int(
        source["useful_tokens_per_second_milli"],
        "useful_tokens_per_second_milli",
        minimum=0,
        maximum=_THROUGHPUT_MILLI_TPS_BUCKETS[-1],
    )
    if throughput not in _THROUGHPUT_MILLI_TPS_BUCKETS:
        raise ValueError("route observation throughput bucket is invalid")
    reliability = _require_int(source["reliability_milli"], "reliability_milli", minimum=0, maximum=1000)
    if reliability % 100:
        raise ValueError("route observation reliability must use 10-percent buckets")
    age = _require_int(source["age_seconds_bucket"], "age_seconds_bucket", minimum=0, maximum=window_seconds)
    if age > maximum_age_seconds:
        raise ValueError("route observation is stale")

    return RouteUtilityObservation(
        manifest_digest=manifest_digest,
        window_seconds=window_seconds,
        attempts_bucket=attempts,
        successes_bucket=successes,
        useful_tokens_per_second_milli=throughput,
        reliability_milli=reliability,
        age_seconds_bucket=age,
    )


class RouteOutcomeTracker:
    """Keep two finite aggregate windows and expose only coarse, content-free signals."""

    def __init__(
        self,
        *,
        window_seconds: int = DEFAULT_ROUTE_OBSERVATION_WINDOW_SECONDS,
        maximum_events_per_model: int = 4096,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, int) or not 60 <= window_seconds <= 3600:
            raise ValueError("window_seconds must be an integer between 60 and 3600")
        if (
            isinstance(maximum_events_per_model, bool)
            or not isinstance(maximum_events_per_model, int)
            or maximum_events_per_model < 64
        ):
            raise ValueError("maximum_events_per_model must be an integer of at least 64")
        self.window_seconds = window_seconds
        self._maximum_events = maximum_events_per_model
        self._clock = clock
        self._windows: Dict[str, Deque[_RouteWindow]] = {}
        self._lock = threading.Lock()

    def record(
        self,
        *,
        manifest_digest: str,
        succeeded: bool,
        completion_tokens: int,
        duration_seconds: float,
    ) -> None:
        """Aggregate one generation without retaining an individual event record."""

        if not isinstance(manifest_digest, str) or _DIGEST_PATTERN.fullmatch(manifest_digest) is None:
            raise ValueError("manifest_digest must be a canonical sha256 digest")
        if not isinstance(succeeded, bool):
            raise ValueError("succeeded must be boolean")
        if isinstance(completion_tokens, bool) or not isinstance(completion_tokens, int) or completion_tokens < 0:
            raise ValueError("completion_tokens must be a non-negative integer")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be finite and positive")
        now = self._clock()
        window_number = int(now // self.window_seconds)
        with self._lock:
            windows = self._windows.setdefault(manifest_digest, deque(maxlen=2))
            if not windows or windows[-1].window_number != window_number:
                windows.append(_RouteWindow(window_number))
            aggregate = windows[-1]
            aggregate.last_observed_at = now
            if aggregate.attempts >= self._maximum_events:
                return
            aggregate.attempts += 1
            if succeeded:
                aggregate.successes += 1
                aggregate.useful_tokens = min((1 << 63) - 1, aggregate.useful_tokens + completion_tokens)
                aggregate.useful_duration_seconds = min(
                    self.window_seconds * self._maximum_events,
                    aggregate.useful_duration_seconds + float(duration_seconds),
                )

    def snapshot(self, manifest_digest: str) -> Optional[Dict[str, Any]]:
        """Return one strict aggregate, or None when no fresh route outcome exists."""

        if not isinstance(manifest_digest, str) or _DIGEST_PATTERN.fullmatch(manifest_digest) is None:
            return None
        now = self._clock()
        with self._lock:
            windows = self._windows.get(manifest_digest)
            if not windows:
                return None
            aggregate = windows[-1]
            if now - aggregate.last_observed_at > self.window_seconds:
                self._windows.pop(manifest_digest, None)
                return None
            attempts = aggregate.attempts
            successes = aggregate.successes
            useful_tokens = aggregate.useful_tokens
            useful_duration = aggregate.useful_duration_seconds
            last_observed_at = aggregate.last_observed_at

        if attempts < 1:
            return None
        throughput_milli = (
            0
            if useful_duration <= 0
            else min(_THROUGHPUT_MILLI_TPS_BUCKETS[-1], int(1000 * useful_tokens / useful_duration))
        )
        reliability_milli = int((1000 * successes / attempts) // 100 * 100)
        age_seconds = max(0, int(now - last_observed_at))
        observation = RouteUtilityObservation(
            manifest_digest=manifest_digest,
            window_seconds=self.window_seconds,
            attempts_bucket=_floor_bucket(attempts, _DEMAND_BUCKETS),
            successes_bucket=_floor_bucket(successes, _DEMAND_BUCKETS),
            useful_tokens_per_second_milli=_floor_bucket(throughput_milli, _THROUGHPUT_MILLI_TPS_BUCKETS),
            reliability_milli=reliability_milli,
            # Round age upward so quantization can never extend freshness.
            age_seconds_bucket=min(self.window_seconds, ((age_seconds + 14) // 15) * 15),
        )
        return observation.to_dict()

    def closed_snapshot(self, manifest_digest: str, *, minimum_attempts: int = 4) -> Optional[Dict[str, Any]]:
        """Return only a completed fixed window that clears the publication threshold."""

        if not isinstance(manifest_digest, str) or _DIGEST_PATTERN.fullmatch(manifest_digest) is None:
            return None
        if isinstance(minimum_attempts, bool) or not isinstance(minimum_attempts, int) or minimum_attempts < 4:
            raise ValueError("minimum_attempts must be an integer of at least four")
        now = self._clock()
        current_window = int(now // self.window_seconds)
        with self._lock:
            windows = self._windows.get(manifest_digest)
            if not windows:
                return None
            if windows[-1].window_number < current_window:
                aggregate = windows[-1]
            elif len(windows) > 1:
                aggregate = windows[-2]
            else:
                return None
            if now - aggregate.last_observed_at > self.window_seconds:
                return None
            attempts = aggregate.attempts
            successes = aggregate.successes
            useful_tokens = aggregate.useful_tokens
            useful_duration = aggregate.useful_duration_seconds
            last_observed_at = aggregate.last_observed_at

        if attempts < minimum_attempts:
            return None
        throughput_milli = (
            0
            if useful_duration <= 0
            else min(_THROUGHPUT_MILLI_TPS_BUCKETS[-1], int(1000 * useful_tokens / useful_duration))
        )
        reliability_milli = int((1000 * successes / attempts) // 100 * 100)
        age_seconds = max(0, int(now - last_observed_at))
        return RouteUtilityObservation(
            manifest_digest=manifest_digest,
            window_seconds=self.window_seconds,
            attempts_bucket=_floor_bucket(attempts, _DEMAND_BUCKETS),
            successes_bucket=_floor_bucket(successes, _DEMAND_BUCKETS),
            useful_tokens_per_second_milli=_floor_bucket(throughput_milli, _THROUGHPUT_MILLI_TPS_BUCKETS),
            reliability_milli=reliability_milli,
            age_seconds_bucket=min(self.window_seconds, ((age_seconds + 14) // 15) * 15),
        ).to_dict()


def aggregate_route_observations(
    sources: Sequence[Mapping[str, Any]],
    *,
    expected_manifest_digest: str,
    maximum_age_seconds: float,
    minimum_signers: int = 2,
    maximum_signers: int = 32,
) -> Optional[Dict[str, Any]]:
    """Return a bounded median hint from independently verified signer observations."""

    if (
        isinstance(minimum_signers, bool)
        or not isinstance(minimum_signers, int)
        or minimum_signers < 2
        or isinstance(maximum_signers, bool)
        or not isinstance(maximum_signers, int)
        or maximum_signers < minimum_signers
        or maximum_signers > 32
    ):
        raise ValueError("route observation signer bounds are invalid")
    observations = []
    for source in tuple(sources)[:maximum_signers]:
        try:
            observations.append(
                validate_route_observation(
                    source,
                    expected_manifest_digest=expected_manifest_digest,
                    maximum_age_seconds=maximum_age_seconds,
                )
            )
        except (TypeError, ValueError):
            continue
    if len(observations) < minimum_signers:
        return None

    return RouteUtilityObservation(
        manifest_digest=expected_manifest_digest,
        window_seconds=DEFAULT_ROUTE_OBSERVATION_WINDOW_SECONDS,
        attempts_bucket=median_low(sorted(item.attempts_bucket for item in observations)),
        successes_bucket=median_low(sorted(item.successes_bucket for item in observations)),
        useful_tokens_per_second_milli=median_low(sorted(item.useful_tokens_per_second_milli for item in observations)),
        reliability_milli=median_low(sorted(item.reliability_milli for item in observations)),
        age_seconds_bucket=max(item.age_seconds_bucket for item in observations),
    ).to_dict()
