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
    _normalize_auto_selection,
    _normalize_contribution_status,
    _normalize_model_download,
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
        self.assertEqual(result["policy_update"], "passed")
        self.assertEqual(result["auto_selection"], "passed")

    def test_rejects_malformed_auto_selection(self):
        selected = {
            "selector": "auto",
            "status": "selected",
            "model": "Qwen 3.5 2B",
            "manifest_digest": "sha256:" + "a" * 64,
            "reason": "A complete live route is available.",
            "covered_blocks": 24,
            "total_blocks": 24,
            "peer_count": 1,
            "source": "discovery",
        }
        self.assertEqual(_normalize_auto_selection(selected)["model"], "Qwen 3.5 2B")

        for mutation in (
            lambda value: value.update({"covered_blocks": 23}),
            lambda value: value.update({"peer_count": 0}),
            lambda value: value.update({"manifest_digest": "sha256:invalid"}),
            lambda value: value.update({"status": "selected", "model": None}),
        ):
            malformed = copy.deepcopy(selected)
            mutation(malformed)
            with self.subTest(value=malformed), self.assertRaises(NodeClientError):
                _normalize_auto_selection(malformed)

    def test_model_download_estimate_is_bounded_and_fail_closed(self):
        valid = {
            "schema_version": 1,
            "selected_whole_shard_bytes": 4_571_197_320,
        }
        self.assertEqual(_normalize_model_download(valid), valid)

        invalid_values = (
            None,
            {},
            {"schema_version": 0, "selected_whole_shard_bytes": 4_571_197_320},
            {"schema_version": True, "selected_whole_shard_bytes": 4_571_197_320},
            {"schema_version": 1.0, "selected_whole_shard_bytes": 4_571_197_320},
            {"schema_version": 1, "selected_whole_shard_bytes": True},
            {"schema_version": 1, "selected_whole_shard_bytes": 0},
            {"schema_version": 1, "selected_whole_shard_bytes": 64 * 1024**4 + 1},
            {**valid, "credential": "must-not-be-accepted"},
        )
        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(NodeClientError):
                _normalize_model_download(value)

    def test_accepts_schema_3_placement_and_rejects_stale_or_malformed_evidence(self):
        with fake_node() as (url, token):
            contribution = NodeClient(url, token).status()["contribution"]

        self.assertEqual(contribution["schema_version"], 3)
        automatic = next(worker for worker in contribution["workers"] if worker["id"] == "worker-b")
        self.assertEqual(
            automatic["placement"],
            {
                "automatic": True,
                "block_indices": "0:36",
                "reason": "Selected a complete catalog route",
            },
        )

        invalid_values = []
        stale = copy.deepcopy(contribution)
        stale["schema_version"] = 2
        invalid_values.append(stale)

        missing = copy.deepcopy(contribution)
        del missing["workers"][0]["placement"]
        invalid_values.append(missing)

        secret_field = copy.deepcopy(contribution)
        secret_field["workers"][0]["placement"]["credential"] = "must-not-be-accepted"
        invalid_values.append(secret_field)

        inconsistent = copy.deepcopy(contribution)
        inconsistent["workers"][0]["placement"]["block_indices"] = "0:1"
        invalid_values.append(inconsistent)

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(NodeClientError):
                _normalize_contribution_status(value)

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

    def test_rejects_malformed_editable_policy_projection(self):
        with fake_node() as (url, token):
            contribution = NodeClient(url, token).status()["contribution"]

        invalid_values = []
        stale_shape = copy.deepcopy(contribution)
        stale_shape["policy"]["config_revision"] = None
        invalid_values.append(stale_shape)

        secret_field = copy.deepcopy(contribution)
        secret_field["policy"]["policy"]["control_token"] = "must-not-be-accepted"
        invalid_values.append(secret_field)

        duplicate_selector = copy.deepcopy(contribution)
        duplicate_selector["policy"]["policy"]["denied_models"] = ["Qwen 3 8B", "qwen 3 8b"]
        invalid_values.append(duplicate_selector)

        invalid_schedule = copy.deepcopy(contribution)
        invalid_schedule["policy"]["policy"]["schedule"] = {
            "timezone": "UTC",
            "windows": [{"days": ["mon"], "start": "25:00", "end": "06:00"}],
        }
        invalid_values.append(invalid_schedule)

        for value in invalid_values:
            with self.subTest(value=value), self.assertRaises(NodeClientError):
                _normalize_contribution_status(value)

    def test_client_replaces_the_complete_policy_with_revision_binding(self):
        with fake_node() as (url, token):
            client = NodeClient(url, token)
            current = client.get_contribution_policy()
            policy = copy.deepcopy(current["policy"])
            policy.update(
                {
                    "sharing_enabled": False,
                    "allowed_models": [],
                    "preferred_models": [],
                    "denied_models": [],
                    "max_disk_space": None,
                    "max_vram": None,
                    "max_bandwidth_mbps": None,
                    "max_power_watts": None,
                    "pause_timeout": 5.0,
                    "schedule": {
                        "timezone": "UTC",
                        "windows": [{"days": ["sat", "sun"], "start": "08:00", "end": "20:00"}],
                    },
                }
            )
            result = client.update_contribution_policy(policy, expected_revision=current["config_revision"])
            self.assertEqual(result["policy"], policy)
            self.assertNotEqual(result["config_revision"], current["config_revision"])
            with self.assertRaises(NodeApiError) as caught:
                client.update_contribution_policy(policy, expected_revision=current["config_revision"])
            self.assertEqual(caught.exception.status_code, 412)

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
        self.assertTrue(snapshot["models"][0]["route_complete"])
        self.assertEqual(snapshot["auto_selection"]["model"], "Qwen 3 8B")
        self.assertIn("complete 36/36-block route", snapshot["auto_selection"]["reason"])
        selected = next(model for model in snapshot["models"] if model["auto_selected"])
        self.assertEqual(selected["id"], "Qwen 3 8B")
        self.assertEqual(selected["selected_whole_shard_bytes"], 12_000_000_000)
        self.assertEqual(selected["download_storage_estimate"], "12.0 GB (12,000,000,000 bytes)")
        self.assertEqual(snapshot["network"]["peer_count"], 96)
        self.assertEqual(len(snapshot["network"]["regions"]), 5)
        self.assertEqual(snapshot["contribution"]["active_models"], ["Qwen 3 8B"])
        self.assertEqual(snapshot["contribution"]["vram_percent"], 50)
        self.assertTrue(snapshot["contribution"]["editable"])
        self.assertEqual(snapshot["contribution"]["policy"]["max_vram"], "50%")
        blocked = next(worker for worker in snapshot["workers"] if worker["id"] == "worker-c")
        self.assertFalse(blocked["can_start"])
        self.assertEqual(blocked["blocked_reason"], "Model is denied by node policy")
        self.assertEqual(snapshot["workers"][0]["state"], "paused")
        self.assertEqual(snapshot["keys"][0]["label"], "bootstrap")


if __name__ == "__main__":
    unittest.main()
