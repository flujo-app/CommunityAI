"""Shared headless acceptance contract for both desktop shells."""

from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, Tuple
from urllib.parse import unquote

from communityai_desktop_spike.client import NodeClient


class _FakeNodeState:
    token = "spike-control-secret"

    def __init__(self):
        self.worker_state = "paused"
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

    def worker(self) -> Dict[str, Any]:
        return {
            "id": "worker-a",
            "model": "Tiny Test",
            "state": self.worker_state,
            "desired_running": self.worker_state == "running",
            "restart_count": 0,
            "last_error": None,
        }


def _handler(state: _FakeNodeState):
    class FakeNodeHandler(BaseHTTPRequestHandler):
        server_version = "CommunityAIFakeNode/1"

        def log_message(self, format, *args):  # noqa: A002, ANN001
            return

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {state.token}":
                return True
            # Intentionally echo the bad candidate so the client contract proves it redacts
            # a hostile or buggy local server response before presenting an error.
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
                        "runtime_budget": {
                            "max_loaded_models": 1,
                            "resident_models": 0,
                        },
                        "models": [
                            {
                                "id": "Tiny Test",
                                "state": "known",
                                "active_requests": 0,
                                "route": {"covered_blocks": 8, "total_blocks": 8},
                            }
                        ],
                        "workers": [state.worker()],
                    },
                )
            elif self.path == "/control/v1/workers":
                self._send(200, {"workers": [state.worker()]})
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
            if len(parts) == 7 and parts[1:4] == ["control", "v1", "workers"]:
                worker_id, action = unquote(parts[4]), parts[5]
                # The route has six slash-separated components plus the leading empty string.
                if parts[6]:
                    self._send(404, {"detail": "not found"})
                    return
                if worker_id == "worker-a" and action in ("start", "pause", "restart"):
                    state.worker_state = "paused" if action == "pause" else "running"
                    self._send(200, {"changed": True, "worker": state.worker()})
                    return
            # Accept the actual route without a trailing slash.
            if len(parts) == 6 and parts[1:4] == ["control", "v1", "workers"]:
                worker_id, action = unquote(parts[4]), parts[5]
                if worker_id == "worker-a" and action in ("start", "pause", "restart"):
                    state.worker_state = "paused" if action == "pause" else "running"
                    self._send(200, {"changed": True, "worker": state.worker()})
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
    thread = threading.Thread(target=server.serve_forever, name="desktop-spike-fake-node", daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state.token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def run_contract(client: NodeClient) -> Dict[str, Any]:
    """Exercise the common protocol surface used by both shells."""
    status = client.status()
    workers = client.list_workers()
    if len(workers) != 1 or workers[0].get("state") != "paused":
        raise AssertionError("acceptance node did not begin with one paused worker")

    for action, expected in (
        ("start", "running"),
        ("pause", "paused"),
        ("restart", "running"),
    ):
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
    }


def run_self_test() -> Dict[str, Any]:
    with fake_node() as (url, token):
        return run_contract(NodeClient(url, token))
