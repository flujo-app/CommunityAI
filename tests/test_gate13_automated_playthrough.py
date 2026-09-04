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


def config_document(root: Path, platform: str = "windows") -> dict:
    executable = root / ("CommunityAI.exe" if platform == "windows" else "CommunityAI")
    executable.write_bytes(b"packaged-desktop")
    archive = root / f"communityai-desktop-{platform}.zip"
    archive.write_bytes(b"verified-production-archive")
    return {
        "schema_version": 2,
        "run_id": "gate13-automated-a",
        "platform": platform,
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
    platform = plan["platform"]
    inference_required = (platform, stage) in {
        ("windows", "initial"),
        ("linux", "initial"),
        ("linux", "restart"),
    }
    policy_session = (platform, stage) in {
        ("windows", "restart"),
        ("linux", "initial"),
    }
    resumed_session = platform == "linux" and stage == "restart"
    pause_session = stage == "restart"
    return {
        "schema_version": 2,
        "scope": "gate13-packaged-desktop-playthrough",
        "run_id": plan["run_id"],
        "platform": platform,
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
        }
        if inference_required
        else None,
        "ui": {
            "real_window_opened": True,
            "policy_dialog_saved": policy_session,
            "start_clicked": policy_session,
            "pause_control_observed": policy_session or resumed_session,
            "pause_clicked": pause_session,
            "restart_resume_observed": resumed_session,
            "sharing_intent_enabled_observed": policy_session or resumed_session,
            "sharing_intent_disabled_observed": pause_session,
        },
        "limits": {
            "storage": policy_session,
            "memory_or_vram": policy_session,
            "bandwidth": policy_session,
            "power": False,
            "pause_timeout": policy_session,
            "schedule": policy_session,
        },
        "timing": {
            "start_observation_seconds": 25.0
            if platform == "windows" and stage == "restart"
            else (20.0 if platform == "linux" and stage == "initial" else 0.0),
            "restart_observation_seconds": 15.0 if resumed_session else 0.0,
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


@pytest.mark.parametrize("platform,expected_inferences,expected_resume", [("windows", 1, False), ("linux", 2, True)])
def test_replay_runs_real_desktop_contract_twice_and_removes_temporaries(
    tmp_path, platform, expected_inferences, expected_resume
):
    document = config_document(tmp_path, platform)
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

    assert stages == ["initial", "restart"]
    assert self_tests == ["--check-runtime", "--self-test", "--ui-self-test", "--onboarding-ui-self-test"]
    assert result["result"] == "passed"
    assert result["real_window_sessions"] == 2
    assert result["localhost_inference_count"] == expected_inferences
    assert result["restart_resume_observed"] is expected_resume
    assert result["pause_control_observed"] is True
    assert result["sharing_intent_paused"] is True
    assert result["sequence_profile"] == replay.SEQUENCE_PROFILES[platform]
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
    evidence = session_evidence({**valid, "stage": "restart"})
    evidence["ui"]["start_clicked"] = False
    evidence_path = config.work_root / "evidence.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay._validate_session(evidence_path, config, "restart")

    evidence = session_evidence({**valid, "stage": "initial"})
    evidence["inference"]["generated_token_count"] = 2
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    with pytest.raises(replay.ReplayError):
        replay._validate_session(evidence_path, config, "initial")


def run_main_with_runner(tmp_path, monkeypatch, capsys, runner, platform="windows"):
    document = config_document(tmp_path, platform)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    real_replay = replay.run_replay
    monkeypatch.setattr(replay, "run_replay", lambda config: real_replay(config, runner=runner))
    assert replay.main(["--config", str(config_path)]) == 1
    return document, json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("action", ["--check-runtime", "--self-test", "--ui-self-test", "--onboarding-ui-self-test"])
@pytest.mark.parametrize("timed_out", [False, True])
def test_self_test_failure_reports_the_exact_action_and_process_outcome(
    tmp_path, monkeypatch, capsys, action, timed_out
):
    actions = []

    def runner(argv, **kwargs):
        actions.append(argv[1])
        if argv[1] == action:
            if timed_out:
                raise subprocess.TimeoutExpired(
                    argv, kwargs["timeout"], output="private prompt", stderr="private secret"
                )
            return subprocess.CompletedProcess(argv, 37, stdout="private prompt", stderr="private secret")
        return subprocess.CompletedProcess(argv, 0)

    document, failure = run_main_with_runner(tmp_path, monkeypatch, capsys, runner)

    assert actions[-1] == action
    assert failure["failed_step"] == action
    assert failure["error_category"] == ("process_timeout" if timed_out else "process_exit")
    assert failure["timeout_seconds" if timed_out else "exit_code"] == (120 if timed_out else 37)
    assert "private" not in json.dumps(failure)
    assert not Path(document["work_root"]).exists()


@pytest.mark.parametrize("platform", ["windows", "linux"])
@pytest.mark.parametrize("stage", ["initial", "restart"])
@pytest.mark.parametrize("outcome", ["exit", "timeout", "failed_evidence"])
def test_session_failure_evidence_survives_temporary_cleanup(tmp_path, monkeypatch, capsys, platform, stage, outcome):
    def runner(argv, **kwargs):
        if "--gate13-ui-playthrough" not in argv:
            return subprocess.CompletedProcess(argv, 0)
        plan = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
        evidence_path = Path(argv[4])
        evidence = session_evidence(plan)
        if plan["stage"] == stage:
            evidence = {
                key: item
                for key, item in evidence.items()
                if key not in ("route", "inference", "ui", "limits", "timing", "privacy")
            }
            evidence.update(
                result="failed",
                failure_code="inference_failed",
                duration_seconds=29.25,
                failure_phase="wait_ready",
                failure_detail="inference_http_503",
            )
        else:
            # Even unknown fields inside accepted inference evidence must not
            # become retained diagnostics when a later session fails.
            evidence["inference"]["unrecognized_private_value"] = "private model output"
        evidence["unrecognized_private_value"] = "private credential" if plan["stage"] == stage else None
        if plan["stage"] != stage:
            del evidence["unrecognized_private_value"]
        evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
        if plan["stage"] == stage:
            if outcome == "timeout":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"], stderr="private stderr")
            return subprocess.CompletedProcess(argv, 19 if outcome == "exit" else 0)
        return subprocess.CompletedProcess(argv, 0)

    document, failure = run_main_with_runner(tmp_path, monkeypatch, capsys, runner, platform)

    assert failure["failed_step"] == f"{stage}_session"
    assert (
        failure["error_category"]
        == {"exit": "process_exit", "timeout": "process_timeout", "failed_evidence": "session_evidence_invalid"}[
            outcome
        ]
    )
    if outcome == "exit":
        assert failure["exit_code"] == 19
    elif outcome == "timeout":
        assert failure["timeout_seconds"] == 90
    retained = failure["session_evidence"][stage]
    assert retained["result"] == "failed"
    assert retained["failure_code"] == "inference_failed"
    assert retained["failure_phase"] == "wait_ready"
    assert retained["failure_detail"] == "inference_http_503"
    assert retained["duration_seconds"] == 29.25
    if stage == "restart":
        assert failure["session_evidence"]["initial"]["result"] == "passed"
        assert failure["session_evidence"]["initial"]["inference"]["passed"] is True
    assert "private" not in json.dumps(failure)
    assert failure["qualification_temporaries_removed"] is True
    assert not Path(document["work_root"]).exists()


@pytest.mark.parametrize("result", ["failed", "passed"])
def test_session_diagnostics_drop_arbitrary_failure_text_and_success_only_failure_fields(tmp_path, result):
    document = config_document(tmp_path)
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    config = replay.load_config(config_path)
    evidence = session_evidence({**document, "stage": "initial"})
    evidence.update(
        result=result,
        failure_code="private credential" if result == "failed" else "playthrough_timed_out",
        failure_phase={"private": "prompt"} if result == "failed" else "wait_ready",
        failure_detail="inference_http_503 private response" if result == "failed" else "inference_timed_out",
    )
    evidence_path = tmp_path / "session.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    retained = replay._session_diagnostic(evidence_path, config, "initial")

    assert all(field not in retained for field in ("failure_code", "failure_phase", "failure_detail"))
    assert "private" not in json.dumps(retained)


def test_cleanup_error_does_not_obscure_the_failed_session(tmp_path, monkeypatch, capsys):
    def runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 23 if "--gate13-ui-playthrough" in argv else 0)

    def failed_cleanup(_path):
        raise OSError("private filesystem path")

    monkeypatch.setattr(replay.shutil, "rmtree", failed_cleanup)
    document, failure = run_main_with_runner(tmp_path, monkeypatch, capsys, runner)

    assert failure["failed_step"] == "initial_session"
    assert failure["exit_code"] == 23
    assert failure["error_category"] == "process_exit"
    assert failure["session_evidence_error"] == "required file is unavailable"
    assert failure["cleanup_failure_code"] == "qualification_temporary_cleanup_failed"
    assert failure["qualification_temporaries_removed"] is False
    assert Path(document["work_root"]).exists()
    assert "private" not in json.dumps(failure)
