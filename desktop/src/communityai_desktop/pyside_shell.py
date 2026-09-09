"""Modern PySide 6 product shell for the standalone CommunityAI node."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import sys
import time
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Dict

from communityai_desktop.presentation import memory_text, model_name, model_summary, sharing_reason, sharing_summary
from communityai_desktop.startup import LoginStartupError, SingleInstanceError, login_startup_enabled, set_login_startup

APP_STYLESHEET = """
QLabel { color: #E7EAF0; font-family: "Segoe UI"; font-size: 14px; }
QMainWindow, QWidget#appShell, QScrollArea, QScrollArea > QWidget > QWidget {
    background: #090C12;
    color: #F4F6FA;
    font-family: "Segoe UI";
    font-size: 14px;
}
QFrame#sidebar { background: #0D111A; border-right: 1px solid #1B2230; }
QLabel#brandMark {
    background: #7657FF; border-radius: 12px; color: white; font-size: 19px;
    font-weight: 800; qproperty-alignment: AlignCenter;
}
QLabel#brandName { color: #FFFFFF; font-size: 18px; font-weight: 750; }
QLabel#brandTag { color: #778198; font-size: 11px; }
QLabel#navLabel, QLabel#eyebrow { color: #69748A; font-size: 10px; font-weight: 700; }
QPushButton#navButton {
    background: transparent; border: none; border-radius: 10px; color: #929CB0;
    padding: 11px 14px; text-align: left; font-size: 14px; font-weight: 600;
}
QPushButton#navButton:hover { background: #141A26; color: #E7EAF0; }
QPushButton#navButton:checked {
    background: #1A2030; color: #FFFFFF; border-left: 3px solid #826BFF; padding-left: 11px;
}
QLabel#sidebarStatus { color: #9AA4B7; font-size: 12px; }
QLabel#sidebarDot { color: #5EE1A2; font-size: 17px; }
QLabel#privacySmall { color: #69748A; font-size: 11px; }
QLabel#pageTitle { color: #FFFFFF; font-size: 28px; font-weight: 750; }
QLabel#pageSubtitle { color: #8C96AA; font-size: 14px; }
QLabel#sectionTitle { color: #F7F8FB; font-size: 17px; font-weight: 700; }
QLabel#sectionSubtitle { color: #7F899D; font-size: 12px; }
QLabel#metricValue { color: #FFFFFF; font-size: 28px; font-weight: 760; }
QLabel#metricLabel { color: #8C96AA; font-size: 12px; }
QLabel#metricNote { color: #667187; font-size: 11px; }
QLabel#bodyStrong { color: #EDEFF5; font-size: 14px; font-weight: 650; }
QLabel#bodyMuted { color: #A0AABC; font-size: 13px; }
QLabel#endpointText {
    color: #D9DDFE; background: #111625; border: 1px solid #29304A; border-radius: 9px;
    padding: 11px 13px; font-family: "Consolas"; font-size: 12px;
}
QFrame#card { background: #111621; border: 1px solid #1E2635; border-radius: 15px; }
QFrame#heroCard { background: #15172A; border: 1px solid #2D3150; border-radius: 18px; }
QFrame#connectionBanner {
    background: #111A1B; border: 1px solid #21433A; border-radius: 12px;
}
QFrame#connectionBanner[connectionState="offline"] {
    background: #1D1715; border-color: #54352A;
}
QFrame#listRow { background: #0D121C; border: 1px solid #1B2331; border-radius: 11px; }
QFrame#listRow:hover { background: #121927; border-color: #2A3448; }
QLabel#avatar {
    background: #232A42; color: #C9C2FF; border-radius: 10px; font-size: 14px;
    font-weight: 750; qproperty-alignment: AlignCenter;
}
QLabel#pill { border-radius: 9px; padding: 4px 9px; font-size: 11px; font-weight: 650; }
QLabel#pill[pillTone="good"] { background: #14352A; color: #72E7AE; }
QLabel#pill[pillTone="warn"] { background: #3B2B18; color: #F3C46C; }
QLabel#pill[pillTone="quiet"] { background: #202738; color: #9DA7BA; }
QPushButton {
    background: #1B2230; border: 1px solid #2A3446; border-radius: 9px; color: #E7EAF0;
    padding: 9px 14px; font-weight: 650;
}
QPushButton:hover { background: #242D3D; border-color: #3A465C; }
QPushButton:pressed { background: #171D29; }
QPushButton:disabled { color: #505A6D; background: #151A24; border-color: #202736; }
QPushButton#primaryButton { background: #7657FF; border-color: #876DFF; color: #FFFFFF; }
QPushButton#primaryButton:hover { background: #856BFF; }
QPushButton#ghostButton { background: transparent; border-color: #2A3345; }
QPushButton#textButton { background: transparent; border: none; color: #9E8CFF; padding: 5px; }
QPushButton#textButton:hover { color: #C0B6FF; }
QPushButton#dangerButton { background: transparent; border-color: #513039; color: #EF8E9D; }
QCheckBox { color: #E6E9EF; spacing: 10px; }
QCheckBox::indicator { width: 38px; height: 22px; }
QCheckBox::indicator:unchecked {
    background: #262E3C; border: 1px solid #3B465A; border-radius: 11px;
}
QCheckBox::indicator:checked {
    background: #7657FF; border: 1px solid #8A73FF; border-radius: 11px;
}
QSlider::groove:horizontal { height: 7px; background: #283142; border-radius: 3px; }
QSlider::sub-page:horizontal { background: #7A60FF; border-radius: 3px; }
QSlider::handle:horizontal {
    background: #FFFFFF; border: 3px solid #7657FF; width: 18px; margin: -8px 0; border-radius: 11px;
}
QProgressBar { background: #242C3B; border: none; border-radius: 3px; height: 6px; }
QProgressBar::chunk { background: #6F82FF; border-radius: 3px; }
QLineEdit {
    background: #0D121C; border: 1px solid #2A3446; border-radius: 9px; color: #F4F6FA;
    padding: 10px; selection-background-color: #7657FF;
}
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: #2A3345; border-radius: 5px; min-height: 35px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #1B2230; color: white; border: 1px solid #343E51; padding: 6px; }
"""


def check_runtime() -> Dict[str, str]:
    import PySide6  # noqa: F401

    return {"shell": "pyside", "framework": "PySide6", "version": version("PySide6")}


def _single_instance_server_name(data_location: Path | str) -> str:
    normalized = os.path.normcase(os.path.abspath(os.fspath(data_location)))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]
    return f"communityai-desktop-{digest}"


def _install_posix_termination_bridge(application, timer_type) -> Callable[[], None]:  # noqa: ANN001
    """Convert POSIX termination into a Qt quit so lifecycle cleanup can finish."""
    if os.name != "posix":
        return lambda: None

    previous = {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)}

    def request_quit(_signum, _frame) -> None:  # noqa: ANN001
        application.quit()

    for signum in previous:
        signal.signal(signum, request_quit)

    # Qt's C++ event loop needs a bounded Python callback so Python dispatches
    # pending signal handlers while the application is otherwise idle.
    heartbeat = timer_type(application)
    heartbeat.setInterval(250)
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()
    restored = False

    def restore() -> None:
        nonlocal restored
        if restored:
            return
        restored = True
        heartbeat.stop()
        for signum, handler in previous.items():
            signal.signal(signum, handler)

    return restore


def _exec_with_termination_cleanup(
    application,  # noqa: ANN001
    restore_termination_handlers: Callable[[], None],
    before_termination_restore: Callable[[], None] | None,
) -> int:
    try:
        return application.exec()
    finally:
        try:
            if before_termination_restore is not None:
                before_termination_restore()
        finally:
            restore_termination_handlers()


def _gib_text(size_bytes: int | None) -> str:
    if not size_bytes:
        return ""
    return f"{size_bytes / (1024**3):.0f} GB"


def run(
    controller=None,  # noqa: ANN001
    *,
    connect: Callable[[], Any] | None = None,
    auto_close_seconds=None,
    screenshot_path: Path | str | None = None,
    screenshot_page: int = 0,
    single_instance: bool = True,
    start_minimized: bool = False,
    activate_existing_instance: bool = True,
    instance_name: str | None = None,
    before_termination_restore: Callable[[], None] | None = None,
    qualification_automation=None,  # noqa: ANN001
) -> int:
    if controller is None and connect is None:
        raise ValueError("the desktop requires an initial controller or connector")

    from PySide6.QtCore import QLockFile, QObject, QRunnable, QStandardPaths, Qt, QThreadPool, QTimer, Signal, Slot
    from PySide6.QtGui import QFont, QGuiApplication, QIcon
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QDialog,
        QDialogButtonBox,
        QFormLayout,
        QFrame,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    from communityai_desktop.model_health import DownloadCard, ModelHealthCard
    from communityai_desktop.resource_controls import ResourceControls

    def label(text: str = "", name: str | None = None) -> QLabel:
        item = QLabel(text)
        item.setTextFormat(Qt.PlainText)
        if name:
            item.setObjectName(name)
        return item

    def card(name: str = "card") -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName(name)
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(13)
        return frame, layout

    def clear_layout(layout) -> None:  # noqa: ANN001
        while layout.count():
            item = layout.takeAt(0)
            child_layout = item.layout()
            child_widget = item.widget()
            if child_layout is not None:
                clear_layout(child_layout)
            if child_widget is not None:
                child_widget.deleteLater()

    def pill(text: str, tone: str = "quiet") -> QLabel:
        item = label(text, "pill")
        item.setProperty("pillTone", tone)
        item.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        return item

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
                result = self.operation()
            except Exception as exc:  # GUI boundary: show a friendly state and remain responsive.
                signal, result = self.signals.error, str(exc)
            else:
                signal = self.signals.result
            try:
                signal.emit(result)
            except RuntimeError as exc:
                # A bounded node request may finish after the user has closed Qt.
                if "deleted" not in str(exc):
                    raise

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("CommunityAI")
            self.resize(1200, 800)
            self.setMinimumSize(960, 680)
            self._pool = QThreadPool.globalInstance()
            self._tasks = set()
            self._busy = 0
            self._refreshing = False
            self._change_version = 0
            self._sharing_pending = None
            self._awaiting_sharing_snapshot = False
            self._sharing_error = None
            self._mode_pending = False
            self._awaiting_mode_snapshot = False
            self._closing = False
            self._controller = controller
            self._snapshot: Dict[str, Any] = {
                "models": [],
                "auto_selection": {
                    "status": "not_configured",
                    "model": None,
                    "title": "auto is not configured",
                    "reason": "Automatic model selection is not configured.",
                },
                "workers": [],
                "keys": [],
                "network": {"peer_count": 0, "regions": []},
                "contribution": {"enabled": False, "active_models": []},
            }
            self._page_buttons = []

            shell = QWidget()
            shell.setObjectName("appShell")
            shell_layout = QHBoxLayout(shell)
            shell_layout.setContentsMargins(0, 0, 0, 0)
            shell_layout.setSpacing(0)
            self.setCentralWidget(shell)

            shell_layout.addWidget(self._build_sidebar())
            self.pages = QStackedWidget()
            self.pages.addWidget(self._build_home_page())
            self.pages.addWidget(self._build_models_page())
            self.pages.addWidget(self._build_sharing_page())
            self.pages.addWidget(self._build_api_page())
            shell_layout.addWidget(self.pages, 1)

            self._show_page(0)
            self._timer = QTimer(self)
            self._timer.setInterval(8_000)
            self._timer.timeout.connect(self.refresh)
            self._timer.start()
            self.refresh()

        def _build_sidebar(self) -> QFrame:
            sidebar = QFrame()
            sidebar.setObjectName("sidebar")
            sidebar.setFixedWidth(212)
            layout = QVBoxLayout(sidebar)
            layout.setContentsMargins(19, 24, 19, 20)
            layout.setSpacing(8)

            brand_row = QHBoxLayout()
            brand_row.setSpacing(11)
            mark = label("C", "brandMark")
            mark.setFixedSize(42, 42)
            brand_copy = QVBoxLayout()
            brand_copy.setSpacing(0)
            brand_copy.addWidget(label("CommunityAI", "brandName"))
            brand_copy.addWidget(label("AI powered by people", "brandTag"))
            brand_row.addWidget(mark)
            brand_row.addLayout(brand_copy, 1)
            layout.addLayout(brand_row)
            layout.addSpacing(28)
            layout.addWidget(label("YOUR SPACE", "navLabel"))
            layout.addSpacing(5)

            group = QButtonGroup(sidebar)
            group.setExclusive(True)
            for index, title in enumerate(("Home", "Models", "Sharing", "API access")):
                button = QPushButton(title)
                button.setObjectName("navButton")
                button.setCheckable(True)
                button.setCursor(Qt.PointingHandCursor)
                button.clicked.connect(lambda checked=False, page=index: self._show_page(page))
                group.addButton(button)
                self._page_buttons.append(button)
                layout.addWidget(button)

            layout.addStretch(1)
            status_row = QHBoxLayout()
            self.sidebar_dot = label("●", "sidebarDot")
            self.sidebar_status = label("Connecting", "sidebarStatus")
            status_row.addWidget(self.sidebar_dot)
            status_row.addWidget(self.sidebar_status)
            status_row.addStretch(1)
            layout.addLayout(status_row)
            return sidebar

        def _scroll_page(self, title: str, subtitle: str) -> tuple[QScrollArea, QVBoxLayout]:
            scroll = QScrollArea()
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setWidgetResizable(True)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            content = QWidget()
            content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            layout = QVBoxLayout(content)
            layout.setContentsMargins(28, 27, 28, 32)
            layout.setSpacing(20)
            layout.addWidget(label(title, "pageTitle"))
            if subtitle:
                subtitle_label = label(subtitle, "pageSubtitle")
                subtitle_label.setWordWrap(True)
                layout.addWidget(subtitle_label)
            scroll.setWidget(content)
            return scroll, layout

        def _section_header(self, title: str, subtitle: str = "") -> QVBoxLayout:
            layout = QVBoxLayout()
            layout.setSpacing(3)
            layout.addWidget(label(title, "sectionTitle"))
            if subtitle:
                text = label(subtitle, "sectionSubtitle")
                text.setWordWrap(True)
                layout.addWidget(text)
            return layout

        def _metric_card(self, title: str, note: str) -> tuple[QFrame, QLabel]:
            frame, layout = card()
            value = label("—", "metricValue")
            layout.addWidget(label(title.upper(), "eyebrow"))
            layout.addWidget(value)
            layout.addWidget(label(note, "metricNote"))
            return frame, value

        def _build_home_page(self) -> QScrollArea:
            page, layout = self._scroll_page("Home", "")
            self.connection_banner, banner_layout = card("connectionBanner")
            self.connection_title = label("Connecting…", "bodyStrong")
            self.connection_detail = label("Starting CommunityAI.", "bodyMuted")
            self.connection_detail.setWordWrap(True)
            banner_layout.addWidget(self.connection_title)
            banner_layout.addWidget(self.connection_detail)
            self.retry_button = QPushButton("Try again")
            self.retry_button.clicked.connect(self._reset_connection)
            banner_layout.addWidget(self.retry_button)
            layout.addWidget(self.connection_banner)

            hero, hero_layout = card("heroCard")
            model_header = QHBoxLayout()
            model_header.addWidget(label("YOUR MODEL", "eyebrow"), 1)
            self.home_model_location = label("", "bodyMuted")
            model_header.addWidget(self.home_model_location)
            hero_layout.addLayout(model_header)
            self.hero_title = label("Checking your model…", "pageTitle")
            self.hero_title.setAccessibleName("Selected model")
            self.hero_subtitle = label("", "pageSubtitle")
            self.hero_subtitle.setWordWrap(True)
            hero_layout.addWidget(self.hero_title)
            hero_layout.addWidget(self.hero_subtitle)
            layout.addWidget(hero)

            hardware, hardware_layout = card()
            hardware_layout.addWidget(label("Your hardware", "sectionTitle"))
            hardware_grid = QFormLayout()
            hardware_grid.setVerticalSpacing(16)
            hardware_grid.setHorizontalSpacing(26)
            self.home_gpu = label("Checking…", "bodyStrong")
            self.home_cpu = label("Checking…", "bodyStrong")
            self.home_gpu.setWordWrap(True)
            self.home_cpu.setWordWrap(True)
            self.home_gpu.setAccessibleName("Graphics card")
            self.home_cpu.setAccessibleName("Processor")
            hardware_grid.addRow(label("Graphics card", "bodyMuted"), self.home_gpu)
            hardware_grid.addRow(label("Processor", "bodyMuted"), self.home_cpu)
            hardware_layout.addLayout(hardware_grid)
            self.home_hardware_detail = label("", "bodyMuted")
            self.home_hardware_detail.hide()
            hardware_layout.addWidget(self.home_hardware_detail)
            layout.addWidget(hardware)

            sharing, sharing_layout = card()
            row = QHBoxLayout()
            self.home_sharing_title = label("Sharing is off", "sectionTitle")
            row.addWidget(self.home_sharing_title, 1)
            self.home_share_button = QPushButton("Start sharing")
            self.home_share_button.setObjectName("primaryButton")
            self.home_share_button.setAccessibleName("Home sharing control")
            self.home_share_button.clicked.connect(self._toggle_all_sharing)
            row.addWidget(self.home_share_button)
            sharing_layout.addLayout(row)
            self.home_sharing_detail = label("", "bodyMuted")
            self.home_sharing_detail.setWordWrap(True)
            self.home_sharing_detail.hide()
            sharing_layout.addWidget(self.home_sharing_detail)
            limits = QHBoxLayout()
            vram = QVBoxLayout()
            vram.addWidget(label("GPU memory for sharing", "bodyMuted"))
            self.home_vram = label("Checking…", "sectionTitle")
            self.home_vram.setAccessibleName("GPU memory limit")
            vram.addWidget(self.home_vram)
            processing = QVBoxLayout()
            processing.addWidget(label("Computing power", "bodyMuted"))
            self.home_processing = label("100%", "sectionTitle")
            self.home_processing.setAccessibleName("Computing power limit")
            processing.addWidget(self.home_processing)
            limits.addLayout(vram, 1)
            limits.addLayout(processing, 1)
            settings = QPushButton("Change limits")
            settings.setObjectName("textButton")
            settings.clicked.connect(lambda: self._show_page(2))
            limits.addWidget(settings, 0, Qt.AlignBottom)
            sharing_layout.addLayout(limits)
            layout.addWidget(sharing)
            layout.addStretch(1)
            return page

        def _build_models_page(self) -> QScrollArea:
            page, layout = self._scroll_page("Models", "")
            selection, selection_layout = card()
            selection_row = QHBoxLayout()
            summary = QVBoxLayout()
            self.auto_selection_title = label("Checking your model…", "sectionTitle")
            self.auto_selection_title.setAccessibleName("Automatic model selection")
            self.auto_selection_detail = label("", "bodyMuted")
            self.auto_selection_detail.setWordWrap(True)
            summary.addWidget(self.auto_selection_title)
            summary.addWidget(self.auto_selection_detail)
            selection_row.addLayout(summary, 1)
            self.inference_mode_button = QPushButton("Use only this computer")
            self.inference_mode_button.setAccessibleName("Switch local-only inference")
            self.inference_mode_button.clicked.connect(self._toggle_inference_mode)
            selection_row.addWidget(self.inference_mode_button)
            selection_layout.addLayout(selection_row)
            layout.addWidget(selection)
            self.models_list_layout = QVBoxLayout()
            self.model_health_cards = {}
            self.models_list_layout.setSpacing(10)
            layout.addLayout(self.models_list_layout)
            privacy = label("Community members helping with your messages may see their contents.", "bodyMuted")
            privacy.setWordWrap(True)
            layout.addWidget(privacy)
            layout.addStretch(1)
            return page

        def _build_sharing_page(self) -> QScrollArea:
            page, layout = self._scroll_page("Sharing", "")
            hero, hero_layout = card("heroCard")
            top = QHBoxLayout()
            copy = QVBoxLayout()
            self.sharing_title = label("Sharing is off", "sectionTitle")
            self.sharing_detail = label("", "bodyMuted")
            self.sharing_detail.setWordWrap(True)
            copy.addWidget(self.sharing_title)
            copy.addWidget(self.sharing_detail)
            top.addLayout(copy, 1)
            self.master_share_button = QPushButton("Start sharing")
            self.master_share_button.setObjectName("primaryButton")
            self.master_share_button.clicked.connect(self._toggle_all_sharing)
            top.addWidget(self.master_share_button)
            hero_layout.addLayout(top)
            layout.addWidget(hero)
            self.sharing_downloads_layout = QVBoxLayout()
            self.sharing_download_cards = {}
            layout.addLayout(self.sharing_downloads_layout)

            memory_card, memory_layout = card()
            memory_layout.addWidget(label("How much to share", "sectionTitle"))
            self.memory_value = label("", "bodyStrong")
            self.memory_detail = label("", "bodyMuted")
            self.memory_detail.setWordWrap(True)
            memory_layout.addWidget(self.memory_value)
            memory_layout.addWidget(self.memory_detail)
            self.resource_controls = ResourceControls()
            self.resource_controls.apply_requested.connect(self._apply_resource_limits)
            memory_layout.addWidget(self.resource_controls)
            layout.addWidget(memory_card)

            advanced, advanced_layout = card()
            more = QPushButton("More settings")
            more.setCheckable(True)
            more.setObjectName("textButton")
            more.setAccessibleName("More sharing settings")
            advanced_layout.addWidget(more)
            body = QWidget()
            body_layout = QVBoxLayout(body)
            body_layout.setContentsMargins(0, 8, 0, 0)
            body_layout.setSpacing(14)
            self.login_startup_toggle = QCheckBox("Open CommunityAI when I sign in")
            self.login_startup_toggle.setAccessibleName("Start CommunityAI when I sign in")
            try:
                startup_enabled = login_startup_enabled()
                startup_detail = ""
            except LoginStartupError:
                startup_enabled = False
                startup_detail = "Sign-in settings could not be read."
                self.login_startup_toggle.setDisabled(True)
            self.login_startup_toggle.setChecked(startup_enabled)
            self.login_startup_detail = label(startup_detail, "bodyMuted")
            self.login_startup_detail.setVisible(bool(startup_detail))
            self.login_startup_toggle.toggled.connect(self._set_login_startup)
            body_layout.addWidget(self.login_startup_toggle)
            body_layout.addWidget(self.login_startup_detail)
            self.edit_policy_button = QPushButton("Storage, internet and schedule…")
            self.edit_policy_button.clicked.connect(self._edit_contribution_policy)
            body_layout.addWidget(self.edit_policy_button)
            self.contribution_models_layout = QVBoxLayout()
            body_layout.addLayout(self.contribution_models_layout)
            advanced_layout.addWidget(body)
            body.hide()

            def expand_settings(expanded):
                body.setVisible(expanded)
                more.setText("Hide settings" if expanded else "More settings")

            more.toggled.connect(expand_settings)
            layout.addWidget(advanced)
            layout.addStretch(1)
            return page

        def _build_api_page(self) -> QScrollArea:
            page, layout = self._scroll_page(
                "API access", "Connect ChatGPT-style apps and developer tools to CommunityAI."
            )
            endpoint_card, endpoint_layout = card("heroCard")
            endpoint_layout.addWidget(label("ENDPOINT URL", "eyebrow"))
            endpoint_layout.addWidget(label("Use this URL in your AI app", "sectionTitle"))
            endpoint_row = QHBoxLayout()
            self.api_endpoint = label("http://127.0.0.1:8080/v1", "endpointText")
            self.api_endpoint.setMinimumWidth(0)
            self.api_endpoint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.api_endpoint.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            endpoint_row.addWidget(self.api_endpoint, 1)
            self.copy_endpoint_button = QPushButton("Copy URL")
            self.copy_endpoint_button.setObjectName("primaryButton")
            self.copy_endpoint_button.clicked.connect(self._copy_endpoint)
            endpoint_row.addWidget(self.copy_endpoint_button)
            endpoint_layout.addLayout(endpoint_row)
            layout.addWidget(endpoint_card)

            keys_card, keys_layout = card()
            keys_header = QHBoxLayout()
            keys_header.addLayout(
                self._section_header("API keys", "Create one key for each app so you can revoke it anytime."), 1
            )
            self.create_key_button = QPushButton("Create API key")
            self.create_key_button.setObjectName("primaryButton")
            self.create_key_button.clicked.connect(self._create_key)
            keys_header.addWidget(self.create_key_button)
            keys_layout.addLayout(keys_header)
            self.keys_layout = QVBoxLayout()
            self.keys_layout.setSpacing(9)
            keys_layout.addLayout(self.keys_layout)
            layout.addWidget(keys_card)
            layout.addStretch(1)
            return page

        def _show_page(self, index: int) -> None:
            self.pages.setCurrentIndex(index)
            for button_index, button in enumerate(self._page_buttons):
                button.setChecked(button_index == index)

        def _set_connection_state(self, connected: bool) -> None:
            self.connection_banner.setVisible(not connected)
            self.retry_button.setVisible(not connected)
            self.sidebar_dot.setStyleSheet("color: #5EE1A2;" if connected else "color: #F3B76A;")
            self.sidebar_status.setText("Connected" if connected else "Connecting…")

        def _set_busy(self, change: int) -> None:
            self._busy = max(0, self._busy + change)
            busy = self._busy > 0
            self.retry_button.setDisabled(busy)
            self.create_key_button.setDisabled(busy or self._controller is None)
            self.inference_mode_button.setDisabled(
                busy
                or self._mode_pending
                or self._controller is None
                or not self._snapshot.get("inference_mode_editable", False)
            )
            contribution = self._snapshot.get("contribution", {})
            self.resource_controls.set_state(
                contribution, busy=busy or self._controller is None or self._sharing_pending is not None
            )
            self.edit_policy_button.setDisabled(
                busy
                or self._controller is None
                or not contribution.get("editable", False)
                or contribution.get("intent_enabled", False)
            )
            for button in (self.master_share_button, self.home_share_button):
                button.setDisabled(
                    busy
                    or self._sharing_pending is not None
                    or self._controller is None
                    or not contribution.get("editable", False)
                )
            for index in range(self.contribution_models_layout.count()):
                widget = self.contribution_models_layout.itemAt(index).widget()
                if widget is not None:
                    widget.setDisabled(busy or self._sharing_pending is not None or self._controller is None)

        def _submit(
            self,
            operation: Callable[[], Any],
            on_result: Callable[[Any], None],
            on_error: Callable[[str], None] | None = None,
            *,
            background: bool = False,
        ) -> None:
            if self._closing:
                return
            if background:
                self._refreshing = True
            else:
                self._change_version += 1
                self._set_busy(1)
            task = Task(operation)
            self._tasks.add(task)

            def finish(result: Any) -> None:
                self._tasks.discard(task)
                if self._closing:
                    return
                if background:
                    self._refreshing = False
                else:
                    self._set_busy(-1)
                on_result(result)

            def fail(message: str) -> None:
                self._tasks.discard(task)
                if self._closing:
                    return
                if background:
                    self._refreshing = False
                else:
                    self._set_busy(-1)
                (on_error or self._connection_failed)(message)

            task.signals.result.connect(finish)
            task.signals.error.connect(fail)
            self._pool.start(task)

        def refresh(self) -> None:
            if self._busy or self._refreshing:
                return
            if self._controller is None:
                self.sidebar_status.setText("Connecting")
                self._submit(connect, self._connected)
                return
            version = self._change_version

            def refreshed(snapshot):
                if version == self._change_version:
                    self._render(snapshot)
                else:
                    QTimer.singleShot(0, self.refresh)

            def refresh_failed(message):
                if version == self._change_version:
                    self._snapshot_failed(message)
                else:
                    QTimer.singleShot(0, self.refresh)

            self._submit(self._controller.snapshot, refreshed, refresh_failed, background=True)

        def _connected(self, connected_controller) -> None:  # noqa: ANN001
            self._controller = connected_controller
            self.refresh()

        def _connection_failed(self, message: str) -> None:
            self._set_connection_state(False)
            self.connection_title.setText("Could not connect to CommunityAI")
            self.connection_detail.setText("Try again. If this keeps happening, restart CommunityAI.")
            self.connection_detail.setToolTip(str(message)[:300])
            self.hero_title.setText("Model unavailable")
            self.hero_subtitle.setText("Waiting for CommunityAI to reconnect.")
            for widget in (self.sharing_title, self.home_sharing_title):
                widget.setText("Checking sharing…")
                widget.setStyleSheet("color: #F3C46C;")
            for widget in (self.sharing_detail, self.home_sharing_detail):
                widget.setText("Waiting for CommunityAI to reconnect.")
                widget.show()
            self._set_busy(0)

        def _snapshot_failed(self, message: str) -> None:
            self._controller = None
            self._connection_failed(message)

        def _reset_connection(self) -> None:
            self._controller = None
            self.refresh()

        def _render(self, snapshot: Dict[str, Any]) -> None:
            self._snapshot = snapshot
            if self._awaiting_sharing_snapshot:
                self._awaiting_sharing_snapshot = False
                self._sharing_pending = None
                self._sharing_error = None
            if self._awaiting_mode_snapshot:
                self._awaiting_mode_snapshot = False
                self._mode_pending = False
            self._set_busy(0)
            self._set_connection_state(True)
            name, reason, location = model_summary(snapshot)
            self.hero_title.setText(name)
            self.hero_subtitle.setText(reason)
            self.home_model_location.setText(location)
            self.auto_selection_title.setText(name)
            self.auto_selection_detail.setText(reason)
            self.api_endpoint.setText(snapshot["openai_base_url"])
            hardware = snapshot.get("hardware", {})
            self.home_gpu.setText(hardware.get("gpu_name") or "No supported graphics card detected")
            self.home_cpu.setText(hardware.get("cpu_name") or "Processor name unavailable")
            device = hardware.get("inference_device") or ""
            device_text = (
                (
                    "Your model runs on the graphics card."
                    if device.startswith("cuda")
                    else "Your model runs on the processor."
                    if device == "cpu"
                    else ""
                )
                if location == "On this computer"
                else ""
            )
            self.home_hardware_detail.setText(device_text)
            self.home_hardware_detail.setVisible(bool(device_text))
            self.inference_mode_button.setText(
                "Changing…"
                if self._mode_pending
                else "Use community models too"
                if snapshot.get("inference_mode") == "local_only"
                else "Use only this computer"
            )
            self._render_models(snapshot["models"])
            self._render_sharing(snapshot)
            self._render_keys(snapshot["keys"])

        def _render_models(self, models: list[Dict[str, Any]]) -> None:
            keys = {model["id"] for model in models}
            for key in list(self.model_health_cards):
                if key not in keys:
                    self.models_list_layout.removeWidget(self.model_health_cards[key])
                    self.model_health_cards.pop(key).deleteLater()
            for model in models:
                if model["id"] not in self.model_health_cards:
                    self.model_health_cards[model["id"]] = ModelHealthCard()
                    self.models_list_layout.addWidget(self.model_health_cards[model["id"]])
                self.model_health_cards[model["id"]].set_state(model, self._snapshot["workers"])

        def _render_sharing(self, snapshot: Dict[str, Any]) -> None:
            contribution = snapshot["contribution"]
            title, detail, state = sharing_summary(snapshot)
            if self._sharing_pending is not None:
                title = "Starting sharing…" if self._sharing_pending else "Stopping sharing…"
                detail = ""
            elif self._sharing_error:
                title, detail = "Sharing could not change", self._sharing_error
                state = "error"
            if self._sharing_pending is not None:
                state = "starting"
            for widget in (self.sharing_title, self.home_sharing_title):
                widget.setText(title)
                widget.setStyleSheet(
                    "color: "
                    + {
                        "running": "#72E7AE",
                        "starting": "#B6A5FF",
                        "waiting": "#F3C46C",
                        "error": "#EF8E9D",
                        "off": "#F4F6FA",
                        "paused": "#F4F6FA",
                    }[state]
                    + ";"
                )
            for widget in (self.sharing_detail, self.home_sharing_detail):
                widget.setText(detail)
                widget.setVisible(bool(detail))
            for button in (self.master_share_button, self.home_share_button):
                if self._sharing_pending is not None:
                    button.setText("Starting…" if self._sharing_pending else "Stopping…")
                else:
                    button.setText(
                        "Pause sharing" if contribution.get("intent_enabled") and state != "paused" else "Start sharing"
                    )
                button.setObjectName(
                    "ghostButton" if contribution.get("intent_enabled") and state != "paused" else "primaryButton"
                )
                button.style().unpolish(button)
                button.style().polish(button)
            self.resource_controls.set_state(
                contribution, busy=self._busy > 0 or self._controller is None or self._sharing_pending is not None
            )
            hardware = snapshot.get("hardware", {})
            total = hardware.get("gpu_total_bytes") or contribution.get("vram_pool_bytes")
            allowed = contribution.get("vram_bytes")
            if allowed is not None and total:
                self.home_vram.setText(f"{memory_text(allowed)} of {memory_text(total)}")
            elif not total:
                self.home_vram.setText("No GPU memory available")
            else:
                self.home_vram.setText("Checking memory limit…")
            percent = contribution.get(
                "processing_percent", (contribution.get("policy") or {}).get("max_processing_percent", 100)
            )
            self.home_processing.setText(f"{percent:g}%")
            self.memory_value.setText(hardware.get("gpu_name") or "")
            self.memory_value.setVisible(bool(hardware.get("gpu_name")))
            self.memory_detail.setText("")
            self.memory_detail.hide()
            downloads = {
                worker["id"]: worker
                for worker in snapshot.get("workers", [])
                if worker.get("download_progress")
                and worker["download_progress"].get("state") not in ("ready", "paused")
            }
            for key in list(self.sharing_download_cards):
                if key not in downloads:
                    self.sharing_downloads_layout.removeWidget(self.sharing_download_cards[key])
                    self.sharing_download_cards.pop(key).deleteLater()
            for key, worker in downloads.items():
                if key not in self.sharing_download_cards:
                    self.sharing_download_cards[key] = DownloadCard()
                    self.sharing_downloads_layout.addWidget(self.sharing_download_cards[key])
                name = "Sharing download" if worker["model"] == "auto" else model_name(worker["model"])
                self.sharing_download_cards[key].set_state(name, worker["download_progress"], worker["state"])
            clear_layout(self.contribution_models_layout)
            # Explicit per-model overrides remain in More settings; automatic
            # contribution needs only the main sharing switch.
            for worker in snapshot.get("workers", []):
                if (worker.get("placement") or {}).get("automatic"):
                    continue
                toggle = QCheckBox(model_name(worker.get("model")))
                toggle.setChecked(worker.get("desired_running", False))
                toggle.setEnabled(not self._busy and self._sharing_pending is None and self._controller is not None)
                toggle.setAccessibleName(f"Share compute with {worker['model']}")
                toggle.toggled.connect(
                    lambda enabled, worker_id=worker["id"]: self._set_model_sharing([worker_id], enabled)
                )
                self.contribution_models_layout.addWidget(toggle)

        def _render_keys(self, keys: list[Dict[str, Any]]) -> None:
            clear_layout(self.keys_layout)
            if not keys:
                self.keys_layout.addWidget(label("No API keys yet. Create one for your first app.", "bodyMuted"))
                return
            for key in keys:
                row = QFrame()
                row.setObjectName("listRow")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(14, 11, 12, 11)
                copy = QVBoxLayout()
                copy.setSpacing(2)
                copy.addWidget(label(key["label"], "bodyStrong"))
                copy.addWidget(label(f"•••• {key['fingerprint']}", "bodyMuted"))
                row_layout.addLayout(copy, 1)
                revoked = key["revoked_at"] is not None
                row_layout.addWidget(pill("Revoked" if revoked else "Active", "quiet" if revoked else "good"))
                if not revoked:
                    rename = QPushButton("Rename")
                    rename.setObjectName("textButton")
                    rename.clicked.connect(lambda checked=False, item=key: self._relabel_key(item["id"], item["label"]))
                    row_layout.addWidget(rename)
                    revoke = QPushButton("Revoke")
                    revoke.setObjectName("dangerButton")
                    revoke.clicked.connect(lambda checked=False, key_id=key["id"]: self._revoke_key(key_id))
                    row_layout.addWidget(revoke)
                self.keys_layout.addWidget(row)

        def _set_login_startup(self, enabled: bool) -> None:
            try:
                set_login_startup(enabled)
            except LoginStartupError as exc:
                self.login_startup_toggle.blockSignals(True)
                self.login_startup_toggle.setChecked(not enabled)
                self.login_startup_toggle.blockSignals(False)
                self.login_startup_detail.setText("Could not save this setting. Try again.")
                self.login_startup_detail.setToolTip(str(exc)[:180])
                self.login_startup_detail.show()
                QMessageBox.warning(self, "Login startup", str(exc)[:300])
                return
            self.login_startup_detail.setText(
                "CommunityAI will open when you sign in." if enabled else "Automatic opening is off."
            )
            self.login_startup_detail.show()

        def _apply_resource_limits(self, changes, revision) -> None:
            if self._controller is None or self._busy:
                return

            def applied(result):
                self.resource_controls.applied(result)
                self.refresh()

            def failed(message):
                self.resource_controls.failed(sharing_reason(message))
                self.resource_controls.message.setToolTip(str(message)[:300])
                self.refresh()

            self._submit(
                lambda: self._controller.update_resource_limits(changes, expected_revision=revision),
                applied,
                failed,
            )

        def _edit_contribution_policy(self) -> None:
            contribution = self._snapshot.get("contribution", {})
            policy = contribution.get("policy")
            revision = contribution.get("config_revision")
            if (
                self._controller is None
                or self._busy
                or not contribution.get("editable")
                or not isinstance(policy, dict)
                or not isinstance(revision, str)
            ):
                return
            if contribution.get("intent_enabled"):
                self._sharing_action_failed("Pause every contribution worker before editing sharing limits.")
                return

            dialog = QDialog(self)
            dialog.setObjectName("sharingPolicyDialog")
            dialog.setWindowTitle("Edit sharing limits")
            dialog.setMinimumWidth(620)
            layout = QVBoxLayout(dialog)
            explanation = label(
                "All values are validated and enforced by the local node. Leave optional limits blank to clear them.",
                "bodyMuted",
            )
            explanation.setWordWrap(True)
            layout.addWidget(explanation)
            form = QFormLayout()

            sharing_enabled = QCheckBox("Allow this node to share compute")
            sharing_enabled.setObjectName("policy_sharing_enabled")
            sharing_enabled.setChecked(policy["sharing_enabled"])
            form.addRow("Sharing", sharing_enabled)

            selector_fields = {}
            for field, title in (
                ("allowed_models", "Allowed models"),
                ("preferred_models", "Preferred models"),
                ("denied_models", "Denied models"),
            ):
                editor = QPlainTextEdit()
                editor.setObjectName(f"policy_{field}")
                editor.setPlainText("\n".join(policy[field]))
                editor.setPlaceholderText("One exact model selector per line")
                editor.setFixedHeight(64)
                editor.setAccessibleName(title)
                selector_fields[field] = editor
                form.addRow(title, editor)

            text_fields = {}
            for field, title, placeholder in (
                ("max_disk_space", "Storage ceiling", "20GiB"),
                ("max_vram", "GPU memory ceiling", "50% or 8GiB"),
                ("max_bandwidth_mbps", "Bandwidth ceiling (Mbps)", "optional"),
                ("max_power_watts", "Power ceiling (W)", "optional"),
                ("pause_timeout", "Pause timeout (seconds)", "10"),
            ):
                editor = QLineEdit()
                editor.setObjectName(f"policy_{field}")
                value = policy[field]
                editor.setText("" if value is None else f"{value:g}" if isinstance(value, float) else str(value))
                editor.setPlaceholderText(placeholder)
                editor.setAccessibleName(title)
                text_fields[field] = editor
                form.addRow(title, editor)

            schedule = QPlainTextEdit()
            schedule.setObjectName("policy_schedule")
            schedule.setPlainText("" if policy["schedule"] is None else json.dumps(policy["schedule"], indent=2))
            schedule.setPlaceholderText(
                '{"timezone":"local","windows":[{"days":["mon"],"start":"22:00","end":"06:00"}]}'
            )
            schedule.setFixedHeight(130)
            schedule.setAccessibleName("Contribution schedule JSON")
            form.addRow("Schedule (JSON)", schedule)
            layout.addLayout(form)

            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
            buttons.setObjectName("sharingPolicyButtons")
            buttons.accepted.connect(dialog.accept)
            buttons.rejected.connect(dialog.reject)
            layout.addWidget(buttons)
            if dialog.exec() != QDialog.Accepted:
                return

            def optional_text(field: str):
                raw = text_fields[field].text()
                return raw if raw.strip() else None

            try:
                updated = {
                    **policy,
                    "sharing_enabled": sharing_enabled.isChecked(),
                    **{
                        field: [line for line in editor.toPlainText().splitlines() if line.strip()]
                        for field, editor in selector_fields.items()
                    },
                    "max_disk_space": optional_text("max_disk_space"),
                    "max_vram": optional_text("max_vram"),
                    "max_bandwidth_mbps": (
                        None
                        if not text_fields["max_bandwidth_mbps"].text().strip()
                        else float(text_fields["max_bandwidth_mbps"].text())
                    ),
                    "max_power_watts": (
                        None
                        if not text_fields["max_power_watts"].text().strip()
                        else float(text_fields["max_power_watts"].text())
                    ),
                    "pause_timeout": float(text_fields["pause_timeout"].text()),
                    "schedule": None if not schedule.toPlainText().strip() else json.loads(schedule.toPlainText()),
                }
            except (TypeError, ValueError) as exc:
                self._sharing_action_failed(f"The sharing policy form is invalid: {str(exc)[:220]}")
                return
            self._submit(
                lambda: self._controller.update_contribution_policy(updated, expected_revision=revision),
                lambda result: self.refresh(),
                self._sharing_action_failed,
            )

        def _sharing_action_failed(self, message: str) -> None:
            self._sharing_pending = None
            self._awaiting_sharing_snapshot = False
            self._sharing_error = sharing_reason(message)
            self._render_sharing(self._snapshot)
            self.sharing_detail.setToolTip(str(message)[:300])
            self.home_sharing_detail.setToolTip(str(message)[:300])
            self._set_busy(0)
            self.refresh()

        def _sharing_changed(self, result) -> None:
            self._awaiting_sharing_snapshot = True
            self.refresh()

        def _set_model_sharing(self, worker_ids: list[str], enabled: bool) -> None:
            if not worker_ids or self._controller is None or self._busy:
                return
            self._sharing_pending = enabled
            self._sharing_error = None
            self._render_sharing(self._snapshot)
            controller = self._controller
            self._submit(
                lambda: controller.set_workers_enabled(worker_ids, enabled),
                self._sharing_changed,
                self._sharing_action_failed,
            )

        def _toggle_inference_mode(self) -> None:
            if self._controller is None or self._busy or self._mode_pending:
                return
            mode = "auto" if self._snapshot.get("inference_mode") == "local_only" else "local_only"
            controller = self._controller
            self._mode_pending = True
            self.inference_mode_button.setText("Changing…")

            def changed(_):
                self._awaiting_mode_snapshot = True
                self.refresh()

            def failed(message):
                self._mode_pending = False
                self._render(self._snapshot)
                self.auto_selection_detail.setText("Could not change this setting. Try again.")
                self.auto_selection_detail.setToolTip(str(message)[:300])

            self._submit(
                lambda: controller.client.set_inference_mode(mode),
                changed,
                failed,
            )

        def _toggle_all_sharing(self) -> None:
            if self._controller is None or self._busy or self._sharing_pending is not None:
                return
            enable = (
                not self._snapshot.get("contribution", {}).get("intent_enabled", False)
                or sharing_summary(self._snapshot)[2] == "paused"
            )
            self._sharing_pending = enable
            self._sharing_error = None
            self._render_sharing(self._snapshot)
            controller = self._controller
            self._submit(
                lambda: controller.set_sharing_enabled(enable),
                self._sharing_changed,
                self._sharing_action_failed,
            )

        def _copy_endpoint(self) -> None:
            QGuiApplication.clipboard().setText(self.api_endpoint.text())
            self.copy_endpoint_button.setText("Copied")
            QTimer.singleShot(2000, lambda: self.copy_endpoint_button.setText("Copy URL"))

        def _create_key(self) -> None:
            if self._controller is None:
                return
            name, accepted = QInputDialog.getText(self, "Create API key", "What app is this key for?", QLineEdit.Normal)
            if accepted and name.strip():
                self._submit(lambda: self._controller.create_client_key(name), self._show_key)

        def _show_key(self, result: Dict[str, Any]) -> None:
            secret = result["secret"]
            QGuiApplication.clipboard().setText(secret)
            message = QMessageBox(self)
            message.setWindowTitle("API key created")
            message.setTextFormat(Qt.PlainText)
            message.setText("Your API key was copied. Save it in your app now—it is shown only once.")
            message.setDetailedText(secret)
            message.exec()
            self.refresh()

        def _relabel_key(self, key_id: str, current: str) -> None:
            if self._controller is None:
                return
            name, accepted = QInputDialog.getText(self, "Rename API key", "Name", QLineEdit.Normal, current)
            if accepted and name.strip():
                self._submit(lambda: self._controller.relabel_client_key(key_id, name), lambda result: self.refresh())

        def _revoke_key(self, key_id: str) -> None:
            if self._controller is None:
                return
            answer = QMessageBox.question(
                self, "Revoke API key", "Revoke this key? The app using it will stop connecting."
            )
            if answer == QMessageBox.Yes:
                self._submit(lambda: self._controller.revoke_client_key(key_id), lambda result: self.refresh())

    application = QApplication.instance() or QApplication([])
    application.setApplicationName("CommunityAI")
    application.setOrganizationName("CommunityAI")
    application.setOrganizationDomain("communityai.org")
    application.setFont(QFont("Segoe UI", 10))
    bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    icon_path = (
        bundle_root / "communityai_desktop" / "assets" / "communityai.ico"
        if hasattr(sys, "_MEIPASS")
        else bundle_root / "assets" / "communityai.ico"
    )
    application.setWindowIcon(QIcon(str(icon_path)))
    application.setStyle("Fusion")
    application.setStyleSheet(APP_STYLESHEET)

    instance_server = None
    instance_lock = None
    instance_server_name = None
    shutdown_sockets = []
    if single_instance:
        data_location = QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)
        if not data_location:
            raise SingleInstanceError("the per-user application-data location is unavailable")
        data_root = Path(data_location)
        try:
            data_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SingleInstanceError(f"could not prepare the per-user instance directory: {exc}") from exc
        instance_server_name = instance_name or _single_instance_server_name(data_location)
        lock_digest = hashlib.sha256(instance_server_name.encode("utf-8")).hexdigest()[:20]
        instance_lock = QLockFile(str(data_root / f"instance-{lock_digest}.lock"))
        instance_lock.setStaleLockTime(0)

        def notify_existing_instance(timeout_ms: int) -> bool:
            socket = QLocalSocket(application)
            deadline = time.monotonic() + timeout_ms / 1_000
            socket.connectToServer(instance_server_name)
            while socket.state() != QLocalSocket.ConnectedState and time.monotonic() < deadline:
                # QLocalServer uses a named pipe on Windows. Pumping the event loop keeps
                # same-user activation responsive even while another instance is starting.
                application.processEvents()
                time.sleep(0.001)
            if socket.state() != QLocalSocket.ConnectedState:
                socket.abort()
                return False
            message = b"activate\n" if activate_existing_instance else b"silent\n"
            if socket.write(message) != len(message):
                socket.abort()
                return False
            while socket.bytesToWrite() and time.monotonic() < deadline:
                socket.flush()
                application.processEvents()
                time.sleep(0.001)
            delivered = socket.bytesToWrite() == 0
            socket.disconnectFromServer()
            return delivered

        if notify_existing_instance(250):
            return 0
        owns_instance_lock = instance_lock.tryLock(0)
        if not owns_instance_lock:
            if notify_existing_instance(1_000):
                return 0
            if instance_lock.removeStaleLockFile():
                owns_instance_lock = instance_lock.tryLock(0)
        if not owns_instance_lock:
            raise SingleInstanceError(
                "another CommunityAI instance is starting, but its activation endpoint is not ready"
            )

        # The lock makes stale endpoint removal ownership-safe: no successor can
        # claim this instance name until cleanup removes the endpoint and unlocks.
        QLocalServer.removeServer(instance_server_name)
        instance_server = QLocalServer(application)
        instance_server.setSocketOptions(QLocalServer.UserAccessOption)
        if not instance_server.listen(instance_server_name):
            error = instance_server.errorString()
            instance_lock.unlock()
            raise SingleInstanceError(f"could not establish the per-user CommunityAI instance endpoint: {error}")

    window = MainWindow()

    def stop_window_refreshes():
        window._closing = True
        window._timer.stop()

    application.aboutToQuit.connect(stop_window_refreshes)
    window._show_page(max(0, min(3, screenshot_page)))
    if start_minimized:
        window.showMinimized()
    else:
        window.show()

    if qualification_automation is not None:
        qualification_automation.install(
            window,
            application,
            {
                "QTimer": QTimer,
                "QDialog": QDialog,
                "QDialogButtonBox": QDialogButtonBox,
                "QCheckBox": QCheckBox,
                "QPlainTextEdit": QPlainTextEdit,
                "QLineEdit": QLineEdit,
            },
        )

    if instance_server is not None:

        def activate_window() -> None:
            should_activate = False
            should_shutdown = False
            while instance_server.hasPendingConnections():
                socket = instance_server.nextPendingConnection()
                socket.setReadBufferSize(64)
                socket.waitForReadyRead(250)
                raw_message = bytes(socket.read(64))
                message = raw_message.strip() if len(raw_message) <= 32 and socket.bytesAvailable() == 0 else b""
                should_activate = should_activate or message == b"activate"
                if message == b"shutdown":
                    shutdown_sockets.append(socket)
                    should_shutdown = True
                else:
                    socket.abort()
                    socket.deleteLater()
            if should_shutdown:
                application.quit()
                return
            if should_activate:
                window.showNormal()
                window.raise_()
                window.activateWindow()

        def close_instance_server() -> None:
            instance_server.close()
            if instance_server_name is not None:
                QLocalServer.removeServer(instance_server_name)
            if instance_lock is not None:
                instance_lock.unlock()

        instance_server.newConnection.connect(activate_window)
        if instance_server.hasPendingConnections():
            QTimer.singleShot(0, activate_window)

    if screenshot_path is not None:
        destination = Path(screenshot_path)

        def capture() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(destination)):
                raise RuntimeError(f"could not capture desktop screenshot to {destination}")

        QTimer.singleShot(600, capture)
    if auto_close_seconds is not None:
        QTimer.singleShot(max(1, int(float(auto_close_seconds) * 1000)), application.quit)
    restore_termination_handlers = _install_posix_termination_bridge(application, QTimer)

    def finish_desktop_cleanup():
        response = b"failed\n"
        try:
            if before_termination_restore is not None:
                before_termination_restore()
            response = b"stopped\n"
        finally:
            for socket in shutdown_sockets:
                socket.write(response)
                socket.flush()
                socket.waitForBytesWritten(1000)
                socket.disconnectFromServer()
            if instance_server is not None:
                close_instance_server()

    return _exec_with_termination_cleanup(
        application,
        restore_termination_handlers,
        finish_desktop_cleanup,
    )
