from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_calibration_challenge as challenge_contract  # noqa: E402
import gate14_hardware_acceptance as acceptance  # noqa: E402
import gate14_host_probe as probe  # noqa: E402

SOURCE = "1" * 40
RUN_ID = "gate14-20260902-a"
CHALLENGE_ISSUED = 2_000_000_000


def calibration_challenge(platform: str, package_payload: bytes) -> dict:
    return dict(
        challenge_contract.create(
            run_id=RUN_ID,
            platform=platform,
            source_commit=SOURCE,
            package_sha256="sha256:" + hashlib.sha256(package_payload).hexdigest(),
            checkpoint_sha256="sha256:" + "c" * 64,
            controller_state_revision=2,
            issued_at_unix=CHALLENGE_ISSUED,
            nonce="a" * 64,
        )
    )


def calibration(kind: str, challenge_sha256: str) -> dict:
    return {
        "measurement_source": {
            "bandwidth": "host-network-counters",
            "power": "nvidia-nvml-device-power",
            "schedule": "utc-policy-clock",
        }[kind],
        "measurement_scope": {
            "bandwidth": "aggregate-host-network",
            "power": "selected-nvidia-l4-device",
            "schedule": "utc-schedule-policy",
        }[kind],
        "sample_count": 4,
        "sample_interval_seconds": 0.5,
        "baseline_value": 1.0 if kind == "schedule" else 10.0,
        "configured_limit": {"bandwidth": 100.0, "power": 250.0, "schedule": 0.5}[kind],
        "trigger_value": 0.0 if kind == "schedule" else (120.0 if kind == "bandwidth" else 275.0),
        "resume_value": 1.0 if kind == "schedule" else 10.0,
        "challenge_sha256": challenge_sha256,
        "sample_started_at_unix": CHALLENGE_ISSUED + 10,
        "sample_ended_at_unix": CHALLENGE_ISSUED + 12,
    }


def facts(platform: str, package_payload: bytes) -> dict:
    challenge_sha256 = challenge_contract.digest(calibration_challenge(platform, package_payload))
    model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[model_id]
    selected = profile["selected_artifact_bytes"]
    return {
        "schema_version": 1,
        "scope": probe.FACT_SCOPE,
        "run_id": RUN_ID,
        "platform": platform,
        "source_commit": SOURCE,
        "gate13_evidence_sha256": acceptance.EXPECTED_GATE13_EVIDENCE_SHA256,
        "expected_package_sha256": "sha256:" + hashlib.sha256(package_payload).hexdigest(),
        "model": {
            "id": model_id,
            "manifest_digest": profile["manifest_digest"],
            "revision_commit": profile["revision_commit"],
            "gate9_envelope_sha256": acceptance.EXPECTED_GATE9_ENVELOPES[platform],
            "selected_artifact_count": profile["selected_artifact_count"],
            "selected_artifact_bytes": selected,
            "total_blocks": profile["total_blocks"],
        },
        "cache": {
            "verified_bytes_before": selected,
            "verified_bytes_after": selected,
            "transfer_bytes_during_gate": 0,
            "digest_mismatch_count": 0,
            "forbidden_model_acquired": False,
        },
        "placement": {
            "automatic": True,
            "worker_count": 1,
            "block_start": 0,
            "block_end": 4,
            "intent_published": True,
            "remote_acknowledged": True,
        },
        "limits": {
            "disk_bytes": 16 * 1024**3,
            "vram_bytes": 20 * 1024**3,
            "bandwidth_mbps": 100.0,
            "power_watts": 250.0,
            "schedule_timezone": "UTC",
            "resource_limit_count": 5,
            "configured_and_resolved_match": True,
            "low_vram_rejected": True,
        },
        "suspensions": [
            {
                "kind": kind,
                "suspended": True,
                "resumed": True,
                "desired_intent_preserved": True,
                "worker_count_during": 0,
                "duration_seconds": 2.0,
                "calibration": calibration(kind, challenge_sha256),
            }
            for kind in ("bandwidth", "power", "schedule")
        ],
        "recovery": {
            "worker_crash_observed": True,
            "worker_restarted": True,
            "restart_seconds": 3.0,
            "previous_worker_absent": True,
            "manifest_unchanged": True,
            "automatic_block_range_valid": True,
            "desired_intent_preserved": True,
        },
        "pause": {
            "requested": True,
            "completed": True,
            "duration_seconds": 2.0,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        },
        "restart": {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": True,
            "worker_resumed": True,
            "duration_seconds": 5.0,
            "cache_reused": True,
        },
        "unsupported_telemetry": {
            "device": "cpu",
            "configured_limit": "power_watts",
            "start_rejected": True,
            "reason_code": "power-telemetry-unavailable",
            "private_detail_retained": False,
        },
        "qualification_temporaries_removed": True,
    }


def hardware(platform: str) -> dict:
    return {
        "os_name": "Windows Server 2022" if platform == "windows" else "Ubuntu 24.04",
        "accelerator": "NVIDIA L4",
        "accelerator_count": 1,
        "accelerator_memory_bytes": 24 * 1024**3,
    }


def write_inputs(tmp_path: Path, platform: str = "windows"):
    package_payload = b"source-bound-production-package"
    package = tmp_path / "package.zip"
    metadata = tmp_path / "release-metadata.json"
    facts_path = tmp_path / "facts.json"
    challenge_path = tmp_path / "challenge.json"
    output = tmp_path / "evidence.json"
    package.write_bytes(package_payload)
    metadata.write_text('{"schema_version":1}', encoding="utf-8")
    facts_path.write_text(json.dumps(facts(platform, package_payload)), encoding="utf-8")
    challenge_path.write_text(
        json.dumps(calibration_challenge(platform, package_payload)),
        encoding="utf-8",
    )
    return facts_path, challenge_path, package, metadata, output


def test_probe_hashes_inputs_measures_hardware_and_emits_only_safe_evidence(tmp_path):
    facts_path, challenge_path, package, metadata, output = write_inputs(tmp_path)

    document = probe.run_probe(
        platform_name="windows",
        facts_path=facts_path,
        challenge_path=challenge_path,
        package_path=package,
        release_metadata_path=metadata,
        output_path=output,
        hardware_probe=hardware,
        now_unix=CHALLENGE_ISSUED + 20,
    )

    assert acceptance.validate_platform_document(document)["platform"] == "windows"
    assert document["package"]["archive_bytes"] == package.stat().st_size
    assert document["hardware"]["accelerator"] == "NVIDIA L4"
    assert all(value is False for value in document["privacy"].values())
    assert json.loads(output.read_text(encoding="utf-8")) == document


def test_probe_rejects_package_drift_and_does_not_publish(tmp_path):
    facts_path, challenge_path, package, metadata, output = write_inputs(tmp_path)
    package.write_bytes(b"changed")

    with pytest.raises(probe.Gate14ProbeError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED + 20,
        )

    assert not output.exists()


def test_probe_rejects_uncalibrated_physical_trigger(tmp_path):
    facts_path, challenge_path, package, metadata, output = write_inputs(tmp_path)
    value = json.loads(facts_path.read_text(encoding="utf-8"))
    value["suspensions"][0]["calibration"]["trigger_value"] = 50.0
    facts_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(acceptance.Gate14EvidenceError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED + 20,
        )


def test_probe_rejects_expired_or_mismatched_challenge(tmp_path):
    facts_path, challenge_path, package, metadata, output = write_inputs(tmp_path)

    with pytest.raises(challenge_contract.Gate14ChallengeError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED - 1,
        )
    assert not output.exists()

    with pytest.raises(probe.Gate14ProbeError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED,
        )
    assert not output.exists()

    with pytest.raises(challenge_contract.Gate14ChallengeError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED + challenge_contract.MAX_LIFETIME_SECONDS + 1,
        )
    assert not output.exists()

    value = json.loads(facts_path.read_text(encoding="utf-8"))
    value["suspensions"][0]["calibration"]["sample_started_at_unix"] = CHALLENGE_ISSUED + 49
    value["suspensions"][0]["calibration"]["sample_ended_at_unix"] = CHALLENGE_ISSUED + 51
    facts_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(probe.Gate14ProbeError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED + 20,
        )
    assert not output.exists()

    package_payload = package.read_bytes()
    value = facts("windows", package_payload)
    value["suspensions"][0]["calibration"]["challenge_sha256"] = "sha256:" + "f" * 64
    facts_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(acceptance.Gate14EvidenceError):
        probe.run_probe(
            platform_name="windows",
            facts_path=facts_path,
            challenge_path=challenge_path,
            package_path=package,
            release_metadata_path=metadata,
            output_path=output,
            hardware_probe=hardware,
            now_unix=CHALLENGE_ISSUED + 20,
        )
    assert not output.exists()


def test_probe_hardware_requires_one_l4(monkeypatch):
    monkeypatch.setattr(probe, "_operating_system", lambda platform: "Ubuntu 24.04")

    def runner(argv, timeout):
        assert tuple(argv) == (
            "nvidia-smi",
            "--query-gpu=name,memory.total",
            "--format=csv,noheader,nounits",
        )
        assert timeout == 30
        return subprocess.CompletedProcess(argv, 0, "NVIDIA L4, 23034\n", "")

    result = probe.probe_hardware("linux", runner=runner)

    assert result["accelerator_count"] == 1
    assert result["accelerator_memory_bytes"] == 23034 * 1024**2


def test_platform_wrappers_fix_their_platform():
    windows = (ROOT / "scripts" / "gate14_windows_probe.ps1").read_text(encoding="utf-8")
    linux = (ROOT / "scripts" / "gate14_linux_probe.sh").read_text(encoding="utf-8")

    assert "--platform windows" in windows
    assert "--platform linux" in linux
    assert "macos" not in windows.casefold() + linux.casefold()
