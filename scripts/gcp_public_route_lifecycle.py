"""Fail-closed lifecycle controller for one finite Gate 11 GCP public route.

All authorization, ledger, image publication, bootstrap, and uploaded-helper bindings
are validated locally before native authentication is read or any provider command is
executed. Provider output and private route identity remain transient; the bounded
report retains aggregate health, inference stage, ceiling, and cleanup facts only.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

try:
    from scripts import qualification_cost_guard as cost_guard
except ModuleNotFoundError:
    import qualification_cost_guard as cost_guard  # type: ignore[no-redef]

SCHEMA_VERSION = 1
MAX_LOCAL_JSON_BYTES = 1_000_000
MAX_COMMAND_OUTPUT_BYTES = 1_000_000
MAX_REPORT_BYTES = 1_000_000
MAX_AUTH_SECONDS = 60
MAX_PROVIDER_SECONDS = 600
MAX_STARTUP_SECONDS = 3600
MAX_PROBE_REMOTE_SECONDS = 960
REMOTE_ACTION_RESERVE_SECONDS = 30
MIN_HOST_ACTION_SECONDS = 120
HEALTH_PERIOD_SECONDS = 300
HEALTH_FRESHNESS_SECONDS = 330
FUTURE_TOLERANCE_SECONDS = 5
_GIB = 1024**3
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_RUN_RE = re.compile(r"^[a-z0-9][a-z0-9-]{5,39}$")
_PEER_RE = re.compile(r"^/(?:ip4|ip6|dns|dns4|dns6)/[^\s]{1,1900}/p2p/[1-9A-HJ-NP-Za-km-z]{32,128}$")
_IMAGE_RE = re.compile(r"^ghcr\.io/flujo-app/communityai-public-route-(qwen3\.5-2b|gemma-4-e2b)@sha256:[0-9a-f]{64}$")
_AUTHORIZATION_KEYS = {
    "schema_version",
    "scope",
    "result",
    "run_id",
    "provider",
    "workload",
    "source_commit",
    "cloud_ceiling_usd",
    "ledger_committed_before_run_usd",
    "remaining_before_run_usd",
    "maximum_estimate_usd",
    "remaining_after_run_maximum_usd",
    "reservation_recorded",
    "provisioning_authorized",
    "cost_authorization_only",
    "provider_preflight_required",
    "provider_calls_authorized_without_preflight",
    "required_ledger_row",
    "ledger_purpose",
    "provider_plan_digest",
    "pricing_as_of",
    "pricing_revalidate_by",
    "cost_assumptions",
    "provider_plan",
    "cleanup_required_for_pass",
    "failure_cleanup_required",
    "persistent_resources_after_pass",
    "qualification_evidence",
    "complete_release_qualification",
}
_PUBLICATION_KEYS = {
    "schema_version",
    "scope",
    "result",
    "candidate",
    "source_commit",
    "source_tree_digest",
    "dockerfile_digest",
    "uv_lock_digest",
    "manifest_digest",
    "model_repository",
    "model_revision",
    "contract_digest",
    "carrier_evidence_digest",
    "carrier_index_reference",
    "carrier_runtime_image",
    "image_tag",
    "image_reference",
    "runtime_image_reference",
    "index_digest",
    "index_size",
    "runtime_manifest_digest",
    "runtime_manifest_size",
    "attestation_manifest_digest",
    "attestation_manifest_size",
    "platform",
    "device",
    "torch_version",
    "cuda_version",
    "nonroot_uid",
    "training_rpcs",
    "health_state_path",
    "full_block_span",
    "provenance",
    "sbom",
    "layers",
    "compressed_layer_bytes",
    "uncompressed_image_bytes",
    "limits",
    "source_hashes_verified",
    "carrier_evidence_verified",
    "artifact_hashes_verified",
    "image_built",
    "image_published",
    "complete_release_qualification",
}
_HEALTH_KEYS = {
    "schema_version",
    "scope",
    "observed_at",
    "worker_healthy",
    "route",
    "admission_available",
    "admission",
    "components",
}
_ADMISSION_KEYS = {
    "accepted_sessions",
    "active_session_routes",
    "active_sessions",
    "healthy",
    "pending_pushes",
    "rejected_sessions",
    "tracked_peers",
}
_COMPONENT_KEYS = {"ready", "announcer_alive", "handlers_alive", "pools_alive"}
_ROUTE_EXPECTATIONS: Mapping[str, Mapping[str, object]] = {
    "qwen3.5-2b": {
        "role": "primary",
        "manifest": cost_guard.GCP_PRIMARY_MANIFEST_DIGEST,
        "repository": cost_guard.GCP_PRIMARY_IMAGE_REPOSITORY,
        "model_repository": "Qwen/Qwen3.5-2B",
        "model_revision": "15852e8c16360a2fea060d615a32b45270f8a8fc",
        "span": (0, 24),
    },
    "gemma-4-e2b": {
        "role": "standby",
        "manifest": cost_guard.GCP_STANDBY_MANIFEST_DIGEST,
        "repository": cost_guard.GCP_STANDBY_IMAGE_REPOSITORY,
        "model_repository": "google/gemma-4-E2B-it",
        "model_revision": "3e22461f65e89153144f8adb70e3b8c2cc9845a7",
        "span": (0, 35),
    },
}
_PRIVACY_FIELDS = {
    "prompts_retained": False,
    "outputs_retained": False,
    "token_ids_retained": False,
    "credentials_retained": False,
    "paths_retained": False,
    "endpoints_retained": False,
    "peer_ids_retained": False,
    "provider_ids_retained": False,
    "provider_output_retained": False,
    "command_argv_retained": False,
}


class LifecycleError(ValueError):
    """The public-route lifecycle cannot safely continue."""


class ProviderCommandError(LifecycleError):
    """A bounded native provider command failed."""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class RouteBinding:
    role: str
    candidate: str
    manifest_digest: str
    image_reference: str
    runtime_image_reference: str
    evidence_digest: str
    full_span: tuple[int, int]


@dataclass(frozen=True)
class BoundPlan:
    authorization: Mapping[str, Any]
    provider_plan: Mapping[str, Any]
    run_id: str
    source_commit: str
    provider_plan_digest: str
    project: str
    zone: str
    region: str
    primary: RouteBinding
    standby: RouteBinding
    host_controller_digest: str
    acceptance_probe_digest: str
    initial_peer: str


Runner = Callable[[Sequence[str], int], CommandResult]
Clock = Callable[[], float]
WallClock = Callable[[], datetime]
Sleeper = Callable[[float], None]


def _run_bounded(argv: Sequence[str], timeout: int) -> CommandResult:
    if (
        not argv
        or any(
            not isinstance(value, str) or not value or "\x00" in value or "\r" in value or "\n" in value
            for value in argv
        )
        or argv[0] not in {"gcloud", "gh"}
        or not 1 <= timeout <= 3600
    ):
        raise ProviderCommandError("provider command contract is invalid")
    executable = shutil.which(argv[0])
    if executable is None:
        raise ProviderCommandError("bounded provider executable is unavailable")
    try:
        completed = subprocess.run(
            [executable, *argv[1:]],
            check=False,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderCommandError("bounded provider command failed or timed out") from exc
    if len(completed.stdout) > MAX_COMMAND_OUTPUT_BYTES or len(completed.stderr) > MAX_COMMAND_OUTPUT_BYTES:
        raise ProviderCommandError("provider command output exceeded its bound")
    return CommandResult(completed.returncode, completed.stdout, completed.stderr)


def _regular_bytes(path: Path, maximum: int, field: str) -> bytes:
    try:
        mode = path.lstat().st_mode
        if stat.S_ISLNK(mode) or not stat.S_ISREG(mode):
            raise LifecycleError(f"{field} must be a regular non-symlink file")
        with path.open("rb") as stream:
            payload = stream.read(maximum + 1)
    except LifecycleError:
        raise
    except OSError as exc:
        raise LifecycleError(f"{field} is unavailable") from exc
    if len(payload) > maximum:
        raise LifecycleError(f"{field} exceeds its bounded size")
    return payload


def _strict_json(path: Path, field: str, maximum: int = MAX_LOCAL_JSON_BYTES) -> tuple[Mapping[str, Any], bytes]:
    payload = _regular_bytes(path, maximum, field)
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise LifecycleError(f"{field} is not bounded JSON") from exc
    if not isinstance(value, dict):
        raise LifecycleError(f"{field} must be a JSON object")
    return value, payload


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _require_digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or _DIGEST_RE.fullmatch(value) is None:
        raise LifecycleError(f"{field} is not a canonical SHA-256 digest")
    return value


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LifecycleError(f"{field} must be a positive integer")
    return value


def _initial_peer(value: Any) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or "\n" in value
        or "\r" in value
        or "\x00" in value
        or _PEER_RE.fullmatch(value) is None
    ):
        raise LifecycleError("provider plan initial peer is invalid")
    return value


def _validate_source_binding(path: Path, binding: Mapping[str, Any], field: str) -> str:
    if set(binding) != {"relative_path", "sha256", "byte_size", "source_commit_bound", "validated_by_cost_guard"}:
        raise LifecycleError(f"{field} binding schema is invalid")
    payload = _regular_bytes(path, 1_000_000, field)
    if (
        binding["relative_path"] != path.as_posix().split("/")[-2] + "/" + path.name
        and binding["relative_path"] != f"scripts/{path.name}"
    ):
        raise LifecycleError(f"{field} relative path is invalid")
    digest = _require_digest(binding["sha256"], f"{field} digest")
    if (
        digest != _sha256(payload)
        or binding["byte_size"] != len(payload)
        or binding["source_commit_bound"] is not True
        or binding["validated_by_cost_guard"] is not False
    ):
        raise LifecycleError(f"{field} source binding does not match the exact file")
    return digest


def _load_publication(
    path: Path,
    *,
    expected_digest: str,
    planned_route: Mapping[str, Any],
) -> RouteBinding:
    evidence, payload = _strict_json(path, "public-route publication evidence")
    if _sha256(payload) != expected_digest:
        raise LifecycleError("public-route publication evidence digest does not match the plan")
    if set(evidence) != _PUBLICATION_KEYS:
        raise LifecycleError("public-route publication evidence schema is invalid")
    candidate = evidence.get("candidate")
    expected = _ROUTE_EXPECTATIONS.get(candidate) if isinstance(candidate, str) else None
    if expected is None:
        raise LifecycleError("public-route publication candidate is invalid")
    role = expected["role"]
    if planned_route.get("role") != role or planned_route.get("candidate") != candidate:
        raise LifecycleError("public-route publication role does not match the plan")
    for field in (
        "source_commit",
        "source_tree_digest",
        "dockerfile_digest",
        "uv_lock_digest",
        "contract_digest",
        "carrier_evidence_digest",
        "index_digest",
        "runtime_manifest_digest",
        "attestation_manifest_digest",
    ):
        _require_digest(evidence[field], f"publication {field}") if field != "source_commit" else None
    source_commit = evidence["source_commit"]
    if not isinstance(source_commit, str) or _COMMIT_RE.fullmatch(source_commit) is None:
        raise LifecycleError("publication source commit is invalid")
    if (
        evidence["schema_version"] != 1
        or evidence["scope"] != "public-route-image-publication-evidence"
        or evidence["result"] != "passed"
        or evidence["manifest_digest"] != expected["manifest"]
        or evidence["model_repository"] != expected["model_repository"]
        or evidence["model_revision"] != expected["model_revision"]
        or planned_route.get("manifest_digest") != expected["manifest"]
        or evidence["platform"] != "linux/amd64"
        or evidence["device"] != "cuda"
        or evidence["torch_version"] != "2.6.0+cu124"
        or evidence["cuda_version"] != "12.4"
        or evidence["nonroot_uid"] != 65532
        or evidence["training_rpcs"] != "disabled"
        or evidence["health_state_path"] != "/run/communityai/health.json"
        or evidence["provenance"] != "slsa-build-arguments-and-materials-verified"
        or evidence["sbom"] != "spdx-2.3-required-cuda-packages-verified"
        or any(
            evidence[field] is not True
            for field in (
                "source_hashes_verified",
                "carrier_evidence_verified",
                "artifact_hashes_verified",
                "image_built",
                "image_published",
            )
        )
        or evidence["complete_release_qualification"] is not False
    ):
        raise LifecycleError("public-route publication evidence result is invalid")
    repository = str(expected["repository"])
    index_digest = evidence["index_digest"]
    runtime_digest = evidence["runtime_manifest_digest"]
    if (
        evidence["image_reference"] != f"{repository}@{index_digest}"
        or evidence["runtime_image_reference"] != f"{repository}@{runtime_digest}"
        or planned_route.get("image") != evidence["image_reference"]
        or _IMAGE_RE.fullmatch(evidence["image_reference"]) is None
        or _IMAGE_RE.fullmatch(evidence["runtime_image_reference"]) is None
    ):
        raise LifecycleError("public-route publication references are not exactly bound")
    span = evidence["full_block_span"]
    expected_span = expected["span"]
    if (
        not isinstance(expected_span, tuple)
        or len(expected_span) != 2
        or any(isinstance(value, bool) or not isinstance(value, int) for value in expected_span)
        or not isinstance(span, str)
        or span != f"{expected_span[0]}:{expected_span[1]}"
    ):
        raise LifecycleError("public-route publication full block span is invalid")
    layers = evidence["layers"]
    if not isinstance(layers, list) or not 1 <= len(layers) <= 256:
        raise LifecycleError("public-route publication layers are invalid")
    compressed = 0
    for index, layer in enumerate(layers):
        if not isinstance(layer, dict) or set(layer) != {"digest", "media_type", "compressed_size"}:
            raise LifecycleError(f"public-route publication layer {index} schema is invalid")
        _require_digest(layer["digest"], f"publication layer {index} digest")
        size = _positive_integer(layer["compressed_size"], f"publication layer {index} size")
        if size > 10_000_000_000:
            raise LifecycleError(f"public-route publication layer {index} exceeds the GHCR limit")
        compressed += size
    if compressed != evidence["compressed_layer_bytes"]:
        raise LifecycleError("public-route publication compressed layer total is invalid")
    limits = evidence["limits"]
    if not isinstance(limits, dict) or limits.get("ghcr_max_layer_bytes") != 10_000_000_000:
        raise LifecycleError("public-route publication limits are invalid")
    if (
        compressed > _positive_integer(limits.get("maximum_compressed_bytes"), "maximum compressed bytes")
        or _positive_integer(evidence["uncompressed_image_bytes"], "uncompressed image bytes")
        > _positive_integer(limits.get("maximum_uncompressed_bytes"), "maximum uncompressed bytes")
        or limits.get("combined_route_disk_ceiling_bytes") != 160 * _GIB
        or limits.get("planned_boot_disk_bytes") != 200 * _GIB
    ):
        raise LifecycleError("public-route publication exceeds its planned limits")
    return RouteBinding(
        role=str(role),
        candidate=str(candidate),
        manifest_digest=str(expected["manifest"]),
        image_reference=str(evidence["image_reference"]),
        runtime_image_reference=str(evidence["runtime_image_reference"]),
        evidence_digest=expected_digest,
        full_span=(expected_span[0], expected_span[1]),
    )


def load_bound_plan(
    *,
    authorization_path: Path,
    ledger_path: Path,
    primary_evidence_path: Path,
    standby_evidence_path: Path,
    bootstrap_path: Path,
    host_controller_path: Path,
    acceptance_probe_path: Path,
    expected_source_commit: str,
) -> BoundPlan:
    if _COMMIT_RE.fullmatch(expected_source_commit) is None:
        raise LifecycleError("lifecycle source commit is invalid")
    authorization, _payload = _strict_json(authorization_path, "cloud authorization")
    if (
        set(authorization) != _AUTHORIZATION_KEYS
        or authorization.get("schema_version") != cost_guard.SCHEMA_VERSION
        or authorization.get("scope") != "communityai-cloud-cost-authorization"
        or authorization.get("result") != "passed"
        or authorization.get("provider") != "GCP"
        or authorization.get("workload") != cost_guard.GCP_PUBLIC_ROUTE_WORKLOAD
        or authorization.get("source_commit") != expected_source_commit
        or authorization.get("reservation_recorded") is not True
        or authorization.get("provisioning_authorized") is not True
        or authorization.get("cost_authorization_only") is not True
        or authorization.get("provider_calls_authorized_without_preflight") is not False
    ):
        raise LifecycleError("cloud authorization is not an exact reserved GCP public-route plan")
    run_id = authorization.get("run_id")
    if not isinstance(run_id, str) or _RUN_RE.fullmatch(run_id) is None:
        raise LifecycleError("cloud authorization run ID is invalid")
    provider_plan = authorization.get("provider_plan")
    if not isinstance(provider_plan, dict):
        raise LifecycleError("cloud authorization provider plan is invalid")
    digest = cost_guard._provider_plan_digest(provider_plan)
    if authorization.get("provider_plan_digest") != digest or digest not in str(
        authorization.get("ledger_purpose", "")
    ):
        raise LifecycleError("cloud authorization provider-plan digest is invalid")

    entries = cost_guard.load_spend_ledger(ledger_path)
    matching = [entry for entry in entries if entry.run_id == run_id]
    if (
        len(matching) != 1
        or matching[0].provider != "GCP"
        or matching[0].state != "PLANNED"
        or matching[0].purpose != authorization.get("ledger_purpose")
        or cost_guard._usd(matching[0].maximum_usd) != authorization.get("maximum_estimate_usd")
    ):
        raise LifecycleError("cloud authorization no longer matches the exact ledger reservation")

    ledger_purpose = authorization.get("ledger_purpose")
    if not isinstance(ledger_purpose, str) or " [workload " not in ledger_purpose:
        raise LifecycleError("cloud authorization ledger purpose is invalid")
    purpose = ledger_purpose.split(" [workload ", 1)[0]
    machine = provider_plan.get("machine")
    bootstrap_binding = provider_plan.get("runtime_bootstrap")
    host_binding = provider_plan.get("host_controller")
    acceptance_binding = provider_plan.get("acceptance_probe")
    raw_routes = provider_plan.get("routes")
    if (
        not isinstance(machine, dict)
        or not isinstance(bootstrap_binding, dict)
        or not isinstance(host_binding, dict)
        or not isinstance(acceptance_binding, dict)
        or not isinstance(raw_routes, list)
        or len(raw_routes) != 2
    ):
        raise LifecycleError("cloud authorization cannot be regenerated from exact plan inputs")
    route_by_role = {route.get("role"): route for route in raw_routes if isinstance(route, dict)}
    if set(route_by_role) != {"primary", "standby"}:
        raise LifecycleError("cloud authorization cannot be regenerated from exact routes")
    try:
        rebuilt = cost_guard.build_authorization(
            entries=entries,
            run_id=run_id,
            provider="gcp",
            workload=cost_guard.GCP_PUBLIC_ROUTE_WORKLOAD,
            purpose=purpose,
            source_commit=expected_source_commit,
            maximum_hours=Decimal(str(provider_plan["maximum_runtime_hours"])),
            project=str(provider_plan["project"]),
            zone=str(provider_plan["zone"]),
            windows_image=None,
            linux_image=str(machine["os_image"]),
            cuda_fallback_zone=None,
            cuda_shape="g2-l4",
            manual_maximum_usd=None,
            primary_image=str(route_by_role["primary"]["image"]),
            primary_image_evidence_digest=str(route_by_role["primary"]["publication_evidence"]["expected_digest"]),
            standby_image=str(route_by_role["standby"]["image"]),
            standby_image_evidence_digest=str(route_by_role["standby"]["publication_evidence"]["expected_digest"]),
            runtime_bootstrap_digest=str(bootstrap_binding["sha256"]),
            runtime_bootstrap_bytes=int(bootstrap_binding["byte_size"]),
            initial_peer=str(provider_plan["initial_peer"]),
            host_controller_digest=str(host_binding["sha256"]),
            host_controller_bytes=int(host_binding["byte_size"]),
            acceptance_probe_digest=str(acceptance_binding["sha256"]),
            acceptance_probe_bytes=int(acceptance_binding["byte_size"]),
            today=date.today(),
        )
    except (KeyError, TypeError, ValueError, cost_guard.CostGuardError) as exc:
        raise LifecycleError("cloud authorization could not be regenerated") from exc
    if rebuilt != authorization:
        raise LifecycleError("cloud authorization does not match the regenerated live-ledger plan")

    routes = provider_plan.get("routes")
    if not isinstance(routes, list) or len(routes) != 2 or any(not isinstance(route, dict) for route in routes):
        raise LifecycleError("provider plan route bindings are invalid")
    by_role = {route.get("role"): route for route in routes}
    if set(by_role) != {"primary", "standby"}:
        raise LifecycleError("provider plan must bind one primary and one standby route")
    primary_spec = by_role["primary"]
    standby_spec = by_role["standby"]
    primary_publication = primary_spec.get("publication_evidence")
    standby_publication = standby_spec.get("publication_evidence")
    if not isinstance(primary_publication, dict) or not isinstance(standby_publication, dict):
        raise LifecycleError("provider plan publication bindings are invalid")
    primary_digest = _require_digest(primary_publication.get("expected_digest"), "primary evidence digest")
    standby_digest = _require_digest(standby_publication.get("expected_digest"), "standby evidence digest")
    primary = _load_publication(
        primary_evidence_path,
        expected_digest=primary_digest,
        planned_route=primary_spec,
    )
    standby = _load_publication(
        standby_evidence_path,
        expected_digest=standby_digest,
        planned_route=standby_spec,
    )

    bootstrap = provider_plan.get("runtime_bootstrap")
    host = provider_plan.get("host_controller")
    acceptance = provider_plan.get("acceptance_probe")
    if not isinstance(bootstrap, dict) or not isinstance(host, dict) or not isinstance(acceptance, dict):
        raise LifecycleError("provider plan source bindings are incomplete")
    _validate_source_binding(bootstrap_path, bootstrap, "runtime bootstrap")
    host_digest = _validate_source_binding(host_controller_path, host, "host controller")
    acceptance_digest = _validate_source_binding(acceptance_probe_path, acceptance, "acceptance probe")

    project = provider_plan.get("project")
    zone = provider_plan.get("zone")
    region = provider_plan.get("region")
    initial_peer = _initial_peer(provider_plan.get("initial_peer"))
    if any(not isinstance(value, str) or not value for value in (project, zone, region)):
        raise LifecycleError("provider plan target is invalid")
    return BoundPlan(
        authorization=authorization,
        provider_plan=provider_plan,
        run_id=run_id,
        source_commit=expected_source_commit,
        provider_plan_digest=digest,
        project=project,
        zone=zone,
        region=region,
        primary=primary,
        standby=standby,
        host_controller_digest=host_digest,
        acceptance_probe_digest=acceptance_digest,
        initial_peer=initial_peer,
    )


def _parse_utc(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40 or not value.endswith("Z"):
        raise LifecycleError(f"{field} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise LifecycleError(f"{field} timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise LifecycleError(f"{field} timestamp is invalid")
    return parsed


def validate_health_sample(
    payload: Mapping[str, Any],
    *,
    route: RouteBinding,
    observed_now: datetime,
    require_healthy: bool,
    previous: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    if set(payload) != _HEALTH_KEYS:
        raise LifecycleError("worker health schema is invalid")
    worker_observed = _parse_utc(payload["observed_at"], "worker health")
    age = (observed_now - worker_observed).total_seconds()
    if age < -FUTURE_TOLERANCE_SECONDS or age > HEALTH_FRESHNESS_SECONDS:
        raise LifecycleError("worker health is future-dated or stale")
    route_payload = payload["route"]
    admission = payload["admission"]
    components = payload["components"]
    if (
        payload["schema_version"] != 1
        or payload["scope"] != "manifested-public-worker-health"
        or not isinstance(route_payload, dict)
        or set(route_payload) != {"manifest_digest", "start_block", "end_block"}
        or route_payload["manifest_digest"] != route.manifest_digest
        or (route_payload["start_block"], route_payload["end_block"]) != route.full_span
        or not isinstance(admission, dict)
        or set(admission) != _ADMISSION_KEYS
        or not isinstance(components, dict)
        or set(components) != _COMPONENT_KEYS
    ):
        raise LifecycleError("worker health identity or exact block coverage is invalid")
    if any(not isinstance(components[field], bool) for field in _COMPONENT_KEYS):
        raise LifecycleError("worker health components are invalid")
    if not isinstance(admission["healthy"], bool):
        raise LifecycleError("worker admission health flag is invalid")
    for field in _ADMISSION_KEYS - {"healthy"}:
        value = admission[field]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 2**63 - 1:
            raise LifecycleError("worker admission health counters are invalid")
    if require_healthy and (
        payload["worker_healthy"] is not True
        or payload["admission_available"] is not True
        or admission["healthy"] is not True
        or any(components[field] is not True for field in _COMPONENT_KEYS)
    ):
        raise LifecycleError("worker aggregate health is unavailable or unhealthy")
    if previous is not None:
        previous_time = _parse_utc(previous["observed_at"], "previous worker health")
        if worker_observed <= previous_time:
            raise LifecycleError("worker health timestamp did not advance")
        previous_admission = previous["admission"]
        if any(admission[field] < previous_admission[field] for field in _ADMISSION_KEYS - {"healthy"}):
            raise LifecycleError("worker admission counters moved backwards")
    return payload


def validate_resource_sample(payload: Mapping[str, Any], plan: BoundPlan) -> Mapping[str, int]:
    expected = {
        "device_bytes",
        "unattributed_device_bytes",
        "combined_device_bytes",
        "host_memory_bytes",
        "route_storage_bytes",
        "combined_log_bytes",
        "restart_counts",
    }
    if set(payload) != expected:
        raise LifecycleError("host resource sample schema is invalid")
    device = payload["device_bytes"]
    restarts = payload["restart_counts"]
    if not isinstance(device, dict) or set(device) != {plan.primary.candidate, plan.standby.candidate}:
        raise LifecycleError("host route GPU attribution is invalid")
    if not isinstance(restarts, dict) or set(restarts) != set(device):
        raise LifecycleError("host restart accounting is invalid")
    values: dict[str, int] = {}
    for field in (
        "unattributed_device_bytes",
        "combined_device_bytes",
        "host_memory_bytes",
        "route_storage_bytes",
        "combined_log_bytes",
    ):
        value = payload[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LifecycleError("host resource sample contains an invalid count")
        values[field] = value
    for candidate, value in device.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise LifecycleError("host route GPU sample is invalid")
        values[f"{candidate}_device_bytes"] = value
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in restarts.values()):
        raise LifecycleError("host restart accounting is invalid")
    ceilings = plan.provider_plan["operating_contract"]["resource_ceilings"]
    if (
        values[f"{plan.primary.candidate}_device_bytes"] > int(ceilings["qwen_device_memory_gib"]) * _GIB
        or values[f"{plan.standby.candidate}_device_bytes"] > int(ceilings["gemma_device_memory_gib"]) * _GIB
        or values["combined_device_bytes"] > int(ceilings["combined_device_memory_gib"]) * _GIB
        or values["unattributed_device_bytes"] != 0
        or values["host_memory_bytes"] > int(ceilings["host_memory_gib"]) * _GIB
        or values["route_storage_bytes"] > int(ceilings["route_storage_gib"]) * _GIB
        or values["combined_log_bytes"] > int(ceilings["combined_logs_gib"]) * _GIB
        or any(value > 0 for value in restarts.values())
    ):
        raise LifecycleError("host resource or restart stop condition was reached")
    return values


def _require_success(result: CommandResult, field: str, *, empty: bool = False) -> bytes:
    if result.returncode != 0:
        raise ProviderCommandError(f"{field} failed")
    if empty and result.stdout.strip():
        raise ProviderCommandError(f"{field} did not return empty stdout")
    return result.stdout


def _quota_headroom(payload: bytes, metric: str) -> float:
    if len(payload) > MAX_COMMAND_OUTPUT_BYTES:
        raise ProviderCommandError("quota response is unbounded")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderCommandError("quota response is invalid") from exc
    quotas = value.get("quotas") if isinstance(value, dict) else None
    if not isinstance(quotas, list) or len(quotas) > 512:
        raise ProviderCommandError("quota response is invalid")
    matches = [item for item in quotas if isinstance(item, dict) and item.get("metric") == metric]
    if len(matches) != 1:
        raise ProviderCommandError(f"required {metric} quota is unavailable")
    limit = matches[0].get("limit")
    usage = matches[0].get("usage")
    if (
        isinstance(limit, bool)
        or isinstance(usage, bool)
        or not isinstance(limit, (int, float))
        or not isinstance(usage, (int, float))
        or not math.isfinite(float(limit))
        or not math.isfinite(float(usage))
        or float(limit) < 0
        or float(usage) < 0
    ):
        raise ProviderCommandError("quota values are invalid")
    return float(limit) - float(usage)


def _protected_bootstrap_running(plan: BoundPlan, runner: Runner) -> bool:
    protected = _require_success(
        runner(
            (
                "gcloud",
                "compute",
                "instances",
                "list",
                "--filter=name=communityai-bootstrap-1",
                "--format=value(status)",
                "--project",
                plan.project,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "protected bootstrap state",
    )
    return protected.strip() == b"RUNNING"


def _native_preflight(plan: BoundPlan, runner: Runner) -> None:
    auth = _require_success(
        runner(("gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"), MAX_AUTH_SECONDS),
        "gcloud authentication",
    )
    if not auth.strip() or len(auth) > 4096:
        raise ProviderCommandError("gcloud authentication is unavailable")
    _require_success(
        runner(("gh", "auth", "status", "--hostname", "github.com"), MAX_AUTH_SECONDS), "GitHub authentication"
    )
    common = ("--project", plan.project)
    machine = _require_success(
        runner(
            (
                "gcloud",
                "compute",
                "machine-types",
                "describe",
                "g2-standard-8",
                "--zone",
                plan.zone,
                "--format=value(name)",
                *common,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "G2 machine availability",
    )
    accelerator = _require_success(
        runner(
            (
                "gcloud",
                "compute",
                "accelerator-types",
                "describe",
                "nvidia-l4",
                "--zone",
                plan.zone,
                "--format=value(name)",
                *common,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "L4 availability",
    )
    if machine.strip() != b"g2-standard-8" or accelerator.strip() != b"nvidia-l4":
        raise ProviderCommandError("exact G2/L4 capacity is unavailable")
    regional_quota = _require_success(
        runner(
            (
                "gcloud",
                "compute",
                "regions",
                "describe",
                plan.region,
                "--format=json(quotas)",
                *common,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "regional L4 quota",
    )
    global_quota = _require_success(
        runner(
            (
                "gcloud",
                "compute",
                "project-info",
                "describe",
                "--format=json(quotas)",
                *common,
            ),
            MAX_PROVIDER_SECONDS,
        ),
        "global GPU quota",
    )
    if _quota_headroom(regional_quota, "NVIDIA_L4_GPUS") < 1 or _quota_headroom(global_quota, "GPUS_ALL_REGIONS") < 1:
        raise ProviderCommandError("one unused regional and global L4 quota slot is required")
    if not _protected_bootstrap_running(plan, runner):
        raise ProviderCommandError("protected bootstrap is not running")
    for index, command in enumerate(plan.provider_plan["verify_cleanup_commands"]):
        _require_success(runner(tuple(command), MAX_PROVIDER_SECONDS), f"initial absence check {index}", empty=True)


def _parse_instance_verification(payload: bytes) -> str:
    if len(payload) > 4096:
        raise ProviderCommandError("instance verification output is unbounded")
    try:
        text = payload.decode("utf-8").strip()
    except UnicodeError as exc:
        raise ProviderCommandError("instance verification output is invalid") from exc
    fields = text.split()
    if (
        len(fields) != 4
        or fields[0] != "RUNNING"
        or "g2-standard-8" not in fields[1]
        or fields[2] not in {"50400", "50400s"}
    ):
        raise ProviderCommandError("created instance does not match the exact plan")
    try:
        address = ipaddress.ip_address(fields[3])
    except ValueError as exc:
        raise ProviderCommandError("created instance public address is invalid") from exc
    if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
        raise ProviderCommandError("created instance public address is not global IPv4")
    return address.compressed


def _ssh_prefix(plan: BoundPlan) -> tuple[str, ...]:
    instance = str(plan.provider_plan["resources"]["instance"])
    return (
        "gcloud",
        "compute",
        "ssh",
        instance,
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--project",
        plan.project,
        "--quiet",
    )


def _remote_command(
    plan: BoundPlan, runner: Runner, argv: Sequence[str], timeout: int = MAX_PROVIDER_SECONDS
) -> Mapping[str, Any]:
    if any(not value or "\n" in value or "\r" in value or "\x00" in value for value in argv):
        raise ProviderCommandError("remote fixed action argv is invalid")
    shell_command = shlex.join(tuple(argv))
    result = runner((*_ssh_prefix(plan), "--command", shell_command), timeout)
    raw = _require_success(result, "fixed host action")
    if len(raw) > 65_536:
        raise ProviderCommandError("fixed host acknowledgement is unbounded")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProviderCommandError("fixed host acknowledgement is invalid") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != 1
        or value.get("scope") != "gcp-public-route-host-action"
        or value.get("result") != "passed"
        or not isinstance(value.get("details"), dict)
    ):
        raise ProviderCommandError("fixed host acknowledgement schema is invalid")
    return value


def _install_helpers(
    plan: BoundPlan,
    runner: Runner,
    host_controller_path: Path,
    acceptance_probe_path: Path,
) -> None:
    instance = str(plan.provider_plan["resources"]["instance"])
    scp = (
        "gcloud",
        "compute",
        "scp",
        os.fspath(host_controller_path),
        os.fspath(acceptance_probe_path),
        f"{instance}:/tmp/",
        "--zone",
        plan.zone,
        "--tunnel-through-iap",
        "--project",
        plan.project,
        "--quiet",
    )
    _require_success(runner(scp, MAX_PROVIDER_SECONDS), "host helper upload")
    install_argv = (
        "sudo",
        "python3",
        "-c",
        (
            "import hashlib,os,pathlib;"
            f"items=[('/tmp/{host_controller_path.name}','/var/lib/communityai-route/{host_controller_path.name}','{plan.host_controller_digest}',0o500),"
            f"('/tmp/{acceptance_probe_path.name}','/var/lib/communityai-route/{acceptance_probe_path.name}','{plan.acceptance_probe_digest}',0o444)];"
            "pathlib.Path('/var/lib/communityai-route').mkdir(mode=0o700,parents=True,exist_ok=True);"
            "[(lambda p,s,d,m:(hashlib.sha256(p).hexdigest()==d[7:] or (_ for _ in ()).throw(SystemExit(2)),"
            "pathlib.Path(s).write_bytes(p),os.chmod(s,m)))(pathlib.Path(a).read_bytes(),b,c,m) for a,b,c,m in items]"
        ),
    )
    install_result = runner((*_ssh_prefix(plan), "--command", shlex.join(install_argv)), MAX_PROVIDER_SECONDS)
    _require_success(install_result, "host helper install")


def _host_action(
    plan: BoundPlan,
    runner: Runner,
    action: str,
    *,
    public_ipv4: str | None = None,
    initial_peer: str | None = None,
    timeout: int = MAX_PROVIDER_SECONDS,
) -> Mapping[str, Any]:
    if not isinstance(timeout, int) or not 1 <= timeout <= MAX_STARTUP_SECONDS:
        raise LifecycleError("fixed host action timeout is invalid")
    argv = [
        "sudo",
        "python3",
        "/var/lib/communityai-route/gcp_public_route_host.py",
        "--action",
        action,
        "--run-id",
        plan.run_id,
    ]
    if action in {"start-primary", "start-standby"}:
        if public_ipv4 is None or initial_peer is None:
            raise LifecycleError("route start is missing its fixed runtime inputs")
        argv.extend(
            [
                "--primary-image",
                plan.primary.runtime_image_reference,
                "--standby-image",
                plan.standby.runtime_image_reference,
                "--public-ipv4",
                public_ipv4,
                "--initial-peer",
                initial_peer,
                "--acceptance-digest",
                plan.acceptance_probe_digest,
            ]
        )
    if action in {
        "start-primary",
        "start-standby",
        "probe-primary",
        "probe-standby",
        "probe-auto",
    }:
        action_timeout = timeout - REMOTE_ACTION_RESERVE_SECONDS
        if action_timeout < MIN_HOST_ACTION_SECONDS:
            raise LifecycleError("fixed host action has insufficient bounded time remaining")
        argv.extend(["--action-timeout-seconds", str(action_timeout)])
    return _remote_command(plan, runner, argv, timeout=timeout)


def _remaining_startup_seconds(deadline: float, clock: Clock) -> int:
    remaining = math.ceil(deadline - clock())
    if remaining < MIN_HOST_ACTION_SECONDS + REMOTE_ACTION_RESERVE_SECONDS:
        raise LifecycleError("route startup exhausted its 60-minute boundary")
    return min(MAX_STARTUP_SECONDS, remaining)


def _sample(
    plan: BoundPlan,
    runner: Runner,
    *,
    observed_now: datetime,
    previous: Mapping[str, Mapping[str, Any]] | None,
    require_primary: bool,
    timeout: int = MAX_PROVIDER_SECONDS,
) -> tuple[dict[str, Mapping[str, Any]], Mapping[str, int], Mapping[str, bool]]:
    response = _host_action(plan, runner, "health", timeout=timeout)
    details = response["details"]
    raw_health = details.get("health")
    continuity = details.get("identity_continuity")
    resources = details.get("resources")
    if not isinstance(raw_health, dict) or not isinstance(continuity, dict) or not isinstance(resources, dict):
        raise LifecycleError("fixed host health acknowledgement is incomplete")
    current: dict[str, Mapping[str, Any]] = {}
    for route, required in ((plan.primary, require_primary), (plan.standby, True)):
        value = raw_health.get(route.candidate)
        if not required and value is None:
            continue
        if not isinstance(value, dict):
            raise LifecycleError("required route health is missing")
        current[route.candidate] = validate_health_sample(
            value,
            route=route,
            observed_now=observed_now,
            require_healthy=True,
            previous=None if previous is None else previous.get(route.candidate),
        )
    if set(continuity) != {plan.primary.candidate, plan.standby.candidate} or any(
        value is not True for value in continuity.values()
    ):
        raise LifecycleError("route identity continuity was lost")
    resource_values = validate_resource_sample(resources, plan)
    return current, resource_values, continuity


def _cleanup_provider(plan: BoundPlan, runner: Runner) -> tuple[int, list[bool]]:
    deleted = 0
    for command in plan.provider_plan["cleanup_commands"]:
        try:
            result = runner(tuple(command), MAX_PROVIDER_SECONDS)
            if result.returncode == 0:
                deleted += 1
        except Exception:
            continue
    absence: list[bool] = []
    for command in plan.provider_plan["verify_cleanup_commands"]:
        try:
            result = runner(tuple(command), MAX_PROVIDER_SECONDS)
            absence.append(result.returncode == 0 and not result.stdout.strip())
        except Exception:
            absence.append(False)
    return deleted, absence


def execute_lifecycle(
    plan: BoundPlan,
    *,
    host_controller_path: Path,
    acceptance_probe_path: Path,
    output_path: Path,
    monitor_seconds: int,
    runner: Runner = _run_bounded,
    clock: Clock = time.monotonic,
    wall_clock: WallClock = lambda: datetime.now(timezone.utc),
    sleeper: Sleeper = time.sleep,
) -> Mapping[str, Any]:
    if not isinstance(monitor_seconds, int) or not 0 <= monitor_seconds <= 50_400:
        raise LifecycleError("monitor duration must be between zero and 50400 seconds")
    stages = {
        "local_validation": True,
        "native_authentication": False,
        "provider_preflight": False,
        "create": False,
        "bootstrap": False,
        "primary_inference": False,
        "standby_inference": False,
        "auto_primary": False,
        "primary_disabled": False,
        "auto_fallback": False,
        "primary_restored": False,
        "auto_restored": False,
        "monitor": False,
        "cleanup": False,
    }
    started = clock()
    created = False
    health_samples = 0
    maxima: dict[str, int] = {}
    cleanup_deleted = 0
    absence = [False] * 6
    result = "failed"
    failure_stage = "native_authentication"
    public_ipv4 = None
    protected_bootstrap_running = False
    previous: dict[str, Mapping[str, Any]] | None = None
    try:
        _native_preflight(plan, runner)
        stages["native_authentication"] = True
        stages["provider_preflight"] = True
        protected_bootstrap_running = True
        failure_stage = "create"
        created = True
        for command in plan.provider_plan["create_commands"]:
            _require_success(runner(tuple(command), MAX_PROVIDER_SECONDS), "provider create command")
        verification = plan.provider_plan["verify_create_commands"]
        if not isinstance(verification, list) or len(verification) != 1:
            raise LifecycleError("provider create verification contract is invalid")
        public_ipv4 = _parse_instance_verification(
            _require_success(runner(tuple(verification[0]), MAX_PROVIDER_SECONDS), "instance verification")
        )
        stages["create"] = True
        failure_stage = "bootstrap"
        _install_helpers(plan, runner, host_controller_path, acceptance_probe_path)
        preflight = _host_action(plan, runner, "preflight")
        if preflight.get("action") != "preflight" or set(preflight["details"]) != {
            "bootstrap_ready",
            "docker_ready",
            "gpu_ready",
        }:
            raise LifecycleError("host bootstrap acknowledgement is invalid")
        stages["bootstrap"] = True

        startup_deadline = clock() + MAX_STARTUP_SECONDS
        failure_stage = "startup_health"
        _host_action(
            plan,
            runner,
            "start-primary",
            public_ipv4=public_ipv4,
            initial_peer=plan.initial_peer,
            timeout=_remaining_startup_seconds(startup_deadline, clock),
        )
        _host_action(
            plan,
            runner,
            "start-standby",
            public_ipv4=public_ipv4,
            initial_peer=plan.initial_peer,
            timeout=_remaining_startup_seconds(startup_deadline, clock),
        )
        while True:
            try:
                previous, values, _continuity = _sample(
                    plan,
                    runner,
                    observed_now=wall_clock(),
                    previous=previous,
                    require_primary=True,
                    timeout=min(
                        MAX_PROVIDER_SECONDS,
                        _remaining_startup_seconds(startup_deadline, clock),
                    ),
                )
                health_samples += 1
                for key, value in values.items():
                    maxima[key] = max(maxima.get(key, 0), value)
                break
            except LifecycleError:
                if clock() >= startup_deadline:
                    raise
                sleeper(min(5.0, max(0.0, startup_deadline - clock())))

        failure_stage = "primary_inference"
        primary_probe = _host_action(plan, runner, "probe-primary", timeout=MAX_PROBE_REMOTE_SECONDS)
        if primary_probe["details"].get("candidate") != plan.primary.candidate:
            raise LifecycleError("explicit primary inference selected the wrong candidate")
        stages["primary_inference"] = True
        failure_stage = "standby_inference"
        standby_probe = _host_action(plan, runner, "probe-standby", timeout=MAX_PROBE_REMOTE_SECONDS)
        if standby_probe["details"].get("candidate") != plan.standby.candidate:
            raise LifecycleError("explicit standby inference selected the wrong candidate")
        stages["standby_inference"] = True
        auto = _host_action(plan, runner, "probe-auto", timeout=MAX_PROBE_REMOTE_SECONDS)
        if auto["details"].get("candidate") != plan.primary.candidate:
            raise LifecycleError("healthy automatic selection did not choose Qwen")
        stages["auto_primary"] = True

        failure_stage = "fallback"
        _host_action(plan, runner, "stop-primary")
        stages["primary_disabled"] = True
        sleeper(35.0)
        previous, values, _continuity = _sample(
            plan,
            runner,
            observed_now=wall_clock(),
            previous=previous,
            require_primary=False,
        )
        health_samples += 1
        for key, value in values.items():
            maxima[key] = max(maxima.get(key, 0), value)
        fallback = _host_action(plan, runner, "probe-auto", timeout=MAX_PROBE_REMOTE_SECONDS)
        if fallback["details"].get("candidate") != plan.standby.candidate:
            raise LifecycleError("automatic fallback did not select Gemma")
        stages["auto_fallback"] = True

        _host_action(plan, runner, "restore-primary")
        restore_deadline = clock() + MAX_STARTUP_SECONDS
        while True:
            try:
                previous, values, _continuity = _sample(
                    plan,
                    runner,
                    observed_now=wall_clock(),
                    previous=previous,
                    require_primary=True,
                    timeout=min(
                        MAX_PROVIDER_SECONDS,
                        _remaining_startup_seconds(restore_deadline, clock),
                    ),
                )
                health_samples += 1
                for key, value in values.items():
                    maxima[key] = max(maxima.get(key, 0), value)
                break
            except LifecycleError:
                if clock() >= restore_deadline:
                    raise
                sleeper(min(5.0, max(0.0, restore_deadline - clock())))
        stages["primary_restored"] = True
        restored = _host_action(plan, runner, "probe-auto", timeout=MAX_PROBE_REMOTE_SECONDS)
        if restored["details"].get("candidate") != plan.primary.candidate:
            raise LifecycleError("restored automatic selection did not return to Qwen")
        stages["auto_restored"] = True

        failure_stage = "monitor"
        monitor_deadline = clock() + monitor_seconds
        while clock() < monitor_deadline:
            sleeper(min(float(HEALTH_PERIOD_SECONDS), max(0.0, monitor_deadline - clock())))
            previous, values, _continuity = _sample(
                plan,
                runner,
                observed_now=wall_clock(),
                previous=previous,
                require_primary=True,
            )
            health_samples += 1
            for key, value in values.items():
                maxima[key] = max(maxima.get(key, 0), value)
        stages["monitor"] = True
        result = "passed"
        failure_stage = ""
    except BaseException:
        raise
    finally:
        try:
            if created:
                try:
                    _host_action(plan, runner, "stop-all")
                except Exception:
                    pass
                try:
                    _host_action(plan, runner, "cleanup")
                except Exception:
                    pass
                cleanup_deleted, absence = _cleanup_provider(plan, runner)
                try:
                    protected_bootstrap_running = _protected_bootstrap_running(plan, runner)
                except Exception:
                    protected_bootstrap_running = False
            stages["cleanup"] = (
                len(absence) == 6 and all(absence) and (not stages["provider_preflight"] or protected_bootstrap_running)
            )
        finally:
            if not stages["cleanup"]:
                result = "failed"
            report = {
                "schema_version": SCHEMA_VERSION,
                "scope": "gcp-public-route-lifecycle-evidence",
                "result": result,
                "run_id": plan.run_id,
                "lifecycle_source_commit": plan.source_commit,
                "provider_plan_digest": plan.provider_plan_digest,
                "route_bindings": [
                    {
                        "role": route.role,
                        "candidate": route.candidate,
                        "manifest_digest": route.manifest_digest,
                        "image_digest": route.image_reference.rsplit("@", 1)[1],
                        "publication_evidence_digest": route.evidence_digest,
                    }
                    for route in (plan.primary, plan.standby)
                ],
                "stages": stages,
                "failure_stage": failure_stage or None,
                "elapsed_seconds": round(max(0.0, clock() - started), 3),
                "health_sample_count": health_samples,
                "observed_maxima_bytes": maxima,
                "cleanup": {
                    "delete_commands_passed": cleanup_deleted,
                    "absence_checks": absence,
                    "all_absent": len(absence) == 6 and all(absence),
                },
                "protected_bootstrap_running": protected_bootstrap_running,
                "co_located_fallback_not_redundancy": True,
                "privacy": dict(_PRIVACY_FIELDS),
                "complete_release_qualification": False,
            }
            _atomic_report(output_path, report)
    return report


def _atomic_report(path: Path, value: Mapping[str, Any]) -> None:
    payload = (json.dumps(value, allow_nan=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(payload) > MAX_REPORT_BYTES:
        raise LifecycleError("lifecycle evidence exceeds its bounded size")
    path = Path(os.path.abspath(os.fspath(path.expanduser())))
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _monitor_seconds(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("monitor seconds must be an integer") from None
    if not 0 <= parsed <= 50_400:
        raise argparse.ArgumentTypeError("monitor seconds must be between zero and 50400")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Operate one exact finite Gate 11 GCP public route")
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, default=Path("docs/RELEASE_READINESS.md"))
    parser.add_argument("--primary-evidence", type=Path, required=True)
    parser.add_argument("--standby-evidence", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, default=Path("scripts/gcp_public_route_startup.sh"))
    parser.add_argument("--host-controller", type=Path, default=Path("scripts/gcp_public_route_host.py"))
    parser.add_argument("--acceptance-probe", type=Path, default=Path("scripts/public_route_acceptance.py"))
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--monitor-seconds", type=_monitor_seconds, default=0)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = load_bound_plan(
            authorization_path=args.authorization,
            ledger_path=args.ledger,
            primary_evidence_path=args.primary_evidence,
            standby_evidence_path=args.standby_evidence,
            bootstrap_path=args.bootstrap,
            host_controller_path=args.host_controller,
            acceptance_probe_path=args.acceptance_probe,
            expected_source_commit=args.source_commit,
        )
        report = execute_lifecycle(
            plan,
            host_controller_path=args.host_controller,
            acceptance_probe_path=args.acceptance_probe,
            output_path=args.output,
            monitor_seconds=args.monitor_seconds,
        )
    except LifecycleError as exc:
        print(f"public-route lifecycle failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"result": report["result"], "run_id": report["run_id"]}, sort_keys=True))
    return 0 if report["result"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
