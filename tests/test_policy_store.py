import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import drift.node.policy_store as policy_store_module
from drift.node.config import NodeConfig, NodeConfigError
from drift.node.config_lock import NodeConfigWriteLockError, node_config_write_lock
from drift.node.policy_store import (
    ContributionPolicyConflictError,
    ContributionPolicyPersistenceError,
    ContributionPolicyStore,
)
from drift.node.worker_supervisor import (
    WorkerLaunch,
    WorkerReconfigurationBusyError,
    WorkerSupervisor,
    WorkerSupervisorSettings,
)


def _config_document(*, sharing_enabled=False):
    return {
        "schema_version": 1,
        "max_loaded_models": 2,
        "discovery_update_period": 45,
        "models": [
            {
                "manifest": "manifest.json",
                "initial_peers": ["/ip4/127.0.0.1/tcp/31337/p2p/example"],
            }
        ],
        "contribution_policy": {
            "sharing_enabled": sharing_enabled,
            "allowed_models": [],
            "preferred_models": [],
            "denied_models": [],
            "max_disk_space": "20GiB",
            "max_vram": None,
            "max_bandwidth_mbps": None,
            "max_power_watts": None,
            "pause_timeout": 10,
            "schedule": None,
        },
        "workers": [
            {
                "id": "worker",
                "model": "model",
                "identity_path": "identity.key",
                "num_blocks": 1,
                "enabled": False,
            }
        ],
    }


def _write_config(path: Path, *, sharing_enabled=False):
    document = _config_document(sharing_enabled=sharing_enabled)
    path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    return document


def _settings(config):
    policy = config.contribution_policy
    admitted = policy.sharing_enabled
    return WorkerSupervisorSettings(
        launches=(
            WorkerLaunch(
                "worker",
                "model",
                (sys.executable, "-c", "import time; time.sleep(30)"),
                auto_restart=False,
                policy_admitted=admitted,
                policy_reason=None if admitted else "sharing is disabled by contribution policy",
                max_disk_bytes=policy.max_disk_bytes,
                max_bandwidth_mbps=policy.max_bandwidth_mbps,
                max_power_watts=policy.max_power_watts,
            ),
        ),
        stop_timeout=policy.pause_timeout,
        schedule_allowed=None if policy.schedule is None else policy.schedule.allows,
    )


def _store(tmp_path: Path, *, sharing_enabled=False):
    path = tmp_path / "node-config.json"
    _write_config(path, sharing_enabled=sharing_enabled)
    initial = _settings(NodeConfig.load(path))
    supervisor = WorkerSupervisor(
        initial.launches,
        stop_timeout=initial.stop_timeout,
        schedule_allowed=initial.schedule_allowed,
    )
    return path, supervisor, ContributionPolicyStore(path, supervisor, _settings)


def _complete_policy(**overrides):
    policy = {
        "sharing_enabled": True,
        "allowed_models": ["model"],
        "preferred_models": ["model"],
        "denied_models": [],
        "max_disk_space": "12GiB",
        "max_vram": "50%",
        "max_bandwidth_mbps": 25.0,
        "max_power_watts": 150.0,
        "pause_timeout": 7.0,
        "schedule": {
            "timezone": "UTC",
            "windows": [
                {
                    "days": ["mon", "wed"],
                    "start": "22:00",
                    "end": "06:00",
                }
            ],
        },
    }
    policy.update(overrides)
    return policy


def test_policy_store_round_trips_every_field_and_preserves_unrelated_config(tmp_path):
    path, supervisor, store = _store(tmp_path)
    before_mode = stat.S_IMODE(path.stat().st_mode)
    initial = store.snapshot()

    result = store.update(_complete_policy(), expected_revision=initial["config_revision"])

    assert result["policy"] == _complete_policy()
    assert result["config_revision"] != initial["config_revision"]
    persisted = json.loads(path.read_text(encoding="utf-8"))
    assert persisted["max_loaded_models"] == 2
    assert persisted["discovery_update_period"] == 45
    assert persisted["models"] == _config_document()["models"]
    assert persisted["workers"] == _config_document()["workers"]
    assert persisted["contribution_policy"] == _complete_policy()
    assert stat.S_IMODE(path.stat().st_mode) == before_mode
    launch = supervisor.launches[0]
    assert launch.policy_admitted is True
    assert launch.max_disk_bytes == 12 * 1024**3
    assert launch.max_bandwidth_mbps == 25.0

    cleared = _complete_policy(
        sharing_enabled=False,
        allowed_models=[],
        preferred_models=[],
        max_disk_space=None,
        max_vram=None,
        max_bandwidth_mbps=None,
        max_power_watts=None,
        schedule=None,
    )
    cleared_result = store.update(cleared, expected_revision=result["config_revision"])
    assert cleared_result["policy"] == cleared
    assert json.loads(path.read_text(encoding="utf-8"))["contribution_policy"] == cleared
    assert supervisor.launches[0].policy_admitted is False
    assert supervisor.launches[0].max_disk_bytes is None
    assert not list(tmp_path.glob(".node-config.json.*.tmp"))
    supervisor.shutdown()


def test_policy_store_rejects_a_config_changed_during_runtime_startup(tmp_path):
    path = tmp_path / "node-config.json"
    document = _write_config(path)
    expected = NodeConfig.load(path)
    initial = _settings(expected)
    supervisor = WorkerSupervisor(initial.launches)
    document["contribution_policy"]["pause_timeout"] = 11
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(NodeConfigError, match="changed.*starting"):
        ContributionPolicyStore(path, supervisor, _settings, expected_config=expected)
    supervisor.shutdown()


def test_policy_store_startup_comparison_includes_max_loaded_models(tmp_path):
    path = tmp_path / "node-config.json"
    document = _write_config(path)
    expected = NodeConfig.load(path)
    initial = _settings(expected)
    supervisor = WorkerSupervisor(initial.launches)
    document["max_loaded_models"] = 3
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(NodeConfigError, match="changed.*starting"):
        ContributionPolicyStore(path, supervisor, _settings, expected_config=expected)
    supervisor.shutdown()


def test_policy_store_serializes_every_repository_config_writer(tmp_path):
    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    before = path.read_bytes()

    with node_config_write_lock(path):
        with pytest.raises(ContributionPolicyConflictError, match="writer.*active"):
            store.update(_complete_policy(), expected_revision=initial["config_revision"])

    assert path.read_bytes() == before
    assert supervisor.launches[0].policy_admitted is False
    supervisor.shutdown()


def test_policy_store_rejects_invalid_stale_and_externally_changed_updates(tmp_path):
    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    original = path.read_bytes()

    with pytest.raises(NodeConfigError, match="max_disk_space"):
        store.update(
            _complete_policy(max_disk_space=None),
            expected_revision=initial["config_revision"],
        )
    assert path.read_bytes() == original
    assert supervisor.launches[0].policy_admitted is False

    with pytest.raises(ContributionPolicyConflictError, match="refresh"):
        store.update(_complete_policy(), expected_revision="sha256:" + "0" * 64)
    assert path.read_bytes() == original

    path.write_bytes(original + b"\n")
    with pytest.raises(ContributionPolicyConflictError, match="refresh"):
        store.update(_complete_policy(), expected_revision=initial["config_revision"])
    assert supervisor.launches[0].policy_admitted is False
    supervisor.shutdown()


def test_policy_store_write_failure_leaves_disk_and_active_policy_unchanged(tmp_path, monkeypatch):
    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    original = path.read_bytes()
    monkeypatch.setattr(
        policy_store_module,
        "_exchange_paths",
        lambda source, destination: (_ for _ in ()).throw(PermissionError("locked")),
    )

    with pytest.raises(ContributionPolicyPersistenceError, match="persistence failed"):
        store.update(_complete_policy(), expected_revision=initial["config_revision"])

    assert path.read_bytes() == original
    assert supervisor.launches[0].policy_admitted is False
    assert not list(tmp_path.glob(".node-config.json.*.tmp"))
    supervisor.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW partial-failure semantics are Windows-specific")
def test_policy_store_restores_original_after_replace_file_error_1177(tmp_path, monkeypatch):
    import ctypes

    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    original = path.read_bytes()

    class FailingReplaceFile:
        argtypes = None
        restype = None

        def __call__(self, target, replacement, backup, flags, exclude, reserved):
            os.replace(target, backup)
            ctypes.set_last_error(1177)
            return 0

    class FakeKernel32:
        ReplaceFileW = FailingReplaceFile()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel32())
    with pytest.raises(ContributionPolicyPersistenceError, match="persistence failed"):
        store.update(_complete_policy(), expected_revision=initial["config_revision"])

    assert path.read_bytes() == original
    assert supervisor.launches[0].policy_admitted is False
    assert not list(tmp_path.glob(".node-config.json.*.tmp"))
    assert not list(tmp_path.glob(".node-config.json.*.previous"))
    supervisor.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="ReplaceFileW recovery semantics are Windows-specific")
def test_policy_store_preserves_recovery_backup_when_error_1177_restore_fails(tmp_path, monkeypatch):
    import ctypes

    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    original = path.read_bytes()
    real_replace = os.replace

    class FailingReplaceFile:
        argtypes = None
        restype = None

        def __call__(self, target, replacement, backup, flags, exclude, reserved):
            real_replace(target, backup)
            ctypes.set_last_error(1177)
            return 0

    class FakeKernel32:
        ReplaceFileW = FailingReplaceFile()

    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: FakeKernel32())
    monkeypatch.setattr(os, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("locked")))
    with pytest.raises(ContributionPolicyPersistenceError, match="persistence failed"):
        store.update(_complete_policy(), expected_revision=initial["config_revision"])

    backups = list(tmp_path.glob(".node-config.json.*.previous"))
    assert not path.exists()
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    assert supervisor.launches[0].policy_admitted is False
    assert not list(tmp_path.glob(".node-config.json.*.tmp"))
    supervisor.shutdown()


def test_policy_store_detects_and_restores_a_write_at_the_exchange_boundary(tmp_path, monkeypatch):
    path, supervisor, store = _store(tmp_path)
    initial = store.snapshot()
    external = path.read_bytes() + b"\n"
    real_exchange = policy_store_module._exchange_paths
    exchanged = False

    def exchange_after_external_write(replacement, target):
        nonlocal exchanged
        if not exchanged:
            exchanged = True
            target.write_bytes(external)
        return real_exchange(replacement, target)

    monkeypatch.setattr(policy_store_module, "_exchange_paths", exchange_after_external_write)
    with pytest.raises(ContributionPolicyConflictError, match="refresh"):
        store.update(_complete_policy(), expected_revision=initial["config_revision"])

    assert path.read_bytes() == external
    assert supervisor.launches[0].policy_admitted is False
    assert not list(tmp_path.glob(".node-config.json.*.tmp"))
    assert not list(tmp_path.glob(".node-config.json.*.previous"))
    supervisor.shutdown()


def test_policy_store_requires_workers_to_be_paused_before_commit(tmp_path):
    path, supervisor, store = _store(tmp_path, sharing_enabled=True)
    supervisor.start_service()
    supervisor.start_worker("worker")
    before = path.read_bytes()
    revision = store.snapshot()["config_revision"]

    with pytest.raises(WorkerReconfigurationBusyError, match="pause all"):
        store.update(_complete_policy(max_disk_space="10GiB"), expected_revision=revision)

    assert path.read_bytes() == before
    assert supervisor.snapshot("worker")["desired_running"] is True
    supervisor.pause_worker("worker")
    supervisor.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows short-path aliases are platform-specific")
def test_policy_store_accepts_a_windows_short_path_alias(tmp_path):
    import ctypes

    target = tmp_path / "node-config-with-a-long-name.json"
    _write_config(target)
    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(target), buffer, len(buffer))
    alias = Path(buffer.value)
    if not length or os.path.normcase(os.fspath(alias)) == os.path.normcase(os.fspath(target)):
        pytest.skip("8.3 short-path aliases are unavailable on this volume")

    initial = _settings(NodeConfig.load(target))
    supervisor = WorkerSupervisor(initial.launches)
    store = ContributionPolicyStore(alias, supervisor, _settings)
    before = store.snapshot()

    result = store.update(_complete_policy(), expected_revision=before["config_revision"])

    assert store.path == target.resolve()
    assert result["policy"] == _complete_policy()
    assert NodeConfig.load(target).contribution_policy.to_dict() == _complete_policy()
    updated_buffer = ctypes.create_unicode_buffer(32768)
    assert ctypes.windll.kernel32.GetShortPathNameW(str(target), updated_buffer, len(updated_buffer))
    with node_config_write_lock(target):
        with pytest.raises(NodeConfigWriteLockError, match="writer.*active"):
            with node_config_write_lock(Path(updated_buffer.value)):
                pass
    supervisor.shutdown()


@pytest.mark.skipif(os.name != "nt", reason="Windows junctions are platform-specific")
def test_policy_store_refuses_a_windows_junction_ancestor(tmp_path):
    real = tmp_path / "real-junction-target"
    real.mkdir()
    target = real / "node-config.json"
    _write_config(target)
    junction = tmp_path / "linked-junction"
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", os.fspath(junction), os.fspath(real)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        pytest.skip("directory junctions are unavailable on this host")

    initial = _settings(NodeConfig.load(target))
    supervisor = WorkerSupervisor(initial.launches)
    with pytest.raises(NodeConfigError, match="links|junctions"):
        ContributionPolicyStore(junction / target.name, supervisor, _settings)
    supervisor.shutdown()


def test_policy_store_refuses_linked_config_paths(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    target = real / "node-config.json"
    _write_config(target)
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(real, target_is_directory=True)
    except (NotImplementedError, OSError):
        pytest.skip("directory symlinks are unavailable on this host")

    initial = _settings(NodeConfig.load(target))
    supervisor = WorkerSupervisor(initial.launches)
    with pytest.raises(NodeConfigError, match="links|linked"):
        ContributionPolicyStore(linked_parent / target.name, supervisor, _settings)
    supervisor.shutdown()
