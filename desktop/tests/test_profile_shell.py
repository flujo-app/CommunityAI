import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QLabel

from communityai_desktop.maintenance import prepare_update
from communityai_desktop.pyside_shell import _instance_data_root, run
from communityai_desktop.startup import SingleInstanceError


class ProfileShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        if cls.application.platformName() != "offscreen":
            raise RuntimeError("profile shell tests require an offscreen QApplication")

    def test_profile_window_identity_and_startup_guard_do_not_touch_regular_settings(self):
        errors = []
        observations = {}
        timers = []
        application_name = "CommunityAI Multi-GPU Test"

        class Automation:
            def install(self, window, application, qt):
                timer = qt["QTimer"](window)
                timers.append(timer)
                timer.setSingleShot(True)

                def exercise():
                    try:
                        observations.update(
                            title=window.windowTitle(),
                            application_name=application.applicationName(),
                            brand=window.findChild(QLabel, "brandName").text(),
                            initial_checked=window.login_startup_toggle.isChecked(),
                            enabled=window.login_startup_toggle.isEnabled(),
                            detail=window.login_startup_detail.text(),
                            locks=len(list(profile_root.glob("instance-*.lock"))),
                        )
                        window.login_startup_toggle.setChecked(True)
                        window._set_login_startup(True)
                        observations["after_programmatic_change"] = window.login_startup_toggle.isChecked()
                        window._connection_failed("offline fixture")
                        observations["offline_title"] = window.connection_title.text()
                        observations["offline_detail"] = window.connection_detail.text()
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        timer.stop()
                        window.close()
                        application.exit(0)

                timer.timeout.connect(exercise)
                timer.start(20)

        def offline():
            raise RuntimeError("offline profile test; no node connection")

        with tempfile.TemporaryDirectory() as directory:
            profile_root = Path(directory).resolve() / "volunteer-instance"
            with (
                mock.patch(
                    "communityai_desktop.pyside_shell.login_startup_enabled",
                    side_effect=AssertionError("regular startup read"),
                ) as read,
                mock.patch(
                    "communityai_desktop.pyside_shell.set_login_startup",
                    side_effect=AssertionError("regular startup write"),
                ) as write,
                mock.patch.object(
                    QStandardPaths, "writableLocation", side_effect=AssertionError("regular instance path read")
                ),
            ):
                try:
                    self.assertEqual(
                        run(
                            connect=offline,
                            application_name=application_name,
                            instance_data_dir=profile_root,
                            allow_login_startup=False,
                            qualification_automation=Automation(),
                        ),
                        0,
                    )
                finally:
                    for timer in timers:
                        timer.stop()
                        timer.timeout.disconnect()
                read.assert_not_called()
                write.assert_not_called()
            self.assertEqual(list(profile_root.glob("instance-*.lock")), [])
        if errors:
            raise errors[0]
        self.assertEqual(observations["title"], application_name)
        self.assertEqual(observations["application_name"], application_name)
        self.assertEqual(observations["brand"], application_name)
        self.assertIn(application_name, observations["offline_title"])
        self.assertIn(application_name, observations["offline_detail"])
        self.assertFalse(observations["initial_checked"])
        self.assertFalse(observations["enabled"])
        self.assertFalse(observations["after_programmatic_change"])
        self.assertIn("test profile", observations["detail"])
        self.assertEqual(observations["locks"], 1)

    def test_explicit_instance_paths_reject_relative_root_traversal_files_and_links(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            regular_file = root / "file"
            regular_file.write_text("untouched", encoding="utf-8")
            for candidate in (Path("relative"), Path(root.anchor), root / ".." / "elsewhere", regular_file / "child"):
                with self.subTest(candidate=candidate), self.assertRaises(SingleInstanceError):
                    _instance_data_root(candidate, None, create=True)
            self.assertEqual(regular_file.read_text(encoding="utf-8"), "untouched")
            target = root / "original"
            target.mkdir()
            link = root / "alias"
            if os.name == "nt":
                # A local junction requires no symlink privilege on Windows.
                subprocess.run(
                    ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                    check=True,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                link.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(SingleInstanceError, "symlinks or junctions"):
                _instance_data_root(link / "instance", None, create=True)
            with self.assertRaisesRegex(SingleInstanceError, "symlinks or junctions"):
                prepare_update(application_name="CommunityAI Multi-GPU Test", instance_data_dir=link / "instance")
            self.assertEqual(list(target.iterdir()), [])

    def test_maintenance_for_absent_profile_does_not_create_or_read_regular_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "unused-profile" / "instance"
            with mock.patch.object(QStandardPaths, "writableLocation", side_effect=AssertionError("regular path read")):
                self.assertEqual(
                    prepare_update(application_name="CommunityAI Multi-GPU Test", instance_data_dir=root), 0
                )
            self.assertFalse(root.parent.exists())

    def test_maintenance_stops_only_the_matching_profile_in_two_real_shell_processes(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = Path(directory).resolve()
            script = fixture / "profile_process.py"
            script.write_text(
                """
import json, sys
from pathlib import Path
from unittest.mock import patch
from PySide6.QtCore import QStandardPaths
from communityai_desktop.maintenance import prepare_update
from communityai_desktop.pyside_shell import run

action, mode, root_text = sys.argv[1:]
root = Path(root_text)
name = "CommunityAI" if mode == "regular" else "CommunityAI Multi-GPU Test"
# Intentionally reuse a legacy override: explicit profile roots must still isolate it.
options = {"application_name": name, "instance_name": "paired-profile-test-" + root.parent.name}
if mode != "regular":
    options["instance_data_dir"] = root
def default_location(*args):
    if mode != "regular":
        raise AssertionError("profile touched regular Qt data location")
    return str(root)
def offline():
    raise RuntimeError("offline profile fixture")
class Automation:
    def install(self, window, application, qt):
        qt["QTimer"].singleShot(20, lambda: (root / "ready.json").write_text(json.dumps({"title": window.windowTitle()})))
with patch.object(QStandardPaths, "writableLocation", default_location), patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False), patch("communityai_desktop.pyside_shell.set_login_startup", side_effect=AssertionError("autostart write")):
    if action == "stop":
        raise SystemExit(prepare_update(timeout=5, **options))
    raise SystemExit(run(connect=offline, allow_login_startup=mode == "regular", auto_close_seconds=30,
                         qualification_automation=Automation(),
                         before_termination_restore=lambda: (root / "stopped.txt").write_text("owned cleanup complete"),
                         **options))
""",
                encoding="utf-8",
            )
            roots = {mode: fixture / mode for mode in ("regular", "volunteer")}
            processes = {}
            environment = {**os.environ, "QT_QPA_PLATFORM": "offscreen", "PYTHONDONTWRITEBYTECODE": "1"}
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

            def stop(mode):
                result = subprocess.run(
                    [sys.executable, str(script), "stop", mode, str(roots[mode])],
                    env=environment,
                    capture_output=True,
                    timeout=10,
                    creationflags=flags,
                )
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace")[-1000:])

            try:
                for mode, root in roots.items():
                    processes[mode] = subprocess.Popen(
                        [sys.executable, str(script), "start", mode, str(root)],
                        env=environment,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                        creationflags=flags,
                    )
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline and not all(
                    (root / "ready.json").exists() for root in roots.values()
                ):
                    if any(process.poll() is not None for process in processes.values()):
                        break
                    time.sleep(0.02)
                for mode, root in roots.items():
                    self.assertTrue((root / "ready.json").exists(), f"{mode} failed to start")
                    self.assertIsNone(processes[mode].poll())
                self.assertEqual(json.loads((roots["regular"] / "ready.json").read_text())["title"], "CommunityAI")
                self.assertEqual(
                    json.loads((roots["volunteer"] / "ready.json").read_text())["title"], "CommunityAI Multi-GPU Test"
                )
                stop("volunteer")
                self.assertEqual(processes["volunteer"].wait(timeout=10), 0)
                self.assertEqual((roots["volunteer"] / "stopped.txt").read_text(), "owned cleanup complete")
                self.assertIsNone(processes["regular"].poll())
                self.assertFalse((roots["regular"] / "stopped.txt").exists())
                stop("volunteer")
                self.assertIsNone(processes["regular"].poll())
                stop("regular")
                self.assertEqual(processes["regular"].wait(timeout=10), 0)
                self.assertEqual((roots["regular"] / "stopped.txt").read_text(), "owned cleanup complete")
            finally:
                for process in processes.values():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=10)
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
