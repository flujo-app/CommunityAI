import concurrent.futures
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import qwen_product_provenance as provenance
import run_qwen_product_test as launcher
from report_qwen_product import report, summarize


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def receipts(tmp_path):
    run, output = tmp_path / "q38pm-test", tmp_path / "package"
    completion = lambda model: {
        "response": {"model": model, "usage": {"completion_tokens": 3}, "choices": [{"text": "Paris"}]}
    }
    remote, local = "Qwen3.8 27B FP8 Dequant", "Qwen3.5-0.8B-Local"
    selection = lambda **kwargs: {"auto_selection": kwargs}
    phases = [
        dict(
            hub_offline=offline,
            node_stopped=True,
            node_sha256="a" * 64,
            community_completion=completion(remote),
            community_chat=completion(remote),
            local_only_completion=completion(local),
            http_downloads_blocked=offline,
        )
        for offline in (False, True)
    ]
    phases[0]["worker_outage"] = {
        "before_stop_status": selection(model=remote),
        "fallback_status": selection(source="local"),
        "recovered_status": selection(model=remote),
        "local_completion": completion(local),
        "community_after_rejoin": completion(remote),
        "stop": {
            "instance": run.name + "-w2",
            "action": "stop",
            "observed_at_unix": 1,
            "after": "MainPID=0\nActiveState=inactive\nKillMode=control-group\n",
        },
        "restart": {"instance": run.name + "-w2", "action": "start", "observed_at_unix": 2},
    }
    packaged = dict(
        run_id=run.name,
        result="passed",
        node_stopped=True,
        phases=phases,
        node_sha256="a" * 64,
        catalog_scope="public policy",
        remote_cache_source="seeded cache",
    )
    source = dict(
        result="passed",
        worker_stopped_acknowledgement={"recovery_nonce": "test", "peer_id": "old"},
        worker_replaced_acknowledgement={"recovery_nonce": "test", "peer_id": "new"},
    )
    final = dict(
        result="passed", run_id=run.name, packaged_client=packaged, evidence=source, cleanup={"verified": True}
    )
    put(run / "result.json", final)
    put(run / "packaged-client-result.json", packaged)
    put(output / "result.json", packaged)
    put(
        run / "provider-config.json",
        dict(client_machine_type="e2-standard-4", gpu_machine_type="g2-standard-8", worker_machine_type="c3-highmem-4"),
    )
    for suffix, machine in zip(
        ("c", "w0", "w2", "w3"), ("e2-standard-4", "g2-standard-8", "c3-highmem-4", "c3-highmem-4")
    ):
        put(
            run / f"{run.name}-{suffix}-instance.json",
            {"name": run.name + "-" + suffix, "machineType": "zones/test/" + machine},
        )
    # This used to break the manual report's broad *-instance.json glob.
    put(run / f"{run.name}-w1-instance.json", {"azure": True})
    (run / "source.tar.gz").write_bytes(b"test archive")
    put(run / "source-inventory.json", {"files": {}, "bundle_sha256": provenance.sha256(run / "source.tar.gz")})
    return run, output


def test_report_accepts_both_providers_without_parsing_azure_as_gcp(receipts):
    value = summarize(*receipts)
    assert value["result"] == "passed"
    assert len(value["gcp_instances"]) == 4


@pytest.mark.parametrize("fault", [None, "failed", "changed-inputs", "changed-inventory"])
def test_standalone_reporting_cannot_override_failed_launcher_provenance(receipts, fault):
    run, output = receipts
    (run / "launcher-source.tar.gz").write_bytes(b"launch archive")
    put(
        run / "launcher-source.json",
        {
            "run_id": run.name,
            "files": {},
            "archive_sha256": provenance.sha256(run / "launcher-source.tar.gz"),
            "inputs": {"node": {"sha256": "a" * 64}},
        },
    )
    put(
        run / "launcher-result.json",
        {
            "run_id": run.name,
            "result": "failed" if fault == "failed" else "passed",
            "inputs_unchanged": fault != "changed-inputs",
            "source_inventory_sha256": "wrong"
            if fault == "changed-inventory"
            else provenance.sha256(run / "launcher-source.json"),
        },
    )
    result = report(run, output, run / "standalone-report.json")
    assert result["result"] == ("passed" if fault is None else "failed")


@pytest.mark.parametrize(
    "fault",
    ["run-id", "cleanup", "receipt", "offline", "already-local", "wrong-worker", "nonce", "topology", "archive"],
)
def test_report_rejects_incomplete_or_unbound_success(receipts, fault):
    run, output = receipts
    final = launcher.read(run / "result.json")
    packaged = final["packaged_client"]
    if fault == "run-id":
        final["run_id"] = "another-run"
    elif fault == "cleanup":
        final["cleanup"]["verified"] = False
    elif fault == "offline":
        packaged["phases"][1]["http_downloads_blocked"] = False
    elif fault == "already-local":
        packaged["phases"][0]["worker_outage"]["before_stop_status"]["auto_selection"]["model"] = "local"
    elif fault == "wrong-worker":
        packaged["phases"][0]["worker_outage"]["stop"]["instance"] = "communityai-bootstrap-1"
    elif fault == "nonce":
        final["evidence"]["worker_replaced_acknowledgement"]["recovery_nonce"] = "stale"
    elif fault == "topology":
        put(run / f"{run.name}-w2-instance.json", {"name": run.name + "-w2", "machineType": "e2-highmem-4"})
    elif fault == "archive":
        (run / "source.tar.gz").write_bytes(b"changed")
    put(run / "result.json", final)
    put(output / "result.json", packaged)
    if fault != "receipt":
        put(run / "packaged-client-result.json", packaged)
    else:
        put(run / "packaged-client-result.json", {"run_id": run.name, "result": "failed"})
    value = report(run, output, run / "report.json")
    assert value["result"] == "failed"
    with pytest.raises(ValueError, match="Preserve"):
        report(run, output, run / "report.json")


@pytest.mark.parametrize("change", ["source", "new-source", "input"])
def test_launch_inventory_preserves_bytes_and_rejects_changes(tmp_path, monkeypatch, change):
    root, run = tmp_path / "repo", tmp_path / "run"
    run.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "desktop/pyproject.toml", "scripts/runner.py"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("before")
    (root / ".publisher-secrets").mkdir()
    (root / ".publisher-secrets/key.pem").write_text("must never enter the archive")
    item = tmp_path / "input.json"
    item.write_text("{}")
    monkeypatch.setattr(provenance.subprocess, "run", lambda *a, **k: type("Reply", (), {"stdout": "a" * 40})())
    inventory = provenance.snapshot(root, run, {"input": item})
    provenance.verify_snapshot(root, run, inventory)
    assert not any("publisher-secrets" in name for name in inventory["files"])
    target = {"source": root / "scripts/runner.py", "new-source": root / "scripts/new.py", "input": item}[change]
    target.write_text("after")
    with pytest.raises(ValueError, match="changed during"):
        provenance.verify_snapshot(root, run, inventory)


def test_package_verification_catches_changed_dependency(tmp_path):
    node, dll = tmp_path / "CommunityAI/node.exe", tmp_path / "CommunityAI/dependency.dll"
    node.parent.mkdir()
    node.write_bytes(b"entrypoint")
    dll.write_bytes(b"dependency")
    digest = provenance.sha256(node)
    p = tmp_path / "provenance.json"
    put(
        p,
        {
            "artifacts": [
                {
                    "path": f.relative_to(tmp_path).as_posix(),
                    "kind": "file",
                    "size_bytes": f.stat().st_size,
                    "sha256": provenance.sha256(f),
                }
                for f in (node, dll)
            ]
        },
    )
    assert provenance.verify_package(node, p, digest)["verified_files"] == 2
    extra = node.parent / "unexpected.dll"
    extra.write_bytes(b"unlisted")
    with pytest.raises(ValueError, match="unlisted"):
        provenance.verify_package(node, p, digest)
    extra.unlink()
    dll.write_bytes(b"wrong-code")
    with pytest.raises(ValueError, match="provenance mismatch"):
        provenance.verify_package(node, p, digest)


def test_main_creates_fresh_runs_and_accepts_preflight_observation_without_changing_inputs(tmp_path, monkeypatch):
    import run_qwen_mixed_inference
    import run_qwen_product_mixed

    root = tmp_path / "repo"
    root.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "desktop/pyproject.toml"):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("source")
    node = root / "CommunityAI/node.exe"
    node.parent.mkdir()
    node.write_bytes(b"package")
    p = root / "provenance.json"
    put(
        p,
        {
            "artifacts": [
                {"path": "CommunityAI/node.exe", "kind": "file", "size_bytes": 7, "sha256": provenance.sha256(node)}
            ]
        },
    )
    proof = root / "proof"
    put(proof / "result.json", {"result": "passed"})
    put(proof / "replacement.json", {})
    put(root / "cache.json", {"result": "passed"})
    put(root / "cloud.json", {})
    (root / "cache").mkdir()
    config = root / "replay.json"
    put(
        config,
        dict(
            node=str(node),
            node_sha256=provenance.sha256(node),
            package_provenance=str(p),
            local_cache=str(root / "cache"),
            remote_cache=str(root / "cache"),
            cache_provenance=str(root / "cache.json"),
            cloud_config=str(root / "cloud.json"),
        ),
    )
    seen = []

    class Preflight:
        def __init__(self, path, config):
            self.path, self.config = path, config
            seen.append(path)

        def preflight(self):
            self.config["admin_ip"] = "192.0.2.1"
            put(self.path / "provider-config.json", self.config)

    monkeypatch.setattr(launcher, "ROOT", root)
    monkeypatch.setattr(run_qwen_product_mixed, "RUNS", root / "runs")
    monkeypatch.setattr(run_qwen_product_mixed, "MixedProductRun", Preflight)
    monkeypatch.setattr(run_qwen_mixed_inference, "require_cpu_proof", lambda path: {})
    monkeypatch.setattr(launcher.subprocess, "run", lambda *a, **k: type("Reply", (), {"stdout": "a" * 40})())
    args = ["--config", str(config), "--cpu-proof", str(proof), "--preflight-only"]
    assert launcher.main(args) == launcher.main(args) == 0
    assert len(set(seen)) == 2
    for path in seen:
        assert launcher.read(path / "provider-config.json")["admin_ip"] == "192.0.2.1"
        assert "admin_ip" not in launcher.read(path / "requested-provider-config.json")
        assert not (path / "result.json").exists()  # Preflight is never a generation pass.


@pytest.mark.parametrize("failure", ["spawn", "timeout", "exit"])
def test_packaged_failure_unblocks_cloud_cleanup(tmp_path, monkeypatch, failure):
    run = tmp_path / "q38pm-test"
    put(run / "packaged-client-ready.json", {"run_id": run.name, "deadline_unix": time.time() + 3600})
    future = concurrent.futures.Future()
    calls = []

    class Process:
        pid = 123
        returncode = 1

        def poll(self):
            return None if failure == "timeout" else 1

    def start(*args, **kwargs):
        if failure == "spawn":
            raise OSError("could not start qualifier")
        if failure == "timeout":
            future.set_result({"result": "failed"})
        return Process()

    monkeypatch.setattr(launcher.subprocess, "Popen", start)
    monkeypatch.setattr(launcher, "stop_process_tree", lambda p: calls.append(p.pid) or True)
    with pytest.raises((OSError, RuntimeError, TimeoutError)):
        launcher.execute_packaged(
            run,
            tmp_path / "out",
            {"local_cache": "local", "remote_cache": "remote"},
            {"node": tmp_path / "node.exe", "cache_provenance": tmp_path / "cache.json"},
            future,
        )
    receipt = launcher.read(run / "packaged-client-result.json")
    assert receipt["run_id"] == run.name and receipt["result"] == "failed"
    assert receipt["node_stopped"] is (failure != "exit")
    assert calls == ([123] if failure == "timeout" else [])


def test_timeout_cleanup_stops_a_real_owned_process_and_child(tmp_path):
    import subprocess

    import psutil

    pid_file = tmp_path / "child-pid.txt"
    program = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(90)']); "
        "Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(90)"
    )
    parent = subprocess.Popen(
        [sys.executable, "-c", program, str(pid_file)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
    )
    try:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        child = psutil.Process(int(pid_file.read_text()))
        assert launcher.stop_process_tree(parent)
        assert parent.poll() is not None
        assert not child.is_running()
    finally:
        if parent.poll() is None:
            launcher.stop_process_tree(parent)


@pytest.mark.parametrize("fail", [False, True])
def test_orchestration_automatically_hands_off_reports_and_waits_for_cleanup(receipts, monkeypatch, fail):
    run_path, output = receipts
    inventory_path = run_path / "launcher-source.json"
    # The report's source binding is separately tested; this test exercises orchestration.
    inventory_path.write_text("{}")
    calls = []

    class Run:
        path, run_id = run_path, run_path.name

        def run(self):
            put(self.path / "packaged-client-ready.json", {"run_id": self.run_id, "deadline_unix": time.time() + 30})
            while not (self.path / "handoff-done").exists():
                time.sleep(0.01)
            calls.append("cleanup")
            return {"result": "passed", "cleanup": {"verified": True}}

    def execute(*args):
        calls.append("package")
        (run_path / "handoff-done").touch()
        if fail:
            raise RuntimeError("package failed")
        return 0

    monkeypatch.setattr(launcher, "verify_snapshot", lambda *a: None)
    monkeypatch.setattr(launcher, "verify_package", lambda *a: None)

    def record(run, output, destination, *, launcher):
        assert calls == ["package", "cleanup"]
        assert (run / "launcher-result.json").exists()
        assert destination.name == "product-report.json"
        calls.append("report")
        return launcher

    monkeypatch.setattr(launcher, "report", record)
    result = launcher.orchestrate(
        Run(),
        output,
        {"node_sha256": "a" * 64},
        {"node": Path("node"), "package_provenance": Path("prov")},
        {},
        {},
        execute=execute,
    )
    assert result["result"] == ("failed" if fail else "passed")
    assert calls == ["package", "cleanup", "report"]
