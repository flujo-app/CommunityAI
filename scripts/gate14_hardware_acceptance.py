"""Validate privacy-safe Gate 14 packaged hardware evidence.

The real host probes are deliberately separate from this verifier. They may use private
paths and provider details while running, but only the strict bounded documents accepted
here can enter the evidence archive. The aggregate binds the exact controller source,
production packages, manifests, Gate 9 envelopes, device profiles, and final cleanup.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import stat
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
PLATFORM_SCOPE = "gate14-packaged-hardware"
CLEANUP_SCOPE = "gate14-provider-cleanup"
AGGREGATE_SCOPE = "gate14-hardware-acceptance"
MAX_INPUT_BYTES = 262_144
MAX_DURATION_SECONDS = 300.0
MAX_BYTES = 1 << 50
MAX_BLOCKS = 512
PROTECTED_INSTANCE = "communityai-bootstrap-1"

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_NAME_RE = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
_PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]")
_ZONE_RE = re.compile(r"[a-z]+(?:-[a-z0-9]+)+-[a-z]")
_OS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+()/-]{0,127}")

MODEL_PROFILES = {
    "Qwen3.5 2B": {
        "manifest_digest": "sha256:3ba8528cb3c0d85e1ed048e0438a0d64cfbbc298944ed674caa6950d415f8e33",
        "revision_commit": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "selected_artifact_count": 8,
        "selected_artifact_bytes": 4_571_197_320,
        "total_blocks": 24,
    },
    "Gemma 4 E2B IT": {
        "manifest_digest": "sha256:2f8debbe0fcdf5af8d4c56c982210fa50aa584314968ae2617e2ccc2de9eafdd",
        "revision_commit": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "selected_artifact_count": 5,
        "selected_artifact_bytes": 10_278_818_149,
        "total_blocks": 35,
    },
}
EXPECTED_PLATFORM_MODELS = {"windows": "Qwen3.5 2B", "linux": "Gemma 4 E2B IT"}
EXPECTED_PLATFORM_OS = {"windows": "Windows Server 2022", "linux": "Ubuntu 24.04"}
EXPECTED_GATE9_ENVELOPES = {
    "windows": "sha256:cd68afb67d9b0f3cb8c82db0d3314ad89b558c20880998ea4d8c4493e9f4bc9f",
    "linux": "sha256:2eb0bcf6419ba085665fad34310453a1b9dc2e89d90e9177f41566df012996c8",
}
EXPECTED_GATE13_EVIDENCE_SHA256 = "sha256:ad4f892f4af9a9aee0dd428d74695981d0cca6241f79c0270c9fcea3a229b72e"

_DOCUMENT_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "platform",
    "result",
    "source_commit",
    "gate13_evidence_sha256",
    "package",
    "model",
    "hardware",
    "cache",
    "placement",
    "limits",
    "suspensions",
    "recovery",
    "pause",
    "restart",
    "unsupported_telemetry",
    "privacy",
    "qualification_temporaries_removed",
}
_PACKAGE_FIELDS = {
    "source_commit",
    "archive_sha256",
    "archive_bytes",
    "release_metadata_sha256",
}
_MODEL_FIELDS = {
    "id",
    "manifest_digest",
    "revision_commit",
    "gate9_envelope_sha256",
    "selected_artifact_count",
    "selected_artifact_bytes",
    "total_blocks",
}
_HARDWARE_FIELDS = {
    "os_name",
    "accelerator",
    "accelerator_count",
    "accelerator_memory_bytes",
}
_CACHE_FIELDS = {
    "verified_bytes_before",
    "verified_bytes_after",
    "transfer_bytes_during_gate",
    "digest_mismatch_count",
    "forbidden_model_acquired",
}
_PLACEMENT_FIELDS = {
    "automatic",
    "worker_count",
    "block_start",
    "block_end",
    "intent_published",
    "remote_acknowledged",
}
_LIMIT_FIELDS = {
    "disk_bytes",
    "vram_bytes",
    "bandwidth_mbps",
    "power_watts",
    "schedule_timezone",
    "resource_limit_count",
    "configured_and_resolved_match",
    "low_vram_rejected",
}
_SUSPENSION_FIELDS = {
    "kind",
    "suspended",
    "resumed",
    "desired_intent_preserved",
    "worker_count_during",
    "duration_seconds",
}
_RECOVERY_FIELDS = {
    "worker_crash_observed",
    "worker_restarted",
    "restart_seconds",
    "previous_worker_absent",
    "manifest_unchanged",
    "automatic_block_range_valid",
    "desired_intent_preserved",
}
_PAUSE_FIELDS = {
    "requested",
    "completed",
    "duration_seconds",
    "worker_count_after",
    "descendant_count_after",
}
_RESTART_FIELDS = {
    "node_restarted",
    "policy_persisted",
    "desired_intent_persisted",
    "worker_resumed",
    "duration_seconds",
    "cache_reused",
}
_UNSUPPORTED_FIELDS = {
    "device",
    "configured_limit",
    "start_rejected",
    "reason_code",
    "private_detail_retained",
}
_PRIVACY_FIELDS = {
    "prompt_retained",
    "response_retained",
    "token_identifiers_retained",
    "credentials_retained",
    "paths_retained",
    "endpoints_retained",
    "provider_output_retained",
}
_CLEANUP_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "result",
    "provider",
    "controller_source_commit",
    "provider_plan_digest",
    "project",
    "zone",
    "deleted_instances",
    "deleted_disks",
    "controller_terminal_state_sha256",
    "native_auth_revalidated",
    "expected_instances",
    "remaining_instances",
    "expected_disks",
    "remaining_disks",
    "remaining_firewalls",
    "l4_usage",
    "protected_bootstrap_running",
    "product_processes_remaining",
    "temporary_credentials_remaining",
}
_TERMINAL_STATE_FIELDS = {
    "schema_version",
    "run_id",
    "authorization_sha256",
    "provider_plan_digest",
    "revision",
    "phase",
    "failure_code",
    "windows_evidence_digest",
    "linux_evidence_digest",
    "windows_consumed",
    "linux_consumed",
    "cleanup_verified",
    "next_action",
}
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
}
_SEQUENCING_FIELDS = {
    "clients_may_run_concurrently",
    "windows_first",
    "fresh_host_per_platform",
}


class Gate14EvidenceError(ValueError):
    """A Gate 14 input was malformed, unsafe, incomplete, or inconsistent."""


def _reject_constant(_value: str) -> None:
    raise Gate14EvidenceError("invalid JSON")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise Gate14EvidenceError("duplicate JSON field")
        result[key] = value
    return result


def _regular_bytes(path: Path) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise Gate14EvidenceError("required evidence is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if (
        reparse
        or path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or not 1 <= metadata.st_size <= MAX_INPUT_BYTES
    ):
        raise Gate14EvidenceError("required evidence is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise Gate14EvidenceError("required evidence is unreadable") from exc


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_INPUT_BYTES:
        raise Gate14EvidenceError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise Gate14EvidenceError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise Gate14EvidenceError("JSON root is invalid")
    return value


def _mapping(value: Any, fields: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise Gate14EvidenceError("evidence schema is invalid")
    return value


def _true(value: Any) -> None:
    if value is not True:
        raise Gate14EvidenceError("required proof is absent")


def _false(value: Any) -> None:
    if value is not False:
        raise Gate14EvidenceError("forbidden retention or result is present")


def _integer(value: Any, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise Gate14EvidenceError("integer evidence is invalid")
    return value


def _number(value: Any, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float):
        raise Gate14EvidenceError("numeric evidence is invalid")
    rendered = float(value)
    if not math.isfinite(rendered) or not minimum <= rendered <= maximum:
        raise Gate14EvidenceError("numeric evidence is invalid")
    return rendered


def _string(value: Any, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise Gate14EvidenceError("string evidence is invalid")
    return value


def _validate_package(value: Any, source_commit: str) -> Mapping[str, Any]:
    package = _mapping(value, _PACKAGE_FIELDS)
    if package["source_commit"] != source_commit:
        raise Gate14EvidenceError("package source is inconsistent")
    _string(package["source_commit"], _COMMIT_RE)
    _string(package["archive_sha256"], _DIGEST_RE)
    _integer(package["archive_bytes"], 1, 8 * 1024**3)
    _string(package["release_metadata_sha256"], _DIGEST_RE)
    return package


def _validate_model(value: Any, platform: str) -> Mapping[str, Any]:
    model = _mapping(value, _MODEL_FIELDS)
    model_id = model["id"]
    if model_id != EXPECTED_PLATFORM_MODELS[platform]:
        raise Gate14EvidenceError("platform model is invalid")
    profile = MODEL_PROFILES[model_id]
    for field in (
        "manifest_digest",
        "revision_commit",
        "selected_artifact_count",
        "selected_artifact_bytes",
        "total_blocks",
    ):
        if model[field] != profile[field]:
            raise Gate14EvidenceError("model identity is inconsistent")
    if model["gate9_envelope_sha256"] != EXPECTED_GATE9_ENVELOPES[platform]:
        raise Gate14EvidenceError("Gate 9 envelope is inconsistent")
    return model


def _validate_hardware(value: Any, platform: str) -> Mapping[str, Any]:
    hardware = _mapping(value, _HARDWARE_FIELDS)
    _string(hardware["os_name"], _OS_RE)
    if hardware["os_name"] != EXPECTED_PLATFORM_OS[platform]:
        raise Gate14EvidenceError("platform operating system is inconsistent")
    if hardware["accelerator"] != "NVIDIA L4":
        raise Gate14EvidenceError("real L4 hardware is required")
    if hardware["accelerator_count"] != 1:
        raise Gate14EvidenceError("exactly one accelerator is required")
    _integer(hardware["accelerator_memory_bytes"], 20 * 1024**3, 32 * 1024**3)
    return hardware


def _validate_cache(value: Any, selected_bytes: int) -> None:
    cache = _mapping(value, _CACHE_FIELDS)
    if (
        cache["verified_bytes_before"] != selected_bytes
        or cache["verified_bytes_after"] != selected_bytes
        or cache["transfer_bytes_during_gate"] != 0
        or cache["digest_mismatch_count"] != 0
    ):
        raise Gate14EvidenceError("verified cache reuse is inconsistent")
    _false(cache["forbidden_model_acquired"])


def _validate_placement(value: Any, total_blocks: int) -> tuple[int, int]:
    placement = _mapping(value, _PLACEMENT_FIELDS)
    _true(placement["automatic"])
    if placement["worker_count"] != 1:
        raise Gate14EvidenceError("exactly one automatic worker is required")
    start = _integer(placement["block_start"], 0, total_blocks - 1)
    end = _integer(placement["block_end"], 1, total_blocks)
    if end <= start:
        raise Gate14EvidenceError("automatic block range is empty")
    _true(placement["intent_published"])
    _true(placement["remote_acknowledged"])
    return start, end


def _validate_limits(value: Any, selected_bytes: int, accelerator_memory: int) -> None:
    limits = _mapping(value, _LIMIT_FIELDS)
    disk = _integer(limits["disk_bytes"], selected_bytes, MAX_BYTES)
    vram = _integer(limits["vram_bytes"], 1, accelerator_memory)
    if disk < selected_bytes or vram >= accelerator_memory:
        raise Gate14EvidenceError("resource ceilings are not bounded")
    _number(limits["bandwidth_mbps"], 0.001, 1_000_000.0)
    _number(limits["power_watts"], 0.001, 1_000.0)
    if limits["schedule_timezone"] != "UTC" or limits["resource_limit_count"] != 5:
        raise Gate14EvidenceError("all five resource classes are required")
    _true(limits["configured_and_resolved_match"])
    _true(limits["low_vram_rejected"])


def _validate_suspensions(value: Any) -> None:
    if not isinstance(value, list) or len(value) != 3:
        raise Gate14EvidenceError("three suspension classes are required")
    seen: set[str] = set()
    for raw in value:
        item = _mapping(raw, _SUSPENSION_FIELDS)
        kind = item["kind"]
        if kind not in {"bandwidth", "power", "schedule"} or kind in seen:
            raise Gate14EvidenceError("suspension class is invalid")
        seen.add(kind)
        _true(item["suspended"])
        _true(item["resumed"])
        _true(item["desired_intent_preserved"])
        if item["worker_count_during"] != 0:
            raise Gate14EvidenceError("worker remained active while suspended")
        _number(item["duration_seconds"], 0.0, MAX_DURATION_SECONDS)


def _validate_recovery(value: Any) -> None:
    recovery = _mapping(value, _RECOVERY_FIELDS)
    for field in _RECOVERY_FIELDS - {"restart_seconds"}:
        _true(recovery[field])
    _number(recovery["restart_seconds"], 0.0, MAX_DURATION_SECONDS)


def _validate_pause(value: Any) -> None:
    pause = _mapping(value, _PAUSE_FIELDS)
    _true(pause["requested"])
    _true(pause["completed"])
    _number(pause["duration_seconds"], 0.0, MAX_DURATION_SECONDS)
    if pause["worker_count_after"] != 0 or pause["descendant_count_after"] != 0:
        raise Gate14EvidenceError("pause cleanup is incomplete")


def _validate_restart(value: Any) -> None:
    restart = _mapping(value, _RESTART_FIELDS)
    for field in _RESTART_FIELDS - {"duration_seconds"}:
        _true(restart[field])
    _number(restart["duration_seconds"], 0.0, MAX_DURATION_SECONDS)


def _validate_unsupported(value: Any) -> None:
    unsupported = _mapping(value, _UNSUPPORTED_FIELDS)
    if (
        unsupported["device"] != "cpu"
        or unsupported["configured_limit"] != "power_watts"
        or unsupported["reason_code"] != "power-telemetry-unavailable"
    ):
        raise Gate14EvidenceError("unsupported telemetry classification is invalid")
    _true(unsupported["start_rejected"])
    _false(unsupported["private_detail_retained"])


def _validate_privacy(value: Any) -> None:
    privacy = _mapping(value, _PRIVACY_FIELDS)
    for field in _PRIVACY_FIELDS:
        _false(privacy[field])


def validate_platform_document(value: Mapping[str, Any]) -> Mapping[str, Any]:
    document = _mapping(value, _DOCUMENT_FIELDS)
    if (
        document["schema_version"] != SCHEMA_VERSION
        or document["scope"] != PLATFORM_SCOPE
        or document["result"] != "passed"
    ):
        raise Gate14EvidenceError("platform evidence header is invalid")
    run_id = _string(document["run_id"], _RUN_RE)
    platform = document["platform"]
    if platform not in EXPECTED_PLATFORM_MODELS:
        raise Gate14EvidenceError("platform is invalid")
    source_commit = _string(document["source_commit"], _COMMIT_RE)
    gate13_evidence_sha256 = _string(document["gate13_evidence_sha256"], _DIGEST_RE)
    if gate13_evidence_sha256 != EXPECTED_GATE13_EVIDENCE_SHA256:
        raise Gate14EvidenceError("Gate 13 lifecycle evidence is inconsistent")
    package = _validate_package(document["package"], source_commit)
    model = _validate_model(document["model"], platform)
    hardware = _validate_hardware(document["hardware"], platform)
    _validate_cache(document["cache"], model["selected_artifact_bytes"])
    block_start, block_end = _validate_placement(document["placement"], model["total_blocks"])
    _validate_limits(
        document["limits"],
        model["selected_artifact_bytes"],
        hardware["accelerator_memory_bytes"],
    )
    _validate_suspensions(document["suspensions"])
    _validate_recovery(document["recovery"])
    _validate_pause(document["pause"])
    _validate_restart(document["restart"])
    _validate_unsupported(document["unsupported_telemetry"])
    _validate_privacy(document["privacy"])
    _true(document["qualification_temporaries_removed"])
    return {
        "run_id": run_id,
        "platform": platform,
        "source_commit": source_commit,
        "gate13_evidence_sha256": gate13_evidence_sha256,
        "package_sha256": package["archive_sha256"],
        "model_id": model["id"],
        "manifest_digest": model["manifest_digest"],
        "gate9_envelope_sha256": model["gate9_envelope_sha256"],
        "accelerator": hardware["accelerator"],
        "block_start": block_start,
        "block_end": block_end,
    }


def _digest(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _resource_names(value: Sequence[str], field: str) -> tuple[str, str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or len(value) != 2:
        raise Gate14EvidenceError(f"{field} inventory is invalid")
    names = tuple(_string(item, _NAME_RE) for item in value)
    if len(set(names)) != 2:
        raise Gate14EvidenceError(f"{field} inventory is not unique")
    if PROTECTED_INSTANCE in names:
        raise Gate14EvidenceError("protected resource is targeted")
    return names


def validate_authorization_document(
    value: Mapping[str, Any],
    *,
    run_id: str,
    source_commit: str,
    provider_plan_digest: str,
    project: str,
    zone: str,
    expected_instances: Sequence[str],
    expected_disks: Sequence[str],
    package_sha256: Mapping[str, str],
) -> Mapping[str, Any]:
    authorization = _mapping(value, _AUTH_FIELDS)
    if (
        authorization["schema_version"] != SCHEMA_VERSION
        or authorization["gate"] != 14
        or authorization["result"] != "authorized"
        or authorization["run_id"] != run_id
        or authorization["source_commit"] != source_commit
        or authorization["provider_plan_digest"] != provider_plan_digest
    ):
        raise Gate14EvidenceError("authorization binding is invalid")

    provider_plan = _mapping(authorization["provider_plan"], _PLAN_FIELDS)
    canonical_plan = json.dumps(provider_plan, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if (
        _digest(canonical_plan) != provider_plan_digest
        or provider_plan["project"] != project
        or provider_plan["zone"] != zone
    ):
        raise Gate14EvidenceError("authorized provider plan is inconsistent")
    sequencing = _mapping(provider_plan["sequencing"], _SEQUENCING_FIELDS)
    if (
        sequencing["clients_may_run_concurrently"] is not False
        or sequencing["windows_first"] is not True
        or sequencing["fresh_host_per_platform"] is not True
    ):
        raise Gate14EvidenceError("authorized sequencing is inconsistent")
    clients = provider_plan["clients"]
    if not isinstance(clients, list) or len(clients) != 2:
        raise Gate14EvidenceError("authorized client plan is invalid")
    by_platform = {
        item.get("platform"): item
        for item in clients
        if isinstance(item, dict) and isinstance(item.get("platform"), str)
    }
    if set(by_platform) != {"windows", "linux"}:
        raise Gate14EvidenceError("authorized platform plan is invalid")
    for index, platform in enumerate(("windows", "linux")):
        client = _mapping(by_platform[platform], _CLIENT_PLAN_FIELDS)
        model_id = EXPECTED_PLATFORM_MODELS[platform]
        if (
            client["platform"] != platform
            or client["instance"] != expected_instances[index]
            or client["disk"] != expected_disks[index]
            or client["source_commit"] != source_commit
            or client["package_sha256"] != package_sha256[platform]
            or client["model_id"] != model_id
            or client["manifest_digest"] != MODEL_PROFILES[model_id]["manifest_digest"]
        ):
            raise Gate14EvidenceError("authorized client binding is inconsistent")
        _integer(client["termination_unix"], 1)

    cost = _mapping(authorization["authorization"], _AUTHORIZATION_FIELDS)
    try:
        ceiling = Decimal(str(cost["combined_cloud_ceiling_usd"]))
        before = Decimal(str(cost["ledger_committed_before_run_usd"]))
        maximum = Decimal(str(cost["maximum_estimate_usd"]))
        remaining = Decimal(str(cost["remaining_after_run_maximum_usd"]))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise Gate14EvidenceError("cost authorization is invalid") from exc
    if (
        (ceiling, before, maximum, remaining)
        != (Decimal("100.00"), Decimal("56.00"), Decimal("44.00"), Decimal("0.00"))
        or not all(item.is_finite() for item in (ceiling, before, maximum, remaining))
        or cost["reservation_recorded"] is not True
        or cost["native_auth_revalidated"] is not True
        or cost["provisioning_authorized_after_fail_closed_preflight"] is not True
    ):
        raise Gate14EvidenceError("cost authorization is inconsistent")
    prohibited = authorization["prohibited"]
    if (
        not isinstance(prohibited, dict)
        or set(prohibited) != {"credits", "macos", "fly_gpu"}
        or any(type(item) is not int or item != 0 for item in prohibited.values())
    ):
        raise Gate14EvidenceError("prohibited work is present")
    return authorization


def validate_terminal_state(
    value: Mapping[str, Any],
    *,
    run_id: str,
    authorization_sha256: str,
    provider_plan_digest: str,
    windows_evidence_sha256: str,
    linux_evidence_sha256: str,
) -> Mapping[str, Any]:
    state = _mapping(value, _TERMINAL_STATE_FIELDS)
    if (
        state["schema_version"] != SCHEMA_VERSION
        or state["run_id"] != run_id
        or state["authorization_sha256"] != authorization_sha256
        or state["provider_plan_digest"] != provider_plan_digest
        or state["phase"] != "CLEANED_PASS"
        or state["failure_code"] is not None
        or state["windows_evidence_digest"] != windows_evidence_sha256
        or state["linux_evidence_digest"] != linux_evidence_sha256
        or state["windows_consumed"] is not True
        or state["linux_consumed"] is not True
        or state["cleanup_verified"] is not True
        or state["next_action"] != "none"
    ):
        raise Gate14EvidenceError("controller terminal state is inconsistent")
    _integer(state["revision"], 1)
    return state


def validate_cleanup_document(
    value: Mapping[str, Any],
    *,
    run_id: str,
    controller_source_commit: str,
    provider_plan_digest: str,
    project: str,
    zone: str,
    expected_instances: Sequence[str],
    expected_disks: Sequence[str],
    terminal_state_sha256: str,
) -> Mapping[str, Any]:
    cleanup = _mapping(value, _CLEANUP_FIELDS)
    if (
        cleanup["schema_version"] != SCHEMA_VERSION
        or cleanup["scope"] != CLEANUP_SCOPE
        or cleanup["run_id"] != run_id
        or cleanup["result"] != "passed"
        or cleanup["provider"] != "GCP"
        or cleanup["controller_source_commit"] != controller_source_commit
        or cleanup["provider_plan_digest"] != provider_plan_digest
        or cleanup["project"] != project
        or cleanup["zone"] != zone
        or cleanup["deleted_instances"] != list(expected_instances)
        or cleanup["deleted_disks"] != list(expected_disks)
        or cleanup["controller_terminal_state_sha256"] != terminal_state_sha256
    ):
        raise Gate14EvidenceError("cleanup evidence binding is invalid")
    _true(cleanup["native_auth_revalidated"])
    if cleanup["expected_instances"] != 2 or cleanup["expected_disks"] != 2:
        raise Gate14EvidenceError("cleanup target count is invalid")
    for field in (
        "remaining_instances",
        "remaining_disks",
        "remaining_firewalls",
        "l4_usage",
        "product_processes_remaining",
        "temporary_credentials_remaining",
    ):
        if cleanup[field] != 0 or type(cleanup[field]) is not int:
            raise Gate14EvidenceError("cleanup is incomplete")
    _true(cleanup["protected_bootstrap_running"])
    return cleanup


def validate_files(
    windows_path: Path,
    linux_path: Path,
    cleanup_path: Path,
    controller_source_commit: str,
    *,
    provider_plan_digest: str,
    project: str,
    zone: str,
    expected_instances: Sequence[str],
    expected_disks: Sequence[str],
    terminal_state_path: Path,
    authorization_path: Path,
) -> Mapping[str, Any]:
    controller_source_commit = _string(controller_source_commit, _COMMIT_RE)
    provider_plan_digest = _string(provider_plan_digest, _DIGEST_RE)
    project = _string(project, _PROJECT_RE)
    zone = _string(zone, _ZONE_RE)
    expected_instances = _resource_names(expected_instances, "instance")
    expected_disks = _resource_names(expected_disks, "disk")
    payloads = {
        "windows": _regular_bytes(windows_path),
        "linux": _regular_bytes(linux_path),
        "cleanup": _regular_bytes(cleanup_path),
        "terminal_state": _regular_bytes(terminal_state_path),
        "authorization": _regular_bytes(authorization_path),
    }
    windows = validate_platform_document(_strict_json(payloads["windows"]))
    linux = validate_platform_document(_strict_json(payloads["linux"]))
    if windows["platform"] != "windows" or linux["platform"] != "linux":
        raise Gate14EvidenceError("platform evidence ordering is invalid")
    if windows["run_id"] != linux["run_id"]:
        raise Gate14EvidenceError("run identity is inconsistent")
    if windows["source_commit"] != linux["source_commit"] or windows["source_commit"] != controller_source_commit:
        raise Gate14EvidenceError("package source identity is inconsistent")
    authorization_sha256 = _digest(payloads["authorization"])
    validate_authorization_document(
        _strict_json(payloads["authorization"]),
        run_id=windows["run_id"],
        source_commit=controller_source_commit,
        provider_plan_digest=provider_plan_digest,
        project=project,
        zone=zone,
        expected_instances=expected_instances,
        expected_disks=expected_disks,
        package_sha256={
            "windows": windows["package_sha256"],
            "linux": linux["package_sha256"],
        },
    )
    terminal_state_sha256 = _digest(payloads["terminal_state"])
    validate_terminal_state(
        _strict_json(payloads["terminal_state"]),
        run_id=windows["run_id"],
        authorization_sha256=authorization_sha256,
        provider_plan_digest=provider_plan_digest,
        windows_evidence_sha256=_digest(payloads["windows"]),
        linux_evidence_sha256=_digest(payloads["linux"]),
    )
    cleanup = validate_cleanup_document(
        _strict_json(payloads["cleanup"]),
        run_id=windows["run_id"],
        controller_source_commit=controller_source_commit,
        provider_plan_digest=provider_plan_digest,
        project=project,
        zone=zone,
        expected_instances=expected_instances,
        expected_disks=expected_disks,
        terminal_state_sha256=terminal_state_sha256,
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "scope": AGGREGATE_SCOPE,
        "run_id": windows["run_id"],
        "result": "passed",
        "controller_source_commit": controller_source_commit,
        "package_source_commit": windows["source_commit"],
        "provider_plan_digest": provider_plan_digest,
        "authorization_sha256": authorization_sha256,
        "platforms": [
            {
                **windows,
                "evidence_sha256": _digest(payloads["windows"]),
            },
            {
                **linux,
                "evidence_sha256": _digest(payloads["linux"]),
            },
        ],
        "cleanup": {
            "evidence_sha256": _digest(payloads["cleanup"]),
            "provider": cleanup["provider"],
            "project": project,
            "zone": zone,
            "deleted_instances": list(expected_instances),
            "deleted_disks": list(expected_disks),
            "terminal_state_sha256": terminal_state_sha256,
            "resource_absence_proved": True,
            "protected_bootstrap_running": True,
        },
        "credits_in_scope": False,
        "macos_in_scope": False,
        "privacy_safe": True,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--windows", type=Path, required=True)
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--cleanup", type=Path, required=True)
    parser.add_argument("--controller-state", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--controller-source-commit", required=True)
    parser.add_argument("--provider-plan-digest", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--zone", required=True)
    parser.add_argument("--instances", nargs=2, required=True)
    parser.add_argument("--disks", nargs=2, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = validate_files(
            args.windows,
            args.linux,
            args.cleanup,
            args.controller_source_commit,
            provider_plan_digest=args.provider_plan_digest,
            project=args.project,
            zone=args.zone,
            expected_instances=args.instances,
            expected_disks=args.disks,
            terminal_state_path=args.controller_state,
            authorization_path=args.authorization,
        )
    except Gate14EvidenceError as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
