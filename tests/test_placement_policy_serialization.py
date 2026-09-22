"""Real store/supervisor serialization without processes, networking or GPUs."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from drift.node.config import NodeConfig
from drift.node.policy_store import ContributionPolicyConflictError, ContributionPolicyStore
from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


@pytest.fixture
def runtime(tmp_path):
    path = tmp_path / "node.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
                "contribution_policy": {"sharing_enabled": False},
                "workers": [
                    {
                        "id": "worker",
                        "model": "model",
                        "identity_path": "worker.key",
                        "num_blocks": 1,
                        "device": "cpu",
                        "enabled": False,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    registry = {"generation": "old"}
    preparations = []
    prepare_hook = [None]

    def prepare(config):
        generation = registry["generation"]
        preparations.append(generation)
        if prepare_hook[0] is not None:
            prepare_hook[0]()
        return WorkerSupervisorSettings(
            launches=(
                WorkerLaunch(
                    "worker",
                    "model",
                    (sys.executable, "-c", f"pass  # {generation}"),
                    auto_start=False,
                    auto_restart=False,
                ),
            ),
            stop_timeout=config.contribution_policy.pause_timeout,
        )

    supervisor = WorkerSupervisor(prepare(NodeConfig.load(path)).launches)
    preparations.clear()
    store = ContributionPolicyStore(path, supervisor, prepare)
    yield SimpleNamespace(
        path=path,
        store=store,
        supervisor=supervisor,
        registry=registry,
        preparations=preparations,
        prepare_hook=prepare_hook,
    )
    supervisor.shutdown()


def mutation(runtime, name, revision):
    store = runtime.store
    if name == "policy":
        return lambda: store.update({"sharing_enabled": False, "pause_timeout": 11}, expected_revision=revision)
    if name == "inference":
        return lambda: store.update_inference_mode("local_only", expected_revision=revision)
    if name == "gpu":
        return lambda: store.update_gpu_selection(
            {"schema_version": 1, "expected_config_revision": revision, "rows": []},
            manager=object(),
            hardware_status=None,
        )
    return lambda: store.update_worker_selection(
        {"schema_version": 1, "expected_config_revision": revision, "operation": "remove", "worker_id": "worker"},
        manager=object(),
    )


def test_status_and_direct_pause_remain_responsive_during_placement(runtime):
    store = runtime.store
    before = store.snapshot()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.placement_transaction(expected_revision=before["config_revision"]):

            def read_and_pause():
                snapshot = store.snapshot()
                provenance = store.worker_provenance()
                runtime.supervisor.pause_worker("worker")
                return snapshot, provenance, runtime.supervisor.snapshot("worker")

            snapshot, provenance, worker = pool.submit(read_and_pause).result(timeout=2)
            assert snapshot == before
            assert provenance == {}
            assert worker["operator_paused"] is True
            assert worker["pid"] is None
    assert runtime.preparations == []


@pytest.mark.parametrize("name", ["policy", "inference", "gpu", "worker"])
def test_all_mutations_fail_fast_before_store_lock_or_effects(runtime, name):
    store = runtime.store
    revision = store.snapshot()["config_revision"]
    original = runtime.path.read_bytes()
    with ThreadPoolExecutor(max_workers=1) as pool:
        with store.placement_transaction(expected_revision=revision):
            # If mutation acquires the store lock first, this future cannot
            # finish until the context exits; a nonblocking guard must win.
            with store._lock:
                future = pool.submit(mutation(runtime, name, revision))
                with pytest.raises(WorkerReconfigurationBusyError, match="transaction"):
                    future.result(timeout=2)
            assert runtime.path.read_bytes() == original
            assert runtime.preparations == []
            assert not runtime.supervisor.configuration_restart_pending
    assert store.snapshot()["config_revision"] == revision


@pytest.mark.parametrize("problem", ["stale", "disk", "store_restart", "supervisor_restart"])
def test_placement_rejects_stale_disk_and_restart_state_before_yield(runtime, problem):
    store = runtime.store
    revision = store.snapshot()["config_revision"]
    original = runtime.path.read_bytes()
    expected = ContributionPolicyConflictError
    if problem == "stale":
        revision = "sha256:" + "0" * 64
    elif problem == "disk":
        runtime.path.write_bytes(original + b"\n")
    elif problem == "store_restart":
        store._restart_pending = True
        expected = WorkerReconfigurationBusyError
    else:
        runtime.supervisor.pause_worker("worker")
        runtime.supervisor.commit_configuration_restart(lambda: None)
        expected = WorkerReconfigurationBusyError
    entered = False
    with pytest.raises(expected):
        with store.placement_transaction(expected_revision=revision):
            entered = True
    assert not entered
    assert runtime.preparations == []
    if problem in ("stale", "disk"):
        runtime.path.write_bytes(original)
        with store.placement_transaction(expected_revision=store.snapshot()["config_revision"]):
            pass  # Failed pre-yield checks released coordination.


def test_placement_error_releases_coordination_without_metadata_commit(runtime):
    store = runtime.store
    revision = store.snapshot()["config_revision"]
    with pytest.raises(RuntimeError, match="cleanup remains pending"):
        with store.placement_transaction(expected_revision=revision):
            raise RuntimeError("cleanup remains pending")
    assert runtime.registry["generation"] == "old"
    result = store.update({"sharing_enabled": False, "pause_timeout": 12}, expected_revision=revision)
    assert result["config_revision"] != revision
    assert runtime.preparations == ["old"]


@pytest.mark.parametrize("name", ["policy", "inference", "gpu", "worker"])
def test_mutation_error_releases_coordination(runtime, monkeypatch, name):
    store = runtime.store
    revision = store.snapshot()["config_revision"]

    def failed_read():
        raise RuntimeError("fixture read failure")

    with monkeypatch.context() as patch:
        patch.setattr(store, "_read", failed_read)
        with pytest.raises(RuntimeError, match="fixture read failure"):
            mutation(runtime, name, revision)()
    with store.placement_transaction(expected_revision=revision):
        pass


def test_inflight_policy_preparation_cannot_cross_placement_acceptance(runtime):
    store = runtime.store
    revision = store.snapshot()["config_revision"]
    prepared_old_registry = threading.Event()
    allow_policy_commit = threading.Event()

    def hold_preparation():
        prepared_old_registry.set()
        assert allow_policy_commit.wait(timeout=5)

    runtime.prepare_hook[0] = hold_preparation
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(mutation(runtime, "policy", revision))
        try:
            assert prepared_old_registry.wait(timeout=2)
            with pytest.raises(WorkerReconfigurationBusyError, match="transaction"):
                with store.placement_transaction(expected_revision=revision):
                    pytest.fail("placement must not cross an in-flight old-registry preparation")
            assert runtime.registry["generation"] == "old"
        finally:
            allow_policy_commit.set()
        result = future.result(timeout=2)
    runtime.prepare_hook[0] = None
    assert runtime.preparations == ["old"]
    assert runtime.supervisor.launches[0].command[-1] == "pass  # old"
    with store.placement_transaction(expected_revision=result["config_revision"]):
        runtime.registry["generation"] = "accepted-new"
    store.update({"sharing_enabled": False, "pause_timeout": 12}, expected_revision=result["config_revision"])
    assert runtime.preparations == ["old", "accepted-new"]
    assert runtime.supervisor.launches[0].command[-1] == "pass  # accepted-new"


def test_nested_mutation_fails_fast_and_outer_placement_remains_usable(runtime):
    store = runtime.store
    revision = store.snapshot()["config_revision"]
    with store.placement_transaction(expected_revision=revision):
        with pytest.raises(WorkerReconfigurationBusyError):
            mutation(runtime, "policy", revision)()
        assert store.snapshot()["config_revision"] == revision
    mutation(runtime, "policy", revision)()
