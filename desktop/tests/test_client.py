from __future__ import annotations

import copy
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler

from communityai_desktop.acceptance import fake_node, run_contract
from communityai_desktop.client import (
    NodeApiError,
    NodeClient,
    NodeClientError,
    _normalize_contribution_status,
    normalize_loopback_url,
)
from communityai_desktop.controller import DesktopController


class NodeClientTests(unittest.TestCase):
    def test_normalizes_loopback_openai_url(self):
        self.assertEqual(normalize_loopback_url("http://127.0.0.1:8080/v1/"), "http://127.0.0.1:8080")
        self.assertEqual(normalize_loopback_url("https://[::1]:9443/v1"), "https://[::1]:9443")
        self.assertEqual(normalize_loopback_url("http://localhost.:8080"), "http://localhost.:8080")

    def test_rejects_destinations_that_could_receive_the_control_credential(self):
        rejected = (
            "https://example.com",
            "http://localhost.example.com",
            "http://user:secret@localhost:8080",
            "file:///tmp/node.sock",
            "http://localhost:8080/control",
            "http://localhost:8080/?redirect=1",
        )
        for value in rejected:
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_loopback_url(value)

    def test_disables_environment_http_proxies(self):
        client = NodeClient("http://127.0.0.1:8080", "control-secret")
        proxy_handlers = [handler for handler in client._opener.handlers if isinstance(handler, ProxyHandler)]
        # build_opener removes its default ProxyHandler when an explicit empty one is
        # supplied; the empty handler itself has no protocol methods and is omitted.
        self.assertEqual(proxy_handlers, [])

    def test_shared_acceptance_contract(self):
        with fake_node() as (url, token):
            result = run_contract(NodeClient(url, token))
        self.assertEqual(result["api_version"], 1)
        self.assertEqual(result["worker_actions"], 3)
        self.assertEqual(result["key_lifecycle"], "passed")
        self.assertEqual(result["contribution_policy"], "passed")

    def test_rejects_incomplete_fail_open_contribution_status(self):
        with fake_node() as (url, token):
            contribution = NodeClient(url, token).status()["contribution"]
        malformed = copy.deepcopy(contribution)
        denied = next(worker for worker in malformed["workers"] if worker["id"] == "worker-c")
        denied["policy"]["reason"] = None
        with self.assertRaises(NodeClientError):
            _normalize_contribution_status(malformed)

    def test_rejects_nonfinite_unbounded_and_inconsistent_status(self):
        with fake_node() as (url, token):
            contribution = NodeClient(url, token).status()["contribution"]

        invalid_values = []
        overlong = copy.deepcopy(contribution)
        overlong["workers"][0]["id"] = "w" * 129
        invalid_values.append(overlong)

        nonfinite = copy.deepcopy(contribution)
        nonfinite["workers"][0]["resources"]["measurements"]["bandwidth_mbps"] = float("nan")
        invalid_values.append(nonfinite)

        inconsistent_vram = copy.deepcopy(contribution)
        inconsistent_vram["workers"][0]["resources"]["limits"]["vram_pool_bytes"] = 1
        invalid_values.append(inconsistent_vram)

        for resource in ("bandwidth_mbps", "power_watts"):
            missing_telemetry = copy.deepcopy(contribution)
            worker = missing_telemetry["workers"][0]
            worker["resources"]["limits"][resource] = 1.0
            worker["resources"]["measurements"][resource] = None
            self.assertTrue(worker["resources"]["admitted"])
            invalid_values.append(missing_telemetry)

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(NodeClientError):
                _normalize_contribution_status(value)

    def test_controller_preserves_schedule_resource_blocks_and_pause(self):
        with fake_node() as (url, token):
            contribution = NodeClient(url, token).status()["contribution"]

        scheduled = copy.deepcopy(contribution)
        worker = scheduled["workers"][0]
        worker["desired_running"] = True
        worker["schedule"] = {
            "admitted": False,
            "reason": "Outside the configured schedule",
            "suspended": True,
        }
        scheduled_worker = DesktopController._worker_view(_normalize_contribution_status(scheduled)["workers"][0])
        self.assertFalse(scheduled_worker["can_start"])
        self.assertTrue(scheduled_worker["schedule_suspended"])
        self.assertEqual(
            scheduled_worker["display_status"],
            "Waiting: Outside the configured schedule",
        )

        unavailable = copy.deepcopy(contribution)
        worker = unavailable["workers"][0]
        worker["resources"]["admitted"] = False
        worker["resources"]["reason"] = "Power telemetry is unavailable"
        worker["resources"]["measurements"]["power_watts"] = None
        resource_worker = DesktopController._worker_view(_normalize_contribution_status(unavailable)["workers"][0])
        self.assertFalse(resource_worker["can_start"])
        self.assertEqual(resource_worker["blocked_reason"], "Power telemetry is unavailable")

        class RecordingClient:
            def __init__(self):
                self.calls = []

            def worker_action(self, worker_id, action):
                self.calls.append((worker_id, action))
                return {"changed": True}

        client = RecordingClient()
        controller = DesktopController(client)
        controller.set_workers_enabled(["blocked-worker"], False)
        self.assertEqual(client.calls, [("blocked-worker", "pause")])

    def test_invalid_auth_does_not_expose_the_candidate_secret(self):
        secret = "must-never-appear-in-errors"
        with fake_node() as (url, _):
            with self.assertRaises(NodeApiError) as caught:
                NodeClient(url, secret).status()
        self.assertEqual(caught.exception.status_code, 401)
        self.assertNotIn(secret, str(caught.exception))

    def test_redirect_is_not_followed_with_the_control_credential(self):
        class RedirectHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002, ANN001
                return

            def do_GET(self):  # noqa: N802
                self.send_response(302)
                self.send_header("Location", "https://example.com/credential-capture")
                self.end_headers()

        server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with self.assertRaises(NodeApiError) as caught:
                NodeClient(f"http://127.0.0.1:{server.server_port}", "control-secret").status()
            self.assertEqual(caught.exception.status_code, 302)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_controller_builds_shell_neutral_view(self):
        with fake_node() as (url, token):
            snapshot = DesktopController(NodeClient(url, token)).snapshot()
        self.assertEqual(snapshot["openai_base_url"].rsplit("/", 1)[-1], "v1")
        self.assertEqual(snapshot["models"][0]["coverage"], "32/32")
        self.assertEqual(snapshot["network"]["peer_count"], 96)
        self.assertEqual(len(snapshot["network"]["regions"]), 5)
        self.assertEqual(snapshot["contribution"]["active_models"], ["Qwen 3 8B"])
        self.assertEqual(snapshot["contribution"]["vram_percent"], 50)
        blocked = next(worker for worker in snapshot["workers"] if worker["id"] == "worker-c")
        self.assertFalse(blocked["can_start"])
        self.assertEqual(blocked["blocked_reason"], "Model is denied by node policy")
        self.assertEqual(snapshot["workers"][0]["state"], "paused")
        self.assertEqual(snapshot["keys"][0]["label"], "bootstrap")


if __name__ == "__main__":
    unittest.main()
