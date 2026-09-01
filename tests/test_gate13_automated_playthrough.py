from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_automated_playthrough as replay

MODEL_ID = "Qwen3.5 2B"
DIGEST = "sha256:" + "a" * 64


def config_document(root: Path) -> dict:
    executable = root / "CommunityAI.exe"
    executable.write_bytes(b"packaged-desktop")
    archive = root / "communityai-desktop-windows.zip"
    archive.write_bytes(b"verified-production-archive")
    return {
        "schema_version": 1,
        "run_id": "gate13-automated-a",
        "platform": "windows",
        "source_commit": "1" * 40,
        "package_archive": str(archive.resolve()),
        "package_sha256": "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest(),
        "package_bytes": archive.stat().st_size,
        "desktop_executable": str(executable.resolve()),
        "work_root": str((root / ".gate13-playthrough-gate13-automated-a").resolve()),
        "model_id": MODEL_ID,
        "manifest_digest": DIGEST,
        "total_blocks": 24,
        "policy": {
            "sharing_enabled": True,
            "allowed_models": [MODEL_ID],
            "preferred_models": [MODEL_ID],
            "denied_models": [],
            "max_disk_space": "32GB",
            "max_vram": "20GB",
            "max_bandwidth_mbps": 100.0,
            "max_power_watts": None,
            "pause_timeout": 120.0,
            "schedule": {
                "timezone": "UTC",
                "windows": [
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                        "start": "00:00",
                        "end": "23:59",
                    }
                ],
            },
        },
        "session_timeout_seconds": 30.0,
        "inference_timeout_seconds": 10.0,
    }


def session_evidence(plan: dict) -> dict:
    stage = plan["stage"]
    return {
        "schema_version": 1,
        "scope": "gate13-packaged-desktop-playthrough",
        "run_id": plan["run_id"],
        "stage": stage,
        "result": "passed",
        "model_id": plan["model_id"],
        "manifest_digest": plan["manifest_digest"],
        "duration_seconds": 1.25,
        "route": {
            "rendered_in_real_window": True,
            "complete": True,
            "covered_blocks": plan["total_blocks"],
            "total_blocks": plan["total_blocks"],
        },
        "inference": {
            "passed": True,
            "model_id": plan["model_id"],
            "manifest_digest": plan["manifest_digest"],
            "completion_count": 1,
            "generated_token_count": 1,
            "response_content_retained": False,
            "token_identifiers_retained": False,
            "temporary_key_removed": True,
        },
        "ui": {
            "real_window_opened": True,
            "policy_dialog_saved": stage == "start",
            "start_clicked": stage == "start",
            "sharing_running_observed": stage == "start",
            "resumed_after_restart_observed": stage == "resume_pause",
            "pause_clicked": stage == "resume_pause",
            "sharing_paused_observed": stage == "resume_pause",
        },
        "limits": {
            "storage": True,
            "memory_or_vram": True,
            "bandwidth": True,
            "power": False,
            "pause_timeout": True,
            "schedule": True,
        },
        "privacy": {
            "prompt_retained": False,
            "response_content_retained": False,
            "token_identifiers_retained": False,
            "credentials_retained": False,
            "paths_retained": False,
            "endpoints_retained": False,
        },
    }


def test_replay_runs_real_desktop_contract_twice_and_removes_temporaries(tmp_path):
    document = config_document(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    config = replay.load_config(config_path)
    stages = []
    self_tests = []

    def runner(argv, **kwargs):
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        if "--gate13-ui-playthrough" not in argv:
            self_tests.append(argv[1])
            return subprocess.CompletedProcess(argv, 0)
        plan_path = Path(argv[argv.index("--gate13-ui-playthrough") + 1])
        evidence_path = Path(argv[argv.index("--gate13-ui-evidence") + 1])
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        stages.append(plan["stage"])
        evidence_path.write_text(json.dumps(session_evidence(plan)), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = replay.run_replay(config, runner=runner)

    assert stages == ["start", "resume_pause"]
    assert self_tests == ["--check-runtime", "--self-test", "--ui-self-test", "--onboarding-ui-self-test"]
    assert result["result"] == "passed"
    assert result["real_window_sessions"] == 2
    assert result["localhost_inference_count"] == 2
    assert result["restart_resume_observed"] is True
    assert result["policy_profile"] == replay.POLICY_PROFILE
    assert result["qualification_temporaries_removed"] is True
    assert not config.work_root.exists()


def test_config_and_session_evidence_fail_closed(tmp_path):
    document = config_document(tmp_path)
    document["work_root"] = str((tmp_path / "wrong-root").resolve())
    config_path = tmp_path / "invalid.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay.load_config(config_path)

    invalid_power = config_document(tmp_path)
    invalid_power["policy"]["max_power_watts"] = 250.0
    invalid_power_path = tmp_path / "invalid-power.json"
    invalid_power_path.write_text(json.dumps(invalid_power), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay.load_config(invalid_power_path)

    valid = config_document(tmp_path)
    valid_path = tmp_path / "valid.json"
    valid_path.write_text(json.dumps(valid), encoding="utf-8")
    config = replay.load_config(valid_path)
    config.work_root.mkdir()
    evidence = session_evidence({**valid, "stage": "start"})
    evidence["ui"]["start_clicked"] = False
    evidence_path = config.work_root / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay._validate_session(evidence_path, config, "start")

    evidence = session_evidence({**valid, "stage": "start"})
    evidence["inference"]["generated_token_count"] = 2
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay._validate_session(evidence_path, config, "start")
