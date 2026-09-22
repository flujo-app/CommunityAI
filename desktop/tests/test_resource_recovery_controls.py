"""Connected Qt recovery copy, Start guarding and reachable Pause/settings."""

import copy
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer

from communityai_desktop.acceptance import fake_node
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from communityai_desktop.presentation import recovery_reason


def test_recovery_keeps_off_and_pause_truth_while_gating_only_start():
    # Other shell tests reuse QApplication and can leave delayed quit timers.
    # A fresh process gives this real UI exercise its own event loop. Invoke
    # pytest so its assertions remain active when the parent runs under -O.
    command = [sys.executable]
    if sys.flags.optimize:
        command.append("-O")
    command.extend(
        [
            "-m",
            "pytest",
            str(Path(__file__).resolve()) + "::_exercise_recovery_controls",
            "-o",
            "python_functions=_exercise_recovery_controls",
            "-q",
            "-p",
            "no:cacheprovider",
            "--tb=short",
            "--disable-warnings",
        ]
    )
    environment = dict(os.environ, PYTEST_ADDOPTS="")
    environment.pop("PYTEST_CURRENT_TEST", None)
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=45,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout, "The isolated Qt exercise did not run: " + result.stdout + result.stderr


def _exercise_recovery_controls():
    from communityai_desktop.pyside_shell import run

    with fake_node(all_workers_paused=True) as (url, token):
        snapshot = DesktopController(NodeClient(url, token)).snapshot()
    snapshot["contribution"].update(intent_enabled=False, enabled=False)
    snapshot["contribution"]["policy"]["sharing_enabled"] = False
    for worker in snapshot["workers"]:
        worker.update(desired_running=False, operator_paused=True, sharing_active=False)
    errors, observed = [], []

    class Controller:
        def snapshot(self):
            return copy.deepcopy(snapshot)

    class Automation:
        def install(self, window, application, types):
            started = time.monotonic()
            self.timer = QTimer(window)

            def tick():
                try:
                    if not window._snapshot.get("models"):
                        assert time.monotonic() - started < 8
                        return
                    for recovery in (
                        {"state": "checking", "reason": "checking", "retryable": True},
                        {"state": "blocked", "reason": "legacy_state", "retryable": False},
                    ):
                        snapshot["contribution"]["recovery"] = recovery
                        window._render(copy.deepcopy(snapshot))
                        assert window.home_sharing_title.text() == "Sharing is off"
                        assert window.home_sharing_detail.text() == recovery_reason(recovery)
                        assert window.home_share_button.text() == "Start sharing"
                        assert not window.home_share_button.isEnabled()
                        assert not window.master_share_button.isEnabled()
                        assert window.edit_policy_button.isEnabled(), "Recovery must not lock saved settings"
                        assert window._controller is not None
                        observed.append(recovery["reason"])
                    snapshot["contribution"].update(intent_enabled=True)
                    snapshot["contribution"]["policy"]["sharing_enabled"] = True
                    for worker in snapshot["workers"]:
                        worker.update(operator_paused=False, desired_running=True)
                    window._render(copy.deepcopy(snapshot))
                    assert window.home_share_button.text() == "Pause sharing"
                    assert window.home_share_button.isEnabled() and window.master_share_button.isEnabled()
                    observed.append("pause-reachable")
                    for worker in snapshot["workers"]:
                        worker.update(operator_paused=True, desired_running=False)
                    window._render(copy.deepcopy(snapshot))
                    assert window.home_sharing_title.text() == "Sharing is paused"
                    assert not window.home_share_button.isEnabled()
                    snapshot["contribution"]["recovery"] = {"state": "ready", "reason": "none", "retryable": False}
                    window._render(copy.deepcopy(snapshot))
                    assert window.home_share_button.isEnabled()
                    assert window.home_sharing_title.text() == "Sharing is paused"
                    assert window.home_share_button.text() == "Start sharing"
                    assert all(not w["desired_running"] for w in window._snapshot["workers"])
                    observed.append("ready-keeps-paused")
                    self.timer.stop()
                    application.quit()
                except BaseException as exc:
                    errors.append(exc)
                    self.timer.stop()
                    application.quit()

            self.timer.timeout.connect(tick)
            self.timer.start(20)

    with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
        run(Controller(), single_instance=False, qualification_automation=Automation(), auto_close_seconds=10)
    if errors:
        raise errors[0]
    assert observed == ["checking", "legacy_state", "pause-reachable", "ready-keeps-paused"]
