"""Headless acceptance contract for the production desktop/node boundary."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, Tuple
from urllib.parse import unquote

from communityai_desktop.client import NodeClient


class _FakeNodeState:
    token = "drift_control_" + "A" * 43

    def __init__(self):
        self.worker_states = {
            "worker-a": ("Llama 3.1 8B", "paused"),
            "worker-b": ("Qwen 3 8B", "running"),
            "worker-c": ("Mistral Small", "paused"),
        }
        self.keys: Dict[str, Dict[str, Any]] = {
            "bootstrap": {
                "id": "bootstrap",
                "label": "bootstrap",
                "fingerprint": "sha256:bootstrap",
                "created_at": 1,
                "revoked_at": None,
            }
        }
        self.next_key = 1

    def worker(self, worker_id: str) -> Dict[str, Any]:
        model, worker_state = self.worker_states[worker_id]
        return {
            "id": worker_id,
            "model": model,
            "state": worker_state,
            "desired_running": worker_state == "running",
            "restart_count": 0,
            "last_error": None,
        }

    def workers(self) -> list[Dict[str, Any]]:
        return [self.worker(worker_id) for worker_id in self.worker_states]

    def contribution_worker(self, worker_id: str) -> Dict[str, Any]:
        worker = self.worker(worker_id)
        denied = worker_id == "worker-c"
        return {
            "id": worker["id"],
            "model": worker["model"],
            "state": worker["state"],
            "desired_running": worker["desired_running"],
            "policy": {
                "admitted": not denied,
                "reason": "Model is denied by node policy" if denied else None,
                "preferred": worker_id == "worker-b",
            },
            "schedule": {"admitted": True, "reason": None, "suspended": False},
            "resources": {
                "admitted": not denied,
                "reason": "Power telemetry is unavailable" if denied else None,
                "suspended": False,
                "limits": {
                    "disk_bytes": 100 * 1024**3,
                    "vram_bytes": 8 * 1024**3,
                    "vram_pool_bytes": 16 * 1024**3,
                    "bandwidth_mbps": 100.0,
                    "power_watts": 250.0,
                },
                "measurements": {
                    "bandwidth_mbps": 20.0 if not denied else None,
                    "power_watts": 120.0 if not denied else None,
                },
            },
        }

    def contribution_workers(self) -> list[Dict[str, Any]]:
        return [self.contribution_worker(worker_id) for worker_id in self.worker_states]


def _handler(state: _FakeNodeState):
    class FakeNodeHandler(BaseHTTPRequestHandler):
        server_version = "CommunityAIFakeNode/1"

        def log_message(self, format, *args):  # noqa: A002, ANN001
            return

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {state.token}":
                return True
            # Echo the candidate deliberately: the client contract must redact it.
            self._send(401, {"detail": f"Invalid API key {self.headers.get('Authorization', '')}"})
            return False

        def _body(self) -> Dict[str, Any]:
            size = int(self.headers.get("Content-Length", "0"))
            value = json.loads(self.rfile.read(size).decode("utf-8")) if size else {}
            if not isinstance(value, dict):
                raise ValueError("body must be an object")
            return value

        def _send(self, code: int, body: Dict[str, Any]) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return
            if self.path == "/control/v1/status":
                self._send(
                    200,
                    {
                        "api_version": 1,
                        "status": "running",
                        "started_at": 1,
                        "openai_base_url": f"http://127.0.0.1:{self.server.server_port}/v1",
                        "runtime_budget": {"max_loaded_models": 1, "resident_models": 0},
                        "models": [
                            {
                                "id": "Llama 3.1 8B",
                                "state": "ready",
                                "active_requests": 0,
                                "route": {"covered_blocks": 32, "total_blocks": 32, "peer_count": 47},
                            },
                            {
                                "id": "Qwen 3 8B",
                                "state": "ready",
                                "active_requests": 2,
                                "route": {"covered_blocks": 36, "total_blocks": 36, "peer_count": 31},
                            },
                            {
                                "id": "Mistral Small",
                                "state": "degraded",
                                "active_requests": 0,
                                "route": {"covered_blocks": 35, "total_blocks": 40, "peer_count": 18},
                            },
                        ],
                        "workers": state.workers(),
                        "network": {
                            "peer_count": 96,
                            "regions": [
                                {"name": "North America", "peers": 34},
                                {"name": "Europe", "peers": 29},
                                {"name": "Asia Pacific", "peers": 21},
                                {"name": "Latin America", "peers": 8},
                                {"name": "Other", "peers": 4},
                            ],
                        },
                        "contribution": {
                            "schema_version": 1,
                            "configured": True,
                            "workers": state.contribution_workers(),
                        },
                    },
                )
            elif self.path == "/control/v1/workers":
                self._send(200, {"workers": state.workers()})
            elif self.path == "/control/v1/keys":
                self._send(200, {"keys": list(state.keys.values())})
            else:
                self._send(404, {"detail": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authorized():
                return
            if self.path == "/control/v1/keys":
                body = self._body()
                key_id = f"client-{state.next_key}"
                state.next_key += 1
                metadata = {
                    "id": key_id,
                    "label": body["label"],
                    "fingerprint": f"sha256:{key_id}",
                    "created_at": state.next_key,
                    "revoked_at": None,
                }
                state.keys[key_id] = metadata
                self._send(201, {"key": metadata, "secret": f"drift_{key_id}-secret"})
                return
            parts = self.path.split("/")
            if len(parts) == 6 and parts[1:4] == ["control", "v1", "workers"]:
                worker_id, action = unquote(parts[4]), parts[5]
                if worker_id in state.worker_states and action in ("start", "pause", "restart"):
                    model, _ = state.worker_states[worker_id]
                    state.worker_states[worker_id] = (model, "paused" if action == "pause" else "running")
                    self._send(200, {"changed": True, "worker": state.worker(worker_id)})
                    return
            self._send(404, {"detail": "not found"})

        def do_PATCH(self):  # noqa: N802
            if not self._authorized():
                return
            prefix = "/control/v1/keys/"
            key_id = unquote(self.path[len(prefix) :]) if self.path.startswith(prefix) else ""
            if key_id not in state.keys:
                self._send(404, {"detail": "unknown key"})
                return
            state.keys[key_id]["label"] = self._body()["label"]
            self._send(200, {"key": state.keys[key_id]})

        def do_DELETE(self):  # noqa: N802
            if not self._authorized():
                return
            prefix = "/control/v1/keys/"
            key_id = unquote(self.path[len(prefix) :]) if self.path.startswith(prefix) else ""
            if key_id not in state.keys:
                self._send(404, {"detail": "unknown key"})
                return
            state.keys[key_id]["revoked_at"] = 2
            self._send(200, {"key": state.keys[key_id]})

    return FakeNodeHandler


@contextmanager
def fake_node() -> Iterator[Tuple[str, str]]:
    state = _FakeNodeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, name="desktop-acceptance-node", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state.token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_contract(client: NodeClient) -> Dict[str, Any]:
    """Exercise every privileged protocol operation used by this desktop slice."""
    status = client.status()
    contribution = status["contribution"]
    if contribution["schema_version"] != 1 or not contribution["configured"]:
        raise AssertionError("acceptance node omitted the authoritative contribution contract")
    from communityai_desktop.controller import DesktopController

    desktop = DesktopController(client).snapshot()
    denied = next(worker for worker in desktop["workers"] if worker["id"] == "worker-c")
    if denied["can_start"] or denied["blocked_reason"] != "Model is denied by node policy":
        raise AssertionError("desktop did not preserve the node's model-policy decision")
    if desktop["contribution"]["vram_percent"] != 50:
        raise AssertionError("desktop did not preserve the node's resolved VRAM budget")
    workers = client.list_workers()
    worker_a = next((worker for worker in workers if worker.get("id") == "worker-a"), None)
    if worker_a is None or worker_a.get("state") != "paused":
        raise AssertionError("acceptance node did not begin with worker-a paused")

    for action, expected in (("start", "running"), ("pause", "paused"), ("restart", "running")):
        response = client.worker_action("worker-a", action)
        if response.get("worker", {}).get("state") != expected:
            raise AssertionError(f"worker {action} did not produce state {expected}")

    created = client.create_key("acceptance client")
    key_id = created["key"]["id"]
    if created["secret"] in json.dumps(client.list_keys()):
        raise AssertionError("API-key listing exposed plaintext secret")
    relabeled = client.relabel_key(key_id, "renamed acceptance client")
    if relabeled.get("key", {}).get("label") != "renamed acceptance client":
        raise AssertionError("API-key relabel did not persist")
    revoked = client.revoke_key(key_id)
    if revoked.get("key", {}).get("revoked_at") is None:
        raise AssertionError("API-key revocation did not persist")

    return {
        "api_version": status["api_version"],
        "model_count": len(status["models"]),
        "worker_actions": 3,
        "key_lifecycle": "passed",
        "contribution_policy": "passed",
    }


def run_self_test() -> Dict[str, Any]:
    with fake_node() as (url, token):
        return run_contract(NodeClient(url, token))
