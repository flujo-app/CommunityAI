from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from communityai_desktop_spike.acceptance import fake_node, run_contract
from communityai_desktop_spike.client import NodeApiError, NodeClient, normalize_loopback_url
from communityai_desktop_spike.controller import DesktopController
from communityai_desktop_spike.credentials import CredentialError, load_private_key_file


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

    def test_shared_acceptance_contract(self):
        with fake_node() as (url, token):
            result = run_contract(NodeClient(url, token))
        self.assertEqual(result["api_version"], 1)
        self.assertEqual(result["worker_actions"], 3)
        self.assertEqual(result["key_lifecycle"], "passed")

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
        self.assertEqual(snapshot["models"][0]["coverage"], "8/8")
        self.assertEqual(snapshot["workers"][0]["state"], "paused")


class CredentialFileTests(unittest.TestCase):
    def test_reads_private_regular_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.key"
            path.write_text("drift_secret\n", encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            self.assertEqual(load_private_key_file(path), "drift_secret")

    def test_rejects_empty_or_public_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "control.key"
            path.write_text("", encoding="utf-8")
            if os.name != "nt":
                path.chmod(0o600)
            with self.assertRaises(CredentialError):
                load_private_key_file(path)

            if os.name != "nt":
                path.write_text("drift_secret", encoding="utf-8")
                path.chmod(0o644)
                with self.assertRaises(CredentialError):
                    load_private_key_file(path)

    def test_rejects_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(CredentialError):
                load_private_key_file(Path(directory))


if __name__ == "__main__":
    unittest.main()
