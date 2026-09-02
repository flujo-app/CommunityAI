from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_hardware_acceptance as acceptance  # noqa: E402

RUN_ID = "gate14-20260902-a"
CONTROLLER_SOURCE = "2" * 40
SOURCE = CONTROLLER_SOURCE
DIGEST = "sha256:" + "a" * 64
PROJECT = "community-ai-506321"
ZONE = "us-central1-a"
INSTANCES = ("gate14-20260902-a-windows", "gate14-20260902-a-linux")
DISKS = ("gate14-20260902-a-windows-disk", "gate14-20260902-a-linux-disk")
CHALLENGE_SHA256 = "sha256:" + "c" * 64
CHALLENGE_ISSUED = 2_000_000_000
CHALLENGE_EXPIRES = CHALLENGE_ISSUED + 900


def provider_plan_document() -> dict:
    return {
        "project": PROJECT,
        "zone": ZONE,
        "clients": [
            {
                "platform": platform,
                "instance": INSTANCES[index],
                "disk": DISKS[index],
                "source_commit": SOURCE,
                "termination_unix": 2_000_010_000 + index,
                "package_sha256": DIGEST,
                "model_id": acceptance.EXPECTED_PLATFORM_MODELS[platform],
                "manifest_digest": acceptance.MODEL_PROFILES[acceptance.EXPECTED_PLATFORM_MODELS[platform]][
                    "manifest_digest"
                ],
                "machine_type": "g2-standard-8",
                "image_project": "windows-cloud" if platform == "windows" else "ubuntu-os-cloud",
                "image": (
                    "windows-server-2022-dc-v20260814" if platform == "windows" else "ubuntu-2404-noble-amd64-v20260826"
                ),
                "boot_disk_gib": 100,
                "boot_disk_type": "pd-balanced",
                "service_account_disabled": True,
                "max_run_seconds": 7_200,
                "termination_action": "DELETE",
            }
            for index, platform in enumerate(("windows", "linux"))
        ],
        "sequencing": {
            "clients_may_run_concurrently": False,
            "windows_first": True,
            "fresh_host_per_platform": True,
        },
    }


PROVIDER_PLAN = provider_plan_document()
PLAN_DIGEST = (
    "sha256:"
    + hashlib.sha256(json.dumps(PROVIDER_PLAN, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
)


def authorization_document() -> dict:
    return {
        "schema_version": 1,
        "gate": 14,
        "result": "authorized",
        "run_id": RUN_ID,
        "source_commit": CONTROLLER_SOURCE,
        "provider_plan_digest": PLAN_DIGEST,
        "provider_plan": PROVIDER_PLAN,
        "authorization": {
            "combined_cloud_ceiling_usd": "100.00",
            "ledger_committed_before_run_usd": "56.00",
            "maximum_estimate_usd": "44.00",
            "remaining_after_run_maximum_usd": "0.00",
            "reservation_recorded": True,
            "native_auth_revalidated": True,
            "provisioning_authorized_after_fail_closed_preflight": True,
        },
        "prohibited": {"credits": 0, "macos": 0, "fly_gpu": 0},
    }


def platform_document(platform: str) -> dict:
    model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[model_id]
    return {
        "schema_version": 1,
        "scope": acceptance.PLATFORM_SCOPE,
        "run_id": RUN_ID,
        "platform": platform,
        "result": "passed",
        "source_commit": SOURCE,
        "gate13_evidence_sha256": acceptance.EXPECTED_GATE13_EVIDENCE_SHA256,
        "package": {
            "source_commit": SOURCE,
            "archive_sha256": DIGEST,
            "archive_bytes": 1024,
            "release_metadata_sha256": "sha256:" + "b" * 64,
        },
        "model": {
            "id": model_id,
            "manifest_digest": profile["manifest_digest"],
            "revision_commit": profile["revision_commit"],
            "gate9_envelope_sha256": acceptance.EXPECTED_GATE9_ENVELOPES[platform],
            "selected_artifact_count": profile["selected_artifact_count"],
            "selected_artifact_bytes": profile["selected_artifact_bytes"],
            "total_blocks": profile["total_blocks"],
        },
        "hardware": {
            "os_name": "Windows Server 2022" if platform == "windows" else "Ubuntu 24.04",
            "accelerator": "NVIDIA L4",
            "accelerator_count": 1,
            "accelerator_memory_bytes": 24 * 1024**3,
        },
        "cache": {
            "verified_bytes_before": profile["selected_artifact_bytes"],
            "verified_bytes_after": profile["selected_artifact_bytes"],
            "transfer_bytes_during_gate": 0,
            "digest_mismatch_count": 0,
            "forbidden_model_acquired": False,
        },
        "placement": {
            "automatic": True,
            "worker_count": 1,
            "block_start": 0,
            "block_end": min(4, profile["total_blocks"]),
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
        "calibration_challenge": {
            "challenge_sha256": CHALLENGE_SHA256,
            "controller_state_revision": 2,
            "issued_at_unix": CHALLENGE_ISSUED,
            "expires_at_unix": CHALLENGE_EXPIRES,
        },
        "suspensions": [
            {
                "kind": kind,
                "suspended": True,
                "resumed": True,
                "desired_intent_preserved": True,
                "worker_count_during": 0,
                "duration_seconds": 2.5,
                "calibration": {
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
                    "configured_limit": {
                        "bandwidth": 100.0,
                        "power": 250.0,
                        "schedule": 0.5,
                    }[kind],
                    "trigger_value": 0.0 if kind == "schedule" else (120.0 if kind == "bandwidth" else 275.0),
                    "resume_value": 1.0 if kind == "schedule" else 10.0,
                    "challenge_sha256": CHALLENGE_SHA256,
                    "sample_started_at_unix": CHALLENGE_ISSUED + 10,
                    "sample_ended_at_unix": CHALLENGE_ISSUED + 12,
                },
            }
            for kind in ("bandwidth", "power", "schedule")
        ],
        "recovery": {
            "worker_crash_observed": True,
            "worker_restarted": True,
            "restart_seconds": 4.5,
            "previous_worker_absent": True,
            "manifest_unchanged": True,
            "automatic_block_range_valid": True,
            "desired_intent_preserved": True,
        },
        "pause": {
            "requested": True,
            "completed": True,
            "duration_seconds": 1.5,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        },
        "restart": {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": True,
            "worker_resumed": True,
            "duration_seconds": 8.0,
            "cache_reused": True,
        },
        "unsupported_telemetry": {
            "device": "cpu",
            "configured_limit": "power_watts",
            "start_rejected": True,
            "reason_code": "power-telemetry-unavailable",
            "private_detail_retained": False,
        },
        "privacy": {
            "prompt_retained": False,
            "response_retained": False,
            "token_identifiers_retained": False,
            "credentials_retained": False,
            "paths_retained": False,
            "endpoints_retained": False,
            "provider_output_retained": False,
        },
        "qualification_temporaries_removed": True,
    }


def cleanup_document(terminal_state_sha256: str) -> dict:
    return {
        "schema_version": 1,
        "scope": acceptance.CLEANUP_SCOPE,
        "run_id": RUN_ID,
        "result": "passed",
        "provider": "GCP",
        "controller_source_commit": CONTROLLER_SOURCE,
        "provider_plan_digest": PLAN_DIGEST,
        "project": PROJECT,
        "zone": ZONE,
        "deleted_instances": list(INSTANCES),
        "deleted_disks": list(DISKS),
        "controller_terminal_state_sha256": terminal_state_sha256,
        "native_auth_revalidated": True,
        "expected_instances": 2,
        "remaining_instances": 0,
        "expected_disks": 2,
        "remaining_disks": 0,
        "remaining_firewalls": 0,
        "l4_usage": 0,
        "protected_bootstrap_running": True,
        "product_processes_remaining": 0,
        "temporary_credentials_remaining": 0,
    }


def write_documents(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    windows_path = tmp_path / "windows.json"
    linux_path = tmp_path / "linux.json"
    cleanup_path = tmp_path / "cleanup.json"
    terminal_state_path = tmp_path / "state.json"
    authorization_path = tmp_path / "authorization.json"
    windows_path.write_text(json.dumps(platform_document("windows")), encoding="utf-8")
    linux_path.write_text(json.dumps(platform_document("linux")), encoding="utf-8")
    authorization_path.write_text(json.dumps(authorization_document()), encoding="utf-8")
    windows_digest = "sha256:" + hashlib.sha256(windows_path.read_bytes()).hexdigest()
    linux_digest = "sha256:" + hashlib.sha256(linux_path.read_bytes()).hexdigest()
    authorization_digest = "sha256:" + hashlib.sha256(authorization_path.read_bytes()).hexdigest()
    terminal_state = {
        "schema_version": 1,
        "run_id": RUN_ID,
        "authorization_sha256": authorization_digest,
        "provider_plan_digest": PLAN_DIGEST,
        "revision": 10,
        "phase": "CLEANED_PASS",
        "failure_code": None,
        "windows_evidence_digest": windows_digest,
        "linux_evidence_digest": linux_digest,
        "windows_challenge_sha256": CHALLENGE_SHA256,
        "linux_challenge_sha256": CHALLENGE_SHA256,
        "windows_challenge_consumed": True,
        "linux_challenge_consumed": True,
        "windows_consumed": True,
        "linux_consumed": True,
        "cleanup_verified": True,
        "next_action": "none",
    }
    terminal_state_path.write_text(json.dumps(terminal_state), encoding="utf-8")
    terminal_digest = "sha256:" + hashlib.sha256(terminal_state_path.read_bytes()).hexdigest()
    cleanup_path.write_text(
        json.dumps(cleanup_document(terminal_digest)),
        encoding="utf-8",
    )
    return windows_path, linux_path, cleanup_path, terminal_state_path, authorization_path


def validate_documents(paths: tuple[Path, Path, Path, Path, Path]) -> dict:
    windows, linux, cleanup, terminal_state, authorization = paths
    return acceptance.validate_files(
        windows,
        linux,
        cleanup,
        CONTROLLER_SOURCE,
        provider_plan_digest=PLAN_DIGEST,
        project=PROJECT,
        zone=ZONE,
        expected_instances=INSTANCES,
        expected_disks=DISKS,
        terminal_state_path=terminal_state,
        authorization_path=authorization,
    )


def test_validate_platform_documents_cover_both_models_and_hardware_contract():
    windows = acceptance.validate_platform_document(platform_document("windows"))
    linux = acceptance.validate_platform_document(platform_document("linux"))

    assert windows["model_id"] == "Qwen3.5 2B"
    assert linux["model_id"] == "Gemma 4 E2B IT"
    assert windows["accelerator"] == linux["accelerator"] == "NVIDIA L4"
    assert windows["block_start"] == 0
    assert windows["block_end"] == 4


def test_validate_files_emits_digest_bound_privacy_safe_aggregate(tmp_path):
    paths = write_documents(tmp_path)

    result = validate_documents(paths)

    assert result["scope"] == acceptance.AGGREGATE_SCOPE
    assert result["result"] == "passed"
    assert result["controller_source_commit"] == CONTROLLER_SOURCE
    assert result["package_source_commit"] == SOURCE
    assert [item["platform"] for item in result["platforms"]] == ["windows", "linux"]
    assert all(item["evidence_sha256"].startswith("sha256:") for item in result["platforms"])
    assert result["cleanup"]["resource_absence_proved"] is True
    assert result["credits_in_scope"] is False
    assert result["macos_in_scope"] is False
    assert result["privacy_safe"] is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda value: value.update(gate13_evidence_sha256="sha256:" + "0" * 64),
        lambda value: value["model"].update(gate9_envelope_sha256="sha256:" + "0" * 64),
        lambda value: value["hardware"].update(os_name="Ubuntu 24.04"),
        lambda value: value["cache"].update(transfer_bytes_during_gate=1),
        lambda value: value["placement"].update(remote_acknowledged=False),
        lambda value: value["limits"].update(low_vram_rejected=False),
        lambda value: value["limits"].update(power_watts=None),
        lambda value: value["suspensions"].pop(),
        lambda value: value["suspensions"][0].update(resumed=False),
        lambda value: value["suspensions"][0]["calibration"].update(trigger_value=50.0),
        lambda value: value["suspensions"][1]["calibration"].update(measurement_scope="aggregate-host-network"),
        lambda value: value["recovery"].update(worker_restarted=False),
        lambda value: value["pause"].update(descendant_count_after=1),
        lambda value: value["restart"].update(policy_persisted=False),
        lambda value: value["unsupported_telemetry"].update(start_rejected=False),
        lambda value: value["privacy"].update(paths_retained=True),
        lambda value: value.update(qualification_temporaries_removed=False),
    ],
)
def test_platform_evidence_fails_closed(mutator):
    value = platform_document("windows")
    mutator(value)

    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(value)


def test_wrong_model_and_unsafe_block_range_fail_closed():
    value = platform_document("windows")
    value["model"] = platform_document("linux")["model"]
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(value)

    value = platform_document("windows")
    value["placement"]["block_end"] = value["placement"]["block_start"]
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(value)


def test_calibration_requires_challenge_bound_bounded_sample_windows():
    missing_timestamp = platform_document("windows")
    missing_timestamp["suspensions"][0]["calibration"].pop("sample_started_at_unix")
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(missing_timestamp)

    wrong_challenge = platform_document("windows")
    wrong_challenge["suspensions"][0]["calibration"]["challenge_sha256"] = "sha256:" + "d" * 64
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(wrong_challenge)

    stale = platform_document("windows")
    stale["suspensions"][0]["calibration"]["sample_ended_at_unix"] = CHALLENGE_EXPIRES + 1
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(stale)

    oversized = platform_document("windows")
    oversized["suspensions"][0]["calibration"]["sample_ended_at_unix"] = CHALLENGE_ISSUED + 131
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_platform_document(oversized)


def test_aggregate_rejects_mismatched_run_source_and_incomplete_cleanup(tmp_path):
    paths = write_documents(tmp_path)
    linux = paths[1]
    linux_value = json.loads(linux.read_text(encoding="utf-8"))
    linux_value["run_id"] = "gate14-20260902-b"
    linux.write_text(json.dumps(linux_value), encoding="utf-8")
    with pytest.raises(acceptance.Gate14EvidenceError):
        validate_documents(paths)

    paths = write_documents(tmp_path)
    cleanup = paths[2]
    cleanup_value = json.loads(cleanup.read_text(encoding="utf-8"))
    cleanup_value["remaining_disks"] = 1
    cleanup.write_text(json.dumps(cleanup_value), encoding="utf-8")
    with pytest.raises(acceptance.Gate14EvidenceError):
        validate_documents(paths)


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("controller_source_commit", "3" * 40),
        ("provider_plan_digest", "sha256:" + "3" * 64),
        ("project", "different-project"),
        ("zone", "us-east1-b"),
        ("deleted_instances", list(reversed(INSTANCES))),
        ("deleted_disks", list(reversed(DISKS))),
        ("controller_terminal_state_sha256", "sha256:" + "3" * 64),
    ],
)
def test_cleanup_must_bind_exact_plan_resources_and_terminal_state(
    tmp_path,
    field,
    replacement,
):
    paths = write_documents(tmp_path)
    cleanup = paths[2]
    value = json.loads(cleanup.read_text(encoding="utf-8"))
    value[field] = replacement
    cleanup.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(acceptance.Gate14EvidenceError):
        validate_documents(paths)


def test_terminal_state_must_be_a_real_digest_bound_pass(tmp_path):
    paths = write_documents(tmp_path)
    cleanup = paths[2]
    terminal_state = paths[3]
    state_value = json.loads(terminal_state.read_text(encoding="utf-8"))
    state_value["cleanup_verified"] = False
    terminal_state.write_text(json.dumps(state_value), encoding="utf-8")
    terminal_digest = "sha256:" + hashlib.sha256(terminal_state.read_bytes()).hexdigest()
    cleanup_value = json.loads(cleanup.read_text(encoding="utf-8"))
    cleanup_value["controller_terminal_state_sha256"] = terminal_digest
    cleanup.write_text(json.dumps(cleanup_value), encoding="utf-8")

    with pytest.raises(acceptance.Gate14EvidenceError):
        validate_documents(paths)


def test_protected_bootstrap_cannot_enter_cleanup_inventory(tmp_path):
    windows, linux, cleanup, terminal_state, authorization = write_documents(tmp_path)

    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance.validate_files(
            windows,
            linux,
            cleanup,
            CONTROLLER_SOURCE,
            provider_plan_digest=PLAN_DIGEST,
            project=PROJECT,
            zone=ZONE,
            expected_instances=(acceptance.PROTECTED_INSTANCE, INSTANCES[1]),
            expected_disks=DISKS,
            terminal_state_path=terminal_state,
            authorization_path=authorization,
        )


def test_terminal_state_binds_exact_semantic_authorization_file(tmp_path):
    paths = write_documents(tmp_path)
    cleanup = paths[2]
    terminal_state = paths[3]
    authorization = paths[4]
    authorization_value = json.loads(authorization.read_text(encoding="utf-8"))
    authorization_value["source_commit"] = "3" * 40
    authorization.write_text(json.dumps(authorization_value), encoding="utf-8")
    authorization_digest = "sha256:" + hashlib.sha256(authorization.read_bytes()).hexdigest()

    state_value = json.loads(terminal_state.read_text(encoding="utf-8"))
    state_value["authorization_sha256"] = authorization_digest
    terminal_state.write_text(json.dumps(state_value), encoding="utf-8")
    terminal_digest = "sha256:" + hashlib.sha256(terminal_state.read_bytes()).hexdigest()
    cleanup_value = json.loads(cleanup.read_text(encoding="utf-8"))
    cleanup_value["controller_terminal_state_sha256"] = terminal_digest
    cleanup.write_text(json.dumps(cleanup_value), encoding="utf-8")

    with pytest.raises(acceptance.Gate14EvidenceError):
        validate_documents(paths)


def test_duplicate_and_non_finite_json_fail_closed():
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance._strict_json(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(acceptance.Gate14EvidenceError):
        acceptance._strict_json(b'{"value":NaN}')


def test_cli_prints_canonical_aggregate(tmp_path):
    windows, linux, cleanup, terminal_state, authorization = write_documents(tmp_path)

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gate14_hardware_acceptance.py"),
            "--windows",
            str(windows),
            "--linux",
            str(linux),
            "--cleanup",
            str(cleanup),
            "--controller-state",
            str(terminal_state),
            "--authorization",
            str(authorization),
            "--controller-source-commit",
            CONTROLLER_SOURCE,
            "--provider-plan-digest",
            PLAN_DIGEST,
            "--project",
            PROJECT,
            "--zone",
            ZONE,
            "--instances",
            *INSTANCES,
            "--disks",
            *DISKS,
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["result"] == "passed"
    assert completed.stdout.strip() == json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
    )
