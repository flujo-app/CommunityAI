"""Strict localhost client for the versioned CommunityAI node control API."""

from __future__ import annotations

import ipaddress
import json
import math
import re
import socket
from typing import Any, Dict, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from communityai_desktop.telemetry import download_view

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SUPPORTED_CONTROL_API_VERSION = 1
CONTRIBUTION_STATUS_SCHEMA_VERSION = 3
CONTRIBUTION_POLICY_SCHEMA_VERSION = 1
MODEL_DOWNLOAD_SCHEMA_VERSION = 1
MAX_SELECTED_WHOLE_SHARD_BYTES = 64 * 1024**4
MAX_GPU_INVENTORY_DEVICES = 16
MAX_HARDWARE_BYTES = 2**63 - 1
MAX_GPU_VISIBLE_COUNT = 2**31 - 1
_GPU_REVISION = re.compile(r"sha256:[0-9a-f]{64}\Z")
_GPU_MEMORY = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(b|bytes?|[kmgtpe]i?b|(?:kilo|mega|giga|tera|peta|exa)bytes?)?", re.I
)


def _valid_gpu_memory(value):
    if not isinstance(value, str) or len(value) > 64 or any(ord(char) < 32 for char in value):
        return False
    if value.endswith("%"):
        try:
            percent = float(value[:-1].strip())
        except ValueError:
            return False
        return math.isfinite(percent) and 0 < percent <= 100
    match = _GPU_MEMORY.fullmatch(value.strip())
    return match is not None and 0 < float(match[1]) < float("inf")


def _gpu_rows(value: Any, *, request: bool = False) -> list[Dict[str, Any]]:
    if not isinstance(value, list) or len(value) > MAX_GPU_INVENTORY_DEVICES:
        raise NodeClientError("GPU selection has an invalid card list")
    rows, seen = [], set()
    fields = {"device", "selected", "max_vram", "max_processing_percent"}
    for row in value:
        if not isinstance(row, dict):
            raise NodeClientError("GPU selection has an invalid card")
        expected = fields | ({"selection_token"} if request and row.get("selected") is True else set())
        if set(row) != expected:
            raise NodeClientError("GPU selection has unsupported card fields")
        device = _public_device(row.get("device"), "GPU selection device", accelerator=True)
        memory, compute = row.get("max_vram"), row.get("max_processing_percent")
        if (
            not isinstance(device, str)
            or not device.startswith("cuda:")
            or device in seen
            or type(row.get("selected")) is not bool
            or not _valid_gpu_memory(memory)
            or isinstance(compute, bool)
            or not isinstance(compute, (int, float))
            or not 1 <= compute <= 100
        ):
            raise NodeClientError("GPU selection has invalid limits or duplicate cards")
        if (
            request
            and row["selected"]
            and (not isinstance(row["selection_token"], str) or not _GPU_REVISION.fullmatch(row["selection_token"]))
        ):
            raise NodeClientError("GPU selection requires its original card token")
        seen.add(device)
        rows.append(dict(row))
    return rows


def _normalize_gpu_selection(value: Any) -> Dict[str, Any]:
    required = {
        "schema_version",
        "config_revision",
        "editable",
        "inventory",
        "rows",
        "restart_required",
        "runtime_ready",
    }
    if (
        not isinstance(value, dict)
        or not required <= set(value)
        or set(value) - required - {"reason"}
        or type(value.get("schema_version")) is not int
    ):
        raise NodeClientError("Local node GPU selection response is malformed")
    revision = value.get("config_revision")
    if value["schema_version"] != 1 or not isinstance(revision, str) or not _GPU_REVISION.fullmatch(revision):
        raise NodeClientError("Local node GPU selection version or revision is invalid")
    if any(type(value.get(field)) is not bool for field in ("editable", "restart_required", "runtime_ready")):
        raise NodeClientError("Local node GPU selection capabilities are invalid")
    if value["restart_required"] and (value["editable"] or value["runtime_ready"]):
        raise NodeClientError("Local node GPU selection reload state is inconsistent")
    reason = value.get("reason", "")
    if not isinstance(reason, str) or len(reason) > 500 or any(ord(char) < 32 for char in reason):
        raise NodeClientError("Local node GPU selection reason is invalid")
    inventory = value["inventory"]
    if not isinstance(inventory, list) or len(inventory) > MAX_GPU_INVENTORY_DEVICES:
        raise NodeClientError("Local node GPU selection inventory is invalid")
    clean_inventory, seen = [], set()
    for card in inventory:
        if not isinstance(card, dict):
            raise NodeClientError("Local node GPU selection card is invalid")
        device = _public_device(card.get("device"), "GPU selection inventory", accelerator=True)
        status, token = card.get("status"), card.get("selection_token")
        total = _hardware_bytes(card.get("total_bytes"), "GPU capacity", positive=True)
        name = card.get("name")
        if (
            device is None
            or device in seen
            or status not in ("available", "unavailable", "unsupported")
            or (status == "available" and total is None)
            or not isinstance(name, str)
            or len(name) > 160
            or any(ord(char) < 32 for char in name)
            or (
                token is not None
                and (
                    not isinstance(token, str)
                    or not _GPU_REVISION.fullmatch(token)
                    or not device.startswith("cuda:")
                    or status != "available"
                )
            )
        ):
            raise NodeClientError("Local node GPU selection card is invalid")
        seen.add(device)
        clean = {"device": device, "name": name, "total_bytes": total, "status": status}
        if token is not None:
            clean["selection_token"] = token
        clean_inventory.append(clean)
    rows = _gpu_rows(value["rows"])
    if len(seen | {row["device"] for row in rows}) > MAX_GPU_INVENTORY_DEVICES:
        raise NodeClientError("Local node GPU selection exceeds the card limit")
    return {**value, "inventory": clean_inventory, "rows": rows, "reason": reason}


class NodeClientError(RuntimeError):
    """The local node could not be reached or returned an invalid response."""


class NodeApiError(NodeClientError):
    """The local node returned an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(f"Local node returned HTTP {status_code}: {detail}")
        self.status_code = status_code
        self.detail = detail


class _RejectRedirects(HTTPRedirectHandler):
    """Never forward the privileged Authorization header through a redirect."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):  # noqa: ANN001
        return None


def normalize_loopback_url(value: str) -> str:
    """Normalize a node URL and reject anything that could exfiltrate its credential."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("node URL must be a non-empty string")
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https"):
        raise ValueError("node URL scheme must be http or https")
    if not parsed.hostname:
        raise ValueError("node URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("node URL must not include user information")
    if parsed.query or parsed.fragment:
        raise ValueError("node URL must not include a query or fragment")

    hostname = parsed.hostname.rstrip(".").casefold()
    if hostname != "localhost":
        try:
            if not ipaddress.ip_address(hostname).is_loopback:
                raise ValueError("node URL must use a loopback address")
        except ValueError as exc:
            if str(exc) == "node URL must use a loopback address":
                raise
            raise ValueError("node URL must use localhost or a literal loopback address") from exc

    path = parsed.path.rstrip("/")
    if path == "/v1":
        path = ""
    if path:
        raise ValueError("node URL path must be empty or /v1")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("node URL has an invalid port") from exc
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _normalize_model_download(value: Any) -> Dict[str, Any]:
    expected_keys = {"schema_version", "selected_whole_shard_bytes"}
    if not isinstance(value, dict) or set(value) - {"progress"} != expected_keys:
        raise NodeClientError("Local node model download estimate has an invalid schema")
    if type(value["schema_version"]) is not int or value["schema_version"] != MODEL_DOWNLOAD_SCHEMA_VERSION:
        raise NodeClientError("Local node model download estimate has an unsupported schema version")
    size = value["selected_whole_shard_bytes"]
    if size is not None and (
        isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_SELECTED_WHOLE_SHARD_BYTES
    ):
        raise NodeClientError("Local node model download estimate has invalid selected whole-shard bytes")
    return {
        "schema_version": MODEL_DOWNLOAD_SCHEMA_VERSION,
        "selected_whole_shard_bytes": size,
        **({"progress": download_view(value["progress"])} if "progress" in value else {}),
    }


def _bounded_status_text(value: Any, field: str, *, limit: int) -> str:
    if not isinstance(value, str):
        raise NodeClientError(f"Local node contribution status has invalid {field}")
    normalized = " ".join(value.split())
    if not normalized or not normalized.isprintable() or len(normalized) > limit:
        raise NodeClientError(f"Local node contribution status has invalid {field}")
    return normalized


def _optional_number(value: Any, field: str, *, integer: bool = False, positive: bool = False):
    if value is None:
        return None
    expected_type = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, expected_type):
        raise NodeClientError(f"Local node contribution status has invalid {field}")
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        raise NodeClientError(f"Local node contribution status has invalid {field}")
    return value


def _hardware_bytes(value: Any, field: str, *, positive: bool = False):
    if value is not None and (type(value) is not int or not (1 if positive else 0) <= value <= MAX_HARDWARE_BYTES):
        raise NodeClientError(f"Local node status has invalid {field}")
    return value


def _public_device(value: Any, field: str, *, accelerator: bool = False):
    if value is None:
        return None
    if isinstance(value, str) and (
        value == "mps"
        or (value == "cpu" and not accelerator)
        or re.fullmatch(r"(?:cuda|xpu):(?:[0-9]|1[0-5])", value) is not None
    ):
        return value
    raise NodeClientError(f"Local node status has invalid {field}")


def _worker_device(worker: dict) -> dict:
    return {"device": _public_device(worker["device"], "worker device")} if "device" in worker else {}


def _normalize_auto_selection(value: Any) -> Dict[str, Any]:
    if value is None:
        return {
            "selector": "auto",
            "status": "not_configured",
            "model": None,
            "manifest_digest": None,
            "reason": "This node does not publish automatic model selection.",
            "covered_blocks": None,
            "total_blocks": None,
            "peer_count": None,
            "source": None,
        }
    if not isinstance(value, dict) or value.get("selector") != "auto":
        raise NodeClientError("Local node status has invalid auto selection")
    status = value.get("status")
    if status not in {"selected", "unavailable", "not_configured"}:
        raise NodeClientError("Local node status has invalid auto selection state")
    reason = _bounded_status_text(value.get("reason"), "auto selection reason", limit=600)
    if status == "selected":
        model = _bounded_status_text(value.get("model"), "auto selection model", limit=256)
        digest = value.get("manifest_digest")
        if not isinstance(digest, str) or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None:
            raise NodeClientError("Local node status has invalid auto selection manifest")
        covered = _optional_number(value.get("covered_blocks"), "auto covered blocks", integer=True, positive=True)
        total = _optional_number(value.get("total_blocks"), "auto total blocks", integer=True, positive=True)
        local = value.get("source") == "local"
        peers = _optional_number(value.get("peer_count"), "auto peer count", integer=True, positive=not local)
        if local and peers != 0:
            raise NodeClientError("Standalone inference must not claim remote peers")
        if covered is None or total is None or peers is None:
            raise NodeClientError("Local node status omitted automatic route evidence")
        if covered != total:
            raise NodeClientError("Local node status has inconsistent auto route coverage")
        source_value = value.get("source")
        source = None if source_value is None else _bounded_status_text(source_value, "auto selection source", limit=64)
    else:
        model = digest = covered = total = peers = source = None
        if any(
            value.get(field) is not None
            for field in ("model", "manifest_digest", "covered_blocks", "total_blocks", "peer_count", "source")
        ):
            raise NodeClientError("Local node status has inconsistent auto selection")
    return {
        "selector": "auto",
        "status": status,
        "model": model,
        "manifest_digest": digest,
        "reason": reason,
        "covered_blocks": covered,
        "total_blocks": total,
        "peer_count": peers,
        "source": source,
    }


def _normalize_gate(value: Any, field: str, *, suspended: bool = False) -> Dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("admitted"), bool):
        raise NodeClientError(f"Local node contribution status has invalid {field}")
    admitted = value["admitted"]
    reason = value.get("reason")
    if admitted:
        if reason is not None:
            raise NodeClientError(f"Local node contribution status has inconsistent {field}")
    else:
        reason = _bounded_status_text(reason, f"{field} reason", limit=300)
    result = {"admitted": admitted, "reason": reason}
    if suspended:
        if not isinstance(value.get("suspended"), bool):
            raise NodeClientError(f"Local node contribution status has invalid {field} suspension")
        result["suspended"] = value["suspended"]
    return result


def _normalize_model_selectors(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise NodeClientError(f"Local node contribution policy has invalid {field}")
    result = []
    for selector in value:
        if not isinstance(selector, str):
            raise NodeClientError(f"Local node contribution policy has invalid {field}")
        if not selector.strip() or not selector.isprintable() or len(selector) > 256:
            raise NodeClientError(f"Local node contribution policy has invalid {field}")
        result.append(selector)
    if len({selector.casefold() for selector in result}) != len(result):
        raise NodeClientError(f"Local node contribution policy has duplicate {field}")
    return result


def _normalize_policy(value: Any) -> Dict[str, Any]:
    fields = {
        "sharing_enabled",
        "allowed_models",
        "preferred_models",
        "denied_models",
        "max_disk_space",
        "max_vram",
        "max_bandwidth_mbps",
        "max_power_watts",
        "pause_timeout",
        "schedule",
    }
    if (
        not isinstance(value, dict)
        or not fields <= set(value) <= fields | {"max_processing_percent", "processing_scope", "max_host_memory"}
        or not isinstance(value["sharing_enabled"], bool)
    ):
        raise NodeClientError("Local node contribution policy is malformed")
    processing = {}
    if "processing_scope" in value:
        scope = value["processing_scope"]
        if not isinstance(scope, str) or scope not in ("node", "per_device"):
            raise NodeClientError("Local node has invalid processing scope")
        processing["processing_scope"] = scope
    if "max_processing_percent" in value:
        percent = _optional_number(value["max_processing_percent"], "processing percentage", positive=True)
        if percent is None or not 1 <= percent <= 100:
            raise NodeClientError("Local node has invalid processing percentage")
        processing["max_processing_percent"] = percent
    allowed = _normalize_model_selectors(value["allowed_models"], "allowed models")
    preferred = _normalize_model_selectors(value["preferred_models"], "preferred models")
    denied = _normalize_model_selectors(value["denied_models"], "denied models")

    def optional_text(field: str):
        item = value[field]
        if item is None:
            return None
        if not isinstance(item, str) or not item.strip() or not item.isprintable() or len(item) > 64:
            raise NodeClientError(f"Local node contribution policy has invalid {field.replace('_', ' ')}")
        return item

    max_disk_space = optional_text("max_disk_space")
    max_vram = optional_text("max_vram")
    if value["sharing_enabled"] and max_disk_space is None:
        raise NodeClientError("Local node contribution policy enables sharing without a storage ceiling")
    bandwidth = _optional_number(value["max_bandwidth_mbps"], "bandwidth limit", positive=True)
    power = _optional_number(value["max_power_watts"], "power limit", positive=True)
    pause_timeout = _optional_number(value["pause_timeout"], "pause timeout", positive=True)
    if pause_timeout is None:
        raise NodeClientError("Local node contribution policy has invalid pause timeout")

    schedule = value["schedule"]
    clean_schedule = None
    if schedule is not None:
        if not isinstance(schedule, dict) or set(schedule) != {"timezone", "windows"}:
            raise NodeClientError("Local node contribution policy has invalid schedule")
        timezone = schedule["timezone"]
        if not isinstance(timezone, str) or not timezone.strip() or not timezone.isprintable() or len(timezone) > 128:
            raise NodeClientError("Local node contribution policy has invalid schedule timezone")
        windows = schedule["windows"]
        if not isinstance(windows, list) or not windows or len(windows) > 64:
            raise NodeClientError("Local node contribution policy has invalid schedule windows")
        clean_windows = []
        for window in windows:
            if not isinstance(window, dict) or set(window) != {"days", "start", "end"}:
                raise NodeClientError("Local node contribution policy has invalid schedule window")
            days = window["days"]
            if (
                not isinstance(days, list)
                or not days
                or any(day not in ("mon", "tue", "wed", "thu", "fri", "sat", "sun") for day in days)
                or len(set(days)) != len(days)
            ):
                raise NodeClientError("Local node contribution policy has invalid schedule days")
            start, end = window["start"], window["end"]
            if (
                not isinstance(start, str)
                or not isinstance(end, str)
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", start) is None
                or re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", end) is None
                or start == end
            ):
                raise NodeClientError("Local node contribution policy has invalid schedule time")
            clean_windows.append({"days": list(days), "start": start, "end": end})
        clean_schedule = {"timezone": timezone, "windows": clean_windows}
    return {
        "sharing_enabled": value["sharing_enabled"],
        **processing,
        "allowed_models": allowed,
        "preferred_models": preferred,
        "denied_models": denied,
        "max_disk_space": max_disk_space,
        **({"max_host_memory": optional_text("max_host_memory")} if "max_host_memory" in value else {}),
        "max_vram": max_vram,
        "max_bandwidth_mbps": bandwidth,
        "max_power_watts": power,
        "pause_timeout": pause_timeout,
        "schedule": clean_schedule,
    }


def _normalize_policy_snapshot(value: Any, *, require_revision: bool) -> Dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "config_revision", "policy"}:
        raise NodeClientError("Local node contribution policy response is malformed")
    if value["schema_version"] != CONTRIBUTION_POLICY_SCHEMA_VERSION:
        raise NodeClientError("Local node contribution policy schema is unsupported")
    revision = value["config_revision"]
    valid_revision = isinstance(revision, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", revision) is not None
    if (require_revision and not valid_revision) or (
        not require_revision and revision is not None and not valid_revision
    ):
        raise NodeClientError("Local node contribution policy revision is invalid")
    return {
        "schema_version": CONTRIBUTION_POLICY_SCHEMA_VERSION,
        "config_revision": revision,
        "policy": _normalize_policy(value["policy"]),
    }


def _normalize_placement(value: Any) -> Dict[str, Any]:
    fields = {"automatic", "block_indices", "reason"}
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value["automatic"], bool):
        raise NodeClientError("Local node contribution status has invalid placement")
    automatic = value["automatic"]
    if automatic:
        block_indices = _bounded_status_text(value["block_indices"], "placement blocks", limit=64)
        reason = _bounded_status_text(value["reason"], "placement reason", limit=300)
    else:
        block_indices = value["block_indices"]
        reason = value["reason"]
        if block_indices is not None or reason is not None:
            raise NodeClientError("Local node contribution status has inconsistent placement")
    return {"automatic": automatic, "block_indices": block_indices, "reason": reason}


def _normalize_resource_recovery(value: Any) -> Dict[str, Any]:
    reasons = {
        "checking": ("checking",),
        "ready": ("none",),
        "blocked": ("active_owner", "cleanup_pending", "legacy_state", "unverifiable_state", "unsupported_platform"),
    }
    if (
        not isinstance(value, dict)
        or set(value) != {"state", "reason", "retryable"}
        or not isinstance(value["state"], str)
        or value["state"] not in reasons
        or not isinstance(value["reason"], str)
        or value["reason"] not in reasons[value["state"]]
        or type(value["retryable"]) is not bool
    ):
        raise NodeClientError("Local node contribution recovery status is invalid")
    return {"state": value["state"], "reason": value["reason"], "retryable": value["retryable"]}


def _normalize_contribution_status(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != CONTRIBUTION_STATUS_SCHEMA_VERSION:
        raise NodeClientError("Local node status has an unsupported contribution schema")
    configured = value.get("configured")
    editable = value.get("editable")
    workers = value.get("workers")
    if not isinstance(configured, bool) or not isinstance(editable, bool) or not isinstance(workers, list):
        raise NodeClientError("Local node contribution status is malformed")
    policy_snapshot = _normalize_policy_snapshot(value.get("policy"), require_revision=editable)
    normalized_workers = []
    for worker in workers:
        if not isinstance(worker, dict):
            raise NodeClientError("Local node contribution status has invalid worker data")
        worker_id = _bounded_status_text(worker.get("id"), "worker id", limit=128)
        model = _bounded_status_text(worker.get("model"), "model id", limit=256)
        state = worker.get("state")
        if state not in ("paused", "starting", "running", "stopping", "crashed", "unknown") or not isinstance(
            worker.get("desired_running"), bool
        ):
            raise NodeClientError("Local node contribution status has invalid worker identity or state")
        placement = _normalize_placement(worker.get("placement"))
        policy = _normalize_gate(worker.get("policy"), "policy")
        if not isinstance(worker["policy"].get("preferred"), bool):
            raise NodeClientError("Local node contribution status has invalid model preference")
        policy["preferred"] = worker["policy"]["preferred"]
        schedule = _normalize_gate(worker.get("schedule"), "schedule", suspended=True)
        resources_value = worker.get("resources")
        if not isinstance(resources_value, dict):
            raise NodeClientError("Local node contribution status has invalid resources")
        resources = _normalize_gate(resources_value, "resource", suspended=True)
        limits, measurements = resources_value.get("limits"), resources_value.get("measurements")
        if not isinstance(limits, dict) or not isinstance(measurements, dict):
            raise NodeClientError("Local node contribution status has invalid resource values")
        clean_limits = {
            "disk_bytes": _hardware_bytes(limits.get("disk_bytes"), "disk limit", positive=True),
            "vram_bytes": _hardware_bytes(limits.get("vram_bytes"), "VRAM limit", positive=True),
            "vram_pool_bytes": _hardware_bytes(limits.get("vram_pool_bytes"), "VRAM pool", positive=True),
            "bandwidth_mbps": _optional_number(limits.get("bandwidth_mbps"), "bandwidth limit", positive=True),
            "power_watts": _optional_number(limits.get("power_watts"), "power limit", positive=True),
        }
        if (clean_limits["vram_bytes"] is None) != (clean_limits["vram_pool_bytes"] is None) or (
            clean_limits["vram_bytes"] is not None and clean_limits["vram_bytes"] > clean_limits["vram_pool_bytes"]
        ):
            raise NodeClientError("Local node contribution status has inconsistent VRAM limits")
        device = _worker_device(worker)
        if "vram_scope" in limits:
            expected_scope = (
                "per_device"
                if device.get("device") not in (None, "cpu") and clean_limits["vram_bytes"] is not None
                else None
            )
            if limits["vram_scope"] != expected_scope:
                raise NodeClientError("Local node contribution status has inconsistent VRAM scope")
            clean_limits["vram_scope"] = expected_scope
        clean_measurements = {
            "bandwidth_mbps": _optional_number(measurements.get("bandwidth_mbps"), "bandwidth measurement"),
            "power_watts": _optional_number(measurements.get("power_watts"), "power measurement"),
        }
        if resources["admitted"] and any(
            clean_limits[field] is not None and clean_measurements[field] is None
            for field in ("bandwidth_mbps", "power_watts")
        ):
            raise NodeClientError("Local node contribution status has inconsistent resource telemetry")
        resources["limits"] = clean_limits
        resources["measurements"] = clean_measurements
        if "managed_by" in worker and worker["managed_by"] != "desktop_gpu":
            raise NodeClientError("Local node contribution worker ownership is invalid")
        loading = {}
        if "load_state" in worker or "model_ready" in worker:
            if (
                "load_state" not in worker
                or "model_ready" not in worker
                or not isinstance(worker["load_state"], str)
                or worker["load_state"] not in ("waiting", "loading", "ready", "failed")
                or type(worker["model_ready"]) is not bool
                or (
                    worker["model_ready"]
                    and (
                        worker["load_state"] != "ready"
                        or state != "running"
                        or not worker["desired_running"]
                        or worker.get("operator_paused") is True
                        or not all(gate["admitted"] for gate in (policy, schedule, resources))
                    )
                )
            ):
                raise NodeClientError("Local node contribution worker readiness is invalid")
            loading = {"load_state": worker["load_state"], "model_ready": worker["model_ready"]}
        normalized_workers.append(
            {
                "id": worker_id,
                "model": model,
                **device,
                **({"managed_by": worker["managed_by"]} if "managed_by" in worker else {}),
                "state": state,
                "desired_running": worker["desired_running"],
                "operator_paused": worker.get("operator_paused") is True,
                **loading,
                "placement": placement,
                "policy": policy,
                "schedule": schedule,
                "resources": resources,
                "download_progress": download_view(worker.get("download_progress")),
            }
        )
    if not configured and normalized_workers:
        raise NodeClientError("Local node reports contribution workers while contribution is not configured")
    return {
        "schema_version": CONTRIBUTION_STATUS_SCHEMA_VERSION,
        "configured": configured,
        "editable": editable,
        "policy": policy_snapshot,
        "workers": normalized_workers,
        **({"recovery": _normalize_resource_recovery(value["recovery"])} if "recovery" in value else {}),
    }


def _normalize_hardware(value: Any) -> Dict[str, Any]:
    """Preserve capacity evidence without inferring opt-in, free VRAM or reservations.

    The legacy singular GPU aliases one inventory row; it is not extra capacity.
    """
    if not isinstance(value, dict):
        return {}
    result = {}
    for field in ("cpu_name", "gpu_name", "gpu_device", "device"):
        item = value.get(field)
        result[field] = (
            " ".join(item.split())[:160] if isinstance(item, str) and item.isprintable() and item.strip() else None
        )
    for field in ("gpu_total_bytes", "sharing_vram_bytes", "sharing_vram_available_bytes"):
        item = value.get(field)
        result[field] = item if type(item) is int and 0 <= item <= 64 * 1024**4 else None
    if "processing_percent" in value:
        percent = value["processing_percent"]
        if type(percent) not in (int, float) or not 1 <= percent <= 100:
            raise NodeClientError("Local node hardware has invalid processing percentage")
        result["processing_percent"] = percent
    inventory_fields = {
        "gpus",
        "gpu_backends",
        "gpu_inventory_limit",
        "gpu_visible_count",
        "gpu_inventory_status",
        "selected_device",
        "device_status",
        "sharing_vram_scope",
        "sharing_vram_kind",
    }
    if not inventory_fields.intersection(value):
        return result
    if not inventory_fields.issubset(value):
        raise NodeClientError("Local node hardware has incomplete GPU inventory")
    if value["sharing_vram_scope"] != "per_device" or value["sharing_vram_kind"] != "capacity":
        raise NodeClientError("Local node hardware has invalid GPU capacity semantics")
    limit, visible = value["gpu_inventory_limit"], value["gpu_visible_count"]
    if type(limit) is not int or not 1 <= limit <= MAX_GPU_INVENTORY_DEVICES:
        raise NodeClientError("Local node hardware has invalid GPU inventory limit")
    if type(visible) is not int or not 0 <= visible <= MAX_GPU_VISIBLE_COUNT:
        raise NodeClientError("Local node hardware has invalid visible GPU count")
    backends = value["gpu_backends"]
    if not isinstance(backends, dict) or set(backends) != {"cuda", "xpu", "mps"}:
        raise NodeClientError("Local node hardware has invalid GPU backends")
    clean_backends = {}
    for kind, summary in backends.items():
        if not isinstance(summary, dict) or set(summary) != {"status", "visible_count"}:
            raise NodeClientError("Local node hardware has invalid GPU backend summary")
        status, count = summary["status"], summary["visible_count"]
        if status not in ("available", "unavailable", "unsupported", "excess"):
            raise NodeClientError("Local node hardware has invalid GPU backend status")
        if count is not None and (type(count) is not int or not 0 <= count <= MAX_GPU_VISIBLE_COUNT):
            raise NodeClientError("Local node hardware has invalid GPU backend count")
        if (
            (status in ("available", "excess") and not count)
            or (status == "unsupported" and count is not None)
            or (kind == "mps" and count not in (None, 0, 1))
        ):
            raise NodeClientError("Local node hardware has inconsistent GPU backend count")
        clean_backends[kind] = {"status": status, "visible_count": count}
    if visible != sum(summary["visible_count"] or 0 for summary in clean_backends.values()):
        raise NodeClientError("Local node hardware has inconsistent visible GPU count")
    rows = value["gpus"]
    if not isinstance(rows, list) or len(rows) > min(limit, visible):
        raise NodeClientError("Local node hardware has invalid GPU inventory size")
    clean_rows = []
    seen = set()
    for row in rows:
        fields = {"device", "name", "total_bytes", "status", "sharing_vram_bytes", "sharing_vram_available_bytes"}
        if not isinstance(row, dict) or set(row) != fields:
            raise NodeClientError("Local node hardware has invalid GPU record")
        device = _public_device(row["device"], "GPU device", accelerator=True)
        if device is None or device in seen:
            raise NodeClientError("Local node hardware has missing or duplicate GPU device")
        seen.add(device)
        kind, _, index = device.partition(":")
        count = clean_backends[kind]["visible_count"]
        if count is None or (int(index) if index else 0) >= count:
            raise NodeClientError("Local node hardware has inconsistent GPU device count")
        status = row["status"]
        if status not in ("available", "unavailable", "unsupported"):
            raise NodeClientError("Local node hardware has invalid GPU status")
        total = _hardware_bytes(row["total_bytes"], "GPU capacity", positive=True)
        allowance = _hardware_bytes(row["sharing_vram_bytes"], "GPU sharing capacity")
        capacity = _hardware_bytes(row["sharing_vram_available_bytes"], "GPU sharing capacity ceiling")
        name = row["name"]
        if status == "available":
            name = _bounded_status_text(name, "GPU model name", limit=160)
            if total is None or capacity is None or allowance is None or not allowance <= capacity <= total:
                raise NodeClientError("Local node hardware has inconsistent GPU capacities")
        elif name is not None or total is not None or (allowance, capacity) not in ((None, None), (0, 0)):
            raise NodeClientError("Local node hardware has capacities for an unavailable GPU")
        clean_rows.append({**row, "name": name})
    inventory_status = value["gpu_inventory_status"]
    if inventory_status not in ("available", "partial", "unavailable", "excess"):
        raise NodeClientError("Local node hardware has invalid GPU inventory status")
    if (
        (inventory_status in ("available", "partial") and not rows)
        or (
            inventory_status == "available"
            and (len(rows) != min(limit, visible) or any(row["status"] != "available" for row in clean_rows))
        )
        or (inventory_status == "unavailable" and rows)
        or (inventory_status == "excess" and visible <= limit)
        or (visible > limit and inventory_status != "excess")
    ):
        raise NodeClientError("Local node hardware has inconsistent GPU inventory status")
    selected = _public_device(value["selected_device"], "selected device")
    device_status = value["device_status"]
    if device_status not in ("available", "unavailable", "unsupported", "excess"):
        raise NodeClientError("Local node hardware has invalid selected device status")
    device = value.get("device")
    device = device if device == "unknown" else _public_device(device, "hardware device")
    available_devices = {row["device"] for row in clean_rows if row["status"] == "available"}
    if device_status == "available":
        if selected is None or device != selected or (selected != "cpu" and selected not in available_devices):
            raise NodeClientError("Local node hardware has inconsistent selected device")
    elif device != "unknown":
        raise NodeClientError("Local node hardware has an unavailable active device")
    gpu_device = _public_device(value.get("gpu_device"), "legacy GPU device", accelerator=True)
    if gpu_device is not None and gpu_device not in available_devices:
        raise NodeClientError("Local node hardware has an unavailable legacy GPU device")
    for field in ("gpu_total_bytes", "sharing_vram_bytes", "sharing_vram_available_bytes"):
        result[field] = _hardware_bytes(value.get(field), field, positive=field == "gpu_total_bytes")
    if gpu_device is None:
        if any(
            result[field] is not None
            for field in ("gpu_name", "gpu_total_bytes", "sharing_vram_bytes", "sharing_vram_available_bytes")
        ):
            raise NodeClientError("Local node hardware has capacities without a legacy GPU")
    else:
        alias = next(row for row in clean_rows if row["device"] == gpu_device)
        allowance, capacity = result["sharing_vram_bytes"], result["sharing_vram_available_bytes"]
        if (
            device_status != "available"
            or (selected != "cpu" and gpu_device != selected)
            or result["gpu_name"] != alias["name"]
            or result["gpu_total_bytes"] != alias["total_bytes"]
            or allowance is None
            or capacity is None
            or not allowance <= capacity <= alias["total_bytes"]
            or (selected == "cpu" and (allowance, capacity) != (0, 0))
        ):
            raise NodeClientError("Local node hardware has inconsistent legacy GPU capacity")
    result.update(
        gpus=clean_rows,
        gpu_backends=clean_backends,
        gpu_inventory_limit=limit,
        gpu_visible_count=visible,
        gpu_inventory_status=inventory_status,
        selected_device=selected,
        device_status=device_status,
        device=device,
        gpu_device=gpu_device,
        sharing_vram_scope="per_device",
        sharing_vram_kind="capacity",
    )
    return result


class NodeClient:
    """Synchronous control client; GUI adapters must call it off their event loop."""

    def __init__(self, node_url: str, control_token: str, *, timeout: float = 5.0):
        self.node_url = normalize_loopback_url(node_url)
        if not isinstance(control_token, str) or not control_token.strip():
            raise ValueError("control credential must be a non-empty string")
        if "\r" in control_token or "\n" in control_token:
            raise ValueError("control credential must not contain newlines")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._control_token = control_token.strip()
        self.timeout = float(timeout)
        # A localhost credential must never be sent through an environment-configured
        # HTTP proxy. Supplying an empty ProxyHandler disables proxy discovery.
        self._opener = build_opener(ProxyHandler({}), _RejectRedirects())

    def _decode_response(self, response) -> Dict[str, Any]:  # noqa: ANN001
        body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise NodeClientError("Local node response exceeded the size limit")
        try:
            decoded = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NodeClientError("Local node returned invalid JSON") from exc
        if not isinstance(decoded, dict):
            raise NodeClientError("Local node response must be a JSON object")
        return decoded

    def _request(self, method: str, path: str, *, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        data = None if payload is None else json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")
        request = Request(
            f"{self.node_url}{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._control_token}",
                **({"Content-Type": "application/json"} if data is not None else {}),
            },
        )
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return self._decode_response(response)
        except HTTPError as exc:
            detail = exc.reason or "request failed"
            try:
                body = exc.read(MAX_RESPONSE_BYTES + 1)
                decoded = json.loads(body.decode("utf-8"))
                if isinstance(decoded, dict) and isinstance(decoded.get("detail"), str):
                    detail = decoded["detail"]
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pass
            raise NodeApiError(exc.code, str(detail).replace(self._control_token, "<redacted>")) from exc
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = str(getattr(exc, "reason", exc)).replace(self._control_token, "<redacted>")
            raise NodeClientError(f"Could not connect to the local node: {reason}") from exc

    def status(self) -> Dict[str, Any]:
        result = self._request("GET", "/control/v1/status")
        if result.get("api_version") != SUPPORTED_CONTROL_API_VERSION:
            raise NodeClientError(
                f"Unsupported control API version {result.get('api_version')!r}; "
                f"expected {SUPPORTED_CONTROL_API_VERSION}"
            )
        if not isinstance(result.get("openai_base_url"), str):
            raise NodeClientError("Local node status omitted openai_base_url")
        normalize_loopback_url(result["openai_base_url"])
        if (
            not isinstance(result.get("models"), list)
            or any(not isinstance(item, dict) for item in result["models"])
            or not isinstance(result.get("workers"), list)
            or any(not isinstance(item, dict) for item in result["workers"])
        ):
            raise NodeClientError("Local node status has invalid model or worker data")
        result["models"] = [
            {**item, "download": _normalize_model_download(item.get("download"))} for item in result["models"]
        ]
        result["workers"] = [{**worker, **_worker_device(worker)} for worker in result["workers"]]
        result["auto_selection"] = _normalize_auto_selection(result.get("auto_selection"))
        result["contribution"] = _normalize_contribution_status(result.get("contribution"))
        result["hardware"] = _normalize_hardware(result.get("hardware"))
        return result

    def get_contribution_policy(self) -> Dict[str, Any]:
        return _normalize_policy_snapshot(
            self._request("GET", "/control/v1/contribution-policy"),
            require_revision=True,
        )

    def get_gpu_selection(self) -> Dict[str, Any]:
        return _normalize_gpu_selection(self._request("GET", "/control/v1/contribution-gpu-selection"))

    def update_gpu_selection(self, rows: list[Dict[str, Any]], *, expected_revision: str) -> Dict[str, Any]:
        if not isinstance(expected_revision, str) or not _GPU_REVISION.fullmatch(expected_revision):
            raise NodeClientError("GPU selection requires its original config revision")
        payload = {
            "schema_version": 1,
            "expected_config_revision": expected_revision,
            "rows": _gpu_rows(rows, request=True),
        }
        if len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")) > 16 * 1024:
            raise NodeClientError("GPU selection request is too large")
        result = self._request("PUT", "/control/v1/contribution-gpu-selection", payload=payload)
        if (
            set(result) != {"schema_version", "config_revision", "restart_required", "runtime_ready"}
            or type(result.get("schema_version")) is not int
            or result["schema_version"] != 1
            or not isinstance(result.get("config_revision"), str)
            or not _GPU_REVISION.fullmatch(result["config_revision"])
            or type(result.get("restart_required")) is not bool
            or type(result.get("runtime_ready")) is not bool
            or (result["restart_required"] and result["runtime_ready"])
        ):
            raise NodeClientError("GPU selection save response is invalid; refresh saved settings")
        return dict(result)

    def set_inference_mode(self, mode: str) -> Dict[str, Any]:
        if mode not in ("auto", "local_only"):
            raise ValueError("inference mode must be auto or local_only")
        policy = self.get_contribution_policy()
        return self._request(
            "PUT",
            "/control/v1/inference-mode",
            payload={"inference_mode": mode, "expected_config_revision": policy["config_revision"]},
        )

    def update_contribution_policy(self, policy: Mapping[str, Any], *, expected_revision: str) -> Dict[str, Any]:
        if not isinstance(policy, Mapping):
            raise ValueError("contribution policy must be a mapping")
        normalized_policy = _normalize_policy(dict(policy))
        if not isinstance(expected_revision, str):
            raise ValueError("expected config revision must be a string")
        return _normalize_policy_snapshot(
            self._request(
                "PUT",
                "/control/v1/contribution-policy",
                payload={
                    "schema_version": CONTRIBUTION_POLICY_SCHEMA_VERSION,
                    "expected_config_revision": expected_revision,
                    "policy": normalized_policy,
                },
            ),
            require_revision=True,
        )

    def list_workers(self) -> List[Dict[str, Any]]:
        result = self._request("GET", "/control/v1/workers")
        workers = result.get("workers")
        if not isinstance(workers, list) or any(not isinstance(item, dict) for item in workers):
            raise NodeClientError("Local node returned invalid worker data")
        return workers

    def worker_action(self, worker_id: str, action: str) -> Dict[str, Any]:
        if action not in ("start", "pause", "restart"):
            raise ValueError("worker action must be start, pause, or restart")
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("worker id must be a non-empty string")
        return self._request("POST", f"/control/v1/workers/{quote(worker_id.strip(), safe='')}/{action}")

    def list_keys(self) -> List[Dict[str, Any]]:
        result = self._request("GET", "/control/v1/keys")
        keys = result.get("keys")
        if not isinstance(keys, list) or any(not isinstance(item, dict) for item in keys):
            raise NodeClientError("Local node returned invalid API-key data")
        return keys

    def create_key(self, label: str) -> Dict[str, Any]:
        if not isinstance(label, str) or not label.strip():
            raise ValueError("API-key label must be a non-empty string")
        result = self._request("POST", "/control/v1/keys", payload={"label": label.strip()})
        if not isinstance(result.get("key"), dict) or not isinstance(result.get("secret"), str):
            raise NodeClientError("Local node returned an invalid API-key creation response")
        return result

    def relabel_key(self, key_id: str, label: str) -> Dict[str, Any]:
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("API-key id must be a non-empty string")
        if not isinstance(label, str) or not label.strip():
            raise ValueError("API-key label must be a non-empty string")
        return self._request(
            "PATCH",
            f"/control/v1/keys/{quote(key_id.strip(), safe='')}",
            payload={"label": label.strip()},
        )

    def revoke_key(self, key_id: str) -> Dict[str, Any]:
        if not isinstance(key_id, str) or not key_id.strip():
            raise ValueError("API-key id must be a non-empty string")
        return self._request("DELETE", f"/control/v1/keys/{quote(key_id.strip(), safe='')}")
