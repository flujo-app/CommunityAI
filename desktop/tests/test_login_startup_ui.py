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
        self.assertEqual(second["initial_detail"], "")
        self.assertEqual(first["final_detail"], "CommunityAI will open when you sign in.")
        self.assertEqual(second["final_detail"], "Automatic opening is off.")
        self.assertEqual(state["writes"], [True, False])
        self.assertEqual(first["qt_platform"], "offscreen")
        self.assertEqual(second["qt_platform"], "offscreen")
        self.assertTrue(first["checkbox_visible"])
        self.assertTrue(second["checkbox_visible"])
        self.assertTrue(first["settings_initially_collapsed"])
        self.assertTrue(second["settings_initially_collapsed"])
        self.assertTrue(first["final_detail_visible"])
        self.assertTrue(second["final_detail_visible"])

    def test_failed_native_write_reverts_checkbox_and_reports_failure(self):
        def denied(enabled):
            raise LoginStartupError("test registry access denied")

        result = replay.checkbox_session(lambda: False, denied, click=True)
        self.assertFalse(result["initial_checked"])
        self.assertFalse(result["final_checked"])
        self.assertEqual(result["warning_count"], 1)
        self.assertEqual(result["final_detail"], "Could not save this setting. Try again.")
        self.assertTrue(result["checkbox_visible"])
        self.assertTrue(result["final_detail_visible"])

    def test_unreadable_startup_registration_disables_the_checkbox(self):
        def denied():
            raise LoginStartupError("test registration unreadable")

        def never_write(enabled):
            raise AssertionError("A disabled control must not write")

        result = replay.checkbox_session(denied, never_write)
        self.assertFalse(result["initial_enabled"])
        self.assertFalse(result["final_checked"])
        self.assertEqual(result["initial_detail"], "Sign-in settings could not be read.")
        self.assertEqual(result["warning_count"], 0)
        self.assertTrue(result["checkbox_visible"])
