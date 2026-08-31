"""Durable state contract for one bounded Gate 13 GCP lifecycle.

This module is deliberately provider-command agnostic.  The paid-run adapter supplies a
fresh, exact provider/host observation before every transition and executes only the
returned allowlisted action.  Persisting the transition before returning makes a local
operator crash recoverable: the next invocation inventories first and either reattaches
to the same durable host job or proceeds to cleanup.

A lifecycle is never resumed after a product attempt fails.  Such a client is consumed
for acceptance even when its product-owned files were removed successfully.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import gate13_packaged_lifecycle as lifecycle
import qualification_cost_guard as cost_guard

SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 1_048_576
MAX_STATE_BYTES = 262_144
MIN_ROUTE_RUNWAY_SECONDS = 3_600
ALLOWED_COMBINED_CLOUD_CEILINGS = frozenset({100.0, 500.0})
PROTECTED_INSTANCE = "communityai-bootstrap-1"

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_NAME_RE = re.compile(r"[a-z](?:[-a-z0-9]{0,61}[a-z0-9])?")
_DIGEST_RE = re.compile(r"(?:sha256:)?[0-9a-f]{64}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")

PHASES = {
    "ABSENT",
    "ROUTE_STARTING",
    "ROUTE_ACCEPTING",
    "ROUTE_ACCEPTED",
    "WINDOWS_RUNNING",
    "WINDOWS_COLLECTING",
    "WINDOWS_COLLECTED",
    "WINDOWS_DELETING",
    "LINUX_RUNNING",
    "LINUX_COLLECTING",
    "LINUX_COLLECTED",
    "LINUX_DELETING",
    "ROUTE_DELETING",
    "CLEANING_FAILED",
    "CLEANED_PASS",
    "CLEANED_FAILURE",
}
TERMINAL_PHASES = {"CLEANED_PASS", "CLEANED_FAILURE"}
JOB_STATES = {"absent", "starting", "running", "passed", "failed", "ambiguous"}
ACTION_STATES = {
    "start_route",
    "accept_route",
    "start_windows",
    "collect_windows",
    "delete_windows",
    "start_linux",
    "collect_linux",
    "delete_linux",
    "delete_route",
    "cleanup_failure",
    "none",
}
CLEANUP_ACTIONS = frozenset({"delete_windows", "delete_linux", "delete_route", "cleanup_failure"})

_STATE_FIELDS = {
    "schema_version",
    "run_id",
    "authorization_sha256",
    "provider_plan_digest",
    "revision",
    "phase",
    "failure_code",
    "route_acceptance_digest",
    "windows_evidence_digest",
    "linux_evidence_digest",
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
    "firewalls",
    "protected_bootstrap_running",
    "route_acceptance",
    "clients",
}
_INSTANCE_FIELDS = {
    "present",
    "run_id",
    "source_commit",
    "termination_unix",
}
_CLIENT_FIELDS = {"job_state", "attempt_ordinal", "evidence_digest"}
_ROUTE_ACCEPTANCE_FIELDS = {"job_state", "evidence_digest"}


class RunControllerError(ValueError):
    """The run state, authorization, or provider observation failed closed."""


@dataclass(frozen=True)
class RunPlan:
    run_id: str
    authorization_sha256: str
    provider_plan_digest: str
    ledger_state: str
    project: str
    zone: str
    route_instance: str
    route_disk: str
    route_firewalls: tuple[str, str]
    route_source_commit: str
    windows_instance: str
    windows_disk: str
    windows_source_commit: str
    linux_instance: str
    linux_disk: str
    linux_source_commit: str
    windows_package_sha256: str
    windows_package_bytes: int
    linux_package_sha256: str
    linux_package_bytes: int
    qwen_manifest: str
    gemma_manifest: str
    clients_may_run_concurrently: bool

    @property
    def instance_names(self) -> tuple[str, str, str]:
        return (self.route_instance, self.windows_instance, self.linux_instance)

    @property
    def disk_names(self) -> tuple[str, str, str]:
        return (self.route_disk, self.windows_disk, self.linux_disk)


def _reject_constant(_value: str) -> None:
    raise RunControllerError("invalid JSON")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise RunControllerError("duplicate JSON field")
        value[key] = item
    return value


def _strict_json_bytes(payload: bytes, maximum: int = MAX_JSON_BYTES) -> Mapping[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= maximum:
        raise RunControllerError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RunControllerError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise RunControllerError("JSON root is invalid")
    return value


def _regular_bytes(path: Path, maximum: int) -> bytes:
    path = Path(path)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RunControllerError("required file is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise RunControllerError("required file is unsafe")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise RunControllerError("required file is unreadable") from exc


def _mapping(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RunControllerError(f"{label} schema is invalid")
    return value


def _string(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RunControllerError(f"{label} is invalid")
    return value


def _boolean(value: Any, label: str) -> bool:
    if type(value) is not bool:
        raise RunControllerError(f"{label} is invalid")
    return value


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise RunControllerError(f"{label} is invalid")
    return value


def _provider_digest(provider_plan: Mapping[str, Any]) -> str:
    return cost_guard._provider_plan_digest(provider_plan)


def load_plan(authorization_path: Path, ledger_path: Path) -> RunPlan:
    authorization_payload = _regular_bytes(authorization_path, MAX_JSON_BYTES)
    authorization = _strict_json_bytes(authorization_payload)
    if authorization.get("schema_version") != 1 or authorization.get("gate") != 13:
        raise RunControllerError("authorization scope is invalid")
    if authorization.get("result") != "authorized":
        raise RunControllerError("authorization is not active")

    run_id = _string(authorization.get("run_id"), _RUN_RE, "run id")
    provider_plan = authorization.get("provider_plan")
    if not isinstance(provider_plan, dict):
        raise RunControllerError("provider plan is invalid")
    provider_plan_digest = _provider_digest(provider_plan)
    if authorization.get("provider_plan_digest") != provider_plan_digest:
        raise RunControllerError("provider plan digest changed")

    authorization_section = authorization.get("authorization")
    if not isinstance(authorization_section, dict):
        raise RunControllerError("cost authorization is invalid")
    try:
        ceiling = float(authorization_section["combined_cloud_ceiling_usd"])
        before = float(authorization_section["ledger_committed_before_run_usd"])
        maximum = float(authorization_section["maximum_estimate_usd"])
        remaining = float(authorization_section["remaining_after_run_maximum_usd"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RunControllerError("cost authorization is invalid") from exc
    if (
        not all(math.isfinite(value) for value in (ceiling, before, maximum, remaining))
        or ceiling not in ALLOWED_COMBINED_CLOUD_CEILINGS
        or before < 0
        or maximum <= 0
        or before + maximum > ceiling
        or abs((ceiling - before - maximum) - remaining) > 0.001
        or authorization_section.get("reservation_recorded") is not True
        or authorization_section.get("provisioning_authorized_after_fail_closed_preflight") is not True
    ):
        raise RunControllerError("cost authorization is inconsistent")

    prohibited = authorization.get("prohibited")
    if not isinstance(prohibited, dict) or any(value != 0 or type(value) is not int for value in prohibited.values()):
        raise RunControllerError("prohibited work is present")

    source = authorization.get("source")
    immutable = authorization.get("immutable_inputs")
    route = provider_plan.get("route")
    clients = provider_plan.get("clients")
    sequencing = provider_plan.get("sequencing")
    if not all(isinstance(value, dict) for value in (source, immutable, route, sequencing)):
        raise RunControllerError("authorization bindings are invalid")
    if (
        not isinstance(clients, list)
        or len(clients) != 2
        or sequencing.get("route_live_for_both_lifecycles") is not True
        or sequencing.get("all_16_phases_required_per_platform") is not True
        or sequencing.get("exact_cleanup_before_pass") is not True
    ):
        raise RunControllerError("execution sequencing is invalid")

    by_platform = {
        client.get("platform"): client
        for client in clients
        if isinstance(client, dict) and isinstance(client.get("platform"), str)
    }
    if set(by_platform) != {"windows", "linux"}:
        raise RunControllerError("client plan is invalid")
    windows = by_platform["windows"]
    linux = by_platform["linux"]

    project = _string(provider_plan.get("project"), _NAME_RE, "project")
    route_instance = _string(route.get("instance"), _NAME_RE, "route instance")
    zone = route.get("zone")
    if not isinstance(zone, str) or not zone or route_instance == PROTECTED_INSTANCE:
        raise RunControllerError("route target is invalid")
    if windows.get("zone") != zone or linux.get("zone") != zone:
        raise RunControllerError("client zones are inconsistent")
    firewalls = route.get("firewalls")
    if not isinstance(firewalls, list) or len(firewalls) != 2:
        raise RunControllerError("firewall plan is invalid")
    firewall_names = tuple(_string(value, _NAME_RE, "firewall") for value in firewalls)
    instance_names = (
        route_instance,
        _string(windows.get("instance"), _NAME_RE, "Windows instance"),
        _string(linux.get("instance"), _NAME_RE, "Linux instance"),
    )
    if len(set(instance_names)) != 3 or PROTECTED_INSTANCE in instance_names:
        raise RunControllerError("instance targets are unsafe")

    ledger = _regular_bytes(ledger_path, MAX_JSON_BYTES * 4).decode("utf-8")
    ledger_rows = [line for line in ledger.splitlines() if line.startswith(f"| {run_id} |")]
    if len(ledger_rows) != 1 or provider_plan_digest not in ledger_rows[0]:
        raise RunControllerError("ledger reservation is absent")
    ledger_cells = [cell.strip() for cell in ledger_rows[0].strip().strip("|").split("|")]
    if len(ledger_cells) != 7 or ledger_cells[0] != run_id:
        raise RunControllerError("ledger reservation is invalid")
    ledger_state = ledger_cells[-1]
    if ledger_state not in {"RESERVED", "CLEANED-COMMITTED", "CLEANED-RELEASED"}:
        raise RunControllerError("ledger state is invalid")

    windows_package = immutable.get("windows_package")
    linux_package = immutable.get("linux_package")
    if not isinstance(windows_package, dict) or not isinstance(linux_package, dict):
        raise RunControllerError("package bindings are invalid")

    route_source = _string(source.get("route_runtime_commit"), _COMMIT_RE, "route source")
    package_source = _string(source.get("package_commit"), _COMMIT_RE, "package source")
    qwen_manifest = _string(immutable.get("qwen_manifest"), _DIGEST_RE, "Qwen manifest")
    gemma_manifest = _string(immutable.get("gemma_manifest"), _DIGEST_RE, "Gemma manifest")
    windows_sha = _string(windows_package.get("sha256"), _DIGEST_RE, "Windows package digest")
    linux_sha = _string(linux_package.get("sha256"), _DIGEST_RE, "Linux package digest")

    authorization_sha256 = "sha256:" + hashlib.sha256(authorization_payload).hexdigest()
    return RunPlan(
        run_id=run_id,
        authorization_sha256=authorization_sha256,
        provider_plan_digest=provider_plan_digest,
        ledger_state=ledger_state,
        project=project,
        zone=zone,
        route_instance=route_instance,
        route_disk=route_instance,
        route_firewalls=(firewall_names[0], firewall_names[1]),
        route_source_commit=route_source,
        windows_instance=instance_names[1],
        windows_disk=instance_names[1],
        windows_source_commit=package_source,
        linux_instance=instance_names[2],
        linux_disk=instance_names[2],
        linux_source_commit=package_source,
        windows_package_sha256=windows_sha,
        windows_package_bytes=_integer(windows_package.get("bytes"), "Windows package bytes", minimum=1),
        linux_package_sha256=linux_sha,
        linux_package_bytes=_integer(linux_package.get("bytes"), "Linux package bytes", minimum=1),
        qwen_manifest=qwen_manifest,
        gemma_manifest=gemma_manifest,
        clients_may_run_concurrently=_boolean(
            sequencing.get("clients_may_run_concurrently"),
            "client concurrency policy",
        ),
    )


def initial_state(plan: RunPlan) -> dict[str, Any]:
    if plan.ledger_state != "RESERVED":
        raise RunControllerError("authorization is not reserved for provisioning")
    if plan.clients_may_run_concurrently:
        raise RunControllerError("concurrent clients are forbidden")
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": plan.run_id,
        "authorization_sha256": plan.authorization_sha256,
        "provider_plan_digest": plan.provider_plan_digest,
        "revision": 0,
        "phase": "ABSENT",
        "failure_code": None,
        "route_acceptance_digest": None,
        "windows_evidence_digest": None,
        "linux_evidence_digest": None,
        "windows_consumed": False,
        "linux_consumed": False,
        "cleanup_verified": False,
        "next_action": "start_route",
    }


def validate_state(raw: Mapping[str, Any], plan: RunPlan) -> dict[str, Any]:
    state = dict(_mapping(raw, _STATE_FIELDS, "state"))
    if (
        state["schema_version"] != STATE_SCHEMA_VERSION
        or state["run_id"] != plan.run_id
        or state["authorization_sha256"] != plan.authorization_sha256
        or state["provider_plan_digest"] != plan.provider_plan_digest
    ):
        raise RunControllerError("state authorization binding changed")
    _integer(state["revision"], "state revision")
    if state["phase"] not in PHASES or state["next_action"] not in ACTION_STATES:
        raise RunControllerError("state transition is invalid")
    for field in ("windows_consumed", "linux_consumed", "cleanup_verified"):
        _boolean(state[field], field)
    for field in ("route_acceptance_digest", "windows_evidence_digest", "linux_evidence_digest"):
        if state[field] is not None:
            _string(state[field], _DIGEST_RE, field)
    failure = state["failure_code"]
    if failure is not None and (not isinstance(failure, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", failure)):
        raise RunControllerError("failure code is invalid")
    if state["phase"] in TERMINAL_PHASES and state["cleanup_verified"] is not True:
        raise RunControllerError("terminal state lacks cleanup proof")
    return state


def load_state(path: Path, plan: RunPlan) -> dict[str, Any]:
    path = Path(path)
    if not path.exists():
        return initial_state(plan)
    return validate_state(_strict_json_bytes(_regular_bytes(path, MAX_STATE_BYTES), MAX_STATE_BYTES), plan)


def _atomic_write(path: Path, state: Mapping[str, Any]) -> None:
    payload = (json.dumps(state, allow_nan=False, separators=(",", ":"), sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_STATE_BYTES:
        raise RunControllerError("state exceeds its bound")
    path = Path(os.path.abspath(os.fspath(path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (path.is_symlink() or not path.is_file()):
        raise RunControllerError("state path is unsafe")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _present(mapping: Mapping[str, Any]) -> bool:
    return any(value is True for value in mapping.values())


def _instance_present(observation: Mapping[str, Any], name: str) -> bool:
    return bool(observation["instances"][name]["present"])


def validate_observation(raw: Mapping[str, Any], plan: RunPlan, now_unix: int) -> dict[str, Any]:
    observation = dict(_mapping(raw, _OBSERVATION_FIELDS, "observation"))
    if observation["schema_version"] != SCHEMA_VERSION or observation["run_id"] != plan.run_id:
        raise RunControllerError("observation binding is invalid")
    observed_at = _integer(observation["observed_at_unix"], "observation time", minimum=1)
    if abs(observed_at - now_unix) > 300:
        raise RunControllerError("observation is stale")
    _boolean(observation["protected_bootstrap_running"], "protected bootstrap state")
    if observation["protected_bootstrap_running"] is not True:
        raise RunControllerError("protected bootstrap is unavailable")

    instances = observation["instances"]
    disks = observation["disks"]
    firewalls = observation["firewalls"]
    if (
        not isinstance(instances, dict)
        or set(instances) != set(plan.instance_names)
        or not isinstance(disks, dict)
        or set(disks) != set(plan.disk_names)
        or not isinstance(firewalls, dict)
        or set(firewalls) != set(plan.route_firewalls)
    ):
        raise RunControllerError("resource inventory is not exact")
    expected_sources = {
        plan.route_instance: plan.route_source_commit,
        plan.windows_instance: plan.windows_source_commit,
        plan.linux_instance: plan.linux_source_commit,
    }
    for name, value in instances.items():
        item = _mapping(value, _INSTANCE_FIELDS, "instance")
        present = _boolean(item["present"], "instance presence")
        if present:
            if item["run_id"] != plan.run_id or item["source_commit"] != expected_sources[name]:
                raise RunControllerError("foreign exact-name instance is present")
            termination = _integer(item["termination_unix"], "termination deadline", minimum=1)
            if termination <= observed_at:
                raise RunControllerError("instance deadline expired")
        elif any(item[field] is not None for field in ("run_id", "source_commit", "termination_unix")):
            raise RunControllerError("absent instance carries identity")
    for value in (*disks.values(), *firewalls.values()):
        _boolean(value, "resource presence")

    route_acceptance = _mapping(observation["route_acceptance"], _ROUTE_ACCEPTANCE_FIELDS, "route acceptance")
    if route_acceptance["job_state"] not in JOB_STATES:
        raise RunControllerError("route acceptance state is invalid")
    if route_acceptance["evidence_digest"] is not None:
        _string(route_acceptance["evidence_digest"], _DIGEST_RE, "route acceptance digest")
    clients = observation["clients"]
    if not isinstance(clients, dict) or set(clients) != {"windows", "linux"}:
        raise RunControllerError("client job inventory is invalid")
    for value in clients.values():
        client = _mapping(value, _CLIENT_FIELDS, "client job")
        if client["job_state"] not in JOB_STATES:
            raise RunControllerError("client job state is invalid")
        _integer(client["attempt_ordinal"], "attempt ordinal", maximum=1)
        if client["evidence_digest"] is not None:
            _string(client["evidence_digest"], _DIGEST_RE, "client evidence digest")
    return observation


def _all_resources_absent(observation: Mapping[str, Any]) -> bool:
    return (
        not any(value["present"] for value in observation["instances"].values())
        and not _present(observation["disks"])
        and not _present(observation["firewalls"])
    )


def _fail(state: dict[str, Any], code: str, observation: Mapping[str, Any]) -> dict[str, Any]:
    state["failure_code"] = code
    state["phase"] = "CLEANING_FAILED"
    state["next_action"] = "cleanup_failure"
    for platform in ("windows", "linux"):
        if observation["clients"][platform]["job_state"] != "absent":
            state[f"{platform}_consumed"] = True
    return state


def begin_action(state: Mapping[str, Any], plan: RunPlan, *, action: str) -> dict[str, Any]:
    """Persist an action intent before its first provider or host mutation.

    A missing resource after one of these transitions is a consumed failed attempt, not
    permission to recreate it. Repeating ``start`` must inventory and reconcile the
    durable job instead of calling this function again.
    """

    current = validate_state(state, plan)
    if plan.ledger_state != "RESERVED" and action not in CLEANUP_ACTIONS:
        raise RunControllerError("authorization is not reserved for forward action")
    if action != current["next_action"] or action not in ACTION_STATES - {"none"}:
        raise RunControllerError("action intent is out of order")
    phases = {
        "start_route": "ROUTE_STARTING",
        "accept_route": "ROUTE_ACCEPTING",
        "start_windows": "WINDOWS_RUNNING",
        "collect_windows": "WINDOWS_COLLECTING",
        "delete_windows": "WINDOWS_DELETING",
        "start_linux": "LINUX_RUNNING",
        "collect_linux": "LINUX_COLLECTING",
        "delete_linux": "LINUX_DELETING",
        "delete_route": "ROUTE_DELETING",
        "cleanup_failure": "CLEANING_FAILED",
    }
    result = dict(current)
    result["phase"] = phases[action]
    result["next_action"] = "none"
    result["revision"] += 1
    return validate_state(result, plan)


def reconcile(
    state: Mapping[str, Any],
    observation: Mapping[str, Any],
    plan: RunPlan,
    *,
    now_unix: int,
) -> dict[str, Any]:
    current = validate_state(state, plan)
    observed = validate_observation(observation, plan, now_unix)
    result = dict(current)

    if current["phase"] in TERMINAL_PHASES:
        if not _all_resources_absent(observed):
            raise RunControllerError("resource reappeared after terminal cleanup")
        return result

    if _all_resources_absent(observed):
        if current["phase"] == "ABSENT":
            result["next_action"] = "start_route"
        elif current["phase"] in {"CLEANING_FAILED", "ROUTE_DELETING", "LINUX_COLLECTED", "LINUX_DELETING"}:
            passed = (
                current["failure_code"] is None
                and current["route_acceptance_digest"] is not None
                and current["windows_evidence_digest"] is not None
                and current["linux_evidence_digest"] is not None
            )
            result["phase"] = "CLEANED_PASS" if passed else "CLEANED_FAILURE"
            result["cleanup_verified"] = True
            result["next_action"] = "none"
        else:
            result["phase"] = "CLEANED_FAILURE"
            result["failure_code"] = "resources_disappeared_before_completion"
            result["cleanup_verified"] = True
            result["next_action"] = "none"
        result["revision"] += 1
        return validate_state(result, plan)

    route_present = _instance_present(observed, plan.route_instance)
    windows_present = _instance_present(observed, plan.windows_instance)
    linux_present = _instance_present(observed, plan.linux_instance)
    route_job = observed["route_acceptance"]["job_state"]
    windows_job = observed["clients"]["windows"]["job_state"]
    linux_job = observed["clients"]["linux"]["job_state"]
    windows_attempt = observed["clients"]["windows"]["attempt_ordinal"]
    linux_attempt = observed["clients"]["linux"]["attempt_ordinal"]

    if not route_present or route_job in {"failed", "ambiguous"}:
        return _fail(result, "route_failed_or_ambiguous", observed)
    route_deadline = observed["instances"][plan.route_instance]["termination_unix"]
    if route_deadline - now_unix < MIN_ROUTE_RUNWAY_SECONDS:
        return _fail(result, "route_runway_exhausted", observed)

    if route_job != "passed":
        if windows_present or linux_present:
            return _fail(result, "client_started_before_route_acceptance", observed)
        if current["phase"] == "ROUTE_ACCEPTING" and current["next_action"] == "none":
            if route_job == "absent":
                return _fail(result, "route_acceptance_disappeared", observed)
            result["phase"] = "ROUTE_ACCEPTING"
            result["next_action"] = "none"
        else:
            result["phase"] = "ROUTE_ACCEPTING"
            result["next_action"] = "accept_route"
    else:
        route_digest = observed["route_acceptance"]["evidence_digest"]
        if route_digest is None:
            return _fail(result, "route_acceptance_digest_absent", observed)
        result["route_acceptance_digest"] = route_digest
        if current["windows_evidence_digest"] is None and windows_attempt == 1 and windows_job == "absent":
            return _fail(result, "windows_attempt_disappeared", observed)
        if current["linux_evidence_digest"] is None and linux_attempt == 1 and linux_job == "absent":
            return _fail(result, "linux_attempt_disappeared", observed)
        if linux_present and current["windows_evidence_digest"] is None:
            return _fail(result, "linux_started_before_windows_evidence", observed)
        if current["phase"] == "WINDOWS_DELETING":
            if windows_present or observed["disks"][plan.windows_disk]:
                result["next_action"] = "delete_windows"
            else:
                result["phase"] = "WINDOWS_COLLECTED"
                result["next_action"] = "start_linux"
        elif current["phase"] == "LINUX_DELETING":
            if linux_present or observed["disks"][plan.linux_disk]:
                result["next_action"] = "delete_linux"
            else:
                result["phase"] = "LINUX_COLLECTED"
                result["next_action"] = "delete_route"
        elif windows_present:
            result["windows_consumed"] = windows_job != "absent"
            if windows_job in {"failed", "ambiguous"}:
                return _fail(result, "windows_failed_or_ambiguous", observed)
            if windows_job == "passed":
                result["phase"] = "WINDOWS_COLLECTING"
                result["next_action"] = "collect_windows"
            elif windows_job == "absent":
                if current["phase"] == "WINDOWS_RUNNING" and current["next_action"] == "none":
                    result["phase"] = "WINDOWS_RUNNING"
                    result["next_action"] = "none"
                else:
                    result["phase"] = "ROUTE_ACCEPTED"
                    result["next_action"] = "start_windows"
            else:
                result["phase"] = "WINDOWS_RUNNING"
                result["next_action"] = "none"
        elif current["windows_evidence_digest"] is None:
            if current["windows_consumed"]:
                return _fail(result, "windows_consumed_without_evidence", observed)
            if current["phase"] == "WINDOWS_RUNNING" and current["next_action"] == "none":
                return _fail(result, "windows_disappeared_after_start_intent", observed)
            result["phase"] = "ROUTE_ACCEPTED"
            result["next_action"] = "start_windows"
        elif linux_present:
            result["linux_consumed"] = linux_job != "absent"
            if linux_job in {"failed", "ambiguous"}:
                return _fail(result, "linux_failed_or_ambiguous", observed)
            if linux_job == "passed":
                result["phase"] = "LINUX_COLLECTING"
                result["next_action"] = "collect_linux"
            elif linux_job == "absent":
                if current["phase"] == "LINUX_RUNNING" and current["next_action"] == "none":
                    result["phase"] = "LINUX_RUNNING"
                    result["next_action"] = "none"
                else:
                    result["phase"] = "WINDOWS_COLLECTED"
                    result["next_action"] = "start_linux"
            else:
                result["phase"] = "LINUX_RUNNING"
                result["next_action"] = "none"
        elif current["linux_evidence_digest"] is None:
            if current["linux_consumed"]:
                return _fail(result, "linux_consumed_without_evidence", observed)
            if current["phase"] == "LINUX_RUNNING" and current["next_action"] == "none":
                return _fail(result, "linux_disappeared_after_start_intent", observed)
            result["phase"] = "WINDOWS_COLLECTED"
            result["next_action"] = "start_linux"
        else:
            result["phase"] = "LINUX_COLLECTED"
            result["next_action"] = "delete_route"

    result["revision"] += 1
    return validate_state(result, plan)


def collect_platform(
    state: Mapping[str, Any],
    plan: RunPlan,
    *,
    platform: str,
    evidence_payload: bytes,
    observed_digest: str,
) -> dict[str, Any]:
    current = validate_state(state, plan)
    if platform not in {"windows", "linux"}:
        raise RunControllerError("platform is invalid")
    expected_phase = "WINDOWS_COLLECTING" if platform == "windows" else "LINUX_COLLECTING"
    if current["phase"] != expected_phase:
        raise RunControllerError("evidence collection is out of order")
    digest = "sha256:" + hashlib.sha256(evidence_payload).hexdigest()
    if digest != observed_digest:
        raise RunControllerError("host evidence digest changed")
    try:
        raw = lifecycle.load_lifecycle_json(evidence_payload.decode("utf-8"))
        validated = lifecycle.validate_lifecycle_document(raw)
    except Exception as exc:
        raise RunControllerError("lifecycle evidence is invalid") from exc
    expected = {
        "windows": {
            "source_commit": plan.windows_source_commit,
            "package_sha256": plan.windows_package_sha256,
            "package_bytes": plan.windows_package_bytes,
            "model_id": "Qwen3.5 2B",
            "manifest_digest": plan.qwen_manifest.removeprefix("sha256:"),
        },
        "linux": {
            "source_commit": plan.linux_source_commit,
            "package_sha256": plan.linux_package_sha256,
            "package_bytes": plan.linux_package_bytes,
            "model_id": "Gemma 4 E2B IT",
            "manifest_digest": plan.gemma_manifest.removeprefix("sha256:"),
        },
    }[platform]
    for field, value in expected.items():
        if validated.get(field) != value:
            raise RunControllerError("lifecycle evidence binding changed")

    result = dict(current)
    result[f"{platform}_evidence_digest"] = digest
    result[f"{platform}_consumed"] = True
    if platform == "windows":
        result["phase"] = "WINDOWS_DELETING"
        result["next_action"] = "delete_windows"
    else:
        result["phase"] = "LINUX_DELETING"
        result["next_action"] = "delete_linux"
    result["revision"] += 1
    return validate_state(result, plan)


def mark_client_absent(
    state: Mapping[str, Any],
    plan: RunPlan,
    *,
    platform: str,
    observation: Mapping[str, Any],
    now_unix: int,
) -> dict[str, Any]:
    current = validate_state(state, plan)
    expected = "WINDOWS_DELETING" if platform == "windows" else "LINUX_DELETING"
    if current["phase"] != expected or current[f"{platform}_evidence_digest"] is None:
        raise RunControllerError("client deletion is out of order")
    observed = validate_observation(observation, plan, now_unix)
    instance = plan.windows_instance if platform == "windows" else plan.linux_instance
    disk = plan.windows_disk if platform == "windows" else plan.linux_disk
    if _instance_present(observed, instance) or observed["disks"][disk]:
        raise RunControllerError("client absence is not proved")
    if not _instance_present(observed, plan.route_instance):
        raise RunControllerError("route disappeared during client deletion")
    result = dict(current)
    if platform == "windows":
        result["phase"] = "WINDOWS_COLLECTED"
        result["next_action"] = "start_linux"
    else:
        result["phase"] = "LINUX_COLLECTED"
        result["next_action"] = "delete_route"
    result["revision"] += 1
    return validate_state(result, plan)


def persist(path: Path, state: Mapping[str, Any], plan: RunPlan) -> None:
    _atomic_write(path, validate_state(state, plan))


def public_status(state: Mapping[str, Any], plan: RunPlan) -> dict[str, Any]:
    current = validate_state(state, plan)
    return {
        "schema_version": 1,
        "run_id": current["run_id"],
        "phase": current["phase"],
        "next_action": current["next_action"],
        "failure_code": current["failure_code"],
        "windows_consumed": current["windows_consumed"],
        "linux_consumed": current["linux_consumed"],
        "cleanup_verified": current["cleanup_verified"],
    }
