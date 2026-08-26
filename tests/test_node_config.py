import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from drift.cli.run_node import _build_model_manager, _build_worker_supervisor, _load_node_config
from drift.model_manifest import ModelManifest
from drift.node.config import (
    NODE_CONFIG_SCHEMA_VERSION,
    ContributionPolicyConfig,
    ContributionScheduleConfig,
    NodeConfig,
    NodeConfigError,
    NodeModelConfig,
    WorkerConfig,
)
from drift.node.model_manager import ModelRuntime, ModelState
from drift.node.worker_supervisor import WorkerPolicyError


def _config_dict(**overrides):
    source = {
        "schema_version": 1,
        "max_loaded_models": 1,
        "discovery_update_period": 12,
        "discovery_startup_timeout": 4,
        "models": [
            {
                "manifest": "manifests/one.json",
                "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/one"],
                "cache_dir": "cache/one",
                "revocation_files": ["trust/revoked.json"],
                "request_timeout": 7.5,
                "max_retries": 2,
            }
        ],
    }
    source.update(overrides)
    return source


def test_contribution_telemetry_providers_are_core_runtime_dependencies():
    pyproject = (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    core_dependencies = pyproject.split("dependencies = [", 1)[1].split("\n]\n", 1)[0]

    assert '"psutil>=5.9"' in core_dependencies
    assert '"nvidia-ml-py>=12.535"' in core_dependencies


def test_node_config_resolves_paths_relative_to_its_own_directory(tmp_path):
    config = NodeConfig.from_json(json.dumps(_config_dict()), base_dir=tmp_path)

    assert config.schema_version == NODE_CONFIG_SCHEMA_VERSION
    assert config.max_loaded_models == 1
    assert config.discovery_update_period == 12
    assert config.discovery_startup_timeout == 4
    model = config.models[0]
    assert model.manifest_path == (tmp_path / "manifests/one.json").resolve()
    assert model.cache_dir == (tmp_path / "cache/one").resolve()
    assert model.revocation_files == ((tmp_path / "trust/revoked.json").resolve(),)
    assert model.request_timeout == 7.5
    assert model.max_retries == 2


def test_node_config_is_strict_and_does_not_accept_secrets(tmp_path):
    source = _config_dict()
    source["models"][0]["token"] = "must-not-live-here"

    with pytest.raises(NodeConfigError, match="unknown field.*token"):
        NodeConfig.from_dict(source, base_dir=tmp_path)


def test_node_config_parses_strict_worker_controls(tmp_path):
    source = _config_dict(
        workers=[
            {
                "id": "gpu-0",
                "model": "tiny-test",
                "identity_path": "secrets/gpu-0.key",
                "block_indices": "0:4",
                "enabled": True,
                "auto_restart": False,
                "restart_backoff": 2,
                "device": "cuda:0",
                "cache_dir": "worker-cache",
                "max_disk_space": "20GiB",
                "max_bandwidth_mbps": 25,
                "max_power_watts": 175.5,
                "throughput": 1.5,
                "port": 31337,
                "public_ip": "203.0.113.4",
            }
        ]
    )

    worker = NodeConfig.from_dict(source, base_dir=tmp_path).workers[0]

    assert worker.worker_id == "gpu-0"
    assert worker.identity_path == (tmp_path / "secrets/gpu-0.key").resolve()
    assert worker.block_indices == "0:4"
    assert worker.enabled is True
    assert worker.auto_restart is False
    assert worker.max_bandwidth_mbps == 25.0
    assert worker.max_power_watts == 175.5
    assert worker.throughput == 1.5
    assert worker.port == 31337


@pytest.mark.parametrize(
    "source, message",
    [
        ('{"schema_version":1,"schema_version":1,"models":[]}', "duplicate object key"),
        (json.dumps(_config_dict(max_loaded_models=0)), "max_loaded_models"),
        (json.dumps(_config_dict(models=[])), "non-empty JSON array"),
        (
            json.dumps(
                _config_dict(
                    models=[{"manifest": "one.json", "initial_peers": []}],
                )
            ),
            "initial_peers",
        ),
    ],
)
def test_node_config_rejects_malformed_inputs(tmp_path, source, message):
    with pytest.raises(NodeConfigError, match=message):
        NodeConfig.from_json(source, base_dir=tmp_path)


def test_build_manager_registers_multiple_manifests_without_loading(monkeypatch, tmp_path):
    first = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    second_dict = first.to_dict()
    second_dict["name"] = "Second Test Model"
    second_dict["aliases"] = ["second-test"]
    second = ModelManifest.from_dict(second_dict)
    first_path = tmp_path / "first.json"
    second_path = tmp_path / "second.json"
    first_path.write_text(first.canonical_json(), encoding="utf-8")
    second_path.write_text(second.canonical_json(), encoding="utf-8")
    loader_calls = []

    def fake_make_loader(manifest, **kwargs):
        loader_calls.append((manifest.digest_id, kwargs))
        return lambda: ModelRuntime(object(), object())

    monkeypatch.setattr("drift.cli.run_node.make_manifest_loader", fake_make_loader)
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(
            NodeModelConfig(first_path, ("peer-one",)),
            NodeModelConfig(second_path, ("peer-two",), cache_dir=Path("cache"), request_timeout=9, max_retries=4),
        ),
    )

    manager, descriptors, discovery = _build_model_manager(config, token="provider-token")

    assert [descriptor.model_id for descriptor in descriptors] == [first.name, second.name]
    assert [snapshot.state for snapshot in manager.snapshots()] == [ModelState.KNOWN, ModelState.KNOWN]
    assert manager.residency() == {"max_loaded_models": 1, "resident_models": 0}
    assert loader_calls[0][1]["initial_peers"] == ("peer-one",)
    assert loader_calls[1][1]["initial_peers"] == ("peer-two",)
    assert loader_calls[1][1]["cache_dir"] == "cache"
    assert loader_calls[1][1]["request_timeout"] == 9
    assert loader_calls[1][1]["max_retries"] == 4
    assert all(call[1]["token"] == "provider-token" for call in loader_calls)
    assert discovery.snapshot(first.digest_id)["status"] == "unknown"
    manager.shutdown()


def test_worker_supervisor_command_is_pinned_to_configured_manifest(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.aliases[0],
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                throughput=1.25,
            ),
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)

    supervisor = _build_worker_supervisor(config, manager, token="provider-token")
    launch = supervisor.launches[0]

    assert launch.model_id == manifest.name
    assert launch.command[:4] == (sys.executable, "-m", "drift.cli", "server")
    assert "--model_manifest" in launch.command
    assert str(manifest_path) in launch.command
    assert launch.command[launch.command.index("--num_blocks") + 1] == "2"
    assert launch.command[launch.command.index("--throughput") + 1] == "1.25"
    assert "provider-token" not in launch.command
    assert launch.environment == (("HF_TOKEN", "provider-token"),)
    supervisor.shutdown()

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    frozen_supervisor = _build_worker_supervisor(config, manager, token=None)
    assert frozen_supervisor.launches[0].command[:2] == (sys.executable, "server")
    assert "-m" not in frozen_supervisor.launches[0].command
    frozen_supervisor.shutdown()
    manager.shutdown()


def test_contribution_policy_defaults_off_and_requires_a_disk_ceiling_when_enabled(tmp_path):
    config = NodeConfig.from_dict(_config_dict(), base_dir=tmp_path)
    assert config.contribution_policy.sharing_enabled is False

    with pytest.raises(NodeConfigError, match="max_disk_space is required"):
        NodeConfig.from_dict(
            _config_dict(contribution_policy={"sharing_enabled": True}),
            base_dir=tmp_path,
        )


@pytest.mark.parametrize("field", ["max_bandwidth_mbps", "max_power_watts"])
@pytest.mark.parametrize("value", [True, 0, -1, float("inf"), "10"])
def test_contribution_policy_rejects_invalid_measured_resource_budgets(field, value):
    with pytest.raises(NodeConfigError, match=field):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                field: value,
            }
        )


@pytest.mark.parametrize("field", ["max_bandwidth_mbps", "max_power_watts"])
def test_worker_config_rejects_invalid_measured_resource_budgets(tmp_path, field):
    worker = {
        "id": "worker",
        "model": "model",
        "identity_path": "worker.key",
        "num_blocks": 1,
        field: 0,
    }
    with pytest.raises(NodeConfigError, match=field):
        NodeConfig.from_dict(_config_dict(workers=[worker]), base_dir=tmp_path)


def test_contribution_schedule_enforces_boundaries_and_overnight_windows():
    schedule = ContributionScheduleConfig.from_dict(
        {
            "timezone": "UTC",
            "windows": [
                {"days": ["mon"], "start": "09:00", "end": "17:00"},
                {"days": ["fri"], "start": "22:00", "end": "02:00"},
            ],
        }
    )

    assert schedule.allows(datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 24, 16, 59, tzinfo=timezone.utc))
    assert not schedule.allows(datetime(2026, 8, 24, 17, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 28, 23, 0, tzinfo=timezone.utc))
    assert schedule.allows(datetime(2026, 8, 29, 1, 59, tzinfo=timezone.utc))
    assert not schedule.allows(datetime(2026, 8, 29, 2, 0, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="timezone-aware"):
        schedule.allows(datetime(2026, 8, 24, 12, 0))


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ({"timezone": "Not/A_Zone", "windows": []}, "available IANA timezone"),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["mon"], "start": "09:00", "end": "09:00"}],
            },
            "start and end must differ",
        ),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["mon", "MON"], "start": "09:00", "end": "17:00"}],
            },
            "case-insensitive duplicates",
        ),
        (
            {
                "timezone": "UTC",
                "windows": [{"days": ["funday"], "start": "09:00", "end": "17:00"}],
            },
            "unsupported weekday",
        ),
    ],
)
def test_contribution_schedule_rejects_ambiguous_or_invalid_values(schedule, message):
    with pytest.raises(NodeConfigError, match=message):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "schedule": schedule,
            }
        )


def test_worker_supervisor_enforces_resolved_model_policy_and_disk_ceiling(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "worker",
                    "model": manifest.digest_id,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "enabled": True,
                    "device": "cpu",
                    "max_disk_space": "2GiB",
                    "max_bandwidth_mbps": 12,
                    "max_power_watts": 250,
                }
            ],
            contribution_policy={
                "sharing_enabled": True,
                "allowed_models": [manifest.aliases[0]],
                "preferred_models": [manifest.name],
                "max_disk_space": "1GiB",
                "max_bandwidth_mbps": 20,
                "max_power_watts": 200,
                "pause_timeout": 1.5,
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    launch = supervisor.launches[0]

    assert launch.policy_admitted is True
    assert launch.policy_reason is None
    assert launch.preferred is True
    assert launch.max_disk_bytes == 1024**3
    assert launch.max_bandwidth_mbps == 12
    assert launch.max_power_watts == 200
    assert launch.command[launch.command.index("--max_disk_space") + 1] == "1GiB"
    assert supervisor._stop_timeout == 1.5

    supervisor.shutdown()
    manager.shutdown()


def test_disabled_contribution_policy_blocks_auto_start_and_control_start(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.name,
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                enabled=True,
            ),
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    supervisor.start_service()

    snapshot = supervisor.snapshot("worker")
    assert snapshot["desired_running"] is False
    assert snapshot["policy_admitted"] is False
    assert snapshot["policy_reason"] == "sharing is disabled by contribution policy"
    with pytest.raises(WorkerPolicyError, match="sharing is disabled"):
        supervisor.start_worker("worker")

    supervisor.shutdown()
    manager.shutdown()


def test_denied_model_cannot_be_started_through_an_alias(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        workers=(
            WorkerConfig(
                worker_id="worker",
                model=manifest.digest_id,
                identity_path=tmp_path / "worker.key",
                num_blocks=2,
                enabled=True,
            ),
        ),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "denied_models": [manifest.aliases[0]],
                "max_disk_space": "1GiB",
            }
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)

    snapshot = supervisor.snapshot("worker")
    assert snapshot["desired_running"] is False
    assert snapshot["policy_admitted"] is False
    assert "denied by contribution policy" in snapshot["policy_reason"]
    with pytest.raises(WorkerPolicyError, match="denied by contribution policy"):
        supervisor.start_worker("worker")

    supervisor.shutdown()
    manager.shutdown()


def test_contribution_policy_rejects_alias_based_allow_deny_conflicts(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(manifest_path, ("peer-one",)),),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "allowed_models": [manifest.name],
                "denied_models": [manifest.aliases[0]],
                "max_disk_space": "1GiB",
            }
        ),
    )
    manager, _, _ = _build_model_manager(config, token=None)

    with pytest.raises(NodeConfigError, match="same model as both allowed and denied"):
        _build_worker_supervisor(config, manager)

    manager.shutdown()


def test_max_loaded_models_override_preserves_contribution_policy(monkeypatch, tmp_path):
    configured = NodeConfig(
        schema_version=1,
        max_loaded_models=1,
        models=(NodeModelConfig(tmp_path / "manifest.json", ("peer-one",)),),
        contribution_policy=ContributionPolicyConfig.from_dict(
            {"sharing_enabled": False, "denied_models": ["blocked-model"]}
        ),
    )
    monkeypatch.setattr(NodeConfig, "load", lambda path: configured)
    args = type("Args", (), {"config": tmp_path / "node.json", "max_loaded_models": 2})()

    overridden = _load_node_config(args)

    assert overridden.max_loaded_models == 2
    assert overridden.contribution_policy is configured.contribution_policy


def test_contribution_policy_parses_absolute_and_percentage_vram_limits():
    absolute = ContributionPolicyConfig.from_dict(
        {"sharing_enabled": True, "max_disk_space": "1GiB", "max_vram": "8GiB"}
    )
    assert absolute.max_vram == "8GiB"
    assert absolute.max_vram_bytes == 8 * 1024**3
    assert absolute.max_vram_fraction is None

    percentage = ContributionPolicyConfig.from_dict(
        {"sharing_enabled": True, "max_disk_space": "1GiB", "max_vram": "62.5%"}
    )
    assert percentage.max_vram == "62.5%"
    assert percentage.max_vram_bytes is None
    assert percentage.max_vram_fraction == 0.625


@pytest.mark.parametrize("value", ["0%", "-1%", "100.1%", "NaN%", "Infinity%", "not-a-limit"])
def test_contribution_policy_rejects_invalid_vram_limits(value):
    with pytest.raises(NodeConfigError, match="max_vram"):
        ContributionPolicyConfig.from_dict(
            {
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": value,
            }
        )


def test_accelerator_worker_inherits_tighter_resolved_vram_limit(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    monkeypatch.setattr("drift.cli.run_node.get_device_total_memory", lambda device: 16 * 1024**3)
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "gpu-worker",
                    "model": manifest.name,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "device": "cuda:0",
                    "max_vram": "8GiB",
                }
            ],
            contribution_policy={
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": "75%",
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)
    launch = supervisor.launches[0]

    assert launch.max_vram_bytes == 8 * 1024**3
    assert launch.vram_pool_bytes == 12 * 1024**3
    assert launch.vram_device == "cuda:0"
    assert launch.command[launch.command.index("--max_device_memory") + 1] == str(8 * 1024**3)
    assert supervisor.snapshot("gpu-worker")["max_vram_bytes"] == 8 * 1024**3

    supervisor.shutdown()
    manager.shutdown()


def test_power_monitor_is_scoped_to_each_cuda_workers_device(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    monkeypatch.setattr("drift.cli.run_node.get_device_total_memory", lambda device: 16 * 1024**3)
    monitor_devices = []

    class FakePowerMonitor:
        def __init__(self, device_indices):
            self.device_indices = tuple(device_indices)
            monitor_devices.append(self.device_indices)

        def __call__(self):
            return {0: 100.0, 1: 200.0}[self.device_indices[0]]

    monkeypatch.setattr("drift.cli.run_node.NvidiaPowerMonitor", FakePowerMonitor)
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": f"gpu-worker-{index}",
                    "model": manifest.name,
                    "identity_path": f"worker-{index}.key",
                    "num_blocks": 2,
                    "device": f"cuda:{index}",
                }
                for index in range(2)
            ],
            contribution_policy={
                "sharing_enabled": True,
                "max_disk_space": "1GiB",
                "max_vram": "8GiB",
                "max_power_watts": 250,
            },
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)
    supervisor = _build_worker_supervisor(config, manager)

    snapshots = {snapshot["id"]: snapshot for snapshot in supervisor.snapshots()}
    assert monitor_devices == [(0,), (1,)]
    assert snapshots["gpu-worker-0"]["current_power_watts"] == 100.0
    assert snapshots["gpu-worker-1"]["current_power_watts"] == 200.0
    assert all(snapshot["resource_admitted"] is True for snapshot in snapshots.values())

    supervisor.shutdown()
    manager.shutdown()


def test_accelerator_worker_requires_node_wide_vram_pool(monkeypatch, tmp_path):
    manifest = ModelManifest.load("tests/data/model_manifest_v1_vector.json")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(manifest.canonical_json(), encoding="utf-8")
    monkeypatch.setattr(
        "drift.cli.run_node.make_manifest_loader",
        lambda *args, **kwargs: lambda: ModelRuntime(object(), object()),
    )
    config = NodeConfig.from_dict(
        _config_dict(
            models=[{"manifest": str(manifest_path), "initial_peers": ["peer-one"]}],
            workers=[
                {
                    "id": "gpu-worker",
                    "model": manifest.name,
                    "identity_path": "worker.key",
                    "num_blocks": 2,
                    "device": "cuda:0",
                }
            ],
            contribution_policy={"sharing_enabled": True, "max_disk_space": "1GiB"},
        ),
        base_dir=tmp_path,
    )
    manager, _, _ = _build_model_manager(config, token=None)

    with pytest.raises(NodeConfigError, match="requires a finite contribution max_vram"):
        _build_worker_supervisor(config, manager)

    manager.shutdown()
