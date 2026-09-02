import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_host_job as shared  # noqa: E402
import gate14_host_job as host_job  # noqa: E402


def sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def config_factory(tmp_path, monkeypatch):
    def make(platform="linux"):
        root = tmp_path / platform
        root.mkdir()
        adapter = root / "gate14_host_job.py"
        adapter.write_bytes(host_job.ADAPTER_PATH.read_bytes())
        entrypoint = root / "gate14_host_lifecycle.py"
        entrypoint.write_text("# bound Gate 14 lifecycle\n", encoding="utf-8")
        lifecycle_config = root / f"gate14-{platform}-run.json"
        lifecycle_config.write_text('{"bound":true}\n', encoding="utf-8")
        python = Path(sys.executable).resolve()

        monkeypatch.setitem(host_job.HOST_ROOTS, platform, root)
        monkeypatch.setitem(host_job.HOST_PYTHON, platform, python)
        monkeypatch.setattr(host_job, "ADAPTER_PATH", adapter.resolve())
        monkeypatch.setattr(host_job, "LINUX_HOME", "/home/gate14-test")
        monkeypatch.setattr(host_job, "LINUX_RUNTIME_DIR", "/qualification/gate14-test/runtime")

        run_id = "gate14-test-a"
        raw = {
            "schema_version": 1,
            "run_id": run_id,
            "lifecycle_run_id": run_id,
            "platform": platform,
            "attempt_ordinal": 1,
            "source_commit": "a" * 40,
            "job_name": f"communityai-gate14-{run_id}-{platform}",
            "host_user": "Gate14Admin" if platform == "windows" else "gate14",
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


def test_tampered_shared_core_is_rejected_before_import(tmp_path):
    wrapper = tmp_path / "gate14_host_job.py"
    shared_core = tmp_path / "gate13_host_job.py"
    wrapper.write_bytes(host_job.ADAPTER_PATH.read_bytes())
    shared_core.write_bytes(host_job._SHARED_CORE_PATH.read_bytes() + b"\nTAMPERED = True\n")
    spec = importlib.util.spec_from_file_location("_tampered_gate14_host_job", wrapper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    with pytest.raises(ImportError, match="core digest changed"):
        spec.loader.exec_module(module)


def test_crlf_shared_core_has_the_same_bound_source_digest(tmp_path):
    wrapper = tmp_path / "gate14_host_job.py"
    shared_core = tmp_path / "gate13_host_job.py"
    wrapper.write_bytes(host_job.ADAPTER_PATH.read_bytes())
    shared_core.write_bytes(host_job._SHARED_CORE_PATH.read_bytes().replace(b"\n", b"\r\n"))
    spec = importlib.util.spec_from_file_location("_crlf_gate14_host_job", wrapper)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)

    spec.loader.exec_module(module)

    assert module._SHARED_CORE_PATH == shared_core.resolve()


def test_gate14_config_uses_separate_namespace_root_and_run_binding(config_factory):
    path, raw = config_factory()

    config = host_job.load_config(path)

    assert config.run_id == "gate14-test-a"
    assert config.lifecycle_run_id == config.run_id
    assert config.job_name == "communityai-gate14-gate14-test-a-linux"
    assert config.host_user == "gate14"
    assert config.adapter_sha256 == raw["adapter_sha256"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("job_name", "communityai-gate13-gate14-test-a-linux"),
        ("lifecycle_run_id", "gate14-test-a-linux"),
        ("host_user", "gate13"),
    ],
)
def test_gate13_or_foreign_execution_bindings_are_rejected(config_factory, field, value):
    path, raw = config_factory()
    raw[field] = value
    path.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(host_job.HostJobError):
        host_job.load_config(path)


def test_gate14_uses_an_isolated_core_and_never_mutates_gate13_defaults(config_factory):
    path, _raw = config_factory()
    expected = {
        "HOST_ROOTS": shared.HOST_ROOTS,
        "HOST_PYTHON": shared.HOST_PYTHON,
        "ADAPTER_PATH": shared.ADAPTER_PATH,
        "GATE_NAME": shared.GATE_NAME,
        "LINUX_HOST_USER": shared.LINUX_HOST_USER,
        "LINUX_HOME": shared.LINUX_HOME,
        "LINUX_RUNTIME_DIR": shared.LINUX_RUNTIME_DIR,
        "LIFECYCLE_CONFIG_NAMES": shared.LIFECYCLE_CONFIG_NAMES,
        "LIFECYCLE_RUN_ID_BUILDER": shared.LIFECYCLE_RUN_ID_BUILDER,
        "_JOB_RE": shared._JOB_RE,
        "MAX_EVIDENCE_BYTES": shared.MAX_EVIDENCE_BYTES,
        "EVIDENCE_VALIDATOR": shared.EVIDENCE_VALIDATOR,
    }

    host_job.load_config(path)

    assert host_job.core is not shared
    assert all(getattr(shared, name) is value for name, value in expected.items())


def test_platform_evidence_validator_calls_strict_gate14_contract(monkeypatch):
    document = {
        "run_id": "gate14-test-a",
        "platform": "linux",
        "source_commit": "a" * 40,
    }
    calls = []

    def strict(payload):
        assert payload == b'{"gate":14}\n'
        return document

    def validate(value):
        calls.append(value)

    monkeypatch.setattr(host_job.acceptance, "_strict_json", strict)
    monkeypatch.setattr(host_job.acceptance, "validate_platform_document", validate)

    assert host_job._validate_platform_evidence(b'{"gate":14}\n') == document
    assert calls == [document]


def test_execute_is_exactly_once_and_collects_digest_bound_platform_evidence(config_factory, monkeypatch):
    path, raw = config_factory()
    payload = b'{"gate":14}\n'
    calls = []

    def validate(value):
        assert value == payload
        return {
            "run_id": raw["run_id"],
            "platform": raw["platform"],
            "source_commit": raw["source_commit"],
        }

    def entrypoint(config):
        calls.append(config.run_id)
        config.evidence_path.write_bytes(payload)
        config.stderr_path.write_bytes(b"")
        return 0

    monkeypatch.setattr(host_job, "_validate_platform_evidence", validate)

    first = host_job.execute(path, clock=lambda: 100, entrypoint_runner=entrypoint)
    second = host_job.execute(path, clock=lambda: 200, entrypoint_runner=lambda _config: 99)

    assert first == second
    assert first["result"] == "passed"
    assert first["evidence_digest"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert calls == [raw["run_id"]]
    assert host_job.collect(path) == payload


def test_invalid_platform_evidence_fails_terminally(config_factory, monkeypatch):
    path, _raw = config_factory()

    def reject(_payload):
        raise ValueError("not Gate 14 platform evidence")

    def entrypoint(config):
        config.evidence_path.write_text('{"gate":13}\n', encoding="utf-8")
        config.stderr_path.write_bytes(b"")
        return 0

    monkeypatch.setattr(host_job, "_validate_platform_evidence", reject)

    terminal = host_job.execute(path, clock=lambda: 100, entrypoint_runner=entrypoint)

    assert terminal["result"] == "failed"
    assert terminal["failure_code"] == "invalid_lifecycle_evidence"
    assert terminal["evidence_digest"] is None
    with pytest.raises(host_job.HostJobError, match="successful terminal"):
        host_job.collect(path)


def test_linux_native_command_is_gate14_bound(config_factory):
    path, _raw = config_factory()
    config = host_job.load_config(path)

    host_job._configure_core()
    argv = host_job.core._linux_start_argv(config)

    assert "--unit" in argv
    assert argv[argv.index("--unit") + 1] == config.job_name
    assert f"--setenv=HOME={host_job.LINUX_HOME}" in argv
    assert f"--setenv=XDG_RUNTIME_DIR={host_job.LINUX_RUNTIME_DIR}" in argv
    assert str(host_job.ADAPTER_PATH) in argv
