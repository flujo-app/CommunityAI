import importlib.util
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux installer process ownership")
class LinuxInstallerTests(unittest.TestCase):
    @staticmethod
    def maintenance():
        path = Path(__file__).resolve().parents[1] / "installers/linux_maintenance.py"
        spec = importlib.util.spec_from_file_location("linux_installer_maintenance", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_stop_owned_tree_preserves_unrelated_process_and_user_data(self):
        maintenance = self.maintenance()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installation = root / "installation"
            installation.mkdir()
            (installation / ".communityai-installation").write_text("CommunityAI installer-managed fixture\n")
            executable = installation / "CommunityAI"
            shutil.copy2("/bin/sh", executable)
            child_pid = root / "child-pid"
            state = root / "user-cache"
            state.write_bytes(b"retained verified data")
            process = subprocess.Popen(
                [str(executable), "-c", 'sleep 60 & echo $! > "$1"; wait', "probe", str(child_pid)]
            )
            unrelated = subprocess.Popen(["sleep", "60"])
            try:
                deadline = time.monotonic() + 5
                while not child_pid.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(child_pid.exists())
                descendant = int(child_pid.read_text())
                maintenance.stop_installation(installation, timeout=2)
                self.assertIsNotNone(process.poll())
                self.assertNotIn(descendant, maintenance.process_snapshot())
                self.assertIsNone(unrelated.poll())
                self.assertEqual(state.read_bytes(), b"retained verified data")
            finally:
                for candidate in (process, unrelated):
                    if candidate.poll() is None:
                        candidate.kill()
                    candidate.wait(timeout=5)

    def test_unmarked_installation_is_not_touched(self):
        maintenance = self.maintenance()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            preserved = root / "unrelated.txt"
            preserved.write_text("preserved")
            with self.assertRaises(RuntimeError):
                maintenance.stop_installation(root)
            self.assertEqual(preserved.read_text(), "preserved")

    def test_inaccessible_process_refuses_replacement_without_sending_signals(self):
        maintenance = self.maintenance()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".communityai-installation").write_text("CommunityAI installer-managed fixture\n")
            with patch.object(maintenance.os, "readlink", side_effect=PermissionError("restricted procfs")):
                with patch.object(maintenance.signal, "pidfd_send_signal") as send:
                    with self.assertRaisesRegex(RuntimeError, "Cannot inspect process ownership"):
                        maintenance.stop_installation(root)
                    send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
