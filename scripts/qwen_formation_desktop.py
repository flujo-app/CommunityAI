"""Real Qt window attached to the formation test's isolated packaged node.

Uses the same Qt automation hook as Gate 13. It observes live selections and
clicks the production local-only control; no fake node or mocked controller.
"""

import argparse
import json
import time
from pathlib import Path

from communityai_desktop.client import NodeClient
from communityai_desktop.controller import DesktopController
from communityai_desktop.pyside_shell import run
from qwen_formation_node import selected, write


class FormationDesktop:
    def __init__(self, root):
        self.root = root
        self.completed = set()
        self.clicked = set()
        self.policy_opened = set()
        self.baseline_paused = set()

    def install(self, window, application, qt):
        self.window, self.application = window, application
        self.qt = qt
        original_failure = window._sharing_action_failed

        def sharing_failed(message):
            write(self.root / "desktop-error.json", {"error": "Production desktop rejected sharing: " + str(message)})
            original_failure(message)

        window._sharing_action_failed = sharing_failed
        self.timer = qt["QTimer"](window)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.tick)
        self.timer.start()

    def tick(self):
        try:
            if (self.root / "desktop-stop").exists():
                self.application.quit()
                return
            window = self.window
            if window._controller is None or window._busy:
                return
            path = self.root / "desktop-command.json"
            if not path.exists():
                return
            action = json.loads(path.read_text())
            identity = action["id"]
            if identity in self.completed:
                return
            if action["action"] == "start-sharing":
                window._page_buttons[2].click()
                if not window._snapshot.get("contribution", {}).get("policy", {}).get("sharing_enabled"):
                    if identity not in self.policy_opened and window.edit_policy_button.isEnabled():
                        self.policy_opened.add(identity)
                        self.qt["QTimer"].singleShot(100, self.enable_policy)
                        window.edit_policy_button.click()
                    return
                if identity not in self.clicked:
                    # Gate 13 saves policy first, then normalizes an automatic
                    # start through the real per-model Pause control. Keep the
                    # persistent startup setting intact for restart recovery.
                    if window._snapshot.get("contribution", {}).get("intent_enabled"):
                        if identity in self.baseline_paused:
                            return
                        desired = {w["model"] for w in window._snapshot.get("workers", []) if w.get("desired_running")}
                        matches = [
                            checkbox
                            for checkbox in window.findChildren(self.qt["QCheckBox"])
                            if checkbox.accessibleName() in {"Share compute with " + name for name in desired}
                            and checkbox.isChecked()
                        ]
                        if len(matches) != 1 or not matches[0].isEnabled():
                            return
                        self.baseline_paused.add(identity)
                        matches[0].click()
                        return
                    if not window.master_share_button.isEnabled():
                        return
                    if window.master_share_button.text() != "Start sharing":
                        return
                    window._page_buttons[2].click()
                    window.master_share_button.click()
                    self.clicked.add(identity)
                    return
                if not window._snapshot.get("contribution", {}).get("intent_enabled"):
                    return
            if action["action"] == "toggle" and identity not in self.clicked:
                if not window.inference_mode_button.isEnabled():
                    return
                window.inference_mode_button.click()
                self.clicked.add(identity)
                return
            if not selected(window._snapshot, action["source"]):
                return
            if action.get("inference_mode") and window._snapshot.get("inference_mode") != action["inference_mode"]:
                return
            window._show_page(1)
            screenshot = self.root / ("desktop-" + identity + ".png")
            if not window.grab().save(str(screenshot)):
                raise RuntimeError("Could not capture the real desktop window")
            write(
                self.root / ("desktop-response-" + identity + ".json"),
                {
                    "result": "passed",
                    "id": identity,
                    "source": action["source"],
                    "real_window_visible": window.isVisible(),
                    "button_clicked": identity in self.clicked,
                    "baseline_pause_clicked": identity in self.baseline_paused,
                    "selection": window._snapshot["auto_selection"],
                    "inference_mode": window._snapshot["inference_mode"],
                    "title": window.auto_selection_title.text(),
                    "detail": window.auto_selection_detail.text(),
                    "screenshot": screenshot.name,
                    "ui_runtime": "production Qt source",
                    "fake_node": False,
                },
            )
            self.completed.add(identity)
        except BaseException as exc:
            write(self.root / "desktop-error.json", {"error": f"{type(exc).__name__}: {exc}"})
            self.application.exit(1)

    def enable_policy(self):
        try:
            dialog = self.window.findChild(self.qt["QDialog"], "sharingPolicyDialog")
            if dialog is None:
                raise RuntimeError("Production sharing policy dialog did not open")
            dialog.findChild(self.qt["QCheckBox"], "policy_sharing_enabled").setChecked(True)
            buttons = dialog.findChild(self.qt["QDialogButtonBox"], "sharingPolicyButtons")
            buttons.button(self.qt["QDialogButtonBox"].StandardButton.Save).click()
        except BaseException as exc:
            write(self.root / "desktop-error.json", {"error": f"{type(exc).__name__}: {exc}"})
            self.application.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads((args.root / "config.json").read_text())
    key_path = args.root / "node/control-api.key"
    while not key_path.exists() and time.time() < config["expires_at_unix"]:
        time.sleep(1)
    client = NodeClient(f"http://127.0.0.1:{config.get('api_port', 8080)}", key_path.read_text().strip())
    try:
        code = (
            run(DesktopController(client), single_instance=False, qualification_automation=FormationDesktop(args.root))
            or 0
        )
    except BaseException as exc:
        write(args.root / "desktop-error.json", {"error": f"{type(exc).__name__}: {exc}"})
        raise
    raise SystemExit(code)
