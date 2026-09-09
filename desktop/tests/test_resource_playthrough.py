import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QPushButton, QWidget

from communityai_desktop.app import main
from communityai_desktop.resource_controls import ResourceControls
from communityai_desktop.resource_playthrough import ResourcePlaythrough


class ResourcePlaythroughTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_home_start_observes_immediate_feedback_without_legacy_opt_in_dialog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {"steps": [{"action": "start"}], "timeout_seconds": 30, "acknowledgement": str(root / "ack.json")}
                ),
                encoding="utf-8",
            )
            playback = ResourcePlaythrough(plan, root / "evidence.json")
            window = QWidget()
            window._controller = object()
            window._busy = 0
            window._page_buttons = [QPushButton(str(index), window) for index in range(3)]
            window.home_share_button = QPushButton("Start sharing", window)
            window.master_share_button = QPushButton("Start sharing", window)
            window.resource_controls = ResourceControls(window)
            contribution = {"editable": True, "intent_enabled": False, "policy": {"sharing_enabled": False}}
            window._snapshot = {"contribution": contribution}
            home_clicks = []
            window._page_buttons[0].clicked.connect(lambda: home_clicks.append(True))

            def start():
                for button in (window.home_share_button, window.master_share_button):
                    button.setText("Starting…")
                    button.setEnabled(False)

            window.home_share_button.clicked.connect(start)
            playback.install(window, self.application, {"QTimer": QTimer})
            playback.timer.stop()
            playback.tick()
            self.assertEqual(playback.phase, "observe")
            self.assertEqual(playback.immediate_feedback, "Starting…")
            self.assertEqual(home_clicks, [True])
            contribution.update(intent_enabled=True, policy={"sharing_enabled": True})
            playback.tick()
            self.assertEqual(playback.phase, "acknowledgement")
            self.assertEqual(playback.result["steps"][0]["immediate_feedback"], "Starting…")
            window.close()

    def test_saved_full_memory_accepts_capped_slider_position(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {
                        "steps": [{"action": "observe", "vram_percent": 100, "processing_percent": 100}],
                        "timeout_seconds": 30,
                        "acknowledgement": str(root / "ack.json"),
                    }
                ),
                encoding="utf-8",
            )
            playback = ResourcePlaythrough(plan, root / "evidence.json")
            window = QWidget()
            window._controller = object()
            window._busy = 0
            window._page_buttons = [QPushButton(str(index), window) for index in range(3)]
            window.resource_controls = ResourceControls(window)
            contribution = {
                "editable": True,
                "config_revision": "test",
                "intent_enabled": False,
                "policy": {"sharing_enabled": False, "max_vram": "100%", "max_processing_percent": 100},
                "vram_pool_bytes": 8 * 1024**3,
                "vram_bytes": int(4.5 * 1024**3),
                "vram_available_bytes": int(4.5 * 1024**3),
            }
            window._snapshot = {"contribution": contribution}
            window.resource_controls.set_state(contribution)
            playback.install(window, self.application, {"QTimer": QTimer})
            playback.timer.stop()
            playback.tick()
            playback.tick()
            self.assertEqual(playback.phase, "acknowledgement")
            self.assertEqual(window.resource_controls.sliders["max_vram"].value(), 57)
            self.assertEqual(playback.result["steps"][0]["saved_vram"], "100%")
            window.close()

    def test_explicit_qualification_cannot_activate_users_running_desktop(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plan = root / "plan.json"
            plan.write_text(
                json.dumps(
                    {"steps": [{"action": "start"}], "timeout_seconds": 30, "acknowledgement": str(root / "ack.json")}
                ),
                encoding="utf-8",
            )
            with patch("communityai_desktop.pyside_shell.run", return_value=0) as run:
                self.assertEqual(
                    main(
                        [
                            "--no-manage-node",
                            "--resource-ui-playthrough",
                            str(plan),
                            "--resource-ui-evidence",
                            str(root / "evidence.json"),
                        ]
                    ),
                    0,
                )
                self.assertFalse(run.call_args.kwargs["single_instance"])
                self.assertIsInstance(run.call_args.kwargs["qualification_automation"], ResourcePlaythrough)
            with patch("communityai_desktop.pyside_shell.run", return_value=0) as run:
                self.assertEqual(main(["--no-manage-node"]), 0)
                self.assertTrue(run.call_args.kwargs["single_instance"])
