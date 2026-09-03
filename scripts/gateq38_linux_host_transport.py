"""Authenticated per-instance status envelopes for the Qwen3.8 Linux hosts.

Guest attributes or another host-to-controller carrier are transport bytes, not a
trust root.  This module authenticates a strict status envelope with a
controller-generated, per-instance key and binds it to the exact provider
generation and route plan.  It never invokes a provider or starts a paid host.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any, Mapping, Sequence

from scripts import gateq38_route_controller as controller

SCHEMA_VERSION = 1
CONTEXT_SCOPE = "qwen3.8-linux-instance-context"
ENVELOPE_SCOPE = "qwen3.8-linux-host-status"
KEY_BYTES = 32
MAX_CONTEXT_SECONDS = controller.EXPECTED_MAX_LIFETIME_SECONDS
MAX_STATUS_AGE_SECONDS = 300
MAX_FUTURE_SKEW_SECONDS = 30
MAX_ENVELOPE_BYTES = 65_536
MAX_REVISION = 2**63 - 1

_CONTEXT_FIELDS = {
    "schema_version",
    "scope",
    "run_id",
    "source_commit",
    "plan_digest",
    "execution_inventory_digest",
    "worker_plan_digest",
    "start_action_id",
    "collect_action_id",
    "project",
    "zone",
    "resource_name",
    "resource_kind",
    "role",
    "worker_id",
    "instance_id",
    "creation_timestamp",
    "instance_generation_digest",
    "issued_at_unix",
    "expires_at_unix",
    "context_digest",
    "context_hmac",
}
_ENVELOPE_FIELDS = {
    "schema_version",
    "scope",
    "context",
    "boot_id",
    "revision",
    "published_at_unix",
    "prepared_record_digest",
    "payload",
    "payload_digest",
    "envelope_hmac",
}
_HMAC_RE = re.compile(r"hmac-sha256:[0-9a-f]{64}")
_BOOT_ID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")


class Q38LinuxHostTransportError(RuntimeError):
    """An instance context or authenticated host status failed closed."""


def _unique_object(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise Q38LinuxHostTransportError("duplicate transport JSON field")
        value[key] = item
    return value


def _reject_constant(_value: str) -> None:
    raise Q38LinuxHostTransportError("non-finite transport JSON value")


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise Q38LinuxHostTransportError("transport value is not canonical") from exc


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _key(value: bytes | bytearray) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != KEY_BYTES:
        raise Q38LinuxHostTransportError("transport key is invalid")
    return bytes(value)


def _mac(domain: bytes, value: Any, key: bytes | bytearray) -> str:
    return "hmac-sha256:" + hmac.new(_key(key), domain + b"\0" + _canonical(value), hashlib.sha256).hexdigest()


def _integer(value: Any, label: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise Q38LinuxHostTransportError(f"{label} is invalid")
    return value


def _digest(value: Any, label: str) -> str:
    if not isinstance(value, str) or controller._DIGEST_RE.fullmatch(value) is None:
        raise Q38LinuxHostTransportError(f"{label} is invalid")
    return value


def _resource(plan: controller.RoutePlan, resource_name: str) -> controller.ResourcePlan:
    if not isinstance(resource_name, str):
        raise Q38LinuxHostTransportError("instance resource name is invalid")
    resource = plan.resource_by_name.get(resource_name)
    if resource is None or not resource.kind.endswith("instance"):
        raise Q38LinuxHostTransportError("instance resource is not planned")
    return resource


def _context_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("context_hmac", None)
    return unsigned


def _context_digest_value(value: Mapping[str, Any]) -> str:
    unsigned = _context_unsigned(value)
    unsigned.pop("context_digest", None)
    return _sha256(unsigned)


def build_instance_context(
    plan: controller.RoutePlan,
    resource_name: str,
    instance_id: str,
    creation_timestamp: str,
    *,
    issued_at_unix: int,
    expires_at_unix: int,
    key: bytes | bytearray,
) -> dict[str, Any]:
    """Create the exact post-provisioning context installed root-only on one VM."""

    resource = _resource(plan, resource_name)
    issued = _integer(issued_at_unix, "context issue time")
    expires = _integer(expires_at_unix, "context expiry")
    if expires <= issued or expires - issued > MAX_CONTEXT_SECONDS or expires > plan.deadline_unix:
        raise Q38LinuxHostTransportError("instance context time window is invalid")
    try:
        generation = controller.instance_generation_digest(
            resource.name,
            instance_id,
            creation_timestamp,
        )
    except controller.RouteControllerError as exc:
        raise Q38LinuxHostTransportError("instance generation is invalid") from exc
    role = "worker" if resource.kind == "worker_instance" else "bootstrap"
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": CONTEXT_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "collect_action_id": controller._action_id(plan, "collect_route"),
        "project": controller.EXPECTED_PROJECT,
        "zone": controller.EXPECTED_ZONE,
        "resource_name": resource.name,
        "resource_kind": resource.kind,
        "role": role,
        "worker_id": resource.worker_id,
        "instance_id": instance_id,
        "creation_timestamp": creation_timestamp,
        "instance_generation_digest": generation,
        "issued_at_unix": issued,
        "expires_at_unix": expires,
        "context_digest": "",
        "context_hmac": "",
    }
    value["context_digest"] = _context_digest_value(value)
    value["context_hmac"] = _mac(b"gateq38-instance-context-v1", _context_unsigned(value), key)
    return value


def validate_instance_context(
    value: Any,
    plan: controller.RoutePlan,
    *,
    key: bytes | bytearray,
    now_unix: int,
    expected_resource_name: str | None = None,
    expected_generation_digest: str | None = None,
    _allow_expired_for_cleanup: bool = False,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _CONTEXT_FIELDS:
        raise Q38LinuxHostTransportError("instance context schema is invalid")
    now = _integer(now_unix, "context verification time")
    resource = _resource(plan, value.get("resource_name"))
    role = "worker" if resource.kind == "worker_instance" else "bootstrap"
    fixed = {
        "schema_version": SCHEMA_VERSION,
        "scope": CONTEXT_SCOPE,
        "run_id": plan.run_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "execution_inventory_digest": plan.execution_inventory_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "collect_action_id": controller._action_id(plan, "collect_route"),
        "project": controller.EXPECTED_PROJECT,
        "zone": controller.EXPECTED_ZONE,
        "resource_name": resource.name,
        "resource_kind": resource.kind,
        "role": role,
        "worker_id": resource.worker_id,
    }
    if any(value[field] != expected for field, expected in fixed.items()):
        raise Q38LinuxHostTransportError("instance context plan binding is invalid")
    if expected_resource_name is not None and resource.name != expected_resource_name:
        raise Q38LinuxHostTransportError("instance context resource changed")
    issued = _integer(value["issued_at_unix"], "context issue time")
    expires = _integer(value["expires_at_unix"], "context expiry")
    if (
        expires <= issued
        or expires - issued > MAX_CONTEXT_SECONDS
        or expires > plan.deadline_unix
        or issued > now + MAX_FUTURE_SKEW_SECONDS
        or not _allow_expired_for_cleanup
        and now >= expires
    ):
        raise Q38LinuxHostTransportError("instance context is stale")
    try:
        generation = controller.instance_generation_digest(
            resource.name,
            value["instance_id"],
            value["creation_timestamp"],
        )
    except (controller.RouteControllerError, TypeError) as exc:
        raise Q38LinuxHostTransportError("instance context generation is invalid") from exc
    if generation != value["instance_generation_digest"]:
        raise Q38LinuxHostTransportError("instance context generation binding changed")
    if expected_generation_digest is not None and generation != expected_generation_digest:
        raise Q38LinuxHostTransportError("instance context uses another provider generation")
    if value["context_digest"] != _context_digest_value(value):
        raise Q38LinuxHostTransportError("instance context digest changed")
    supplied_hmac = value["context_hmac"]
    if not isinstance(supplied_hmac, str) or _HMAC_RE.fullmatch(supplied_hmac) is None:
        raise Q38LinuxHostTransportError("instance context authentication is invalid")
    expected_hmac = _mac(b"gateq38-instance-context-v1", _context_unsigned(value), key)
    if not hmac.compare_digest(supplied_hmac, expected_hmac):
        raise Q38LinuxHostTransportError("instance context authentication failed")
    return dict(value)


def encode_instance_context(value: Mapping[str, Any]) -> bytes:
    payload = _canonical(value) + b"\n"
    if not 1 <= len(payload) <= MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("instance context exceeded its size bound")
    return payload


def decode_instance_context(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("instance context transport bytes are invalid")
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise Q38LinuxHostTransportError("instance context transport framing is invalid")
    try:
        value = json.loads(
            payload[:-1].decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Q38LinuxHostTransportError("instance context transport JSON is invalid") from exc
    if not isinstance(value, dict) or payload != _canonical(value) + b"\n":
        raise Q38LinuxHostTransportError("instance context transport is not canonical")
    return value


def _worker_payload(value: Any, plan: controller.RoutePlan, worker_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != controller._OBS_WORKER_FIELDS:
        raise Q38LinuxHostTransportError("worker status payload schema is invalid")
    worker = plan.worker_by_id.get(worker_id)
    if worker is None:
        raise Q38LinuxHostTransportError("worker status identity is invalid")
    if value["state"] not in {"starting", "ready", "failed"}:
        raise Q38LinuxHostTransportError("worker status state is invalid")
    expected = {
        "machine_id": worker.machine_id,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "span": worker.span,
        "manifest_digest": plan.manifest_digest,
        "artifact_bytes": worker.artifact_bytes,
        "artifact_set_digest": worker.artifact_set_digest,
        "cache_root": worker.cache_root,
    }
    if any(value[field] != expected_item for field, expected_item in expected.items()):
        raise Q38LinuxHostTransportError("worker status plan binding is invalid")
    peer_id = value["peer_id"]
    if value["state"] == "ready":
        if not isinstance(peer_id, str) or controller._PEER_RE.fullmatch(peer_id) is None:
            raise Q38LinuxHostTransportError("ready worker status lacks an exact peer")
    elif peer_id is not None:
        raise Q38LinuxHostTransportError("unfinished worker exposed a peer identity")
    return dict(value)


def _bootstrap_payload(value: Any, plan: controller.RoutePlan) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != controller._ROUTE_JOB_FIELDS:
        raise Q38LinuxHostTransportError("route-job status payload schema is invalid")
    state = value["state"]
    if state not in {"absent", "running", "passed", "failed"}:
        raise Q38LinuxHostTransportError("route-job status state is invalid")
    if state == "absent":
        if any(item is not None for field, item in value.items() if field != "state"):
            raise Q38LinuxHostTransportError("absent route-job status exposed metadata")
        return dict(value)
    expected = {
        "job_id": plan.route_job_id,
        "collect_action_id": controller._action_id(plan, "collect_route"),
        "run_id": plan.run_id,
        "plan_digest": plan.plan_digest,
        "source_commit": plan.source_commit,
        "manifest_digest": plan.manifest_digest,
        "worker_plan_digest": plan.worker_plan_digest,
    }
    if any(value[field] != expected_item for field, expected_item in expected.items()):
        raise Q38LinuxHostTransportError("route-job status plan binding is invalid")
    if state == "passed":
        record = value["route_record"]
        evidence_digest = value["evidence_digest"]
        if not isinstance(record, dict) or evidence_digest != controller._canonical_digest(record):
            raise Q38LinuxHostTransportError("passed route-job status evidence is invalid")
    elif value["route_record"] is not None or value["evidence_digest"] is not None:
        raise Q38LinuxHostTransportError("non-passed route-job status exposed evidence")
    return dict(value)


def initial_status_payload(context: Mapping[str, Any], plan: controller.RoutePlan) -> dict[str, Any]:
    resource = _resource(plan, context.get("resource_name"))
    if context.get("resource_kind") != resource.kind or context.get("worker_id") != resource.worker_id:
        raise Q38LinuxHostTransportError("instance context resource binding is invalid")
    if resource.kind == "bootstrap_instance":
        return {field: ("absent" if field == "state" else None) for field in controller._ROUTE_JOB_FIELDS}
    if resource.worker_id is None:
        raise Q38LinuxHostTransportError("worker context lacks a worker identity")
    worker = plan.worker_by_id[resource.worker_id]
    return {
        "state": "starting",
        "machine_id": worker.machine_id,
        "peer_id": None,
        "source_commit": plan.source_commit,
        "plan_digest": plan.plan_digest,
        "worker_plan_digest": plan.worker_plan_digest,
        "start_action_id": controller._action_id(plan, "start_route"),
        "span": worker.span,
        "manifest_digest": plan.manifest_digest,
        "artifact_bytes": worker.artifact_bytes,
        "artifact_set_digest": worker.artifact_set_digest,
        "cache_root": worker.cache_root,
    }


def _payload(value: Any, context: Mapping[str, Any], plan: controller.RoutePlan) -> dict[str, Any]:
    if context["role"] == "worker":
        worker_id = context["worker_id"]
        if not isinstance(worker_id, str):
            raise Q38LinuxHostTransportError("worker context lacks a worker identity")
        return _worker_payload(value, plan, worker_id)
    if context["worker_id"] is not None:
        raise Q38LinuxHostTransportError("bootstrap context exposed a worker identity")
    return _bootstrap_payload(value, plan)


def _envelope_unsigned(value: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = dict(value)
    unsigned.pop("envelope_hmac", None)
    return unsigned


def build_status_envelope(
    context: Mapping[str, Any],
    payload: Mapping[str, Any],
    plan: controller.RoutePlan,
    *,
    key: bytes | bytearray,
    boot_id: str,
    revision: int,
    published_at_unix: int,
    prepared_record_digest: str,
) -> dict[str, Any]:
    published = _integer(published_at_unix, "status publication time")
    validated_context = validate_instance_context(context, plan, key=key, now_unix=published)
    if not isinstance(boot_id, str) or _BOOT_ID_RE.fullmatch(boot_id) is None:
        raise Q38LinuxHostTransportError("Linux boot identity is invalid")
    current_revision = _integer(revision, "status revision", minimum=1, maximum=MAX_REVISION)
    prepared = _digest(prepared_record_digest, "prepared record digest")
    validated_payload = _payload(dict(payload), validated_context, plan)
    value: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "scope": ENVELOPE_SCOPE,
        "context": validated_context,
        "boot_id": boot_id,
        "revision": current_revision,
        "published_at_unix": published,
        "prepared_record_digest": prepared,
        "payload": validated_payload,
        "payload_digest": _sha256(validated_payload),
        "envelope_hmac": "",
    }
    value["envelope_hmac"] = _mac(b"gateq38-host-status-v1", _envelope_unsigned(value), key)
    if len(_canonical(value)) + 1 > MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("host status envelope exceeded its size bound")
    return value


def validate_status_envelope(
    value: Any,
    plan: controller.RoutePlan,
    *,
    key: bytes | bytearray,
    now_unix: int,
    expected_resource_name: str,
    expected_generation_digest: str,
    minimum_revision: int = 0,
    expected_boot_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _ENVELOPE_FIELDS:
        raise Q38LinuxHostTransportError("host status envelope schema is invalid")
    now = _integer(now_unix, "status verification time")
    context = validate_instance_context(
        value["context"],
        plan,
        key=key,
        now_unix=now,
        expected_resource_name=expected_resource_name,
        expected_generation_digest=expected_generation_digest,
    )
    boot_id = value["boot_id"]
    if not isinstance(boot_id, str) or _BOOT_ID_RE.fullmatch(boot_id) is None:
        raise Q38LinuxHostTransportError("Linux boot identity is invalid")
    if expected_boot_id is not None and boot_id != expected_boot_id:
        raise Q38LinuxHostTransportError("Linux boot identity changed")
    revision = _integer(value["revision"], "status revision", minimum=1, maximum=MAX_REVISION)
    floor = _integer(minimum_revision, "minimum status revision", maximum=MAX_REVISION)
    if revision <= floor:
        raise Q38LinuxHostTransportError("host status revision is stale")
    published = _integer(value["published_at_unix"], "status publication time")
    if (
        published < context["issued_at_unix"]
        or published > now + MAX_FUTURE_SKEW_SECONDS
        or now - published > MAX_STATUS_AGE_SECONDS
    ):
        raise Q38LinuxHostTransportError("host status publication is stale")
    _digest(value["prepared_record_digest"], "prepared record digest")
    payload = _payload(value["payload"], context, plan)
    if value["payload_digest"] != _sha256(payload):
        raise Q38LinuxHostTransportError("host status payload digest changed")
    supplied_hmac = value["envelope_hmac"]
    if not isinstance(supplied_hmac, str) or _HMAC_RE.fullmatch(supplied_hmac) is None:
        raise Q38LinuxHostTransportError("host status authentication is invalid")
    expected_hmac = _mac(b"gateq38-host-status-v1", _envelope_unsigned(value), key)
    if not hmac.compare_digest(supplied_hmac, expected_hmac):
        raise Q38LinuxHostTransportError("host status authentication failed")
    if len(_canonical(value)) + 1 > MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("host status envelope exceeded its size bound")
    result = dict(value)
    result["context"] = context
    result["payload"] = json.loads(_canonical(payload).decode("ascii"))
    return result


def encode_status_envelope(value: Mapping[str, Any]) -> bytes:
    payload = _canonical(value) + b"\n"
    if not 1 <= len(payload) <= MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("host status envelope exceeded its size bound")
    return payload


def decode_status_envelope(payload: bytes) -> dict[str, Any]:
    if not isinstance(payload, bytes) or not 1 <= len(payload) <= MAX_ENVELOPE_BYTES:
        raise Q38LinuxHostTransportError("host status transport bytes are invalid")
    if not payload.endswith(b"\n") or payload.count(b"\n") != 1:
        raise Q38LinuxHostTransportError("host status transport framing is invalid")
    try:
        value = json.loads(
            payload[:-1].decode("ascii"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Q38LinuxHostTransportError("host status transport JSON is invalid") from exc
    if not isinstance(value, dict):
        raise Q38LinuxHostTransportError("host status transport JSON is invalid")
    if payload != _canonical(value) + b"\n":
        raise Q38LinuxHostTransportError("host status transport is not canonical")
    return value
