import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from drift.cli import run_node
from drift.model_manifest import ModelManifest
from drift.node import device_binding
from drift.node.config import ContributionPolicyConfig, NodeConfig, NodeConfigError, WorkerConfig
from drift.node.model_manager import ModelManager
from drift.server.processing_budget import ProcessingBudget


@pytest.fixture
def processing_runtime(tmp_path, monkeypatch):
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
    loader = Mock(side_effect=AssertionError("processing configuration must not load models"))
    manager = ModelManager()
    manager.register_manifest(manifest, loader)

    def config(devices, percentages, *, scope="per_device", parent_percent=5):
        return NodeConfig.from_dict(
            {
                "schema_version": 1,
                "models": [{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
                "contribution_policy": {
                    "sharing_enabled": True,
                    "max_disk_space": "1GiB",
                    "max_vram": "50%",
                    "max_processing_percent": parent_percent,
                    **({"processing_scope": scope} if scope is not None else {}),
                },
                "workers": [
                    {
                        "id": f"worker-{index}",
                        "model": manifest.digest_id,
                        "block_indices": f"{index}:{index + 1}",
                        "identity_path": f"worker-{index}.key",
                        **({"device": device} if device is not None else {}),
                        **({"max_processing_percent": percent} if percent is not None else {}),
                    }
                    for index, (device, percent) in enumerate(zip(devices, percentages))
                ],
            },
            base_dir=tmp_path,
        )

    def prepare(config):
        return run_node._prepare_worker_supervisor_settings(config, manager).launches

    yield SimpleNamespace(config=config, prepare=prepare, cards=cards, root=tmp_path)
    manager.shutdown()
    loader.assert_not_called()


def argument(launch, name):
    return launch.command[launch.command.index(name) + 1]


def budget(launch, **kwargs):
    return ProcessingBudget(
        float(argument(launch, "--max_processing_percent")),
        path=argument(launch, "--processing_budget_path"),
        **kwargs,
    )


def test_default_scope_preserves_policy_document_and_exact_global_lock(processing_runtime):
    runtime = processing_runtime
    config = runtime.config(["cuda:0", "cuda:1"], [None, None], scope=None, parent_percent=25)
    assert config.contribution_policy.processing_scope == "node"
    assert "processing_scope" not in config.contribution_policy.to_dict()
    launches = runtime.prepare(config)
    for launch in launches:
        assert argument(launch, "--max_processing_percent") == "25.0"
        assert argument(launch, "--processing_budget_path") == str(runtime.root / ".worker-0.key.processing-budget")


def test_device_scope_roundtrips_without_inheriting_parent_cap(processing_runtime):
    runtime = processing_runtime
    config = runtime.config(["cuda:0", "cuda:1"], [25, 75], parent_percent=5)
    policy = config.contribution_policy
    assert ContributionPolicyConfig.from_dict(policy.to_dict()) == policy
    first, second = runtime.prepare(config)
    assert argument(first, "--max_processing_percent") == "25.0"
    assert argument(second, "--max_processing_percent") == "75.0"
    assert argument(first, "--processing_budget_path") != argument(second, "--processing_budget_path")
    for launch, card in zip((first, second), runtime.cards):
        assert card.uuid not in argument(launch, "--processing_budget_path")


@pytest.mark.parametrize("value", [0, -1, 101, True, "50", float("nan"), float("inf"), 10**1000])
def test_worker_percent_rejects_invalid_values(processing_runtime, value):
    with pytest.raises(NodeConfigError, match="max_processing_percent"):
        processing_runtime.config(["cuda:0"], [value])


@pytest.mark.parametrize("value", [None, "all", True, {}, "PER_DEVICE"])
def test_policy_rejects_unknown_scope(value):
    with pytest.raises(NodeConfigError, match="processing_scope"):
        ContributionPolicyConfig.from_dict({"sharing_enabled": False, "processing_scope": value})


@pytest.mark.parametrize("marker", [None, "desktop_gpu"])
def test_worker_ownership_marker_is_optional_metadata(tmp_path, marker):
    source = {"id": "worker", "model": "model", "identity_path": "worker.key", "block_indices": "0:1"}
    original = WorkerConfig.from_dict(source, base_dir=tmp_path, index=0)
    marked = WorkerConfig.from_dict({**source, "managed_by": marker}, base_dir=tmp_path, index=0)
    assert marked.managed_by == marker
    assert replace(marked, managed_by=None) == original


@pytest.mark.parametrize("marker", [True, "desktop", "DESKTOP_GPU", "", [], {}])
def test_worker_ownership_marker_rejects_other_values(tmp_path, marker):
    source = {
        "id": "worker",
        "model": "model",
        "identity_path": "worker.key",
        "block_indices": "0:1",
        "managed_by": marker,
    }
    with pytest.raises(NodeConfigError, match="managed_by"):
        WorkerConfig.from_dict(source, base_dir=tmp_path, index=0)


@pytest.mark.parametrize(
    "device,percent,scope,error",
    [
        ("cuda:0", 50, "node", "overrides require"),
        (None, 50, "per_device", "explicit device"),
        ("cuda:0", None, "per_device", "max_processing_percent"),
    ],
)
def test_scopes_cannot_be_silently_mixed(processing_runtime, device, percent, scope, error):
    with pytest.raises(NodeConfigError, match=error):
        processing_runtime.config([device], [percent], scope=scope)


def test_same_configured_card_requires_one_percentage(processing_runtime):
    with pytest.raises(NodeConfigError, match="same processing percentage"):
        processing_runtime.config(["cuda:0", "cuda:0"], [25, 50])


def test_physical_aliases_require_one_percentage(processing_runtime):
    runtime = processing_runtime
    runtime.cards[1] = runtime.cards[0]
    config = runtime.config(["cuda:0", "cuda:1"], [25, 50])
    with pytest.raises(NodeConfigError, match="physical device"):
        runtime.prepare(config)


def test_physical_aliases_share_lock_and_order_does_not_change_it(processing_runtime):
    runtime = processing_runtime
    runtime.cards[1] = runtime.cards[0]
    config = runtime.config(["cuda:0", "cuda:1"], [50, 50])
    first, second = runtime.prepare(config)
    assert argument(first, "--processing_budget_path") == argument(second, "--processing_budget_path")
    reversed_launches = runtime.prepare(replace(config, workers=tuple(reversed(config.workers))))
    assert argument(reversed_launches[0], "--processing_budget_path") == argument(first, "--processing_budget_path")


def test_cpu_workers_have_one_separate_bucket(processing_runtime):
    runtime = processing_runtime
    launches = runtime.prepare(runtime.config(["cpu", "cpu:0", "cuda:0"], [50, 50, 50]))
    paths = [argument(launch, "--processing_budget_path") for launch in launches]
    assert paths[0] == paths[1] != paths[2]


@pytest.mark.parametrize("mutation", ["scope", "missing_percent", "invalid_percent", "missing_device", "node_override"])
def test_programmatic_configs_cannot_bypass_validation(processing_runtime, mutation):
    runtime = processing_runtime
    config = runtime.config(["cuda:0"], [50])
    if mutation in ("scope", "node_override"):
        scope = "invalid" if mutation == "scope" else "node"
        config = replace(config, contribution_policy=replace(config.contribution_policy, processing_scope=scope))
    else:
        fields = {
            "missing_percent": {"max_processing_percent": None},
            "invalid_percent": {"max_processing_percent": True},
            "missing_device": {"device": None},
        }[mutation]
        config = replace(config, workers=(replace(config.workers[0], **fields),))
    with pytest.raises(NodeConfigError):
        runtime.prepare(config)


def test_full_compute_preserves_uncapped_fast_path(processing_runtime):
    runtime = processing_runtime
    launch = runtime.prepare(runtime.config(["cuda:0"], [100]))[0]
    assert argument(launch, "--max_processing_percent") == "100.0"
    assert "--processing_budget_path" not in launch.command


def test_missing_card_keeps_processing_setting_but_cannot_fall_back(processing_runtime):
    runtime = processing_runtime
    config = runtime.config(["cuda:1"], [25])
    runtime.prepare(config)
    runtime.cards.pop()
    launch = runtime.prepare(config)[0]
    assert launch.device == "cuda:1" and launch.device_available() is False
    assert argument(launch, "--max_processing_percent") == "25.0"
    assert "CUDA_VISIBLE_DEVICES" not in dict(launch.environment)


def test_different_cards_compute_concurrently_with_real_os_locks(processing_runtime):
    runtime = processing_runtime
    launches = runtime.prepare(runtime.config(["cuda:0", "cuda:1"], [50, 50]))
    both_computing = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(budget(launch).run, lambda: both_computing.wait(timeout=3)) for launch in launches]
        assert sorted(future.result(timeout=5) for future in futures) == [0, 1]


def test_same_card_waits_for_other_workers_cooldown(processing_runtime):
    runtime = processing_runtime
    first, second = runtime.prepare(runtime.config(["cuda:0", "cuda:0"], [50, 50]))
    cooling, finish_cooldown, attempted, second_computed = (threading.Event() for _ in range(4))

    class HeldCooldown:
        def is_set(self):
            return False

        def wait(self, seconds):
            cooling.set()
            assert finish_cooldown.wait(5)

    def competing_worker():
        attempted.set()
        budget(second).run(second_computed.set)

    clock = iter((0.0, 1.0))
    with ThreadPoolExecutor(max_workers=2) as pool:
        leader = pool.submit(budget(first, stop=HeldCooldown(), clock=lambda: next(clock)).run, lambda: None)
        try:
            assert cooling.wait(5)
            follower = pool.submit(competing_worker)
            assert attempted.wait(5)
            assert not second_computed.wait(0.05)
        finally:
            finish_cooldown.set()
        leader.result(timeout=5)
        follower.result(timeout=5)
    assert second_computed.is_set()
