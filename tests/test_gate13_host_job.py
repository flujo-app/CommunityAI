import hashlib
import io
import json
import subprocess
import sys
import threading
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_host_job as host_job  # noqa: E402


def sha256(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    def make(platform="linux"):
        root = tmp_path / platform
        root.mkdir()
        adapter = root / "gate13_host_job.py"
        adapter.write_bytes(host_job.ADAPTER_PATH.read_bytes())
        entrypoint = root / (
            "gate13_windows_packaged_lifecycle.ps1" if platform == "windows" else "gate13_linux_packaged_lifecycle.py"
        )
        entrypoint.write_text("# bound lifecycle\n", encoding="utf-8")
        lifecycle_config = root / ("gate13-windows-run.json" if platform == "windows" else "gate13-linux-run.json")
        lifecycle_config.write_text('{"bound":true}\n', encoding="utf-8")
        python = Path(sys.executable).resolve()

        monkeypatch.setitem(host_job.HOST_ROOTS, platform, root)
        monkeypatch.setitem(host_job.HOST_PYTHON, platform, python)
        monkeypatch.setattr(host_job, "ADAPTER_PATH", adapter.resolve())

        run_id = "gate13-test-a"
        raw = {
            "schema_version": 1,
            "run_id": run_id,
            "lifecycle_run_id": f"{run_id}-{platform}",
            "platform": platform,
            "attempt_ordinal": 1,
            "source_commit": "a" * 40,
            "job_name": f"communityai-gate13-{run_id}-{platform}",
            "host_user": "gate13",
            "adapter_path": str(adapter.resolve()),
            "adapter_sha256": sha256(adapter),
            "config_path": str((root / "host-job.json").resolve()),
            "entrypoint_path": str(entrypoint.resolve()),
            "entrypoint_sha256": sha256(entrypoint),
            "lifecycle_config_path": str(lifecycle_config.resolve()),
            "lifecycle_config_sha256": sha256(lifecycle_config),
            "evidence_path": str((root / "evidence.json").resolve()),
            "stderr_path": str((root / "stderr.log").resolve()),
            "status_path": str((root / "status.json").resolve()),
            "terminal_path": str((root / "terminal.json").resolve()),
            "working_directory": str(root.resolve()),
            "python_executable": str(python),
            "max_run_seconds": 3600,
        }
        path = root / "host-job.json"
        path.write_text(json.dumps(raw), encoding="utf-8")
        return path, raw

    return make


def test_load_config_binds_exact_files_paths_and_single_attempt(config_factory):
    path, raw = config_factory()

    config = host_job.load_config(path)

    assert config.attempt_ordinal == 1
    assert config.job_name == "communityai-gate13-gate13-test-a-linux"
    assert config.adapter_sha256 == raw["adapter_sha256"]
    assert config.entrypoint_sha256 == raw["entrypoint_sha256"]
    assert config.lifecycle_config_sha256 == raw["lifecycle_config_sha256"]
    assert config.host_user == "gate13"


def test_windows_environment_keeps_standard_user_runtime_and_drops_secrets(config_factory, monkeypatch):
    path, _raw = config_factory("windows")
    config = host_job.load_config(path)
    expected = {
        "APPDATA": r"C:\\Users\\M\\AppData\\Roaming",
        "LOCALAPPDATA": r"C:\\Users\\M\\AppData\\Local",
        "PATH": r"C:\\Windows\\System32",
        "USERPROFILE": r"C:\\Users\\M",
    }
    for key, value in expected.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("GH_TOKEN", "must-not-cross-the-host-boundary")
    monkeypatch.setenv("COMMUNITYAI_CONTROL_TOKEN", "must-not-cross-the-host-boundary")

    environment = host_job._bounded_environment(config)

    assert all(environment[key] == value for key, value in expected.items())
    assert set(environment).issubset(set(host_job.WINDOWS_RUNTIME_ENVIRONMENT))
    assert "GH_TOKEN" not in environment
    assert "COMMUNITYAI_CONTROL_TOKEN" not in environment


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("attempt_ordinal", 2),
        ("job_name", "communityai-gate13-foreign-linux"),
        ("lifecycle_run_id", "foreign-linux"),
        ("max_run_seconds", 86_400),
        ("host_user", "root"),
    ],
)
def test_changed_execution_binding_fails_closed(config_factory, field, value):
    path, raw = config_factory()
    raw[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(host_job.HostJobError):
        host_job.load_config(path)


def test_path_escape_and_entrypoint_tampering_fail_closed(config_factory, tmp_path):
    path, raw = config_factory()
    raw["evidence_path"] = str((tmp_path / "escaped.json").resolve())
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(host_job.HostJobError, match="escapes"):
        host_job.load_config(path)

    raw["evidence_path"] = str((path.parent / "evidence.json").resolve())
    Path(raw["entrypoint_path"]).write_text("# changed\n", encoding="utf-8")
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(host_job.HostJobError, match="entrypoint digest changed"):
        host_job.load_config(path)


def test_lifecycle_config_tampering_fails_closed(config_factory):
    path, raw = config_factory()
    Path(raw["lifecycle_config_path"]).write_text('{"changed":true}\n', encoding="utf-8")

    with pytest.raises(host_job.HostJobError, match="lifecycle config digest changed"):
        host_job.load_config(path)


def test_windows_lifecycle_config_must_be_beside_entrypoint(config_factory):
    path, raw = config_factory("windows")
    nested = path.parent / "nested"
    nested.mkdir()
    nominated = nested / "gate13-windows-run.json"
    nominated.write_text('{"bound":true}\n', encoding="utf-8")
    raw["lifecycle_config_path"] = str(nominated.resolve())
    raw["lifecycle_config_sha256"] = sha256(nominated)
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(host_job.HostJobError, match="not beside"):
        host_job.load_config(path)


def test_windows_task_is_bounded_ordinary_user_single_instance(config_factory):
    path, _raw = config_factory("windows")
    config = host_job.load_config(path)

    script = host_job._windows_register_script(config)

    assert "New-ScheduledTaskPrincipal -UserId $currentUser" in script
    assert "-LogonType S4U -RunLevel Limited" in script
    assert "$identity.IsSystem" in script
    assert "'SYSTEM'" not in script
    assert "-MultipleInstances IgnoreNew" in script
    assert "-ExecutionTimeLimit" in script
    assert str(config.adapter_path) in script
    assert str(config.config_path) in script
    assert "password" not in script.lower()
    assert "token" not in script.lower()

    snapshot = host_job._windows_snapshot_script(config)
    assert "MultipleInstances -eq 'IgnoreNew'" in snapshot
    assert "ExecutionTimeLimit -eq $expectedLimit" in snapshot
    assert "LogonType -eq 'S4U'" in snapshot
    assert "$taskSid -eq $identity.User.Value" in snapshot
    assert "NTAccount]::new([string]$task.Principal.UserId)" in snapshot
    assert "RunLevel -eq 'Limited'" in snapshot


@pytest.mark.skipif(sys.platform != "win32", reason="requires Windows PowerShell parser")
def test_windows_task_scripts_parse_natively(config_factory):
    import base64

    path, _raw = config_factory("windows")
    config = host_job.load_config(path)
    for source in (
        host_job._windows_register_script(config),
        host_job._windows_snapshot_script(config),
    ):
        encoded = base64.b64encode(source.encode("utf-16le")).decode("ascii")
        probe = (
            "$source=[Text.Encoding]::Unicode.GetString("
            f"[Convert]::FromBase64String('{encoded}'));"
            "$tokens=$null;$errors=$null;"
            "[Management.Automation.Language.Parser]::ParseInput("
            "$source,[ref]$tokens,[ref]$errors)|Out-Null;"
            "if($errors.Count -ne 0){exit 2}"
        )
        result = subprocess.run(
            host_job._powershell_argv(probe),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stderr


def test_linux_unit_is_bounded_non_root_and_non_restarting(config_factory):
    path, _raw = config_factory()
    config = host_job.load_config(path)

    argv = host_job._linux_start_argv(config)

    assert argv[:4] == ["sudo", "-n", "/usr/bin/systemd-run", "--quiet"]
    assert f"--unit" in argv
    assert config.job_name in argv
    assert "--property=User=gate13" in argv
    assert "--property=Restart=no" in argv
    assert "--property=KillMode=control-group" in argv
    assert "--property=NoNewPrivileges=no" in argv
    assert "--property=TimeoutStartSec=120" in argv
    assert f"--property=RuntimeMaxSec={config.max_run_seconds + 2 * host_job.SUPERVISOR_GRACE_SECONDS}" in argv
    assert "--wait" not in argv
    assert host_job._entrypoint_argv(config)[-2:] == [
        "--config",
        str(config.lifecycle_config_path),
    ]


def test_windows_automated_python_replay_uses_the_bound_python(config_factory):
    path, _raw = config_factory("windows")
    config = host_job.load_config(path)
    automated = replace(config, entrypoint_path=config.entrypoint_path.with_suffix(".py"))

    assert host_job._entrypoint_argv(automated) == [
        str(config.python_executable),
        str(automated.entrypoint_path),
        "--config",
        str(config.lifecycle_config_path),
    ]


def test_bounded_copy_caps_private_diagnostics(tmp_path):
    destination = tmp_path / "stderr.log"
    overflow = threading.Event()
    errors = []

    host_job._bounded_copy(
        io.BytesIO(b"x" * 257),
        destination,
        256,
        overflow,
        errors,
    )

    assert overflow.is_set()
    assert errors == []
    assert destination.stat().st_size == 256


def test_real_entrypoint_output_is_capped(config_factory):
    path, raw = config_factory()
    entrypoint = Path(raw["entrypoint_path"])
    entrypoint.write_text(
        "import sys\nsys.stdout.buffer.write(b'x' * 1048577)\n",
        encoding="utf-8",
    )
    raw["entrypoint_sha256"] = sha256(entrypoint)
    path.write_text(json.dumps(raw), encoding="utf-8")
    config = host_job.load_config(path)

    assert host_job._run_entrypoint(config) == 126
    assert config.evidence_path.stat().st_size == host_job.MAX_EVIDENCE_BYTES
    assert config.stderr_path.stat().st_size == 0


def test_linux_tree_shutdown_escalates_to_process_group(config_factory, monkeypatch):
    path, _raw = config_factory()
    config = host_job.load_config(path)
    signals = []

    class Process:
        pid = 4321

        def __init__(self):
            self.waits = 0

        def wait(self, timeout):
            self.waits += 1
            if self.waits == 1:
                raise subprocess.TimeoutExpired("entrypoint", timeout)
            return -9

    monkeypatch.setattr(
        host_job.os,
        "killpg",
        lambda pid, requested: signals.append((pid, requested)),
        raising=False,
    )
    host_job._stop_process_tree(config, Process())

    assert signals == [
        (4321, host_job.POSIX_SIGTERM),
        (4321, host_job.POSIX_SIGKILL),
    ]


def test_execute_persists_status_validates_evidence_and_never_relaunches(config_factory, monkeypatch):
    path, _raw = config_factory()
    calls = []

    monkeypatch.setattr(host_job.lifecycle, "load_lifecycle_json", lambda _payload: {})
    monkeypatch.setattr(
        host_job.lifecycle,
        "validate_lifecycle_document",
        lambda _document: {
            "run_id": "gate13-test-a-linux",
            "platform": "linux",
            "source_commit": "a" * 40,
        },
    )

    def runner(config):
        calls.append(config.job_name)
        config.evidence_path.write_text('{"canonical":true}', encoding="utf-8")
        config.stderr_path.write_bytes(b"")
        return 0

    terminal = host_job.execute(path, clock=lambda: 2_000_000_000, entrypoint_runner=runner)
    repeated = host_job.execute(path, clock=lambda: 2_000_000_001, entrypoint_runner=runner)

    assert terminal["result"] == "passed"
    assert terminal["failure_code"] is None
    assert terminal["evidence_digest"].startswith("sha256:")
    assert repeated == terminal
    assert calls == ["communityai-gate13-gate13-test-a-linux"]
    status = json.loads((path.parent / "status.json").read_text(encoding="utf-8"))
    assert status["state"] == "running"
    assert status["attempt_ordinal"] == 1


def test_started_attempt_without_terminal_is_never_relaunched(config_factory):
    path, _raw = config_factory()
    config = host_job.load_config(path)
    host_job._atomic_json(
        config.status_path,
        {
            "schema_version": 1,
            "run_id": config.run_id,
            "platform": config.platform,
            "attempt_ordinal": 1,
            "state": "running",
            "started_at_unix": 2_000_000_000,
        },
        exclusive=True,
    )
    called = False

    def runner(_config):
        nonlocal called
        called = True
        return 0

    with pytest.raises(host_job.HostJobError, match="already started"):
        host_job.execute(path, entrypoint_runner=runner)
    assert called is False


def test_observation_distinguishes_pristine_active_terminal_and_ambiguous(config_factory, monkeypatch):
    path, _raw = config_factory()
    config = host_job.load_config(path)

    assert host_job.observe_job(config, {"native_state": "absent", "binding_ok": False}) == {
        "job_state": "absent",
        "attempt_ordinal": 0,
        "evidence_digest": None,
    }
    assert host_job.observe_job(config, {"native_state": "running", "binding_ok": True}) == {
        "job_state": "starting",
        "attempt_ordinal": 1,
        "evidence_digest": None,
    }
    assert host_job.observe_job(config, {"native_state": "running", "binding_ok": False}) == {
        "job_state": "ambiguous",
        "attempt_ordinal": 1,
        "evidence_digest": None,
    }

    monkeypatch.setattr(host_job.lifecycle, "load_lifecycle_json", lambda _payload: {})
    monkeypatch.setattr(
        host_job.lifecycle,
        "validate_lifecycle_document",
        lambda _document: {
            "run_id": "gate13-test-a-linux",
            "platform": "linux",
            "source_commit": "a" * 40,
        },
    )

    def runner(bound):
        bound.evidence_path.write_text("{}", encoding="utf-8")
        bound.stderr_path.write_bytes(b"")
        return 0

    terminal = host_job.execute(path, clock=lambda: 2_000_000_000, entrypoint_runner=runner)
    observed = host_job.observe_job(config, {"native_state": "absent", "binding_ok": False})
    assert observed["job_state"] == "passed"
    assert observed["evidence_digest"] == terminal["evidence_digest"]
    assert host_job.observe_job(config, {"native_state": "running", "binding_ok": False}) == {
        "job_state": "ambiguous",
        "attempt_ordinal": 1,
        "evidence_digest": None,
    }


def test_inactive_after_persisted_start_is_ambiguous(config_factory):
    path, _raw = config_factory()
    config = host_job.load_config(path)
    host_job._atomic_json(
        config.status_path,
        {
            "schema_version": 1,
            "run_id": config.run_id,
            "platform": config.platform,
            "attempt_ordinal": 1,
            "state": "running",
            "started_at_unix": 2_000_000_000,
        },
        exclusive=True,
    )

    assert host_job.observe_job(config, {"native_state": "inactive", "binding_ok": True}) == {
        "job_state": "ambiguous",
        "attempt_ordinal": 1,
        "evidence_digest": None,
    }


def test_collect_revalidates_terminal_digest_and_lifecycle_binding(config_factory, monkeypatch):
    path, _raw = config_factory()
    monkeypatch.setattr(host_job.lifecycle, "load_lifecycle_json", lambda _payload: {})
    monkeypatch.setattr(
        host_job.lifecycle,
        "validate_lifecycle_document",
        lambda _document: {
            "run_id": "gate13-test-a-linux",
            "platform": "linux",
            "source_commit": "a" * 40,
        },
    )

    def runner(config):
        config.evidence_path.write_text('{"ok":true}', encoding="utf-8")
        config.stderr_path.write_bytes(b"")
        return 0

    host_job.execute(path, clock=lambda: 2_000_000_000, entrypoint_runner=runner)
    assert host_job.collect(path) == b'{"ok":true}'

    (path.parent / "evidence.json").write_text('{"ok":false}', encoding="utf-8")
    with pytest.raises(host_job.HostJobError, match="digest changed"):
        host_job.collect(path)


def test_start_reattaches_to_bound_native_job_without_mutation(config_factory, monkeypatch):
    path, _raw = config_factory()
    monkeypatch.setattr(
        host_job,
        "native_snapshot",
        lambda _config, _runner: {"native_state": "running", "binding_ok": True},
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("native start must not run")

    observed = host_job.start(path, runner=forbidden)

    assert observed == {
        "job_state": "starting",
        "attempt_ordinal": 1,
        "evidence_digest": None,
    }


def test_linux_snapshot_binds_exact_service_command(config_factory):
    path, _raw = config_factory()
    config = host_job.load_config(path)
    stdout = "\n".join(
        [
            "LoadState=loaded",
            "ActiveState=active",
            "SubState=running",
            "User=gate13",
            "Group=gate13",
            (
                "ExecStart={ path="
                f"{config.python_executable} ; argv[]={config.python_executable} "
                f"{config.adapter_path} execute --config {config.config_path} ; "
                "ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; "
                "pid=0 ; code=(null) ; status=0/0 }"
            ),
            f"WorkingDirectory={config.working_directory}",
            "Restart=no",
            "KillMode=control-group",
            "UMask=0077",
            "NoNewPrivileges=no",
            "PrivateTmp=yes",
            "TimeoutStartUSec=2min",
            "RuntimeMaxUSec=1h 2min",
        ]
    )

    def runner(_argv, timeout):
        assert timeout == 60
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    assert host_job._linux_snapshot(config, runner) == {
        "native_state": "running",
        "binding_ok": True,
    }

    foreign_stdout = stdout.replace(
        f"execute --config {config.config_path} ;",
        f"execute --config {config.config_path} --extra ;",
    )

    def foreign_runner(_argv, timeout):
        assert timeout == 60
        return subprocess.CompletedProcess([], 0, stdout=foreign_stdout, stderr="")

    assert host_job._linux_snapshot(config, foreign_runner) == {
        "native_state": "running",
        "binding_ok": False,
    }

    for foreign_stdout in (
        stdout.replace("ignore_errors=no", "ignore_errors=yes"),
        stdout.replace("status=0/0 }", "status=0/0 ; arbitrary=value }"),
    ):

        def foreign_metadata_runner(_argv, timeout):
            assert timeout == 60
            return subprocess.CompletedProcess([], 0, stdout=foreign_stdout, stderr="")

        assert host_job._linux_snapshot(config, foreign_metadata_runner) == {
            "native_state": "running",
            "binding_ok": False,
        }


def test_public_cli_failure_is_bounded_and_path_free(capsys, tmp_path):
    missing = tmp_path / "secret-token-config.json"

    exit_code = host_job.main(["status", "--config", str(missing)])

    assert exit_code == 2
    output = capsys.readouterr().out
    assert json.loads(output) == {
        "failure_code": "host_job_rejected",
        "result": "failed",
        "schema_version": 1,
    }
    assert str(missing) not in output
