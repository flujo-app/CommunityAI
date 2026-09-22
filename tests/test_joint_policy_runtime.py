"""Real saved-config coordination around the single-auto runtime service.

The shared fixture supplies controlled coverage/acknowledgements and contained
sleeping CPU children. These tests load an accepted one-auto config from disk;
they do not bypass schema admission or run models, network calls or GPUs.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from test_joint_placement_runtime import _planner_state, _set_span, joint_runtime  # noqa: F401

from drift.cli import run_node
from drift.node.config import NodeConfig
from drift.node.discovery import PeerCache
from drift.node.policy_store import ContributionPolicyConflictError, ContributionPolicyStore
from drift.node.worker_supervisor import WorkerReconfigurationBusyError


@pytest.fixture
def policy_runtime(joint_runtime):
    def create(*, active):
        runtime = joint_runtime(count=1, active=active)
        worker = runtime.config.workers[0]
        path = worker.identity_path.parent / "node.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "models": [
                        {
                            "manifest": str(runtime.config.models[0].manifest_path),
                            "initial_peers": list(runtime.config.models[0].initial_peers),
                        }
                    ],
                    "workers": [
                        {
                            "id": worker.worker_id,
                            "model": "auto",
                            "identity_path": str(worker.identity_path),
                            "num_blocks": worker.num_blocks,
                            "enabled": worker.enabled,
                            "device": "cpu",
                        }
                    ],
                    "contribution_policy": dict(runtime.config.contribution_policy.to_dict()),
                }
            ),
            encoding="utf-8",
        )
        assert NodeConfig.load(path) == runtime.config
        preparations = []

        def prepare(config):
            plans = runtime.registry.snapshot()
            preparations.append(plans)
            return run_node._prepare_worker_supervisor_settings(
                config,
                runtime.manager,
                automatic_placements=plans,
                automatic_placement_guards=runtime.guards,
            )

        runtime.path = path
        runtime.preparations = preparations
        runtime.store = ContributionPolicyStore(path, runtime.supervisor, prepare, expected_config=runtime.config)
        runtime.service.close()
        runtime.service = run_node._build_automatic_placement_service(
            runtime.config,
            runtime.manager,
            runtime.discovery,
            runtime.supervisor,
            runtime.registry,
            token=None,
            config_path=path,
            peer_cache=PeerCache(path.parent / "peers.json"),
            policy_store=runtime.store,
            automatic_placement_guards=runtime.guards,
        )
        return runtime

    return create


def _next_policy(runtime):
    snapshot = runtime.store.snapshot()
    policy = dict(snapshot["policy"])
    policy["pause_timeout"] += 1
    return policy, snapshot["config_revision"]


def test_integrated_cleanup_blocks_policy_mutation_but_allows_status_and_pause(policy_runtime, monkeypatch):
    runtime = policy_runtime(active=True)
    _set_span(runtime, "gpu-0", 0)
    runtime.service.reconcile_once()
    old_child = runtime.children[0]
    original_disk = runtime.path.read_bytes()
    old_plans = runtime.registry.snapshot()
    old_planners = _planner_state(runtime)
    policy, revision = _next_policy(runtime)
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    actual_cleanup = runtime.supervisor._terminate_launch_tree

    def controlled_cleanup(process):
        cleanup_entered.set()
        assert release_cleanup.wait(timeout=5)
        return actual_cleanup(process)

    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", controlled_cleanup)
    _set_span(runtime, "gpu-0", 1)
    with ThreadPoolExecutor(max_workers=3) as pool:
        reconcile = pool.submit(runtime.service.reconcile_once)
        try:
            assert cleanup_entered.wait(timeout=2)
            update = pool.submit(runtime.store.update, policy, expected_revision=revision)
            with pytest.raises(WorkerReconfigurationBusyError, match="transaction"):
                update.result(timeout=2)

            def status_and_pause():
                snapshot = runtime.store.snapshot()
                runtime.supervisor.pause_worker("gpu-0")
                return snapshot, runtime.supervisor.snapshot("gpu-0")

            snapshot, worker = pool.submit(status_and_pause).result(timeout=2)
            assert snapshot["config_revision"] == revision
            assert worker["operator_paused"] and worker["state"] == "stopping"
            assert runtime.path.read_bytes() == original_disk
            assert not runtime.preparations
            assert runtime.registry.snapshot() == old_plans
            assert _planner_state(runtime) == old_planners
        finally:
            release_cleanup.set()
        reconcile.result(timeout=5)
    assert old_child.poll() is not None
    assert len(runtime.children) == 1
    worker = runtime.supervisor.snapshot("gpu-0")
    assert worker["operator_paused"] and not worker["desired_running"] and worker["pid"] is None
    assert runtime.registry.snapshot()["gpu-0"].decision.block_indices == "1:2"
    assert runtime.store.snapshot()["config_revision"] == revision


def test_disk_edit_during_cleanup_rejects_before_runtime_install_or_metadata_commit(policy_runtime, monkeypatch):
    runtime = policy_runtime(active=True)
    _set_span(runtime, "gpu-0", 0)
    runtime.service.reconcile_once()
    original_disk = runtime.path.read_bytes()
    old_launches = runtime.supervisor.launches
    old_plans = runtime.registry.snapshot()
    old_planners = _planner_state(runtime)
    actual_cleanup = runtime.supervisor._terminate_launch_tree

    def edit_during_cleanup(process):
        result = actual_cleanup(process)
        runtime.path.write_bytes(original_disk + b"\n")
        return result

    monkeypatch.setattr(runtime.supervisor, "_terminate_launch_tree", edit_during_cleanup)
    _set_span(runtime, "gpu-0", 1)
    with pytest.raises(ContributionPolicyConflictError, match="during placement cleanup"):
        runtime.service.reconcile_once()
    assert runtime.supervisor.launches == old_launches
    assert runtime.registry.snapshot() == old_plans
    assert _planner_state(runtime) == old_planners
    assert runtime.supervisor.launch_transition_status["state"] == "cleanup_failed"
    assert len(runtime.children) == 1 and runtime.children[0].poll() is not None


def test_policy_update_during_proposal_rejects_stale_acceptance_before_runtime_change(policy_runtime, monkeypatch):
    runtime = policy_runtime(active=False)
    _set_span(runtime, "gpu-0", 0)
    runtime.service.reconcile_once()
    runtime.supervisor.pause_worker("gpu-0")
    old_plans = runtime.registry.snapshot()
    old_planners = _planner_state(runtime)
    policy, revision = _next_policy(runtime)
    proposal_entered = threading.Event()
    release_proposal = threading.Event()
    actual_candidates = run_node._automatic_placement_candidates

    def controlled_candidates(*args, **kwargs):
        proposal_entered.set()
        assert release_proposal.wait(timeout=5)
        return actual_candidates(*args, **kwargs)

    monkeypatch.setattr(run_node, "_automatic_placement_candidates", controlled_candidates)
    batch = Mock(wraps=runtime.supervisor.replace_launches)
    monkeypatch.setattr(runtime.supervisor, "replace_launches", batch)
    _set_span(runtime, "gpu-0", 1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reconcile = pool.submit(runtime.service.reconcile_once)
        try:
            assert proposal_entered.wait(timeout=2)
            result = pool.submit(runtime.store.update, policy, expected_revision=revision).result(timeout=2)
            assert result["config_revision"] != revision
            assert runtime.preparations == [old_plans]
            accepted_launches = runtime.supervisor.launches
        finally:
            release_proposal.set()
        with pytest.raises(ContributionPolicyConflictError, match="refresh before placement"):
            reconcile.result(timeout=5)
    batch.assert_not_called()
    assert runtime.supervisor.launches == accepted_launches
    assert runtime.registry.snapshot() == old_plans
    assert _planner_state(runtime) == old_planners
    assert not runtime.children
    assert runtime.store.snapshot()["config_revision"] == result["config_revision"]
    assert NodeConfig.load(runtime.path).contribution_policy.pause_timeout == policy["pause_timeout"]
