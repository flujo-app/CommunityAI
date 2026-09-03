from drift.node.contribution_planner import (
    AutomaticContributionPlanner,
    PlacementArtifactPlan,
    PlacementCandidate,
    PlacementRegistry,
)


def _candidate(
    name,
    *,
    digest,
    counts,
    priority=0,
    preferred=False,
    age=0.0,
    policy_reason=None,
    route_observation=None,
    remote_route_observation=None,
    artifact_plans=(),
    max_artifact_bytes=None,
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
        route_observation=route_observation,
        remote_route_observation=remote_route_observation,
        policy_reason=policy_reason,
        artifact_plans=artifact_plans,
        max_artifact_bytes=max_artifact_bytes,
    )


def _observation(digest, *, attempts=64, successes=64, throughput=64_000, reliability=1000, age=0):
    return {
        "schema_version": 1,
        "manifest_digest": digest,
        "window_seconds": 300,
        "attempts_bucket": attempts,
        "successes_bucket": successes,
        "useful_tokens_per_second_milli": throughput,
        "reliability_milli": reliability,
        "age_seconds_bucket": age,
    }


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


def test_planner_skips_over_budget_window_and_uses_exact_deduplicated_bytes():
    plans = (
        PlacementArtifactPlan(0, 2, 30, "a" * 64),
        PlacementArtifactPlan(1, 3, 50, "b" * 64),
        PlacementArtifactPlan(2, 4, 40, "c" * 64),
    )
    planner = AutomaticContributionPlanner(num_blocks=2, jitter_seed="node-a")
    candidate = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(2, 0, 0, 2),
        artifact_plans=plans,
        max_artifact_bytes=45,
    )

    decision = planner.plan((candidate,), sharing_enabled=True, now=10).decision

    assert decision.block_indices != "1:3"
    selected = next(plan for plan in plans if f"{plan.start_block}:{plan.end_block}" == decision.block_indices)
    assert decision.artifact_bytes == selected.artifact_bytes
    assert decision.artifact_set_digest == selected.artifact_set_digest

    rejected = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(2, 0, 0, 2),
        artifact_plans=plans,
        max_artifact_bytes=29,
    )
    result = AutomaticContributionPlanner(num_blocks=2, jitter_seed="node-a").plan(
        (rejected,), sharing_enabled=True, now=10
    )
    assert result.decision is None
    assert "every 2-block artifact set exceeds" in result.reason


def test_hysteresis_cannot_retain_a_now_over_budget_range():
    plans = (
        PlacementArtifactPlan(0, 1, 30, "a" * 64),
        PlacementArtifactPlan(1, 2, 10, "b" * 64),
        PlacementArtifactPlan(2, 3, 10, "c" * 64),
    )
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=1_000,
        cooldown_seconds=1_000,
    )
    initial = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(0, 2, 2),
        artifact_plans=plans,
        max_artifact_bytes=100,
    )
    assert planner.plan((initial,), sharing_enabled=True, now=0).decision.block_indices == "0:1"

    reduced = _candidate(
        "qwen",
        digest="sha256:" + "1" * 64,
        counts=(0, 2, 2),
        artifact_plans=plans,
        max_artifact_bytes=20,
    )
    decision = planner.plan((reduced,), sharing_enabled=True, now=1).decision

    assert decision.block_indices != "0:1"
    assert decision.artifact_bytes == 10


def test_completed_route_utility_breaks_only_comparable_coverage_ties():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-1",
        minimum_residency_seconds=0,
        cooldown_seconds=0,
        switch_margin=0,
    )
    first_digest = "sha256:" + "1" * 64
    second_digest = "sha256:" + "2" * 64
    unobserved = _candidate("qwen", digest=first_digest, counts=(2,))
    useful = _candidate(
        "gemma",
        digest=second_digest,
        counts=(2,),
        route_observation=_observation(second_digest),
    )

    baseline = planner.propose(
        (unobserved, _candidate("gemma", digest=second_digest, counts=(2,))),
        sharing_enabled=True,
        now=0,
    ).decision
    selected = planner.plan((unobserved, useful), sharing_enabled=True, now=0).decision

    assert baseline.model_id == "qwen"
    assert selected.model_id == "gemma"
    assert "local demand bucket 64" in selected.reason
    assert "reliability 1000/1000" in selected.reason

    scarce = _candidate("scarce", digest="sha256:" + "3" * 64, counts=(1,))
    selected = (
        AutomaticContributionPlanner(num_blocks=1, jitter_seed="node-a")
        .plan((useful, scarce), sharing_enabled=True, now=0)
        .decision
    )
    assert selected.model_id == "scarce"


def test_invalid_or_stale_route_utility_is_neutral():
    first_digest = "sha256:" + "1" * 64
    second_digest = "sha256:" + "2" * 64
    baseline = (
        _candidate("qwen", digest=first_digest, counts=(2,)),
        _candidate("gemma", digest=second_digest, counts=(2,)),
    )
    expected = (
        AutomaticContributionPlanner(num_blocks=1, jitter_seed="node-a")
        .plan(baseline, sharing_enabled=True, now=0)
        .decision.model_id
    )
    malformed = _observation(second_digest, age=105)
    malformed["private_path"] = "forbidden"
    candidates = (
        baseline[0],
        _candidate("gemma", digest=second_digest, counts=(2,), route_observation=malformed),
    )

    selected = (
        AutomaticContributionPlanner(num_blocks=1, jitter_seed="node-a")
        .plan(candidates, sharing_enabled=True, now=0)
        .decision
    )

    assert selected.model_id == expected
    assert "local demand" not in selected.reason


def test_local_route_signal_cannot_independently_cross_switch_margin():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=0,
        cooldown_seconds=0,
        switch_margin=10,
    )
    first_digest = "sha256:" + "1" * 64
    second_digest = "sha256:" + "2" * 64
    initial = (
        _candidate("qwen", digest=first_digest, counts=(2,), preferred=True),
        _candidate("gemma", digest=second_digest, counts=(2,)),
    )
    assert planner.plan(initial, sharing_enabled=True, now=0).decision.model_id == "qwen"
    changed = (
        _candidate("qwen", digest=first_digest, counts=(2,)),
        _candidate(
            "gemma",
            digest=second_digest,
            counts=(2,),
            route_observation=_observation(second_digest),
        ),
    )

    assert planner.plan(changed, sharing_enabled=True, now=1).decision.model_id == "qwen"


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


def test_signed_remote_route_hint_is_bounded_below_the_switch_margin():
    planner = AutomaticContributionPlanner(
        num_blocks=1,
        jitter_seed="node-a",
        minimum_residency_seconds=0,
        cooldown_seconds=0,
        switch_margin=10,
    )
    first_digest = "sha256:" + "1" * 64
    second_digest = "sha256:" + "2" * 64
    assert (
        planner.plan(
            (
                _candidate("qwen", digest=first_digest, counts=(2,), preferred=True),
                _candidate("gemma", digest=second_digest, counts=(2,)),
            ),
            sharing_enabled=True,
            now=0,
        ).decision.model_id
        == "qwen"
    )

    plan = planner.plan(
        (
            _candidate("qwen", digest=first_digest, counts=(2,)),
            _candidate(
                "gemma",
                digest=second_digest,
                counts=(2,),
                remote_route_observation=_observation(second_digest),
            ),
        ),
        sharing_enabled=True,
        now=1,
    )
    assert plan.decision.model_id == "qwen"

    candidate = _candidate(
        "gemma",
        digest=second_digest,
        counts=(2,),
        remote_route_observation=_observation(second_digest),
    )
    signal, observation = planner._route_signal(candidate.remote_route_observation, candidate, cap=2.0)
    assert signal == 2.0
    assert observation.manifest_digest == second_digest
