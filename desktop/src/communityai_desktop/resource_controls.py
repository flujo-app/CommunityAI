"""The two sharing budget sliders, backed by the node's persisted policy."""

import math
import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSlider,
    QStyle,
    QStyleOptionSlider,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)


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
            ("max_vram", "GPU memory limit"),
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
        self._draft[field] = f"{percent}%" if field == "max_vram" else percent
        self.values[field].setText(self._vram_text(percent) if field == "max_vram" else f"{percent}%")
        self.message.setText("Unsaved changes")
        self._update_enabled()

    def _vram_text(self, percent=None):
        if self._vram_total is not None:
            amount = self._vram_saved
            if percent is not None:
                amount = self._vram_total * percent / 100
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
                if raw and percent is None:
                    text = str(raw)
            else:
                percent = raw
                text = f"{percent:g}%"
            slider.blockSignals(True)
            slider.setMaximum(100)
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


_GPU_LIMIT = 16
_GPU_FIELDS = ("max_vram", "max_processing_percent")
_GPU_DEVICE = re.compile(r"(?:cuda|xpu):(?:[0-9]|1[0-5])|mps")
_MEMORY_LIMIT = re.compile(
    r"([0-9]+(?:\.[0-9]+)?)\s*(b|bytes?|[kmgtpe]i?b|(?:kilo|mega|giga|tera|peta|exa)bytes?)?", re.I
)


def _gpu_label(device):
    return f"GPU{int(device.split(':')[1]) + 1}" if device.startswith("cuda:") else device.upper()


def _gpu_percent(raw):
    if isinstance(raw, str) and raw.endswith("%"):
        try:
            percent = float(raw[:-1].strip())
        except ValueError:
            raise ValueError("Saved GPU memory percentage is invalid") from None
        if not math.isfinite(percent) or not 0 < percent <= 100:
            raise ValueError("Saved GPU memory percentage is invalid")
        return percent
    return None


class _GpuSlider(QSlider):
    """Draw a numeric thumb only when there is one exact integer position."""

    def __init__(self):
        super().__init__(Qt.Horizontal)
        self.mixed = False
        self.setRange(1, 100)
        self.setPageStep(10)

    def set_mixed(self, mixed):
        self.mixed = mixed
        self.setAccessibleDescription("Custom or different settings. Move to choose a percentage." if mixed else "")
        self.update()

    def paintEvent(self, event):
        if not self.mixed:
            super().paintEvent(event)
            return
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        option.subControls = QStyle.SC_SliderGroove | QStyle.SC_SliderTickmarks
        painter = QStylePainter(self)
        painter.drawComplexControl(QStyle.CC_Slider, option)


class _GpuResourceRow(QWidget):
    def __init__(self, device, changed):
        super().__init__()
        self.device = device
        layout = QVBoxLayout(self)
        heading = QHBoxLayout()
        self.selected = QCheckBox()
        self.selected.setObjectName(f"gpu_{device}_selected")
        self.selected.setAccessibleName(f"Use {_gpu_label(device)}")
        self.selected.toggled.connect(lambda selected: changed(device, "selected", selected))
        heading.addWidget(self.selected)
        self.title = QLabel()
        self.title.setTextFormat(Qt.PlainText)
        heading.addWidget(self.title, 1)
        layout.addLayout(heading)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.sliders, self.values = {}, {}
        for field, title in zip(_GPU_FIELDS, ("Memory limit", "Compute limit")):
            line = QHBoxLayout()
            line.addWidget(QLabel(title), 1)
            value = QLabel()
            value.setTextFormat(Qt.PlainText)
            line.addWidget(value)
            layout.addLayout(line)
            slider = _GpuSlider()
            slider.setObjectName(f"gpu_{device}_{field}")
            slider.setAccessibleName(f"{_gpu_label(device)} {title.lower()}")
            slider.valueChanged.connect(lambda percent, name=field: changed(device, name, percent))
            slider.actionTriggered.connect(
                lambda action, name=field, control=slider: changed(device, name, control.sliderPosition())
            )
            layout.addWidget(slider)
            self.sliders[field], self.values[field] = slider, value


class GpuResourceControls(QWidget):
    """Draft a bounded, complete set of CUDA sharing choices without starting work.

    ``set_state`` takes ``config_revision``, ``editable``, ``inventory`` (the
    sanitized hardware GPU list), and ``rows`` containing saved device,
    selected, max_vram and max_processing_percent values. Inventory alone never
    opts a card in. ``apply_requested`` emits those four fields for every managed
    CUDA row and the draft's revision. The caller owns pause/save/reload, then
    calls ``applied`` with the authoritative state or ``failed`` with an error.
    """

    apply_requested = Signal(object, str)
    dirty_changed = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._revision = None
        self._inventory = {}
        self._saved, self._draft = {}, {}
        self._latest = None
        self._editable = self._busy = self._submitting = self._conflict = False
        self._was_dirty = False
        self._feedback = ""
        self.rows, self.master_sliders, self.master_values = {}, {}, {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("All GPUs"))
        for field, title in zip(_GPU_FIELDS, ("Memory limit", "Compute limit")):
            line = QHBoxLayout()
            line.addWidget(QLabel(title), 1)
            value = QLabel()
            line.addWidget(value)
            layout.addLayout(line)
            slider = _GpuSlider()
            slider.setObjectName(f"all_gpus_{field}")
            slider.setAccessibleName(f"All GPUs {title.lower()}")
            slider.valueChanged.connect(lambda percent, name=field: self._master_changed(name, percent))
            slider.actionTriggered.connect(
                lambda action, name=field, control=slider: self._master_changed(name, control.sliderPosition())
            )
            layout.addWidget(slider)
            self.master_sliders[field], self.master_values[field] = slider, value
        hint = QLabel("Use the master sliders to set all limits. Select the GPUs you want to share.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.rows_scroll = QScrollArea()
        self.rows_scroll.setObjectName("gpuRows")
        self.rows_scroll.setWidgetResizable(True)
        self.rows_scroll.setMinimumHeight(180)
        rows_widget = QWidget()
        self.rows_layout = QVBoxLayout(rows_widget)
        self.rows_scroll.setWidget(rows_widget)
        # Keep Save/Discard outside the scrolling list even with sixteen cards.
        layout.addWidget(self.rows_scroll, 1)
        self.message = QLabel()
        self.message.setTextFormat(Qt.PlainText)
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Discard changes")
        self.cancel_button.clicked.connect(self.discard)
        actions.addWidget(self.cancel_button)
        self.apply_button = QPushButton("Save changes")
        self.apply_button.setObjectName("applyGpuResourceLimits")
        self.apply_button.clicked.connect(self._apply)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        self._render()

    @property
    def dirty(self):
        return self._draft != self._saved

    @staticmethod
    def _validated_state(state):
        if not isinstance(state, dict):
            raise ValueError("GPU settings must be an object")
        revision = state.get("config_revision")
        if not isinstance(revision, str) or not revision or len(revision) > 128:
            raise ValueError("GPU settings require a config revision")
        inventory, saved = {}, {}
        for field, target in (("inventory", inventory), ("rows", saved)):
            records = state.get(field)
            if not isinstance(records, list) or len(records) > _GPU_LIMIT:
                raise ValueError("GPU settings exceed the supported card limit")
            for record in records:
                device = record.get("device") if isinstance(record, dict) else None
                if not isinstance(device, str) or _GPU_DEVICE.fullmatch(device) is None or device in target:
                    raise ValueError("GPU settings require unique supported device names")
                if field == "inventory":
                    total = record.get("total_bytes")
                    status = record.get("status")
                    if status not in ("available", "unavailable", "unsupported") or (
                        total is not None and (type(total) is not int or not 0 < total <= 2**63 - 1)
                    ):
                        raise ValueError("GPU inventory has invalid capacity or status")
                    if status == "available" and total is None:
                        raise ValueError("An available GPU must report its capacity")
                    name = record.get("name")
                    name = " ".join(name.split())[:160] if isinstance(name, str) and name.isprintable() else ""
                    target[device] = {"device": device, "name": name, "total_bytes": total, "status": status}
                else:
                    raw, percent = record.get("max_vram"), record.get("max_processing_percent")
                    memory_valid = False
                    if isinstance(raw, str) and len(raw) <= 64:
                        memory_percent = _gpu_percent(raw)
                        match = _MEMORY_LIMIT.fullmatch(raw.strip())
                        memory_valid = memory_percent is not None or (
                            match is not None and 0 < float(match[1]) < float("inf")
                        )
                    if (
                        not device.startswith("cuda:")
                        or type(record.get("selected")) is not bool
                        or isinstance(percent, bool)
                        or not isinstance(percent, (int, float))
                        or not 1 <= percent <= 100
                        or not memory_valid
                    ):
                        raise ValueError("Saved GPU settings are invalid")
                    target[device] = {
                        "device": device,
                        "selected": record["selected"],
                        "max_vram": raw,
                        "max_processing_percent": percent,
                    }
        if len(inventory.keys() | saved.keys()) > _GPU_LIMIT:
            raise ValueError("GPU settings exceed the supported card limit")
        return revision, inventory, saved

    def set_state(self, state, *, busy=False):
        revision, inventory, saved = self._validated_state(state)
        # Keep a disconnected row until an authoritative settings reload. Never
        # silently omit a card that disappeared while the user was editing it.
        if revision == self._revision:
            for device, record in self._saved.items():
                saved.setdefault(device, dict(record))
        for device in inventory:
            if device.startswith("cuda:"):
                saved.setdefault(
                    device, {"device": device, "selected": False, "max_vram": "100%", "max_processing_percent": 100}
                )
        retained = self._draft.keys() if self.dirty else set()
        if len(inventory.keys() | saved.keys() | retained) > _GPU_LIMIT:
            raise ValueError("GPU settings exceed the supported card limit")
        self._latest = (revision, inventory, saved)
        self._inventory = inventory
        self._editable, self._busy = state.get("editable") is True, busy
        if self.dirty and (revision != self._revision or saved != self._saved):
            self._conflict = True
        elif not self.dirty:
            self._revision, self._saved = revision, saved
            self._draft = {device: dict(record) for device, record in saved.items()}
            self._conflict = False
        self._render()

    def _can_edit(self):
        return self._editable and not (self._busy or self._submitting or self._conflict)

    def _available(self, device):
        return device.startswith("cuda:") and self._inventory.get(device, {}).get("status") == "available"

    def _changed(self, device, field, value):
        if not self._can_edit() or device not in self._draft:
            return
        if not self._available(device) and (field != "selected" or value):
            return
        self._draft[device][field] = f"{value}%" if field == "max_vram" else value
        self._feedback = ""
        self._render()

    def _master_changed(self, field, percent):
        if not self._can_edit():
            return
        for device, record in self._draft.items():
            if self._available(device):
                record[field] = f"{percent}%" if field == "max_vram" else percent
        self._feedback = ""
        self._render()

    @staticmethod
    def _set_slider(slider, percent):
        exact_position = percent is not None and 1 <= percent <= 100 and float(percent).is_integer()
        slider.blockSignals(True)
        slider.setValue(int(percent) if exact_position else 100)
        slider.set_mixed(not exact_position)
        if percent is not None and not exact_position:
            slider.setAccessibleDescription(f"Saved limit {percent:g} percent. Move to choose a whole percentage.")
        slider.blockSignals(False)

    def _render(self):
        devices = sorted(
            self._inventory.keys() | self._draft.keys(),
            key=lambda key: (key.split(":")[0], int(key.split(":")[-1]) if ":" in key else 0),
        )
        for device in list(self.rows):
            if device not in devices:
                row = self.rows.pop(device)
                self.rows_layout.removeWidget(row)
                row.deleteLater()
        for index, device in enumerate(devices):
            if device not in self.rows:
                self.rows[device] = _GpuResourceRow(device, self._changed)
            row = self.rows[device]
            self.rows_layout.insertWidget(index, row)
            inventory = self._inventory.get(device, {})
            saved = self._draft.get(device, {})
            selected = saved.get("selected", False)
            available = self._available(device)
            row.title.setText(" · ".join(filter(None, (_gpu_label(device), inventory.get("name")))))
            row.selected.blockSignals(True)
            row.selected.setChecked(selected)
            row.selected.blockSignals(False)
            row.selected.setEnabled(self._can_edit() and bool(saved) and (available or selected))
            row.status.setText(
                ("Selected" if selected else "Not selected")
                if available
                else ("Not supported for sharing yet" if not device.startswith("cuda:") else "Unavailable")
            )
            for field in _GPU_FIELDS:
                raw = saved.get(field)
                percent = _gpu_percent(raw) if field == "max_vram" else raw
                self._set_slider(row.sliders[field], percent)
                row.sliders[field].setEnabled(self._can_edit() and available)
                text = f"{percent:g}%" if percent is not None else (raw.strip() if raw else "Unavailable")
                total = inventory.get("total_bytes")
                if field == "max_vram" and total and percent is not None:
                    text += f" · {total * percent / 100 / 1024**3:.1f} GiB of {total / 1024**3:.1f} GiB"
                row.values[field].setText(text)
        available_rows = [record for device, record in self._draft.items() if self._available(device)]
        for field in _GPU_FIELDS:
            values = set()
            for record in available_rows:
                value = record[field]
                parsed = _gpu_percent(value) if field == "max_vram" else None
                values.add(parsed if parsed is not None else value)
            raw = next(iter(values)) if len(values) == 1 else None
            percent = raw if isinstance(raw, (int, float)) else None
            self._set_slider(self.master_sliders[field], percent)
            self.master_sliders[field].setEnabled(self._can_edit() and bool(available_rows))
            self.master_values[field].setText(
                "No supported GPUs"
                if not available_rows
                else (
                    f"{percent:g}%"
                    if percent is not None
                    else (
                        f"{raw.strip()} · move to set all"
                        if raw is not None
                        else "Different settings · move to set all"
                    )
                )
            )
        lost_selected = any(
            record["selected"] and not self._available(device) for device, record in self._draft.items()
        )
        self.apply_button.setEnabled(self._can_edit() and self.dirty and not lost_selected)
        self.apply_button.setText("Saving…" if self._submitting else "Save changes")
        self.cancel_button.setEnabled(not (self._busy or self._submitting) and (self.dirty or self._conflict))
        self.cancel_button.setText("Reload saved settings" if self._conflict else "Discard changes")
        self.message.setText(
            "Saving changes…"
            if self._submitting
            else "Settings changed elsewhere. Reload saved settings before saving."
            if self._conflict
            else "A selected GPU is unavailable. Deselect it before saving."
            if lost_selected
            else self._feedback
            or (
                "Unsaved changes. Saving pauses sharing."
                if self.dirty
                else "Choose GPUs and limits, then save. Sharing starts only when you start it."
            )
        )
        if self.dirty != self._was_dirty:
            self._was_dirty = self.dirty
            self.dirty_changed.emit(self.dirty)

    def _apply(self):
        if not self.apply_button.isEnabled():
            return
        self._submitting = True
        self._render()
        self.apply_requested.emit([dict(record) for record in self._draft.values()], self._revision)

    def discard(self):
        if self._busy or self._submitting or self._latest is None:
            return
        self._revision, self._inventory, saved = self._latest
        self._saved = {device: dict(record) for device, record in saved.items()}
        self._draft = {device: dict(record) for device, record in saved.items()}
        self._conflict = False
        self._feedback = "Showing saved settings."
        self._render()

    def applied(self, state):
        """Accept the authoritative settings observed after a successful reload."""
        self._validated_state(state)
        self._saved, self._draft = {}, {}
        self._submitting = self._conflict = False
        self._feedback = "Saved. Sharing is paused."
        self.set_state(state)

    def failed(self, message):
        self._submitting = False
        self._feedback = " ".join(str(message).split())[:240]
        self._render()
