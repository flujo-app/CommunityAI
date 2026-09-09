import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qwen_qualification as qualification
import run_qwen_qualification as launcher
from qwen_product_provenance import sha256
from run_qwen_product_mixed import MixedProductRun
from test_qwen_product_test import receipts


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.mark.parametrize("result_name,exit_code", [("passed", 0), ("failed", 1)])
@pytest.mark.parametrize("old_marker", [None, "not even valid JSON"])
def test_gate13_style_launcher_starts_only_a_fresh_run(tmp_path, monkeypatch, result_name, exit_code, old_marker):
    run_id = "q38pm-new-test"
    runs_root = tmp_path / ".gate13-runs/qwen-product-mixed"
    old = runs_root / "q38pm-old"
    old.mkdir(parents=True)
    (old / "result.json").write_text('{"result":"failed"}')
    if old_marker is not None:
        (runs_root / "active.json").write_text(old_marker)
    calls = []

    class Run:
        def __init__(self, **kwargs):
            assert kwargs["output_root"] == runs_root / run_id
            assert kwargs["cloud_config"] == {"project": "test-project"}

        def run(self):
            calls.append(run_id)
            return {"result": result_name, "events": [], "duration_seconds": 0}

    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "scripts/run_qwen_qualification.py"))
    monkeypatch.setattr(launcher, "_new_run_id", lambda: run_id)
    monkeypatch.setattr(launcher, "_inputs", lambda root: ({}, {}, {"project": "test-project"}))
    monkeypatch.setattr(launcher, "QwenQualification", Run)
    assert launcher.main([]) == exit_code
    assert calls == [run_id]
    assert (old / "result.json").read_text() == '{"result":"failed"}'
    assert not (runs_root / "launcher.lock").exists()
    if old_marker is not None:
        assert (runs_root / "active.json").read_text() == old_marker


def test_no_arguments_contract_rejects_before_loading_config_or_touching_cloud(monkeypatch):
    monkeypatch.setattr(launcher, "_inputs", lambda root: pytest.fail("must not read inputs"))
    assert launcher.main(["--resume"]) == 2


@pytest.mark.parametrize(
    "fail_at",
    [None, "preflight", "package", "bundle", "create", "stage", "source", "client", "interrupt", "cleanup", "report"],
)
def test_ordered_route_client_cleanup_and_failure_records(receipts, monkeypatch, fail_at):
    path, old_output = receipts
    packaged = json.loads((old_output / "result.json").read_text())
    source = json.loads((path / "result.json").read_text())["evidence"]
    # Use the synthetic response payloads as a fake provider's output. The new
    # run must generate its own raw result and receive its own client receipt.
    (path / "result.json").unlink()
    (path / "packaged-client-result.json").unlink()
    calls = []
    thread_ids = []
    config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
    config.update(worker_machine_type="c3-highmem-4", packaged_client_wait_seconds=3600, max_duration_seconds=10800)
    put(path / "provider-config.json", config)
    put(
        path / "workers.json",
        {
            "workers": [
                {"hardware": {"gpu_name": "L4"}},
                {"hardware": {"gpu_name": "T4"}},
                {"hardware": {"device": "cpu"}},
                {"hardware": {"device": "cpu"}},
            ]
        },
    )

    def step(name):
        calls.append(name)
        thread_ids.append(threading.get_ident())
        if fail_at == name:
            raise RuntimeError("injected " + name)

    def capture_source(root, run, inputs):
        (run / "launcher-source.tar.gz").write_bytes(b"launcher archive")
        value = {
            "run_id": run.name,
            "files": {},
            "archive_sha256": sha256(run / "launcher-source.tar.gz"),
            "inputs": {"node": {"sha256": "a" * 64}},
        }
        put(run / "launcher-source.json", value)
        return value

    def package(*args):
        step("package")
        return {"verified_files": 1, "node_sha256": "a" * 64}

    def source_test(self):
        step("source")
        return {"result": "passed", "evidence": source}

    def client(run, output, config, inputs):
        assert (run / "packaged-client-ready.json").is_file()
        step("client")
        if fail_at == "interrupt":
            raise KeyboardInterrupt("injected client interrupt")
        put(output / "result.json", packaged)
        put(run / "packaged-client-result.json", packaged)
        return 0

    def cleanup(self):
        step("cleanup")
        return {"verified": True}

    def report(*args, **kwargs):
        step("report")
        from report_qwen_product import report as real_report

        return real_report(*args, **kwargs)

    monkeypatch.setattr(qualification, "snapshot", capture_source)
    monkeypatch.setattr(qualification, "verify_snapshot", lambda *args: None)
    monkeypatch.setattr(qualification, "verify_package", package)
    monkeypatch.setattr(qualification, "execute_packaged", client)
    monkeypatch.setattr(qualification, "report", report)
    monkeypatch.setattr(
        qualification.LoggedRunner,
        "run",
        lambda self, argv, **kwargs: None if argv[0] == sys.executable else pytest.fail("unexpected provider call"),
    )
    monkeypatch.setattr(MixedProductRun, "preflight", lambda self: step("preflight"))
    monkeypatch.setattr(MixedProductRun, "bundle", lambda self: step("bundle"))
    monkeypatch.setattr(MixedProductRun, "create_firewalls", lambda self: step("create"))
    monkeypatch.setattr(MixedProductRun, "stage", lambda self, *args: step("stage"))
    monkeypatch.setattr(MixedProductRun, "exercise_workers", source_test)
    monkeypatch.setattr(MixedProductRun, "cleanup", cleanup)
    monkeypatch.setattr(MixedProductRun, "az_json", lambda self, *args: {"registrationState": "Registered"})
    monkeypatch.setattr(MixedProductRun, "wait_file", lambda self, *args, **kwargs: {"peers": []})
    # A source failure before its final receipt never launches the package test.
    monkeypatch.setattr(MixedProductRun, "read", lambda self, *args: None)
    for method in (
        "create_gcp",
        "create_azure",
        "finish_network",
        "enable_packaged_client",
        "wait_setup",
        "start_job",
        "start_product",
        "capture_product",
    ):
        monkeypatch.setattr(MixedProductRun, method, lambda *args, **kwargs: None)
    provenance = path / "package-provenance.json"
    put(provenance, {})
    result = qualification.QwenQualification(
        root=ROOT,
        output_root=path,
        cloud_config=config,
        packaged_config={"node_sha256": "a" * 64},
        inputs={"node": path / "node.exe", "package_provenance": provenance},
    ).run()
    assert result["result"] == ("passed" if fail_at is None else "failed")
    assert len(set(thread_ids)) == 1  # Ordered execution, including the packaged callback and cleanup.
    assert json.loads((path / "qualification/result.json").read_text()) == result
    assert json.loads((path / "qualification/run-state.json").read_text())["result"] == result["result"]
    if fail_at in ("preflight", "package", "bundle"):
        assert "create" not in calls and "cleanup" not in calls
    else:
        assert calls.count("cleanup") == 1
    if fail_at is None:
        assert calls == [
            "preflight",
            "package",
            "bundle",
            "create",
            *(["stage"] * 5),
            "source",
            "client",
            "cleanup",
            "package",
            "report",
        ]
        assert result["cleanup"]["result"] == "passed"
        assert set(result["clients"]) == {"windows"}
        assert json.loads((path / "result.json").read_text())["scope"] == "production-node-model-transitions"
    else:
        assert result["failure_reason"]
        assert any(e["phase"] == "FAILURE" for e in result["events"])
        if fail_at == "source":
            assert (
                next(e for e in result["events"] if e["phase"] == "FAILURE")["details"]["failed_phase"]
                == "SOURCE_RUNNING"
            )


@pytest.mark.skipif(os.name != "nt", reason="Windows one-click shell")
@pytest.mark.parametrize("exit_code", [0, 1, 2])
def test_cmd_preserves_exit_code_and_handles_paths_with_spaces(tmp_path, exit_code):
    root = tmp_path / "repo with spaces"
    (root / "scripts").mkdir(parents=True)
    cmd = root / "Run Qwen Qualification.cmd"
    shutil.copyfile(ROOT / cmd.name, cmd)
    (root / "scripts/run_qwen_qualification.py").write_text(
        "import sys\nprint('launched test entrypoint')\nsys.exit(" + str(exit_code) + ")\n"
    )
    result = subprocess.run(
        # cmd.exe has its own quoting grammar; a list would insert backslashes
        # before these quotes using the C-runtime argv rules.
        f'cmd.exe /d /s /c ""{cmd}""',
        cwd=tmp_path,
        env=dict(os.environ, COMMUNITYAI_TEST_PYTHON=sys.executable),
        input="\n",
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == exit_code, result.stdout + result.stderr
    assert "launched test entrypoint" in result.stdout
    assert f"finished with exit code {exit_code}" in result.stdout
