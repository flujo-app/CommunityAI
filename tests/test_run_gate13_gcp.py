import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_gate13_gcp as launcher
from gate13_gcp_provider import GcpConfig

RUN_ID = "g13-20260905-000000-abcd"


@pytest.mark.parametrize("result_name,exit_code", [("passed", 0), ("failed", 1)])
@pytest.mark.parametrize("old_marker", [None, "not even valid JSON"])
def test_launcher_starts_only_a_new_run_without_historic_recovery(
    tmp_path, monkeypatch, result_name, exit_code, old_marker
):
    config = GcpConfig.load(ROOT / "config" / "gate13_gcp.json")
    runs_root = tmp_path / ".gate13-runs" / "gcp"
    old_run = runs_root / "g13-20260904-000000-aaaa"
    old_run.mkdir(parents=True)
    old_evidence = old_run / "result.json"
    old_evidence.write_text('{"result":"failed"}\n', encoding="utf-8")
    active_path = runs_root / "active.json"
    if old_marker is not None:
        active_path.write_text(old_marker, encoding="utf-8")
    calls = []

    def make_provider(**kwargs):
        calls.append(("provider", kwargs["run_id"]))
        return object()

    class CurrentRun:
        def __init__(self, **kwargs):
            assert kwargs["run_id"] == RUN_ID

        def run(self):
            calls.append(("run", RUN_ID))
            return {
                "result": result_name,
                "cleanup": {"result": result_name},
                "events": [],
                "duration_seconds": 0,
                "failure_reason": "test failure" if result_name == "failed" else None,
            }

    monkeypatch.setattr(launcher, "__file__", str(tmp_path / "scripts" / "run_gate13_gcp.py"))
    monkeypatch.setattr(launcher.GcpConfig, "load", lambda _path: config)
    monkeypatch.setattr(launcher, "_new_run_id", lambda: RUN_ID)
    monkeypatch.setattr(launcher, "_provider", make_provider)
    monkeypatch.setattr(launcher, "Gate13CloudOrchestrator", CurrentRun)
    monkeypatch.setattr(
        launcher.LoggedRunner,
        "run",
        lambda *_args, **_kwargs: pytest.fail("must not issue historic recovery commands"),
    )

    assert launcher.main([]) == exit_code
    assert calls == [("provider", RUN_ID), ("run", RUN_ID)]
    assert json.loads((runs_root / RUN_ID / "provider-config.json").read_text())["project"] == config.project
    assert old_evidence.read_text() == '{"result":"failed"}\n'
    if old_marker is None:
        assert not active_path.exists()
    else:
        assert active_path.read_text() == old_marker
    assert not (runs_root / "launcher.lock").exists()
