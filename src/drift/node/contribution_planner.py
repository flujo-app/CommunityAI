"""Automatic model and contiguous-block placement for contribution workers."""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple


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
    policy_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.model_id or not self.manifest_digest:
            raise ValueError("placement candidate identity must not be empty")
        if self.priority < 0 or self.artifact_bytes < 0 or self.total_blocks < 1:
            raise ValueError("placement candidate sizes and priority must be non-negative")


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


@dataclass(frozen=True)
class PlacementPlan:
    """The selected assignment, or a fail-closed reason for not starting."""

    decision: Optional[PlacementDecision]
    reason: str
    evaluated_models: int


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
        payload = f"{self._jitter_seed}\0{digest}".encode("utf-8")
        value = int.from_bytes(hashlib.sha256(payload).digest()[:8], "big")
        return value / ((1 << 64) - 1)

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

        options = []
        for start in range(candidate.total_blocks - self.num_blocks + 1):
            window = tuple(counts[start : start + self.num_blocks])
            # Lexicographically minimize the worst and aggregate coverage. This is
            # the coverage-only equivalent of the server's throughput-aware range
            # chooser and deliberately targets scarce contiguous blocks.
            options.append(((max(window), sum(window), start), window))
        (_, _, start), window = min(options, key=lambda item: item[0])
        minimum_replicas = min(window)
        coverage_pressure = max(0, 2 - minimum_replicas) * 100.0
        preference_bonus = 20.0 if candidate.preferred else 0.0
        priority_bonus = 10.0 / (candidate.priority + 1)
        score = coverage_pressure + preference_bonus + priority_bonus + self._jitter(candidate.manifest_digest)
        end = start + self.num_blocks
        return (
            PlacementDecision(
                model_id=candidate.model_id,
                manifest_digest=candidate.manifest_digest,
                block_indices=f"{start}:{end}",
                artifact_bytes=candidate.artifact_bytes,
                replica_counts=window,
                score=score,
                reason=(
                    f"selected {start}:{end} from fresh verified coverage; " f"minimum replicas {minimum_replicas}"
                ),
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
        if current is not None and self._assigned_at is not None:
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
