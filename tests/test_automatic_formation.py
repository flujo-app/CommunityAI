"""Regression coverage for a network with just enough multi-block capacity."""

from drift.node.contribution_planner import AutomaticContributionPlanner, PlacementCandidate


def candidate(counts):
    return PlacementCandidate(
        model_id="qwen",
        manifest_digest="sha256:" + "a" * 64,
        priority=0,
        preferred=False,
        artifact_bytes=1,
        total_blocks=len(counts),
        health={
            "status": "complete" if all(counts) else "incomplete",
            "last_updated_age": 0,
            "replica_counts": counts.copy(),
        },
    )


def test_four_sixteen_block_contributors_fill_all_sixty_four_blocks():
    for cohort in range(32):
        counts = [0] * 64
        allocations = []
        for index in range(4):
            planner = AutomaticContributionPlanner(num_blocks=16, jitter_seed=f"{cohort}-{index}")
            decision = planner.plan((candidate(counts),), sharing_enabled=True, now=0).decision
            allocations.append(decision.block_indices)
            start, end = map(int, decision.block_indices.split(":"))
            for block in range(start, end):
                counts[block] += 1
        assert counts == [1] * 64, allocations


def test_complete_route_does_not_move_a_sole_provider_after_residency():
    planner = AutomaticContributionPlanner(num_blocks=16, jitter_seed="node")
    counts = [1] * 64
    counts[0:16] = [0] * 16
    original = planner.plan((candidate(counts),), sharing_enabled=True, now=0).decision
    counts[:] = [1] * 64
    later = planner.plan((candidate(counts),), sharing_enabled=True, now=1800).decision
    assert later.block_indices == original.block_indices


def test_slow_growth_does_not_move_existing_unique_blocks_after_residency():
    planners = []
    counts = [0] * 64
    for index in range(4):
        now = index * 1800
        for planner, original in planners:
            later = planner.plan((candidate(counts),), sharing_enabled=True, now=now).decision
            assert later.block_indices == original.block_indices
        planner = AutomaticContributionPlanner(num_blocks=16, jitter_seed=f"slow-{index}")
        original = planner.plan((candidate(counts),), sharing_enabled=True, now=now).decision
        start, end = map(int, original.block_indices.split(":"))
        for block in range(start, end):
            counts[block] += 1
        planners.append((planner, original))
    assert counts == [1] * 64


def test_partial_overlap_may_move_when_it_increases_total_coverage():
    planner = AutomaticContributionPlanner(num_blocks=16, jitter_seed="overlap")
    counts = [0] * 16 + [2] * 48
    original = planner.plan((candidate(counts),), sharing_enabled=True, now=0).decision
    assert original.block_indices == "0:16"
    counts = [2] * 8 + [1] * 8 + [0] * 48
    later = planner.plan((candidate(counts),), sharing_enabled=True, now=1800).decision
    assert later.block_indices != original.block_indices
