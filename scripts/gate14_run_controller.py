"""Durable, source-bound controller for one bounded Gate 14 GCP run.

The controller never invokes a provider. Every start, status, collect, or cleanup
operation first consumes an exact provider observation, persists its decision, and
returns one allowlisted action. A provider adapter may execute only that action.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

import gate14_calibration_challenge as challenge_contract
import gate14_hardware_acceptance as acceptance
import gate14_packaged_lifecycle as packaged_lifecycle
import qualification_cost_guard as cost_guard

SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 262_144
PROTECTED_INSTANCE = "communityai-bootstrap-1"
ALLOWED_CEILING_USD = 100.0
CURRENT_EPOCH_ANCHOR_RUN_ID = "gate13-20260901-a"
CURRENT_EPOCH_ANCHOR_MAXIMUM_USD = Decimal("56.00")

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_NAME_RE = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

PHASES = {
    "ABSENT",
    "WINDOWS_RUNNING",
    "WINDOWS_DELETING",
    "LINUX_RUNNING",
    "LINUX_DELETING",
    "CLEANING_FAILED",
    "CLEANED_PASS",
    "CLEANED_FAILURE",
}
TERMINAL_PHASES = {"CLEANED_PASS", "CLEANED_FAILURE"}
ACTIONS = {
    "start_windows",
    "collect_windows",
    "delete_windows",
    "start_linux",
    "collect_linux",
    "delete_linux",
    "cleanup_failure",
    "none",
}
JOB_STATES = {"absent", "starting", "running", "passed", "failed", "ambiguous"}

_AUTH_FIELDS = {
    "schema_version",
    "gate",
    "result",
    "run_id",
    "source_commit",
    "provider_plan_digest",
    "provider_plan",
    "authorization",
    "prohibited",
}
_AUTHORIZATION_FIELDS = {
    "combined_cloud_ceiling_usd",
    "ledger_committed_before_run_usd",
    "maximum_estimate_usd",
    "remaining_after_run_maximum_usd",
    "reservation_recorded",
    "native_auth_revalidated",
    "provisioning_authorized_after_fail_closed_preflight",
}
_PLAN_FIELDS = {"project", "zone", "clients", "sequencing"}
_CLIENT_PLAN_FIELDS = {
    "platform",
    "instance",
    "disk",
    "source_commit",
    "termination_unix",
    "package_sha256",
    "model_id",
    "manifest_digest",
    "machine_type",
    "image_project",
    "image",
    "boot_disk_gib",
    "boot_disk_type",
    "service_account_disabled",
    "max_run_seconds",
    "termination_action",
}
_SEQUENCING_FIELDS = {
    "clients_may_run_concurrently",
    "windows_first",
    "fresh_host_per_platform",
}
_STATE_FIELDS = {
    "schema_version",
    "run_id",
    "authorization_sha256",
    "provider_plan_digest",
    "revision",
    "phase",
    "failure_code",
    "windows_evidence_digest",
    "linux_evidence_digest",
    "windows_challenge_sha256",
    "linux_challenge_sha256",
    "windows_challenge_consumed",
    "linux_challenge_consumed",
    "windows_consumed",
    "linux_consumed",
    "cleanup_verified",
    "next_action",
}
_OBSERVATION_FIELDS = {
    "schema_version",
    "run_id",
    "observed_at_unix",
    "instances",
    "disks",
    "clients",
    "l4_usage",
    "protected_bootstrap_running",
}
_INSTANCE_FIELDS = {"present", "run_id", "source_commit", "termination_unix"}
_CLIENT_FIELDS = {"job_state", "attempt_ordinal", "evidence_digest"}


class Gate14ControllerError(ValueError):
    """The plan, state, observation, or transition failed closed."""


@dataclass(frozen=True)
class ClientPlan:
    platform: str
    instance: str
    disk: str
    source_commit: str
    termination_unix: int
    package_sha256: str
    model_id: str
    manifest_digest: str
    machine_type: str
    image_project: str
    image: str
    boot_disk_gib: int
    boot_disk_type: str
    max_run_seconds: int


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    authorization_sha256: str
    provider_plan_digest: str
    source_commit: str
    ledger_state: str
    project: str
    zone: str
    windows: ClientPlan
    linux: ClientPlan

    @property
    def instances(self) -> tuple[str, str]:
        return (self.windows.instance, self.linux.instance)

    @property
    def disks(self) -> tuple[str, str]:
        return (self.windows.disk, self.linux.disk)


def _reject_constant(_value: str) -> None:
    raise Gate14ControllerError("invalid JSON")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14ControllerError("duplicate JSON field")
        result[key] = value
    return result


def _regular_bytes(path: Path, maximum: int = MAX_JSON_BYTES) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Gate14ControllerError("required file is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise Gate14ControllerError("required file is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Gate14ControllerError("required file is unreadable") from exc


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_JSON_BYTES:
        raise Gate14ControllerError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14ControllerError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise Gate14ControllerError("JSON root is invalid")
    return value


def _mapping(value: Any, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise Gate14ControllerError("schema is invalid")
    return value


def _string(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise Gate14ControllerError("string is invalid")
    return value


def _integer(value: Any, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Gate14ControllerError("integer is invalid")
    return value


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _client_plan(value: Any, platform: str, source_commit: str) -> ClientPlan:
    raw = _mapping(value, _CLIENT_PLAN_FIELDS)
    if raw["platform"] != platform or raw["source_commit"] != source_commit:
        raise Gate14ControllerError("client source binding is invalid")
    instance = _string(raw["instance"], _NAME_RE)
    disk = _string(raw["disk"], _NAME_RE)
    if instance == PROTECTED_INSTANCE or disk == PROTECTED_INSTANCE:
        raise Gate14ControllerError("protected resource is targeted")
    package_sha256 = _string(raw["package_sha256"], _DIGEST_RE)
    expected_model = acceptance.EXPECTED_PLATFORM_MODELS[platform]
    if raw["model_id"] != expected_model:
        raise Gate14ControllerError("client model is invalid")
    expected_manifest = acceptance.MODEL_PROFILES[expected_model]["manifest_digest"]
    if raw["manifest_digest"] != expected_manifest:
        raise Gate14ControllerError("client manifest is invalid")
    if raw["machine_type"] != "g2-standard-8":
        raise Gate14ControllerError("client machine type is invalid")
    expected_image_project = "windows-cloud" if platform == "windows" else "ubuntu-os-cloud"
    expected_image_pattern = (
        re.compile(r"windows-server-2022-dc-v[0-9]{8}")
        if platform == "windows"
        else re.compile(r"ubuntu-2404-noble-amd64-v[0-9]{8}")
    )
    if raw["image_project"] != expected_image_project:
        raise Gate14ControllerError("client image project is invalid")
    image = _string(raw["image"], expected_image_pattern)
    boot_disk_gib = _integer(raw["boot_disk_gib"], 100, 200)
    if (
        raw["boot_disk_type"] != "pd-balanced"
        or raw["service_account_disabled"] is not True
        or raw["termination_action"] != "DELETE"
    ):
        raise Gate14ControllerError("client runtime boundary is invalid")
    max_run_seconds = _integer(raw["max_run_seconds"], 1_800, 14_400)
    return ClientPlan(
        platform=platform,
        instance=instance,
        disk=disk,
        source_commit=source_commit,
        termination_unix=_integer(raw["termination_unix"], 1),
        package_sha256=package_sha256,
        model_id=expected_model,
        manifest_digest=expected_manifest,
        machine_type="g2-standard-8",
        image_project=expected_image_project,
        image=image,
        boot_disk_gib=boot_disk_gib,
        boot_disk_type="pd-balanced",
        max_run_seconds=max_run_seconds,
    )


def load_plan(authorization_path: Path, ledger_path: Path) -> RunPlan:
    authorization_payload = _regular_bytes(authorization_path)
    raw = _mapping(_strict_json(authorization_payload), _AUTH_FIELDS)
    if raw["schema_version"] != SCHEMA_VERSION or raw["gate"] != 14 or raw["result"] != "authorized":
        raise Gate14ControllerError("authorization scope is invalid")
    run_id = _string(raw["run_id"], _RUN_RE)
    source_commit = _string(raw["source_commit"], _COMMIT_RE)
    provider_plan = _mapping(raw["provider_plan"], _PLAN_FIELDS)
    provider_digest = _canonical_digest(provider_plan)
    if raw["provider_plan_digest"] != provider_digest:
        raise Gate14ControllerError("provider plan digest changed")
    project = _string(provider_plan["project"], re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]"))
    zone = _string(provider_plan["zone"], re.compile(r"[a-z]+(?:-[a-z0-9]+)+-[a-z]"))
    sequencing = _mapping(provider_plan["sequencing"], _SEQUENCING_FIELDS)
    if (
        sequencing["clients_may_run_concurrently"] is not False
        or sequencing["windows_first"] is not True
        or sequencing["fresh_host_per_platform"] is not True
    ):
        raise Gate14ControllerError("client sequencing is invalid")
    clients = provider_plan["clients"]
    if not isinstance(clients, list) or len(clients) != 2:
        raise Gate14ControllerError("client plan is invalid")
    by_platform = {
        item.get("platform"): item
        for item in clients
        if isinstance(item, dict) and isinstance(item.get("platform"), str)
    }
    if set(by_platform) != {"windows", "linux"}:
        raise Gate14ControllerError("client platform plan is invalid")
    windows = _client_plan(by_platform["windows"], "windows", source_commit)
    linux = _client_plan(by_platform["linux"], "linux", source_commit)
    if windows.instance == linux.instance or windows.disk == linux.disk:
        raise Gate14ControllerError("client resources overlap")

    cost = _mapping(raw["authorization"], _AUTHORIZATION_FIELDS)
    try:
        ceiling = Decimal(str(cost["combined_cloud_ceiling_usd"]))
        before = Decimal(str(cost["ledger_committed_before_run_usd"]))
        maximum = Decimal(str(cost["maximum_estimate_usd"]))
        remaining = Decimal(str(cost["remaining_after_run_maximum_usd"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Gate14ControllerError("cost authorization is invalid") from exc
    if (
        not all(value.is_finite() for value in (ceiling, before, maximum, remaining))
        or ceiling != Decimal(str(ALLOWED_CEILING_USD))
        or before < 0
        or maximum <= 0
        or before + maximum > ceiling
        or ceiling - before - maximum != remaining
        or cost["reservation_recorded"] is not True
        or cost["native_auth_revalidated"] is not True
        or cost["provisioning_authorized_after_fail_closed_preflight"] is not True
    ):
        raise Gate14ControllerError("cost authorization is inconsistent")
    prohibited = raw["prohibited"]
    if (
        not isinstance(prohibited, dict)
        or set(prohibited) != {"credits", "macos", "fly_gpu"}
        or any(type(value) is not int or value != 0 for value in prohibited.values())
    ):
        raise Gate14ControllerError("prohibited work is present")
    try:
        entries = cost_guard.load_spend_ledger(ledger_path)
    except cost_guard.CostGuardError as exc:
        raise Gate14ControllerError("spend ledger is invalid") from exc
    anchors = [entry for entry in entries if entry.run_id == CURRENT_EPOCH_ANCHOR_RUN_ID]
    if len(anchors) != 1 or anchors[0].maximum_usd != CURRENT_EPOCH_ANCHOR_MAXIMUM_USD:
        raise Gate14ControllerError("current accounting epoch anchor is invalid")
    anchor_index = entries.index(anchors[0])
    historical_entries = entries[anchor_index + 1 :]
    if any(entry.state not in {"CANCELED", "CLEANED-COMMITTED", "CLEANED-RELEASED"} for entry in historical_entries):
        raise Gate14ControllerError("active reservation is hidden below the epoch anchor")
    current_epoch_entries = entries[: anchor_index + 1]
    matches = [entry for entry in current_epoch_entries if entry.run_id == run_id]
    if (
        len(matches) != 1
        or matches[0].provider != "GCP"
        or matches[0].maximum_usd != maximum
        or matches[0].state != "RESERVED"
        or provider_digest not in matches[0].purpose
    ):
        raise Gate14ControllerError("spend ledger reservation is invalid")
    ledger_committed = sum(
        (entry.committed_usd for entry in current_epoch_entries),
        Decimal("0"),
    )
    committed_before = ledger_committed - matches[0].committed_usd
    if committed_before != before or ledger_committed > ceiling:
        raise Gate14ControllerError("spend ledger exceeds the authorized ceiling")
    return RunPlan(
        run_id=run_id,
        authorization_sha256="sha256:" + hashlib.sha256(authorization_payload).hexdigest(),
        provider_plan_digest=provider_digest,
        source_commit=source_commit,
        ledger_state=matches[0].state,
        project=project,
        zone=zone,
        windows=windows,
        linux=linux,
    )


def initial_state(plan: RunPlan) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": plan.run_id,
        "authorization_sha256": plan.authorization_sha256,
        "provider_plan_digest": plan.provider_plan_digest,
        "revision": 0,
        "phase": "ABSENT",
        "failure_code": None,
        "windows_evidence_digest": None,
        "linux_evidence_digest": None,
        "windows_challenge_sha256": None,
        "linux_challenge_sha256": None,
        "windows_challenge_consumed": False,
        "linux_challenge_consumed": False,
        "windows_consumed": False,
        "linux_consumed": False,
        "cleanup_verified": False,
        "next_action": "none",
    }


def validate_state(value: Mapping[str, Any], plan: RunPlan) -> dict[str, Any]:
    state = dict(_mapping(value, _STATE_FIELDS))
    if (
        state["schema_version"] != STATE_SCHEMA_VERSION
        or state["run_id"] != plan.run_id
        or state["authorization_sha256"] != plan.authorization_sha256
        or state["provider_plan_digest"] != plan.provider_plan_digest
        or state["phase"] not in PHASES
        or state["next_action"] not in ACTIONS
    ):
        raise Gate14ControllerError("state binding is invalid")
    _integer(state["revision"])
    for field in (
        "windows_consumed",
        "linux_consumed",
        "windows_challenge_consumed",
        "linux_challenge_consumed",
        "cleanup_verified",
    ):
        if type(state[field]) is not bool:
            raise Gate14ControllerError("state boolean is invalid")
    for field in (
        "windows_evidence_digest",
        "linux_evidence_digest",
        "windows_challenge_sha256",
        "linux_challenge_sha256",
    ):
        if state[field] is not None:
            _string(state[field], _DIGEST_RE)
    for platform in ("windows", "linux"):
        if state[f"{platform}_challenge_consumed"] and state[f"{platform}_challenge_sha256"] is None:
            raise Gate14ControllerError("consumed calibration challenge is missing")
    if state["failure_code"] is not None:
        _string(state["failure_code"], re.compile(r"[a-z0-9][a-z0-9-]{0,63}"))
    allowed_actions = {
        "ABSENT": {"none", "start_windows"},
        "WINDOWS_RUNNING": {"none", "collect_windows"},
        "WINDOWS_DELETING": {"delete_windows", "start_linux"},
        "LINUX_RUNNING": {"none", "collect_linux"},
        "LINUX_DELETING": {"delete_linux"},
        "CLEANING_FAILED": {"cleanup_failure"},
        "CLEANED_PASS": {"none"},
        "CLEANED_FAILURE": {"none"},
    }
    if state["next_action"] not in allowed_actions[state["phase"]]:
        raise Gate14ControllerError("state action is inconsistent")
    if state["windows_evidence_digest"] is not None and not state["windows_consumed"]:
        raise Gate14ControllerError("Windows evidence state is inconsistent")
    if state["linux_evidence_digest"] is not None and not state["linux_consumed"]:
        raise Gate14ControllerError("Linux evidence state is inconsistent")
    for platform in ("windows", "linux"):
        if state[f"{platform}_evidence_digest"] is not None and state[f"{platform}_challenge_sha256"] is None:
            raise Gate14ControllerError("platform evidence lacks a calibration challenge")
    phase = state["phase"]
    failed_phase = phase in {"CLEANING_FAILED", "CLEANED_FAILURE"}
    if failed_phase is (state["failure_code"] is None):
        raise Gate14ControllerError("failure state is inconsistent")
    if state["cleanup_verified"] is not (phase in TERMINAL_PHASES):
        raise Gate14ControllerError("cleanup state is inconsistent")
    windows_evidence = state["windows_evidence_digest"] is not None
    linux_evidence = state["linux_evidence_digest"] is not None
    if phase == "ABSENT" and any(
        (
            state["windows_consumed"],
            state["linux_consumed"],
            windows_evidence,
            linux_evidence,
            state["windows_challenge_sha256"] is not None,
            state["linux_challenge_sha256"] is not None,
        )
    ):
        raise Gate14ControllerError("initial state is inconsistent")
    if phase == "WINDOWS_RUNNING" and (
        not state["windows_consumed"]
        or state["linux_consumed"]
        or linux_evidence
        or (state["next_action"] == "collect_windows") is not windows_evidence
    ):
        raise Gate14ControllerError("Windows running state is inconsistent")
    if phase == "WINDOWS_DELETING" and (
        not state["windows_consumed"]
        or state["linux_consumed"]
        or not windows_evidence
        or linux_evidence
        or not state["windows_challenge_consumed"]
    ):
        raise Gate14ControllerError("Windows deletion state is inconsistent")
    if phase == "LINUX_RUNNING" and (
        not state["windows_consumed"]
        or not state["linux_consumed"]
        or not windows_evidence
        or not state["windows_challenge_consumed"]
        or (state["next_action"] == "collect_linux") is not linux_evidence
    ):
        raise Gate14ControllerError("Linux running state is inconsistent")
    if phase in {"LINUX_DELETING", "CLEANED_PASS"} and (
        not state["windows_consumed"]
        or not state["linux_consumed"]
        or not windows_evidence
        or not linux_evidence
        or not state["windows_challenge_consumed"]
        or not state["linux_challenge_consumed"]
    ):
        raise Gate14ControllerError("completed evidence state is inconsistent")
    return state


def validate_observation(value: Mapping[str, Any], plan: RunPlan) -> dict[str, Any]:
    observation = dict(_mapping(value, _OBSERVATION_FIELDS))
    if observation["schema_version"] != SCHEMA_VERSION or observation["run_id"] != plan.run_id:
        raise Gate14ControllerError("observation binding is invalid")
    now = _integer(observation["observed_at_unix"], 1)
    if observation["protected_bootstrap_running"] is not True:
        raise Gate14ControllerError("protected bootstrap is not healthy")
    _integer(observation["l4_usage"], 0, 1)
    instances = observation["instances"]
    disks = observation["disks"]
    clients = observation["clients"]
    if (
        not isinstance(instances, dict)
        or set(instances) != set(plan.instances)
        or not isinstance(disks, dict)
        or set(disks) != set(plan.disks)
        or not isinstance(clients, dict)
        or set(clients) != {"windows", "linux"}
    ):
        raise Gate14ControllerError("provider inventory is not exact")
    for client in (plan.windows, plan.linux):
        instance = _mapping(instances[client.instance], _INSTANCE_FIELDS)
        if type(instance["present"]) is not bool or type(disks[client.disk]) is not bool:
            raise Gate14ControllerError("provider inventory type is invalid")
        if instance["present"]:
            if (
                instance["run_id"] != plan.run_id
                or instance["source_commit"] != client.source_commit
                or _integer(instance["termination_unix"], 1) != client.termination_unix
                or disks[client.disk] is not True
            ):
                raise Gate14ControllerError("provider resource binding is invalid")
        elif any(
            value is not None
            for value in (
                instance["run_id"],
                instance["source_commit"],
                instance["termination_unix"],
            )
        ):
            raise Gate14ControllerError("absent instance metadata is invalid")
        job = _mapping(clients[client.platform], _CLIENT_FIELDS)
        job_state = job["job_state"]
        if job_state not in JOB_STATES:
            raise Gate14ControllerError("host job state is invalid")
        attempt = _integer(job["attempt_ordinal"], 0, 1)
        evidence_digest = job["evidence_digest"]
        if job_state == "absent":
            if attempt != 0 or evidence_digest is not None:
                raise Gate14ControllerError("absent host job evidence is inconsistent")
        else:
            if attempt != 1:
                raise Gate14ControllerError("host job attempt is inconsistent")
            if job_state == "passed":
                _string(evidence_digest, _DIGEST_RE)
            elif evidence_digest is not None:
                raise Gate14ControllerError("unfinished host job exposed evidence")
    expected_l4_usage = sum(int(instances[client.instance]["present"]) for client in (plan.windows, plan.linux))
    if observation["l4_usage"] != expected_l4_usage:
        raise Gate14ControllerError("accelerator inventory is inconsistent")
    return observation


def _next(state: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    result = dict(state)
    result.update(changes)
    result["revision"] = int(state["revision"]) + 1
    return result


def _resources_absent(observation: Mapping[str, Any], plan: RunPlan) -> bool:
    return (
        all(not observation["instances"][name]["present"] for name in plan.instances)
        and all(observation["disks"][name] is False for name in plan.disks)
        and observation["l4_usage"] == 0
    )


def _observed_evidence_matches(state: Mapping[str, Any], observation: Mapping[str, Any], platform: str) -> bool:
    job = observation["clients"][platform]
    return job["job_state"] == "passed" and job["evidence_digest"] == state[f"{platform}_evidence_digest"]


def reconcile(
    state_value: Mapping[str, Any],
    observation_value: Mapping[str, Any],
    plan: RunPlan,
) -> dict[str, Any]:
    state = validate_state(state_value, plan)
    observation = validate_observation(observation_value, plan)
    phase = state["phase"]
    windows_present = observation["instances"][plan.windows.instance]["present"]
    linux_present = observation["instances"][plan.linux.instance]["present"]
    windows_job = observation["clients"]["windows"]["job_state"]
    linux_job = observation["clients"]["linux"]["job_state"]
    observed_at = observation["observed_at_unix"]

    if phase in TERMINAL_PHASES:
        if not _resources_absent(observation, plan):
            raise Gate14ControllerError("resources returned after terminal cleanup")
        if phase == "CLEANED_PASS" and not (
            _observed_evidence_matches(state, observation, "windows")
            and _observed_evidence_matches(state, observation, "linux")
        ):
            raise Gate14ControllerError("terminal evidence binding is inconsistent")
        return state
    if phase != "CLEANING_FAILED":
        deadline = (
            plan.windows.termination_unix if phase in {"ABSENT", "WINDOWS_RUNNING"} else plan.linux.termination_unix
        )
        if observed_at >= deadline:
            if _resources_absent(observation, plan):
                return _next(
                    state,
                    phase="CLEANED_FAILURE",
                    failure_code="run-expired",
                    cleanup_verified=True,
                    next_action="none",
                )
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="run-expired",
                next_action="cleanup_failure",
            )
    if phase == "CLEANING_FAILED":
        if _resources_absent(observation, plan):
            return _next(
                state,
                phase="CLEANED_FAILURE",
                cleanup_verified=True,
                next_action="none",
            )
        return _next(state, next_action="cleanup_failure")
    if phase == "ABSENT":
        orphan_disk = (observation["disks"][plan.windows.disk] and not windows_present) or (
            observation["disks"][plan.linux.disk] and not linux_present
        )
        if orphan_disk:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="orphaned-planned-disk",
                next_action="cleanup_failure",
            )
        if linux_present or linux_job != "absent":
            if _resources_absent(observation, plan):
                return _next(
                    state,
                    phase="CLEANED_FAILURE",
                    failure_code="unexpected-linux-state",
                    cleanup_verified=True,
                    next_action="none",
                )
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="unexpected-linux-state",
                next_action="cleanup_failure",
            )
        if windows_present:
            return _next(
                state,
                phase="WINDOWS_RUNNING",
                windows_consumed=True,
                next_action="none",
            )
        if windows_job != "absent":
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code="stale-windows-job",
                cleanup_verified=True,
                next_action="none",
            )
        return _next(state, next_action="start_windows")
    if phase == "WINDOWS_RUNNING":
        if linux_present or observation["disks"][plan.linux.disk] or not windows_present:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="windows-inventory-lost",
                next_action="cleanup_failure",
            )
        if state["next_action"] == "collect_windows" and not _observed_evidence_matches(state, observation, "windows"):
            raise Gate14ControllerError("reported Windows evidence changed")
        if windows_job in {"starting", "running"}:
            return _next(state, next_action="none")
        if windows_job == "passed":
            return _next(
                state,
                windows_evidence_digest=observation["clients"]["windows"]["evidence_digest"],
                next_action="collect_windows",
            )
        return _next(
            state,
            phase="CLEANING_FAILED",
            failure_code="windows-job-failed",
            next_action="cleanup_failure",
        )
    if phase == "WINDOWS_DELETING":
        if not _observed_evidence_matches(state, observation, "windows"):
            raise Gate14ControllerError("validated Windows evidence is unavailable")
        if windows_present or observation["disks"][plan.windows.disk]:
            if linux_present:
                return _next(
                    state,
                    phase="CLEANING_FAILED",
                    failure_code="clients-overlapped",
                    next_action="cleanup_failure",
                )
            return _next(state, next_action="delete_windows")
        if linux_present:
            return _next(
                state,
                phase="LINUX_RUNNING",
                linux_consumed=True,
                next_action="none",
            )
        if observation["disks"][plan.linux.disk]:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="orphaned-linux-disk",
                next_action="cleanup_failure",
            )
        if linux_job != "absent":
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code="stale-linux-job",
                cleanup_verified=True,
                next_action="none",
            )
        return _next(state, next_action="start_linux")
    if phase == "LINUX_RUNNING":
        if observation["disks"][plan.windows.disk]:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="orphaned-windows-disk",
                next_action="cleanup_failure",
            )
        if not _observed_evidence_matches(state, observation, "windows"):
            raise Gate14ControllerError("validated Windows evidence is unavailable")
        if windows_present or not linux_present:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="linux-inventory-lost",
                next_action="cleanup_failure",
            )
        if state["next_action"] == "collect_linux" and not _observed_evidence_matches(state, observation, "linux"):
            raise Gate14ControllerError("reported Linux evidence changed")
        if linux_job in {"starting", "running"}:
            return _next(state, next_action="none")
        if linux_job == "passed":
            return _next(
                state,
                linux_evidence_digest=observation["clients"]["linux"]["evidence_digest"],
                next_action="collect_linux",
            )
        return _next(
            state,
            phase="CLEANING_FAILED",
            failure_code="linux-job-failed",
            next_action="cleanup_failure",
        )
    if phase == "LINUX_DELETING":
        if windows_present or observation["disks"][plan.windows.disk]:
            return _next(
                state,
                phase="CLEANING_FAILED",
                failure_code="windows-resources-returned",
                next_action="cleanup_failure",
            )
        if not (
            _observed_evidence_matches(state, observation, "windows")
            and _observed_evidence_matches(state, observation, "linux")
        ):
            raise Gate14ControllerError("validated platform evidence is unavailable")
        if not _resources_absent(observation, plan):
            return _next(state, next_action="delete_linux")
        if state["windows_evidence_digest"] is None or state["linux_evidence_digest"] is None:
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code="evidence-missing",
                cleanup_verified=True,
                next_action="none",
            )
        return _next(
            state,
            phase="CLEANED_PASS",
            cleanup_verified=True,
            next_action="none",
        )
    raise Gate14ControllerError("unhandled lifecycle phase")


def issue_calibration_challenge(
    state_value: Mapping[str, Any],
    plan: RunPlan,
    platform: str,
    challenge_path: Path,
    checkpoint_path: Path,
    *,
    issued_at_unix: int | None = None,
    nonce: str | None = None,
) -> tuple[dict[str, Any], Mapping[str, Any]]:
    state = validate_state(state_value, plan)
    expected_phase = "WINDOWS_RUNNING" if platform == "windows" else "LINUX_RUNNING"
    if (
        state["phase"] != expected_phase
        or state["next_action"] != "none"
        or state[f"{platform}_challenge_sha256"] is not None
        or state[f"{platform}_challenge_consumed"]
    ):
        raise Gate14ControllerError("calibration challenge is out of sequence")
    client = plan.windows if platform == "windows" else plan.linux
    issued = int(time.time()) if issued_at_unix is None else issued_at_unix
    lifetime = min(challenge_contract.MAX_LIFETIME_SECONDS, client.termination_unix - issued)
    if lifetime < challenge_contract.MIN_LIFETIME_SECONDS:
        raise Gate14ControllerError("calibration challenge would outlive the host")
    try:
        checkpoint = packaged_lifecycle.load_checkpoint_for_controller(
            checkpoint_path,
            run_id=plan.run_id,
            platform=platform,
            source_commit=client.source_commit,
            package_sha256=client.package_sha256,
            now_unix=issued,
        )
        checkpoint_sha256 = packaged_lifecycle.checkpoint_digest(checkpoint)
    except packaged_lifecycle.Gate14LifecycleError as exc:
        raise Gate14ControllerError("challenge-ready checkpoint is invalid") from exc
    if Path(challenge_path).exists():
        value = challenge_contract.validate(
            challenge_contract.load(challenge_path),
            run_id=plan.run_id,
            platform=platform,
            source_commit=client.source_commit,
            package_sha256=client.package_sha256,
            checkpoint_sha256=checkpoint_sha256,
            now_unix=issued,
        )
        if value["controller_state_revision"] != state["revision"]:
            raise Gate14ControllerError("existing calibration challenge is stale")
    else:
        value = challenge_contract.create(
            run_id=plan.run_id,
            platform=platform,
            source_commit=client.source_commit,
            package_sha256=client.package_sha256,
            checkpoint_sha256=checkpoint_sha256,
            controller_state_revision=state["revision"],
            issued_at_unix=issued,
            lifetime_seconds=lifetime,
            nonce=nonce,
        )
        challenge_contract.write_new(challenge_path, value)
    challenge_sha256 = challenge_contract.digest(value)
    return (
        _next(
            state,
            **{f"{platform}_challenge_sha256": challenge_sha256},
        ),
        value,
    )


def collect_platform(
    state_value: Mapping[str, Any],
    plan: RunPlan,
    platform: str,
    evidence_path: Path,
    challenge_path: Path,
) -> dict[str, Any]:
    state = validate_state(state_value, plan)
    expected_phase = "WINDOWS_RUNNING" if platform == "windows" else "LINUX_RUNNING"
    expected_action = f"collect_{platform}"
    if state["phase"] != expected_phase or state["next_action"] != expected_action:
        raise Gate14ControllerError("collect is out of sequence")
    payload = _regular_bytes(evidence_path)
    summary = acceptance.validate_platform_document(_strict_json(payload))
    client = plan.windows if platform == "windows" else plan.linux
    challenge_value = challenge_contract.validate(
        challenge_contract.load(challenge_path),
        run_id=plan.run_id,
        platform=platform,
        source_commit=client.source_commit,
        package_sha256=client.package_sha256,
    )
    challenge_sha256 = challenge_contract.digest(challenge_value)
    if (
        challenge_value["controller_state_revision"] >= state["revision"]
        or state[f"{platform}_challenge_consumed"]
        or state[f"{platform}_challenge_sha256"] != challenge_sha256
    ):
        raise Gate14ControllerError("calibration challenge state is invalid")
    if summary["calibration_challenge_sha256"] != challenge_sha256:
        raise Gate14ControllerError("platform evidence used a different calibration challenge")
    if (
        summary["run_id"] != plan.run_id
        or summary["platform"] != platform
        or summary["source_commit"] != client.source_commit
        or summary["package_sha256"] != client.package_sha256
        or summary["model_id"] != client.model_id
        or summary["manifest_digest"] != client.manifest_digest
    ):
        raise Gate14ControllerError("platform evidence does not match the plan")
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    if digest != state[f"{platform}_evidence_digest"]:
        raise Gate14ControllerError("collected evidence changed after host completion")
    return _next(
        state,
        **{
            f"{platform}_evidence_digest": digest,
            f"{platform}_challenge_consumed": True,
            "phase": f"{platform.upper()}_DELETING",
            "next_action": f"delete_{platform}",
        },
    )


def begin_cleanup(
    state_value: Mapping[str, Any],
    plan: RunPlan,
    failure_code: str,
) -> dict[str, Any]:
    state = validate_state(state_value, plan)
    if state["phase"] in TERMINAL_PHASES:
        return state
    _string(failure_code, re.compile(r"[a-z0-9][a-z0-9-]{0,63}"))
    return _next(
        state,
        phase="CLEANING_FAILED",
        failure_code=failure_code,
        next_action="cleanup_failure",
    )


def load_state(path: Path, plan: RunPlan) -> dict[str, Any]:
    return validate_state(_strict_json(_regular_bytes(path)), plan)


def save_state(path: Path, state_value: Mapping[str, Any], plan: RunPlan) -> None:
    state = validate_state(state_value, plan)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(state, sort_keys=True, separators=(",", ":")) + os.linesep).encode("utf-8")
    handle = tempfile.NamedTemporaryFile(
        mode="wb",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def _common_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("start", "status", "challenge", "collect", "cleanup"))
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--observation", type=Path)
    parser.add_argument("--platform", choices=("windows", "linux"))
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--challenge", type=Path)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--failure-code", default="operator-cleanup")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _common_parser().parse_args(argv)
    try:
        plan = load_plan(args.authorization, args.ledger)
        state = load_state(args.state, plan) if args.state.exists() else initial_state(plan)
        result: Mapping[str, Any]
        if args.operation in {"start", "status"}:
            if args.observation is None:
                raise Gate14ControllerError("observation is required")
            state = reconcile(
                state,
                _strict_json(_regular_bytes(args.observation)),
                plan,
            )
            result = state
        elif args.operation == "challenge":
            if args.platform is None or args.challenge is None or args.checkpoint is None:
                raise Gate14ControllerError("challenge platform, checkpoint, and output are required")
            state, result = issue_calibration_challenge(
                state,
                plan,
                args.platform,
                args.challenge,
                args.checkpoint,
            )
        elif args.operation == "collect":
            if args.platform is None or args.evidence is None or args.challenge is None:
                raise Gate14ControllerError("platform evidence and challenge are required")
            state = collect_platform(
                state,
                plan,
                args.platform,
                args.evidence,
                args.challenge,
            )
            result = state
        else:
            state = begin_cleanup(state, plan, args.failure_code)
            if args.observation is not None:
                state = reconcile(
                    state,
                    _strict_json(_regular_bytes(args.observation)),
                    plan,
                )
            result = state
        save_state(args.state, state, plan)
    except (
        Gate14ControllerError,
        challenge_contract.Gate14ChallengeError,
        acceptance.Gate14EvidenceError,
    ) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
