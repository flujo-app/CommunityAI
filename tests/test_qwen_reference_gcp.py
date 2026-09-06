import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from run_qwen_reference_gcp import ROOT, ReferenceRun


def test_reference_failure_and_diagnostic_failure_still_clean_up(tmp_path, monkeypatch):
    config = json.loads((ROOT / "config/qwen_full_inference_gcp.json").read_text())
    run = ReferenceRun(tmp_path / "q38r-test", config)
    assert len(run.names) == 1
    calls = []
    for name in ("preflight", "bundle", "create_firewalls", "create", "stage", "wait_setup"):
        monkeypatch.setattr(run, name, lambda *args: None)
    monkeypatch.setattr(run, "ssh", lambda *args, **kwargs: SimpleNamespace(stdout="", stderr=""))

    def fail(*args, **kwargs):
        raise RuntimeError("simulated host failure")

    monkeypatch.setattr(run, "wait_file", fail)
    monkeypatch.setattr(run, "read", fail)

    def cleanup():
        calls.append("cleanup")
        return {"verified": True}

    monkeypatch.setattr(run, "cleanup", cleanup)
    result = run.run()
    assert result["result"] == "failed"
    assert result["cleanup"]["verified"]
    assert "diagnostic_error" in result
    assert calls == ["cleanup"]
    assert json.loads((run.path / "result.json").read_text())["cleanup"]["verified"]
