"""Pure resource allocation fixtures; no accepted multi-auto config or GPU claim."""

import hashlib
from dataclasses import replace

import pytest

from drift.node.contribution_planner import (
    AutomaticContributionPlanner,
    PlacementCandidate,
    PlacementResourcePlan,
    propose_joint_placements,
)


def candidate(*, layers=(10,) * 64, budget=640, disk_budget=None, digest="model-a", counts=None, calls=None):
    def resolve(start, end):
        if calls is not None:
            calls.append((start, end))
        artifact_digest = hashlib.sha256(f"{digest}:{start}:{end}".encode()).hexdigest()
        return PlacementResourcePlan(start, end, (end - start) * 3, artifact_digest, 5 + sum(layers[start:end]))

    return PlacementCandidate(
        model_id=digest,
        manifest_digest=digest,
        priority=0,
        preferred=False,
        artifact_bytes=len(layers) * 3,
        total_blocks=len(layers),
        health={"status": "incomplete", "last_updated_age": 0, "replica_counts": list(counts or (0,) * len(layers))},
        resource_plan=resolve,
        max_device_memory_bytes=budget,
        max_artifact_bytes=disk_budget,
    )


def planners(names, *, residency=0, blocks=1):
    return {
        name: AutomaticContributionPlanner(
            num_blocks=blocks,
            jitter_seed=name,
            minimum_residency_seconds=residency,
            cooldown_seconds=residency,
        )
        for name in names
    }


def spans(plans):
    return {
        worker: (plan.decision.manifest_digest, tuple(map(int, plan.decision.block_indices.split(":"))))
        for worker, plan in plans.items()
        if plan.decision is not None
    }


def lengths(plans):
    return {worker: end - start for worker, (_, (start, end)) in spans(plans).items()}


def verify(plans, candidates):
    occupied = set()
    for worker, plan in plans.items():
        if plan.decision is None:
            continue
        decision = plan.decision
        item = next(item for item in candidates[worker] if item.manifest_digest == decision.manifest_digest)
        start, end = map(int, decision.block_indices.split(":"))
        resolved = item.resource_plan(start, end)
        assert decision.device_memory_bytes == resolved.device_memory_bytes <= item.max_device_memory_bytes
        assert decision.artifact_bytes == resolved.artifact_bytes
        assert decision.artifact_set_digest == resolved.artifact_set_digest
        if item.max_artifact_bytes is not None:
            assert decision.artifact_bytes <= item.max_artifact_bytes
        slots = {(decision.manifest_digest, block) for block in range(start, end)}
        assert not occupied.intersection(slots)
        occupied.update(slots)


def test_eight_equal_cards_grow_to_useful_fair_spans_without_commit():
    workers = planners([f"gpu-{index}" for index in range(8)])
    candidates = {worker: (candidate(),) for worker in workers}
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert lengths(result) == {worker: 8 for worker in workers}
    verify(result, candidates)
    assert all(planner.current_decision is None for planner in workers.values())
    assert all(planner._assigned_at is None and planner._last_switch_at is None for planner in workers.values())


def test_resource_allocation_is_independent_of_mapping_and_candidate_order():
    names = [f"gpu-{index}" for index in range(8)]

    def run(order, reverse_candidates=False):
        workers = planners(order)
        candidates = {
            worker: tuple(
                candidate(digest=digest) for digest in ("model-a", "model-b")[:: -1 if reverse_candidates else 1]
            )
            for worker in reversed(order)
        }
        return spans(propose_joint_placements(workers, candidates, sharing_enabled=True, now=0))

    assert run(names) == run(list(reversed(names)), True)


@pytest.mark.parametrize("capacities", [(1, 7), (1, 2, 5), (2, 2, 2, 2), (8, 8)])
def test_heterogeneous_capacities_cover_without_first_card_monopoly(capacities):
    workers = planners([f"gpu-{index}" for index in range(len(capacities))])
    candidates = {
        worker: (candidate(layers=(10,) * 8, budget=5 + 10 * capacity),)
        for worker, capacity in zip(workers, capacities)
    }
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert len(spans(result)) == len(workers)
    assert sum(lengths(result).values()) == 8
    assert all(lengths(result)[worker] <= capacity for worker, capacity in zip(workers, capacities))
    verify(result, candidates)


def test_singleton_matching_reassigns_flexible_card_for_only_feasible_layer():
    workers = planners(["flexible", "small"])
    candidates = {
        "flexible": (candidate(layers=(100, 1), budget=106),),
        "small": (candidate(layers=(100, 1), budget=6),),
    }
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert spans(result)["small"][1] == (1, 2)
    assert spans(result)["flexible"][1] == (0, 1)
    verify(result, candidates)


def test_heterogeneous_layer_geometry_and_artifact_budget_are_exact():
    workers = planners(["gpu"])
    candidates = {"gpu": (candidate(layers=(80, 5, 5, 5, 80), budget=20, disk_budget=6),)}
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    start, end = spans(result)["gpu"][1]
    assert 1 <= start < end <= 4
    assert end - start == 2
    verify(result, candidates)


@pytest.mark.parametrize("reverse_layers", [False, True])
@pytest.mark.parametrize("small_name,large_name", [("gpu-0", "gpu-1"), ("gpu-1", "gpu-0")])
def test_constrained_span_anchors_keep_small_card_from_trapping_free_layers(reverse_layers, small_name, large_name):
    layers = tuple(100 * (index + 1) + 10 for index in range(8))
    small_budget = 5 + sum(layers[:4])
    if reverse_layers:
        layers = layers[::-1]
    workers = planners([small_name, large_name])
    candidates = {
        small_name: (candidate(layers=layers, budget=small_budget),),
        large_name: (candidate(layers=layers, budget=5 + sum(layers)),),
    }
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert lengths(result) == {small_name: 4, large_name: 4}
    assert spans(result)[small_name][1] == ((4, 8) if reverse_layers else (0, 4))
    verify(result, candidates)


def test_model_specific_budget_does_not_inherit_placeholder_length():
    worker = planners(["gpu"], blocks=32)["gpu"]
    expensive = candidate(layers=(100,) * 8, budget=45, digest="expensive")
    useful = candidate(layers=(10,) * 8, budget=45, digest="useful")
    plan = worker.propose((expensive, useful), sharing_enabled=True, now=0)
    assert plan.decision.manifest_digest == "useful"
    assert len(plan.decision.replica_counts) == 4
    assert plan.decision.device_memory_bytes == 45
    assert worker.current_decision is None


def test_budget_shrink_forces_replan_during_residency_without_commit():
    workers = planners(["gpu"], residency=1000)
    original = {"gpu": (candidate(layers=(10,) * 8, budget=85),)}
    old = propose_joint_placements(workers, original, sharing_enabled=True, now=0)
    workers["gpu"].commit(old["gpu"], now=0)
    retained = {"gpu": replace(old["gpu"], intent_published=True, remote_acknowledged=True)}
    reduced = {"gpu": (candidate(layers=(10,) * 8, budget=25),)}
    result = propose_joint_placements(workers, reduced, sharing_enabled=True, retained_plans=retained, now=1)
    assert lengths(result) == {"gpu": 2}
    assert workers["gpu"].current_decision == old["gpu"].decision
    verify(result, reduced)


def test_new_card_gets_work_from_resident_whole_model_assignment():
    workers = planners(["old", "new"], residency=1000)
    candidates = {worker: (candidate(layers=(10,) * 8, budget=85),) for worker in workers}
    old = workers["old"].plan(candidates["old"], sharing_enabled=True, now=0)
    retained = {"old": replace(old, intent_published=True, remote_acknowledged=True)}
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, retained_plans=retained, now=1)
    assert lengths(result) == {"old": 4, "new": 4}
    assert workers["old"].current_decision == old.decision
    assert workers["new"].current_decision is None
    verify(result, candidates)


def test_acknowledged_fair_spans_stay_stable_during_residency():
    workers = planners(["one", "two"], residency=1000)
    candidates = {worker: (candidate(layers=(10,) * 8, budget=85),) for worker in workers}
    first = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    for worker, planner in workers.items():
        planner.commit(first[worker], now=0)
    retained = {
        worker: replace(plan, intent_published=True, remote_acknowledged=True) for worker, plan in first.items()
    }
    second = propose_joint_placements(workers, candidates, sharing_enabled=True, retained_plans=retained, now=1)
    assert spans(first) == spans(second)


@pytest.mark.parametrize("result", ["wrong-type", "wrong-span", "exception"])
def test_malformed_resource_callback_fails_closed_without_mutating_planner(result):
    item = candidate(layers=(10,) * 4)

    def malformed(start, end):
        if result == "wrong-type":
            return object()
        if result == "wrong-span":
            return PlacementResourcePlan(start + 1, end + 1, 3, "0" * 64, 15)
        raise RuntimeError("private identity path and token")

    item = replace(item, resource_plan=malformed)
    worker = planners(["gpu"])["gpu"]
    with pytest.raises(ValueError, match="resource") as error:
        worker.propose((item,), sharing_enabled=True)
    assert "private" not in str(error.value)
    assert worker.current_decision is None


@pytest.mark.parametrize("memory", [True, 0, -1, 1.5, float("inf")])
def test_resource_claim_rejects_nonpositive_or_noninteger_device_memory(memory):
    with pytest.raises(ValueError):
        PlacementResourcePlan(0, 1, 3, "0" * 64, memory)


def test_unavailable_metadata_and_no_feasible_singleton_stay_disabled():
    workers = planners(["policy", "budget"])
    candidates = {
        "policy": (replace(candidate(), resource_plan=None, policy_reason="verified metadata unavailable"),),
        "budget": (candidate(budget=1),),
    }
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert not spans(result)
    assert "metadata unavailable" in result["policy"].reason


def test_single_worker_sizing_has_linear_probe_bound_at_512_blocks():
    calls = []
    item = candidate(layers=(1,) * 512, budget=517, calls=calls)
    worker = planners(["gpu"])["gpu"]
    result = worker.propose((item,), sharing_enabled=True, now=0)
    assert result.decision.block_indices == "0:512"
    assert len(calls) <= 3 * 512


def test_matching_moves_flexible_worker_to_another_model_to_use_every_card():
    workers = planners(["flexible", "only-a", "only-b"])
    candidates = {
        "flexible": tuple(candidate(digest=digest, layers=(10,), budget=15) for digest in ("a", "b", "c")),
        "only-a": (candidate(digest="a", layers=(10,), budget=15),),
        "only-b": (candidate(digest="b", layers=(10,), budget=15),),
    }
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert {worker: digest for worker, (digest, _) in spans(result).items()} == {
        "flexible": "c",
        "only-a": "a",
        "only-b": "b",
    }
    verify(result, candidates)


def test_legacy_fixed_worker_keeps_capacity_and_resource_worker_uses_remainder():
    workers = planners(["fixed", "sized"], blocks=3)
    dynamic = candidate(layers=(10,) * 8, budget=85)
    fixed = replace(dynamic, resource_plan=None, max_device_memory_bytes=None)
    candidates = {"fixed": (fixed,), "sized": (dynamic,)}
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert lengths(result) == {"fixed": 3, "sized": 5}
    assert result["fixed"].decision.device_memory_bytes is None
    fixed_span = spans(result)["fixed"][1]
    sized_span = spans(result)["sized"][1]
    assert fixed_span[1] <= sized_span[0] or sized_span[1] <= fixed_span[0]


def test_fresh_coverage_utility_precedes_model_preference_and_range_ties():
    worker = planners(["gpu"])["gpu"]
    replicated = replace(candidate(digest="preferred", layers=(10,) * 8, budget=25, counts=(2,) * 8), preferred=True)
    gap = candidate(digest="gap", layers=(10,) * 8, budget=25, counts=(3, 3, 0, 0, 3, 3, 3, 3))
    result = propose_joint_placements({"gpu": worker}, {"gpu": (replicated, gap)}, sharing_enabled=True, now=0)
    assert spans(result)["gpu"] == ("gap", (2, 4))


def test_resource_callback_none_is_infeasible_and_budget_grow_can_be_replanned():
    worker = planners(["gpu"])["gpu"]
    absent = replace(candidate(layers=(10,) * 8), resource_plan=lambda start, end: None)
    assert worker.propose((absent,), sharing_enabled=True).decision is None
    small = worker.plan((candidate(layers=(10,) * 8, budget=25),), sharing_enabled=True, now=0)
    larger = worker.propose((candidate(layers=(10,) * 8, budget=85),), sharing_enabled=True, now=1000)
    assert len(small.decision.replica_counts) == 2
    assert len(larger.decision.replica_counts) == 8
    assert worker.current_decision == small.decision


def test_model_bound_is_global_across_workers():
    workers = planners(["one", "two"])
    candidates = {
        "one": tuple(candidate(digest=f"model-{index}", layers=(10,)) for index in range(32)),
        "two": (candidate(digest="model-32", layers=(10,)),),
    }
    with pytest.raises(ValueError, match="32 distinct manifests"):
        propose_joint_placements(workers, candidates, sharing_enabled=True)


def test_maximum_card_and_block_bounds_have_useful_fair_growth():
    calls = []
    workers = planners([f"gpu-{index:02}" for index in range(16)])
    candidates = {worker: (candidate(layers=(1,) * 512, budget=517, calls=calls),) for worker in workers}
    result = propose_joint_placements(workers, candidates, sharing_enabled=True, now=0)
    assert lengths(result) == {worker: 32 for worker in workers}
    assert len(calls) <= 20 * 16 * 512
    verify(result, candidates)
