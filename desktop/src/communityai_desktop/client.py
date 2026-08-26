"""Strict localhost client for the versioned CommunityAI node control API."""

from __future__ import annotations

import ipaddress
import json
import math
import socket
from typing import Any, Dict, List, Mapping, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

MAX_RESPONSE_BYTES = 4 * 1024 * 1024
SUPPORTED_CONTROL_API_VERSION = 1
CONTRIBUTION_STATUS_SCHEMA_VERSION = 1


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


def _normalize_contribution_status(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") != CONTRIBUTION_STATUS_SCHEMA_VERSION:
        raise NodeClientError("Local node status has an unsupported contribution schema")
    configured = value.get("configured")
    workers = value.get("workers")
    if not isinstance(configured, bool) or not isinstance(workers, list):
        raise NodeClientError("Local node contribution status is malformed")
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
        data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
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
        result["contribution"] = _normalize_contribution_status(result.get("contribution"))
        return result

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
