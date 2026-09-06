import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from run_qwen_product_gcp import ROOT, ProductRun
from run_qwen_product_mixed import MixedProductRun


def test_mixed_product_failure_and_failed_diagnostics_still_clean_both_providers(tmp_path, monkeypatch):
    config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
    run = MixedProductRun(tmp_path / "q38pm-test", config)
    calls = []
    for name in ("preflight", "bundle", "create_firewalls", "create_gcp"):
        monkeypatch.setattr(run, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "az_json", lambda *args: {"registrationState": "Registered"})

    def fail(*args, **kwargs):
        raise RuntimeError("simulated provider failure")

    monkeypatch.setattr(run, "create_azure", fail)
    monkeypatch.setattr(run, "capture_product", fail)
    monkeypatch.setattr(run, "cleanup", lambda: calls.append("both-providers") or {"verified": True})
    result = run.run()
    assert result["result"] == "failed"
    assert result["cleanup"]["verified"]
    assert "diagnostic_error" in result
    assert calls == ["both-providers"]


def test_mixed_product_stages_api_and_gpu_dependencies(tmp_path):
    config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
    run = MixedProductRun(tmp_path / "q38pm-test", config)
    coordinator = run.setup_source(run.names[0], "0" * 64)
    worker = run.setup_source(run.names[1], "0" * 64)
    assert "source[api]" in coordinator
    assert "nvidia-driver-580-server" in worker
    assert "cu124" in worker
    assert run.bundle.__func__ is ProductRun.bundle


@pytest.mark.parametrize("source_finished", [False, True])
def test_failed_source_can_hold_loaded_workers_for_independent_package_without_passing(
    tmp_path, monkeypatch, source_finished
):
    config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
    config["packaged_client_wait_seconds"] = 60
    run = MixedProductRun(tmp_path / "q38pm-test", config)
    calls = []
    for name in (
        "preflight",
        "bundle",
        "create_firewalls",
        "create_gcp",
        "create_azure",
        "finish_network",
        "enable_packaged_client",
        "stage",
        "wait_setup",
        "start_job",
        "start_product",
        "capture_product",
    ):
        monkeypatch.setattr(run, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(run, "az_json", lambda *args: {"registrationState": "Registered"})
    monkeypatch.setattr(run, "wait_file", lambda *args, **kwargs: {"peers": []})
    monkeypatch.setattr(run, "read", lambda *args: {"result": "failed"} if source_finished else None)
    monkeypatch.setattr(run, "wait_packaged_client", lambda: calls.append("package") or {"result": "passed"})
    monkeypatch.setattr(run, "cleanup", lambda: calls.append("cleanup") or {"verified": True})
    (run.path / "workers.json").write_text('{"workers": []}')

    def fail():
        raise RuntimeError("source transition assertion")

    monkeypatch.setattr(run, "exercise_workers", fail)
    result = run.run()
    assert result["result"] == "failed"
    assert "source transition assertion" in result["error"]
    assert result["cleanup"]["verified"]
    assert calls == (["package", "cleanup"] if source_finished else ["cleanup"])


@pytest.mark.parametrize("receipt", ["passed", "failed", "wrong-run", "not-stopped", "timeout"])
def test_packaged_wait_requires_bound_result_and_always_has_a_deadline(tmp_path, receipt):
    config = json.loads((ROOT / "config/qwen_mixed_inference.json").read_text())
    config["packaged_client_wait_seconds"] = 1
    run = MixedProductRun(tmp_path / "q38pm-test", config)
    if receipt == "timeout":
        run.started = 0
    else:
        (run.path / "packaged-client-result.json").write_text(
            json.dumps(
                {
                    "run_id": "another-run" if receipt == "wrong-run" else run.run_id,
                    "node_stopped": receipt != "not-stopped",
                    "result": "failed" if receipt == "failed" else "passed",
                }
            )
        )
    if receipt == "passed":
        assert run.wait_packaged_client()["result"] == "passed"
    else:
        with pytest.raises((RuntimeError, ValueError, TimeoutError)):
            run.wait_packaged_client()


def test_source_recovery_rejects_a_result_completed_before_the_injected_outage(tmp_path, monkeypatch):
    config = json.loads((ROOT / "config/qwen_full_inference_gcp.json").read_text())
    run = ProductRun(tmp_path / "q38p-test", config)
    monkeypatch.setattr(run, "start_job", lambda *args: None)
    monkeypatch.setattr(
        run,
        "ssh",
        lambda *args, **kwargs: type(
            "Reply", (), {"stdout": "MainPID=0\nActiveState=inactive\nKillMode=control-group\n"}
        )(),
    )
    calls = []

    def wait_file(name, filename, **kwargs):
        calls.append(filename)
        if filename == "worker.json":
            return {"peer_id": "replacement" if kwargs.get("predicate") else name}
        if filename == "product-ready-for-loss.json":
            return {"ready": True, "recovery_nonce": "test-outage"}
        if filename == "product-local-after-loss.json":
            return {"recovery_nonce": "test-outage"}
        if filename == "product-result.json":
            # The old harness accepted this even if a spontaneous local fallback
            # and recovery had completed while cloud control was unavailable.
            return {"result": "passed"}
        return {}

    monkeypatch.setattr(run, "wait_file", wait_file)
    with pytest.raises(RuntimeError, match="recovery acknowledgements"):
        run.exercise_workers()
