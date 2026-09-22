"""Joint placement integration with real planners, supervision and local children.

The schema still rejects multiple automatic workers. This internal fixture alone
combines individually validated CPU workers with dataclasses.replace to exercise
the service seam before aggregate admission is qualified. Its children only
sleep; artifact claims, coverage and remote acknowledgements are controlled
doubles. No model, network, GPU qualification or accepted multi-auto config is
claimed, and no placement-service or supervisor monitor thread is started.
"""

import subprocess
import sys
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.model_manifest import ModelManifest
from drift.node.config import NodeConfig
from drift.node.contribution_planner import (
    AutomaticContributionPlanner,
    PlacementArtifactPlan,
    PlacementCandidate,
    PlacementRegistry,
)
from drift.node.discovery import PeerCache
from drift.node.worker_supervisor import WorkerSupervisor


def _assert_disjoint(plans):
    occupied = {}
    for plan in plans.values():
        if plan.decision is None:
            continue
        decision = plan.decision
        start, end = map(int, decision.block_indices.split(":"))
        spans = occupied.setdefault(decision.manifest_digest, [])
        assert all(end <= left or right <= start for left, right in spans)
        spans.append((start, end))


def _planner_state(runtime):
    return {worker_id: planner.current_decision for worker_id, planner in runtime.planners.items()}


def _set_span(runtime, worker_id, start, *, gap=False):
    previous = runtime.candidates[worker_id]
    health = (
        {"status": "unknown", "last_known_status": "incomplete", "last_updated_age": 10}
        if gap
        else {"status": "incomplete", "last_updated_age": 0, "replica_counts": [0] * 8}
    )
    runtime.candidates[worker_id] = replace(
        previous,
        health=health,
        # Exact planning requires metadata for every possible span. Keep the
        # complete map and admit only the chosen span under the artifact budget.
        artifact_plans=tuple(
            PlacementArtifactPlan(
                index,
                index + 1,
                4_242 + index if index == start else previous.max_artifact_bytes + 1,
                f"{index + 1:064x}",
            )
            for index in range(8)
        ),
    )


@pytest.fixture
def joint_runtime(tmp_path, monkeypatch):
    runtimes = []

    def create(count=2, *, enabled=True, active=False, require_explicit_start=False):
        directory = tmp_path / str(len(runtimes))
        directory.mkdir()
        manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
        manifest_path = directory / "manifest.json"
        manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
        configurations = [
            NodeConfig.from_dict(
                {
                    "schema_version": 1,
                    "models": [{"manifest": str(manifest_path), "initial_peers": ["fixture-peer"]}],
                    "workers": [
                        {
                            "id": f"gpu-{index}",
                            "model": "auto",
                            "identity_path": f"worker-{index}.key",
                            "num_blocks": 1,
                            "enabled": enabled,
                            "device": "cpu",
                        }
                    ],
                    "contribution_policy": {"sharing_enabled": True, "max_disk_space": "1GiB"},
                },
                base_dir=directory,
            )
            for index in range(count)
        ]
        # Deliberate fixture-only bypass: this does not remove NodeConfig's
        # production one-auto guard or establish aggregate resource admission.
        config = replace(configurations[0], workers=tuple(item.workers[0] for item in configurations))

        def forbidden_loader(*args, **kwargs):
            def load():
                raise AssertionError("joint placement fixture must not load a model")

            return load

        monkeypatch.setattr(run_node, "make_text_peer_loader", forbidden_loader)
        manager, _, discovery = run_node._build_model_manager(config, token=None)
        candidate = PlacementCandidate(
            model_id=manifest.name,
            manifest_digest=manifest.digest_id,
            priority=0,
            preferred=False,
            artifact_bytes=100_000,
            total_blocks=8,
            health={"status": "incomplete", "last_updated_age": 0, "replica_counts": [0] * 8},
            artifact_plans=tuple(
                PlacementArtifactPlan(index, index + 1, 4_242 + index, f"{index + 1:064x}") for index in range(8)
            ),
            max_artifact_bytes=1024**3,
        )
        candidates = {worker.worker_id: candidate for worker in config.workers}
        clock = [2_000.0]
        planners = {}

        def new_planner(**kwargs):
            kwargs.update(minimum_residency_seconds=0, cooldown_seconds=0, switch_margin=0, clock=lambda: clock[0])
            planner = AutomaticContributionPlanner(**kwargs)
            planners[kwargs["jitter_seed"]] = planner
            return planner

        monkeypatch.setattr(run_node, "AutomaticContributionPlanner", new_planner)
        monkeypatch.setattr(run_node, "_automatic_placement_seed", lambda worker: worker.worker_id)
        monkeypatch.setattr(run_node, "get_dht_time", lambda: clock[0])
        monkeypatch.setattr(
            run_node,
            "_automatic_placement_candidates",
            lambda config, manager, discovery, worker, **kwargs: (candidates[worker.worker_id],),
        )
        publication = Mock(return_value=True)
        monkeypatch.setattr(discovery, "publish_intent", publication)
        registry = PlacementRegistry()
        guards = {}
        settings = run_node._prepare_worker_supervisor_settings(
            config, manager, automatic_placements={}, automatic_placement_guards=guards
        )
        children = []
        events = []
        before_spawn = [None]

        def popen(command, **kwargs):
            if before_spawn[0] is not None:
                before_spawn[0]()
            process = subprocess.Popen(
                (sys.executable, "-c", "import time; time.sleep(60)", "communityai-joint-placement-fixture"),
                **kwargs,
            )
            children.append(process)
            events.append(("spawn", process.pid))
            return process

        supervisor = WorkerSupervisor(
            settings.launches,
            coordinated_launches=True,
            stop_timeout=0.2,
            poll_period=0.01,
            popen=popen,
        )
        # Activate synchronous spawn paths only. All reconciles and process
        # observations remain test-controlled, with neither monitor running.
        supervisor._started = active
        if require_explicit_start:
            for worker in config.workers:
                supervisor.pause_worker(worker.worker_id)
        runtime = SimpleNamespace(
            config=config,
            manager=manager,
            discovery=discovery,
            registry=registry,
            supervisor=supervisor,
            candidates=candidates,
            planners=planners,
            publication=publication,
            clock=clock,
            children=children,
            events=events,
            before_spawn=before_spawn,
            guards=guards,
        )
        runtimes.append(runtime)
        runtime.service = run_node._build_automatic_placement_service(
            config,
            manager,
            discovery,
            supervisor,
            registry,
            token=None,
            config_path=None,
            peer_cache=PeerCache(directory / "peers.json"),
            require_explicit_start=require_explicit_start,
            automatic_placement_guards=guards,
        )
        return runtime

    yield create
    for runtime in runtimes:
        if hasattr(runtime, "service"):
            runtime.service.close()
        runtime.supervisor.shutdown()
        runtime.manager.shutdown()
        for process in runtime.children:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.parametrize("count", [2, 8])
def test_joint_service_assigns_disjoint_acknowledged_ranges_in_one_runtime_batch(joint_runtime, monkeypatch, count):
    runtime = joint_runtime(count)
    batch = Mock(wraps=runtime.supervisor.replace_launches)
    monkeypatch.setattr(runtime.supervisor, "replace_launches", batch)

    def forbidden(*args, **kwargs):
        raise AssertionError("joint service must not mutate one worker at a time")

    monkeypatch.setattr(runtime.supervisor, "replace_launch", forbidden)
    monkeypatch.setattr(runtime.supervisor, "pause_worker_for_reconfiguration", forbidden)
    runtime.service.reconcile_once()
    plans = runtime.registry.snapshot()
    assert set(plans) == {f"gpu-{index}" for index in range(count)}
    assert all(plan.decision is not None and plan.remote_acknowledged for plan in plans.values())
    _assert_disjoint(plans)
    assert batch.call_count == 1
    assert {launch.worker_id for launch in batch.call_args.args[0]} == set(plans)
    assert _planner_state(runtime) == {worker: plan.decision for worker, plan in plans.items()}
    assert not runtime.children
    assert runtime.service._thread is None and runtime.supervisor._monitor is None


def test_runtime_batch_must_succeed_before_registry_or_planner_commit(joint_runtime, monkeypatch):
    runtime = joint_runtime()
    before_registry, before_planners = runtime.registry.snapshot(), _planner_state(runtime)
    actual_batch = runtime.supervisor.replace_launches

    def rejected_batch(launches, **kwargs):
        assert runtime.registry.snapshot() == before_registry
        assert _planner_state(runtime) == before_planners
        raise RuntimeError("controlled runtime batch rejection")

    monkeypatch.setattr(runtime.supervisor, "replace_launches", rejected_batch)
    with pytest.raises(RuntimeError, match="controlled runtime batch rejection"):
        runtime.service.reconcile_once()
    assert runtime.registry.snapshot() == before_registry
    assert _planner_state(runtime) == before_planners
    monkeypatch.setattr(runtime.supervisor, "replace_launches", actual_batch)
    runtime.service.reconcile_once()
    _assert_disjoint(runtime.registry.snapshot())
    assert all(plan.decision is not None for plan in runtime.registry.snapshot().values())


def test_range_swap_stops_all_old_children_before_new_spawn_and_acceptance(joint_runtime, monkeypatch):
    runtime = joint_runtime(active=True)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", index)
    runtime.service.reconcile_once()
    old_children = tuple(runtime.children)
    assert len(old_children) == 2
    old_registry, old_planners = runtime.registry.snapshot(), _planner_state(runtime)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", 1 - index)
    actual_batch = runtime.supervisor.replace_launches

    def batch(launches, **kwargs):
        assert runtime.registry.snapshot() == old_registry
        assert _planner_state(runtime) == old_planners
        return actual_batch(launches, **kwargs)

    def before_spawn():
        assert all(child.poll() is not None for child in old_children)
        assert runtime.registry.snapshot() == old_registry
        assert _planner_state(runtime) == old_planners
        assert [launch.block_indices for launch in runtime.supervisor.launches] == ["1:2", "0:1"]

    monkeypatch.setattr(runtime.supervisor, "replace_launches", batch)
    runtime.before_spawn[0] = before_spawn
    runtime.service.reconcile_once()
    assert len(runtime.children) == 4
    assert [runtime.registry.snapshot()[f"gpu-{index}"].decision.block_indices for index in range(2)] == [
        "1:2",
        "0:1",
    ]
    _assert_disjoint(runtime.registry.snapshot())


def test_recent_coverage_gap_reserves_retained_span_against_earlier_sibling(joint_runtime):
    runtime = joint_runtime()
    _set_span(runtime, "gpu-0", 0)
    _set_span(runtime, "gpu-1", 1)
    runtime.service.reconcile_once()
    retained = runtime.registry.snapshot()["gpu-1"]
    _set_span(runtime, "gpu-0", 1)
    _set_span(runtime, "gpu-1", 1, gap=True)
    runtime.service.reconcile_once()
    plans = runtime.registry.snapshot()
    assert plans["gpu-1"] == retained
    _assert_disjoint(plans)
    assert plans["gpu-0"].decision is None or plans["gpu-0"].decision.block_indices != "1:2"


def test_failed_intent_refresh_retains_live_acknowledgement_without_overlapping_sibling(joint_runtime):
    runtime = joint_runtime()
    _set_span(runtime, "gpu-0", 0)
    _set_span(runtime, "gpu-1", 1)
    runtime.service.reconcile_once()
    retained = runtime.registry.snapshot()
    runtime.clock[0] = 2_500.0  # Refresh is due, but the acknowledged lease expires at 2,600.
    runtime.publication.reset_mock(return_value=True)
    runtime.publication.return_value = False
    runtime.service.reconcile_once()
    assert runtime.publication.call_count == 2
    plans = runtime.registry.snapshot()
    assert plans == retained
    _assert_disjoint(plans)


def test_failed_cleanup_retries_complete_batch_without_premature_plan_acceptance(joint_runtime, monkeypatch):
    runtime = joint_runtime(active=True)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", index)
    runtime.service.reconcile_once()
    before_registry, before_planners = runtime.registry.snapshot(), _planner_state(runtime)
    old_children = tuple(runtime.children)
    actual_cleanup = runtime.supervisor._terminate_launch_tree
    actual_batch = runtime.supervisor.replace_launches
    batches = []

    def batch(launches, **kwargs):
        batches.append({launch.worker_id for launch in launches})
        return actual_batch(launches, **kwargs)

    def failed_cleanup(process):
        if process is old_children[0]:
            raise OSError("controlled cleanup failure")
        return actual_cleanup(process)

    monkeypatch.setattr(runtime.supervisor, "replace_launches", batch)
    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", failed_cleanup)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", 1 - index)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        runtime.service.reconcile_once()
    assert runtime.registry.snapshot() == before_registry
    assert _planner_state(runtime) == before_planners
    assert runtime.supervisor.launch_transition_status["state"] == "cleanup_failed"
    assert len(runtime.children) == 2
    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", actual_cleanup)
    runtime.service.reconcile_once()
    assert batches == [{"gpu-0", "gpu-1"}, {"gpu-0", "gpu-1"}]
    assert runtime.supervisor.launch_transition_status["state"] == "idle"
    assert all(child.poll() is not None for child in old_children)
    _assert_disjoint(runtime.registry.snapshot())


def test_operator_pause_received_during_joint_handoff_survives_batch_start(joint_runtime, monkeypatch):
    runtime = joint_runtime(active=True)
    actual_batch = runtime.supervisor.replace_launches

    def pause_before_batch(launches, **kwargs):
        runtime.supervisor.pause_worker("gpu-1")
        return actual_batch(launches, **kwargs)

    monkeypatch.setattr(runtime.supervisor, "replace_launches", pause_before_batch)
    runtime.service.reconcile_once()
    first, second = (runtime.supervisor.snapshot(f"gpu-{index}") for index in range(2))
    assert first["desired_running"] and first["pid"] is not None
    assert second["operator_paused"] and not second["desired_running"] and second["pid"] is None
    assert len(runtime.children) == 1


def test_explicit_start_of_disabled_placeholder_survives_admission_and_keeps_sibling_paused(joint_runtime):
    runtime = joint_runtime(enabled=False, active=True, require_explicit_start=True)
    runtime.service.reconcile_once()
    assert not runtime.registry.snapshot() and not runtime.publication.called
    assert not runtime.supervisor.start_worker("gpu-0")
    runtime.service.reconcile_once()
    first, second = (runtime.supervisor.snapshot(f"gpu-{index}") for index in range(2))
    assert first["desired_running"] and first["pid"] is not None and not first["operator_paused"]
    assert second["operator_paused"] and second["pid"] is None
    assert len(runtime.children) == 1


def test_intent_expiry_during_cleanup_rejects_install_and_retries_after_fresh_publication(joint_runtime, monkeypatch):
    runtime = joint_runtime(active=True)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", index)
    runtime.service.reconcile_once()
    before_registry, before_planners = runtime.registry.snapshot(), _planner_state(runtime)
    old_children = tuple(runtime.children)
    actual_cleanup = runtime.supervisor._terminate_launch_tree
    batches = Mock(wraps=runtime.supervisor.replace_launches)

    def expire_during_cleanup(process):
        result = actual_cleanup(process)
        runtime.clock[0] = 2_601.0
        return result

    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", expire_during_cleanup)
    monkeypatch.setattr(runtime.supervisor, "replace_launches", batches)
    for index in range(2):
        _set_span(runtime, f"gpu-{index}", 1 - index)
    with pytest.raises(ValueError, match="live matching signed intent"):
        runtime.service.reconcile_once()
    assert runtime.registry.snapshot() == before_registry
    assert _planner_state(runtime) == before_planners
    assert runtime.supervisor.launch_transition_status["state"] == "cleanup_failed"
    assert all(process.poll() is not None for process in old_children)
    assert len(runtime.children) == 2
    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", actual_cleanup)
    runtime.service.reconcile_once()
    assert all({launch.worker_id for launch in call.args[0]} == {"gpu-0", "gpu-1"} for call in batches.call_args_list)
    assert batches.call_count == 2 and len(runtime.children) == 4
    assert runtime.supervisor.launch_transition_status["state"] == "idle"
    _assert_disjoint(runtime.registry.snapshot())


def test_direct_start_cannot_launch_expired_admission_until_service_refreshes_lease(joint_runtime):
    runtime = joint_runtime()
    runtime.service.reconcile_once()
    runtime.clock[0] = 2_601.0
    assert not runtime.supervisor.start_worker("gpu-0")
    assert not runtime.children
    assert not runtime.supervisor.snapshot("gpu-0")["resource_admitted"]
    runtime.service.reconcile_once()
    assert runtime.supervisor.start_worker("gpu-0")
    assert len(runtime.children) == 1


def test_policy_reprepare_preserves_live_placement_guard_after_reconfigure(joint_runtime):
    runtime = joint_runtime()
    runtime.service.reconcile_once()
    for worker in runtime.config.workers:
        runtime.supervisor.pause_worker(worker.worker_id)
    settings = run_node._prepare_worker_supervisor_settings(
        runtime.config,
        runtime.manager,
        automatic_placements=runtime.registry.snapshot(),
        automatic_placement_guards=runtime.guards,
    )
    persisted = []
    runtime.supervisor.reconfigure(settings, persist=lambda: persisted.append(True))
    assert persisted == [True]
    runtime.clock[0] = 2_601.0
    assert not runtime.supervisor.start_worker("gpu-0")
    assert not runtime.children
    assert not runtime.supervisor.snapshot("gpu-0")["resource_admitted"]


def test_expiry_between_sequential_spawns_blocks_later_worker_in_same_batch(joint_runtime):
    runtime = joint_runtime(active=True)

    def expire_after_first_guard():
        runtime.clock[0] = 2_601.0

    runtime.before_spawn[0] = expire_after_first_guard
    runtime.service.reconcile_once()
    assert len(runtime.children) == 1
    assert runtime.supervisor.snapshot("gpu-0")["pid"] is not None
    second = runtime.supervisor.snapshot("gpu-1")
    assert second["pid"] is None and second["resource_suspended"]
    assert not second["resource_admitted"]
