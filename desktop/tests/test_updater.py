import base64
import hashlib
import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from communityai_desktop import updater
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat


class Response(io.BytesIO):
    def __init__(self, body, status=200, headers=None):
        super().__init__(body)
        self.status = status
        self.headers = headers or {"Content-Length": str(len(body))}


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.key = Ed25519PrivateKey.generate()
        self.public = base64.b64encode(self.key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        self.payload = b"verified fixture installer"
        self.item = {
            "filename": "communityai-0.1.0-alpha.20260909.3-windows-setup.exe",
            "url": updater.ORIGIN + "/alpha/20260909.3/communityai-0.1.0-alpha.20260909.3-windows-setup.exe",
            "sha256": hashlib.sha256(self.payload).hexdigest(),
            "size_bytes": len(self.payload),
        }
        self.signed = {
            "schema_version": 1,
            "channel": "alpha",
            "version": "0.1.0-alpha.20260909.3",
            "sequence": 2026090903,
            "published_at": 1000,
            "expires_at": 3000,
            "artifacts": {"windows-x64": self.item},
        }

    def envelope(self):
        return updater.canonical(
            {
                "signed": self.signed,
                "signature": base64.b64encode(
                    self.key.sign(updater.SIGNATURE_DOMAIN + updater.canonical(self.signed))
                ).decode(),
            }
        )

    def verify(self, raw=None, **kwargs):
        return updater.verify_feed(raw or self.envelope(), public_key=self.public, now=2000, **kwargs)

    def test_signed_feed_rejects_tampering_and_wrong_key(self):
        self.assertEqual(self.verify()["version"], self.signed["version"])
        forged = json.loads(self.envelope())
        forged["signed"]["artifacts"]["windows-x64"]["sha256"] = "a" * 64
        with self.assertRaises(updater.UpdateError):
            self.verify(updater.canonical(forged))
        with self.assertRaises(updater.UpdateError):
            updater.verify_feed(self.envelope(), now=2000)

    def test_expiry_rollback_unknown_platform_and_external_url_rejected(self):
        for change in (
            {"expires_at": 1999},
            {"published_at": 2500},
            {"channel": "stable"},
            {"schema_version": True},
            {"sequence": 0},
        ):
            original = dict(self.signed)
            self.signed.update(change)
            with self.assertRaises(updater.UpdateError):
                self.verify()
            self.signed = original
        with self.assertRaises(updater.UpdateError):
            self.verify(minimum_sequence=2026090904)
        self.item["url"] = self.item["url"].replace(updater.ORIGIN, "https://example.com")
        with self.assertRaises(updater.UpdateError):
            self.verify()

    def test_duplicate_fields_and_oversized_feed_rejected(self):
        with self.assertRaises(updater.UpdateError):
            self.verify(b'{"signed":{},"signed":{},"signature":""}')
        with self.assertRaises(updater.UpdateError):
            self.verify(b" " * (updater.MAX_FEED_BYTES + 1))

    def test_versions_compare_numerically_and_stable_is_newer(self):
        self.assertLess(updater.version_key("0.1.0-alpha.20260909.2"), updater.version_key("0.1.0-alpha.20260909.10"))
        self.assertLess(updater.version_key("0.1.0-alpha.20260909.10"), updater.version_key("0.1.0"))
        with self.assertRaises(updater.UpdateError):
            updater.version_key("../../installer")

    def test_download_resumes_verified_prefix_and_reuses_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / (self.item["filename"] + ".part")).write_bytes(self.payload[:8])
            offsets = []

            def open_fixture(url, offset=0):
                offsets.append(offset)
                return Response(
                    self.payload[offset:],
                    206,
                    {
                        "Content-Length": str(len(self.payload) - offset),
                        "Content-Range": f"bytes {offset}-{len(self.payload) - 1}/{len(self.payload)}",
                    },
                )

            path = updater.download(self.item, root, lambda *args: None, threading.Event(), opener=open_fixture)
            self.assertEqual(path.read_bytes(), self.payload)
            updater.download(self.item, root, lambda *args: None, threading.Event(), opener=open_fixture)
            self.assertEqual(offsets, [8])

    def test_ignored_range_restarts_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / (self.item["filename"] + ".part")).write_bytes(b"old bytes")
            path = updater.download(
                self.item,
                root,
                lambda *args: None,
                threading.Event(),
                opener=lambda *args, **kwargs: Response(self.payload),
            )
            self.assertEqual(path.read_bytes(), self.payload)

    def test_wrong_range_and_corrupt_download_never_become_ready(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            partial = root / (self.item["filename"] + ".part")
            partial.write_bytes(self.payload[:2])
            with self.assertRaises(updater.UpdateError):
                updater.download(
                    self.item,
                    root,
                    lambda *args: None,
                    threading.Event(),
                    opener=lambda *args, **kwargs: Response(
                        self.payload[2:],
                        206,
                        {"Content-Length": str(len(self.payload) - 2), "Content-Range": "bytes 0-1/2"},
                    ),
                )
            partial.unlink()
            with self.assertRaises(updater.UpdateError):
                updater.download(
                    self.item,
                    root,
                    lambda *args: None,
                    threading.Event(),
                    opener=lambda *args, **kwargs: Response(b"x" * len(self.payload)),
                )
            self.assertFalse((root / self.item["filename"]).exists())
            self.assertFalse(partial.exists())

    def test_cancellation_and_low_disk_leave_existing_data_intact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sentinel = root / "user-settings"
            sentinel.write_text("preserve")
            cancel = threading.Event()
            cancel.set()
            with self.assertRaises(updater.UpdateError):
                updater.download(self.item, root, lambda *args: None, cancel)
            cancel.clear()
            with patch.object(updater.shutil, "disk_usage", return_value=type("Disk", (), {"free": 0})()):
                with self.assertRaises(updater.UpdateError):
                    updater.download(self.item, root, lambda *args: None, cancel)
            self.assertEqual(sentinel.read_text(), "preserve")

    def test_manager_downloads_without_installing_and_rechecks_cached_hash(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = updater.UpdateManager(directory, root=Path(directory), system="Windows")
            self.signed.update(published_at=int(time.time()) - 10, expires_at=int(time.time()) + 1000)
            actual_verify = updater.verify_feed

            def verified(raw, **kwargs):
                return actual_verify(raw, public_key=self.public, **kwargs)

            with (
                patch.object(updater, "verify_feed", side_effect=verified),
                patch.object(updater, "open_url", return_value=Response(self.envelope())),
                patch.object(updater, "download", return_value=Path(directory) / "setup.exe"),
                patch.object(updater.subprocess, "Popen") as launch,
            ):
                manager.check()
                manager.thread.join(5)
                self.assertEqual(manager.snapshot()["status"], "ready")
                launch.assert_not_called()
                manager.candidate[1].write_bytes(b"tampered")
                manager.install()
                manager.thread.join(5)
                self.assertEqual(manager.snapshot()["status"], "error")
                launch.assert_not_called()

    def test_windows_handoff_uses_verified_setup_and_existing_install_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / self.item["filename"]
            package.write_bytes(self.payload)
            self.signed["expires_at"] = int(time.time()) + 1000
            manager = updater.UpdateManager(root, root=root / "installed app", system="Windows")
            manager.candidate = self.signed, package
            with patch.object(updater.subprocess, "Popen") as launch, patch.object(
                updater.subprocess, "CREATE_NO_WINDOW", 0, create=True
            ):
                launch.return_value.wait.return_value = 1
                manager.install()
                manager.thread.join(5)
                argv = launch.call_args.args[0]
                self.assertEqual(argv[0], str(package))
                self.assertIn("/UPDATE=1", argv)
                self.assertIn("/DIR=" + str(root / "installed app"), argv)
                self.assertEqual(manager.snapshot()["status"], "error")

    def test_real_http_interruption_resumes_without_redownloading_prefix(self):
        import http.server
        import urllib.request

        payload = b"a" * (1024**2) + b"b" * 2048
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                offset = int(self.headers.get("Range", "bytes=0-")[6:-1])
                requests.append(offset)
                self.send_response(206 if offset else 200)
                self.send_header("Content-Length", str(len(payload) - offset))
                if offset:
                    self.send_header("Content-Range", f"bytes {offset}-{len(payload)-1}/{len(payload)}")
                self.end_headers()
                self.wfile.write(payload[offset:] if offset else payload[: 1024**2])
                self.close_connection = True

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            item = {**self.item, "sha256": hashlib.sha256(payload).hexdigest(), "size_bytes": len(payload)}

            def opener(url, offset=0):
                headers = {"Range": f"bytes={offset}-"} if offset else {}
                return urllib.request.urlopen(
                    urllib.request.Request(f"http://127.0.0.1:{server.server_port}/setup", headers=headers)
                )

            with tempfile.TemporaryDirectory() as directory:
                result = updater.download(item, Path(directory), lambda *args: None, threading.Event(), opener=opener)
                self.assertEqual(result.read_bytes(), payload)
                self.assertEqual(requests, [0, 1024**2])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(5)


class UpdateUiTests(unittest.TestCase):
    def test_progress_restart_and_active_answer_feedback_are_visible(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from communityai_desktop.acceptance import fake_node
        from communityai_desktop.client import NodeClient
        from communityai_desktop.controller import DesktopController
        from communityai_desktop.pyside_shell import run
        from PySide6.QtCore import QTimer

        errors = []

        class Updates:
            state = {"status": "idle", "message": "Check for updates"}
            installed = False

            def snapshot(self):
                return self.state

            def check(self):
                self.state = {"status": "checking", "message": "Checking for updates…"}

            def install(self):
                self.installed = True
                self.state = {"status": "installing", "message": "Installing update…"}

            def close(self):
                pass

        updates = Updates()

        class Automation:
            def install(self, window, application, types):
                def exercise():
                    try:
                        assert window.update_button.isVisible()
                        window.update_button.click()
                        assert window.update_button.text() == "Checking for updates…"
                        assert not window.update_button.isEnabled()
                        updates.state = {"status": "downloading", "message": "Downloading update… 50%"}
                        window._render_update()
                        assert "50%" in window.update_button.text()
                        updates.state = {"status": "ready", "message": "Restart to update"}
                        window._render_update()
                        window._snapshot["models"] = [{"active_requests": 1}]
                        window.update_button.click()
                        window._render_update()
                        assert window.update_detail.isVisible() and "answer" in window.update_detail.text()
                        assert not updates.installed
                        window._snapshot["models"] = []
                        window.update_button.click()
                        assert updates.installed and window.update_button.text() == "Installing update…"
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        application.quit()

                QTimer.singleShot(500, exercise)

        with fake_node() as (url, token), patch(
            "communityai_desktop.pyside_shell.login_startup_enabled", return_value=False
        ):
            run(
                DesktopController(NodeClient(url, token)),
                updater=updates,
                single_instance=False,
                qualification_automation=Automation(),
                auto_close_seconds=5,
            )
        if errors:
            raise errors[0]


if __name__ == "__main__":
    unittest.main()
