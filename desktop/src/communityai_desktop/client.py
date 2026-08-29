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

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SUPPORTED_CONTROL_API_VERSION = 1
CONTRIBUTION_STATUS_SCHEMA_VERSION = 3
CONTRIBUTION_POLICY_SCHEMA_VERSION = 1


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
        peers = _optional_number(value.get("peer_count"), "auto peer count", integer=True, positive=True)
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
    if not isinstance(value, dict) or set(value) != fields or not isinstance(value["sharing_enabled"], bool):
        raise NodeClientError("Local node contribution policy is malformed")
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
        "allowed_models": allowed,
        "preferred_models": preferred,
        "denied_models": denied,
        "max_disk_space": max_disk_space,
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
            "disk_bytes": _optional_number(limits.get("disk_bytes"), "disk limit", integer=True, positive=True),
            "vram_bytes": _optional_number(limits.get("vram_bytes"), "VRAM limit", integer=True, positive=True),
            "vram_pool_bytes": _optional_number(
                limits.get("vram_pool_bytes"), "VRAM pool", integer=True, positive=True
            ),
            "bandwidth_mbps": _optional_number(limits.get("bandwidth_mbps"), "bandwidth limit", positive=True),
            "power_watts": _optional_number(limits.get("power_watts"), "power limit", positive=True),
        }
        if (clean_limits["vram_bytes"] is None) != (clean_limits["vram_pool_bytes"] is None) or (
            clean_limits["vram_bytes"] is not None and clean_limits["vram_bytes"] > clean_limits["vram_pool_bytes"]
        ):
            raise NodeClientError("Local node contribution status has inconsistent VRAM limits")
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
        normalized_workers.append(
            {
                "id": worker_id,
                "model": model,
                "state": state,
                "desired_running": worker["desired_running"],
                "placement": placement,
                "policy": policy,
                "schedule": schedule,
                "resources": resources,
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
    }


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
        result["auto_selection"] = _normalize_auto_selection(result.get("auto_selection"))
        result["contribution"] = _normalize_contribution_status(result.get("contribution"))
        return result

    def get_contribution_policy(self) -> Dict[str, Any]:
        return _normalize_policy_snapshot(
            self._request("GET", "/control/v1/contribution-policy"),
            require_revision=True,
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
