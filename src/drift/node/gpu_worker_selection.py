"""Bounded whole-set GPU drafts; persistence and physical enrollment remain external.

This module only builds paused configuration candidates. Callers must compare the
request revision with the current on-disk revision, verify every selection token,
and validate the resulting NodeConfig before committing while sharing is idle.
The trusted physical map is an early duplicate check, not enrollment evidence:
callers must also compare tokens with every actual immutable enrollment and reject
physical collisions with retained manual workers before persistence. In particular,
this helper does not relax the existing automatic-worker runtime guard.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Mapping

from drift.node.config import (
    ContributionPolicyConfig,
    NodeConfigError,
    WorkerConfig,
    _require_processing_percent,
    _require_vram_limit,
    validate_processing_configuration,
)
from drift.node.device_binding import normalize_cuda_uuid
from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS
from drift.node.worker_selection import _new_identity

MAX_GPU_SELECTION_REQUEST_BYTES = 16 * 1024
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_DEVICE = re.compile(r"cuda:(0|[1-9][0-9]?)")
_ROW_FIELDS = {"device", "selected", "max_vram", "max_processing_percent"}


def _valid_device(value: Any) -> bool:
    return (
        isinstance(value, str)
        and _DEVICE.fullmatch(value) is not None
        and int(value.split(":")[1]) < MAX_VISIBLE_ACCELERATORS
    )


def validate_gpu_selection_request(request: Any) -> dict:
    """Validate a complete draft without interpreting client-controlled config."""
    if not isinstance(request, dict) or set(request) != {"schema_version", "expected_config_revision", "rows"}:
        raise NodeConfigError("GPU selection has missing or unknown fields")
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise NodeConfigError("unsupported GPU selection schema")
    revision = request["expected_config_revision"]
    if not isinstance(revision, str) or _DIGEST.fullmatch(revision) is None:
        raise NodeConfigError("GPU selection has an invalid config revision")
    rows = request["rows"]
    if not isinstance(rows, list) or len(rows) > MAX_VISIBLE_ACCELERATORS:
        raise NodeConfigError("GPU selection exceeds the supported card limit")
    validated, devices = [], set()
    for row in rows:
        if not isinstance(row, dict) or type(row.get("selected")) is not bool:
            raise NodeConfigError("GPU selection rows require an explicit selection")
        fields = _ROW_FIELDS | ({"selection_token"} if row["selected"] else set())
        if set(row) != fields:
            raise NodeConfigError("GPU selection row has missing or unknown fields")
        device = row["device"]
        if not _valid_device(device) or device in devices:
            raise NodeConfigError("GPU selection requires unique visible CUDA cards")
        devices.add(device)
        raw = row["max_vram"]
        if not isinstance(raw, str) or len(raw) > 64:
            raise NodeConfigError("GPU selection has an invalid memory limit")
        _require_vram_limit(raw, "GPU selection max_vram")
        _require_processing_percent(row["max_processing_percent"], "GPU selection max_processing_percent")
        if row["selected"]:
            token = row["selection_token"]
            if not isinstance(token, str) or _DIGEST.fullmatch(token) is None:
                raise NodeConfigError("GPU selection has an invalid device token")
        validated.append(dict(row))
    return {"schema_version": 1, "expected_config_revision": revision, "rows": validated}


def parse_gpu_selection_request(payload: bytes) -> dict:
    """Decode bounded UTF-8 JSON, rejecting duplicate fields and nonfinite values."""
    if not isinstance(payload, bytes) or len(payload) > MAX_GPU_SELECTION_REQUEST_BYTES:
        raise NodeConfigError("GPU selection exceeds the size limit")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NodeConfigError("GPU selection contains duplicate fields")
            result[key] = value
        return result

    def reject_constant(value):
        raise NodeConfigError("GPU selection contains a non-finite number")

    try:
        request = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise NodeConfigError("GPU selection is invalid JSON") from exc
    return validate_gpu_selection_request(request)


def _validate_physical_cards(selected: dict, physical_devices: Mapping[str, str] | None) -> None:
    if physical_devices is None and not selected:
        return
    if not isinstance(physical_devices, Mapping) or len(physical_devices) > MAX_VISIBLE_ACCELERATORS:
        raise NodeConfigError("GPU selection requires verified physical cards")
    identities = set()
    for device in selected:
        physical = normalize_cuda_uuid(physical_devices.get(device))
        if physical is None or physical in identities:
            raise NodeConfigError("GPU selection requires distinct verified physical cards")
        identities.add(physical)


def candidate_gpu_selection(
    document: dict,
    request: dict,
    *,
    base_dir: Path,
    physical_devices: Mapping[str, str] | None = None,
) -> tuple[dict, list[dict], bool]:
    """Return a detached paused candidate, private enrollment plan and changed flag.

    Omitted and unchecked cards remove only workers explicitly marked desktop_gpu.
    Retained cards keep their immutable identities; changing the physical card
    therefore requires a separate removal and later new selection. A new worker's
    auto/one-block fields are the current schema placeholder, not a sizing result.
    No identity files, private pins, configuration or runtime state are written.
    The enrollment plan is internal; it is not a public response schema.
    """
    request = validate_gpu_selection_request(request)
    selected = {row["device"]: row for row in request["rows"] if row["selected"]}
    _validate_physical_cards(selected, physical_devices)
    if not isinstance(document, dict):
        raise NodeConfigError("GPU selection requires a node configuration")
    workers = document.get("workers", [])
    if not isinstance(workers, list) or len(workers) > MAX_VISIBLE_ACCELERATORS:
        raise NodeConfigError("GPU selection exceeds the supported worker limit")
    parsed_workers = tuple(
        WorkerConfig.from_dict(worker, base_dir=base_dir, index=index) for index, worker in enumerate(workers)
    )
    if len({worker.worker_id.casefold() for worker in parsed_workers}) != len(workers):
        raise NodeConfigError("GPU selection requires unique existing worker IDs")
    source_policy = document.get("contribution_policy")
    policy = ContributionPolicyConfig() if source_policy is None else ContributionPolicyConfig.from_dict(source_policy)
    validate_processing_configuration(policy, parsed_workers)

    managed, manual = {}, []
    for worker in workers:
        if worker.get("managed_by") != "desktop_gpu":
            if worker["model"].casefold() == "auto":
                raise NodeConfigError("legacy automatic worker ownership must be resolved before editing GPUs")
            device = worker.get("device")
            if device not in {"cpu", "cuda"} and not _valid_device(device):
                raise NodeConfigError("manual worker devices must be explicit and supported before editing GPUs")
            manual.append(worker)
            continue
        device = worker.get("device")
        if not _valid_device(device) or device in managed:
            raise NodeConfigError("managed GPU worker ownership is ambiguous")
        managed[device] = worker
    if len(manual) + len(selected) > MAX_VISIBLE_ACCELERATORS:
        raise NodeConfigError("GPU selection exceeds the supported worker limit")
    if selected and manual and policy.processing_scope != "per_device":
        raise NodeConfigError("manual worker processing scope must be migrated before editing GPUs")
    # Runtime enrollment also rejects different ordinals resolving to one manual
    # physical card. Here reject obvious ordinal aliases without probing hardware.
    manual_devices = {"cuda:0" if worker.get("device") == "cuda" else worker.get("device") for worker in manual}
    if selected.keys() & manual_devices:
        raise NodeConfigError("GPU selection conflicts with a manually configured worker")

    result = copy.deepcopy(document)
    saved = []
    enrollment_by_device = {}
    for worker in result.get("workers", []):
        if worker.get("managed_by") == "desktop_gpu":
            device = worker["device"]
            if device not in selected:
                continue
            row = selected[device]
            worker.update(max_vram=row["max_vram"], max_processing_percent=row["max_processing_percent"])
            enrollment_by_device[device] = {
                "worker_id": worker["id"],
                "device": device,
                "selection_token": row["selection_token"],
            }
        worker["enabled"] = False
        saved.append(worker)

    # Include removed IDs as well as IDs generated in this candidate: a retired
    # identity is never reused, even before any private files have been created.
    occupied = list(workers)
    for device in sorted(selected.keys() - managed.keys(), key=lambda value: int(value.split(":")[1])):
        row = selected[device]
        worker_id, relative = _new_identity(base_dir, occupied)
        worker = {
            "id": worker_id,
            "model": "auto",
            "num_blocks": 1,
            "identity_path": relative,
            "device": device,
            "managed_by": "desktop_gpu",
            "max_vram": row["max_vram"],
            "max_processing_percent": row["max_processing_percent"],
            "enabled": False,
        }
        occupied.append(worker)
        saved.append(worker)
        enrollment_by_device[device] = {
            "worker_id": worker_id,
            "device": device,
            "selection_token": row["selection_token"],
        }
    if "workers" in result or saved:
        result["workers"] = saved
    if selected or source_policy is not None:
        updated_policy = result.setdefault("contribution_policy", {})
        if updated_policy is None:
            updated_policy = result["contribution_policy"] = {}
        updated_policy["sharing_enabled"] = False
        if selected:
            updated_policy["processing_scope"] = "per_device"
    enrollments = [
        enrollment_by_device[device]
        for device in sorted(enrollment_by_device, key=lambda value: int(value.split(":")[1]))
    ]
    return result, enrollments, result != document
