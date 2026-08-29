from drift.node.contribution_planner import AutomaticContributionPlanner, PlacementCandidate, PlacementRegistry


def _candidate(
    name,
    *,
    digest,
    counts,
    priority=0,
    preferred=False,
    age=0.0,
    policy_reason=None,
):
    return PlacementCandidate(
        model_id=name,
        manifest_digest=digest,
        priority=priority,
        preferred=preferred,
        artifact_bytes=100,
        total_blocks=len(counts),
        health={
            "status": "complete" if all(counts) else "incomplete",
            "last_updated_age": age,
            "replica_counts": list(counts),
        },
        policy_reason=policy_reason,
    )


def test_planner_selects_preferred_model_and_least_covered_contiguous_range():
    planner = AutomaticContributionPlanner(
        num_blocks=2,
        jitter_seed="node-a",
        minimum_residency_seconds=0,
        cooldown_seconds=0,
        switch_margin=0,
    )
    primary = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(2, 0, 0, 2),
        priority=0,
    )
    preferred = _candidate(
        "gemma",
        digest="sha256:" + "2" * 64,
        counts=(2, 0, 1, 2),
        priority=1,
        preferred=True,
    )

    plan = planner.plan((primary, preferred), sharing_enabled=True, now=10)

    assert plan.decision.model_id == "gemma"
    assert plan.decision.block_indices == "1:3"
    assert plan.decision.replica_counts == (0, 1)
    assert "fresh verified coverage" in plan.reason


def test_planner_fails_closed_without_fresh_coverage():
    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="node-a")
    unknown = PlacementCandidate(
        model_id="qwen",
        manifest_digest="sha256:" + "1" * 64,
        priority=0,
        preferred=False,
        artifact_bytes=100,
        total_blocks=2,
        health={
            "status": "unknown",
            "last_updated_age": None,
            "replica_counts": None,
        },
    )

    plan = planner.plan((unknown,), sharing_enabled=True, now=10)

    assert plan.decision is None
    assert "coverage observation is unavailable" in plan.reason


def test_planner_filters_policy_rejection_before_selection():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=0,
        cooldown_seconds=0,
        switch_margin=0,
    )
    denied = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(0, 0),
        policy_reason="manifested artifacts exceed the disk budget",
    )
    admitted = _candidate(
        "gemma",
        digest="sha256:" + "2" * 64,
        counts=(1, 0),
        priority=1,
    )

    plan = planner.plan((denied, admitted), sharing_enabled=True, now=10)

    assert plan.decision.model_id == "gemma"
    assert plan.decision.block_indices == "1:2"


def test_planner_holds_assignment_until_residency_and_margin_are_met():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=10,
        cooldown_seconds=0,
        switch_margin=10,
    )
    qwen_initial = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(0,),
        preferred=True,
    )
    gemma_initial = _candidate(
        "gemma",
        digest="sha256:" + "2" * 64,
        counts=(1,),
        priority=1,
    )
    assert planner.plan((qwen_initial, gemma_initial), sharing_enabled=True, now=0).decision.model_id == "qwen"

    qwen_weaker = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(1,),
        preferred=True,
    )
    gemma_needed = _candidate(
        "gemma",
        digest="sha256:" + "2" * 64,
        counts=(0,),
        priority=1,
    )
    assert planner.plan((qwen_weaker, gemma_needed), sharing_enabled=True, now=5).decision.model_id == "qwen"
    assert planner.plan((qwen_weaker, gemma_needed), sharing_enabled=True, now=11).decision.model_id == "gemma"


def test_planner_proposal_does_not_advance_hysteresis_before_commit():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=10,
        cooldown_seconds=0,
        switch_margin=0,
    )
    qwen_initial = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(0,),
        preferred=True,
    )
    gemma_initial = _candidate(
        "gemma",
        digest="sha256:" + "2" * 64,
        counts=(1,),
        priority=1,
    )
    initial = planner.propose((qwen_initial, gemma_initial), sharing_enabled=True, now=0)
    assert initial.decision.model_id == "qwen"

    qwen_weaker = _candidate("qwen", digest="sha256:" + "1" * 64, counts=(2,))
    gemma_needed = _candidate("gemma", digest="sha256:" + "2" * 64, counts=(0,), priority=1)
    assert planner.propose((qwen_weaker, gemma_needed), sharing_enabled=True, now=5).decision.model_id == "gemma"

    planner.commit(initial, now=0)
    assert planner.propose((qwen_weaker, gemma_needed), sharing_enabled=True, now=5).decision.model_id == "qwen"


def test_disabled_sharing_preserves_fail_closed_status_and_registry_is_copying():
    planner = AutomaticContributionPlanner(num_blocks=1, jitter_seed="node-a")
    plan = planner.plan((), sharing_enabled=False, now=0)
    registry = PlacementRegistry()
    source = {"automatic": plan}
    registry.replace(source)
    source.clear()

    assert registry.snapshot()["automatic"].decision is None
    assert registry.snapshot()["automatic"].reason == "sharing is disabled by contribution policy"
