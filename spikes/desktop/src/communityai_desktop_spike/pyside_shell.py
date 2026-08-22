"""PySide 6 implementation of the fixed desktop-spike workflow."""

from __future__ import annotations

from importlib.metadata import version
from typing import Any, Callable, Dict


def check_runtime() -> Dict[str, str]:
    import PySide6  # noqa: F401

    return {"shell": "pyside", "framework": "PySide6", "version": version("PySide6")}


def run(controller, *, auto_close_seconds=None) -> int:  # noqa: ANN001
    from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (
        QApplication,
        QComboBox,
        QGridLayout,
        QGroupBox,
        QHBoxLayout,
        QHeaderView,
        QInputDialog,
        QLabel,
        QMainWindow,
        QMessageBox,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QVBoxLayout,
        QWidget,
    )

    class TaskSignals(QObject):
        result = Signal(object)
        error = Signal(str)

    class Task(QRunnable):
        def __init__(self, operation: Callable[[], Any]):
            super().__init__()
            self.operation = operation
            self.signals = TaskSignals()

        @Slot()
        def run(self):
            try:
                self.signals.result.emit(self.operation())
            except Exception as exc:  # GUI boundary: render the node error and remain responsive.
                self.signals.error.emit(str(exc))

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("CommunityAI desktop shell spike — PySide")
            self.resize(900, 640)
            self._pool = QThreadPool.globalInstance()
            self._tasks = set()
            self._busy = 0

            root = QWidget()
            layout = QVBoxLayout(root)
            self.setCentralWidget(root)

            endpoint_group = QGroupBox("Local OpenAI endpoint")
            endpoint_layout = QHBoxLayout(endpoint_group)
            self.endpoint = QLabel("Connecting…")
            self.endpoint.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            self.copy_endpoint = QPushButton("Copy endpoint")
            self.copy_endpoint.clicked.connect(self._copy_endpoint)
            endpoint_layout.addWidget(self.endpoint, 1)
            endpoint_layout.addWidget(self.copy_endpoint)
            layout.addWidget(endpoint_group)

            self.node_state = QLabel("Connecting to the local node…")
            self.node_state.setAccessibleName("Local node connection status")
            layout.addWidget(self.node_state)

            models_group = QGroupBox("Community models")
            models_layout = QVBoxLayout(models_group)
            self.models = QTableWidget(0, 4)
            self.models.setHorizontalHeaderLabels(("Model", "State", "Route coverage", "Active requests"))
            self.models.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.models.setEditTriggers(QTableWidget.NoEditTriggers)
            self.models.setSelectionBehavior(QTableWidget.SelectRows)
            models_layout.addWidget(self.models)
            layout.addWidget(models_group, 1)

            workers_group = QGroupBox("Contribution")
            workers_layout = QGridLayout(workers_group)
            self.workers = QTableWidget(0, 4)
            self.workers.setHorizontalHeaderLabels(("Worker", "Model", "State", "Restarts"))
            self.workers.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
            self.workers.setEditTriggers(QTableWidget.NoEditTriggers)
            workers_layout.addWidget(self.workers, 0, 0, 1, 4)
            self.worker_choice = QComboBox()
            self.worker_choice.setAccessibleName("Contribution worker")
            workers_layout.addWidget(self.worker_choice, 1, 0)
            self.worker_buttons = []
            for column, (label, action) in enumerate(
                (("Start", "start"), ("Pause", "pause"), ("Restart", "restart")),
                start=1,
            ):
                button = QPushButton(label)
                button.clicked.connect(lambda checked=False, selected=action: self._worker_action(selected))
                self.worker_buttons.append(button)
                workers_layout.addWidget(button, 1, column)
            layout.addWidget(workers_group, 1)

            buttons = QHBoxLayout()
            self.refresh_button = QPushButton("Refresh")
            self.refresh_button.clicked.connect(self.refresh)
            self.create_key_button = QPushButton("Create client API key")
            self.create_key_button.clicked.connect(self._create_key)
            buttons.addWidget(self.refresh_button)
            buttons.addWidget(self.create_key_button)
            buttons.addStretch(1)
            layout.addLayout(buttons)

            disclosure = QLabel(
                "Privacy: volunteer workers process request-derived data and may be able to observe or retain it."
            )
            disclosure.setWordWrap(True)
            disclosure.setAccessibleName("Volunteer worker privacy disclosure")
            layout.addWidget(disclosure)

            self._timer = QTimer(self)
            self._timer.setInterval(10_000)
            self._timer.timeout.connect(self.refresh)
            self._timer.start()
            self.refresh()

        def _set_busy(self, change: int) -> None:
            self._busy += change
            busy = self._busy > 0
            self.refresh_button.setDisabled(busy)
            self.create_key_button.setDisabled(busy)
            for button in self.worker_buttons:
                button.setDisabled(busy or self.worker_choice.count() == 0)

        def _submit(self, operation: Callable[[], Any], on_result: Callable[[Any], None]) -> None:
            self._set_busy(1)
            task = Task(operation)
            self._tasks.add(task)

            def finish(result: Any) -> None:
                self._tasks.discard(task)
                self._set_busy(-1)
                on_result(result)

            def fail(message: str) -> None:
                self._tasks.discard(task)
                self._set_busy(-1)
                self.node_state.setText(f"Disconnected: {message}")

            task.signals.result.connect(finish)
            task.signals.error.connect(fail)
            self._pool.start(task)

        def refresh(self) -> None:
            if self._busy:
                return
            self.node_state.setText("Refreshing local node status…")
            self._submit(controller.snapshot, self._render)

        def _render(self, snapshot: Dict[str, Any]) -> None:
            self.node_state.setText(f"Node: {snapshot['node_status']}")
            self.endpoint.setText(snapshot["openai_base_url"])
            self.models.setRowCount(len(snapshot["models"]))
            for row, model in enumerate(snapshot["models"]):
                values = (
                    model["id"],
                    model["state"],
                    model["coverage"],
                    str(model["active_requests"]),
                )
                for column, value in enumerate(values):
                    self.models.setItem(row, column, QTableWidgetItem(value))

            selected = self.worker_choice.currentData()
            self.worker_choice.clear()
            self.workers.setRowCount(len(snapshot["workers"]))
            for row, worker in enumerate(snapshot["workers"]):
                values = (
                    worker["id"],
                    worker["model"],
                    worker["state"],
                    str(worker["restart_count"]),
                )
                for column, value in enumerate(values):
                    self.workers.setItem(row, column, QTableWidgetItem(value))
                self.worker_choice.addItem(worker["id"], worker["id"])
            index = self.worker_choice.findData(selected)
            if index >= 0:
                self.worker_choice.setCurrentIndex(index)
            for button in self.worker_buttons:
                button.setDisabled(self.worker_choice.count() == 0)

        def _copy_endpoint(self) -> None:
            QGuiApplication.clipboard().setText(self.endpoint.text())
            self.node_state.setText("Copied the OpenAI endpoint")

        def _worker_action(self, action: str) -> None:
            worker_id = self.worker_choice.currentData()
            if worker_id:
                self.node_state.setText(f"Requesting {action} for {worker_id}…")
                self._submit(
                    lambda: controller.worker_action(worker_id, action),
                    lambda result: self.refresh(),
                )

        def _create_key(self) -> None:
            label, accepted = QInputDialog.getText(self, "Create client API key", "Label")
            if accepted and label.strip():
                self._submit(lambda: controller.create_client_key(label), self._show_key)

        def _show_key(self, result: Dict[str, Any]) -> None:
            secret = result["secret"]
            QGuiApplication.clipboard().setText(secret)
            message = QMessageBox(self)
            message.setWindowTitle("Client API key created")
            message.setTextFormat(Qt.PlainText)
            message.setText("The new API key was copied to the clipboard. It is shown only once:")
            message.setDetailedText(secret)
            message.exec()
            self.node_state.setText("Created and copied a client API key")

    application = QApplication.instance() or QApplication([])
    application.setApplicationName("CommunityAI desktop spike")
    window = MainWindow()
    window.show()
    if auto_close_seconds is not None:
        QTimer.singleShot(max(1, int(float(auto_close_seconds) * 1000)), application.quit)
    return application.exec()
