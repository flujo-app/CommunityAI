"""Real Qt policy dialog and production client codec; no physical hardware or sockets."""

import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import test_gpu_sharing_integration as gpu_fixture
from PySide6.QtCore import QPoint, QRect, QTimer
from PySide6.QtGui import QFontDatabase, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QLineEdit, QPlainTextEdit, QScrollArea

from communityai_desktop.client import NodeClientError, _normalize_policy
from communityai_desktop.presentation import sharing_reason


def contrast(foreground, background):
    def luminance(color):
        channels = (color.redF(), color.greenF(), color.blueF())
        linear = [value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in channels]
        return sum(value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722)))

    values = sorted((luminance(foreground), luminance(background)))
    return (values[1] + 0.05) / (values[0] + 0.05)


class HostMemoryClientTests(unittest.TestCase):
    def test_optional_allowance_accepts_legacy_shape_and_explicit_clear(self):
        _, transport = gpu_fixture.controller()
        legacy = dict(transport.node.policy)
        self.assertNotIn("max_host_memory", _normalize_policy(legacy))
        for value in ("16GiB", None):
            with self.subTest(value=value):
                normalized = _normalize_policy({**legacy, "max_host_memory": value})
                self.assertEqual(normalized["max_host_memory"], value)
                self.assertEqual({key: item for key, item in normalized.items() if key != "max_host_memory"}, legacy)

    def test_malformed_allowance_is_rejected_before_any_transport_write(self):
        desktop, transport = gpu_fixture.controller()
        for value in (True, 123, "", " ", "16GiB\n", "x" * 65):
            with self.subTest(value=value), self.assertRaises(NodeClientError):
                desktop.update_contribution_policy(
                    {**transport.node.policy, "max_host_memory": value},
                    expected_revision=gpu_fixture.revision(1),
                )
        self.assertFalse(transport.writes)


class HostMemorySettingsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])
        # The Windows offscreen plugin does not enumerate the installed system
        # fonts. Use the actual shell font for meaningful wrapping/contrast checks.
        font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / "segoeui.ttf"
        if os.name == "nt" and font.is_file():
            QFontDatabase.addApplicationFont(str(font))

    def exercise_dialog(self, initial, edited, expected, *, omitted=False):
        phase = [0]
        errors = []

        def tick(window, transport, desktop):
            if phase[0] == 0:
                phase[0] = 1
                if initial is not None:
                    transport.node.policy["max_host_memory"] = initial
                window._render(desktop.snapshot())
                self.assertTrue(window.edit_policy_button.isEnabled())

                def edit_and_save():
                    dialog = window.findChild(QDialog, "sharingPolicyDialog")
                    try:
                        self.assertIsNotNone(dialog)
                        self.assertTrue(dialog.isVisible())
                        scroll = dialog.findChild(QScrollArea, "sharingPolicyScroll")
                        buttons = dialog.findChild(QDialogButtonBox, "sharingPolicyButtons")
                        viewport = QRect(scroll.mapTo(dialog, QPoint(0, 0)), scroll.size())
                        footer = QRect(buttons.mapTo(dialog, QPoint(0, 0)), buttons.size())
                        self.assertFalse(viewport.intersects(footer))
                        self.assertLessEqual(dialog.height(), dialog.screen().availableGeometry().height())
                        self.assertTrue(dialog.rect().contains(footer))
                        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
                        schedule = dialog.findChild(QPlainTextEdit, "policy_schedule")
                        schedule_rect = QRect(schedule.mapTo(dialog, QPoint(0, 0)), schedule.size())
                        self.assertFalse(schedule_rect.intersects(footer))
                        self.assertLessEqual(schedule_rect.bottom(), viewport.bottom())
                        scroll.verticalScrollBar().setValue(0)
                        background = dialog.palette().color(QPalette.ColorRole.Window)
                        for label in dialog.findChildren(QLabel):
                            if label.text():
                                self.assertGreaterEqual(
                                    contrast(label.palette().color(QPalette.ColorRole.WindowText), background), 4.5
                                )
                        for editor in dialog.findChildren(QPlainTextEdit):
                            self.assertGreaterEqual(
                                contrast(
                                    editor.palette().color(QPalette.ColorRole.Text),
                                    editor.palette().color(QPalette.ColorRole.Base),
                                ),
                                4.5,
                            )
                        editor = dialog.findChild(QLineEdit, "policy_max_host_memory")
                        self.assertEqual(editor.text(), initial or "")
                        self.assertEqual(editor.accessibleName(), "Shared host RAM allowance")
                        detail_label = dialog.findChild(QLabel, "policy_host_memory_help")
                        self.assertLessEqual(detail_label.heightForWidth(detail_label.width()), detail_label.height())
                        detail = detail_label.text()
                        self.assertIn("all contribution workers", detail)
                        self.assertIn("separate from GPU", detail)
                        self.assertIn("NVIDIA GPUs selected in CommunityAI", detail)
                        self.assertIn("blocks manually configured workers", detail)
                        self.assertIn("no default allowance", detail)
                        self.assertIn("not an OS memory cap", detail)
                        editor.setText(edited)
                        dialog.findChild(QDialogButtonBox, "sharingPolicyButtons").button(
                            QDialogButtonBox.StandardButton.Save
                        ).click()
                    except BaseException as exc:
                        errors.append(exc)
                        if dialog is not None:
                            dialog.reject()

                QTimer.singleShot(10, edit_and_save)
                window.edit_policy_button.click()
            elif errors:
                raise errors[0]
            elif transport.writes:
                self.assertEqual(len(transport.writes), 1)
                method, path, payload = transport.writes[0]
                self.assertEqual((method, path), ("PUT", "/control/v1/contribution-policy"))
                self.assertEqual(payload["expected_config_revision"], gpu_fixture.revision(1))
                if omitted:
                    self.assertNotIn("max_host_memory", payload["policy"])
                else:
                    self.assertEqual(payload["policy"]["max_host_memory"], expected)
                self.assertEqual(payload["policy"]["max_vram"], "50%")
                self.assertFalse(payload["policy"]["sharing_enabled"])
                self.assertTrue(all(state == "paused" for _, state in transport.node.worker_states.values()))
                return True
            return False

        gpu_fixture.GpuSharingQtIntegrationTests.exercise(self, tick)

    def test_operator_can_save_explicit_shared_allowance_from_unset_dialog(self):
        self.exercise_dialog(None, "24GiB", "24GiB")

    def test_clearing_existing_allowance_sends_explicit_null(self):
        self.exercise_dialog("24GiB", "", None)

    def test_leaving_legacy_allowance_blank_does_not_invent_consent_or_add_key(self):
        self.exercise_dialog(None, "", None, omitted=True)

    def test_host_capacity_and_cleanup_messages_are_visible_and_do_not_expose_private_details(self):
        cases = (
            ("set a shared host RAM allowance before starting managed GPU sharing", "Set a shared host RAM allowance"),
            (
                "shared host RAM admission currently requires managed GPU workers",
                "Clear it to use manually configured sharing workers",
            ),
            ("shared host memory or cache storage is unavailable for this worker", "Close other apps, free space"),
            ("worker is waiting for an aggregate resource reservation", "waiting for RAM and storage checks"),
            ("worker resource release is incomplete; retry cleanup", "Choose Pause to retry cleanup"),
            ("worker process creation is uncertain; resource reservation remains held", "Keep sharing paused"),
            (
                "shared resource admission is unavailable; retained reservations require verified cleanup",
                "Keep sharing paused",
            ),
            ("automatic placement is waiting for verified shared resource estimates", "Checking how much RAM"),
        )

        def tick(window, transport, desktop):
            original_worker = transport.node.contribution_worker
            transport.node.policy["sharing_enabled"] = True
            for reason, instruction in cases:
                with self.subTest(reason=reason):

                    def blocked_worker(worker_id):
                        worker = original_worker(worker_id)
                        if worker_id == "worker-b":
                            worker["desired_running"] = True
                            worker["resources"].update(admitted=False, reason=reason)
                        return worker

                    transport.node.contribution_worker = blocked_worker
                    snapshot = desktop.snapshot()
                    blocked = next(worker for worker in snapshot["workers"] if worker["id"] == "worker-b")
                    self.assertEqual(blocked["blocked_reason"], reason)
                    self.assertIn(instruction, blocked["display_status"])
                    window._sharing_error = None
                    window._render(snapshot)
                    for widget in (window.sharing_detail, window.home_sharing_detail):
                        self.assertIn(instruction, widget.text())
                        self.assertNotIn("GPU memory", widget.text())
                        self.assertNotIn("reservation", widget.text())
                    window._sharing_action_failed(reason + " C:/private-node/generations.json token-secret")
                    for widget in (window.sharing_detail, window.home_sharing_detail):
                        self.assertIn(instruction, widget.text())
                        self.assertIn(instruction, widget.toolTip())
                        self.assertNotIn("private-node", widget.toolTip())
                        self.assertNotIn("token-secret", widget.text() + widget.toolTip())
            self.assertFalse(transport.writes)
            return True

        gpu_fixture.GpuSharingQtIntegrationTests.exercise(self, tick)

    def test_invalid_host_size_and_gpu_memory_keep_distinct_instructions(self):
        for reason in (
            "contribution_policy.max_host_memory must be a positive byte size such as 20GiB",
            "Local node contribution policy has invalid max host memory",
        ):
            self.assertEqual(sharing_reason(reason), "Enter a positive shared host RAM size, such as 16GiB.")
        self.assertEqual(
            sharing_reason("selected blocks exceed the VRAM budget"),
            "Not enough GPU memory. Increase the memory limit or close another app.",
        )
