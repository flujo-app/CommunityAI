"""Live block coverage, observed peers, and this computer's artifact transfers."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

COLORS = {
    "covered": "#237851",
    "replicated": "#35b779",
    "joining": "#367ed6",
    "reserved": "#8060dc",
    "missing": "#303b4d",
    "offline": "#954354",
    "unknown": "#242b38",
}


def caption(value, style="bodyMuted"):
    widget = QLabel(value)
    widget.setTextFormat(Qt.PlainText)
    widget.setObjectName(style)
    widget.setWordWrap(True)
    return widget


def byte_text(value):
    if value is None:
        return "unknown"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{value:.0f} B"
        value /= 1024


def block_ranges(indices):
    spans = []
    for value in sorted(set(indices)):
        if spans and value == spans[-1][1] + 1:
            spans[-1][1] = value
        else:
            spans.append([value, value])
    return ", ".join(str(start) if start == end else f"{start}–{end}" for start, end in spans) or "—"


class DownloadCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("listRow")
        layout = QVBoxLayout(self)
        self.title = caption("Your download", "bodyStrong")
        self.detail = caption("")
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(False)
        self.bar.setAccessibleName("Current artifact download progress")
        self.totals = caption("")
        for widget in (self.title, self.detail, self.bar, self.totals):
            layout.addWidget(widget)

    def set_state(self, name, progress, model_state=None):
        if progress is None:
            self.title.setText(f"{name} · Download has not started")
            self.detail.setText("Files are downloaded when this model is first needed.")
            self.bar.hide()
            self.totals.setText("")
            return
        state = progress["state"]
        labels = {
            "waiting": "Waiting",
            "checking": "Checking cache",
            "downloading": "Downloading",
            "retrying": "Retrying download",
            "verifying": "Verifying files",
            "loading": "Loading model",
            "ready": "Ready",
            "failed": "Download or loading failed",
            "paused": "Paused",
        }
        if state == "ready" and model_state == "known":
            labels["ready"] = "Files verified · Model unloaded"
        self.title.setText(f"{name} · {labels[state]}")
        size, received = progress["artifact_bytes"], progress["artifact_received_bytes"]
        artifact = progress.get("artifact") or "Preparing file selection"
        speed = progress.get("bytes_per_second") or 0
        detail = f"{artifact} · {byte_text(received)} / {byte_text(size)}"
        if state == "downloading":
            detail += f" · {byte_text(speed)}/s"
        self.detail.setText(detail)
        self.bar.setVisible(bool(size))
        self.bar.setValue(min(1000, int(1000 * (received or 0) / size)) if size else 0)
        self.bar.setAccessibleDescription(detail)
        self.totals.setText(
            f"{byte_text(progress['verified_bytes'])} verified across {progress['verified_files'] or 0} files"
            f" · {byte_text(progress['received_bytes'])} received or cached"
            + (f" · Resumed {byte_text(progress['resumed_bytes'])}" if progress["resumed_bytes"] else "")
            + (f" · {progress['retries']} retries" if progress["retries"] else "")
        )


class DownloadsPanel(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.addWidget(caption("Your downloads", "sectionTitle"))
        self.empty = caption("No model downloads have started on this computer.")
        self.layout.addWidget(self.empty)
        self.cards = {}

    def set_state(self, snapshot):
        entries = [(f"model:{m['id']}", m["id"], m.get("download_progress"), m["state"]) for m in snapshot["models"]]
        entries += [
            (f"worker:{w['id']}", f"Sharing · {w['model']}", w.get("download_progress"), w["state"])
            for w in snapshot["workers"]
        ]
        entries = [entry for entry in entries if entry[2] is not None][:64]
        keys = {entry[0] for entry in entries}
        for key in list(self.cards):
            if key not in keys:
                self.layout.removeWidget(self.cards[key])
                self.cards.pop(key).deleteLater()
        for key, name, progress, state in entries:
            if key not in self.cards:
                self.cards[key] = DownloadCard()
                self.layout.addWidget(self.cards[key])
            self.cards[key].set_state(name, progress, state)
        self.empty.setVisible(not entries)


class ModelHealthCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.title = caption("", "sectionTitle")
        self.summary = caption("")
        self.legend = caption(
            "Green: covered · Bright green: replicated · Blue: joining · Purple: reserved · Red: offline / local failure · Gray: missing"
        )
        self.grid = QGridLayout()
        self.grid.setSpacing(5)
        self.grid.setAlignment(Qt.AlignLeft)
        self.cells = []
        self.selected = 0
        self.block_detail = caption("Select a block to inspect its coverage.")
        self.download = DownloadCard()
        self.peer_button = QPushButton("Show peer details")
        self.peer_button.setCheckable(True)
        self.peer_button.toggled.connect(self._toggle_peers)
        self.peer_table = QTableWidget(0, 4)
        self.peer_table.setStyleSheet(
            "QTableWidget { background: #10151f; color: #e7eaf0; gridline-color: #293344; border: 1px solid #293344; }"
            "QHeaderView::section { background: #192231; color: #aab8cc; border: 0; padding: 7px; }"
            "QTableWidget::item:selected { background: #334861; }"
        )
        self.peer_table.setHorizontalHeaderLabels(["Peer", "Observed state", "Blocks", "Runtime"])
        self.peer_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.peer_table.verticalHeader().hide()
        self.peer_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.peer_table.setAccessibleName("Observed model peers")
        self.peer_table.hide()
        self.peer_note = caption(
            "Reservations express intent. Joining includes download/loading. Remote download percentages and unused capacity are not reported."
        )
        for widget in (self.title, self.summary, self.legend):
            self.layout.addWidget(widget)
        self.layout.addLayout(self.grid)
        for widget in (self.block_detail, self.download, self.peer_button, self.peer_table, self.peer_note):
            self.layout.addWidget(widget)

    def _toggle_peers(self, checked):
        self.peer_table.setVisible(checked)
        self.peer_button.setText("Hide peer details" if checked else "Show peer details")

    def _select(self, index):
        self.selected = index
        self.block_detail.setText(self.cells[index].toolTip())

    def set_state(self, model, workers=()):
        health = model["health"]
        local = model["execution"] == "local"
        total = min(512, health["total_blocks"]) if not local else 0
        while len(self.cells) != total:
            if len(self.cells) > total:
                cell = self.cells.pop()
                self.grid.removeWidget(cell)
                cell.deleteLater()
            else:
                index = len(self.cells)
                cell = QPushButton(str(index))
                cell.setFixedSize(34, 28)
                cell.clicked.connect(lambda checked=False, index=index: self._select(index))
                self.grid.addWidget(cell, index // 16, index % 16)
                self.cells.append(cell)
        self.title.setText(model["id"])
        age = health["last_updated_age"]
        stale = health["status"] not in ("complete", "incomplete") or (age is not None and age > 120)
        summary = (
            "Runs on this computer"
            if local
            else f"{model['coverage']} blocks covered · {model.get('peer_count') or 0} peers serving"
        )
        if not local:
            summary += f" · Observed {int(age)}s ago" if age is not None else " · Waiting for observations"
            if stale:
                summary += " · Coverage unknown / stale"
            if health["total_blocks"] > total:
                summary += f" · Showing first {total} blocks"
            if not health["reservations_known"]:
                summary += " · Reservation lookup unavailable"
        self.summary.setText(summary)
        self.legend.setVisible(not local)
        self.block_detail.setVisible(not local)
        for index, cell in enumerate(self.cells):
            replicas = health["replica_counts"][index] if health["replica_counts"] is not None else None
            joining = health["joining_counts"][index] if health["joining_counts"] is not None else 0
            offline = health["offline_counts"][index] if health["offline_counts"] is not None else 0
            reservations = [r for r in health["reservations"] if r["start_block"] <= index < r["end_block"]]
            failures = []
            for worker in workers:
                span = worker.get("placement", {}).get("block_indices") or ""
                parts = span.split(":")
                if (
                    worker["model"] == model["id"]
                    and worker["state"] == "crashed"
                    and len(parts) == 2
                    and all(p.isdigit() for p in parts)
                ):
                    if int(parts[0]) <= index < int(parts[1]):
                        failures.append(worker["id"])
            state = (
                "unknown"
                if stale or replicas is None
                else "replicated"
                if replicas > 1
                else "covered"
                if replicas
                else "joining"
                if joining
                else "reserved"
                if reservations
                else "offline"
                if offline or failures
                else "missing"
            )
            detail = f"Block {index} · {state.capitalize()} · {replicas if replicas is not None else 'Unknown'} serving replicas · {joining or 0} joining · {len(reservations)} reservations"
            owners = [p["public_name"] or p["peer_id"][:12] for p in health["peers"] if index in p["online_blocks"]]
            if owners:
                detail += " · Peers: " + ", ".join(owners[:8])
            if failures:
                detail += " · Local worker failed: " + ", ".join(failures)
            cell.setToolTip(detail)
            cell.setAccessibleName(detail)
            cell.setStyleSheet(
                f"QPushButton {{ background: {COLORS[state]}; color: #edf6ff; padding: 0; border: 1px solid #526174; border-radius: 4px; font-size: 10px; }} QPushButton:focus {{ border: 2px solid white; }}"
            )
        if self.cells:
            self._select(min(self.selected, len(self.cells) - 1))
        self.download.set_state(model["id"], model.get("download_progress"), model["state"])
        peer_rows = []
        by_id = {p["peer_id"]: p for p in health["peers"]}
        for reservation in health["reservations"]:
            by_id.setdefault(
                reservation["peer_id"],
                {
                    "peer_id": reservation["peer_id"],
                    "public_name": None,
                    "online_blocks": [],
                    "joining_blocks": [],
                    "offline_blocks": [],
                },
            )
        for peer in by_id.values():
            reserved = sorted(
                {
                    i
                    for r in health["reservations"]
                    if r["peer_id"] == peer["peer_id"]
                    for i in range(r["start_block"], r["end_block"])
                }
            )
            states = []
            if peer["online_blocks"]:
                states.append(f"Serving {len(peer['online_blocks'])}")
            if peer["joining_blocks"]:
                states.append(f"Joining {len(peer['joining_blocks'])}")
            if reserved:
                states.append(f"Reserved {len(reserved)}")
            if peer["offline_blocks"]:
                states.append("Offline announcement")
            blocks = block_ranges(peer["online_blocks"] + peer["joining_blocks"] + peer["offline_blocks"] + reserved)
            runtime = (
                " · ".join(
                    filter(
                        None,
                        [
                            peer.get("version"),
                            peer.get("torch_dtype"),
                            peer.get("quant_type"),
                            "Relay" if peer.get("using_relay") else None,
                        ],
                    )
                )
                or "Not reported"
            )
            peer_rows.append(
                (
                    peer["public_name"] or (peer["peer_id"] or "Unknown")[:16],
                    " · ".join(states) + (" · Stale" if stale else ""),
                    blocks,
                    runtime,
                    peer["peer_id"],
                )
            )
        for worker in workers:
            if worker["model"] == model["id"]:
                progress = worker.get("download_progress")
                state = progress["state"] if progress is not None else worker["display_status"]
                peer_rows.append(
                    (
                        f"This computer · {worker['id']}",
                        state.capitalize(),
                        worker.get("placement", {}).get("block_indices") or "Unassigned",
                        "Local contribution",
                        worker["id"],
                    )
                )
        self.peer_table.setRowCount(len(peer_rows))
        self.peer_table.setFixedHeight(min(300, 55 + 30 * len(peer_rows)))
        for row, values in enumerate(peer_rows):
            for column, value in enumerate(values[:4]):
                item = QTableWidgetItem(value)
                item.setToolTip(values[4] if column == 0 else value)
                self.peer_table.setItem(row, column, item)
        self.peer_button.setVisible(not local)
        self.peer_table.setVisible(not local and self.peer_button.isChecked())
        self.peer_note.setVisible(not local)
