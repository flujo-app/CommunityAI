import importlib.util
import os
import unittest
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "qualify_login_startup_source.py"
spec = importlib.util.spec_from_file_location("qualify_login_startup_source", SCRIPT)
replay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay)

from communityai_desktop.startup import LoginStartupError


class LoginStartupUiTests(unittest.TestCase):
    def test_literal_checkbox_reflects_saved_state_after_window_recreation(self):
        state = {"enabled": False, "writes": []}

        def save(enabled):
            state["writes"].append(enabled)
            state["enabled"] = enabled

        first = replay.checkbox_session(lambda: state["enabled"], save, click=True)
        second = replay.checkbox_session(lambda: state["enabled"], save, click=True)
        self.assertFalse(first["initial_checked"])
        self.assertTrue(first["final_checked"])
        self.assertTrue(second["initial_checked"])
        self.assertFalse(second["final_checked"])
        self.assertEqual(second["initial_detail"], "Enabled for this user")
        self.assertEqual(second["final_detail"], "Off")
        self.assertEqual(state["writes"], [True, False])
        self.assertEqual(first["qt_platform"], "offscreen")
        self.assertEqual(second["qt_platform"], "offscreen")

    def test_failed_native_write_reverts_checkbox_and_reports_failure(self):
        def denied(enabled):
            raise LoginStartupError("test registry access denied")

        result = replay.checkbox_session(lambda: False, denied, click=True)
        self.assertFalse(result["initial_checked"])
        self.assertFalse(result["final_checked"])
        self.assertEqual(result["warning_count"], 1)
        self.assertIn("Could not change login startup", result["final_detail"])

    def test_unreadable_startup_registration_disables_the_checkbox(self):
        def denied():
            raise LoginStartupError("test registration unreadable")

        def never_write(enabled):
            raise AssertionError("A disabled control must not write")

        result = replay.checkbox_session(denied, never_write)
        self.assertFalse(result["initial_enabled"])
        self.assertFalse(result["final_checked"])
        self.assertTrue(result["initial_detail"].startswith("Unavailable:"))
        self.assertEqual(result["warning_count"], 0)
