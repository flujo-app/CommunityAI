"""Explicit, bounded qualification of the real packaged sharing controls.

The host runner observes the actual node between steps and acknowledges each
observation through a local file. No control credentials enter the UI evidence.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from communityai_desktop.gate13_playthrough import PlaythroughError, _regular_bytes


class ResourcePlaythrough:
    def __init__(self, plan_path: Path, evidence_path: Path):
        plan = json.loads(_regular_bytes(plan_path, 16384))
        if not isinstance(plan, dict) or set(plan) != {"steps", "timeout_seconds", "acknowledgement"}:
            raise PlaythroughError("Resource playthrough plan is invalid")
        steps = plan["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 24:
            raise PlaythroughError("Resource playthrough requires 1..24 steps")
        for step in steps:
            if not isinstance(step, dict) or step.get("action") not in ("observe", "limits", "start", "pause"):
                raise PlaythroughError("Resource playthrough action is invalid")
            fields = {"action"}
            if step["action"] in ("observe", "limits"):
                fields |= {"vram_percent", "processing_percent"}
                for field in ("vram_percent", "processing_percent"):
                    if type(step.get(field)) is not int or not 1 <= step[field] <= 100:
                        raise PlaythroughError("Resource playthrough percentages must be 1..100")
            if set(step) != fields:
                raise PlaythroughError("Resource playthrough step fields are invalid")
        timeout = plan["timeout_seconds"]
        if type(timeout) is not int or not 30 <= timeout <= 3600:
            raise PlaythroughError("Resource playthrough timeout must be 30..3600 seconds")
        acknowledgement = plan["acknowledgement"]
        if not isinstance(acknowledgement, str) or not Path(acknowledgement).is_absolute():
            raise PlaythroughError("Resource acknowledgement must be an absolute local path")
        self.steps = steps
        self.acknowledgement = Path(acknowledgement)
        self.evidence_path = Path(evidence_path)
        self.timeout = timeout
        self.index = 0
        self.phase = "ready"
        self.done = False
        self.started = time.monotonic()
        self.result = {
            "scope": "packaged-resource-controls",
            "frozen": bool(getattr(sys, "frozen", False)),
            "result": "running",
            "steps": [],
        }

    def install(self, window, application, qt):
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest

        self.window, self.application = window, application
        self.types = qt
        self.qt, self.test = Qt, QTest
        self.timer = qt["QTimer"](window)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.tick)
        self.timer.start()
        self.write()

    def write(self):
        temporary = self.evidence_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.result, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.evidence_path)

    def finish(self, error=None):
        self.done = True
        self.timer.stop()
        self.result["result"] = "passed" if error is None else "failed"
        if error:
            self.result["error"] = error
            self.result["phase"] = self.phase
            self.result["step_index"] = self.index
            self.result["blocked_reasons"] = self.window._snapshot.get("contribution", {}).get("blocked_reasons", [])
        self.window.grab().save(str(self.evidence_path.with_suffix(".png")))
        self.write()
        self.application.exit(0 if error is None else 2)

    def click(self, button):
        if not button.isEnabled():
            raise PlaythroughError("resource_control_disabled")
        self.test.mouseClick(button, self.qt.LeftButton)

    def tick(self):
        if self.done:
            return
        try:
            if time.monotonic() - self.started > self.timeout:
                self.finish("resource_playthrough_timed_out")
                return
            if self.phase == "acknowledgement":
                if self.acknowledgement.exists():
                    acknowledgement = json.loads(_regular_bytes(self.acknowledgement, 128))
                    if type(acknowledgement) is int and acknowledgement == self.index:
                        self.index += 1
                        self.phase = "ready"
                        if self.index == len(self.steps):
                            self.finish()
                return
            window = self.window
            if window._controller is None or window._busy:
                return
            controls = window.resource_controls
            contribution = window._snapshot.get("contribution", {})
            if not contribution.get("editable"):
                return
            step = self.steps[self.index]
            if self.phase == "ready":
                self.action_started = time.monotonic()
                self.click(window._page_buttons[0 if step["action"] in ("start", "pause") else 2])
                if step["action"] == "limits":
                    for field, key in (("max_vram", "vram_percent"), ("max_processing_percent", "processing_percent")):
                        slider = controls.sliders[field]
                        if not slider.isEnabled():
                            raise PlaythroughError("resource_slider_disabled")
                        if field == "max_vram" and step[key] != 100 and step[key] >= slider.maximum():
                            raise PlaythroughError("resource_vram_target_above_available_memory")
                        target = min(step[key], slider.maximum())
                        slider.setFocus()
                        self.test.keyClick(slider, self.qt.Key_Home)
                        for _ in range(target - 1):
                            self.test.keyClick(slider, self.qt.Key_Right)
                        if slider.value() != target:
                            raise PlaythroughError("resource_slider_value_mismatch")
                    self.click(controls.apply_button)
                elif step["action"] in ("start", "pause"):
                    expected = "Start sharing" if step["action"] == "start" else "Pause sharing"
                    if window.home_share_button.text() != expected:
                        raise PlaythroughError("resource_sharing_button_mismatch")
                    self.click(window.home_share_button)
                    pending_text = "Starting…" if step["action"] == "start" else "Stopping…"
                    confirmed_text = "Pause sharing" if step["action"] == "start" else "Start sharing"
                    for button in (window.home_share_button, window.master_share_button):
                        if button.text() not in (pending_text, confirmed_text):
                            raise PlaythroughError("resource_sharing_immediate_feedback_missing")
                        if button.text() == pending_text and button.isEnabled():
                            raise PlaythroughError("resource_sharing_pending_button_enabled")
                    self.immediate_feedback = window.home_share_button.text()
                self.phase = "observe"
                return
            policy = contribution.get("policy") or {}
            if step["action"] in ("observe", "limits"):
                expected = {
                    "max_vram": f"{step['vram_percent']}%",
                    "max_processing_percent": step["processing_percent"],
                }
                if any(policy.get(field) != value for field, value in expected.items()):
                    return
                if controls.sliders["max_vram"].value() != min(
                    step["vram_percent"], controls.sliders["max_vram"].maximum()
                ):
                    raise PlaythroughError("resource_display_mismatch")
                if controls.sliders["max_processing_percent"].value() != step["processing_percent"]:
                    raise PlaythroughError("processing_display_mismatch")
            elif bool(contribution.get("intent_enabled")) != (step["action"] == "start"):
                return
            self.result["steps"].append(
                {
                    "index": self.index,
                    **step,
                    "seconds": round(time.monotonic() - self.started, 3),
                    "action_seconds": round(time.monotonic() - self.action_started, 3),
                    "saved_vram": policy.get("max_vram"),
                    "saved_processing_percent": policy.get("max_processing_percent"),
                    "sharing_intent": bool(contribution.get("intent_enabled")),
                    "vram_display": controls.values["max_vram"].text(),
                    "processing_display": controls.values["max_processing_percent"].text(),
                    "message": controls.message.text()[:240],
                    **({"immediate_feedback": self.immediate_feedback} if step["action"] in ("start", "pause") else {}),
                }
            )
            window.grab().save(str(self.evidence_path.with_suffix(".png")))
            self.write()
            self.phase = "acknowledgement"
        except Exception as exc:
            self.finish(str(exc) if isinstance(exc, PlaythroughError) else "resource_playthrough_failed")
