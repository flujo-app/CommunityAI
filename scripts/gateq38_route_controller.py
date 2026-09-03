"""Durable no-provider controller for the Qwen3.8 complete-route attempt.

The controller never invokes a provider. Each operation consumes an exact bounded
observation, persists a source/plan-bound state, and emits at most one allowlisted
action for a separate provider adapter.
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
from contextlib import contextmanager
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping, Sequence

SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
GATE = "qwen3.8-complete-route"
MAX_JSON_BYTES = 262_144
PROTECTED_INSTANCE = "communityai-bootstrap-1"
EXPECTED_PROVIDER = "gcp"
EXPECTED_PROJECT = "community-ai-506321"
EXPECTED_REGION = "us-central1"
EXPECTED_ZONE = "us-central1-b"
EXPECTED_WORKER_MACHINE_TYPE = "g2-standard-8"
EXPECTED_BOOTSTRAP_MACHINE_TYPE = "e2-standard-2"
EXPECTED_ACCELERATOR_TYPE = "nvidia-l4"
EXPECTED_SOURCE_IMAGE = "deeplearning-platform-release/common-cu129-ubuntu-2404-nvidia-580-v20260831"
EXPECTED_DISK_TYPE = "pd-balanced"
EXPECTED_DISK_SIZE_GB = 50
EXPECTED_NETWORK = "communityai-discovery"
EXPECTED_SUBNET = "communityai-us-central1"
EXPECTED_MAX_LIFETIME_SECONDS = 39_600
EXPECTED_PRICED_DURATION_HOURS = Decimal("11.00")
EXPECTED_MANIFEST_DIGEST = "sha256:c4dfe76969bd769bf4b6bd28d08961a97eb2d73d588187c8dd4b9aa40b1055a4"
EXPECTED_MODEL_REVISION = "017b9c7af6b5689d5dd426a76e0bc077eb5ca20a"
EXPECTED_INDEX_DIGEST = "sha256:f0838c766951bdfe76d6afbdb2771a8f67aaa2231dedb3d33cebd817729843a2"
EXPECTED_BLOCK_PREFIX = "model.language_model.layers"
EXPECTED_ARTIFACTS_PER_SPAN = 18
VERIFIER_SOURCE_PATH = "src/drift/model_manifest.py"
READINESS_LEDGER_PATH = "docs/RELEASE_READINESS.md"
PROTECTION_SOURCE_PATH = "scripts/gate14_packaged_lifecycle.py"
REQUIRED_SOURCE_PATHS = {
    "docs/RELEASE_READINESS.md",
    "scripts/gate14_packaged_lifecycle.py",
    "scripts/gateq38_route_controller.py",
    "scripts/qualify_model_multimachine.py",
    "src/drift/model_manifest.py",
    "src/drift/server/server.py",
}
MAX_PLAN_REVALIDATION_AGE_SECONDS = 300
EXPECTED_SPANS = {
    "0:16": (
        6_095_829_165,
        "sha256:70c0c950845c0c53dc0269d525c755bc72e661cf4ded8a78a7b5f99d8d195d89",
    ),
    "16:32": (
        6_095_829_389,
        "sha256:01d4ca6e77a9564e6896343b0c8558619fcda78819eeafb0d49393a955460866",
    ),
    "32:48": (
        6_095_829_389,
        "sha256:4b3ac15527d87d2dbd089fc4ba4ab0dec4610a5e9870df1401473159b55138e5",
    ),
    "48:64": (
        6_095_829_389,
        "sha256:2e779c52ab2eb5156aa3cfba60e5d08b4dd691e0302101cbc1a39c24d45745e1",
    ),
}
EXPECTED_RESOURCE_KINDS = {
    "bootstrap_instance": 1,
    "bootstrap_disk": 1,
    "worker_instance": 4,
    "worker_disk": 4,
    "firewall": 1,
}
ACTIONS = {"start_route", "collect_route", "cleanup_route", "none"}
PHASES = {
    "ABSENT",
    "STARTING",
    "READY",
    "COLLECTING",
    "CLEANING",
    "CLEANED_PASS",
    "CLEANED_FAILURE",
}
TERMINAL_PHASES = {"CLEANED_PASS", "CLEANED_FAILURE"}
WORKER_STATES = {"absent", "starting", "ready", "failed"}
JOB_STATES = {"absent", "running", "passed", "failed"}

_RUN_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,62}")
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_COMMIT_RE = re.compile(r"[0-9a-f]{40}")
_REVISION_RE = re.compile(r"[0-9a-f]{40}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_PEER_RE = re.compile(r"[A-Za-z0-9]{20,128}")

_PLAN_FIELDS = {
    "schema_version",
    "gate",
    "run_id",
    "route_job_id",
    "source_commit",
    "manifest_digest",
    "model_revision",
    "deadline_unix",
    "authorization",
    "source_bindings",
    "resources",
    "workers",
}
_AUTH_FIELDS = {
    "combined_cloud_ceiling_usd",
    "ledger_committed_before_run_usd",
    "maximum_estimate_usd",
    "reservation_recorded",
    "native_auth_revalidated",
    "inventory_revalidated",
    "pricing_revalidated",
    "provisioning_authorized",
    "reservation_id",
    "reservation_record_path",
    "reservation_record_sha256",
    "reservation_record_byte_size",
    "preflight_record_path",
    "preflight_record_sha256",
    "preflight_record_byte_size",
    "readiness_ledger_sha256",
}
_BINDING_FIELDS = {"relative_path", "sha256", "byte_size"}
_RESOURCE_FIELDS = {"name", "kind", "provider", "region", "worker_id"}
_WORKER_FIELDS = {
    "worker_id",
    "machine_id",
    "instance",
    "disk",
    "span",
    "artifact_bytes",
    "artifact_set_digest",
    "cache_root",
}
_JOURNAL_FIELDS = {
    "schema_version",
    "run_id",
    "plan_digest",
    "start_action_id",
    "status",
    "issued_at_unix",
    "completed_at_unix",
    "terminal_phase",
    "terminal_revision",
    "failure_code",
    "evidence_digest",
    "cleanup_verified",
}
_STATE_FIELDS = {
    "schema_version",
    "run_id",
    "plan_digest",
    "revision",
    "phase",
    "failure_code",
    "evidence_digest",
    "cleanup_verified",
    "next_action",
}
_OBSERVATION_FIELDS = {
    "schema_version",
    "run_id",
    "observed_at_unix",
    "protected_bootstrap_running",
    "artifact_plan_revalidation",
    "resources",
    "workers",
    "route_job",
}
_REVALIDATION_FIELDS = {
    "verified_at_unix",
    "source_commit",
    "manifest_digest",
    "model_revision",
    "index_digest",
    "block_prefix",
    "worker_plan_digest",
    "verifier_source_sha256",
}
_OBS_RESOURCE_FIELDS = {
    "present",
    "kind",
    "provider",
    "region",
    "run_id",
    "source_commit",
    "deadline_unix",
    "plan_digest",
    "start_action_id",
    "worker_id",
}
_OBS_WORKER_FIELDS = {
    "state",
    "machine_id",
    "peer_id",
    "source_commit",
    "plan_digest",
    "worker_plan_digest",
    "start_action_id",
    "span",
    "manifest_digest",
    "artifact_bytes",
    "artifact_set_digest",
    "cache_root",
}
_ROUTE_JOB_FIELDS = {
    "state",
    "job_id",
    "collect_action_id",
    "run_id",
    "plan_digest",
    "source_commit",
    "manifest_digest",
    "worker_plan_digest",
    "evidence_digest",
    "route_record",
}
_ROUTE_RECORD_FIELDS = {
    "schema_version",
    "result",
    "run_id",
    "job_id",
    "collect_action_id",
    "plan_digest",
    "source_commit",
    "manifest_digest",
    "worker_plan_digest",
    "route_span",
    "session_id",
    "route_rpc_evidence_digest",
    "cleanup_ready",
    "worker_results",
}
_ROUTE_WORKER_RESULT_FIELDS = {
    "worker_id",
    "machine_id",
    "peer_id",
    "span",
    "source_commit",
    "manifest_digest",
    "artifact_bytes",
    "artifact_set_digest",
    "cache_root",
    "worker_evidence_digest",
}
_RESERVATION_FIELDS = {
    "schema_version",
    "reservation_id",
    "run_id",
    "combined_cloud_ceiling_usd",
    "ledger_committed_before_run_usd",
    "maximum_estimate_usd",
    "deadline_unix",
    "plan_digest",
    "execution_inventory_digest",
    "worker_plan_digest",
    "resource_costs",
    "readiness_ledger_sha256",
    "recorded_at_unix",
    "expires_at_unix",
    "reservation_recorded",
}
_COST_FIELDS = {
    "resource_name",
    "resource_spec_digest",
    "unit_rate_usd",
    "quantity",
    "duration_hours",
    "maximum_usd",
}
_RESOURCE_SPEC_FIELDS = {
    "resource_name",
    "kind",
    "provider",
    "region",
    "project",
    "zone",
    "machine_type",
    "accelerator_type",
    "accelerator_count",
    "source_image",
    "disk_type",
    "disk_size_gb",
    "network",
    "subnet",
    "max_lifetime_seconds",
}


_RPC_EVIDENCE_FIELDS = {
    "schema_version",
    "result",
    "run_id",
    "job_id",
    "collect_action_id",
    "plan_digest",
    "source_commit",
    "manifest_digest",
    "worker_plan_digest",
    "route_span",
    "session_id",
}
_WORKER_EVIDENCE_FIELDS = {
    "schema_version",
    "result",
    "run_id",
    "job_id",
    "collect_action_id",
    "plan_digest",
    "source_commit",
    "manifest_digest",
    "worker_plan_digest",
    "start_action_id",
    "worker_id",
    "machine_id",
    "peer_id",
    "span",
    "artifact_bytes",
    "artifact_set_digest",
    "cache_root",
}


_PREFLIGHT_FIELDS = {
    "schema_version",
    "run_id",
    "source_commit",
    "plan_digest",
    "execution_inventory_digest",
    "worker_plan_digest",
    "provider",
    "resource_names",
    "resource_specs",
    "pricing_source",
    "pricing_currency",
    "pricing_checked_at_unix",
    "gpu_quota_limit",
    "gpu_quota_usage",
    "required_gpu_count",
    "checked_at_unix",
    "native_auth_revalidated",
    "inventory_revalidated",
    "pricing_revalidated",
    "provisioning_authorized",
    "protected_bootstrap_running",
    "reservation_record_sha256",
}


class RouteControllerError(ValueError):
    """The route plan, observation, state, or transition failed closed."""


@dataclass(frozen=True)
class WorkerPlan:
    worker_id: str
    machine_id: str
    instance: str
    disk: str
    span: str
    artifact_bytes: int
    artifact_set_digest: str
    cache_root: str


@dataclass(frozen=True)
class ResourcePlan:
    name: str
    kind: str
    provider: str
    region: str
    worker_id: str | None


@dataclass(frozen=True)
class RoutePlan:
    run_id: str
    route_job_id: str
    source_commit: str
    manifest_digest: str
    model_revision: str
    deadline_unix: int
    authorization: Mapping[str, Any]
    source_bindings: tuple[Mapping[str, Any], ...]
    resources: tuple[ResourcePlan, ...]
    workers: tuple[WorkerPlan, ...]
    plan_digest: str

    @property
    def resource_by_name(self) -> dict[str, ResourcePlan]:
        return {resource.name: resource for resource in self.resources}

    @property
    def worker_by_id(self) -> dict[str, WorkerPlan]:
        return {worker.worker_id: worker for worker in self.workers}

    @property
    def worker_plan_digest(self) -> str:
        return _worker_plan_digest(self.workers)

    @property
    def execution_inventory_digest(self) -> str:
        return _canonical_digest(
            {
                "run_id": self.run_id,
                "route_job_id": self.route_job_id,
                "source_commit": self.source_commit,
                "manifest_digest": self.manifest_digest,
                "model_revision": self.model_revision,
                "deadline_unix": self.deadline_unix,
                "plan_digest": self.plan_digest,
                "source_bindings": [dict(binding) for binding in self.source_bindings],
                "worker_plan_digest": self.worker_plan_digest,
                "resources": [
                    {
                        "name": resource.name,
                        "kind": resource.kind,
                        "provider": resource.provider,
                        "region": resource.region,
                        "worker_id": resource.worker_id,
                        "resource_spec_digest": _canonical_digest(_expected_resource_spec(resource)),
                    }
                    for resource in self.resources
                ],
            }
        )


def _expected_resource_spec(resource: ResourcePlan) -> dict[str, Any]:
    is_worker_instance = resource.kind == "worker_instance"
    is_bootstrap_instance = resource.kind == "bootstrap_instance"
    is_instance = is_worker_instance or is_bootstrap_instance
    is_disk = resource.kind in {"bootstrap_disk", "worker_disk"}
    return {
        "resource_name": resource.name,
        "kind": resource.kind,
        "provider": EXPECTED_PROVIDER,
        "region": EXPECTED_REGION,
        "project": EXPECTED_PROJECT,
        "zone": EXPECTED_ZONE,
        "machine_type": (
            EXPECTED_WORKER_MACHINE_TYPE
            if is_worker_instance
            else EXPECTED_BOOTSTRAP_MACHINE_TYPE
            if is_bootstrap_instance
            else "none"
        ),
        "accelerator_type": EXPECTED_ACCELERATOR_TYPE if is_worker_instance else "none",
        "accelerator_count": 1 if is_worker_instance else 0,
        "source_image": EXPECTED_SOURCE_IMAGE if is_instance else "none",
        "disk_type": EXPECTED_DISK_TYPE if is_disk else "none",
        "disk_size_gb": EXPECTED_DISK_SIZE_GB if is_disk else 0,
        "network": EXPECTED_NETWORK,
        "subnet": EXPECTED_SUBNET,
        "max_lifetime_seconds": EXPECTED_MAX_LIFETIME_SECONDS,
    }


def _reject_constant(_value: str) -> None:
    raise RouteControllerError("invalid JSON constant")


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RouteControllerError("duplicate JSON field")
        result[key] = value
    return result


def _regular_bytes(path: Path, maximum: int = MAX_JSON_BYTES) -> bytes:
    descriptor: int | None = None
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise RouteControllerError("required file is unavailable") from exc
    reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
    if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode) or not 1 <= metadata.st_size <= maximum:
        raise RouteControllerError("required file is unsafe")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            getattr(item, "st_mtime_ns", int(item.st_mtime * 1_000_000_000)),
        )
        if not stat.S_ISREG(opened.st_mode) or identity(opened) != identity(metadata):
            raise RouteControllerError("required file identity changed while opening")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            payload = handle.read(maximum + 1)
        after = os.fstat(descriptor)
        if identity(after) != identity(opened) or len(payload) != opened.st_size:
            raise RouteControllerError("required file changed while reading")
        return payload
    except OSError as exc:
        raise RouteControllerError("required file is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _strict_json(payload: bytes) -> Mapping[str, Any]:
    if not 1 <= len(payload) <= MAX_JSON_BYTES:
        raise RouteControllerError("JSON size is invalid")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RouteControllerError("invalid JSON") from exc
    if not isinstance(value, dict):
        raise RouteControllerError("JSON root must be an object")
    return value


def _mapping(value: Any, fields: set[str], field: str) -> Mapping[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise RouteControllerError(f"{field} schema is invalid")
    return value


def _string(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise RouteControllerError(f"{field} is invalid")
    return value


def _integer(value: Any, field: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise RouteControllerError(f"{field} is invalid")
    return value


def _boolean(value: Any, field: str) -> bool:
    if type(value) is not bool:
        raise RouteControllerError(f"{field} is invalid")
    return value


def _money(value: Any, field: str) -> Decimal:
    if not isinstance(value, str):
        raise RouteControllerError(f"{field} is invalid")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise RouteControllerError(f"{field} is invalid") from exc
    if not result.is_finite() or result < 0 or result.quantize(Decimal("0.01")) != result:
        raise RouteControllerError(f"{field} is invalid")
    return result


def _canonical_digest(value: Mapping[str, Any]) -> str:
    payload = json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _stable_plan_digest(raw: Mapping[str, Any]) -> str:
    """Bind the exact plan without introducing record-hash self-reference."""

    authorization = dict(raw["authorization"])
    for field in (
        "reservation_record_sha256",
        "reservation_record_byte_size",
        "preflight_record_sha256",
        "preflight_record_byte_size",
    ):
        authorization.pop(field)
    stable = dict(raw)
    stable["authorization"] = authorization
    return _canonical_digest(stable)


def _worker_plan_digest(workers: Sequence[WorkerPlan]) -> str:
    value = {
        "manifest_digest": EXPECTED_MANIFEST_DIGEST,
        "model_revision": EXPECTED_MODEL_REVISION,
        "block_prefix": EXPECTED_BLOCK_PREFIX,
        "workers": [
            {
                "worker_id": worker.worker_id,
                "machine_id": worker.machine_id,
                "span": worker.span,
                "artifact_bytes": worker.artifact_bytes,
                "artifact_set_digest": worker.artifact_set_digest,
                "cache_root": worker.cache_root,
            }
            for worker in sorted(workers, key=lambda item: item.span)
        ],
    }
    return _canonical_digest(value)


def _action_id(plan: RoutePlan, action: str) -> str:
    if action not in ACTIONS - {"none"}:
        raise RouteControllerError("action identity is invalid")
    return _canonical_digest(
        {
            "run_id": plan.run_id,
            "plan_digest": plan.plan_digest,
            "action": action,
        }
    )


def _relative_path(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\\" in value:
        raise RouteControllerError("source binding path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RouteControllerError("source binding path is invalid")
    return value


def _cache_root(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512 or not value.startswith("/") or "\\" in value or "//" in value:
        raise RouteControllerError("worker cache root is invalid")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise RouteControllerError("worker cache root is invalid")
    return str(path)


def _validate_source_bindings(value: Any, source_root: Path) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise RouteControllerError("source bindings must be a bounded list")
    bindings: list[Mapping[str, Any]] = []
    previous = ""
    resolved_root = source_root.resolve()
    for index, item in enumerate(value):
        binding = _mapping(item, _BINDING_FIELDS, f"source_bindings[{index}]")
        relative = _relative_path(binding["relative_path"])
        if relative <= previous:
            raise RouteControllerError("source bindings must be strictly sorted")
        previous = relative
        expected_size = _integer(binding["byte_size"], "source binding byte size", 1)
        expected_digest = _string(binding["sha256"], _DIGEST_RE, "source binding digest")
        candidate = (resolved_root / Path(*PurePosixPath(relative).parts)).resolve()
        try:
            candidate.relative_to(resolved_root)
        except ValueError as exc:
            raise RouteControllerError("source binding escapes source root") from exc
        payload = _regular_bytes(candidate)
        if len(payload) != expected_size:
            raise RouteControllerError("source binding size changed")
        observed_digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        if observed_digest != expected_digest:
            raise RouteControllerError("source binding digest changed")
        bindings.append(dict(binding))
    return tuple(bindings)


def load_plan(path: Path, source_root: Path) -> RoutePlan:
    raw_bytes = _regular_bytes(path)
    raw = _mapping(_strict_json(raw_bytes), _PLAN_FIELDS, "plan")
    if raw["schema_version"] != SCHEMA_VERSION or raw["gate"] != GATE:
        raise RouteControllerError("plan identity is invalid")
    run_id = _string(raw["run_id"], _RUN_RE, "run_id")
    route_job_id = _string(raw["route_job_id"], _LABEL_RE, "route_job_id")
    source_commit = _string(raw["source_commit"], _COMMIT_RE, "source_commit")
    manifest_digest = _string(raw["manifest_digest"], _DIGEST_RE, "manifest_digest")
    model_revision = _string(raw["model_revision"], _REVISION_RE, "model_revision")
    if manifest_digest != EXPECTED_MANIFEST_DIGEST or model_revision != EXPECTED_MODEL_REVISION:
        raise RouteControllerError("Qwen3.8 model binding is invalid")
    deadline_unix = _integer(raw["deadline_unix"], "deadline_unix", 1)

    authorization = dict(_mapping(raw["authorization"], _AUTH_FIELDS, "authorization"))
    ceiling = _money(authorization["combined_cloud_ceiling_usd"], "combined cloud ceiling")
    committed = _money(authorization["ledger_committed_before_run_usd"], "ledger committed amount")
    maximum = _money(authorization["maximum_estimate_usd"], "maximum estimate")
    if (
        ceiling != Decimal("100.00")
        or committed != Decimal("56.00")
        or maximum > Decimal("44.00")
        or committed + maximum > ceiling
    ):
        raise RouteControllerError("authorization exceeds the current combined cloud ledger")
    for field in (
        "reservation_recorded",
        "native_auth_revalidated",
        "inventory_revalidated",
        "pricing_revalidated",
        "provisioning_authorized",
    ):
        _boolean(authorization[field], f"authorization.{field}")
    _string(authorization["reservation_id"], _LABEL_RE, "authorization.reservation_id")
    _relative_path(authorization["reservation_record_path"])
    _integer(
        authorization["reservation_record_byte_size"],
        "authorization.reservation_record_byte_size",
        1,
    )
    _string(
        authorization["reservation_record_sha256"],
        _DIGEST_RE,
        "authorization.reservation_record_sha256",
    )
    _relative_path(authorization["preflight_record_path"])
    _integer(
        authorization["preflight_record_byte_size"],
        "authorization.preflight_record_byte_size",
        1,
    )
    _string(
        authorization["preflight_record_sha256"],
        _DIGEST_RE,
        "authorization.preflight_record_sha256",
    )
    _string(
        authorization["readiness_ledger_sha256"],
        _DIGEST_RE,
        "authorization.readiness_ledger_sha256",
    )
    if authorization["provisioning_authorized"] and maximum == 0:
        raise RouteControllerError("authorized provisioning requires a positive bounded estimate")

    source_bindings = _validate_source_bindings(raw["source_bindings"], source_root)
    if {binding["relative_path"] for binding in source_bindings} != REQUIRED_SOURCE_PATHS:
        raise RouteControllerError("exact route execution sources are not bound")
    ledger_binding = next(binding for binding in source_bindings if binding["relative_path"] == READINESS_LEDGER_PATH)
    if ledger_binding["sha256"] != authorization["readiness_ledger_sha256"]:
        raise RouteControllerError("authorization is not bound to the readiness ledger")

    raw_workers = raw["workers"]
    if not isinstance(raw_workers, list) or len(raw_workers) != 4:
        raise RouteControllerError("plan must contain exactly four workers")
    workers: list[WorkerPlan] = []
    for index, item in enumerate(raw_workers):
        worker = _mapping(item, _WORKER_FIELDS, f"workers[{index}]")
        span = worker["span"]
        if span not in EXPECTED_SPANS:
            raise RouteControllerError("worker span is not canonical")
        expected_bytes, expected_digest = EXPECTED_SPANS[span]
        artifact_bytes = _integer(worker["artifact_bytes"], "artifact_bytes", 1)
        artifact_digest = _string(worker["artifact_set_digest"], _DIGEST_RE, "artifact_set_digest")
        if artifact_bytes != expected_bytes or artifact_digest != expected_digest:
            raise RouteControllerError("worker artifact plan changed")
        workers.append(
            WorkerPlan(
                worker_id=_string(worker["worker_id"], _LABEL_RE, "worker_id"),
                machine_id=_string(worker["machine_id"], _LABEL_RE, "machine_id"),
                instance=_string(worker["instance"], _LABEL_RE, "instance"),
                disk=_string(worker["disk"], _LABEL_RE, "disk"),
                span=span,
                artifact_bytes=artifact_bytes,
                artifact_set_digest=artifact_digest,
                cache_root=_cache_root(worker["cache_root"]),
            )
        )
    if [worker.span for worker in workers] != list(EXPECTED_SPANS):
        raise RouteControllerError("worker spans must be the canonical exact route")
    for field, values in (
        ("worker_id", [worker.worker_id for worker in workers]),
        ("machine_id", [worker.machine_id for worker in workers]),
        ("instance", [worker.instance for worker in workers]),
        ("disk", [worker.disk for worker in workers]),
        ("cache_root", [worker.cache_root for worker in workers]),
    ):
        if len(set(values)) != len(values):
            raise RouteControllerError(f"workers must have unique {field}")
    if PROTECTED_INSTANCE in {value for worker in workers for value in (worker.instance, worker.disk)}:
        raise RouteControllerError("protected bootstrap is targeted")

    raw_resources = raw["resources"]
    if not isinstance(raw_resources, list) or len(raw_resources) != 11:
        raise RouteControllerError("resource inventory must contain exactly 11 resources")
    resources: list[ResourcePlan] = []
    kind_counts = {kind: 0 for kind in EXPECTED_RESOURCE_KINDS}
    worker_by_id = {worker.worker_id: worker for worker in workers}
    for index, item in enumerate(raw_resources):
        resource = _mapping(item, _RESOURCE_FIELDS, f"resources[{index}]")
        kind = resource["kind"]
        if kind not in EXPECTED_RESOURCE_KINDS:
            raise RouteControllerError("resource kind is invalid")
        worker_id = resource["worker_id"]
        if kind.startswith("worker_"):
            worker_id = _string(worker_id, _LABEL_RE, "resource worker_id")
            if worker_id not in worker_by_id:
                raise RouteControllerError("resource references an unknown worker")
        elif worker_id is not None:
            raise RouteControllerError("non-worker resource has a worker_id")
        name = _string(resource["name"], _LABEL_RE, "resource name")
        if name == PROTECTED_INSTANCE:
            raise RouteControllerError("protected bootstrap is targeted")
        resources.append(
            ResourcePlan(
                name=name,
                kind=kind,
                provider=_string(resource["provider"], _LABEL_RE, "resource provider"),
                region=_string(resource["region"], _LABEL_RE, "resource region"),
                worker_id=worker_id,
            )
        )
        kind_counts[kind] += 1
    if kind_counts != EXPECTED_RESOURCE_KINDS:
        raise RouteControllerError("resource kind inventory is invalid")
    if len({resource.name for resource in resources}) != len(resources):
        raise RouteControllerError("resource names must be unique")
    if [resource.name for resource in resources] != sorted(resource.name for resource in resources):
        raise RouteControllerError("resource inventory must be sorted by name")
    if {resource.provider for resource in resources} != {EXPECTED_PROVIDER} or {
        resource.region for resource in resources
    } != {EXPECTED_REGION}:
        raise RouteControllerError("route resources must use one exact provider and region")
    for worker in workers:
        expected = {
            ("worker_instance", worker.instance),
            ("worker_disk", worker.disk),
        }
        observed = {(resource.kind, resource.name) for resource in resources if resource.worker_id == worker.worker_id}
        if observed != expected:
            raise RouteControllerError("worker resource inventory is inconsistent")

    return RoutePlan(
        run_id=run_id,
        route_job_id=route_job_id,
        source_commit=source_commit,
        manifest_digest=manifest_digest,
        model_revision=model_revision,
        deadline_unix=deadline_unix,
        authorization=authorization,
        source_bindings=source_bindings,
        resources=tuple(resources),
        workers=tuple(workers),
        plan_digest=_stable_plan_digest(raw),
    )


def _bound_record_bytes(
    root: Path,
    relative_path: str,
    expected_size: int,
    expected_digest: str,
) -> bytes:
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise RouteControllerError("authorization record escapes its root") from exc
    payload = _regular_bytes(candidate)
    if len(payload) != expected_size:
        raise RouteControllerError("authorization record size changed")
    if "sha256:" + hashlib.sha256(payload).hexdigest() != expected_digest:
        raise RouteControllerError("authorization record digest changed")
    return payload


def _assert_protected_path(
    path: Path,
    plan: RoutePlan,
    source_root: Path,
    *,
    directory: bool,
) -> None:
    """Require the controller-owned protection implementation and its native checks."""

    try:
        from scripts import gate14_packaged_lifecycle as lifecycle
    except ImportError as exc:
        raise RouteControllerError("controller protection verifier is unavailable") from exc
    expected_module = (source_root.resolve() / Path(*PurePosixPath(PROTECTION_SOURCE_PATH).parts)).resolve()
    imported_module = Path(lifecycle.__file__).resolve()
    protection_binding = next(
        binding for binding in plan.source_bindings if binding["relative_path"] == PROTECTION_SOURCE_PATH
    )
    expected_payload = _regular_bytes(expected_module)
    if (
        imported_module != expected_module
        or "sha256:" + hashlib.sha256(expected_payload).hexdigest() != protection_binding["sha256"]
    ):
        raise RouteControllerError("controller protection verifier is not source-bound")
    try:
        lifecycle._assert_controller_owned(path, directory=directory)
    except (OSError, lifecycle.Gate14LifecycleError) as exc:
        raise RouteControllerError("controller-managed input is not protected") from exc


def _protected_record_bytes(
    plan: RoutePlan,
    source_root: Path,
    root: Path,
    relative_path: str,
    expected_size: int,
    expected_digest: str,
) -> bytes:
    _assert_protected_path(root, plan, source_root, directory=True)
    resolved_root = root.resolve()
    candidate = (resolved_root / Path(*PurePosixPath(relative_path).parts)).resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as exc:
        raise RouteControllerError("protected record escapes its root") from exc
    _assert_protected_path(candidate, plan, source_root, directory=False)
    return _bound_record_bytes(
        root,
        relative_path,
        expected_size,
        expected_digest,
    )


def _ledger_reservation_marker(plan: RoutePlan) -> bytes:
    authorization = plan.authorization
    return (
        "Q38_ROUTE_RESERVATION "
        f"run_id={plan.run_id} "
        f"reservation_id={authorization['reservation_id']} "
        f"maximum_usd={authorization['maximum_estimate_usd']} "
        f"deadline_unix={plan.deadline_unix}"
    ).encode("ascii")


def revalidate_authorization_evidence(
    plan: RoutePlan,
    authorization_root: Path,
    source_root: Path | None = None,
    *,
    now_unix: int,
) -> None:
    """Open and validate exact ledger, pricing, quota, and provider records."""

    now_unix = _integer(now_unix, "trusted current time", 1)
    if source_root is None:
        source_root = authorization_root.parent / "source"
    authorization = plan.authorization
    readiness_path = source_root.resolve() / Path(*PurePosixPath(READINESS_LEDGER_PATH).parts)
    readiness_payload = _regular_bytes(readiness_path)
    readiness_binding = next(
        binding for binding in plan.source_bindings if binding["relative_path"] == READINESS_LEDGER_PATH
    )
    if (
        len(readiness_payload) != readiness_binding["byte_size"]
        or "sha256:" + hashlib.sha256(readiness_payload).hexdigest() != readiness_binding["sha256"]
    ):
        raise RouteControllerError("readiness ledger source binding changed")
    if _ledger_reservation_marker(plan) not in readiness_payload.splitlines():
        raise RouteControllerError("readiness ledger does not contain the exact reservation")
    reservation_payload = _protected_record_bytes(
        plan,
        source_root,
        authorization_root,
        authorization["reservation_record_path"],
        authorization["reservation_record_byte_size"],
        authorization["reservation_record_sha256"],
    )
    reservation = _mapping(
        _strict_json(reservation_payload),
        _RESERVATION_FIELDS,
        "reservation record",
    )
    recorded_at = _integer(
        reservation["recorded_at_unix"],
        "reservation recorded_at_unix",
        1,
    )
    expires_at = _integer(
        reservation["expires_at_unix"],
        "reservation expires_at_unix",
        1,
    )
    resource_costs = reservation["resource_costs"]
    if not isinstance(resource_costs, list) or len(resource_costs) != len(plan.resources):
        raise RouteControllerError("reservation cost inventory is invalid")
    expected_resource_names = [resource.name for resource in plan.resources]
    observed_cost_names: list[str] = []
    observed_cost_spec_digests: list[str] = []
    total_cost = Decimal("0.00")
    for index, value in enumerate(resource_costs):
        item = _mapping(value, _COST_FIELDS, f"resource_costs[{index}]")
        observed_cost_names.append(_string(item["resource_name"], _LABEL_RE, "cost resource name"))
        observed_cost_spec_digests.append(
            _string(item["resource_spec_digest"], _DIGEST_RE, "cost resource spec digest")
        )
        unit_rate = _money(item["unit_rate_usd"], "resource unit rate")
        quantity = _money(item["quantity"], "resource quantity")
        duration = _money(item["duration_hours"], "resource duration")
        maximum = _money(item["maximum_usd"], "resource maximum cost")
        recomputed = (unit_rate * quantity * duration).quantize(
            Decimal("0.01"),
            rounding=ROUND_CEILING,
        )
        if maximum != recomputed:
            raise RouteControllerError("reservation resource cost was not recomputed")
        resource = plan.resources[index]
        if (
            quantity != Decimal("1.00")
            or duration != EXPECTED_PRICED_DURATION_HOURS
            or (resource.kind == "firewall" and maximum != Decimal("0.00"))
            or (resource.kind != "firewall" and maximum <= Decimal("0.00"))
        ):
            raise RouteControllerError("reservation pricing horizon or quantity is invalid")
        total_cost += maximum
    if observed_cost_names != expected_resource_names:
        raise RouteControllerError("reservation cost inventory is not exact")
    if total_cost != _money(
        authorization["maximum_estimate_usd"],
        "maximum estimate",
    ):
        raise RouteControllerError("reservation cost total changed")
    if (
        reservation["schema_version"] != SCHEMA_VERSION
        or reservation["reservation_id"] != authorization["reservation_id"]
        or reservation["run_id"] != plan.run_id
        or reservation["combined_cloud_ceiling_usd"] != authorization["combined_cloud_ceiling_usd"]
        or reservation["ledger_committed_before_run_usd"] != authorization["ledger_committed_before_run_usd"]
        or reservation["maximum_estimate_usd"] != authorization["maximum_estimate_usd"]
        or reservation["deadline_unix"] != plan.deadline_unix
        or reservation["plan_digest"] != plan.plan_digest
        or reservation["execution_inventory_digest"] != plan.execution_inventory_digest
        or reservation["worker_plan_digest"] != plan.worker_plan_digest
        or reservation["readiness_ledger_sha256"] != authorization["readiness_ledger_sha256"]
        or reservation["reservation_recorded"] is not True
        or recorded_at > now_unix
        or expires_at < plan.deadline_unix
        or now_unix >= expires_at
    ):
        raise RouteControllerError("reservation record is invalid or expired")

    preflight_payload = _protected_record_bytes(
        plan,
        source_root,
        authorization_root,
        authorization["preflight_record_path"],
        authorization["preflight_record_byte_size"],
        authorization["preflight_record_sha256"],
    )
    preflight = _mapping(
        _strict_json(preflight_payload),
        _PREFLIGHT_FIELDS,
        "preflight record",
    )
    checked_at = _integer(
        preflight["checked_at_unix"],
        "preflight checked_at_unix",
        1,
    )
    pricing_checked_at = _integer(
        preflight["pricing_checked_at_unix"],
        "preflight pricing_checked_at_unix",
        1,
    )
    _string(preflight["pricing_source"], _LABEL_RE, "preflight pricing_source")
    if preflight["pricing_currency"] != "USD":
        raise RouteControllerError("provider pricing currency is invalid")
    resource_specs = preflight["resource_specs"]
    if not isinstance(resource_specs, list) or len(resource_specs) != len(plan.resources):
        raise RouteControllerError("provider resource specification inventory is invalid")
    observed_spec_names: list[str] = []
    observed_spec_digests: list[str] = []
    for index, value in enumerate(resource_specs):
        spec = _mapping(value, _RESOURCE_SPEC_FIELDS, f"resource_specs[{index}]")
        name = _string(spec["resource_name"], _LABEL_RE, "spec resource name")
        observed_spec_names.append(name)
        resource = plan.resource_by_name.get(name)
        if (
            resource is None
            or spec["kind"] != resource.kind
            or spec["provider"] != resource.provider
            or spec["region"] != resource.region
        ):
            raise RouteControllerError("provider resource specification binding is invalid")
        if dict(spec) != _expected_resource_spec(resource):
            raise RouteControllerError("provider resource specification is not the exact launch profile")
        if checked_at + EXPECTED_MAX_LIFETIME_SECONDS > plan.deadline_unix:
            raise RouteControllerError("provider resource lifetime exceeds the route deadline")
        observed_spec_digests.append(_canonical_digest(spec))
    if observed_spec_names != expected_resource_names:
        raise RouteControllerError("provider resource specification inventory is not exact")
    if observed_cost_spec_digests != observed_spec_digests:
        raise RouteControllerError("reservation costs are not bound to the resource specifications")
    quota_limit = _integer(
        preflight["gpu_quota_limit"],
        "preflight gpu_quota_limit",
    )
    quota_usage = _integer(
        preflight["gpu_quota_usage"],
        "preflight gpu_quota_usage",
    )
    required_gpu_count = _integer(
        preflight["required_gpu_count"],
        "preflight required_gpu_count",
        1,
    )
    providers = {resource.provider for resource in plan.resources}
    if (
        preflight["schema_version"] != SCHEMA_VERSION
        or preflight["run_id"] != plan.run_id
        or preflight["source_commit"] != plan.source_commit
        or preflight["plan_digest"] != plan.plan_digest
        or preflight["execution_inventory_digest"] != plan.execution_inventory_digest
        or preflight["worker_plan_digest"] != plan.worker_plan_digest
        or preflight["provider"] != next(iter(providers))
        or preflight["resource_names"] != expected_resource_names
        or required_gpu_count != len(plan.workers)
        or quota_usage > quota_limit
        or quota_limit - quota_usage < required_gpu_count
        or preflight["reservation_record_sha256"] != authorization["reservation_record_sha256"]
        or checked_at > now_unix
        or pricing_checked_at > now_unix
        or now_unix - checked_at > MAX_PLAN_REVALIDATION_AGE_SECONDS
        or now_unix - pricing_checked_at > MAX_PLAN_REVALIDATION_AGE_SECONDS
        or preflight["protected_bootstrap_running"] is not True
        or any(
            preflight[field] is not True or authorization[field] is not True
            for field in (
                "native_auth_revalidated",
                "inventory_revalidated",
                "pricing_revalidated",
                "provisioning_authorized",
            )
        )
    ):
        raise RouteControllerError("provider preflight record is invalid or stale")


def revalidate_production_artifact_plan(
    plan: RoutePlan,
    manifest_path: Path,
    artifact_root: Path,
    source_root: Path,
    *,
    verified_at_unix: int,
) -> dict[str, Any]:
    """Rederive the exact four span plans through the source-bound production verifier."""

    try:
        from drift import model_manifest as manifest_module
    except ImportError as exc:
        raise RouteControllerError("production artifact planner is unavailable") from exc
    expected_module = (source_root.resolve() / Path(*PurePosixPath(VERIFIER_SOURCE_PATH).parts)).resolve()
    imported_module = Path(manifest_module.__file__).resolve()
    verifier_binding = next(
        binding for binding in plan.source_bindings if binding["relative_path"] == VERIFIER_SOURCE_PATH
    )
    expected_module_payload = _regular_bytes(expected_module)
    if (
        imported_module != expected_module
        or "sha256:" + hashlib.sha256(expected_module_payload).hexdigest() != verifier_binding["sha256"]
    ):
        raise RouteControllerError("imported artifact planner is not the source-bound verifier")

    ManifestArtifactVerifier = manifest_module.ManifestArtifactVerifier
    ManifestError = manifest_module.ManifestError
    ModelManifest = manifest_module.ModelManifest
    try:
        manifest = ModelManifest.load(manifest_path)
        if (
            manifest.digest_id != plan.manifest_digest
            or manifest.source.revision != plan.model_revision
            or manifest.model.num_blocks != 64
        ):
            raise RouteControllerError("production manifest identity is invalid")
        indices = manifest.artifacts_for_roles({"weight_index"})
        if len(indices) != 1 or "sha256:" + indices[0].sha256 != EXPECTED_INDEX_DIGEST:
            raise RouteControllerError("production checkpoint index identity is invalid")
        metadata_paths = [artifact.path for artifact in manifest.artifacts_for_roles({"config", "weight_index"})]
        verifier = ManifestArtifactVerifier(
            manifest,
            repository=manifest.source.repository,
            revision=manifest.source.revision,
            token=False,
            artifact_root=artifact_root,
            allowed_paths=metadata_paths,
        )
        for worker in plan.workers:
            start, end = (int(value) for value in worker.span.split(":", 1))
            derived = verifier.plan_block_artifacts(
                block_prefix=EXPECTED_BLOCK_PREFIX,
                start_block=start,
                end_block=end,
            )
            if (
                derived.artifact_bytes != worker.artifact_bytes
                or "sha256:" + derived.artifact_set_digest != worker.artifact_set_digest
                or len(derived.artifacts) != EXPECTED_ARTIFACTS_PER_SPAN
            ):
                raise RouteControllerError("production artifact plan differs from the route plan")
    except RouteControllerError:
        raise
    except (ManifestError, OSError, UnicodeError, ValueError) as exc:
        raise RouteControllerError("production artifact plan could not be revalidated") from exc

    return {
        "verified_at_unix": _integer(verified_at_unix, "verified_at_unix", 1),
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "model_revision": plan.model_revision,
        "index_digest": EXPECTED_INDEX_DIGEST,
        "block_prefix": EXPECTED_BLOCK_PREFIX,
        "worker_plan_digest": plan.worker_plan_digest,
        "verifier_source_sha256": verifier_binding["sha256"],
    }


def initial_state(plan: RoutePlan) -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "revision": 0,
        "phase": "ABSENT",
        "failure_code": None,
        "evidence_digest": None,
        "cleanup_verified": False,
        "next_action": "none",
    }


def validate_state(value: Mapping[str, Any], plan: RoutePlan) -> dict[str, Any]:
    state = dict(_mapping(value, _STATE_FIELDS, "state"))
    if (
        state["schema_version"] != STATE_SCHEMA_VERSION
        or state["run_id"] != plan.run_id
        or state["plan_digest"] != plan.plan_digest
        or state["phase"] not in PHASES
        or state["next_action"] not in ACTIONS
    ):
        raise RouteControllerError("state binding is invalid")
    _integer(state["revision"], "state revision")
    if state["failure_code"] is not None:
        _string(state["failure_code"], _LABEL_RE, "failure_code")
    if state["evidence_digest"] is not None:
        _string(state["evidence_digest"], _DIGEST_RE, "evidence_digest")
    _boolean(state["cleanup_verified"], "cleanup_verified")
    if state["cleanup_verified"] is not (state["phase"] in TERMINAL_PHASES):
        raise RouteControllerError("cleanup state is inconsistent")
    if state["phase"] == "CLEANED_PASS" and state["evidence_digest"] is None:
        raise RouteControllerError("passing terminal state lacks evidence")
    if state["phase"] == "CLEANED_FAILURE" and state["failure_code"] is None:
        raise RouteControllerError("failed terminal state lacks a failure code")
    if state["evidence_digest"] is not None and state["phase"] not in {
        "CLEANING",
        "CLEANED_PASS",
        "CLEANED_FAILURE",
    }:
        raise RouteControllerError("evidence state is inconsistent")
    if state["phase"] not in {"CLEANING", "CLEANED_FAILURE"} and state["failure_code"] is not None:
        raise RouteControllerError("failure code is inconsistent")
    allowed_actions = {
        "ABSENT": {"none"},
        "STARTING": {"none", "start_route"},
        "READY": {"none"},
        "COLLECTING": {"none", "collect_route"},
        "CLEANING": {"cleanup_route"},
        "CLEANED_PASS": {"none"},
        "CLEANED_FAILURE": {"none"},
    }
    if state["next_action"] not in allowed_actions[state["phase"]]:
        raise RouteControllerError("state action is inconsistent")
    return state


def _validate_route_record(
    value: Any,
    plan: RoutePlan,
    workers: Mapping[str, Any],
) -> Mapping[str, Any]:
    record = _mapping(value, _ROUTE_RECORD_FIELDS, "route record")
    if (
        record["schema_version"] != SCHEMA_VERSION
        or record["result"] != "passed"
        or record["run_id"] != plan.run_id
        or record["job_id"] != plan.route_job_id
        or record["collect_action_id"] != _action_id(plan, "collect_route")
        or record["plan_digest"] != plan.plan_digest
        or record["source_commit"] != plan.source_commit
        or record["manifest_digest"] != plan.manifest_digest
        or record["worker_plan_digest"] != plan.worker_plan_digest
        or record["route_span"] != "0:64"
        or record["cleanup_ready"] is not True
    ):
        raise RouteControllerError("route record binding is invalid")
    _string(record["session_id"], _LABEL_RE, "route record session_id")
    _string(
        record["route_rpc_evidence_digest"],
        _DIGEST_RE,
        "route record RPC evidence digest",
    )
    results = record["worker_results"]
    if not isinstance(results, list) or len(results) != len(plan.workers):
        raise RouteControllerError("route record worker inventory is invalid")
    for index, (result_value, worker_plan) in enumerate(zip(results, plan.workers)):
        result = _mapping(
            result_value,
            _ROUTE_WORKER_RESULT_FIELDS,
            f"route record worker_results[{index}]",
        )
        observed_worker = workers[worker_plan.worker_id]
        if (
            result["worker_id"] != worker_plan.worker_id
            or result["machine_id"] != worker_plan.machine_id
            or result["peer_id"] != observed_worker["peer_id"]
            or result["span"] != worker_plan.span
            or result["source_commit"] != plan.source_commit
            or result["manifest_digest"] != plan.manifest_digest
            or result["artifact_bytes"] != worker_plan.artifact_bytes
            or result["artifact_set_digest"] != worker_plan.artifact_set_digest
            or result["cache_root"] != worker_plan.cache_root
        ):
            raise RouteControllerError("route record worker binding is invalid")
        _string(
            result["worker_evidence_digest"],
            _DIGEST_RE,
            "route record worker evidence digest",
        )
    return record


def _protected_named_bytes(
    plan: RoutePlan,
    source_root: Path,
    root: Path,
    name: str,
) -> bytes:
    if not _LABEL_RE.fullmatch(name):
        raise RouteControllerError("protected evidence filename is invalid")
    _assert_protected_path(root, plan, source_root, directory=True)
    candidate = root.resolve() / name
    _assert_protected_path(candidate, plan, source_root, directory=False)
    return _regular_bytes(candidate)


def revalidate_route_evidence(
    plan: RoutePlan,
    observation: Mapping[str, Any],
    evidence_root: Path,
    source_root: Path,
) -> str:
    """Validate protected host-job, RPC, and per-worker evidence records."""

    workers = observation["workers"]
    route_job = observation["route_job"]
    if route_job["state"] != "passed":
        raise RouteControllerError("route evidence is only valid for a passed job")
    record = _validate_route_record(route_job["route_record"], plan, workers)
    expected_names = {
        "route-terminal.json",
        "route-rpc.json",
        *(f"{worker.worker_id}-evidence.json" for worker in plan.workers),
    }
    _assert_protected_path(evidence_root, plan, source_root, directory=True)
    try:
        observed_names = {entry.name for entry in evidence_root.iterdir()}
    except OSError as exc:
        raise RouteControllerError("protected route evidence inventory is unavailable") from exc
    if observed_names != expected_names:
        raise RouteControllerError("protected route evidence inventory is not exact")

    terminal_payload = _protected_named_bytes(
        plan,
        source_root,
        evidence_root,
        "route-terminal.json",
    )
    terminal_record = _mapping(
        _strict_json(terminal_payload),
        _ROUTE_RECORD_FIELDS,
        "protected route terminal record",
    )
    if dict(terminal_record) != dict(record):
        raise RouteControllerError("protected route terminal record does not match the observation")

    rpc_payload = _protected_named_bytes(
        plan,
        source_root,
        evidence_root,
        "route-rpc.json",
    )
    if "sha256:" + hashlib.sha256(rpc_payload).hexdigest() != record["route_rpc_evidence_digest"]:
        raise RouteControllerError("protected route RPC evidence digest changed")
    rpc = _mapping(
        _strict_json(rpc_payload),
        _RPC_EVIDENCE_FIELDS,
        "protected route RPC evidence",
    )
    if (
        rpc["schema_version"] != SCHEMA_VERSION
        or rpc["result"] != "passed"
        or rpc["run_id"] != plan.run_id
        or rpc["job_id"] != plan.route_job_id
        or rpc["collect_action_id"] != _action_id(plan, "collect_route")
        or rpc["plan_digest"] != plan.plan_digest
        or rpc["source_commit"] != plan.source_commit
        or rpc["manifest_digest"] != plan.manifest_digest
        or rpc["worker_plan_digest"] != plan.worker_plan_digest
        or rpc["route_span"] != "0:64"
        or rpc["session_id"] != record["session_id"]
    ):
        raise RouteControllerError("protected route RPC evidence binding is invalid")

    for worker_plan, result in zip(plan.workers, record["worker_results"]):
        payload = _protected_named_bytes(
            plan,
            source_root,
            evidence_root,
            f"{worker_plan.worker_id}-evidence.json",
        )
        if "sha256:" + hashlib.sha256(payload).hexdigest() != result["worker_evidence_digest"]:
            raise RouteControllerError("protected worker evidence digest changed")
        evidence = _mapping(
            _strict_json(payload),
            _WORKER_EVIDENCE_FIELDS,
            "protected worker evidence",
        )
        if (
            evidence["schema_version"] != SCHEMA_VERSION
            or evidence["result"] != "passed"
            or evidence["run_id"] != plan.run_id
            or evidence["job_id"] != plan.route_job_id
            or evidence["collect_action_id"] != _action_id(plan, "collect_route")
            or evidence["plan_digest"] != plan.plan_digest
            or evidence["source_commit"] != plan.source_commit
            or evidence["manifest_digest"] != plan.manifest_digest
            or evidence["worker_plan_digest"] != plan.worker_plan_digest
            or evidence["start_action_id"] != _action_id(plan, "start_route")
            or evidence["worker_id"] != worker_plan.worker_id
            or evidence["machine_id"] != worker_plan.machine_id
            or evidence["peer_id"] != result["peer_id"]
            or evidence["span"] != worker_plan.span
            or evidence["artifact_bytes"] != worker_plan.artifact_bytes
            or evidence["artifact_set_digest"] != worker_plan.artifact_set_digest
            or evidence["cache_root"] != worker_plan.cache_root
        ):
            raise RouteControllerError("protected worker evidence binding is invalid")
    return route_job["evidence_digest"]


def validate_observation(
    value: Mapping[str, Any],
    plan: RoutePlan,
    *,
    cleanup_only: bool = False,
) -> dict[str, Any]:
    observation = dict(_mapping(value, _OBSERVATION_FIELDS, "observation"))
    if observation["schema_version"] != SCHEMA_VERSION or observation["run_id"] != plan.run_id:
        raise RouteControllerError("observation identity is invalid")
    _boolean(
        observation["protected_bootstrap_running"],
        "protected_bootstrap_running",
    )
    observed_at = _integer(observation["observed_at_unix"], "observed_at_unix", 1)

    if cleanup_only:
        revalidation = observation["artifact_plan_revalidation"]
    else:
        revalidation = _mapping(
            observation["artifact_plan_revalidation"],
            _REVALIDATION_FIELDS,
            "artifact_plan_revalidation",
        )
        verified_at = _integer(
            revalidation["verified_at_unix"],
            "artifact plan verified_at_unix",
            1,
        )
        verifier_binding = next(
            (binding for binding in plan.source_bindings if binding["relative_path"] == VERIFIER_SOURCE_PATH),
            None,
        )
        if (
            verified_at > observed_at
            or revalidation["source_commit"] != plan.source_commit
            or revalidation["manifest_digest"] != plan.manifest_digest
            or revalidation["model_revision"] != plan.model_revision
            or revalidation["index_digest"] != EXPECTED_INDEX_DIGEST
            or revalidation["block_prefix"] != EXPECTED_BLOCK_PREFIX
            or revalidation["worker_plan_digest"] != plan.worker_plan_digest
            or verifier_binding is None
            or revalidation["verifier_source_sha256"] != verifier_binding["sha256"]
        ):
            raise RouteControllerError("production artifact plan revalidation is invalid")

    resources = observation["resources"]
    if not isinstance(resources, dict) or set(resources) != set(plan.resource_by_name):
        raise RouteControllerError("provider resource inventory is not exact")
    for name, resource_plan in plan.resource_by_name.items():
        resource = _mapping(resources[name], _OBS_RESOURCE_FIELDS, f"resources[{name!r}]")
        present = _boolean(resource["present"], "resource present")
        if (
            resource["kind"] != resource_plan.kind
            or resource["provider"] != resource_plan.provider
            or resource["region"] != resource_plan.region
        ):
            raise RouteControllerError("provider resource identity is invalid")
        if present:
            if (
                resource["run_id"] != plan.run_id
                or resource["source_commit"] != plan.source_commit
                or _integer(resource["deadline_unix"], "resource deadline", 1) != plan.deadline_unix
                or resource["plan_digest"] != plan.plan_digest
                or resource["start_action_id"] != _action_id(plan, "start_route")
                or resource["worker_id"] != resource_plan.worker_id
            ):
                raise RouteControllerError("provider resource binding is invalid")
        elif any(
            resource[field] is not None
            for field in (
                "run_id",
                "source_commit",
                "deadline_unix",
                "plan_digest",
                "start_action_id",
                "worker_id",
            )
        ):
            raise RouteControllerError("absent resource metadata is invalid")

    workers = observation["workers"]
    if not isinstance(workers, dict) or set(workers) != set(plan.worker_by_id):
        raise RouteControllerError("worker inventory is not exact")
    if cleanup_only:
        for worker in workers.values():
            if not isinstance(worker, dict) or worker.get("state") not in WORKER_STATES:
                raise RouteControllerError("cleanup worker inventory is invalid")
            if worker["state"] == "absent" and (
                set(worker) != _OBS_WORKER_FIELDS
                or any(value is not None for field, value in worker.items() if field != "state")
            ):
                raise RouteControllerError("absent worker metadata is invalid")
        route_job = observation["route_job"]
        if not isinstance(route_job, dict) or route_job.get("state") not in JOB_STATES:
            raise RouteControllerError("cleanup route job inventory is invalid")
        if route_job["state"] == "absent" and (
            set(route_job) != _ROUTE_JOB_FIELDS
            or any(value is not None for field, value in route_job.items() if field != "state")
        ):
            raise RouteControllerError("absent route job metadata is invalid")
        return observation
    ready_peer_ids: list[str] = []
    for worker_id, worker_plan in plan.worker_by_id.items():
        worker = _mapping(workers[worker_id], _OBS_WORKER_FIELDS, f"workers[{worker_id!r}]")
        state = worker["state"]
        if state not in WORKER_STATES:
            raise RouteControllerError("worker state is invalid")
        if state == "absent":
            if any(value is not None for field, value in worker.items() if field != "state"):
                raise RouteControllerError("absent worker metadata is invalid")
            continue
        if (
            worker["machine_id"] != worker_plan.machine_id
            or worker["source_commit"] != plan.source_commit
            or worker["plan_digest"] != plan.plan_digest
            or worker["worker_plan_digest"] != plan.worker_plan_digest
            or worker["start_action_id"] != _action_id(plan, "start_route")
            or worker["span"] != worker_plan.span
            or worker["manifest_digest"] != plan.manifest_digest
            or worker["artifact_bytes"] != worker_plan.artifact_bytes
            or worker["artifact_set_digest"] != worker_plan.artifact_set_digest
            or worker["cache_root"] != worker_plan.cache_root
        ):
            raise RouteControllerError("worker binding is invalid")
        peer_id = worker["peer_id"]
        if state == "ready":
            ready_peer_ids.append(_string(peer_id, _PEER_RE, "worker peer_id"))
        elif peer_id is not None:
            raise RouteControllerError("unfinished worker exposed a peer identity")
    if len(set(ready_peer_ids)) != len(ready_peer_ids):
        raise RouteControllerError("ready workers must have unique peer identities")

    route_job = _mapping(observation["route_job"], _ROUTE_JOB_FIELDS, "route_job")
    if route_job["state"] not in JOB_STATES:
        raise RouteControllerError("route job state is invalid")
    evidence_digest = route_job["evidence_digest"]
    job_bindings = (
        "job_id",
        "collect_action_id",
        "run_id",
        "plan_digest",
        "source_commit",
        "manifest_digest",
        "worker_plan_digest",
    )
    if route_job["state"] == "absent":
        if any(route_job[field] is not None for field in (*job_bindings, "evidence_digest", "route_record")):
            raise RouteControllerError("absent route job metadata is invalid")
    elif (
        route_job["job_id"] != plan.route_job_id
        or route_job["collect_action_id"] != _action_id(plan, "collect_route")
        or route_job["run_id"] != plan.run_id
        or route_job["plan_digest"] != plan.plan_digest
        or route_job["source_commit"] != plan.source_commit
        or route_job["manifest_digest"] != plan.manifest_digest
        or route_job["worker_plan_digest"] != plan.worker_plan_digest
    ):
        raise RouteControllerError("route job binding is invalid")
    if route_job["state"] == "passed":
        _string(evidence_digest, _DIGEST_RE, "route evidence digest")
        record = _validate_route_record(route_job["route_record"], plan, workers)
        if evidence_digest != _canonical_digest(record):
            raise RouteControllerError("route evidence digest does not bind the route record")
    elif evidence_digest is not None or route_job["route_record"] is not None:
        raise RouteControllerError("unfinished route job exposed evidence")
    return observation


def _all_absent(observation: Mapping[str, Any]) -> bool:
    return (
        all(not resource["present"] for resource in observation["resources"].values())
        and all(worker["state"] == "absent" for worker in observation["workers"].values())
        and observation["route_job"]["state"] == "absent"
    )


def _all_resources_present(observation: Mapping[str, Any]) -> bool:
    return all(resource["present"] for resource in observation["resources"].values())


def _all_workers_ready(observation: Mapping[str, Any]) -> bool:
    return all(worker["state"] == "ready" for worker in observation["workers"].values())


def _authorized(plan: RoutePlan) -> bool:
    return all(
        plan.authorization[field] is True
        for field in (
            "reservation_recorded",
            "native_auth_revalidated",
            "inventory_revalidated",
            "pricing_revalidated",
            "provisioning_authorized",
        )
    )


def _next(state: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
    result = dict(state)
    result.update(changes)
    if all(result[key] == state[key] for key in state):
        return result
    result["revision"] = int(state["revision"]) + 1
    return result


def _cleanup_state(state: Mapping[str, Any], failure_code: str | None = None) -> dict[str, Any]:
    changes: dict[str, Any] = {
        "phase": "CLEANING",
        "next_action": "cleanup_route",
    }
    if failure_code is not None:
        changes["failure_code"] = failure_code
    return _next(state, **changes)


def reconcile(
    operation: str,
    state_value: Mapping[str, Any],
    observation_value: Mapping[str, Any],
    plan: RoutePlan,
    *,
    now_unix: int | None = None,
    route_evidence_validated: bool = False,
    start_was_issued: bool = False,
) -> dict[str, Any]:
    if operation not in {"start", "status", "collect", "cleanup"}:
        raise RouteControllerError("operation is invalid")
    state = validate_state(state_value, plan)
    phase = state["phase"]
    raw_observed_at = observation_value.get("observed_at_unix")
    effective_now = (
        _integer(now_unix, "trusted current time", 1)
        if now_unix is not None
        else _integer(raw_observed_at, "observed_at_unix", 1)
    )
    cleanup_only = (
        operation == "cleanup"
        or phase == "CLEANING"
        or observation_value.get("protected_bootstrap_running") is False
        or effective_now >= plan.deadline_unix
    )
    observation = validate_observation(
        observation_value,
        plan,
        cleanup_only=cleanup_only,
    )
    all_absent = _all_absent(observation)
    resources_present = _all_resources_present(observation)
    workers_ready = _all_workers_ready(observation)
    worker_states = {worker["state"] for worker in observation["workers"].values()}
    job_state = observation["route_job"]["state"]
    if not cleanup_only and job_state == "passed" and not route_evidence_validated:
        raise RouteControllerError("passed route evidence was not revalidated from protected records")

    if phase in TERMINAL_PHASES:
        if not all_absent:
            raise RouteControllerError("resources returned after terminal cleanup")
        if observation["protected_bootstrap_running"] is not True:
            raise RouteControllerError("protected bootstrap was lost after terminal cleanup")
        return state

    if observation["protected_bootstrap_running"] is not True:
        if all_absent:
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code="protected-bootstrap-lost",
                cleanup_verified=True,
                next_action="none",
            )
        return _cleanup_state(state, "protected-bootstrap-lost")

    if effective_now >= plan.deadline_unix:
        if all_absent:
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code=state["failure_code"] or "run-expired",
                cleanup_verified=True,
                next_action="none",
            )
        return _cleanup_state(state, state["failure_code"] or "run-expired")

    if operation == "cleanup":
        if all_absent:
            if state["evidence_digest"] is not None and state["failure_code"] is None:
                return _next(
                    state,
                    phase="CLEANED_PASS",
                    cleanup_verified=True,
                    next_action="none",
                )
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code=state["failure_code"] or "operator-cleanup",
                cleanup_verified=True,
                next_action="none",
            )
        return _cleanup_state(state)

    if phase == "CLEANING":
        if all_absent:
            if state["evidence_digest"] is not None and state["failure_code"] is None:
                return _next(
                    state,
                    phase="CLEANED_PASS",
                    cleanup_verified=True,
                    next_action="none",
                )
            return _next(
                state,
                phase="CLEANED_FAILURE",
                failure_code=state["failure_code"] or "route-failed",
                cleanup_verified=True,
                next_action="none",
            )
        return _next(state, next_action="cleanup_route")

    plan_verified_at = observation["artifact_plan_revalidation"]["verified_at_unix"]
    if (
        observation["observed_at_unix"] > effective_now
        or effective_now - observation["observed_at_unix"] > MAX_PLAN_REVALIDATION_AGE_SECONDS
        or (
            (operation in {"start", "collect"} or job_state == "passed")
            and effective_now - plan_verified_at > MAX_PLAN_REVALIDATION_AGE_SECONDS
        )
    ):
        raise RouteControllerError("controller observation or artifact plan is stale")

    if phase == "ABSENT" and not all_absent and not start_was_issued:
        return _cleanup_state(state, "unrecorded-resources")

    if phase == "ABSENT" and operation == "start" and all_absent and start_was_issued:
        return _next(
            state,
            phase="CLEANED_FAILURE",
            failure_code="state-lost-after-start",
            cleanup_verified=True,
            next_action="none",
        )

    if phase == "ABSENT" and resources_present and workers_ready and job_state == "passed":
        return _next(
            state,
            phase="CLEANING",
            evidence_digest=observation["route_job"]["evidence_digest"],
            next_action="cleanup_route",
        )

    if operation == "start":
        if phase == "ABSENT":
            if not _authorized(plan):
                raise RouteControllerError("paid provisioning is not authorized")
            if all_absent:
                return _next(state, phase="STARTING", next_action="start_route")
            if resources_present and job_state == "failed":
                return _cleanup_state(state, "qualification-failed")
            if resources_present and workers_ready and job_state in {"absent", "running"}:
                return _next(state, phase="READY", next_action="none")
            if resources_present and worker_states <= {"starting", "ready"} and job_state == "absent":
                return _next(state, phase="STARTING", next_action="none")
            return _cleanup_state(state, "partial-reattach")

    if phase == "ABSENT":
        if all_absent:
            return _next(state, next_action="none")
        return _cleanup_state(state, "unexpected-resources")

    if phase == "STARTING" and all_absent:
        if state["next_action"] == "start_route":
            return state
        return _cleanup_state(state, "route-inventory-lost")

    if not resources_present or "failed" in worker_states or "absent" in worker_states:
        return _cleanup_state(state, "route-inventory-lost")

    if phase == "STARTING":
        if workers_ready:
            return _next(state, phase="READY", next_action="none")
        if worker_states <= {"starting", "ready"} and job_state == "absent":
            return _next(state, next_action="none")
        return _cleanup_state(state, "route-start-failed")

    if phase == "READY":
        if not workers_ready:
            return _cleanup_state(state, "route-readiness-lost")
        if job_state == "passed":
            return _next(
                state,
                phase="CLEANING",
                evidence_digest=observation["route_job"]["evidence_digest"],
                next_action="cleanup_route",
            )
        if job_state == "failed":
            return _cleanup_state(state, "qualification-failed")
        if operation == "collect" and job_state == "absent":
            return _next(state, phase="COLLECTING", next_action="collect_route")
        if operation == "collect" and job_state == "running":
            return _next(state, phase="COLLECTING", next_action="none")
        return _next(state, next_action="none")

    if phase == "COLLECTING":
        if not workers_ready:
            return _cleanup_state(state, "route-readiness-lost")
        if job_state == "passed":
            return _next(
                state,
                phase="CLEANING",
                evidence_digest=observation["route_job"]["evidence_digest"],
                next_action="cleanup_route",
            )
        if job_state == "failed":
            return _cleanup_state(state, "qualification-failed")
        if job_state == "absent" and state["next_action"] == "collect_route":
            return state
        return _next(state, next_action="none")

    raise RouteControllerError("state transition is invalid")


def action_record(state: Mapping[str, Any], plan: RoutePlan) -> dict[str, Any]:
    workers = [
        {
            "worker_id": worker.worker_id,
            "machine_id": worker.machine_id,
            "instance": worker.instance,
            "disk": worker.disk,
            "span": worker.span,
            "artifact_bytes": worker.artifact_bytes,
            "artifact_set_digest": worker.artifact_set_digest,
            "cache_root": worker.cache_root,
        }
        for worker in plan.workers
    ]
    resources = [
        {
            "name": resource.name,
            "kind": resource.kind,
            "provider": resource.provider,
            "region": resource.region,
            "worker_id": resource.worker_id,
        }
        for resource in plan.resources
    ]
    action = state["next_action"]
    action_id = None
    if action != "none":
        action_id = _action_id(plan, action)
    return {
        "schema_version": SCHEMA_VERSION,
        "gate": GATE,
        "run_id": plan.run_id,
        "route_job_id": plan.route_job_id,
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "model_revision": plan.model_revision,
        "worker_plan_digest": plan.worker_plan_digest,
        "deadline_unix": plan.deadline_unix,
        "revision": state["revision"],
        "action": action,
        "action_id": action_id,
        "authorization": dict(plan.authorization),
        "source_bindings": [dict(binding) for binding in plan.source_bindings],
        "resources": resources,
        "resource_specs": [_expected_resource_spec(resource) for resource in plan.resources],
        "workers": workers,
    }


def _prepare_output_path(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parent_metadata = path.parent.lstat()
    parent_reparse = bool(
        getattr(parent_metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )
    if parent_reparse or path.parent.is_symlink() or not stat.S_ISDIR(parent_metadata.st_mode):
        raise RouteControllerError("output parent is unsafe")
    if path.exists() or path.is_symlink():
        metadata = path.lstat()
        reparse = bool(getattr(metadata, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        if reparse or path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise RouteControllerError("output target is unsafe")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_JSON_BYTES:
        raise RouteControllerError("output is too large")
    _prepare_output_path(path)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Advance one durable Qwen3.8 complete-route controller operation")
    parser.add_argument("operation", choices=("start", "status", "collect", "cleanup"))
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--observation", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--artifact-root", type=Path)
    parser.add_argument("--authorization-root", type=Path)
    parser.add_argument("--evidence-root", type=Path)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--decision", type=Path, required=True)
    return parser


def _assert_output_isolation(
    outputs: Sequence[Path],
    input_files: Sequence[Path | None],
    input_roots: Sequence[Path | None],
) -> None:
    output_paths = [path.resolve(strict=False) for path in outputs]
    file_paths = [path.resolve(strict=False) for path in input_files if path is not None]
    if len(set(map(os.path.normcase, map(os.fspath, output_paths)))) != len(output_paths):
        raise RouteControllerError("controller output paths must be distinct")
    if any(
        os.path.normcase(os.fspath(output)) == os.path.normcase(os.fspath(input_path))
        for output in output_paths
        for input_path in file_paths
    ):
        raise RouteControllerError("controller input and output paths must be distinct")
    for root in (path.resolve(strict=False) for path in input_roots if path is not None):
        for output in output_paths:
            try:
                output.relative_to(root)
            except ValueError:
                continue
            raise RouteControllerError("controller output overlaps a protected input root")


@contextmanager
def _controller_lock(path: Path) -> Iterator[None]:
    _prepare_output_path(path)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\\0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RouteControllerError("another controller invocation holds the state lock") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RouteControllerError("another controller invocation holds the state lock") from exc
        try:
            yield
        finally:
            try:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def _validate_journal(
    value: Mapping[str, Any],
    plan: RoutePlan,
    *,
    now_unix: int,
) -> dict[str, Any]:
    journal = dict(_mapping(value, _JOURNAL_FIELDS, "issuance journal"))
    issued_at = _integer(
        journal["issued_at_unix"],
        "issuance journal issued_at_unix",
        1,
    )
    if (
        journal["schema_version"] != SCHEMA_VERSION
        or journal["run_id"] != plan.run_id
        or journal["plan_digest"] != plan.plan_digest
        or journal["start_action_id"] != _action_id(plan, "start_route")
        or issued_at > now_unix
        or journal["status"] not in {"issued", "completed"}
    ):
        raise RouteControllerError("issuance journal binding is invalid")
    if journal["status"] == "issued":
        if (
            any(
                journal[field] is not None
                for field in (
                    "completed_at_unix",
                    "terminal_phase",
                    "terminal_revision",
                    "failure_code",
                    "evidence_digest",
                )
            )
            or journal["cleanup_verified"] is not False
        ):
            raise RouteControllerError("open issuance journal is invalid")
        return journal

    completed_at = _integer(
        journal["completed_at_unix"],
        "issuance journal completed_at_unix",
        issued_at,
    )
    terminal_revision = _integer(
        journal["terminal_revision"],
        "issuance journal terminal_revision",
        1,
    )
    phase = journal["terminal_phase"]
    if completed_at > now_unix or phase not in TERMINAL_PHASES or journal["cleanup_verified"] is not True:
        raise RouteControllerError("completed issuance journal is invalid")
    if phase == "CLEANED_PASS":
        _string(journal["evidence_digest"], _DIGEST_RE, "issuance journal evidence_digest")
        if journal["failure_code"] is not None:
            raise RouteControllerError("passing issuance journal is invalid")
    elif journal["failure_code"] is None or journal["evidence_digest"] is not None:
        raise RouteControllerError("failing issuance journal is invalid")
    return journal


def _issued_journal(plan: RoutePlan, now_unix: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "start_action_id": _action_id(plan, "start_route"),
        "status": "issued",
        "issued_at_unix": now_unix,
        "completed_at_unix": None,
        "terminal_phase": None,
        "terminal_revision": None,
        "failure_code": None,
        "evidence_digest": None,
        "cleanup_verified": False,
    }


def _completed_journal(
    journal: Mapping[str, Any],
    state: Mapping[str, Any],
    now_unix: int,
) -> dict[str, Any]:
    result = dict(journal)
    result.update(
        status="completed",
        completed_at_unix=now_unix,
        terminal_phase=state["phase"],
        terminal_revision=state["revision"],
        failure_code=state["failure_code"],
        evidence_digest=state["evidence_digest"],
        cleanup_verified=state["cleanup_verified"],
    )
    return result


def _state_from_completed_journal(
    journal: Mapping[str, Any],
    plan: RoutePlan,
) -> dict[str, Any]:
    state = initial_state(plan)
    state.update(
        revision=journal["terminal_revision"],
        phase=journal["terminal_phase"],
        failure_code=journal["failure_code"],
        evidence_digest=journal["evidence_digest"],
        cleanup_verified=True,
        next_action="none",
    )
    return validate_state(state, plan)


def _execute_controller(
    args: argparse.Namespace,
    *,
    now_unix: int,
    journal_path: Path,
) -> dict[str, Any]:
    plan = load_plan(args.plan, args.source_root)
    observation = _strict_json(_regular_bytes(args.observation))
    journal = None
    if journal_path.exists():
        journal = _validate_journal(
            _strict_json(_regular_bytes(journal_path)),
            plan,
            now_unix=now_unix,
        )
    if args.state.exists():
        state = _strict_json(_regular_bytes(args.state))
        validated_state = validate_state(state, plan)
        if (
            journal is not None
            and journal["status"] == "completed"
            and (
                validated_state["phase"] != journal["terminal_phase"]
                or validated_state["revision"] != journal["terminal_revision"]
                or validated_state["failure_code"] != journal["failure_code"]
                or validated_state["evidence_digest"] != journal["evidence_digest"]
                or validated_state["cleanup_verified"] is not True
            )
        ):
            raise RouteControllerError("state conflicts with the completed issuance journal")
    elif journal is not None and journal["status"] == "completed":
        validated_state = _state_from_completed_journal(journal, plan)
    else:
        validated_state = initial_state(plan)
    raw_observed_at = observation.get("observed_at_unix")
    cleanup_first = (
        args.operation == "cleanup"
        or validated_state["phase"] == "CLEANING"
        or observation.get("protected_bootstrap_running") is False
        or now_unix >= plan.deadline_unix
    )
    validated_observation = validate_observation(
        observation,
        plan,
        cleanup_only=cleanup_first,
    )
    if not cleanup_first and (
        type(raw_observed_at) is not int
        or raw_observed_at > now_unix
        or now_unix - raw_observed_at > MAX_PLAN_REVALIDATION_AGE_SECONDS
    ):
        raise RouteControllerError("controller observation is stale")
    job_state = validated_observation["route_job"]["state"]
    requires_production_revalidation = not cleanup_first and (
        args.operation in {"start", "collect"} or job_state == "passed"
    )
    requires_start_authorization = (
        not cleanup_first
        and args.operation == "start"
        and _authorized(plan)
        and journal is None
        and validated_state["phase"] == "ABSENT"
        and _all_absent(validated_observation)
    )
    if requires_start_authorization:
        if args.authorization_root is None:
            raise RouteControllerError("paid start requires controller-owned authorization records")
        revalidate_authorization_evidence(
            plan,
            args.authorization_root,
            args.source_root,
            now_unix=now_unix,
        )
    if requires_production_revalidation:
        if args.manifest is None or args.artifact_root is None:
            raise RouteControllerError("start and collection require production artifact plan inputs")
        revalidation = validated_observation["artifact_plan_revalidation"]
        expected_revalidation = revalidate_production_artifact_plan(
            plan,
            args.manifest,
            args.artifact_root,
            args.source_root,
            verified_at_unix=revalidation["verified_at_unix"],
        )
        if dict(revalidation) != expected_revalidation:
            raise RouteControllerError("observation was not produced by the revalidated production artifact plan")
    route_evidence_validated = False
    if not cleanup_first and job_state == "passed":
        if args.evidence_root is None:
            raise RouteControllerError("passed route requires protected evidence records")
        revalidate_route_evidence(
            plan,
            validated_observation,
            args.evidence_root,
            args.source_root,
        )
        route_evidence_validated = True
    next_state = reconcile(
        args.operation,
        validated_state,
        validated_observation,
        plan,
        now_unix=now_unix,
        route_evidence_validated=route_evidence_validated,
        start_was_issued=journal is not None,
    )
    decision = action_record(next_state, plan)
    if decision["action"] == "start_route" and journal is None:
        journal = _issued_journal(plan, now_unix)
        _atomic_json(journal_path, journal)
    if next_state["phase"] in TERMINAL_PHASES and journal is not None and journal["status"] != "completed":
        journal = _completed_journal(journal, next_state, now_unix)
        _atomic_json(journal_path, journal)
    neutral_state = dict(validated_state)
    neutral_state["next_action"] = "none"
    _atomic_json(args.decision, action_record(neutral_state, plan))
    _atomic_json(args.state, next_state)
    _atomic_json(args.decision, decision)
    return decision


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    lock_path = args.state.with_name(f".{args.state.name}.lock")
    journal_path = args.state.with_name(f".{args.state.name}.issuance.json")
    try:
        _assert_output_isolation(
            (args.state, args.decision, lock_path, journal_path),
            (args.plan, args.observation, args.manifest),
            (args.source_root, args.artifact_root, args.authorization_root, args.evidence_root),
        )
        with _controller_lock(lock_path):
            decision = _execute_controller(
                args,
                now_unix=int(time.time()),
                journal_path=journal_path,
            )
    except (OSError, RouteControllerError) as exc:
        print(f"Gate Q3.8 route controller failed: {exc}")
        return 2
    print(json.dumps(decision, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
