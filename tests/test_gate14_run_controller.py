from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate14_calibration_challenge as challenge_contract  # noqa: E402
import gate14_hardware_acceptance as acceptance  # noqa: E402
import gate14_run_controller as controller  # noqa: E402

RUN_ID = "gate14-20260902-a"
SOURCE = "1" * 40
NOW = 2_000_000_000
PACKAGE_DIGESTS = {
    "windows": "sha256:" + "a" * 64,
    "linux": "sha256:" + "b" * 64,
}


def challenge_document(platform: str, revision: int = 2) -> dict:
    return dict(
        challenge_contract.create(
            run_id=RUN_ID,
            platform=platform,
            source_commit=SOURCE,
            package_sha256=PACKAGE_DIGESTS[platform],
            controller_state_revision=revision,
            issued_at_unix=NOW,
            nonce=("a" if platform == "windows" else "b") * 64,
        )
    )


def write_challenge(tmp_path: Path, platform: str, value: dict | None = None) -> Path:
    path = tmp_path / f"{platform}-challenge.json"
    path.write_text(json.dumps(value or challenge_document(platform)), encoding="utf-8")
    return path


def provider_plan() -> dict:
    clients = []
    for index, platform in enumerate(("windows", "linux"), start=1):
        model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
        clients.append(
            {
                "platform": platform,
                "instance": f"gate14-20260902-a-{platform}",
                "disk": f"gate14-20260902-a-{platform}-disk",
                "source_commit": SOURCE,
                "termination_unix": NOW + index * 10_000,
                "package_sha256": PACKAGE_DIGESTS[platform],
                "model_id": model_id,
                "manifest_digest": acceptance.MODEL_PROFILES[model_id]["manifest_digest"],
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
        )
    return {
        "project": "community-ai-506321",
        "zone": "us-central1-a",
        "clients": clients,
        "sequencing": {
            "clients_may_run_concurrently": False,
            "windows_first": True,
            "fresh_host_per_platform": True,
        },
    }


def write_plan(
    tmp_path: Path,
    *,
    additional_current_maximum: str | None = None,
    hide_additional_below_anchor: bool = False,
) -> controller.RunPlan:
    provider = provider_plan()
    digest = controller._canonical_digest(provider)
    authorization = {
        "schema_version": 1,
        "gate": 14,
        "result": "authorized",
        "run_id": RUN_ID,
        "source_commit": SOURCE,
        "provider_plan_digest": digest,
        "provider_plan": provider,
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
    authorization_path = tmp_path / "authorization.json"
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
    ledger_path = tmp_path / "ledger.md"
    ledger_lines = [
        "## Cloud authorization and spend ledger",
        "",
        "| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |",
        "| --- | --- | --- | ---: | ---: | --- | --- |",
        f"| {RUN_ID} | GCP | Gate 14 packaged hardware [plan {digest}] | USD 44.00 | — | — | RESERVED |",
    ]
    additional_row = (
        "| gate14-prior-run | GCP | Unexpected same-epoch reservation | "
        f"USD {additional_current_maximum} | — | — | RESERVED |"
        if additional_current_maximum is not None
        else None
    )
    if additional_row is not None and not hide_additional_below_anchor:
        ledger_lines.append(additional_row)
    ledger_lines.append(
        "| gate13-20260901-a | GCP | Current epoch anchor | "
        "USD 56.00 | — | Existing cleanup proof | CLEANED-COMMITTED |"
    )
    if additional_row is not None and hide_additional_below_anchor:
        ledger_lines.append(additional_row)
    ledger_lines.append("")
    ledger_path.write_text("\n".join(ledger_lines), encoding="utf-8")
    return controller.load_plan(authorization_path, ledger_path)


def observation(
    plan: controller.RunPlan,
    *,
    windows: bool = False,
    linux: bool = False,
    windows_job: str = "absent",
    linux_job: str = "absent",
    windows_digest: str | None = None,
    linux_digest: str | None = None,
    windows_disk: bool | None = None,
    linux_disk: bool | None = None,
) -> dict:
    present = {"windows": windows, "linux": linux}
    jobs = {"windows": windows_job, "linux": linux_job}
    digests = {"windows": windows_digest, "linux": linux_digest}
    disks = {
        "windows": windows if windows_disk is None else windows_disk,
        "linux": linux if linux_disk is None else linux_disk,
    }
    return {
        "schema_version": 1,
        "run_id": plan.run_id,
        "observed_at_unix": NOW,
        "instances": {
            client.instance: {
                "present": present[client.platform],
                "run_id": plan.run_id if present[client.platform] else None,
                "source_commit": client.source_commit if present[client.platform] else None,
                "termination_unix": client.termination_unix if present[client.platform] else None,
            }
            for client in (plan.windows, plan.linux)
        },
        "disks": {client.disk: disks[client.platform] for client in (plan.windows, plan.linux)},
        "clients": {
            platform: {
                "job_state": jobs[platform],
                "attempt_ordinal": 1 if jobs[platform] != "absent" else 0,
                "evidence_digest": digests[platform] if jobs[platform] == "passed" else None,
            }
            for platform in ("windows", "linux")
        },
        "l4_usage": int(windows or linux),
        "protected_bootstrap_running": True,
    }


def platform_evidence(platform: str, challenge_value: dict | None = None) -> dict:
    challenge = challenge_value or challenge_document(platform)
    challenge_sha256 = challenge_contract.digest(challenge)
    model_id = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    profile = acceptance.MODEL_PROFILES[model_id]
    selected = profile["selected_artifact_bytes"]
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
            "archive_sha256": PACKAGE_DIGESTS[platform],
            "archive_bytes": 1024,
            "release_metadata_sha256": "sha256:" + "e" * 64,
        },
        "model": {
            "id": model_id,
            "manifest_digest": profile["manifest_digest"],
            "revision_commit": profile["revision_commit"],
            "gate9_envelope_sha256": acceptance.EXPECTED_GATE9_ENVELOPES[platform],
            "selected_artifact_count": profile["selected_artifact_count"],
            "selected_artifact_bytes": selected,
            "total_blocks": profile["total_blocks"],
        },
        "hardware": {
            "os_name": "Windows Server 2022" if platform == "windows" else "Ubuntu 24.04",
            "accelerator": "NVIDIA L4",
            "accelerator_count": 1,
            "accelerator_memory_bytes": 24 * 1024**3,
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
        "calibration_challenge": {
            "challenge_sha256": challenge_sha256,
            "controller_state_revision": challenge["controller_state_revision"],
            "issued_at_unix": challenge["issued_at_unix"],
            "expires_at_unix": challenge["expires_at_unix"],
        },
        "suspensions": [
            {
                "kind": kind,
                "suspended": True,
                "resumed": True,
                "desired_intent_preserved": True,
                "worker_count_during": 0,
                "duration_seconds": 3.0,
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
                    "challenge_sha256": challenge_sha256,
                    "sample_started_at_unix": NOW + 10,
                    "sample_ended_at_unix": NOW + 12,
                },
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
            "duration_seconds": 3.0,
            "worker_count_after": 0,
            "descendant_count_after": 0,
        },
        "restart": {
            "node_restarted": True,
            "policy_persisted": True,
            "desired_intent_persisted": True,
            "worker_resumed": True,
            "duration_seconds": 3.0,
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


@pytest.fixture
def plan(tmp_path):
    return write_plan(tmp_path)


def test_load_plan_binds_budget_sequence_models_and_exact_resources(plan):
    assert plan.run_id == RUN_ID
    assert plan.ledger_state == "RESERVED"
    assert plan.instances == (
        "gate14-20260902-a-windows",
        "gate14-20260902-a-linux",
    )
    assert plan.windows.model_id == "Qwen3.5 2B"
    assert plan.linux.model_id == "Gemma 4 E2B IT"
    assert controller.PROTECTED_INSTANCE not in plan.instances


def test_controller_issues_one_time_source_bound_calibration_challenge(tmp_path, plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    path = tmp_path / "controller-challenge.json"

    next_state, value = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        path,
        issued_at_unix=NOW + 1,
        nonce="f" * 64,
    )

    assert path.exists()
    assert value["run_id"] == plan.run_id
    assert value["platform"] == "windows"
    assert value["source_commit"] == plan.windows.source_commit
    assert value["package_sha256"] == plan.windows.package_sha256
    assert value["controller_state_revision"] == state["revision"]
    assert next_state["windows_challenge_sha256"] == challenge_contract.digest(value)
    recovered_state, recovered = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        path,
        issued_at_unix=NOW + 2,
        nonce="e" * 64,
    )
    assert recovered == value
    assert recovered_state["windows_challenge_sha256"] == next_state["windows_challenge_sha256"]

    path.unlink()
    with pytest.raises(controller.Gate14ControllerError):
        controller.issue_calibration_challenge(
            next_state,
            plan,
            "windows",
            path,
            issued_at_unix=NOW + 2,
            nonce="e" * 64,
        )


def test_load_plan_rejects_spend_above_remaining_ceiling(tmp_path):
    provider = provider_plan()
    digest = controller._canonical_digest(provider)
    authorization = {
        "schema_version": 1,
        "gate": 14,
        "result": "authorized",
        "run_id": RUN_ID,
        "source_commit": SOURCE,
        "provider_plan_digest": digest,
        "provider_plan": provider,
        "authorization": {
            "combined_cloud_ceiling_usd": "100.00",
            "ledger_committed_before_run_usd": "56.00",
            "maximum_estimate_usd": "45.00",
            "remaining_after_run_maximum_usd": "-1.00",
            "reservation_recorded": True,
            "native_auth_revalidated": True,
            "provisioning_authorized_after_fail_closed_preflight": True,
        },
        "prohibited": {"credits": 0, "macos": 0, "fly_gpu": 0},
    }
    authorization_path = tmp_path / "bad.json"
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
    ledger_path = tmp_path / "ledger.md"
    ledger_path.write_text(
        "\n".join(
            (
                "## Cloud authorization and spend ledger",
                "| Run | Provider | Purpose | Maximum estimate | Observed cost | Cleanup proof | State |",
                "| --- | --- | --- | ---: | ---: | --- | --- |",
                f"| {RUN_ID} | GCP | Gate 14 [plan {digest}] | USD 45.00 | — | — | RESERVED |",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(controller.Gate14ControllerError):
        controller.load_plan(authorization_path, ledger_path)


def test_lifecycle_reattaches_collects_serially_and_cleanup_passes(
    tmp_path,
    plan,
):
    state = controller.initial_state(plan)
    assert state["next_action"] == "none"

    state = controller.reconcile(state, observation(plan), plan)
    assert state["phase"] == "ABSENT"
    assert state["next_action"] == "start_windows"

    state = controller.reconcile(
        state,
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    assert state["phase"] == "WINDOWS_RUNNING"
    assert state["windows_consumed"] is True
    assert state["next_action"] == "none"

    windows_challenge_path = tmp_path / "windows-challenge.json"
    state, windows_challenge = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        windows_challenge_path,
        issued_at_unix=NOW,
        nonce="a" * 64,
    )
    windows_path = tmp_path / "windows.json"
    windows_path.write_text(
        json.dumps(platform_evidence("windows", windows_challenge)),
        encoding="utf-8",
    )
    windows_digest = "sha256:" + hashlib.sha256(windows_path.read_bytes()).hexdigest()
    state = controller.reconcile(
        state,
        observation(
            plan,
            windows=True,
            windows_job="passed",
            windows_digest=windows_digest,
        ),
        plan,
    )
    assert state["next_action"] == "collect_windows"
    state = controller.collect_platform(
        state,
        plan,
        "windows",
        windows_path,
        windows_challenge_path,
    )
    assert state["phase"] == "WINDOWS_DELETING"
    assert state["next_action"] == "delete_windows"

    state = controller.reconcile(
        state,
        observation(
            plan,
            windows_job="passed",
            windows_digest=windows_digest,
        ),
        plan,
    )
    assert state["phase"] == "WINDOWS_DELETING"
    assert state["next_action"] == "start_linux"

    state = controller.reconcile(
        state,
        observation(
            plan,
            linux=True,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_job="running",
        ),
        plan,
    )
    assert state["phase"] == "LINUX_RUNNING"
    assert state["linux_consumed"] is True

    linux_challenge_path = tmp_path / "linux-challenge.json"
    state, linux_challenge = controller.issue_calibration_challenge(
        state,
        plan,
        "linux",
        linux_challenge_path,
        issued_at_unix=NOW,
        nonce="b" * 64,
    )
    linux_path = tmp_path / "linux.json"
    linux_path.write_text(
        json.dumps(platform_evidence("linux", linux_challenge)),
        encoding="utf-8",
    )
    linux_digest = "sha256:" + hashlib.sha256(linux_path.read_bytes()).hexdigest()
    state = controller.reconcile(
        state,
        observation(
            plan,
            linux=True,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_job="passed",
            linux_digest=linux_digest,
        ),
        plan,
    )
    assert state["next_action"] == "collect_linux"
    state = controller.collect_platform(
        state,
        plan,
        "linux",
        linux_path,
        linux_challenge_path,
    )
    assert state["phase"] == "LINUX_DELETING"
    assert state["next_action"] == "delete_linux"

    state = controller.reconcile(
        state,
        observation(
            plan,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_job="passed",
            linux_digest=linux_digest,
        ),
        plan,
    )
    assert state["phase"] == "CLEANED_PASS"
    assert state["cleanup_verified"] is True
    assert state["next_action"] == "none"


def test_failed_job_goes_directly_to_exact_cleanup(plan):
    state = controller.initial_state(plan)
    state = controller.reconcile(
        state,
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    state = controller.reconcile(
        state,
        observation(plan, windows=True, windows_job="failed"),
        plan,
    )
    assert state["phase"] == "CLEANING_FAILED"
    assert state["next_action"] == "cleanup_failure"

    state = controller.reconcile(state, observation(plan), plan)
    assert state["phase"] == "CLEANED_FAILURE"
    assert state["cleanup_verified"] is True


def test_foreign_or_overlapping_resource_observations_fail_closed(plan):
    state = controller.initial_state(plan)
    value = observation(plan, windows=True, windows_job="running")
    value["instances"][plan.windows.instance]["source_commit"] = "9" * 40
    with pytest.raises(controller.Gate14ControllerError):
        controller.reconcile(state, value, plan)

    value = observation(
        plan,
        windows=True,
        linux=True,
        windows_job="running",
        linux_job="running",
    )
    state = controller.reconcile(
        state,
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    state = {
        **state,
        "phase": "WINDOWS_DELETING",
        "next_action": "delete_windows",
    }
    with pytest.raises(controller.Gate14ControllerError):
        controller.validate_observation(
            {
                **value,
                "l4_usage": 2,
            },
            plan,
        )


def test_collect_rejects_wrong_package_and_save_round_trips(tmp_path, plan):
    state = controller.initial_state(plan)
    state = controller.reconcile(
        state,
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    challenge_path = tmp_path / "wrong-package-challenge.json"
    state, challenge = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        challenge_path,
        issued_at_unix=NOW,
        nonce="a" * 64,
    )
    evidence = platform_evidence("windows", challenge)
    evidence["package"]["archive_sha256"] = "sha256:" + "9" * 64
    evidence_path = tmp_path / "wrong.json"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
    evidence_digest = "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    state = controller.reconcile(
        state,
        observation(
            plan,
            windows=True,
            windows_job="passed",
            windows_digest=evidence_digest,
        ),
        plan,
    )
    with pytest.raises(controller.Gate14ControllerError):
        controller.collect_platform(
            state,
            plan,
            "windows",
            evidence_path,
            challenge_path,
        )

    state_path = tmp_path / "state.json"
    controller.save_state(state_path, state, plan)
    assert controller.load_state(state_path, plan) == state


def test_collect_rejects_evidence_from_a_different_challenge(tmp_path, plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    issued_path = tmp_path / "issued-challenge.json"
    state, issued = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        issued_path,
        issued_at_unix=NOW,
        nonce="a" * 64,
    )
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(platform_evidence("windows", issued)),
        encoding="utf-8",
    )
    evidence_digest = "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest()
    state = controller.reconcile(
        state,
        observation(
            plan,
            windows=True,
            windows_job="passed",
            windows_digest=evidence_digest,
        ),
        plan,
    )
    different = dict(issued)
    different["nonce"] = "f" * 64
    challenge_path = tmp_path / "different-challenge.json"
    challenge_path.write_text(json.dumps(different), encoding="utf-8")

    with pytest.raises(controller.Gate14ControllerError):
        controller.collect_platform(
            state,
            plan,
            "windows",
            evidence_path,
            challenge_path,
        )


def test_begin_cleanup_is_idempotent_after_terminal_state(plan):
    state = controller.initial_state(plan)
    state = controller.begin_cleanup(state, plan, "manual-stop")
    assert state["phase"] == "CLEANING_FAILED"
    state = controller.reconcile(state, observation(plan), plan)
    assert state["phase"] == "CLEANED_FAILURE"
    assert controller.begin_cleanup(state, plan, "manual-stop") == state


def test_forged_success_and_deletion_states_fail_closed(plan):
    initial = controller.initial_state(plan)
    forged_pass = {
        **initial,
        "phase": "CLEANED_PASS",
        "cleanup_verified": True,
        "next_action": "none",
    }
    with pytest.raises(controller.Gate14ControllerError):
        controller.validate_state(forged_pass, plan)

    forged_windows_deleting = {
        **initial,
        "phase": "WINDOWS_DELETING",
        "windows_consumed": True,
        "next_action": "start_linux",
    }
    with pytest.raises(controller.Gate14ControllerError):
        controller.validate_state(forged_windows_deleting, plan)

    forged_linux_deleting = {
        **initial,
        "phase": "LINUX_DELETING",
        "windows_consumed": True,
        "linux_consumed": True,
        "windows_evidence_digest": "sha256:" + "c" * 64,
        "linux_evidence_digest": "sha256:" + "d" * 64,
        "next_action": "delete_linux",
    }
    with pytest.raises(controller.Gate14ControllerError):
        controller.reconcile(forged_linux_deleting, observation(plan), plan)


def test_expired_run_never_returns_a_start_action(plan):
    value = observation(plan)
    value["observed_at_unix"] = plan.windows.termination_unix

    state = controller.reconcile(controller.initial_state(plan), value, plan)

    assert state["phase"] == "CLEANED_FAILURE"
    assert state["failure_code"] == "run-expired"
    assert state["cleanup_verified"] is True
    assert state["next_action"] == "none"


def test_passed_job_requires_and_binds_exact_evidence_digest(tmp_path, plan):
    missing = observation(plan, windows=True, windows_job="passed")
    with pytest.raises(controller.Gate14ControllerError):
        controller.validate_observation(missing, plan)

    stale = controller.reconcile(
        controller.initial_state(plan),
        observation(
            plan,
            windows_job="passed",
            windows_digest="sha256:" + "c" * 64,
        ),
        plan,
    )
    assert stale["phase"] == "CLEANED_FAILURE"
    assert stale["failure_code"] == "stale-windows-job"
    assert stale["next_action"] == "none"

    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, windows=True, windows_job="running"),
        plan,
    )
    challenge_path = tmp_path / "reported-challenge.json"
    state, challenge = controller.issue_calibration_challenge(
        state,
        plan,
        "windows",
        challenge_path,
        issued_at_unix=NOW,
        nonce="a" * 64,
    )
    reported_digest = "sha256:" + "c" * 64
    state = controller.reconcile(
        state,
        observation(
            plan,
            windows=True,
            windows_job="passed",
            windows_digest=reported_digest,
        ),
        plan,
    )
    evidence_path = tmp_path / "different.json"
    evidence_path.write_text(
        json.dumps(platform_evidence("windows", challenge)),
        encoding="utf-8",
    )
    assert "sha256:" + hashlib.sha256(evidence_path.read_bytes()).hexdigest() != reported_digest
    with pytest.raises(controller.Gate14ControllerError):
        controller.collect_platform(
            state,
            plan,
            "windows",
            evidence_path,
            challenge_path,
        )


@pytest.mark.parametrize("platform", ["windows", "linux"])
def test_initial_state_requires_all_planned_disks_absent_before_start(plan, platform):
    value = observation(
        plan,
        **{f"{platform}_disk": True},
    )

    state = controller.reconcile(controller.initial_state(plan), value, plan)

    assert state["phase"] == "CLEANING_FAILED"
    assert state["failure_code"] == "orphaned-planned-disk"
    assert state["next_action"] == "cleanup_failure"


def test_stale_passed_job_with_orphan_disk_cannot_claim_terminal_cleanup(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(
            plan,
            windows_job="passed",
            windows_digest="sha256:" + "c" * 64,
            windows_disk=True,
        ),
        plan,
    )

    assert state["phase"] == "CLEANING_FAILED"
    assert state["cleanup_verified"] is False
    assert state["next_action"] == "cleanup_failure"


def test_linux_start_requires_absent_disk_and_absent_stale_job(plan):
    windows_digest = "sha256:" + "c" * 64
    state = {
        **controller.initial_state(plan),
        "revision": 2,
        "phase": "WINDOWS_DELETING",
        "windows_evidence_digest": windows_digest,
        "windows_challenge_sha256": "sha256:" + "e" * 64,
        "windows_challenge_consumed": True,
        "windows_consumed": True,
        "next_action": "delete_windows",
    }
    orphan = controller.reconcile(
        state,
        observation(
            plan,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_disk=True,
        ),
        plan,
    )
    assert orphan["phase"] == "CLEANING_FAILED"
    assert orphan["failure_code"] == "orphaned-linux-disk"
    assert orphan["next_action"] == "cleanup_failure"

    stale_job = controller.reconcile(
        state,
        observation(
            plan,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_job="passed",
            linux_digest="sha256:" + "d" * 64,
        ),
        plan,
    )
    assert stale_job["phase"] == "CLEANED_FAILURE"
    assert stale_job["failure_code"] == "stale-linux-job"
    assert stale_job["cleanup_verified"] is True
    assert stale_job["next_action"] == "none"


@pytest.mark.parametrize("returned_resource", [{"windows": True}, {"windows_disk": True}])
def test_linux_deletion_escalates_returned_windows_resources(plan, returned_resource):
    windows_digest = "sha256:" + "c" * 64
    linux_digest = "sha256:" + "d" * 64
    state = {
        **controller.initial_state(plan),
        "revision": 5,
        "phase": "LINUX_DELETING",
        "windows_evidence_digest": windows_digest,
        "linux_evidence_digest": linux_digest,
        "windows_challenge_sha256": "sha256:" + "e" * 64,
        "linux_challenge_sha256": "sha256:" + "f" * 64,
        "windows_challenge_consumed": True,
        "linux_challenge_consumed": True,
        "windows_consumed": True,
        "linux_consumed": True,
        "next_action": "delete_linux",
    }

    state = controller.reconcile(
        state,
        observation(
            plan,
            windows_job="passed",
            windows_digest=windows_digest,
            linux_job="passed",
            linux_digest=linux_digest,
            **returned_resource,
        ),
        plan,
    )

    assert state["phase"] == "CLEANING_FAILED"
    assert state["failure_code"] == "windows-resources-returned"
    assert state["next_action"] == "cleanup_failure"


def test_load_plan_recomputes_total_ledger_commitment(tmp_path):
    with pytest.raises(controller.Gate14ControllerError):
        write_plan(tmp_path, additional_current_maximum="99.00")


def test_load_plan_rejects_hidden_active_reservation_below_epoch_anchor(tmp_path):
    with pytest.raises(controller.Gate14ControllerError):
        write_plan(
            tmp_path,
            additional_current_maximum="1.00",
            hide_additional_below_anchor=True,
        )
