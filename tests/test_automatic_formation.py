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
