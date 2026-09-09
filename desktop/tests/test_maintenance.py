import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


class MaintenanceTests(unittest.TestCase):
    def test_broken_installed_runtime_returns_failure_without_traceback_dialog(self):
        from communityai_desktop.app import main

        with mock.patch("communityai_desktop.maintenance.prepare_update", side_effect=ImportError("Qt unavailable")):
            with self.assertRaises(SystemExit) as caught:
                main(["--prepare-update"])
        self.assertEqual(caught.exception.code, 2)

    def test_shutdown_acknowledges_only_after_owned_cleanup(self):
        name = "communityai-maintenance-test-" + uuid.uuid4().hex

        def request_shutdown():
            return subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import sys; from communityai_desktop.maintenance import prepare_update; "
                    "sys.exit(prepare_update(instance_name=sys.argv[1], timeout=10))",
                    name,
                ],
                capture_output=True,
                timeout=15,
            ).returncode

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "stopped.txt"
            ready = Path(directory) / "ready.txt"
            script = Path(directory) / "desktop.py"
            script.write_text(
                """
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from communityai_desktop.acceptance import fake_node
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from communityai_desktop.pyside_shell import run

class Automation:
    def install(self, window, application, types):
        QTimer.singleShot(100, lambda: Path(sys.argv[2]).write_text('ready'))

with fake_node() as (url, token):
    run(DesktopController(NodeClient(url, token)), instance_name=sys.argv[1],
        auto_close_seconds=20, qualification_automation=Automation(),
        before_termination_restore=lambda: Path(sys.argv[3]).write_text('owned cleanup complete'))
""",
                encoding="utf-8",
            )
            process = subprocess.Popen(
                [sys.executable, str(script), name, str(ready), str(marker)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                deadline = time.monotonic() + 15
                while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready.exists())
                self.assertEqual(request_shutdown(), 0)
                self.assertEqual(marker.read_text(), "owned cleanup complete")
                self.assertEqual(process.wait(timeout=10), 0)
                self.assertEqual(request_shutdown(), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=10)
                process.stderr.close()
