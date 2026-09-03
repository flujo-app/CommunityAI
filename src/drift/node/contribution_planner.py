"""Automatic model and contiguous-block placement for contribution workers."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from drift.node.route_metrics import RouteUtilityObservation, validate_route_observation

MAX_AUTOMATIC_PLACEMENT_CANDIDATES = 32
MAX_AUTOMATIC_PLACEMENT_BLOCKS = 512
MODEL_DISPERSION_POINTS = 32.0


@dataclass(frozen=True)
class PlacementArtifactPlan:
    """Content-bound resource claim for one possible contiguous span."""

    start_block: int
    end_block: int
    artifact_bytes: int
    artifact_set_digest: str

    def __post_init__(self) -> None:
        if self.start_block < 0 or self.end_block <= self.start_block or self.artifact_bytes < 0:
            raise ValueError("placement artifact plan range and byte count are invalid")
        if (
            len(self.artifact_set_digest) != 64
            or self.artifact_set_digest.lower() != self.artifact_set_digest
            or any(character not in "0123456789abcdef" for character in self.artifact_set_digest)
        ):
            raise ValueError("placement artifact plan digest must be lowercase SHA-256")


@dataclass(frozen=True)
class PlacementCandidate:
    """One exact manifested model evaluated against local policy and live coverage."""

    model_id: str
    manifest_digest: str
    priority: int
    preferred: bool
    artifact_bytes: int
    total_blocks: int
    health: Mapping[str, Any]
    route_observation: Optional[Mapping[str, Any]] = None
    remote_route_observation: Optional[Mapping[str, Any]] = None
    policy_reason: Optional[str] = None
    artifact_plans: Tuple[PlacementArtifactPlan, ...] = ()
    max_artifact_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.model_id or not self.manifest_digest:
            raise ValueError("placement candidate identity must not be empty")
        if self.priority < 0 or self.artifact_bytes < 0 or self.total_blocks < 1:
            raise ValueError("placement candidate sizes and priority must be non-negative")
        if self.total_blocks > MAX_AUTOMATIC_PLACEMENT_BLOCKS:
            raise ValueError("placement candidate exceeds the automatic placement block limit")
        if self.max_artifact_bytes is not None and self.max_artifact_bytes < 0:
            raise ValueError("placement candidate artifact budget must be non-negative")
        ranges = set()
        for plan in self.artifact_plans:
            if plan.end_block > self.total_blocks:
                raise ValueError("placement artifact plan exceeds the candidate block range")
            key = (plan.start_block, plan.end_block)
            if key in ranges:
                raise ValueError("placement candidate contains duplicate artifact-plan ranges")
            ranges.add(key)


@dataclass(frozen=True)
class PlacementDecision:
    """A bounded, exact assignment suitable for one supervised worker."""

    model_id: str
    manifest_digest: str
    block_indices: str
    artifact_bytes: int
    replica_counts: Tuple[int, ...]
    score: float
    reason: str
    artifact_set_digest: Optional[str] = None


@dataclass(frozen=True)
class PlacementPlan:
    """The selected assignment, or a fail-closed reason for not starting."""

    decision: Optional[PlacementDecision]
    reason: str
    evaluated_models: int
    intent_published: bool = False
    remote_acknowledged: bool = False

    def __post_init__(self) -> None:
        if type(self.intent_published) is not bool or type(self.remote_acknowledged) is not bool:
            raise ValueError("placement intent publication fields must be booleans")
        if self.intent_published != self.remote_acknowledged:
            raise ValueError("placement intent publication requires a remote acknowledgement")
        if self.decision is None and self.intent_published:
            raise ValueError("an empty placement cannot carry an acknowledged intent")


class PlacementRegistry:
    """Thread-safe placement handoff between policy preparation and reconciliation."""

    def __init__(self) -> None:
        self._plans: dict[str, PlacementPlan] = {}
        self._lock = threading.Lock()

    def snapshot(self) -> dict[str, PlacementPlan]:
        with self._lock:
            return dict(self._plans)

    def replace(self, plans: Mapping[str, PlacementPlan]) -> None:
        with self._lock:
            self._plans = dict(plans)


class AutomaticContributionPlanner:
    """Choose one useful exact model/range without oscillating between snapshots."""

    def __init__(
        self,
        *,
        num_blocks: int,
        jitter_seed: str,
        minimum_residency_seconds: float = 15 * 60,
        cooldown_seconds: float = 5 * 60,
        switch_margin: float = 10.0,
        maximum_observation_age_seconds: float = 90.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if num_blocks < 1:
            raise ValueError("automatic placement num_blocks must be positive")
        if num_blocks > MAX_AUTOMATIC_PLACEMENT_BLOCKS:
            raise ValueError("automatic placement num_blocks exceeds the block limit")
        if not jitter_seed:
            raise ValueError("automatic placement jitter seed must not be empty")
        if (
            minimum_residency_seconds < 0
            or cooldown_seconds < 0
            or switch_margin < 0
            or maximum_observation_age_seconds <= 0
        ):
            raise ValueError("automatic placement timing and hysteresis limits are invalid")
        self.num_blocks = num_blocks
        self._jitter_seed = jitter_seed
        self._minimum_residency = minimum_residency_seconds
        self._cooldown = cooldown_seconds
        self._switch_margin = switch_margin
        self._maximum_observation_age = maximum_observation_age_seconds
        self._clock = clock
        self._current: Optional[PlacementDecision] = None
        self._assigned_at: Optional[float] = None
        self._last_switch_at: Optional[float] = None

    def _jitter(self, digest: str) -> float:
        """Return the bounded node-specific model dispersion score."""

        payload = f"{self._jitter_seed}\0{digest}".encode("utf-8")
        value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        return MODEL_DISPERSION_POINTS * value / (1 << 64)

    def _range_jitter(self, digest: str, start: int, end: int) -> int:
        """Return a stable rendezvous rank for one node/model/range."""

        payload = f"{self._jitter_seed}\0{digest}\0{start}:{end}".encode("utf-8")
        return int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")

    def _route_signal(
        self, source: Optional[Mapping[str, Any]], candidate: PlacementCandidate, *, cap: float
    ) -> tuple[float, Optional[RouteUtilityObservation]]:
        if source is None:
            return 0.0, None
        try:
            observation = validate_route_observation(
                source,
                expected_manifest_digest=candidate.manifest_digest,
                maximum_age_seconds=self._maximum_observation_age,
            )
        except (TypeError, ValueError):
            # Demand is a hint. Invalid, mismatched, or stale input cannot make a
            # model eligible and cannot disqualify otherwise valid coverage.
            return 0.0, None
        demand = math.log2(1 + observation.attempts_bucket) / math.log2(65)
        useful_tps = observation.useful_tokens_per_second_milli / 1000
        useful_throughput = min(1.0, math.log2(1 + useful_tps) / math.log2(65))
        reliability = observation.reliability_milli / 1000
        return cap * demand * useful_throughput * reliability, observation

    def _evaluate(self, candidate: PlacementCandidate) -> tuple[Optional[PlacementDecision], str]:
        if candidate.policy_reason is not None:
            return None, candidate.policy_reason
        if self.num_blocks > candidate.total_blocks:
            return None, f"model has only {candidate.total_blocks} blocks"
        health = candidate.health
        if health.get("status") not in ("complete", "incomplete"):
            return None, "coverage observation is unavailable"
        age = health.get("last_updated_age")
        if (
            isinstance(age, bool)
            or not isinstance(age, (int, float))
            or not math.isfinite(age)
            or age < 0
            or age > self._maximum_observation_age
        ):
            return None, "coverage observation is stale"
        counts = health.get("replica_counts")
        if (
            not isinstance(counts, (list, tuple))
            or len(counts) != candidate.total_blocks
            or any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in counts)
        ):
            return None, "coverage observation has invalid replica counts"

        artifact_plans = {(plan.start_block, plan.end_block): plan for plan in candidate.artifact_plans}
        if not artifact_plans and candidate.max_artifact_bytes is not None:
            if candidate.artifact_bytes > candidate.max_artifact_bytes:
                return None, (
                    f"manifested artifacts require {candidate.artifact_bytes} bytes, above the "
                    f"{candidate.max_artifact_bytes}-byte disk budget"
                )
        if artifact_plans:
            expected_ranges = {
                (start, start + self.num_blocks) for start in range(candidate.total_blocks - self.num_blocks + 1)
            }
            if set(artifact_plans) != expected_ranges:
                return None, f"exact artifact plans are unavailable for every {self.num_blocks}-block span"

        # Find the least-covered contiguous window in one bounded pass. Equal
        # windows use a node-specific rendezvous rank instead of numeric start, so
        # a cohort sharing one snapshot does not all announce range zero.
        maxima: deque[int] = deque()
        window_sum = 0
        best_key = None
        best_start = 0
        for index, count in enumerate(counts):
            window_sum += count
            while maxima and counts[maxima[-1]] <= count:
                maxima.pop()
            maxima.append(index)
            if index >= self.num_blocks:
                outgoing = index - self.num_blocks
                window_sum -= counts[outgoing]
                if maxima[0] == outgoing:
                    maxima.popleft()
            if index + 1 < self.num_blocks:
                continue
            start = index - self.num_blocks + 1
            end = start + self.num_blocks
            artifact_plan = artifact_plans.get((start, end))
            if (
                artifact_plan is not None
                and candidate.max_artifact_bytes is not None
                and artifact_plan.artifact_bytes > candidate.max_artifact_bytes
            ):
                continue
            key = (
                counts[maxima[0]],
                window_sum,
                self._range_jitter(candidate.manifest_digest, start, end),
                start,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_start = start
        if best_key is None:
            return None, (
                f"every {self.num_blocks}-block artifact set exceeds the "
                f"{candidate.max_artifact_bytes}-byte disk budget"
            )
        start = best_start
        end = start + self.num_blocks
        selected_artifacts = artifact_plans.get((start, end))
        artifact_bytes = candidate.artifact_bytes if selected_artifacts is None else selected_artifacts.artifact_bytes
        artifact_set_digest = None if selected_artifacts is None else selected_artifacts.artifact_set_digest
        window = tuple(counts[start : start + self.num_blocks])
        minimum_replicas = min(window)
        coverage_pressure = max(0, 2 - minimum_replicas) * 100.0
        preference_bonus = 20.0 if candidate.preferred else 0.0
        priority_bonus = 10.0 / (candidate.priority + 1)
        local_signal, local_observation = self._route_signal(candidate.route_observation, candidate, cap=6.0)
        remote_signal, remote_observation = self._route_signal(candidate.remote_route_observation, candidate, cap=2.0)
        # The combined eight-point demand cap stays below the ten-point switch
        # margin. Preference (20), priority (10), demand (8), and node-specific
        # dispersion (<32) total less than one 100-point coverage step.
        score = (
            coverage_pressure
            + preference_bonus
            + priority_bonus
            + local_signal
            + remote_signal
            + self._jitter(candidate.manifest_digest)
        )
        reason = f"selected {start}:{end} from fresh verified coverage; minimum replicas {minimum_replicas}"
        if local_observation is not None:
            reason += (
                f"; local demand bucket {local_observation.attempts_bucket}, useful throughput bucket "
                f"{local_observation.useful_tokens_per_second_milli} milli-tokens/s, reliability "
                f"{local_observation.reliability_milli}/1000"
            )
        if remote_observation is not None:
            reason += (
                f"; signed remote demand bucket {remote_observation.attempts_bucket}, useful throughput bucket "
                f"{remote_observation.useful_tokens_per_second_milli} milli-tokens/s, reliability "
                f"{remote_observation.reliability_milli}/1000"
            )
        return (
            PlacementDecision(
                model_id=candidate.model_id,
                manifest_digest=candidate.manifest_digest,
                block_indices=f"{start}:{end}",
                artifact_bytes=artifact_bytes,
                replica_counts=window,
                score=score,
                reason=reason,
                artifact_set_digest=artifact_set_digest,
            ),
            "",
        )

    def propose(
        self,
        candidates: Sequence[PlacementCandidate],
        *,
        sharing_enabled: bool,
        now: Optional[float] = None,
    ) -> PlacementPlan:
        now = self._clock() if now is None else now
        if not sharing_enabled:
            return PlacementPlan(None, "sharing is disabled by contribution policy", len(candidates))
        if len(candidates) > MAX_AUTOMATIC_PLACEMENT_CANDIDATES:
            return PlacementPlan(
                None,
                f"automatic placement candidate limit is {MAX_AUTOMATIC_PLACEMENT_CANDIDATES}",
                len(candidates),
            )

        eligible: list[PlacementDecision] = []
        rejected = []
        for candidate in candidates:
            decision, reason = self._evaluate(candidate)
            if decision is None:
                rejected.append(f"{candidate.model_id}: {reason}")
            else:
                eligible.append(decision)
        if not eligible:
            detail = "; ".join(rejected) if rejected else "no manifested models are configured"
            return PlacementPlan(None, detail, len(candidates))

        best = max(eligible, key=lambda item: (item.score, item.manifest_digest))
        current = next(
            (
                item
                for item in eligible
                if self._current is not None and item.manifest_digest == self._current.manifest_digest
            ),
            None,
        )
        current_assignment_is_eligible = current is not None
        if current_assignment_is_eligible:
            current_candidate = next(
                candidate for candidate in candidates if candidate.manifest_digest == self._current.manifest_digest
            )
            if current_candidate.artifact_plans:
                start, end = (int(value) for value in self._current.block_indices.split(":"))
                plan = next(
                    (
                        plan
                        for plan in current_candidate.artifact_plans
                        if (plan.start_block, plan.end_block) == (start, end)
                    ),
                    None,
                )
                current_assignment_is_eligible = (
                    plan is not None
                    and (
                        current_candidate.max_artifact_bytes is None
                        or plan.artifact_bytes <= current_candidate.max_artifact_bytes
                    )
                    and plan.artifact_bytes == self._current.artifact_bytes
                    and plan.artifact_set_digest == self._current.artifact_set_digest
                )
        if current_assignment_is_eligible and self._assigned_at is not None:
            residency_elapsed = now - self._assigned_at
            cooldown_elapsed = math.inf if self._last_switch_at is None else now - self._last_switch_at
            if residency_elapsed < self._minimum_residency or cooldown_elapsed < self._cooldown:
                best = self._current
            elif best.manifest_digest != current.manifest_digest and best.score < current.score + self._switch_margin:
                best = self._current

        return PlacementPlan(best, best.reason, len(candidates))

    def commit(self, plan: PlacementPlan, *, now: Optional[float] = None) -> PlacementPlan:
        """Commit a proposal only after its external pre-download checks pass."""

        decision = plan.decision
        if decision is None:
            return plan
        now = self._clock() if now is None else now
        if self._current is None:
            self._assigned_at = now
        elif decision.manifest_digest != self._current.manifest_digest:
            self._assigned_at = now
            self._last_switch_at = now
        elif decision.block_indices != self._current.block_indices:
            # A range handoff uses the same residency/cooldown boundary as a model
            # migration. The service pauses the old child before replacing it.
            self._assigned_at = now
            self._last_switch_at = now
        self._current = decision
        return plan

    def plan(
        self,
        candidates: Sequence[PlacementCandidate],
        *,
        sharing_enabled: bool,
        now: Optional[float] = None,
    ) -> PlacementPlan:
        now = self._clock() if now is None else now
        return self.commit(
            self.propose(candidates, sharing_enabled=sharing_enabled, now=now),
            now=now,
        )


class AutomaticPlacementService:
    """Periodically reconcile planner output into already configured auto workers."""

    def __init__(
        self,
        *,
        reconcile: Callable[[], None],
        period: float,
    ) -> None:
        if period <= 0:
            raise ValueError("automatic placement period must be positive")
        self._reconcile = reconcile
        self._period = period
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="drift-automatic-placement",
            daemon=True,
        )
        self._thread.start()

    def reconcile_once(self) -> None:
        self._reconcile()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.reconcile_once()
            except Exception:
                # Reconciliation errors are surfaced by the waiting placement
                # snapshot and must never take down inference or the control API.
                import logging

                logging.getLogger(__name__).exception("Automatic contribution placement failed")
            if self._stop.wait(self._period):
                break

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=min(self._period + 1, 5))
