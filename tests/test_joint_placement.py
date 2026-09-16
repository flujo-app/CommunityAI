from collections.abc import Mapping, Sequence
from dataclasses import replace

import pytest

from drift.node.contribution_planner import (
    MAX_JOINT_PLACEMENT_WORKERS,
    AutomaticContributionPlanner,
    PlacementCandidate,
    propose_joint_placements,
)

DIGEST = "sha256:" + "1" * 64
OTHER_DIGEST = "sha256:" + "2" * 64


class OversizedMapping(Mapping):
    def __init__(self, length):
        self.length = length

    def __len__(self):
        return self.length

    def __iter__(self):
        raise AssertionError("oversized mapping must be rejected before iteration")

    def __getitem__(self, key):
        raise AssertionError("oversized mapping must be rejected before access")


class OversizedSequence(Sequence):
    def __init__(self, length):
        self.length = length

    def __len__(self):
        return self.length

    def __getitem__(self, index):
        raise AssertionError("oversized sequence must be rejected before materialization")


def candidate(*, digest=DIGEST, counts=(0,) * 8, name="model"):
    return PlacementCandidate(
        model_id=name,
        manifest_digest=digest,
        priority=0,
        preferred=False,
        artifact_bytes=100,
        total_blocks=len(counts),
        health={
            "status": "complete" if all(counts) else "incomplete",
            "last_updated_age": 0,
            "replica_counts": list(counts),
        },
    )


def planner(worker_id, *, blocks=1, residency=0):
    return AutomaticContributionPlanner(
        num_blocks=blocks,
        jitter_seed=worker_id,
        minimum_residency_seconds=residency,
        cooldown_seconds=residency,
        switch_margin=0,
    )


def spans(plans):
    return {
        worker_id: None if plan.decision is None else (plan.decision.manifest_digest, plan.decision.block_indices)
        for worker_id, plan in plans.items()
    }


def acknowledged(plan):
    assert plan.decision is not None
    return replace(plan, intent_published=True, remote_acknowledged=True)


def assert_nonoverlapping(plans):
    occupied = {}
    for plan in plans.values():
        if plan.decision is None:
            continue
        start, end = map(int, plan.decision.block_indices.split(":"))
        ranges = occupied.setdefault(plan.decision.manifest_digest, [])
        assert all(end <= other_start or other_end <= start for other_start, other_end in ranges)
        ranges.append((start, end))


def test_eight_cards_receive_distinct_spans_without_preaccept_commit():
    planners = {f"worker-{index}": planner(f"worker-{index}") for index in range(8)}
    candidates = {worker_id: (candidate(),) for worker_id in planners}

    result = propose_joint_placements(planners, candidates, sharing_enabled=True, now=10)

    assert len(result) == 8
    assert all(plan.decision is not None for plan in result.values())
    assert len({plan.decision.block_indices for plan in result.values()}) == 8
    assert_nonoverlapping(result)
    assert all(item._current is None for item in planners.values())


def test_joint_result_is_independent_of_mapping_insertion_order():
    worker_ids = [f"worker-{index}" for index in range(8)]

    def run(order):
        planners = {worker_id: planner(worker_id) for worker_id in order}
        candidates = {worker_id: (candidate(),) for worker_id in reversed(order)}
        return spans(propose_joint_placements(planners, candidates, sharing_enabled=True, now=10))

    assert run(worker_ids) == run(list(reversed(worker_ids)))


def test_exhausted_model_fails_closed_and_other_manifest_can_reuse_range():
    planners = {f"worker-{index}": planner(f"worker-{index}") for index in range(3)}
    same_model = {worker_id: (candidate(counts=(0, 0)),) for worker_id in planners}
    exhausted = propose_joint_placements(planners, same_model, sharing_enabled=True, now=10)

    assert sum(plan.decision is not None for plan in exhausted.values()) == 2
    assert any("reserved by another local worker" in plan.reason for plan in exhausted.values())
    assert_nonoverlapping(exhausted)

    different_models = {
        "one": (candidate(digest=DIGEST, counts=(0,), name="one"),),
        "two": (candidate(digest=OTHER_DIGEST, counts=(0,), name="two"),),
    }
    reused = propose_joint_placements(
        {worker_id: planner(worker_id) for worker_id in different_models},
        different_models,
        sharing_enabled=True,
        now=10,
    )
    assert {plan.decision.block_indices for plan in reused.values()} == {"0:1"}
    assert {plan.decision.manifest_digest for plan in reused.values()} == {DIGEST, OTHER_DIGEST}


def test_unequal_worker_sizes_pack_without_overlap():
    sizes = {"large": 3, "medium-a": 2, "medium-b": 2, "small": 1}
    planners = {worker_id: planner(worker_id, blocks=size) for worker_id, size in sizes.items()}
    candidates = {worker_id: (candidate(),) for worker_id in planners}

    result = propose_joint_placements(planners, candidates, sharing_enabled=True, now=10)

    assert all(plan.decision is not None for plan in result.values())
    assert_nonoverlapping(result)
    assert (
        sum(
            int(plan.decision.block_indices.split(":")[1]) - int(plan.decision.block_indices.split(":")[0])
            for plan in result.values()
        )
        == 8
    )


def test_acknowledged_retained_plans_remain_stable_and_uncommitted():
    planners = {}
    candidates = {}
    retained = {}
    for index in range(4):
        worker_id = f"worker-{index}"
        counts = tuple(0 if block == index else 3 for block in range(4))
        item = planner(worker_id, residency=1000)
        current = item.plan((candidate(counts=counts),), sharing_enabled=True, now=0)
        planners[worker_id] = item
        candidates[worker_id] = (candidate(counts=counts),)
        retained[worker_id] = acknowledged(current)

    before = {worker_id: item._current for worker_id, item in planners.items()}
    result = propose_joint_placements(
        planners,
        candidates,
        sharing_enabled=True,
        retained_plans=retained,
        now=1,
    )

    assert spans(result) == spans(retained)
    assert {worker_id: item._current for worker_id, item in planners.items()} == before
    assert_nonoverlapping(result)


def test_hysteresis_cannot_restore_a_peer_reserved_current_span():
    item = planner("worker", residency=1000)
    current = item.plan((candidate(counts=(0, 3, 3)),), sharing_enabled=True, now=0)
    start, end = map(int, current.decision.block_indices.split(":"))

    proposal = item.propose(
        (candidate(counts=(0, 3, 3)),),
        sharing_enabled=True,
        now=1,
        excluded_spans={DIGEST: ((start, end),)},
    )

    assert proposal.decision is not None
    assert proposal.decision.block_indices != current.decision.block_indices
    assert item._current == current.decision


@pytest.mark.parametrize("malformation", ["unacknowledged", "range", "metadata"])
def test_malformed_retained_plan_fails_closed(malformation):
    item = planner("worker")
    proposed = item.propose((candidate(),), sharing_enabled=True, now=0)
    retained = acknowledged(proposed)
    if malformation == "unacknowledged":
        retained = proposed
    elif malformation == "range":
        retained = replace(retained, decision=replace(retained.decision, block_indices="01:2"))
    else:
        retained = replace(retained, decision=replace(retained.decision, replica_counts=[0]))

    # Reach the structural guard rather than failing at accepted-decision identity.
    item.commit(retained, now=0)
    expected = {
        "unacknowledged": "acknowledged non-empty plans",
        "range": "invalid block range",
        "metadata": "invalid bounded metadata",
    }[malformation]
    with pytest.raises(ValueError, match=expected):
        propose_joint_placements(
            {"worker": item},
            {"worker": (candidate(),)},
            sharing_enabled=True,
            retained_plans={"worker": retained},
            now=1,
        )


def test_overlapping_retained_plans_fail_closed():
    planners = {worker_id: planner(worker_id) for worker_id in ("one", "two")}
    candidates = {worker_id: (candidate(counts=(0, 3)),) for worker_id in planners}
    retained = {}
    for worker_id, item in planners.items():
        plan = item.propose(candidates[worker_id], sharing_enabled=True, now=0)
        item.commit(plan, now=0)
        retained[worker_id] = acknowledged(plan)
    assert len({plan.decision.block_indices for plan in retained.values()}) == 1

    with pytest.raises(ValueError, match="overlap"):
        propose_joint_placements(
            planners,
            candidates,
            sharing_enabled=True,
            retained_plans=retained,
            now=1,
        )


def test_casefold_collisions_unknown_retention_and_worker_bound_fail_closed():
    one = planner("one")
    values = (candidate(),)
    with pytest.raises(ValueError, match="duplicate worker IDs"):
        propose_joint_placements(
            {"GPU": one, "gpu": planner("two")},
            {"GPU": values, "gpu": values},
            sharing_enabled=True,
        )
    with pytest.raises(ValueError, match="unknown worker"):
        propose_joint_placements(
            {"one": one},
            {"one": values},
            sharing_enabled=True,
            retained_plans={"two": acknowledged(one.propose(values, sharing_enabled=True, now=0))},
        )
    too_many = {f"worker-{index}": planner(f"worker-{index}") for index in range(MAX_JOINT_PLACEMENT_WORKERS + 1)}
    with pytest.raises(ValueError, match="at most"):
        propose_joint_placements(
            too_many,
            {worker_id: values for worker_id in too_many},
            sharing_enabled=True,
        )


@pytest.mark.parametrize("worker_id", ["../worker", "worker/one", "x" * 65])
def test_noncanonical_worker_ids_fail_closed(worker_id):
    with pytest.raises(ValueError, match="canonical"):
        propose_joint_placements(
            {worker_id: planner("worker")},
            {worker_id: (candidate(),)},
            sharing_enabled=True,
        )


def test_oversized_inputs_are_rejected_before_iteration_or_materialization():
    with pytest.raises(ValueError, match="at most"):
        propose_joint_placements(
            OversizedMapping(MAX_JOINT_PLACEMENT_WORKERS + 1),
            {},
            sharing_enabled=True,
        )
    with pytest.raises(ValueError, match="candidates per worker"):
        propose_joint_placements(
            {"worker": planner("worker")},
            {"worker": OversizedSequence(33)},
            sharing_enabled=True,
        )
    with pytest.raises(ValueError, match="exclusions support at most"):
        planner("worker").propose(
            (candidate(),),
            sharing_enabled=True,
            excluded_spans=OversizedMapping(33),
        )
    with pytest.raises(ValueError, match="ranges per manifest"):
        planner("worker").propose(
            (candidate(),),
            sharing_enabled=True,
            excluded_spans={DIGEST: OversizedSequence(MAX_JOINT_PLACEMENT_WORKERS + 1)},
        )


@pytest.mark.parametrize(
    "excluded",
    [
        [],
        {DIGEST: "0:1"},
        {DIGEST: ((True, 1),)},
        {DIGEST: ((-1, 1),)},
        {DIGEST: ((1, 1),)},
        {DIGEST: ((0, 513),)},
        {DIGEST: ((0, 1.5),)},
    ],
)
def test_malformed_exclusions_fail_closed(excluded):
    with pytest.raises(ValueError, match="exclusion"):
        planner("worker").propose((candidate(),), sharing_enabled=True, excluded_spans=excluded)
