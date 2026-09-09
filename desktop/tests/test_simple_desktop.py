import copy
import os
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from communityai_desktop.acceptance import fake_node
from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from communityai_desktop.presentation import model_summary, sharing_summary


class SimpleSummaryTests(unittest.TestCase):
    def test_local_choice_explains_fallback_and_respects_explicit_local_mode(self):
        snapshot = {"auto_selection": {"model": "Qwen3.5-0.8B-Local", "source": "local"}}
        name, reason, location = model_summary(snapshot)
        self.assertEqual(name, "Qwen3.5 0.8B")
        self.assertIn("community cannot answer right now", reason)
        self.assertEqual(location, "On this computer")
        snapshot["inference_mode"] = "local_only"
        self.assertEqual(model_summary(snapshot)[1], "You chose to use only this computer.")

    def test_live_process_downloading_is_not_reported_as_sharing(self):
        snapshot = {
            "contribution": {"intent_enabled": True},
            "workers": [
                {
                    "model": "Qwen",
                    "state": "running",
                    "sharing_active": False,
                    "desired_running": True,
                    "download_progress": {"state": "downloading"},
                }
            ],
        }
        self.assertEqual(sharing_summary(snapshot)[0], "Downloading for sharing")

    def test_paused_worker_can_resume_and_automatic_model_is_not_shown_as_a_name(self):
        snapshot = {
            "contribution": {"intent_enabled": True},
            "workers": [
                {"model": "auto", "operator_paused": True, "state": "paused", "placement": {"automatic": True}}
            ],
        }
        self.assertEqual(sharing_summary(snapshot), ("Sharing is paused", "", "paused"))
        snapshot["workers"][0].update(operator_paused=False, sharing_active=True)
        self.assertEqual(sharing_summary(snapshot), ("Sharing is on", "Helping the community.", "running"))


class SimpleDesktopInteractionTests(unittest.TestCase):
    def test_start_has_immediate_feedback_and_polling_does_not_disable_controls(self):
        self._exercise_poll_race(False)

    def test_stale_poll_error_does_not_cancel_a_successful_start(self):
        self._exercise_poll_race(True)

    def _exercise_poll_race(self, fail_poll):
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QLabel

        from communityai_desktop.pyside_shell import run

        with fake_node(all_workers_paused=True) as (url, token):
            state = DesktopController(NodeClient(url, token)).snapshot()
        state["hardware"] = {
            "cpu_name": "Test processor",
            "gpu_name": "Test graphics card",
            "gpu_total_bytes": 8 * 1024**3,
            "inference_device": "cuda:0",
        }
        state["contribution"].update(
            intent_enabled=False,
            enabled=False,
            vram_bytes=4 * 1024**3,
            vram_pool_bytes=8 * 1024**3,
            vram_available_bytes=4 * 1024**3,
            processing_percent=100,
        )
        state["contribution"]["policy"].update(sharing_enabled=False, max_processing_percent=100)
        action_started = threading.Event()
        allow_action = threading.Event()
        poll_started = threading.Event()
        allow_poll = threading.Event()
        confirmation_started = threading.Event()
        allow_confirmation = threading.Event()
        observed = []
        errors = []

        class Controller:
            block_poll = False

            def snapshot(self):
                captured = copy.deepcopy(state)
                if self.block_poll:
                    self.block_poll = False
                    poll_started.set()
                    allow_poll.wait(5)
                    if fail_poll:
                        raise RuntimeError("Old request lost its connection")
                elif action_started.is_set():
                    confirmation_started.set()
                    allow_confirmation.wait(5)
                return captured

            def set_sharing_enabled(self, enabled):
                action_started.set()
                allow_action.wait(5)
                state["contribution"]["intent_enabled"] = enabled
                state["contribution"]["policy"]["sharing_enabled"] = enabled
                for worker in state["workers"]:
                    worker["desired_running"] = enabled
                    worker["state"] = "paused"
                    worker["operator_paused"] = False
                return {"message": "Sharing enabled."}

        controller = Controller()

        class Automation:
            phase = 0

            def install(self, window, application, types):
                self.started = time.monotonic()
                self.timer = QTimer(window)
                self.timer.setInterval(15)

                def tick():
                    try:
                        if time.monotonic() - self.started > 8:
                            raise AssertionError("UI interaction did not finish")
                        if self.phase == 0:
                            if not window._snapshot.get("models"):
                                return
                            visible = [
                                item.text() for item in window.pages.widget(0).findChildren(QLabel) if item.isVisible()
                            ]
                            joined = "\n".join(visible)
                            for removed in (
                                "Ready to use",
                                "World regions",
                                "Your local AI is ready",
                                "Your downloads",
                                "Everything is connected",
                                "ENDPOINT URL",
                            ):
                                assert removed not in joined, removed
                            assert window.home_vram.text() == "4.0 GB of 8.0 GB"
                            assert window.home_gpu.text() == "Test graphics card"
                            controller.block_poll = True
                            window.refresh()
                            self.phase = 1
                        elif self.phase == 1 and poll_started.is_set():
                            assert window.home_share_button.isEnabled(), "Background polling disabled Start"
                            window.home_share_button.click()
                            assert window.home_share_button.text() == "Starting…"
                            assert window.master_share_button.text() == "Starting…"
                            assert not window.home_share_button.isEnabled()
                            observed.append("immediate-feedback")
                            self.phase = 2
                        elif self.phase == 2 and action_started.is_set():
                            allow_action.set()
                            allow_poll.set()
                            self.phase = 3
                        elif self.phase == 3 and confirmation_started.is_set():
                            assert window.home_share_button.text() == "Starting…"
                            assert not window.home_share_button.isEnabled(), "Start unlocked before state confirmation"
                            assert window._controller is controller, "Stale error disconnected the current controller"
                            observed.append("waiting-for-confirmation")
                            allow_confirmation.set()
                            self.phase = 4
                        elif self.phase == 4 and window.home_share_button.text() == "Pause sharing":
                            assert window._snapshot["contribution"]["intent_enabled"], "Stale poll overwrote Start"
                            assert window.master_share_button.text() == "Pause sharing"
                            observed.append("confirmed-intent")
                            window._snapshot_failed("Connection interrupted")
                            assert window.home_sharing_title.text() == "Checking sharing…"
                            assert not window.home_share_button.isEnabled()
                            assert not window.resource_controls.sliders["max_processing_percent"].isEnabled()
                            self.timer.stop()
                            application.quit()
                    except BaseException as exc:
                        errors.append(exc)
                        allow_action.set()
                        allow_poll.set()
                        allow_confirmation.set()
                        self.timer.stop()
                        application.quit()

                self.timer.timeout.connect(tick)
                self.timer.start()

        with patch("communityai_desktop.pyside_shell.login_startup_enabled", return_value=False):
            run(controller, single_instance=False, qualification_automation=Automation(), auto_close_seconds=10)
        if errors:
            raise errors[0]
        self.assertEqual(observed, ["immediate-feedback", "waiting-for-confirmation", "confirmed-intent"])


if __name__ == "__main__":
    unittest.main()
