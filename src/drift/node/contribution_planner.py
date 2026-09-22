"""Automatic model and contiguous-block placement for contribution workers."""

from __future__ import annotations

import hashlib
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from drift.node.route_metrics import RouteUtilityObservation, validate_route_observation

MAX_AUTOMATIC_PLACEMENT_CANDIDATES = 32
MAX_AUTOMATIC_PLACEMENT_BLOCKS = 512
MODEL_DISPERSION_POINTS = 32.0
MAX_JOINT_PLACEMENT_WORKERS = 16
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def _normalize_excluded_spans(
    value: Optional[Mapping[str, Sequence[tuple[int, int]]]],
) -> dict[str, tuple[tuple[int, int], ...]]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("automatic placement exclusions must be a mapping")
    if len(value) > MAX_AUTOMATIC_PLACEMENT_CANDIDATES:
        raise ValueError(
            f"automatic placement exclusions support at most {MAX_AUTOMATIC_PLACEMENT_CANDIDATES} manifests"
        )
    result = {}
    for manifest_digest, ranges in value.items():
        if not isinstance(manifest_digest, str) or not manifest_digest:
            raise ValueError("automatic placement exclusion manifest must be a non-empty string")
        if isinstance(ranges, (str, bytes)) or not isinstance(ranges, Sequence):
            raise ValueError("automatic placement exclusion ranges must be a sequence")
        if len(ranges) > MAX_JOINT_PLACEMENT_WORKERS:
            raise ValueError(
                f"automatic placement exclusions support at most {MAX_JOINT_PLACEMENT_WORKERS} ranges per manifest"
            )
        normalized = []
        for item in ranges:
            if (
                not isinstance(item, (tuple, list))
                or len(item) != 2
                or any(isinstance(part, bool) or not isinstance(part, int) for part in item)
                or item[0] < 0
                or item[1] <= item[0]
                or item[1] > MAX_AUTOMATIC_PLACEMENT_BLOCKS
            ):
                raise ValueError("automatic placement exclusion must be a non-empty non-negative range")
            normalized.append((item[0], item[1]))
        result[manifest_digest] = tuple(normalized)
    return result


def _overlaps_any(start: int, end: int, ranges: Sequence[tuple[int, int]]) -> bool:
    return any(start < reserved_end and reserved_start < end for reserved_start, reserved_end in ranges)


def _resolve_resource_plan(candidate: PlacementCandidate, start: int, end: int) -> Optional[PlacementResourcePlan]:
    """Resolve a pure, monotone resource claim and validate its trust boundary.

    Resolvers perform no I/O. A feasible span must have feasible subspans, with
    non-increasing artifact and memory bytes. This permits bounded sliding-window
    sizing without enumerating every possible span. None denotes infeasibility.
    """

    if not 0 <= start < end <= candidate.total_blocks:
        return None
    try:
        plan = candidate.resource_plan(start, end)
    except Exception:
        raise ValueError("automatic placement resource resolution failed") from None
    if plan is None:
        return None
    if not isinstance(plan, PlacementResourcePlan) or (plan.start_block, plan.end_block) != (start, end):
        raise ValueError("automatic placement resource resolver returned an invalid exact span")
    # Revalidate frozen instances as well: callbacks are an explicit boundary.
    PlacementResourcePlan.__post_init__(plan)
    if plan.device_memory_bytes > candidate.max_device_memory_bytes:
        return None
    if candidate.max_artifact_bytes is not None and plan.artifact_bytes > candidate.max_artifact_bytes:
        return None
    return plan


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
class PlacementResourcePlan(PlacementArtifactPlan):
    """Exact artifact and estimated device-memory claim for a feasible span."""

    device_memory_bytes: int

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in (self.start_block, self.end_block, self.artifact_bytes, self.device_memory_bytes)
        ):
            raise ValueError("placement resource ranges and byte counts must be integers")
        if self.device_memory_bytes <= 0 or not isinstance(self.artifact_set_digest, str):
            raise ValueError("placement resource memory and artifact binding are invalid")
        super().__post_init__()


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
    resource_plan: Optional[Callable[[int, int], Optional[PlacementResourcePlan]]] = field(
        default=None, compare=False, repr=False
    )
    max_device_memory_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.model_id or not self.manifest_digest:
            raise ValueError("placement candidate identity must not be empty")
        if self.priority < 0 or self.artifact_bytes < 0 or self.total_blocks < 1:
            raise ValueError("placement candidate sizes and priority must be non-negative")
        if self.total_blocks > MAX_AUTOMATIC_PLACEMENT_BLOCKS:
            raise ValueError("placement candidate exceeds the automatic placement block limit")
        if self.max_artifact_bytes is not None and self.max_artifact_bytes < 0:
            raise ValueError("placement candidate artifact budget must be non-negative")
        if self.resource_plan is not None:
            if not callable(self.resource_plan):
                raise ValueError("placement candidate resource plan must be callable")
            if any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (self.total_blocks, self.artifact_bytes, self.priority)
            ):
                raise ValueError("resource-sized placement sizes and priority must be integers")
            if (
                isinstance(self.max_device_memory_bytes, bool)
                or not isinstance(self.max_device_memory_bytes, int)
                or self.max_device_memory_bytes < 0
            ):
                raise ValueError("resource-sized placement requires an integer device-memory budget")
            if self.max_artifact_bytes is not None and (
                isinstance(self.max_artifact_bytes, bool) or not isinstance(self.max_artifact_bytes, int)
            ):
                raise ValueError("resource-sized placement requires an integer artifact budget")
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
    device_memory_bytes: Optional[int] = None


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

    def _evaluate(
        self,
        candidate: PlacementCandidate,
        *,
        excluded_spans: Sequence[tuple[int, int]] = (),
    ) -> tuple[Optional[PlacementDecision], str]:
        if candidate.policy_reason is not None:
            return None, candidate.policy_reason
        if candidate.resource_plan is None and self.num_blocks > candidate.total_blocks:
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

        if candidate.resource_plan is not None:
            return self._evaluate_resource(candidate, excluded_spans=excluded_spans)

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

        # Break equal-coverage ties by how many workers of this capacity would
        # still be needed to fill the remaining gaps. Random interior windows
        # can strand short gaps, preventing four 16-block workers from covering
        # 64 blocks despite having enough total capacity. Prefix/suffix costs
        # keep this bounded scan linear; rendezvous still disperses equal fits.
        prefix_cost = [0] * (len(counts) + 1)
        suffix_cost = [0] * (len(counts) + 1)
        gap = 0
        for index, count in enumerate(counts):
            gap = gap + 1 if count == 0 else 0
            prefix_cost[index + 1] = prefix_cost[index] + int(gap > 0 and (gap - 1) % self.num_blocks == 0)
        gap = 0
        for index in range(len(counts) - 1, -1, -1):
            gap = gap + 1 if counts[index] == 0 else 0
            suffix_cost[index] = suffix_cost[index + 1] + int(gap > 0 and (gap - 1) % self.num_blocks == 0)
        maxima: deque[int] = deque()
        window_sum = 0
        best_key = None
        best_start = 0
        unreserved_window = False
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
            if _overlaps_any(start, end, excluded_spans):
                continue
            unreserved_window = True
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
                prefix_cost[start] + suffix_cost[end],
                self._range_jitter(candidate.manifest_digest, start, end),
                start,
            )
            if best_key is None or key < best_key:
                best_key = key
                best_start = start
        if best_key is None:
            if not unreserved_window:
                return None, f"every {self.num_blocks}-block span is reserved by another local worker"
            return None, (
                f"every {self.num_blocks}-block artifact set exceeds the "
                f"{candidate.max_artifact_bytes}-byte disk budget"
            )
        start = best_start
        end = start + self.num_blocks
        selected_artifacts = artifact_plans.get((start, end))
        return self._span_decision(candidate, start, end, selected_artifacts), ""

    def _evaluate_resource(
        self,
        candidate: PlacementCandidate,
        *,
        excluded_spans: Sequence[tuple[int, int]] = (),
        maximum_blocks: int = MAX_AUTOMATIC_PLACEMENT_BLOCKS,
    ) -> tuple[Optional[PlacementDecision], str]:
        best = None
        best_key = None
        end = 0
        for start in range(candidate.total_blocks):
            end = max(end, start)
            selected = None
            if end > start and not _overlaps_any(start, end, excluded_spans):
                selected = _resolve_resource_plan(candidate, start, end)
            while (
                end < candidate.total_blocks
                and end - start < maximum_blocks
                and not _overlaps_any(start, end + 1, excluded_spans)
            ):
                proposed = _resolve_resource_plan(candidate, start, end + 1)
                if proposed is None:
                    break
                end += 1
                selected = proposed
            if selected is None:
                continue
            counts = candidate.health["replica_counts"][start:end]
            key = (
                -sum(count == 0 for count in counts),
                -(end - start),
                max(counts),
                sum(counts),
                # Prefer an available interval's edge over splitting it. This
                # matters when the same scan supplies fair matching anchors.
                int(start > 0 and not any(reserved_end == start for _, reserved_end in excluded_spans))
                + int(
                    end < candidate.total_blocks
                    and not any(reserved_start == end for reserved_start, _ in excluded_spans)
                ),
                self._range_jitter(candidate.manifest_digest, start, end),
                start,
            )
            if best_key is None or key < best_key:
                best_key = key
                best = self._span_decision(candidate, start, end, selected)
        return best, "no exact span fits the current device-memory and artifact budgets" if best is None else ""

    def _span_decision(
        self, candidate: PlacementCandidate, start: int, end: int, artifacts: Optional[PlacementArtifactPlan]
    ) -> PlacementDecision:
        artifact_bytes = candidate.artifact_bytes if artifacts is None else artifacts.artifact_bytes
        artifact_set_digest = None if artifacts is None else artifacts.artifact_set_digest
        window = tuple(candidate.health["replica_counts"][start:end])
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
        return PlacementDecision(
            model_id=candidate.model_id,
            manifest_digest=candidate.manifest_digest,
            block_indices=f"{start}:{end}",
            artifact_bytes=artifact_bytes,
            replica_counts=window,
            score=score,
            reason=reason,
            artifact_set_digest=artifact_set_digest,
            device_memory_bytes=artifacts.device_memory_bytes if isinstance(artifacts, PlacementResourcePlan) else None,
        )

    def propose(
        self,
        candidates: Sequence[PlacementCandidate],
        *,
        sharing_enabled: bool,
        now: Optional[float] = None,
        excluded_spans: Optional[Mapping[str, Sequence[tuple[int, int]]]] = None,
    ) -> PlacementPlan:
        now = self._clock() if now is None else now
        exclusions = _normalize_excluded_spans(excluded_spans)
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
            decision, reason = self._evaluate(
                candidate,
                excluded_spans=exclusions.get(candidate.manifest_digest, ()),
            )
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
            try:
                start, end = (int(value) for value in self._current.block_indices.split(":"))
            except (AttributeError, TypeError, ValueError):
                current_assignment_is_eligible = False
            else:
                current_assignment_is_eligible = (
                    (current_candidate.resource_plan is not None or end - start == self.num_blocks)
                    and 0 <= start < end <= current_candidate.total_blocks
                    and not _overlaps_any(start, end, exclusions.get(current_candidate.manifest_digest, ()))
                )
            if current_assignment_is_eligible and current_candidate.resource_plan is not None:
                resource = _resolve_resource_plan(current_candidate, start, end)
                current_assignment_is_eligible = (
                    resource is not None
                    and resource.artifact_bytes == self._current.artifact_bytes
                    and resource.artifact_set_digest == self._current.artifact_set_digest
                    and resource.device_memory_bytes == self._current.device_memory_bytes
                )
            elif current_assignment_is_eligible and current_candidate.artifact_plans:
                plan = next(
                    (
                        plan
                        for plan in current_candidate.artifact_plans
                        if (plan.start_block, plan.end_block) == (start, end)
                    ),
                    None,
                )
                current_assignment_is_eligible = current_assignment_is_eligible and (
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
            elif best.manifest_digest == self._current.manifest_digest:
                counts = current_candidate.health["replica_counts"]
                old_start, old_end = map(int, self._current.block_indices.split(":"))
                new_start, new_end = map(int, best.block_indices.split(":"))
                # Slow joins must not move a coverage gap around the model.
                # Permit abandoning unique blocks only for a net coverage gain;
                # redundant overlapping workers can still move to fill a gap.
                lost = sum(
                    counts[index] == 1 and not new_start <= index < new_end for index in range(old_start, old_end)
                )
                gained = sum(
                    counts[index] == 0 for index in range(new_start, new_end) if not old_start <= index < old_end
                )
                if lost and gained <= lost:
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

    @property
    def current_decision(self) -> Optional[PlacementDecision]:
        """Return the last externally accepted decision without changing it."""

        return self._current

    def plan(
        self,
        candidates: Sequence[PlacementCandidate],
        *,
        sharing_enabled: bool,
        now: Optional[float] = None,
        excluded_spans: Optional[Mapping[str, Sequence[tuple[int, int]]]] = None,
    ) -> PlacementPlan:
        now = self._clock() if now is None else now
        return self.commit(
            self.propose(
                candidates,
                sharing_enabled=sharing_enabled,
                now=now,
                excluded_spans=excluded_spans,
            ),
            now=now,
        )


def _normalize_worker_mapping(value: Mapping[str, Any], label: str) -> dict[str, tuple[str, Any]]:
    if not isinstance(value, Mapping):
        raise ValueError(f"joint placement {label} must be a mapping")
    if len(value) > MAX_JOINT_PLACEMENT_WORKERS:
        raise ValueError(f"joint placement {label} supports at most {MAX_JOINT_PLACEMENT_WORKERS} workers")
    result = {}
    for worker_id, item in value.items():
        if not isinstance(worker_id, str) or _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError(f"joint placement {label} worker IDs must be canonical and at most 64 characters")
        normalized = worker_id.casefold()
        if normalized in result:
            raise ValueError(f"joint placement {label} contains case-insensitively duplicate worker IDs")
        result[normalized] = (worker_id, item)
    return result


def _retained_span(
    plan: PlacementPlan,
    planner: AutomaticContributionPlanner,
    candidates: Sequence[PlacementCandidate],
) -> tuple[str, int, int]:
    if not isinstance(plan, PlacementPlan):
        raise ValueError("joint placement retained plans must be PlacementPlan instances")
    if plan.decision is None or not plan.intent_published or not plan.remote_acknowledged:
        raise ValueError("joint placement can retain only acknowledged non-empty plans")
    if (
        isinstance(plan.evaluated_models, bool)
        or not isinstance(plan.evaluated_models, int)
        or plan.evaluated_models < 0
    ):
        raise ValueError("joint placement retained plan has an invalid evaluated-model count")
    if not isinstance(plan.reason, str) or not plan.reason:
        raise ValueError("joint placement retained plan has an invalid reason")

    decision = plan.decision
    if planner.current_decision != decision:
        raise ValueError("joint placement can retain only the planner's last accepted decision")
    if (
        not isinstance(decision.model_id, str)
        or not decision.model_id
        or not isinstance(decision.manifest_digest, str)
        or not decision.manifest_digest
        or not isinstance(decision.block_indices, str)
    ):
        raise ValueError("joint placement retained decision has an invalid identity")
    match = re.fullmatch(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)", decision.block_indices)
    if match is None:
        raise ValueError("joint placement retained decision has an invalid block range")
    start, end = int(match.group(1)), int(match.group(2))
    candidate = next(
        (
            item
            for item in candidates
            if item.manifest_digest == decision.manifest_digest and item.model_id == decision.model_id
        ),
        None,
    )
    resource_sized = candidate is not None and candidate.resource_plan is not None
    if not resource_sized and end - start != planner.num_blocks:
        raise ValueError("joint placement retained decision has a mismatched block count")
    if (
        isinstance(decision.artifact_bytes, bool)
        or not isinstance(decision.artifact_bytes, int)
        or decision.artifact_bytes < 0
        or not isinstance(decision.replica_counts, tuple)
        or len(decision.replica_counts) != end - start
        or any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in decision.replica_counts)
        or isinstance(decision.score, bool)
        or not isinstance(decision.score, (int, float))
        or not math.isfinite(decision.score)
        or not isinstance(decision.reason, str)
        or not decision.reason
        or (
            decision.artifact_set_digest is not None
            and (
                not isinstance(decision.artifact_set_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", decision.artifact_set_digest) is None
            )
        )
    ):
        raise ValueError("joint placement retained decision has invalid bounded metadata")

    if candidate is None or not 0 <= start < end <= candidate.total_blocks:
        raise ValueError("joint placement retained decision does not match a current candidate")
    if resource_sized:
        if (
            isinstance(decision.device_memory_bytes, bool)
            or not isinstance(decision.device_memory_bytes, int)
            or decision.device_memory_bytes <= 0
            or decision.artifact_set_digest is None
        ):
            raise ValueError("joint placement retained decision has invalid resource metadata")
        # Resource budgets and artifact metadata can change. The resource path
        # re-resolves this old assignment before retaining it, rather than
        # rejecting the whole replan when it legitimately needs to shrink.
    elif candidate.artifact_plans:
        artifact_plan = next(
            (item for item in candidate.artifact_plans if (item.start_block, item.end_block) == (start, end)),
            None,
        )
        if (
            artifact_plan is None
            or artifact_plan.artifact_bytes != decision.artifact_bytes
            or artifact_plan.artifact_set_digest != decision.artifact_set_digest
        ):
            raise ValueError("joint placement retained decision has a stale artifact binding")
    elif decision.artifact_bytes != candidate.artifact_bytes or decision.artifact_set_digest is not None:
        raise ValueError("joint placement retained decision has a stale artifact binding")
    return decision.manifest_digest, start, end


def propose_joint_placements(
    planners: Mapping[str, AutomaticContributionPlanner],
    candidates_by_worker: Mapping[str, Sequence[PlacementCandidate]],
    *,
    sharing_enabled: bool,
    retained_plans: Optional[Mapping[str, PlacementPlan]] = None,
    now: Optional[float] = None,
) -> dict[str, PlacementPlan]:
    """Propose a bounded, deterministic set of non-overlapping local spans.

    This helper is deliberately side-effect free with respect to planner
    hysteresis. Callers publish and validate external intent leases first, then
    call each planner's ``commit`` only for the final accepted plan. Partial
    per-worker acceptance is not safe by itself: if publication falls back to an
    old span, the service MUST revalidate the complete final mapping and retire
    every conflicting old worker as a batch before installing any new plan.
    """

    normalized_planners = _normalize_worker_mapping(planners, "planners")
    normalized_candidates = _normalize_worker_mapping(candidates_by_worker, "candidate sets")
    if set(normalized_planners) != set(normalized_candidates):
        raise ValueError("joint placement planners and candidate sets must have identical worker IDs")
    if len(normalized_planners) > MAX_JOINT_PLACEMENT_WORKERS:
        raise ValueError(f"joint placement supports at most {MAX_JOINT_PLACEMENT_WORKERS} workers")
    if len({id(item[1]) for item in normalized_planners.values()}) != len(normalized_planners):
        raise ValueError("joint placement workers must not share planner instances")

    candidate_sets = {}
    for normalized, (_, values) in normalized_candidates.items():
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ValueError("joint placement candidates must be sequences")
        if len(values) > MAX_AUTOMATIC_PLACEMENT_CANDIDATES:
            raise ValueError(
                f"joint placement supports at most {MAX_AUTOMATIC_PLACEMENT_CANDIDATES} candidates per worker"
            )
        values = tuple(values)
        if any(not isinstance(item, PlacementCandidate) for item in values):
            raise ValueError("joint placement candidates must be PlacementCandidate instances")
        candidate_sets[normalized] = values

    if (
        len({candidate.manifest_digest for values in candidate_sets.values() for candidate in values})
        > MAX_AUTOMATIC_PLACEMENT_CANDIDATES
    ):
        raise ValueError(f"joint placement supports at most {MAX_AUTOMATIC_PLACEMENT_CANDIDATES} distinct manifests")

    normalized_retained = _normalize_worker_mapping(
        {} if retained_plans is None else retained_plans,
        "retained plans",
    )
    if not set(normalized_retained).issubset(normalized_planners):
        raise ValueError("joint placement retained plans contain an unknown worker")

    reservations: dict[str, list[tuple[int, int]]] = {}
    retained_spans = {}
    for normalized in sorted(normalized_retained):
        plan = normalized_retained[normalized][1]
        planner = normalized_planners[normalized][1]
        manifest_digest, start, end = _retained_span(plan, planner, candidate_sets[normalized])
        ranges = reservations.setdefault(manifest_digest, [])
        if _overlaps_any(start, end, ranges):
            raise ValueError("joint placement retained plans overlap on one manifest")
        ranges.append((start, end))
        retained_spans[normalized] = (manifest_digest, start, end)

    if any(candidate.resource_plan is not None for values in candidate_sets.values() for candidate in values):
        return _propose_resource_joint(
            normalized_planners,
            candidate_sets,
            normalized_retained,
            sharing_enabled=sharing_enabled,
            now=now,
        )

    order = sorted(
        normalized_planners,
        key=lambda worker_id: (
            0 if worker_id in retained_spans else 1,
            -normalized_planners[worker_id][1].num_blocks,
            worker_id,
        ),
    )
    result = {}
    accepted_spans = {}
    for normalized in order:
        previous = retained_spans.get(normalized)
        if previous is not None:
            manifest_digest, start, end = previous
            reservations[manifest_digest].remove((start, end))
        planner = normalized_planners[normalized][1]
        proposal = planner.propose(
            candidate_sets[normalized],
            sharing_enabled=sharing_enabled,
            now=now,
            excluded_spans=reservations,
        )
        decision = proposal.decision
        if decision is not None:
            match = re.fullmatch(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)", decision.block_indices)
            if match is None:
                raise ValueError("joint placement planner returned an invalid block range")
            start, end = int(match.group(1)), int(match.group(2))
            ranges = reservations.setdefault(decision.manifest_digest, [])
            if _overlaps_any(start, end, ranges):
                raise ValueError("joint placement planner returned overlapping local spans")
            ranges.append((start, end))
            accepted_spans[normalized] = (decision.manifest_digest, start, end)
        original_worker_id = normalized_planners[normalized][0]
        result[original_worker_id] = proposal

    spans_by_manifest: dict[str, list[tuple[int, int]]] = {}
    for manifest_digest, start, end in accepted_spans.values():
        ranges = spans_by_manifest.setdefault(manifest_digest, [])
        if _overlaps_any(start, end, ranges):
            raise ValueError("joint placement result contains overlapping local spans")
        ranges.append((start, end))
    return result


def _propose_resource_joint(planners, candidate_sets, retained, *, sharing_enabled, now):
    """Match feasible cards first, then grow disjoint contiguous spans fairly.

    Singleton augmenting paths maximize participating resource-sized workers.
    Progressive growth uses monotone resolvers and bounded neighbor shifts; it
    does not claim globally optimal interval packing for heterogeneous layers.
    Every successful shift chain adds one occupied block, so growth terminates.
    """

    preferred = {
        worker: planner.propose(candidate_sets[worker], sharing_enabled=sharing_enabled, now=now)
        for worker, (_, planner) in planners.items()
    }
    fixed_workers = {
        worker
        for worker, proposal in preferred.items()
        if not any(candidate.resource_plan is not None for candidate in candidate_sets[worker])
        or (
            proposal.decision is not None
            and any(
                candidate.manifest_digest == proposal.decision.manifest_digest and candidate.resource_plan is None
                for candidate in candidate_sets[worker]
            )
        )
    }
    fixed = propose_joint_placements(
        {planners[worker][0]: planners[worker][1] for worker in fixed_workers},
        {
            planners[worker][0]: tuple(
                candidate for candidate in candidate_sets[worker] if candidate.resource_plan is None
            )
            for worker in fixed_workers
        },
        sharing_enabled=sharing_enabled,
        retained_plans={
            planners[worker][0]: retained[worker][1]
            for worker in fixed_workers
            if worker in retained
            and any(
                candidate.manifest_digest == retained[worker][1].decision.manifest_digest
                and candidate.resource_plan is None
                for candidate in candidate_sets[worker]
            )
        },
        now=now,
    )
    fixed_slots = set()
    for plan in fixed.values():
        if plan.decision is not None:
            start, end = map(int, plan.decision.block_indices.split(":"))
            fixed_slots.update((plan.decision.manifest_digest, block) for block in range(start, end))

    resource_workers = sorted(set(planners) - fixed_workers)
    # Offers are bounded by workers * models * blocks, not all possible spans.
    offers = {worker: {} for worker in resource_workers}
    eligible = {}
    for worker in resource_workers:
        planner = planners[worker][1]
        if not sharing_enabled:
            continue
        for candidate in candidate_sets[worker]:
            if candidate.resource_plan is not None:
                decision, _ = planner._evaluate(candidate)
                if decision is not None:
                    eligible[worker, candidate.manifest_digest] = candidate, decision

    # Singleton counts alone cannot distinguish a card that fits every layer
    # individually but only fits useful multi-layer spans at one model end.
    # Resolve provisional fair spans, constrained cards first, and use those
    # exact starts as matching anchors. These are hints, not reservations: the
    # augmenting matcher can still move them to maximize participating cards.
    anchors = {}
    target_spans = {}
    for digest in sorted({digest for _, digest in eligible}):
        members = sorted(
            (worker for worker, item_digest in eligible if item_digest == digest),
            key=lambda worker: (len(eligible[worker, digest][1].replica_counts), worker),
        )
        reserved = []
        for plan in fixed.values():
            if plan.decision is not None and plan.decision.manifest_digest == digest:
                reserved.append(tuple(map(int, plan.decision.block_indices.split(":"))))
        for index, worker in enumerate(members):
            candidate, decision = eligible[worker, digest]
            remaining = candidate.total_blocks - sum(end - start for start, end in reserved)
            fair_blocks = max(1, math.ceil(remaining / (len(members) - index)))
            provisional, _ = planners[worker][1]._evaluate_resource(
                candidate, excluded_spans=reserved, maximum_blocks=min(fair_blocks, len(decision.replica_counts))
            )
            if provisional is not None:
                start, end = map(int, provisional.block_indices.split(":"))
                anchors[worker, digest] = start
                target_spans[worker, digest] = start, end
                reserved.append((start, end))

    for worker in resource_workers:
        planner = planners[worker][1]
        if not sharing_enabled:
            continue
        for candidate in candidate_sets[worker]:
            if (worker, candidate.manifest_digest) not in eligible or candidate.resource_plan is None:
                continue
            digest = candidate.manifest_digest
            anchor = anchors.get((worker, digest), 0)
            current = preferred[worker].decision
            for block in range(candidate.total_blocks):
                slot = digest, block
                if slot in fixed_slots:
                    continue
                resource = _resolve_resource_plan(candidate, block, block + 1)
                if resource is None:
                    continue
                decision = planner._span_decision(candidate, block, block + 1, resource)
                rank = (
                    0 if current is not None and current.manifest_digest == digest else 1,
                    -decision.score,
                    abs(block - anchor),
                    planner._range_jitter(digest, block, block + 1),
                    digest,
                    block,
                )
                previous = offers[worker].get(slot)
                if previous is None or rank < previous[0]:
                    offers[worker][slot] = rank, candidate, resource

    ranked_offers = {worker: sorted(values, key=lambda item: values[item][0]) for worker, values in offers.items()}

    def match(workers, blocked=()):
        blocked = set(blocked)
        owners = {}
        selected = {}

        def augment(worker, visited):
            for slot in ranked_offers[worker]:
                if slot in blocked or slot in visited:
                    continue
                visited.add(slot)
                other = owners.get(slot)
                if other is None or augment(other, visited):
                    owners[slot] = worker
                    selected[worker] = slot
                    return True
            return False

        for worker in sorted(workers, key=lambda item: (len(offers[item]), item)):
            augment(worker, set())
        return selected

    matched = match(resource_workers)
    kept = {}
    kept_slots = set()
    for worker in sorted(matched):
        planner = planners[worker][1]
        decision = preferred[worker].decision
        if worker not in retained or decision is None or decision != planner.current_decision:
            continue
        candidate = next(
            (item for item in candidate_sets[worker] if item.manifest_digest == decision.manifest_digest), None
        )
        if candidate is None or candidate.resource_plan is None:
            continue
        start, end = map(int, decision.block_indices.split(":"))
        resource = _resolve_resource_plan(candidate, start, end)
        slots = {(decision.manifest_digest, block) for block in range(start, end)}
        if (
            resource is not None
            and resource.artifact_bytes == decision.artifact_bytes
            and resource.artifact_set_digest == decision.artifact_set_digest
            and resource.device_memory_bytes == decision.device_memory_bytes
            and not slots.intersection(fixed_slots | kept_slots)
        ):
            kept[worker] = candidate, resource
            kept_slots.update(slots)
    if kept:
        rematched = match([worker for worker in resource_workers if worker not in kept], kept_slots)
        if len(rematched) + len(kept) == len(matched):
            matched = rematched
        else:
            # Residency cannot monopolize a model when another feasible card
            # can participate after a coordinated split of the old assignment.
            kept = {}

    assigned = dict(kept)
    for worker, slot in matched.items():
        _, candidate, resource = offers[worker][slot]
        assigned[worker] = candidate, resource

    def occupancy(values):
        occupied = dict.fromkeys(fixed_slots, "")
        for worker, (candidate, resource) in values.items():
            for block in range(resource.start_block, resource.end_block):
                occupied[candidate.manifest_digest, block] = worker
        return occupied

    def grow(worker, direction, occupied):
        changes = {}
        candidate, old = assigned[worker]
        start = old.start_block - (direction < 0)
        end = old.end_block + (direction > 0)
        resource = _resolve_resource_plan(candidate, start, end)
        if resource is None:
            return None

        def shift(other, visiting):
            if not other or other in kept or other in visiting:
                return False
            visiting.add(other)
            peer_candidate, peer = assigned[other]
            shifted = _resolve_resource_plan(peer_candidate, peer.start_block + direction, peer.end_block + direction)
            if shifted is None:
                return False
            frontier = shifted.start_block if direction < 0 else shifted.end_block - 1
            neighbor = occupied.get((peer_candidate.manifest_digest, frontier))
            if neighbor is not None and neighbor != other and not shift(neighbor, visiting):
                return False
            changes[other] = peer_candidate, shifted
            return True

        frontier = start if direction < 0 else end - 1
        neighbor = occupied.get((candidate.manifest_digest, frontier))
        if neighbor is not None and not shift(neighbor, {worker}):
            return None
        changes[worker] = candidate, resource
        return changes

    occupied = occupancy(assigned)
    while True:
        changed = False
        order = sorted(
            assigned, key=lambda worker: (assigned[worker][1].end_block - assigned[worker][1].start_block, worker)
        )
        for worker in order:
            if worker in kept:
                continue
            choices = []
            for direction in (-1, 1):
                changes = grow(worker, direction, occupied)
                if changes is None:
                    continue
                candidate, resource = changes[worker]
                counts = candidate.health["replica_counts"][resource.start_block : resource.end_block]
                target_start, target_end = target_spans.get(
                    (worker, candidate.manifest_digest), (resource.start_block, resource.end_block)
                )
                key = (
                    -sum(count == 0 for count in counts),
                    sum(counts),
                    -max(0, min(resource.end_block, target_end) - max(resource.start_block, target_start)),
                    resource.device_memory_bytes,
                    len(changes),
                    planners[worker][1]._range_jitter(
                        candidate.manifest_digest, resource.start_block, resource.end_block
                    ),
                )
                choices.append((key, changes))
            if choices:
                changes = min(choices, key=lambda item: item[0])[1]
                # A growth adds one boundary block; every pushed neighbor moves
                # by one. Updating only boundaries avoids rebuilding all slots
                # for every successful probe at the 512-block limit.
                for other, (candidate, resource) in changes.items():
                    previous = assigned[other][1]
                    if previous.start_block < resource.start_block:
                        occupied.pop((candidate.manifest_digest, previous.start_block))
                    if previous.end_block > resource.end_block:
                        occupied.pop((candidate.manifest_digest, previous.end_block - 1))
                for other, (candidate, resource) in changes.items():
                    previous = assigned[other][1]
                    if resource.start_block < previous.start_block:
                        occupied[candidate.manifest_digest, resource.start_block] = other
                    if resource.end_block > previous.end_block:
                        occupied[candidate.manifest_digest, resource.end_block - 1] = other
                assigned.update(changes)
                changed = True
        if not changed:
            break

    result = dict(fixed)
    for worker in resource_workers:
        original, planner = planners[worker]
        if worker in assigned:
            candidate, resource = assigned[worker]
            decision = (
                planner.current_decision
                if worker in kept
                else planner._span_decision(candidate, resource.start_block, resource.end_block, resource)
            )
            result[original] = PlacementPlan(decision, decision.reason, len(candidate_sets[worker]))
        else:
            reason = (
                preferred[worker].reason
                if not offers[worker]
                else "no disjoint resource-feasible span remains for this worker"
            )
            result[original] = PlacementPlan(None, reason, len(candidate_sets[worker]))
    return result


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
