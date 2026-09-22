"""An old single-worker client cannot bypass managed GPU batch admission."""

import copy
import json
import sys
from types import SimpleNamespace

import pytest

import drift.node.device_binding as binding_module
from drift.node.config import NodeConfig, NodeConfigError
from drift.node.gpu_selection_tokens import GpuSelectionTokens
from drift.node.model_manager import ModelDescriptor, ModelManager, ModelRuntime
from drift.node.policy_store import ContributionPolicyConflictError, ContributionPolicyStore
from drift.node.worker_supervisor import WorkerLaunch, WorkerSupervisor, WorkerSupervisorSettings


def worker(worker_id, device, *, managed=False):
    result = {
        "id": worker_id,
        "model": "model",
        "block_indices": "2:4",
        "identity_path": f"{worker_id}.key",
        "device": device,
        "max_processing_percent": 25,
        "enabled": False,
    }
    if managed:
        result["managed_by"] = "desktop_gpu"
    return result


@pytest.fixture
def runtime_factory(tmp_path, monkeypatch):
    identities = {f"cuda:{index}": f"GPU-00000000-0000-0000-0000-{index + 1:012d}" for index in range(2)}
    monkeypatch.setattr(binding_module, "_cuda_identity", identities.get)
    monkeypatch.setattr(binding_module, "_DEFAULT_LIVENESS_PROBE", lambda value: value in identities.values())
    runtimes = []

    def build(workers):
        directory = tmp_path / str(len(runtimes))
        directory.mkdir()
        path = directory / "node.json"
        document = {
            "schema_version": 1,
            "models": [{"manifest": "manifest.json", "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"]}],
            "contribution_policy": {"sharing_enabled": False, "processing_scope": "per_device"},
            "workers": copy.deepcopy(workers),
        }
        path.write_text(json.dumps(document), encoding="utf-8")
        prepare_calls = []

        def prepare(config):
            prepare_calls.append(config)
            return WorkerSupervisorSettings(
                launches=tuple(
                    WorkerLaunch(
                        item.worker_id,
                        item.model,
                        (sys.executable, "-c", "pass"),
                        auto_start=False,
                        auto_restart=False,
                        device=item.device,
                    )
                    for item in config.workers
                ),
                stop_timeout=1,
            )

        supervisor = WorkerSupervisor(prepare(NodeConfig.load(path)).launches)
        prepare_calls.clear()
        for item in workers:
            supervisor.pause_worker(item["id"])
        manager = ModelManager()
        manager.register(ModelDescriptor("model"), lambda: ModelRuntime(object(), object()))
        store = ContributionPolicyStore(path, supervisor, prepare)
        store._gpu_selection_tokens = GpuSelectionTokens(
            devices=lambda: tuple(identities), identity=identities.get, live=lambda value: value in identities.values()
        )
        result = SimpleNamespace(
            path=path, store=store, manager=manager, supervisor=supervisor, prepare_calls=prepare_calls
        )
        runtimes.append(result)
        return result

    yield build
    for runtime in runtimes:
        runtime.supervisor.shutdown()
        runtime.manager.shutdown()


def legacy_request(runtime, operation, worker_id="managed-zero", device="cuda:1", **changes):
    result = {
        "schema_version": 1,
        "expected_config_revision": runtime.store.snapshot()["config_revision"],
        "operation": operation,
    }
    if operation != "add":
        result["worker_id"] = worker_id
    if operation != "remove":
        result["device"] = device
    result.update(changes)
    return result


def private_files(runtime):
    return {
        str(path.relative_to(runtime.path.parent)): path.read_bytes()
        for path in runtime.path.parent.rglob("*")
        if path.is_file()
    }


@pytest.mark.parametrize(
    "operation,target,device,mixed",
    [
        ("add", None, "cuda:1", False),
        ("remove", "managed-zero", None, False),
        ("reselect", "managed-zero", "cuda:0", False),
        ("reselect", "managed-zero", "cuda:1", False),
        ("add", None, "cuda:1", True),
        ("remove", "managed-zero", None, True),
        ("reselect", "managed-zero", "cuda:1", True),
        ("remove", "manual-one", None, True),
        ("reselect", "manual-one", "cuda:0", True),
    ],
)
@pytest.mark.parametrize("with_token", [False, True])
def test_legacy_mutations_cannot_bypass_managed_batch_admission(
    runtime_factory, operation, target, device, mixed, with_token
):
    workers = [worker("managed-zero", "cuda:0", managed=True)]
    if mixed:
        workers.append(worker("manual-one", "cuda:1"))
    runtime = runtime_factory(workers)
    request = legacy_request(runtime, operation, worker_id=target, device=device)
    if with_token and operation != "remove":
        choices = runtime.store._gpu_selection_tokens.snapshot(request["expected_config_revision"])["devices"]
        request["selection_token"] = next(item["selection_token"] for item in choices if item["device"] == device)
    before, revision = private_files(runtime), runtime.store.snapshot()["config_revision"]
    with pytest.raises(NodeConfigError, match="GPU selection batch") as error:
        runtime.store.update_worker_selection(request, manager=runtime.manager)
    assert type(error.value).__name__ == "ManagedGpuSelectionRequiredError"
    assert private_files(runtime) == before
    assert runtime.store.snapshot()["config_revision"] == revision
    assert runtime.prepare_calls == []
    assert not runtime.supervisor.configuration_restart_pending
    with runtime.manager.load("model"):
        pass


@pytest.mark.parametrize("operation", ["add", "remove", "reselect"])
def test_legacy_only_configuration_retains_existing_operations(runtime_factory, operation):
    workers = [] if operation == "add" else [worker("worker-looks-managed", "cuda:0")]
    runtime = runtime_factory(workers)
    result = runtime.store.update_worker_selection(
        legacy_request(runtime, operation, worker_id="WORKER-LOOKS-MANAGED"), manager=runtime.manager
    )
    saved = NodeConfig.load(runtime.path)
    assert result["restart_required"] and runtime.supervisor.configuration_restart_pending
    if operation == "remove":
        assert not saved.workers
    else:
        assert len(saved.workers) == 1 and saved.workers[0].device == "cuda:1"
        assert saved.workers[0].enabled is False
        if operation == "reselect":
            assert saved.workers[0].managed_by is None
            assert saved.workers[0].block_indices == "2:4"


def test_managed_guard_keeps_revision_conflicts_authoritative(runtime_factory):
    runtime = runtime_factory([worker("managed-zero", "cuda:0", managed=True)])
    before = private_files(runtime)
    with pytest.raises(ContributionPolicyConflictError):
        runtime.store.update_worker_selection(
            legacy_request(runtime, "remove", expected_config_revision="sha256:" + "0" * 64),
            manager=runtime.manager,
        )
    assert private_files(runtime) == before and runtime.prepare_calls == []
