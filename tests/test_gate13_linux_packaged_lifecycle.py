import hashlib
import json
import signal
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_linux_packaged_lifecycle as linux_lifecycle  # noqa: E402
import gate13_packaged_lifecycle as controller  # noqa: E402

MODEL_ID = "Qwen3.5 2B"
PROFILE = controller.MODEL_PROFILES[MODEL_ID]
DIGEST = PROFILE["manifest_digest"]


def test_release_metadata_accepts_exact_production_format_and_rejects_changed_claims():
    expected = linux_lifecycle._release_metadata()
    production_payload = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert hashlib.sha256(production_payload).hexdigest() == (
        "6a434cf14100572954452052b8a1e6e8565b2930e3251b1b8327cfdcd7383a25"
    )
    assert linux_lifecycle._validate_release_metadata(production_payload) == expected

    altered = dict(expected, credits_enabled=True)
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle._validate_release_metadata(json.dumps(altered).encode("utf-8"))

    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle._validate_release_metadata(b'{"schema_version":1,"schema_version":1}')


@pytest.mark.parametrize(
    ("field", "spoofed"),
    [
        ("schema_version", True),
        ("schema_version", 1.0),
        ("unsigned", 1),
        ("publisher_signature", 0),
        ("automatic_updates", 0),
        ("install_archive_required", 1),
    ],
)
def test_release_metadata_rejects_bool_int_float_type_spoofs(field, spoofed):
    altered = dict(linux_lifecycle._release_metadata())
    altered[field] = spoofed
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle._validate_release_metadata(json.dumps(altered).encode("utf-8"))


def test_release_evidence_accepts_pretty_json_with_exact_recursive_types():
    expected = {
        "schema_version": 1,
        "claims": {"unsigned": True, "sizes": [4, 10]},
        "status": "public-alpha",
    }
    production_payload = (json.dumps(expected, indent=2, sort_keys=True) + "\n").encode("utf-8")
    parsed = linux_lifecycle._strict_release_json(production_payload)
    assert linux_lifecycle._same_json_value(parsed, expected)

    for spoofed in (
        {**expected, "schema_version": True},
        {**expected, "schema_version": 1.0},
        {**expected, "claims": {"unsigned": 1, "sizes": [4, 10]}},
        {**expected, "claims": {"unsigned": True, "sizes": [4.0, 10]}},
    ):
        assert not linux_lifecycle._same_json_value(spoofed, expected)

    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle._strict_release_json(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle._strict_release_json(b'{"schema_version":NaN}')


def acquisition_record():
    count = PROFILE["selected_artifact_count"]
    size = PROFILE["selected_artifact_bytes"]
    artifacts = [
        {
            "path": f"private/path/{index}",
            "role": "weight",
            "size_bytes": size // count + (1 if index < size % count else 0),
            "sha256": f"{index:064x}",
            "materialization_attempts": 1,
            "resumptions": 0,
            "resumed_from_bytes": [],
            "elapsed_seconds": 1.0,
        }
        for index in range(count)
    ]
    return {
        "schema_version": 1,
        "acquired_at_unix": 1,
        "runtime": {"python": "3.12", "platform": "private-host", "drift": "0.4"},
        "model": {
            "id": MODEL_ID,
            "manifest_digest": "sha256:" + DIGEST,
            "repository": "private-upstream-value",
            "revision": PROFILE["revision_commit"],
            "dtype": "float16",
        },
        "selection": {
            "startup_artifact_paths": [item["path"] for item in artifacts[:-1]],
            "weight_artifact_paths": [artifacts[-1]["path"]],
            "artifact_count": count,
            "artifact_bytes": size,
            "weight_artifact_bytes": size,
        },
        "artifacts": artifacts,
        "transfer": {
            "direct_upstream_transfer": True,
            "mirror_used": False,
            "source_class_verified": True,
            "transport_override_present": False,
            "elapsed_seconds": 2.0,
            "max_resumptions": 3,
            "resumptions": 0,
            "completed": True,
        },
        "storage": {
            "cold_start": True,
            "cache_bytes_before": 0,
            "cache_bytes_after": size,
            "cache_growth_bytes": size,
            "verified": True,
        },
        "privacy": {
            "credentials_retained": False,
            "local_paths_retained": False,
            "response_bodies_retained": False,
            "urls_retained": False,
        },
    }


def policy_snapshot():
    return {
        "schema_version": 1,
        "config_revision": "sha256:" + "a" * 64,
        "policy": {
            "sharing_enabled": False,
            "allowed_models": [],
            "preferred_models": [],
            "denied_models": [],
            "max_disk_space": None,
            "max_vram": None,
            "max_bandwidth_mbps": None,
            "max_power_watts": None,
            "pause_timeout": 10.0,
            "schedule": None,
        },
    }


def running_status(*, vram_bytes=8 * 1024**3):
    worker = {
        "id": "auto-worker",
        "model": MODEL_ID,
        "state": "running",
        "desired_running": True,
        "placement": {
            "automatic": True,
            "block_indices": "0:4",
            "reason": "private-reason",
        },
        "policy": {"admitted": True, "reason": None, "preferred": True},
        "schedule": {"admitted": True, "reason": None, "suspended": False},
        "resources": {
            "admitted": True,
            "reason": None,
            "suspended": False,
            "limits": {
                "disk_bytes": 20 * 1024**3,
                "vram_bytes": vram_bytes,
                "vram_pool_bytes": 16 * 1024**3 if vram_bytes else None,
                "bandwidth_mbps": 100.0,
                "power_watts": 250.0,
            },
            "measurements": {"bandwidth_mbps": 1.0, "power_watts": 10.0},
        },
    }
    return {
        "api_version": 1,
        "status": "running",
        "openai_base_url": "http://127.0.0.1:8080/v1",
        "auto_selection": {
            "selector": "auto",
            "status": "selected",
            "model": MODEL_ID,
            "manifest_digest": "sha256:" + DIGEST,
        },
        "workers": [
            {
                "id": "auto-worker",
                "model": MODEL_ID,
                "state": "running",
                "desired_running": True,
            }
        ],
        "contribution": {
            "schema_version": 3,
            "configured": True,
            "editable": True,
            "policy": policy_snapshot(),
            "workers": [worker],
        },
    }


def test_edge_acquire_command_is_exact_and_record_is_bounded():
    command = linux_lifecycle.edge_acquire_command(
        Path("/package/CommunityAI/node/CommunityAI-Node"),
        Path("/private/model.json"),
        Path("/private/cache"),
    )
    assert command == (
        "/package/CommunityAI/node/CommunityAI-Node",
        "edge-acquire",
        "/private/model.json",
        "--cache_dir",
        "/private/cache",
        "--max_resumptions",
        "3",
        "--require_direct_upstream",
    )

    phase, private_artifacts = linux_lifecycle.validate_acquisition(acquisition_record(), MODEL_ID, DIGEST, 2.5)
    assert phase["phase"] == "verified_acquisition"
    assert phase["direct_upstream_transfer"] is True
    assert phase["mirror_used"] is False
    assert phase["resume_count"] == 0
    assert phase["selected_artifact_count"] == PROFILE["selected_artifact_count"]
    assert phase["selected_artifact_bytes"] == PROFILE["selected_artifact_bytes"]
    assert "repository" not in json.dumps(phase)
    assert "private/path" not in json.dumps(phase)
    assert len(private_artifacts) == PROFILE["selected_artifact_count"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("transfer", "direct_upstream_transfer"), False),
        (("transfer", "mirror_used"), True),
        (("transfer", "source_class_verified"), False),
        (("transfer", "transport_override_present"), True),
        (("transfer", "resumptions"), 4),
        (("transfer", "completed"), False),
        (("storage", "cold_start"), False),
        (("storage", "cache_bytes_before"), 1),
        (("storage", "cache_bytes_after"), PROFILE["selected_artifact_bytes"] - 1),
        (("storage", "cache_growth_bytes"), PROFILE["selected_artifact_bytes"] - 1),
        (("storage", "verified"), False),
    ],
)
def test_acquisition_mismatch_fails_closed(path, value):
    record = acquisition_record()
    record[path[0]][path[1]] = value
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.validate_acquisition(record, MODEL_ID, DIGEST, 1.0)


def test_acquisition_binds_exact_installed_manifest_artifacts():
    manifest = json.loads(
        (ROOT / "public-alpha" / "catalog-v1" / "manifests" / f"{DIGEST}.json").read_text(encoding="utf-8")
    )
    record = acquisition_record()
    record["artifacts"] = [
        {
            "path": artifact["path"],
            "role": artifact["role"],
            "size_bytes": artifact["size"],
            "sha256": artifact["sha256"],
            "materialization_attempts": 1,
            "resumptions": 0,
            "resumed_from_bytes": [],
            "elapsed_seconds": 1.0,
        }
        for artifact in manifest["artifacts"]
    ]
    record["selection"]["startup_artifact_paths"] = [
        artifact["path"] for artifact in manifest["artifacts"] if artifact["role"] != "weight"
    ]
    record["selection"]["weight_artifact_paths"] = [
        artifact["path"] for artifact in manifest["artifacts"] if artifact["role"] == "weight"
    ]
    linux_lifecycle.validate_acquisition(
        record,
        MODEL_ID,
        DIGEST,
        1.0,
        installed_manifest=manifest,
    )

    record["artifacts"][0]["sha256"] = "f" * 64
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.validate_acquisition(
            record,
            MODEL_ID,
            DIGEST,
            1.0,
            installed_manifest=manifest,
        )


def test_acquisition_rejects_duplicate_artifact_paths():
    record = acquisition_record()
    record["artifacts"][1]["path"] = record["artifacts"][0]["path"]
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.validate_acquisition(record, MODEL_ID, DIGEST, 1.0)


def test_policy_put_is_complete_and_optimistic():
    request, expected_policy = linux_lifecycle.build_policy_update(
        policy_snapshot(),
        model_id=MODEL_ID,
        max_disk_space="20GiB",
        max_vram="8GiB",
        max_bandwidth_mbps=100.0,
        max_power_watts=250.0,
        pause_timeout=30.0,
        sharing_enabled=True,
    )
    assert request == {
        "schema_version": 1,
        "expected_config_revision": "sha256:" + "a" * 64,
        "policy": expected_policy,
    }
    assert set(expected_policy) == {
        "sharing_enabled",
        "allowed_models",
        "preferred_models",
        "denied_models",
        "max_disk_space",
        "max_vram",
        "max_bandwidth_mbps",
        "max_power_watts",
        "pause_timeout",
        "schedule",
    }
    assert expected_policy["allowed_models"] == [MODEL_ID]
    assert expected_policy["sharing_enabled"] is True


def test_contribution_facts_come_from_status_and_require_four_limit_classes():
    facts = linux_lifecycle.contribution_phase(running_status(), MODEL_ID, DIGEST, 3.0)
    assert facts == {
        "phase": "bounded_contribution",
        "passed": True,
        "duration_seconds": 3.0,
        "opt_in": True,
        "automatic_placement": True,
        "manifest_digest": DIGEST,
        "model_id": MODEL_ID,
        "worker_count": 1,
        "block_start": 0,
        "block_end": 4,
        "block_count": 4,
        "resource_limit_count": 4,
        "limits_enforced": True,
        "accepted_request_count": 0,
        "source_imports_used": False,
    }

    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.contribution_phase(running_status(vram_bytes=None), MODEL_ID, DIGEST, 3.0)


def test_revoked_inference_keys_do_not_change_retained_secret_count(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_lifecycle, "_credential_count", lambda: 1)
    store_path = tmp_path / "api-keys.json"

    def key_record(index, *, revoked_at):
        return {
            "id": f"key_{index:016x}",
            "label": f"Gate 13 key {index}",
            "secret_hash": f"{index + 1:064x}",
            "created_at": index,
            "revoked_at": revoked_at,
        }

    active = [
        key_record(1, revoked_at=None),
        key_record(2, revoked_at=None),
    ]
    revoked = [key_record(3, revoked_at=4)]
    store_path.write_text(
        json.dumps({"schema_version": 1, "keys": active + revoked}),
        encoding="utf-8",
    )
    assert linux_lifecycle._persistent_secret_material_count(tmp_path) == 3

    revoked.extend(
        [
            key_record(4, revoked_at=5),
            key_record(5, revoked_at=6),
        ]
    )
    store_path.write_text(
        json.dumps({"schema_version": 1, "keys": active + revoked}),
        encoding="utf-8",
    )
    assert linux_lifecycle._persistent_secret_material_count(tmp_path) == 3


def test_exact_worker_pid_is_bound_to_node_cgroup_and_pause_proves_absence():
    running = {
        "workers": [
            {
                "id": "automatic",
                "model": MODEL_ID,
                "state": "running",
                "desired_running": True,
                "operator_paused": False,
                "automatic": True,
                "pid": 4200,
            }
        ]
    }
    assert linux_lifecycle.exact_running_worker_pid(running, MODEL_ID, frozenset({4100, 4200})) == 4200
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.exact_running_worker_pid(running, MODEL_ID, frozenset({4100}))
    extra = {
        "id": "unexpected",
        "model": MODEL_ID,
        "state": "running",
        "desired_running": True,
        "operator_paused": False,
        "automatic": False,
        "pid": 4300,
    }
    running["workers"].append(extra)
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.exact_running_worker_pid(running, MODEL_ID, frozenset({4100, 4200, 4300}))
    running["workers"].pop()

    status = running_status()
    status["workers"][0].update(state="paused", desired_running=False)
    status["contribution"]["workers"][0].update(state="paused", desired_running=False)
    paused = {
        "workers": [
            {
                "id": "automatic",
                "model": MODEL_ID,
                "state": "paused",
                "desired_running": False,
                "operator_paused": True,
                "automatic": True,
                "pid": None,
            }
        ]
    }
    phase = linux_lifecycle.pause_phase(
        status,
        paused,
        original_worker_pid=4200,
        node_process_ids=frozenset({4100}),
        duration=2.0,
    )
    assert phase["worker_count_after"] == 0
    assert phase["process_count_after"] == 0

    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.pause_phase(
            status,
            paused,
            original_worker_pid=4200,
            node_process_ids=frozenset({4100, 4200}),
            duration=2.0,
        )

    paused["workers"].append(extra)
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.pause_phase(
            status,
            paused,
            original_worker_pid=4200,
            node_process_ids=frozenset({4100, 4300}),
            duration=2.0,
        )
    paused["workers"].pop()

    status["workers"][0]["state"] = "stopping"
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.pause_phase(
            status,
            paused,
            original_worker_pid=4200,
            node_process_ids=frozenset({4100}),
            duration=2.0,
        )


def test_packaged_worker_self_test_contract_is_exact():
    record = {
        "schema_version": 1,
        "application": "CommunityAI-Worker",
        "entrypoint": "server",
        "server_class": "Server",
        "model_loading_performed": False,
        "network_join_performed": False,
        "throughput_mode": "dry_run",
        "training_rpcs_enabled": False,
        "process_lifetime_guard_armed": True,
        "frozen": True,
    }
    linux_lifecycle.validate_worker_self_test(record)
    record["network_join_performed"] = True
    with pytest.raises(linux_lifecycle.LifecycleRunError):
        linux_lifecycle.validate_worker_self_test(record)


def test_process_groups_are_created_owned_and_proved_empty():
    calls = []

    class FakeProcess:
        pid = 4100

    def popen(command, **kwargs):
        calls.append(("start", tuple(command), kwargs))
        return FakeProcess()

    existence = iter([True, True, False])

    def group_exists(pgid):
        calls.append(("exists", pgid))
        return next(existence)

    def killpg(pgid, sig):
        calls.append(("kill", pgid, sig))

    groups = linux_lifecycle.ProcessGroupOwner(
        process_factory=popen,
        kill_group=killpg,
        group_exists=group_exists,
        sleeper=lambda _seconds: None,
        clock=iter([0.0, 0.1, 0.2, 0.3]).__next__,
    )
    owned = groups.start(("/package/CommunityAI", "--no-manage-node"), cwd=Path("/private"))
    forced = groups.stop(owned, grace_seconds=0.15)

    assert calls[0][2]["start_new_session"] is True
    assert calls[0][2]["stdin"] is linux_lifecycle.subprocess.DEVNULL
    assert calls[0][2]["stdout"] is linux_lifecycle.subprocess.DEVNULL
    assert calls[0][2]["stderr"] is linux_lifecycle.subprocess.DEVNULL
    assert ("kill", 4100, signal.SIGTERM) in calls
    assert forced is False
    assert groups.owned == []


def test_systemd_capture_timeout_stops_owned_cgroup(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_lifecycle, "_trusted_root_binary", lambda path: path)
    cgroup_root = tmp_path / "cgroup"
    cgroup_root.mkdir()
    (cgroup_root / "cgroup.controllers").write_text("cpu memory", encoding="ascii")
    events = []

    class TimeoutProcess:
        returncode = None

        def communicate(self, *, timeout):
            events.append(("communicate", timeout))
            raise linux_lifecycle.subprocess.TimeoutExpired("systemd-run", timeout)

        def kill(self):
            events.append(("wrapper_kill",))

    owner = linux_lifecycle.SystemdUnitOwner(
        "linux-timeout",
        process_factory=lambda *_args, **kwargs: (events.append(("spawn", kwargs)) or TimeoutProcess()),
        run_command=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=b""),
        sudo=Path("/usr/bin/sudo"),
        systemd_run=Path("/usr/bin/systemd-run"),
        systemctl=Path("/usr/bin/systemctl"),
        cgroup_root=cgroup_root,
        uid=1001,
    )
    owned = linux_lifecycle.OwnedUnit(
        unit="communityai-gate13-linux-timeout-1.service",
        control_group="/system.slice/communityai-gate13-linux-timeout-1.service",
        main_pid=4100,
    )

    def wait_for_identity(_unit, _wrapper):
        owner.owned.append(owned)
        return owned

    def stop(target, *, grace_seconds):
        events.append(("stop", target, grace_seconds))
        owner.owned.remove(target)
        return True

    monkeypatch.setattr(owner, "_wait_for_identity", wait_for_identity)
    monkeypatch.setattr(owner, "stop", stop)

    with pytest.raises(
        linux_lifecycle.LifecycleRunError,
        match="^contained packaged command failed$",
    ):
        owner.run_capture(
            ("/package/CommunityAI-Node", "edge-acquire"),
            cwd=tmp_path,
            timeout=1.0,
        )

    spawn = next(event for event in events if event[0] == "spawn")
    assert spawn[1]["start_new_session"] is True
    assert ("communicate", 1.0) in events
    assert ("stop", owned, 1.0) in events
    assert ("wrapper_kill",) not in events
    assert owner.owned == []


def test_system_systemd_unit_is_authoritative_and_graceful(monkeypatch, tmp_path):
    monkeypatch.setattr(linux_lifecycle, "_trusted_root_binary", lambda path: path)
    cgroup_root = tmp_path / "cgroup"
    cgroup = cgroup_root / "gate"
    cgroup.mkdir(parents=True)
    (cgroup_root / "cgroup.controllers").write_text("cpu memory", encoding="ascii")
    (cgroup / "cgroup.procs").write_text("4100\n", encoding="ascii")
    calls = []

    def run(command, **kwargs):
        calls.append(tuple(command))
        if "--property=ControlGroup" in command:
            return SimpleNamespace(returncode=0, stdout=b"/gate\n")
        if "--property=MainPID" in command:
            return SimpleNamespace(returncode=0, stdout=b"4100\n")
        if "--property=ActiveState" in command:
            return SimpleNamespace(returncode=0, stdout=b"active\n")
        if "--kill-whom=main" in command:
            (cgroup / "cgroup.procs").write_text("", encoding="ascii")
        return SimpleNamespace(returncode=0, stdout=b"")

    owner = linux_lifecycle.SystemdUnitOwner(
        "linux-a",
        run_command=run,
        sudo=Path("/usr/bin/sudo"),
        systemd_run=Path("/usr/bin/systemd-run"),
        systemctl=Path("/usr/bin/systemctl"),
        cgroup_root=cgroup_root,
        uid=1001,
        sleeper=lambda _seconds: None,
        clock=iter([0.0, 0.1, 0.2, 0.3, 0.4]).__next__,
    )
    owned = owner.start(("/package/CommunityAI",), cwd=Path("/private"))
    start = calls[0]
    assert start[:3] == ("/usr/bin/sudo", "-n", "/usr/bin/systemd-run")
    assert "--system" in start
    assert "--user" not in start
    assert "--scope" not in start
    assert "--uid=1001" in start
    assert "--property=KillMode=control-group" in start
    assert "--property=LimitCORE=0" in start
    assert owned.main_pid == 4100

    owner.stop_gracefully(owned)
    assert any("--kill-whom=main" in command and "--signal=TERM" in command for command in calls)
    assert not any("--kill-whom=all" in command and "--signal=KILL" in command for command in calls)
    assert owner.owned == []


def test_main_failure_is_generic_and_does_not_echo_config(monkeypatch, capsys):
    marker = "/private/path/must-not-escape"
    monkeypatch.setattr(linux_lifecycle, "_disable_core_dumps", lambda: None)
    monkeypatch.setattr(
        linux_lifecycle,
        "run_from_config",
        lambda _path: (_ for _ in ()).throw(linux_lifecycle.LifecycleRunError(marker)),
    )

    assert linux_lifecycle.main(["--config", marker]) == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "failure_code": "linux_lifecycle_failed",
        "result": "failed",
        "schema_version": 1,
    }
    assert marker not in captured.out
    assert captured.err == ""
