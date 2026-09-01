import hashlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import gate13_run_controller as controller  # noqa: E402

AUTHORIZATION = ROOT / "docs" / "evidence" / "gate13-20260831-a-cost-authorization.json"
LEDGER = ROOT / "docs" / "RELEASE_READINESS.md"
NOW = 2_000_000_000
ROUTE_DIGEST = "sha256:" + "a" * 64
WINDOWS_DIGEST = "sha256:" + "b" * 64
LINUX_DIGEST = "sha256:" + "c" * 64


def reserved_ledger_text(*, old_digest, new_digest):
    lines = LEDGER.read_text(encoding="utf-8").splitlines(keepends=True)
    matches = [index for index, line in enumerate(lines) if line.startswith("| gate13-20260831-a |")]
    assert len(matches) == 1
    index = matches[0]
    assert old_digest in lines[index]
    assert lines[index].rstrip().endswith("| CLEANED-COMMITTED |")
    lines[index] = lines[index].replace(old_digest, new_digest, 1).replace("| CLEANED-COMMITTED |", "| RESERVED |", 1)
    return "".join(lines)


@pytest.fixture
def plan(tmp_path):
    raw = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    raw["provider_plan"]["sequencing"]["clients_may_run_concurrently"] = False
    old_digest = raw["provider_plan_digest"]
    new_digest = controller._provider_digest(raw["provider_plan"])
    raw["provider_plan_digest"] = new_digest
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(raw), encoding="utf-8")

    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        reserved_ledger_text(old_digest=old_digest, new_digest=new_digest),
        encoding="utf-8",
    )
    return controller.load_plan(authorization, ledger)


def observation(
    plan, *, route=False, windows=False, linux=False, route_job="absent", windows_job="absent", linux_job="absent"
):
    present = {
        plan.route_instance: (route, plan.route_source_commit),
        plan.windows_instance: (windows, plan.windows_source_commit),
        plan.linux_instance: (linux, plan.linux_source_commit),
    }
    return {
        "schema_version": 1,
        "run_id": plan.run_id,
        "observed_at_unix": NOW,
        "instances": {
            name: {
                "present": exists,
                "run_id": plan.run_id if exists else None,
                "source_commit": source if exists else None,
                "termination_unix": NOW + 20_000 if exists else None,
            }
            for name, (exists, source) in present.items()
        },
        "disks": {
            plan.route_disk: route,
            plan.windows_disk: windows,
            plan.linux_disk: linux,
        },
        "firewalls": {
            plan.route_firewalls[0]: route,
            plan.route_firewalls[1]: route,
        },
        "protected_bootstrap_running": True,
        "route_acceptance": {
            "job_state": route_job,
            "evidence_digest": ROUTE_DIGEST if route_job == "passed" else None,
        },
        "clients": {
            "windows": {
                "job_state": windows_job,
                "attempt_ordinal": 1 if windows_job != "absent" else 0,
                "evidence_digest": WINDOWS_DIGEST if windows_job == "passed" else None,
            },
            "linux": {
                "job_state": linux_job,
                "attempt_ordinal": 1 if linux_job != "absent" else 0,
                "evidence_digest": LINUX_DIGEST if linux_job == "passed" else None,
            },
        },
    }


def test_load_plan_binds_exact_cost_and_resources(plan):
    assert plan.run_id == "gate13-20260831-a"
    assert plan.provider_plan_digest.startswith("sha256:")
    assert plan.ledger_state == "RESERVED"
    assert plan.instance_names == (
        "route-20260831-a-node",
        "gate13-20260831-a-win",
        "gate13-20260831-a-linux",
    )
    assert controller.PROTECTED_INSTANCE not in plan.instance_names
    assert plan.clients_may_run_concurrently is False


def test_load_plan_accepts_the_automated_replay_instead_of_legacy_16_phases(tmp_path):
    raw = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    sequencing = raw["provider_plan"]["sequencing"]
    sequencing["clients_may_run_concurrently"] = False
    sequencing["all_16_phases_required_per_platform"] = False
    sequencing["automated_gate13_replay_required"] = True
    old_digest = raw["provider_plan_digest"]
    new_digest = controller._provider_digest(raw["provider_plan"])
    raw["provider_plan_digest"] = new_digest
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(raw), encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        reserved_ledger_text(old_digest=old_digest, new_digest=new_digest),
        encoding="utf-8",
    )

    replay_plan = controller.load_plan(authorization, ledger)

    assert replay_plan.clients_may_run_concurrently is False


def test_load_plan_accepts_only_documented_owner_ceiling(tmp_path):
    raw = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    raw["provider_plan"]["sequencing"]["clients_may_run_concurrently"] = False
    old_digest = raw["provider_plan_digest"]
    new_digest = controller._provider_digest(raw["provider_plan"])
    raw["provider_plan_digest"] = new_digest
    raw["authorization"].update(
        {
            "combined_cloud_ceiling_usd": "500.00",
            "ledger_committed_before_run_usd": "52.00",
            "maximum_estimate_usd": "56.00",
            "remaining_after_run_maximum_usd": "392.00",
        }
    )
    authorization = tmp_path / "authorization.json"
    authorization.write_text(json.dumps(raw), encoding="utf-8")
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        reserved_ledger_text(old_digest=old_digest, new_digest=new_digest),
        encoding="utf-8",
    )

    raised_plan = controller.load_plan(authorization, ledger)
    assert raised_plan.ledger_state == "RESERVED"
    assert controller.initial_state(raised_plan)["next_action"] == "start_route"

    raw["authorization"]["combined_cloud_ceiling_usd"] = "499.00"
    raw["authorization"]["remaining_after_run_maximum_usd"] = "391.00"
    authorization.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(controller.RunControllerError, match="inconsistent"):
        controller.load_plan(authorization, ledger)


def test_cleaned_committed_ledger_cannot_start_a_new_run():
    historical_plan = controller.load_plan(AUTHORIZATION, LEDGER)

    assert historical_plan.ledger_state == "CLEANED-COMMITTED"
    with pytest.raises(controller.RunControllerError, match="not reserved"):
        controller.initial_state(historical_plan)


def test_non_reserved_ledger_allows_cleanup_only():
    historical_plan = controller.load_plan(AUTHORIZATION, LEDGER)
    reserved_plan = replace(
        historical_plan,
        ledger_state="RESERVED",
        clients_may_run_concurrently=False,
    )
    reserved_state = controller.initial_state(reserved_plan)

    forward_actions = controller.ACTION_STATES - controller.CLEANUP_ACTIONS - {"none"}
    for action in forward_actions:
        with pytest.raises(controller.RunControllerError, match="not reserved"):
            controller.begin_action(reserved_state, historical_plan, action=action)

    cleanup_state = dict(reserved_state)
    cleanup_state.update(
        {
            "phase": "CLEANING_FAILED",
            "failure_code": "operator_cleanup",
            "next_action": "cleanup_failure",
        }
    )
    cleaning = controller.begin_action(
        cleanup_state,
        historical_plan,
        action="cleanup_failure",
    )
    assert cleaning["phase"] == "CLEANING_FAILED"
    assert cleaning["next_action"] == "none"


def test_reserved_parallel_client_plan_cannot_start(tmp_path):
    ledger = tmp_path / "ledger.md"
    digest = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))["provider_plan_digest"]
    ledger.write_text(
        reserved_ledger_text(old_digest=digest, new_digest=digest),
        encoding="utf-8",
    )
    parallel = controller.load_plan(AUTHORIZATION, ledger)

    with pytest.raises(controller.RunControllerError, match="concurrent clients"):
        controller.initial_state(parallel)


def test_changed_authorization_fails_closed(tmp_path):
    raw = json.loads(AUTHORIZATION.read_text(encoding="utf-8"))
    raw["provider_plan"]["route"]["machine_type"] = "e2-micro"
    changed = tmp_path / "authorization.json"
    changed.write_text(json.dumps(raw), encoding="utf-8")

    with pytest.raises(controller.RunControllerError, match="digest changed"):
        controller.load_plan(changed, LEDGER)


def test_inventory_precedes_route_and_route_acceptance_precedes_clients(plan):
    state = controller.initial_state(plan)

    absent = controller.reconcile(state, observation(plan), plan, now_unix=NOW)
    assert absent["phase"] == "ABSENT"
    assert absent["next_action"] == "start_route"

    accepting = controller.reconcile(
        absent,
        observation(plan, route=True, route_job="running"),
        plan,
        now_unix=NOW,
    )
    assert accepting["phase"] == "ROUTE_ACCEPTING"
    assert accepting["next_action"] == "accept_route"

    invalid = controller.reconcile(
        accepting,
        observation(plan, route=True, windows=True, route_job="running", windows_job="starting"),
        plan,
        now_unix=NOW,
    )
    assert invalid["phase"] == "CLEANING_FAILED"
    assert invalid["failure_code"] == "client_started_before_route_acceptance"


def test_exact_name_with_foreign_identity_fails_closed(plan):
    raw = observation(plan, route=True, route_job="running")
    raw["instances"][plan.route_instance]["run_id"] = "foreign-run"

    with pytest.raises(controller.RunControllerError, match="foreign exact-name"):
        controller.reconcile(controller.initial_state(plan), raw, plan, now_unix=NOW)


def test_route_acceptance_starts_windows_before_linux(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, route_job="passed"),
        plan,
        now_unix=NOW,
    )
    assert state["phase"] == "ROUTE_ACCEPTED"
    assert state["next_action"] == "start_windows"

    invalid = controller.reconcile(
        state,
        observation(plan, route=True, linux=True, route_job="passed", linux_job="running"),
        plan,
        now_unix=NOW,
    )
    assert invalid["phase"] == "CLEANING_FAILED"
    assert invalid["failure_code"] == "linux_started_before_windows_evidence"


@pytest.mark.parametrize("job_state", ["failed", "ambiguous"])
def test_failed_or_ambiguous_windows_is_consumed_and_never_resumed(plan, job_state):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, windows=True, route_job="passed", windows_job=job_state),
        plan,
        now_unix=NOW,
    )

    assert state["phase"] == "CLEANING_FAILED"
    assert state["windows_consumed"] is True
    assert state["next_action"] == "cleanup_failure"


def test_action_intent_is_persisted_before_mutation_and_cannot_relaunch(plan):
    state = controller.initial_state(plan)
    started = controller.begin_action(state, plan, action="start_route")

    assert started["phase"] == "ROUTE_STARTING"
    assert started["next_action"] == "none"
    missing = controller.reconcile(started, observation(plan), plan, now_unix=NOW)
    assert missing["phase"] == "CLEANED_FAILURE"
    assert missing["failure_code"] == "resources_disappeared_before_completion"
    with pytest.raises(controller.RunControllerError, match="out of order"):
        controller.begin_action(started, plan, action="start_route")


def test_route_acceptance_intent_cannot_rearm_after_dispatch(plan):
    ready = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, route_job="absent"),
        plan,
        now_unix=NOW,
    )
    dispatched = controller.begin_action(ready, plan, action="accept_route")

    running = controller.reconcile(
        dispatched,
        observation(plan, route=True, route_job="running"),
        plan,
        now_unix=NOW,
    )
    assert running["phase"] == "ROUTE_ACCEPTING"
    assert running["next_action"] == "none"

    missing = controller.reconcile(
        dispatched,
        observation(plan, route=True, route_job="absent"),
        plan,
        now_unix=NOW,
    )

    assert missing["phase"] == "CLEANING_FAILED"
    assert missing["failure_code"] == "route_acceptance_disappeared"
    assert missing["next_action"] == "cleanup_failure"


def test_client_start_intent_cannot_rearm_after_dispatch(plan):
    route_ready = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, route_job="passed"),
        plan,
        now_unix=NOW,
    )
    windows_dispatched = controller.begin_action(route_ready, plan, action="start_windows")

    provisioning = controller.reconcile(
        windows_dispatched,
        observation(plan, route=True, windows=True, route_job="passed", windows_job="absent"),
        plan,
        now_unix=NOW,
    )
    assert provisioning["phase"] == "WINDOWS_RUNNING"
    assert provisioning["next_action"] == "none"

    missing = controller.reconcile(
        windows_dispatched,
        observation(plan, route=True, route_job="passed"),
        plan,
        now_unix=NOW,
    )
    assert missing["phase"] == "CLEANING_FAILED"
    assert missing["failure_code"] == "windows_disappeared_after_start_intent"

    linux_ready = dict(route_ready)
    linux_ready.update(
        {
            "phase": "WINDOWS_COLLECTED",
            "windows_evidence_digest": WINDOWS_DIGEST,
            "windows_consumed": True,
            "next_action": "start_linux",
        }
    )
    linux_dispatched = controller.begin_action(linux_ready, plan, action="start_linux")
    linux_provisioning = controller.reconcile(
        linux_dispatched,
        observation(plan, route=True, linux=True, route_job="passed", linux_job="absent"),
        plan,
        now_unix=NOW,
    )
    assert linux_provisioning["phase"] == "LINUX_RUNNING"
    assert linux_provisioning["next_action"] == "none"

    linux_missing = controller.reconcile(
        linux_dispatched,
        observation(plan, route=True, route_job="passed"),
        plan,
        now_unix=NOW,
    )
    assert linux_missing["phase"] == "CLEANING_FAILED"
    assert linux_missing["failure_code"] == "linux_disappeared_after_start_intent"


def test_observed_attempt_cannot_disappear_and_relaunch(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, route_job="passed"),
        plan,
        now_unix=NOW,
    )
    disappeared = observation(plan, route=True, route_job="passed")
    disappeared["clients"]["windows"]["attempt_ordinal"] = 1

    failed = controller.reconcile(state, disappeared, plan, now_unix=NOW)
    assert failed["phase"] == "CLEANING_FAILED"
    assert failed["failure_code"] == "windows_attempt_disappeared"


def test_active_host_job_is_observed_not_relaunched(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, windows=True, route_job="passed", windows_job="running"),
        plan,
        now_unix=NOW,
    )

    assert state["phase"] == "WINDOWS_RUNNING"
    assert state["next_action"] == "none"


def test_collect_binds_canonical_evidence_then_deletes_windows(monkeypatch, plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, windows=True, route_job="passed", windows_job="passed"),
        plan,
        now_unix=NOW,
    )
    assert state["phase"] == "WINDOWS_COLLECTING"

    payload = b'{"bounded":true}'
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    monkeypatch.setattr(controller.lifecycle, "load_lifecycle_json", lambda _payload: {"validated": True})
    monkeypatch.setattr(
        controller.lifecycle,
        "validate_lifecycle_document",
        lambda _raw: {
            "source_commit": plan.windows_source_commit,
            "package_sha256": plan.windows_package_sha256,
            "package_bytes": plan.windows_package_bytes,
            "model_id": "Qwen3.5 2B",
            "manifest_digest": plan.qwen_manifest.removeprefix("sha256:"),
        },
    )

    collected = controller.collect_platform(
        state,
        plan,
        platform="windows",
        evidence_payload=payload,
        observed_digest=digest,
    )
    assert collected["phase"] == "WINDOWS_DELETING"
    assert collected["next_action"] == "delete_windows"
    assert collected["windows_consumed"] is True

    deleting = observation(plan, route=True, route_job="passed")
    deleting["clients"]["windows"]["attempt_ordinal"] = 1
    deleting["disks"][plan.windows_disk] = True
    still_present = controller.reconcile(collected, deleting, plan, now_unix=NOW)
    assert still_present["phase"] == "WINDOWS_DELETING"
    assert still_present["next_action"] == "delete_windows"
    with pytest.raises(controller.RunControllerError, match="absence is not proved"):
        controller.mark_client_absent(
            collected,
            plan,
            platform="windows",
            observation=deleting,
            now_unix=NOW,
        )

    absent = observation(plan, route=True, route_job="passed")
    absent["clients"]["windows"]["attempt_ordinal"] = 1
    after_delete = controller.mark_client_absent(
        collected,
        plan,
        platform="windows",
        observation=absent,
        now_unix=NOW,
    )
    assert after_delete["phase"] == "WINDOWS_COLLECTED"
    assert after_delete["next_action"] == "start_linux"


def test_collect_accepts_current_automated_desktop_replay(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, windows=True, route_job="passed", windows_job="passed"),
        plan,
        now_unix=NOW,
    )
    evidence = {
        "schema_version": 1,
        "scope": "gate13-automated-desktop-replay",
        "run_id": f"{plan.run_id}-windows",
        "platform": "windows",
        "result": "passed",
        "source_commit": plan.windows_source_commit,
        "package": {
            "sha256": "sha256:" + plan.windows_package_sha256.removeprefix("sha256:"),
            "bytes": plan.windows_package_bytes,
            "verified_before_run": True,
            "self_test_count": 4,
        },
        "model_id": "Qwen3.5 2B",
        "manifest_digest": "sha256:" + plan.qwen_manifest.removeprefix("sha256:"),
        "real_window_sessions": 2,
        "localhost_inference_count": 2,
        "policy_dialog_saved": True,
        "start_clicked": True,
        "restart_resume_observed": True,
        "pause_clicked": True,
        "sharing_paused": True,
        "policy_profile": "gate13-manual-cpu-v1",
        "session_duration_seconds": {"start": 120.0, "resume_pause": 90.0},
        "privacy_safe": True,
        "qualification_temporaries_removed": True,
    }
    payload = (json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n").encode()
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()

    collected = controller.collect_platform(
        state,
        plan,
        platform="windows",
        evidence_payload=payload,
        observed_digest=digest,
    )

    assert collected["phase"] == "WINDOWS_DELETING"
    assert collected["windows_consumed"] is True


def test_partial_or_wrong_digest_evidence_cannot_advance(plan):
    state = controller.reconcile(
        controller.initial_state(plan),
        observation(plan, route=True, windows=True, route_job="passed", windows_job="passed"),
        plan,
        now_unix=NOW,
    )

    with pytest.raises(controller.RunControllerError, match="digest changed"):
        controller.collect_platform(
            state,
            plan,
            platform="windows",
            evidence_payload=b"{}",
            observed_digest=WINDOWS_DIGEST,
        )


def test_success_requires_both_records_and_exact_absence(plan):
    state = controller.initial_state(plan)
    state.update(
        {
            "phase": "LINUX_COLLECTED",
            "route_acceptance_digest": ROUTE_DIGEST,
            "windows_evidence_digest": WINDOWS_DIGEST,
            "linux_evidence_digest": LINUX_DIGEST,
            "windows_consumed": True,
            "linux_consumed": True,
            "next_action": "delete_route",
        }
    )

    complete = controller.reconcile(state, observation(plan), plan, now_unix=NOW)
    assert complete["phase"] == "CLEANED_PASS"
    assert complete["cleanup_verified"] is True
    assert complete["next_action"] == "none"


def test_failure_cleanup_is_idempotent_and_never_becomes_pass(plan):
    state = controller.initial_state(plan)
    state.update(
        {
            "phase": "CLEANING_FAILED",
            "failure_code": "windows_failed_or_ambiguous",
            "windows_consumed": True,
            "next_action": "cleanup_failure",
        }
    )

    cleaned = controller.reconcile(state, observation(plan), plan, now_unix=NOW)
    assert cleaned["phase"] == "CLEANED_FAILURE"
    assert controller.reconcile(cleaned, observation(plan), plan, now_unix=NOW) == cleaned


def test_stale_observation_and_expired_deadline_fail_closed(plan):
    stale = observation(plan)
    stale["observed_at_unix"] = NOW - 301
    with pytest.raises(controller.RunControllerError, match="stale"):
        controller.reconcile(controller.initial_state(plan), stale, plan, now_unix=NOW)

    expired = observation(plan, route=True, route_job="running")
    expired["instances"][plan.route_instance]["termination_unix"] = NOW
    with pytest.raises(controller.RunControllerError, match="deadline expired"):
        controller.reconcile(controller.initial_state(plan), expired, plan, now_unix=NOW)


def test_atomic_state_round_trip_and_public_status_are_bounded(tmp_path, plan):
    state_path = tmp_path / "state.json"
    state = controller.initial_state(plan)
    controller.persist(state_path, state, plan)

    assert controller.load_state(state_path, plan) == state
    public = controller.public_status(state, plan)
    assert set(public) == {
        "schema_version",
        "run_id",
        "phase",
        "next_action",
        "failure_code",
        "windows_consumed",
        "linux_consumed",
        "cleanup_verified",
    }
    rendered = json.dumps(public)
    for forbidden in ("token", "password", "prompt", "endpoint", str(tmp_path)):
        assert forbidden not in rendered.lower()
