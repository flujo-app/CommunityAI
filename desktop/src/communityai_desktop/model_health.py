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

from communityai_desktop.presentation import model_name

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
        self.bar.setAccessibleName("Current file download progress")
        self.totals = caption("")
        for widget in (self.title, self.detail, self.bar, self.totals):
            layout.addWidget(widget)

    def set_state(self, name, progress, model_state=None):
        name = model_name(name)
        if progress is None:
            self.title.setText(f"{name} · Not downloaded")
            self.detail.setText("Downloads when needed.")
            self.bar.hide()
            self.totals.setText("")
            self.totals.hide()
            return
        state = progress["state"]
        labels = {
            "waiting": "Waiting",
            "checking": "Checking downloaded files",
            "downloading": "Downloading",
            "retrying": "Retrying download",
            "verifying": "Verifying files",
            "loading": "Loading model",
            "ready": "Ready",
            "failed": "Couldn’t prepare this model",
            "paused": "Paused",
        }
        if state == "ready" and model_state == "known":
            labels["ready"] = "Downloaded"
        self.title.setText(f"{name} · {labels[state]}")
        size, received = progress["artifact_bytes"], progress["artifact_received_bytes"]
        artifact = progress.get("artifact") or "Preparing download"
        speed = progress.get("bytes_per_second") or 0
        detail = f"{byte_text(received or 0)} / {byte_text(size)}" if size else "Preparing download"
        if state == "downloading" and speed:
            detail += f" · {byte_text(speed)}/s"
        self.detail.setText(detail)
        self.detail.setToolTip(artifact)
        self.bar.setVisible(bool(size))
        self.bar.setValue(min(1000, int(1000 * (received or 0) / size)) if size else 0)
        self.bar.setAccessibleDescription(f"{artifact} · {detail}")
        diagnostic = (
            f"{byte_text(progress['verified_bytes'])} verified across {progress['verified_files'] or 0} files"
            f" · {byte_text(progress['received_bytes'])} received or cached"
            + (f" · Resumed {byte_text(progress['resumed_bytes'])}" if progress["resumed_bytes"] else "")
            + (f" · {progress['retries']} retries" if progress["retries"] else "")
        )
        files, total_files = progress.get("verified_files"), progress.get("selected_files")
        self.totals.setText(f"{files or 0} of {total_files} files checked" if total_files else "")
        self.totals.setVisible(bool(total_files))
        self.totals.setToolTip(diagnostic)
        self.setToolTip(diagnostic)


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


class _ModelDisclosureButton(QPushButton):
    def sizeHint(self):
        return self.layout().totalSizeHint() if self.layout() else super().sizeHint()

    def minimumSizeHint(self):
        return self.layout().totalMinimumSize() if self.layout() else super().minimumSizeHint()


class ModelHealthCard(QFrame):
    def __init__(self):
        super().__init__()
        self.setObjectName("card")
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(8, 8, 8, 8)
        self.expand_button = _ModelDisclosureButton()
        self.expand_button.setObjectName("modelDisclosure")
        self.expand_button.setCheckable(True)
        self.expand_button.setStyleSheet(
            "QPushButton#modelDisclosure { background: transparent; border: 0; padding: 0; text-align: left; }"
            "QPushButton#modelDisclosure:hover { background: #192131; }"
            "QPushButton#modelDisclosure:focus { border: 1px solid #826BFF; }"
        )
        header = QHBoxLayout(self.expand_button)
        header.setContentsMargins(12, 10, 12, 10)
        copy = QVBoxLayout()
        copy.setSpacing(4)
        self.title = caption("", "sectionTitle")
        self.summary = caption("")
        self.summary.setStyleSheet("font-size: 13px;")
        self.disclosure = caption("Show details", "bodyStrong")
        for widget in (self.title, self.summary, self.disclosure):
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
        copy.addWidget(self.title)
        copy.addWidget(self.summary)
        header.addLayout(copy, 1)
        header.addWidget(self.disclosure)
        self.layout.addWidget(self.expand_button)
        self.details = QWidget()
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(12, 4, 12, 12)
        details_layout.setSpacing(10)
        self.layout.addWidget(self.details)
        self.details.hide()
        self.expand_button.toggled.connect(self._toggle_details)
        self.legend = caption("Green: available · Blue: joining · Purple: reserved · Red: offline · Gray: missing")
        self.legend.setToolTip("Brighter green means a block has more than one copy. Select a block for details.")
        self.grid = QGridLayout()
        self.grid.setSpacing(5)
        self.grid.setAlignment(Qt.AlignLeft)
        self.cells = []
        self.selected = 0
        self.block_detail = caption("Select a block for details.")
        self.local_info = caption("")
        self.download = DownloadCard()
        self.worker_downloads = {}
        self.worker_downloads_layout = QVBoxLayout()
        self.peer_button = QPushButton("Contributors")
        self.peer_button.setCheckable(True)
        self.peer_button.toggled.connect(self._toggle_peers)
        self.peer_table = QTableWidget(0, 3)
        self.peer_table.setStyleSheet(
            "QTableWidget { background: #10151f; color: #e7eaf0; gridline-color: #293344; border: 1px solid #293344; }"
            "QHeaderView::section { background: #192231; color: #aab8cc; border: 0; padding: 7px; }"
            "QTableWidget::item:selected { background: #334861; }"
        )
        self.peer_table.setHorizontalHeaderLabels(["Contributor", "Status", "Blocks"])
        self.peer_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.peer_table.verticalHeader().hide()
        self.peer_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.peer_table.setAccessibleName("Model contributors")
        self.peer_table.hide()
        self.peer_note = caption("No contributors connected yet.")
        self.peer_note.hide()
        details_layout.addWidget(self.legend)
        details_layout.addLayout(self.grid)
        for widget in (self.block_detail, self.local_info, self.download):
            details_layout.addWidget(widget)
        details_layout.addLayout(self.worker_downloads_layout)
        for widget in (self.peer_button, self.peer_table, self.peer_note):
            details_layout.addWidget(widget)

    def _toggle_details(self, checked):
        self.details.setVisible(checked)
        self.disclosure.setText("Hide details" if checked else "Show details")
        self._update_accessible_header()

    def _update_accessible_header(self):
        self.expand_button.setAccessibleName(f"{self.title.text()}. {self.summary.text()}. {self.disclosure.text()}")
        self.expand_button.setAccessibleDescription("Expanded" if self.expand_button.isChecked() else "Collapsed")

    def _toggle_peers(self, checked):
        self.peer_table.setVisible(checked and self.peer_table.rowCount() > 0)
        self.peer_note.setVisible(checked and self.peer_table.rowCount() == 0)
        self.peer_button.setText(f"{'Hide contributors' if checked else 'Contributors'} ({self.peer_table.rowCount()})")

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
        self.title.setText(model_name(model["id"]))
        age = health["last_updated_age"]
        stale = health["status"] not in ("complete", "incomplete") or (age is not None and age > 120)
        if local:
            summary = (
                "On this computer · Ready" if model["state"] == "ready" else "On this computer · Downloads when needed"
            )
        elif stale:
            summary = "Checking availability"
        elif model.get("route_complete", health["status"] == "complete"):
            count = model.get("peer_count") or 0
            summary = f"Available · {count} {'contributor' if count == 1 else 'contributors'}"
        elif model.get("chat_ready") is False and health["status"] == "complete":
            summary = "Model blocks available · Waiting for a peer to handle chat"
        else:
            summary = f"Waiting for contributors · {model['coverage']} blocks available"
        progress = model.get("download_progress")
        if local and progress and progress["state"] == "ready" and model["state"] != "ready":
            summary = "On this computer · Downloaded"
        if progress and progress["state"] in ("downloading", "retrying", "verifying", "loading", "failed", "paused"):
            activity = {
                "downloading": "Downloading",
                "retrying": "Retrying download",
                "verifying": "Checking downloaded files",
                "loading": "Loading",
                "failed": "Couldn’t prepare this model",
                "paused": "Download paused",
            }[progress["state"]]
            summary = f"{'On this computer' if local else 'Your download'} · {activity}"
        self.summary.setText(summary)
        self.summary.setToolTip(f"Updated {int(age)} seconds ago" if age is not None and not local else "")
        self._update_accessible_header()
        self.legend.setVisible(not local)
        self.block_detail.setVisible(not local)
        size = model.get("selected_whole_shard_bytes")
        self.local_info.setText(f"Download size: {byte_text(size)}" if size else "Uses this computer for answers.")
        self.local_info.setVisible(local)
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
            detail = f"Block {index} · {state.capitalize()} · {replicas if replicas is not None else 'Unknown'} copies"
            if joining:
                detail += f" · {joining} joining"
            if reservations:
                detail += f" · {len(reservations)} reservations"
            owners = [p["public_name"] or p["peer_id"][:12] for p in health["peers"] if index in p["online_blocks"]]
            if owners:
                detail += " · Peers: " + ", ".join(owners[:8])
            if failures:
                detail += " · Sharing failed on this computer"
            cell.setToolTip(detail)
            cell.setAccessibleName(detail)
            cell.setStyleSheet(
                f"QPushButton {{ background: {COLORS[state]}; color: #edf6ff; padding: 0; border: 1px solid #526174; border-radius: 4px; font-size: 10px; }} QPushButton:focus {{ border: 2px solid white; }}"
            )
        if self.cells:
            self._select(min(self.selected, len(self.cells) - 1))
        self.download.set_state("Your download", model.get("download_progress"), model["state"])
        self.download.setVisible(model.get("download_progress") is not None)
        downloading_workers = [
            worker
            for worker in workers
            if worker["model"] == model["id"] and worker.get("download_progress") is not None
        ]
        worker_ids = {worker["id"] for worker in downloading_workers}
        for worker_id in list(self.worker_downloads):
            if worker_id not in worker_ids:
                card = self.worker_downloads.pop(worker_id)
                self.worker_downloads_layout.removeWidget(card)
                card.deleteLater()
        for worker in downloading_workers:
            if worker["id"] not in self.worker_downloads:
                card = self.worker_downloads[worker["id"]] = DownloadCard()
                self.worker_downloads_layout.addWidget(card)
            self.worker_downloads[worker["id"]].set_state(
                "Sharing download", worker["download_progress"], worker["state"]
            )
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
                states.append(f"Preparing {len(peer['joining_blocks'])}")
            if reserved:
                states.append(f"Planning to share {len(reserved)}")
            if peer["offline_blocks"]:
                states.append("Offline")
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
                        "This computer",
                        state.capitalize(),
                        worker.get("placement", {}).get("block_indices") or "Unassigned",
                        "Local contribution",
                        worker["id"],
                    )
                )
        self.peer_table.setRowCount(len(peer_rows))
        self.peer_table.setFixedHeight(min(300, 55 + 30 * len(peer_rows)))
        for row, values in enumerate(peer_rows):
            for column, value in enumerate(values[:3]):
                item = QTableWidgetItem(value)
                item.setToolTip(f"{values[4]}\n{values[3]}" if column == 0 else value)
                self.peer_table.setItem(row, column, item)
        self.peer_button.setVisible(not local)
        self._toggle_peers(not local and self.peer_button.isChecked())
