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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.sliders = {}
        self.values = {}
        for field, title, description in (
            ("max_vram", "VRAM", "GPU memory available for sharing. Local inference keeps its reserved memory."),
            (
                "max_processing_percent",
                "Processing usage",
                "Sharing compute time. Lower values add rest between steps; brief usage spikes are possible.",
            ),
        ):
            row = QHBoxLayout()
            row.addWidget(QLabel(title), 1)
            value = QLabel("100%")
            value.setObjectName(f"resource_{field}_value")
            row.addWidget(value)
            layout.addLayout(row)
            slider = QSlider(Qt.Horizontal)
            slider.setObjectName(f"resource_{field}")
            slider.setAccessibleName(f"{title} percentage")
            slider.setRange(1, 100)
            slider.setValue(100)
            slider.setPageStep(10)
            slider.valueChanged.connect(lambda percent, name=field: self._changed(name, percent))
            layout.addWidget(slider)
            detail = QLabel(description)
            detail.setObjectName("bodyMuted")
            detail.setWordWrap(True)
            layout.addWidget(detail)
            self.sliders[field] = slider
            self.values[field] = value
        self.message = QLabel("Sharing stays off until you start it. Use Pause sharing to stop completely.")
        self.message.setWordWrap(True)
        self.message.setObjectName("bodyMuted")
        layout.addWidget(self.message)
        self.apply_button = QPushButton("Apply limits")
        self.apply_button.setObjectName("applyResourceLimits")
        self.apply_button.clicked.connect(self._apply)
        layout.addWidget(self.apply_button)
        self._update_enabled()

    def _changed(self, field, percent):
        self._draft[field] = f"{percent}%" if field == "max_vram" else percent
        self.values[field].setText(f"{percent}%")
        self.message.setText(
            "Unsaved. Applying stops workers, saves your limits, then resumes previously selected sharing."
        )
        self._update_enabled()

    def set_state(self, contribution, *, busy=False):
        revision = contribution.get("config_revision")
        if revision != self._revision:
            if self._draft:
                self.message.setText("The node's settings changed. Showing the saved limits.")
            self._draft.clear()
        self._revision = revision
        self._policy = contribution.get("policy") or {}
        self._editable = contribution.get("editable", False) and isinstance(revision, str)
        self._busy = busy
        for field, slider in self.sliders.items():
            if field in self._draft:
                continue
            raw = self._policy.get(field, 100)
            if field == "max_vram":
                try:
                    percent = float(raw[:-1]) if isinstance(raw, str) and raw.endswith("%") else None
                    if percent is not None and (not math.isfinite(percent) or not 0 < percent <= 100):
                        percent = None
                except ValueError:
                    percent = None
                text = f"{percent:g}%" if percent is not None else f"Custom: {raw}" if raw else "100% (default)"
            else:
                percent = raw
                text = f"{percent:g}%"
            slider.blockSignals(True)
            slider.setValue(100 if percent is None else round(percent))
            slider.blockSignals(False)
            self.values[field].setText(text)
        self._update_enabled()

    def _update_enabled(self):
        supported = "max_processing_percent" in self._policy
        for slider in self.sliders.values():
            slider.setEnabled(self._editable and supported and not self._busy)
        self.apply_button.setEnabled(self._editable and supported and bool(self._draft) and not self._busy)
        if self._editable and not supported:
            self.message.setText("Update the local node to use both resource controls.")

    def _apply(self):
        if self.apply_button.isEnabled():
            self.apply_requested.emit(dict(self._draft), self._revision)

    def applied(self, result):
        self._draft.clear()
        self.message.setText(result.get("message", "Limits saved."))

    def failed(self, message):
        self.message.setText(f"Could not apply limits: {str(message)[:240]}")
