"""Bounded local worker selection requests; paths and new identities are private."""

from __future__ import annotations

import copy
import json
import os
import re
import secrets
from pathlib import Path
from typing import Any

from drift.node.config import NodeConfigError
from drift.node.device_binding import DeviceBindingStore
from drift.node.hardware_status import MAX_VISIBLE_ACCELERATORS
from drift.node.worker_supervisor import WorkerNotFoundError

MAX_SELECTION_REQUEST_BYTES = 8192
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def validate_selection_request(request: Any) -> dict:
    if not isinstance(request, dict):
        raise NodeConfigError("worker selection must be a JSON object")
    operation = request.get("operation")
    if not isinstance(operation, str) or operation not in ("add", "remove", "reselect"):
        raise NodeConfigError("unsupported worker selection operation")
    fields = {"schema_version", "expected_config_revision", "operation"}
    if operation != "add":
        fields.add("worker_id")
    if operation != "remove":
        fields.add("device")
    if set(request) != fields:
        raise NodeConfigError("worker selection has missing or unknown fields")
    if type(request["schema_version"]) is not int or request["schema_version"] != 1:
        raise NodeConfigError("unsupported worker selection schema")
    revision = request["expected_config_revision"]
    if not isinstance(revision, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is None:
        raise NodeConfigError("worker selection has an invalid config revision")
    if operation != "add":
        worker_id = request["worker_id"]
        if not isinstance(worker_id, str) or _WORKER_ID.fullmatch(worker_id) is None:
            raise NodeConfigError("worker selection has an invalid worker ID")
    if operation != "remove":
        device = request["device"]
        if (
            not isinstance(device, str)
            or re.fullmatch(r"cuda:(0|[1-9][0-9]?)", device) is None
            or int(device.split(":")[1]) >= MAX_VISIBLE_ACCELERATORS
        ):
            raise NodeConfigError("worker selection currently supports visible CUDA cards only")
    return dict(request)


def parse_selection_request(payload: bytes) -> dict:
    if len(payload) > MAX_SELECTION_REQUEST_BYTES:
        raise NodeConfigError("worker selection exceeds the size limit")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise NodeConfigError("worker selection contains duplicate fields")
            result[key] = value
        return result

    def reject_constant(value):
        raise NodeConfigError("worker selection contains a non-finite number")

    try:
        request = json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise NodeConfigError("worker selection is invalid JSON") from exc
    return validate_selection_request(request)


def _new_identity(base_dir: Path, workers: list) -> tuple[str, str]:
    directory = base_dir / "worker-identities"
    # Check the lexical path before NodeConfig resolves it. Otherwise an
    # existing junction could silently move the generated identity out of its
    # managed profile before private binding validation sees the path.
    for path in (directory, *directory.parents):
        if path.is_symlink() or bool(getattr(os.path, "isjunction", lambda value: False)(path)):
            raise NodeConfigError("managed worker identities require an unlinked directory")
    if directory.exists() and not directory.is_dir():
        raise NodeConfigError("managed worker identities require a directory")
    existing = {worker["id"].casefold() for worker in workers}
    for _ in range(8):
        worker_id = "worker-" + secrets.token_hex(16)
        relative = f"worker-identities/{worker_id}.key"
        identity = base_dir / relative
        binding_dir = identity.with_name(f".{identity.name}.device-binding")
        # Retired private pins are deliberately retained and never reused.
        if worker_id.casefold() not in existing and not os.path.lexists(identity) and not os.path.lexists(binding_dir):
            return worker_id, relative
    raise NodeConfigError("could not allocate a fresh worker identity")


def candidate_selection(document: dict, request: dict, *, base_dir: Path) -> tuple[dict, str, str | None]:
    """Build a paused config without accepting arbitrary worker configuration.

    First-card creation is automatic. Additional model/span allocation remains
    a separate contract; reselect preserves an existing manual assignment.
    """
    request = validate_selection_request(request)
    result = copy.deepcopy(document)
    workers = result.setdefault("workers", [])
    operation = request["operation"]
    if len(workers) > MAX_VISIBLE_ACCELERATORS:
        raise NodeConfigError("worker selection exceeds the supported worker limit")
    if operation == "add":
        if workers:
            raise NodeConfigError("additional GPU allocation is not configured; reselect an existing worker")
        worker = {"model": "auto", "num_blocks": 1}
        workers.append(worker)
    else:
        worker = next((item for item in workers if item["id"].casefold() == request["worker_id"].casefold()), None)
        if worker is None:
            raise WorkerNotFoundError("selected contribution worker was not found")
    if operation == "remove":
        worker_id = worker["id"]
        workers.remove(worker)
        device = None
    else:
        # Allocate against the original list (the new auto template has no ID).
        worker_id, relative = _new_identity(base_dir, document.get("workers", []))
        device = request["device"]
        worker.update(id=worker_id, identity_path=relative, device=device)
    for item in workers:
        item["enabled"] = False
    return result, worker_id, device


def enroll_selection(config, worker_id: str) -> None:
    """Pin the chosen physical card before the durable configuration commit."""
    worker = next(item for item in config.workers if item.worker_id == worker_id)
    DeviceBindingStore(worker.identity_path.with_name(f".{worker.identity_path.name}.device-binding")).bind(
        worker.worker_id, worker.device
    )
