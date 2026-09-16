import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.model_manifest import ModelManifest
from drift.node import device_binding
from drift.node.config import NodeConfig
from drift.node.device_binding import DeviceBindingError
from drift.node.model_manager import ModelManager, ModelManagerClosedError
from drift.node.policy_store import ContributionPolicyStore
from drift.node.worker_supervisor import WorkerPolicyError, WorkerSupervisor


@pytest.fixture
def prepared_runtime(tmp_path, monkeypatch):
    # Metadata only: no CUDA initialization, model loading, download or discovery.
    cards = [
        SimpleNamespace(uuid="GPU-00000000-0000-0000-0000-000000000001", total_memory=8 * 1024**3),
        SimpleNamespace(uuid="GPU-00000000-0000-0000-0000-000000000002", total_memory=24 * 1024**3),
    ]
    monkeypatch.setattr(run_node.torch.cuda, "is_available", lambda: bool(cards))
    monkeypatch.setattr(run_node.torch.cuda, "device_count", lambda: len(cards))
    monkeypatch.setattr(run_node.torch.cuda, "get_device_properties", lambda index: cards[index])
    monkeypatch.setattr(run_node, "get_device_total_memory", lambda device: cards[device.index].total_memory)
    monkeypatch.setattr(
        device_binding, "_DEFAULT_LIVENESS_PROBE", lambda identity: any(card.uuid == identity for card in cards)
    )
    manifest = ModelManifest.load(Path(__file__).parent / "data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    loader = Mock(side_effect=AssertionError("selection must never load a model"))
    popen = Mock(side_effect=AssertionError("selection must never launch a worker"))
    managers = []
    supervisors = []

    def manager():
        result = ModelManager()
        result.register_manifest(manifest, loader)
        result.set_catalog_models((manifest.digest_id,))
        managers.append(result)
        return result

    def supervisor(settings):
        result = WorkerSupervisor(settings.launches, popen=popen)
        supervisors.append(result)
        return result

    def setup(mode="manual"):
        document = {
            "schema_version": 1,
            "models": [{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            "auto_model_priority": [manifest.digest_id],
            "contribution_policy": {
                "sharing_enabled": True,
                "allowed_models": [manifest.digest_id],
                "max_disk_space": "1GiB",
                "max_vram": "50%",
            },
            "workers": [
                {
                    "id": "original",
                    "model": "auto" if mode == "auto" else manifest.digest_id,
                    **({"num_blocks": 1} if mode == "auto" else {"block_indices": "2:4"}),
                    "device": "cuda:0",
                    "identity_path": "original.key",
                    "enabled": False,
                }
            ],
        }
        path = tmp_path / "node.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        models = manager()
        prepare = Mock(wraps=lambda config: run_node._prepare_worker_supervisor_settings(config, models))
        workers = supervisor(prepare(NodeConfig.load(path)))
        workers.pause_worker("original")
        prepare.reset_mock()
        store = ContributionPolicyStore(path, workers, prepare)
        selection = {
            "schema_version": 1,
            "expected_config_revision": store.snapshot()["config_revision"],
            "operation": "reselect",
            "worker_id": "original",
            "device": "cuda:1",
        }
        return SimpleNamespace(
            path=path, manager=models, supervisor=workers, prepare=prepare, store=store, request=selection
        )

    yield SimpleNamespace(setup=setup, cards=cards, manager=manager, supervisor=supervisor, popen=popen, loader=loader)
    for workers in supervisors:
        workers.shutdown()
    for models in managers:
        models.shutdown()
    loader.assert_not_called()
    popen.assert_not_called()


@pytest.mark.parametrize("mode", ["manual", "auto"])
def test_real_preparation_reenters_readonly_manager_and_reloads_stopped(prepared_runtime, monkeypatch, mode):
    runtime = prepared_runtime.setup(mode)
    resolve = Mock(wraps=runtime.manager.resolve)
    catalog = Mock(wraps=runtime.manager.catalog_allows_contribution)
    monkeypatch.setattr(runtime.manager, "resolve", resolve)
    monkeypatch.setattr(runtime.manager, "catalog_allows_contribution", catalog)
    result = runtime.store.update_worker_selection(runtime.request, manager=runtime.manager)
    runtime.prepare.assert_called_once()
    assert resolve.called
    if mode == "auto":
        assert catalog.called
    with pytest.raises(ModelManagerClosedError):
        runtime.manager.load("tiny")

    config = NodeConfig.load(runtime.path)
    worker = config.workers[0]
    assert worker.worker_id == result["worker_id"] != "original"
    assert worker.identity_path != runtime.path.parent / "original.key"
    assert worker.device == "cuda:1" and worker.enabled is False
    assert (worker.num_blocks, worker.block_indices) == ((1, None) if mode == "auto" else (None, "2:4"))
    settings = run_node._prepare_worker_supervisor_settings(config, prepared_runtime.manager())
    launch = settings.launches[0]
    assert launch.worker_id == worker.worker_id and launch.device == "cuda:1"
    assert launch.command[launch.command.index("--identity_path") + 1] == str(worker.identity_path)
    assert launch.command[launch.command.index("--device") + 1] == "cuda:0"
    assert dict(launch.environment)["CUDA_VISIBLE_DEVICES"] == prepared_runtime.cards[1].uuid
    assert launch.max_vram_bytes == 12 * 1024**3
    assert launch.auto_start is False
    restarted = prepared_runtime.supervisor(settings)
    run_node._apply_startup_pause(SimpleNamespace(pause_sharing_on_start=True), restarted)
    snapshot = restarted.snapshot(worker.worker_id)
    assert snapshot["state"] == "paused" and snapshot["operator_paused"] is True
    assert snapshot["desired_running"] is False and snapshot["pid"] is None


@pytest.mark.parametrize("change", ["missing", "reordered"])
def test_saved_selection_rejects_changed_hardware_without_fallback(prepared_runtime, change):
    runtime = prepared_runtime.setup()
    result = runtime.store.update_worker_selection(runtime.request, manager=runtime.manager)
    committed = runtime.path.read_bytes()
    if change == "missing":
        prepared_runtime.cards.pop()
    else:
        prepared_runtime.cards.reverse()
    config = NodeConfig.load(runtime.path)
    settings = run_node._prepare_worker_supervisor_settings(config, prepared_runtime.manager())
    launch = settings.launches[0]
    assert launch.device == "cuda:1" and launch.device_available() is False
    assert launch.max_vram_bytes is None and "CUDA_VISIBLE_DEVICES" not in dict(launch.environment)
    restarted = prepared_runtime.supervisor(settings)
    with pytest.raises(WorkerPolicyError, match="selected device is unavailable or has changed"):
        restarted.start_worker(result["worker_id"])
    assert restarted.snapshot(result["worker_id"])["pid"] is None
    assert runtime.path.read_bytes() == committed


def test_missing_card_rejects_reselection_before_config_or_admission_changes(prepared_runtime):
    runtime = prepared_runtime.setup()
    original = runtime.path.read_bytes()
    prepared_runtime.cards.pop()
    with pytest.raises(DeviceBindingError):
        runtime.store.update_worker_selection(runtime.request, manager=runtime.manager)
    runtime.prepare.assert_not_called()
    assert runtime.path.read_bytes() == original
    assert not runtime.supervisor.configuration_restart_pending
    assert runtime.manager.resolve("tiny").model_id == "Tiny Test"
    assert runtime.supervisor.launches[0].device == "cuda:0"
