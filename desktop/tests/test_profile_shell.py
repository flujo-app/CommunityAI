import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from communityai_desktop.lifecycle import NodeLifecycleError
from communityai_desktop.maintenance import prepare_update
from communityai_desktop.pyside_shell import _instance_data_root, run
from communityai_desktop.startup import SingleInstanceError
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QLabel


class ProfileShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        if cls.application.platformName() != "offscreen":
            raise RuntimeError("profile shell tests require an offscreen QApplication")

    def test_anchored_shell_rejects_raw_maintenance_without_quitting_or_cleanup(self):
        from communityai_desktop.pyside_shell import _instance_server_name
        from PySide6.QtCore import QTimer
        from PySide6.QtNetwork import QLocalSocket

        errors, observations, sockets = [], {}, []
        cleanup = mock.Mock()

        class Automation:
            def install(self, window, application, qt):
                def request():
                    try:
                        window._connection_failed("fixture recovery")
                        observations["detail"] = window.connection_detail.text()
                        endpoint = QLocalSocket(application)
                        sockets.append(endpoint)
                        endpoint.connectToServer(_instance_server_name(root, None, profile_scoped=True))
                        assert endpoint.waitForConnected(500)
                        endpoint.write(b"shutdown\n")
                        endpoint.flush()
                    except BaseException as exc:
                        errors.append(exc)
                    QTimer.singleShot(250, verify)

                def verify():
                    try:
                        assert sockets and bytes(sockets[0].readAll()) == b"failed\n"
                        assert cleanup.call_count == 0 and window.isVisible()
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        application.exit(0)

                QTimer.singleShot(20, request)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(
                run(
                    connect=lambda: None,
                    application_name="CommunityAI Multi-GPU Test",
                    instance_data_dir=root,
                    allow_login_startup=False,
                    allow_instance_directory_creation=False,
                    allow_maintenance_ack=False,
                    before_termination_restore=cleanup,
                    qualification_automation=Automation(),
                    auto_close_seconds=3,
                ),
                0,
            )
        if errors:
            raise errors[0]
        self.assertIn("do not delete profile files", observations["detail"])
        cleanup.assert_called_once()

    def test_retryable_anchor_setup_failure_is_visible_and_requires_button_retry(self):
        from communityai_desktop.anchor_lifecycle import RETRYABLE_SETUP_ERROR, RetryableAnchorSetupError
        from PySide6.QtCore import QTimer

        errors, calls, failures = [], [], []
        release_failure = threading.Event()

        def unavailable():
            calls.append("connect")
            if len(calls) == 1:
                secret_only_in_traceback = "must-not-cross-queued-signal"
                if not release_failure.wait(2):
                    raise AssertionError(secret_only_in_traceback)
                raise RetryableAnchorSetupError(RETRYABLE_SETUP_ERROR)
            raise RuntimeError("transient connection fixture")

        class Automation:
            def install(self, window, application, qt):
                checks = [0, 0, 0]
                try:
                    self_case.assertEqual(len(window._tasks), 1)
                    task = next(iter(window._tasks))
                    task.signals.error.connect(failures.append)
                    release_failure.set()
                except BaseException as exc:
                    errors.append(exc)
                    application.exit(0)
                    return

                def verify():
                    try:
                        checks[0] += 1
                        if len(calls) < 1 or window._busy:
                            if checks[0] < 30:
                                QTimer.singleShot(10, verify)
                                return
                            raise AssertionError("initial connection failure did not finish")
                        self_case.assertEqual(calls, ["connect"])
                        self_case.assertEqual(window.connection_detail.text(), RETRYABLE_SETUP_ERROR)
                        self_case.assertEqual(window.retry_button.text(), "Retry setup")
                        self_case.assertTrue(window._connection_retry_required)
                        self_case.assertEqual(len(failures), 1)
                        failure = failures[0]
                        self_case.assertEqual(tuple(failure), (RETRYABLE_SETUP_ERROR, True))
                        self_case.assertFalse(isinstance(failure, BaseException))
                        self_case.assertFalse(hasattr(failure, "__traceback__"))
                        self_case.assertFalse(
                            any(isinstance(value, (BaseException, types.TracebackType)) for value in tuple(failure))
                        )
                        self_case.assertNotIn("must-not-cross-queued-signal", repr(failure))
                        # Timer/background refreshes remain observation-only while
                        # the fixed failure waits for an explicit user decision.
                        window.refresh()
                        window.refresh()
                        self_case.assertEqual(calls, ["connect"])
                        window.retry_button.click()
                        QTimer.singleShot(10, verify_retry)
                    except BaseException as exc:
                        errors.append(exc)
                        application.exit(0)

                def verify_retry():
                    try:
                        checks[1] += 1
                        if len(calls) < 2 or window._busy:
                            if checks[1] < 50:
                                QTimer.singleShot(10, verify_retry)
                                return
                            raise AssertionError("explicit retry did not finish")
                        self_case.assertEqual(calls, ["connect", "connect"])
                        self_case.assertFalse(window._connection_retry_required)
                        self_case.assertEqual(window.retry_button.text(), "Try again")
                        window.refresh()  # Ordinary transient failures still auto-reconnect.
                        QTimer.singleShot(10, verify_automatic_retry)
                    except BaseException as exc:
                        errors.append(exc)
                        application.exit(0)

                def verify_automatic_retry():
                    try:
                        checks[2] += 1
                        if len(calls) < 3:
                            if checks[2] < 50:
                                QTimer.singleShot(10, verify_automatic_retry)
                                return
                            raise AssertionError("ordinary periodic retry did not run")
                        self_case.assertEqual(calls, ["connect", "connect", "connect"])
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        application.exit(0)

                QTimer.singleShot(10, verify)

        self_case = self
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            self.assertEqual(
                run(
                    connect=unavailable,
                    application_name="CommunityAI Multi-GPU Test",
                    instance_data_dir=root,
                    allow_login_startup=False,
                    allow_instance_directory_creation=True,
                    allow_maintenance_ack=False,
                    qualification_automation=Automation(),
                    auto_close_seconds=3,
                ),
                0,
            )
        if errors:
            raise errors[0]

    def test_early_exit_cancels_auto_close_screenshot_and_updater_timers(self):
        from PySide6.QtCore import QTimer

        observed, windows, timers, errors = [], [], [], []
        updater = mock.Mock()
        updater.snapshot.return_value = {"status": "idle", "message": "fixture updater"}

        def offline():
            raise RuntimeError("offline sequential event-loop fixture")

        class ExitAfter:
            def __init__(self, delay_ms, marker):
                self.delay_ms, self.marker = delay_ms, marker

            def install(self, window, application, qt):
                windows.append(window)
                if self.marker == "first":
                    # Shorten only fixture deadlines; every updater timer must
                    # be inactive before owner cleanup or the next event loop.
                    window._update_initial_timer.start(100)
                    window._update_check_timer.start(100)
                timer = QTimer(window)
                timer.setSingleShot(True)
                timers.append(timer)

                def finish():
                    observed.append(self.marker)
                    application.exit(0)

                timer.timeout.connect(finish)
                timer.start(self.delay_ms)

        def owner_cleanup():
            try:
                window = windows[0]
                for name in ("_timer", "_update_timer", "_update_check_timer", "_update_initial_timer"):
                    self.assertFalse(getattr(window, name).isActive(), name)
                updater.close.assert_called_once_with()
                # Reentrant event processing must not resurrect this run's work.
                self.application.processEvents()
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "communityai_desktop.pyside_shell.login_startup_enabled", return_value=False
        ):
            screenshot = Path(directory) / "stale-screenshot.png"
            self.assertEqual(
                run(
                    connect=offline,
                    single_instance=False,
                    allow_login_startup=False,
                    qualification_automation=ExitAfter(10, "first"),
                    auto_close_seconds=0.15,
                    screenshot_path=screenshot,
                    updater=updater,
                    before_termination_restore=owner_cleanup,
                ),
                0,
            )
            # Old static singleShot quits this shared event loop at150ms,
            # before the second marker. The600ms screenshot must not fire either.
            self.assertEqual(
                run(
                    connect=offline,
                    single_instance=False,
                    allow_login_startup=False,
                    qualification_automation=ExitAfter(750, "second"),
                    auto_close_seconds=2,
                ),
                0,
            )
            self.assertFalse(screenshot.exists())
        if errors:
            raise errors[0]
        self.assertEqual(observed, ["first", "second"])
        updater.check.assert_not_called()
        updater.snapshot.assert_not_called()
        updater.close.assert_called_once_with()

    def test_setup_failure_after_timer_scheduling_stops_timers_before_owner_cleanup(self):
        from PySide6.QtCore import QTimer

        windows = []
        updater = mock.Mock()
        updater.snapshot.return_value = {"status": "idle", "message": "fixture updater"}
        owner_cleanup = mock.Mock()

        class CaptureWindow:
            def install(self, window, application, qt):
                windows.append(window)

        def cleanup():
            owner_cleanup()
            self.assertTrue(windows[0]._closing)
            self.assertTrue(all(not timer.isActive() for timer in windows[0].findChildren(QTimer)))
            updater.close.assert_called_once_with()

        with mock.patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False), mock.patch(
            "communityai_desktop.pyside_shell._install_posix_termination_bridge",
            side_effect=RuntimeError("fixture bridge setup failure"),
        ), self.assertRaisesRegex(RuntimeError, "fixture bridge setup failure"):
            run(
                connect=lambda: None,
                single_instance=False,
                allow_login_startup=False,
                updater=updater,
                qualification_automation=CaptureWindow(),
                auto_close_seconds=0.15,
                before_termination_restore=cleanup,
            )
        owner_cleanup.assert_called_once_with()
        updater.check.assert_not_called()

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
            with self.assertRaisesRegex(
                (SingleInstanceError, NodeLifecycleError), "symlinks or junctions|anchor maintenance"
            ):
                prepare_update(application_name="CommunityAI Multi-GPU Test", instance_data_dir=link / "instance")
            self.assertEqual(list(target.iterdir()), [])

    def test_maintenance_for_absent_profile_does_not_create_or_read_regular_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "unused-profile" / "instance"
            with mock.patch.object(QStandardPaths, "writableLocation", side_effect=AssertionError("regular path read")):
                if sys.platform.startswith("linux"):
                    with self.assertRaisesRegex(NodeLifecycleError, "anchor maintenance"):
                        prepare_update(application_name="CommunityAI Multi-GPU Test", instance_data_dir=root)
                else:
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
                if mode == "volunteer" and sys.platform.startswith("linux"):
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(b"anchor maintenance", result.stderr)
                else:
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
                if sys.platform.startswith("linux"):
                    self.assertIsNone(processes["volunteer"].poll())
                    self.assertFalse((roots["volunteer"] / "stopped.txt").exists())
                else:
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
