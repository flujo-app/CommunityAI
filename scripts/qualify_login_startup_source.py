"""Offscreen source-Qt sign-in checkbox regression; never frozen acceptance.

The native Windows replay redirects registration to a new, non-autostart HKCU
qualification key. It never changes the user's actual CommunityAI Run entry,
creates a credential, starts a node, or presents a visible window.
"""

import argparse
import hashlib
import json
import os
import sys
import types
import uuid
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "desktop" / "src"))


def code_objects(code):
    yield code
    for value in code.co_consts:
        if isinstance(value, types.CodeType):
            yield from code_objects(value)


def inspect_frozen_hooks(executable):
    """Read embedded code constants without loading or modifying the application."""
    from PyInstaller.archive.readers import CArchiveReader

    archive = CArchiveReader(executable).open_embedded_archive("PYZ.pyz")
    resource = archive.extract("communityai_desktop.resource_playthrough")
    initializer = next(code for code in code_objects(resource) if code.co_name == "__init__")
    actions = next(value for value in initializer.co_consts if value == ("observe", "limits", "start", "pause"))
    parser = next(
        code for code in code_objects(archive.extract("communityai_desktop.app")) if code.co_name == "build_parser"
    )
    return {
        "frozen_executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
        "embedded_resource_actions": list(actions),
        "embedded_cli_options": [
            value for value in parser.co_consts if isinstance(value, str) and value.startswith("--")
        ],
        "read_only_bytecode_inspection": True,
        "frozen_checkbox_exercised": False,
    }


def checkbox_session(read_enabled, write_enabled, *, click=False):
    # An inherited native Qt setting must not open a test window. If another
    # caller already constructed a native QApplication, refuse before run().
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QStyle, QStyleOptionButton

    from communityai_desktop.pyside_shell import run

    application = QApplication.instance() or QApplication([])
    if application.platformName() != "offscreen":
        raise RuntimeError("Source checkbox regression requires an offscreen QApplication")
    observed = {}
    errors = []
    session_timers = []

    class Automation:
        def install(self, window, app, qt):
            watchdog = qt["QTimer"](window)
            exercise_timer = qt["QTimer"](window)
            session_timers.extend((watchdog, exercise_timer))
            watchdog.setSingleShot(True)
            exercise_timer.setSingleShot(True)

            def finish(code):
                for timer in session_timers:
                    timer.stop()
                window.close()
                app.exit(code)

            def expired():
                errors.append(TimeoutError("Source checkbox callback exceeded its deadline"))
                finish(1)

            def exercise():
                try:
                    checkbox = window.login_startup_toggle
                    more = [
                        button
                        for button in window.findChildren(QPushButton)
                        if button.accessibleName() == "More sharing settings"
                    ]
                    if len(more) != 1 or more[0].isChecked():
                        raise AssertionError("Sharing settings must start collapsed")
                    observed["settings_initially_collapsed"] = not checkbox.isVisible()
                    window.pages.currentWidget().ensureWidgetVisible(more[0])
                    app.processEvents()
                    QTest.mouseClick(more[0], Qt.LeftButton)
                    if not more[0].isChecked():
                        raise AssertionError("Sharing settings did not expand after clicking their control")
                    window.pages.currentWidget().ensureWidgetVisible(checkbox)
                    app.processEvents()
                    if not checkbox.isVisible():
                        raise AssertionError("Sign-in checkbox is not visible on the offscreen Sharing page")
                    observed.update(
                        initial_checked=checkbox.isChecked(),
                        initial_enabled=checkbox.isEnabled(),
                        initial_detail=window.login_startup_detail.text(),
                        checkbox_visible=checkbox.isVisible(),
                    )
                    if click:
                        if not checkbox.isEnabled():
                            raise AssertionError("Sign-in checkbox is disabled")
                        # Checkbox hit regions differ by style; a stretched
                        # widget's center can lie outside its clickable label.
                        option = QStyleOptionButton()
                        checkbox.initStyleOption(option)
                        indicator = checkbox.style().subElementRect(QStyle.SE_CheckBoxIndicator, option, checkbox)
                        hit_region = checkbox.style().subElementRect(QStyle.SE_CheckBoxClickRect, option, checkbox)
                        position = indicator.center()
                        if (
                            not indicator.isValid()
                            or not checkbox.rect().contains(position)
                            or not hit_region.contains(position)
                        ):
                            raise AssertionError("The checkbox style did not expose a valid indicator click target")
                        QTest.mouseClick(checkbox, Qt.LeftButton, pos=position)
                    observed.update(
                        final_checked=checkbox.isChecked(),
                        final_detail=window.login_startup_detail.text(),
                        final_detail_visible=window.login_startup_detail.isVisible(),
                        qt_platform=app.platformName(),
                    )
                except BaseException as exc:
                    errors.append(exc)
                finally:
                    finish(0)

            # Cancel both timers when this session ends. Static singleShot quit
            # timers survive a fast run and can interrupt a later QApplication
            # event loop in the same unittest process.
            watchdog.timeout.connect(expired)
            exercise_timer.timeout.connect(exercise)
            watchdog.start(3_000)
            exercise_timer.start(50)

    def offline():
        raise RuntimeError("Isolated offline UI regression; no node or fixture telemetry")

    with (
        patch("communityai_desktop.pyside_shell.login_startup_enabled", read_enabled),
        patch("communityai_desktop.pyside_shell.set_login_startup", write_enabled),
        patch.object(QMessageBox, "warning") as warning,
    ):
        try:
            exit_code = run(
                connect=offline,
                single_instance=False,
                screenshot_page=2,
                qualification_automation=Automation(),
            )
        finally:
            for timer in session_timers:
                timer.stop()
                timer.timeout.disconnect()
        observed["warning_count"] = warning.call_count
    if errors:
        raise errors[0]
    if exit_code != 0 or "final_checked" not in observed:
        raise RuntimeError("Source checkbox callback did not complete")
    return observed


def run_native_windows(executable, output):
    if os.name != "nt":
        raise RuntimeError("Native registry replay requires Windows")
    import ctypes
    import winreg

    from communityai_desktop import startup

    if ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("Run the checkbox regression as a non-elevated ordinary user")
    output.mkdir(parents=True, exist_ok=False)
    real_key = startup.WINDOWS_RUN_KEY
    value_name = startup.WINDOWS_VALUE_NAME
    private_key = r"Software\CommunityAI\Qualification" + "\\" + uuid.uuid4().hex

    def read_value(key_path):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                return winreg.QueryValueEx(key, value_name)
        except FileNotFoundError:
            return None

    original = read_value(real_key)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, private_key):
            raise RuntimeError("The unique qualification key is unexpectedly occupied")
    except FileNotFoundError:
        pass
    result = {
        "result": "failed",
        "scope": "source-Qt-checkbox-with-native-isolated-registry",
        "frozen_gate_passed": False,
        "visible_windows": False,
        "native_credentials_created": False,
        "nodes_workers_or_inference_started": False,
        "frozen_hook_inspection": inspect_frozen_hooks(executable),
    }
    try:
        with patch.object(startup, "WINDOWS_RUN_KEY", private_key):
            enabled = checkbox_session(startup.login_startup_enabled, startup.set_login_startup, click=True)
            assert enabled["initial_checked"] is False and enabled["final_checked"] is True
            assert enabled["final_detail"] == "CommunityAI will open when you sign in."
            assert startup.login_startup_enabled()
            reopened = checkbox_session(startup.login_startup_enabled, startup.set_login_startup, click=True)
            assert reopened["initial_checked"] is True and reopened["final_checked"] is False
            assert reopened["initial_detail"] == "" and reopened["final_detail"] == "Automatic opening is off."
            assert not startup.login_startup_enabled() and read_value(private_key) is None
            result["enable"] = enabled
            result["reopen_and_disable"] = reopened
        result["result"] = "passed-source-only"
    finally:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, private_key)
        except FileNotFoundError:
            pass
        result["qualification_registry_key_removed"] = True
        result["real_login_entry_unchanged"] = read_value(real_key) == original
        if not result["real_login_entry_unchanged"]:
            result["result"] = "failed-concurrent-real-login-entry-change"
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--desktop", type=Path, required=True, help="Read-only frozen-hook inspection target")
    parser.add_argument("--output", type=Path, required=True, help="New evidence directory")
    arguments = parser.parse_args()
    facts = run_native_windows(arguments.desktop.resolve(strict=True), arguments.output.resolve())
    print(json.dumps({"result": facts["result"], "frozen_gate_passed": False}))
