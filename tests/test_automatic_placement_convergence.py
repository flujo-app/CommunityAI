from collections import Counter, defaultdict

import pytest

from drift.node.contribution_planner import (
    MAX_AUTOMATIC_PLACEMENT_BLOCKS,
    MAX_AUTOMATIC_PLACEMENT_CANDIDATES,
    AutomaticContributionPlanner,
    PlacementCandidate,
)

PRIMARY_DIGEST = "sha256:" + "1" * 64
STANDBY_DIGEST = "sha256:" + "2" * 64
MODELS = (
    ("primary", PRIMARY_DIGEST, 0),
    ("standby", STANDBY_DIGEST, 1),
)


def _observation(digest):
    return {
        "schema_version": 1,
        "manifest_digest": digest,
        "window_seconds": 300,
        "attempts_bucket": 64,
        "successes_bucket": 64,
        "useful_tokens_per_second_milli": 64_000,
        "reliability_milli": 1000,
        "age_seconds_bucket": 0,
    }


def _candidate(name, digest, priority, counts, *, local=False, remote=False):
    return PlacementCandidate(
        model_id=name,
        manifest_digest=digest,
        priority=priority,
        preferred=False,
        artifact_bytes=1,
        total_blocks=len(counts),
        health={
            "status": "complete" if all(counts) else "incomplete",
            "last_updated_age": 0,
            "replica_counts": list(counts),
        },
        route_observation=_observation(digest) if local else None,
        remote_route_observation=_observation(digest) if remote else None,
    )


def _candidates(counts_by_model, *, demand_for=None):
    return tuple(
        _candidate(
            name,
            digest,
            priority,
            counts_by_model[name],
            local=name == demand_for,
            remote=name == demand_for,
        )
        for name, digest, priority in MODELS
    )


def _cohort_decisions(size=512):
    decisions = []
    counts = {"primary": (2,) * 8, "standby": (2,) * 8}
    for index in range(size):
        planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed=f"node-{index:03d}")
        decision = planner.propose(_candidates(counts), sharing_enabled=True, now=0).decision
        decisions.append((decision.model_id, decision.block_indices))
    return decisions


def test_equal_snapshot_cohort_is_deterministic_and_does_not_herd():
    first = _cohort_decisions()
    assert first == _cohort_decisions()

    models = Counter(model for model, _ in first)
    assert set(models) == {"primary", "standby"}
    assert max(models.values()) < len(first) * 0.85

    ranges_by_model = defaultdict(Counter)
    for model, block_indices in first:
        ranges_by_model[model][block_indices] += 1
    for model, ranges in ranges_by_model.items():
        assert len(ranges) == 8
        mean = models[model] / 8
        assert max(ranges.values()) < mean * 2


def test_bursty_local_and_remote_demand_cannot_migrate_incumbents():
    counts = {"primary": (2,), "standby": (2,)}
    planners = []
    initial_models = []
    for index in range(128):
        planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed=f"burst-{index:03d}")
        initial = planner.plan(_candidates(counts), sharing_enabled=True, now=0).decision
        planners.append(planner)
        initial_models.append(initial.model_id)

    for now in (60, 901):
        selected = []
        for planner, current_model in zip(planners, initial_models):
            other_model = "standby" if current_model == "primary" else "primary"
            decision = planner.propose(
                _candidates(counts, demand_for=other_model),
                sharing_enabled=True,
                now=now,
            ).decision
            selected.append(decision.model_id)
        assert selected == initial_models


@pytest.mark.parametrize("demand_for", ("primary", "standby"))
def test_fresh_arrivals_remain_distributed_under_maximum_demand(demand_for):
    counts = {"primary": (2,), "standby": (2,)}
    selected = Counter()
    for index in range(4096):
        planner = AutomaticContributionPlanner(
            num_blocks=1,
            jitter_seed=f"fresh-{demand_for}-{index:04d}",
        )
        decision = planner.propose(
            _candidates(counts, demand_for=demand_for),
            sharing_enabled=True,
            now=0,
        ).decision
        selected[decision.model_id] += 1

    assert set(selected) == {"primary", "standby"}
    assert max(selected.values()) < 4096 * 0.85


def test_real_coverage_loss_can_migrate_once_but_cannot_immediately_reverse():
    counts = {"primary": (2,), "standby": (2,)}
    for index in range(64):
        planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed=f"loss-{index:03d}")
        initial = planner.plan(_candidates(counts), sharing_enabled=True, now=0).decision.model_id
        other = "standby" if initial == "primary" else "primary"

        deficit = {initial: (2,), other: (1,)}
        migrated = planner.plan(_candidates(deficit), sharing_enabled=True, now=901).decision
        assert migrated.model_id == other

        reversed_deficit = {initial: (1,), other: (2,)}
        held = planner.plan(_candidates(reversed_deficit), sharing_enabled=True, now=1000).decision
        assert held.model_id == other

        restored = planner.plan(_candidates(reversed_deficit), sharing_enabled=True, now=1802).decision
        assert restored.model_id == initial


def test_rolling_arrivals_converge_and_restore_an_abrupt_block_loss():
    counts = {"primary": [0] * 8, "standby": [0] * 8}
    for wave in range(4):
        for index in range(128):
            planner = AutomaticContributionPlanner(
                num_blocks=1,
                jitter_seed=f"wave-{wave}-node-{index:03d}",
            )
            decision = planner.propose(_candidates(counts), sharing_enabled=True, now=0).decision
            start = int(decision.block_indices.split(":", 1)[0])
            counts[decision.model_id][start] += 1

    assert sum(sum(model_counts) for model_counts in counts.values()) == 512
    assert all(value > 0 for model_counts in counts.values() for value in model_counts)
    assert all(max(model_counts) - min(model_counts) <= 1 for model_counts in counts.values())
    assert max(sum(model_counts) for model_counts in counts.values()) < 512 * 0.85

    counts["standby"][3] = 0
    for index in range(2):
        planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed=f"repair-{index}")
        decision = planner.propose(_candidates(counts), sharing_enabled=True, now=0).decision
        assert decision.model_id == "standby"
        assert decision.block_indices == "3:4"
        counts["standby"][3] += 1
    assert counts["standby"][3] == 2


def test_bounded_window_scan_preserves_the_coverage_optimum():
    digest = "sha256:" + "e" * 64
    for total_blocks in range(1, 17):
        for num_blocks in range(1, total_blocks + 1):
            for fixture in range(4):
                counts = tuple((index * 17 + total_blocks * 7 + fixture * 11) % 6 for index in range(total_blocks))
                planner = AutomaticContributionPlanner(
                    num_blocks=num_blocks,
                    jitter_seed=f"window-{total_blocks}-{num_blocks}-{fixture}",
                )
                decision = planner.propose(
                    (_candidate("model", digest, 0, counts),),
                    sharing_enabled=True,
                    now=0,
                ).decision
                start = int(decision.block_indices.split(":", 1)[0])
                selected = counts[start : start + num_blocks]
                optimum = min(
                    (max(counts[offset : offset + num_blocks]), sum(counts[offset : offset + num_blocks]))
                    for offset in range(total_blocks - num_blocks + 1)
                )
                assert (max(selected), sum(selected)) == optimum


def test_planner_load_boundaries_fail_closed_before_unbounded_work():
    digest = "sha256:" + "f" * 64
    accepted = _candidate(
        "maximum-blocks",
        digest,
        0,
        (2,) * MAX_AUTOMATIC_PLACEMENT_BLOCKS,
    )
    assert accepted.total_blocks == MAX_AUTOMATIC_PLACEMENT_BLOCKS

    with pytest.raises(ValueError, match="block limit"):
        AutomaticContributionPlanner(
            num_blocks=MAX_AUTOMATIC_PLACEMENT_BLOCKS + 1,
            jitter_seed="too-many-blocks",
        )
    with pytest.raises(ValueError, match="block limit"):
        _candidate(
            "too-many-blocks",
            digest,
            0,
            (2,) * (MAX_AUTOMATIC_PLACEMENT_BLOCKS + 1),
        )

    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="candidate-limit")
    candidates = tuple(
        _candidate(f"model-{index}", f"sha256:{index:064x}", index, (2,))
        for index in range(MAX_AUTOMATIC_PLACEMENT_CANDIDATES)
    )
    assert planner.propose(candidates, sharing_enabled=True, now=0).decision is not None

    excess = candidates + (
        _candidate(
            "excess",
            f"sha256:{MAX_AUTOMATIC_PLACEMENT_CANDIDATES:064x}",
            MAX_AUTOMATIC_PLACEMENT_CANDIDATES,
            (2,),
        ),
    )
    rejected = planner.propose(excess, sharing_enabled=True, now=0)
    assert rejected.decision is None
    assert rejected.evaluated_models == MAX_AUTOMATIC_PLACEMENT_CANDIDATES + 1
    assert f"candidate limit is {MAX_AUTOMATIC_PLACEMENT_CANDIDATES}" in rejected.reason
