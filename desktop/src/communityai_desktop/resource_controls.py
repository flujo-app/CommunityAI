"""The two sharing budget sliders, backed by the node's persisted policy."""

import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget


class ResourceControls(QWidget):
    apply_requested = Signal(object, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._draft = {}
        self._revision = None
        self._policy = {}
        self._editable = False
        self._busy = False
        self._vram_total = None
        self._vram_available = None
        self._vram_saved = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sliders = {}
        self.values = {}
        for field, title in (
            ("max_vram", "GPU memory"),
            ("max_processing_percent", "Computing"),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(title), 1)
            value = QLabel("100%")
            value.setObjectName(f"resource_{field}_value")
            row.addWidget(value)
            layout.addLayout(row)
            slider = QSlider(Qt.Horizontal)
            slider.setObjectName(f"resource_{field}")
            slider.setAccessibleName(title)
            slider.setRange(1, 100)
            slider.setValue(100)
            slider.setPageStep(10)
            slider.valueChanged.connect(lambda percent, name=field: self._changed(name, percent))
            layout.addWidget(slider)
            self.sliders[field] = slider
            self.values[field] = value
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setObjectName("bodyMuted")
        layout.addWidget(self.message)
        self.apply_button = QPushButton("Save changes")
        self.apply_button.setObjectName("applyResourceLimits")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        self._update_enabled()

    def _changed(self, field, percent):
        if field == "max_vram" and percent == self.sliders[field].maximum():
            # The top means all currently available sharing memory, including
            # after a future hardware or local-reserve change.
            percent = 100
        self._draft[field] = f"{percent}%" if field == "max_vram" else percent
        self.values[field].setText(self._vram_text(percent) if field == "max_vram" else f"{percent}%")
        self.message.setText("Unsaved changes")
        self._update_enabled()

    def _vram_text(self, percent=None):
        if self._vram_total is not None:
            amount = self._vram_saved
            if percent is not None:
                amount = self._vram_total * percent / 100
                if self._vram_available is not None:
                    amount = min(amount, self._vram_available)
            if amount is not None:
                return f"{amount / 1024**3:.1f} GB of {self._vram_total / 1024**3:.1f} GB"
        return "No GPU memory detected"

    def set_state(self, contribution, *, busy=False):
        revision = contribution.get("config_revision")
        if revision != self._revision:
            if self._draft:
                self.message.setText("Settings updated elsewhere. Showing saved values.")
            self._draft.clear()
        self._revision = revision
        self._policy = contribution.get("policy") or {}
        self._editable = contribution.get("editable", False) and isinstance(revision, str)
        self._busy = busy
        self._vram_total = contribution.get("vram_pool_bytes")
        self._vram_available = contribution.get("vram_available_bytes")
        self._vram_saved = contribution.get("vram_bytes")
        maximum = 100
        if self._vram_total and self._vram_available is not None:
            maximum = max(1, min(100, math.ceil(self._vram_available * 100 / self._vram_total)))
        for field, slider in self.sliders.items():
            raw = self._draft.get(field, self._policy.get(field, 100))
            if field == "max_vram":
                try:
                    percent = float(raw[:-1]) if isinstance(raw, str) and raw.endswith("%") else None
                    if percent is not None and (not math.isfinite(percent) or not 0 < percent <= 100):
                        percent = None
                except ValueError:
                    percent = None
                text = self._vram_text(percent)
                if self._vram_total is None and raw and percent is None:
                    text = str(raw)
            else:
                percent = raw
                text = f"{percent:g}%"
            slider.blockSignals(True)
            slider.setMaximum(maximum if field == "max_vram" else 100)
            slider.setValue(100 if percent is None else round(percent))
            slider.blockSignals(False)
            self.values[field].setText(text)
        self._update_enabled()

    def _update_enabled(self):
        supported = "max_processing_percent" in self._policy
        for slider in self.sliders.values():
            slider.setEnabled(self._editable and supported and not self._busy)
        self.sliders["max_vram"].setEnabled(
            self._editable and supported and not self._busy and bool(self._vram_total) and self._vram_available != 0
        )
        self.apply_button.setEnabled(self._editable and supported and bool(self._draft) and not self._busy)
        if self._editable and not supported:
            self.message.setText("Update the local node to use both resource controls.")

    def _apply(self):
        if self.apply_button.isEnabled():
            self.message.setText("Saving changes…")
            self.apply_button.setText("Saving…")
            self.apply_requested.emit(dict(self._draft), self._revision)

    def applied(self, result):
        self._draft.clear()
        self.apply_button.setText("Save changes")
        self.message.setText(result.get("message", "Limits saved."))

    def failed(self, message):
        self.apply_button.setText("Save changes")
        self.message.setText(f"Could not apply limits: {str(message)[:240]}")
