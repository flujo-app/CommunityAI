import json
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_gate13_gcp as launcher
from gate13_cloud_orchestrator import Gate13CloudError
from gate13_gcp_provider import GcpConfig

RUN_ID = "g13-20260902-000000-abcd"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")


def active_record():
    return {
        "schema_version": 1,
        "provider": "gcp",
        "run_id": RUN_ID,
        "output_directory": RUN_ID,
    }


class RecoveryProvider:
    def __init__(self):
        self.calls = []

    def cleanup_all(self):
        self.calls.append("cleanup")
        return {"result": "passed"}

    def verify_cleanup(self):
        self.calls.append("verify")
        return {"result": "passed"}


def test_interrupted_recovery_uses_the_original_provider_snapshot(tmp_path, monkeypatch):
    runs_root = tmp_path / "gcp"
    output_root = runs_root / RUN_ID
    output_root.mkdir(parents=True)
    config = asdict(GcpConfig.load(ROOT / "config" / "gate13_gcp.json"))
    config["project"] = "original-snapshot-project"
    write_json(output_root / "provider-config.json", config)
    active_path = runs_root / "active.json"
    write_json(active_path, active_record())
    recovered = RecoveryProvider()
    observed = {}

    def fake_provider(**kwargs):
        observed.update(kwargs)
        return recovered

    monkeypatch.setattr(launcher, "_provider", fake_provider)
    launcher._recover_previous(active_path, ROOT, runs_root)

    assert observed["config"].project == "original-snapshot-project"
    assert recovered.calls == ["cleanup", "verify"]
    assert not active_path.exists()
    assert json.loads((output_root / "recovery.json").read_text())["result"] == "passed"


def test_clean_terminal_run_clears_active_pointer_without_recovery(tmp_path, monkeypatch):
    runs_root = tmp_path / "gcp"
    output_root = runs_root / RUN_ID
    output_root.mkdir(parents=True)
    config = asdict(GcpConfig.load(ROOT / "config" / "gate13_gcp.json"))
    write_json(output_root / "provider-config.json", config)
    write_json(output_root / "result.json", {"cleanup": {"result": "passed"}})
    active_path = runs_root / "active.json"
    write_json(active_path, active_record())
    monkeypatch.setattr(
        launcher,
        "_provider",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected recovery")),
    )

    launcher._recover_previous(active_path, ROOT, runs_root)

    assert not active_path.exists()


def test_recovery_rejects_a_cross_provider_active_record(tmp_path):
    runs_root = tmp_path / "gcp"
    output_root = runs_root / RUN_ID
    output_root.mkdir(parents=True)
    config = asdict(GcpConfig.load(ROOT / "config" / "gate13_gcp.json"))
    write_json(output_root / "provider-config.json", config)
    active = active_record()
    active["provider"] = "azure"
    active_path = runs_root / "active.json"
    write_json(active_path, active)

    with pytest.raises(Gate13CloudError, match="active-run recovery record"):
        launcher._recover_previous(active_path, ROOT, runs_root)
