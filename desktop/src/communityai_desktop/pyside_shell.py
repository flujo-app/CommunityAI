"""Modern PySide 6 product shell for the standalone CommunityAI node."""

from __future__ import annotations

import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, Callable, Dict

APP_STYLESHEET = """
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
QLabel#bodyMuted { color: #7F899D; font-size: 12px; }
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
) -> int:
    if controller is None and connect is None:
        raise ValueError("the desktop requires an initial controller or connector")

    from PySide6.QtCore import QObject, QRunnable, QSettings, Qt, QThreadPool, QTimer, Signal, Slot
    from PySide6.QtGui import QFont, QGuiApplication, QIcon
    from PySide6.QtWidgets import (
        QApplication,
        QButtonGroup,
        QCheckBox,
        QFrame,
        QHBoxLayout,
        QInputDialog,
        QLabel,
        QLineEdit,
        QMainWindow,
        QMessageBox,
        QProgressBar,
        QPushButton,
        QScrollArea,
        QSizePolicy,
        QSlider,
        QStackedWidget,
        QVBoxLayout,
        QWidget,
    )

    def label(text: str = "", name: str | None = None) -> QLabel:
        item = QLabel(text)
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
                self.signals.result.emit(self.operation())
            except Exception as exc:  # GUI boundary: show a friendly state and remain responsive.
                self.signals.error.emit(str(exc))

    class MainWindow(QMainWindow):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("CommunityAI")
            self.resize(1200, 800)
            self.setMinimumSize(960, 680)
            self._pool = QThreadPool.globalInstance()
            self._tasks = set()
            self._busy = 0
            self._controller = controller
            self._snapshot: Dict[str, Any] = {
                "models": [],
                "workers": [],
                "keys": [],
                "network": {"peer_count": 0, "regions": []},
                "contribution": {"enabled": False, "active_models": []},
            }
            self._settings = QSettings("CommunityAI", "CommunityAI")
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
            privacy_card, privacy_layout = card()
            privacy_card.setStyleSheet("QFrame#card { background: #0B1018; }")
            privacy_layout.setContentsMargins(13, 12, 13, 12)
            privacy_layout.setSpacing(4)
            privacy_layout.addWidget(label("A note on privacy", "bodyStrong"))
            note = label("Computers helping with a request may be able to see what was sent.", "privacySmall")
            note.setWordWrap(True)
            privacy_layout.addWidget(note)
            layout.addWidget(privacy_card)
            layout.addSpacing(12)

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
            subtitle_label = label(subtitle, "pageSubtitle")
            subtitle_label.setWordWrap(True)
            layout.addWidget(subtitle_label)
            layout.addSpacing(3)
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
            page, layout = self._scroll_page("Welcome home", "Everything you need, without the network homework.")

            self.connection_banner = QFrame()
            self.connection_banner.setObjectName("connectionBanner")
            banner_layout = QHBoxLayout(self.connection_banner)
            banner_layout.setContentsMargins(16, 13, 14, 13)
            banner_copy = QVBoxLayout()
            banner_copy.setSpacing(2)
            self.connection_title = label("Getting things ready…", "bodyStrong")
            self.connection_detail = label("CommunityAI connects automatically.", "bodyMuted")
            banner_copy.addWidget(self.connection_title)
            banner_copy.addWidget(self.connection_detail)
            banner_layout.addLayout(banner_copy, 1)
            self.retry_button = QPushButton("Try again")
            self.retry_button.setObjectName("ghostButton")
            self.retry_button.clicked.connect(self._reset_connection)
            self.retry_button.hide()
            banner_layout.addWidget(self.retry_button)
            layout.addWidget(self.connection_banner)

            hero, hero_layout = card("heroCard")
            hero_row = QHBoxLayout()
            hero_copy = QVBoxLayout()
            hero_copy.setSpacing(5)
            hero_copy.addWidget(label("LOCAL AI", "eyebrow"))
            self.hero_title = label("Your AI is getting ready", "pageTitle")
            self.hero_title.setStyleSheet("font-size: 23px;")
            hero_copy.addWidget(self.hero_title)
            self.hero_subtitle = label("Your apps will connect here automatically.", "pageSubtitle")
            hero_copy.addWidget(self.hero_subtitle)
            hero_row.addLayout(hero_copy, 1)
            endpoint_box = QVBoxLayout()
            endpoint_box.setSpacing(7)
            endpoint_box.addWidget(label("ENDPOINT URL", "eyebrow"))
            endpoint_row = QHBoxLayout()
            self.endpoint = label("http://127.0.0.1:8080/v1", "endpointText")
            self.endpoint.setMinimumWidth(0)
            self.endpoint.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            self.endpoint.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            self.endpoint.setAccessibleName("Local API endpoint URL")
            endpoint_row.addWidget(self.endpoint, 1)
            copy_button = QPushButton("Copy")
            copy_button.clicked.connect(self._copy_endpoint)
            endpoint_row.addWidget(copy_button)
            endpoint_box.addLayout(endpoint_row)
            hero_row.addLayout(endpoint_box, 1)
            hero_layout.addLayout(hero_row)
            layout.addWidget(hero)

            metrics = QHBoxLayout()
            model_metric, self.models_metric = self._metric_card("Models ready", "Available to your apps")
            peer_metric, self.peers_metric = self._metric_card("Peers online", "Across the community")
            region_metric, self.regions_metric = self._metric_card("World regions", "Community around the world")
            metrics.addWidget(model_metric)
            metrics.addWidget(peer_metric)
            metrics.addWidget(region_metric)
            layout.addLayout(metrics)

            lower = QHBoxLayout()
            models_card, models_layout = card()
            models_header = QHBoxLayout()
            models_header.addLayout(self._section_header("Ready to use", "Available models"), 1)
            view_models = QPushButton("View all")
            view_models.setObjectName("textButton")
            view_models.clicked.connect(lambda: self._show_page(1))
            models_header.addWidget(view_models)
            models_layout.addLayout(models_header)
            self.home_models_layout = QVBoxLayout()
            self.home_models_layout.setSpacing(8)
            models_layout.addLayout(self.home_models_layout)
            models_layout.addStretch(1)

            regions_card, regions_layout = card()
            regions_layout.addLayout(self._section_header("Around the world", "Peers by region"))
            self.region_layout = QVBoxLayout()
            self.region_layout.setSpacing(6)
            regions_layout.addLayout(self.region_layout)
            regions_layout.addStretch(1)
            lower.addWidget(models_card, 3)
            lower.addWidget(regions_card, 2)
            layout.addLayout(lower)
            layout.addStretch(1)
            return page

        def _build_models_page(self) -> QScrollArea:
            page, layout = self._scroll_page(
                "Models", "Pick a model in your AI app. CommunityAI connects you automatically."
            )
            info, info_layout = card("heroCard")
            info_layout.addWidget(label("COMMUNITY LIBRARY", "eyebrow"))
            info_layout.addWidget(label("One place. Every available model.", "sectionTitle"))
            info_layout.addWidget(
                label("Models appear here when enough community computers are online to run them.", "sectionSubtitle")
            )
            layout.addWidget(info)
            self.models_list_layout = QVBoxLayout()
            self.models_list_layout.setSpacing(10)
            layout.addLayout(self.models_list_layout)
            layout.addStretch(1)
            return page

        def _build_sharing_page(self) -> QScrollArea:
            page, layout = self._scroll_page("Sharing", "Help the community when it suits you. You stay in control.")
            hero, hero_layout = card("heroCard")
            top = QHBoxLayout()
            copy = QVBoxLayout()
            copy.setSpacing(4)
            copy.addWidget(label("SHARING", "eyebrow"))
            self.sharing_title = label("Your GPU is not sharing right now", "sectionTitle")
            self.sharing_detail = label("Choose models below, then start whenever you're ready.", "sectionSubtitle")
            copy.addWidget(self.sharing_title)
            copy.addWidget(self.sharing_detail)
            top.addLayout(copy, 1)
            self.master_share_button = QPushButton("Start sharing")
            self.master_share_button.setObjectName("primaryButton")
            self.master_share_button.clicked.connect(self._toggle_all_sharing)
            top.addWidget(self.master_share_button)
            hero_layout.addLayout(top)
            layout.addWidget(hero)

            memory_card, memory_layout = card()
            memory_header = QHBoxLayout()
            memory_header.addLayout(
                self._section_header(
                    "GPU memory to share", "Choose a comfortable target. It's saved on this computer."
                ),
                1,
            )
            self.memory_value = label("50%", "metricValue")
            self.memory_value.setStyleSheet("font-size: 22px;")
            memory_header.addWidget(self.memory_value)
            memory_layout.addLayout(memory_header)
            self.gpu_label = label("Your GPU", "bodyMuted")
            memory_layout.addWidget(self.gpu_label)
            self.memory_slider = QSlider(Qt.Horizontal)
            self.memory_slider.setRange(10, 100)
            self.memory_slider.setSingleStep(5)
            saved_percent = int(self._settings.value("sharing/gpuMemoryPercent", 50))
            self.memory_slider.setValue(max(10, min(100, saved_percent)))
            self.memory_slider.valueChanged.connect(self._memory_changed)
            self.memory_slider.sliderReleased.connect(self._save_memory_preference)
            self.memory_slider.setAccessibleName("GPU memory to share")
            memory_layout.addWidget(self.memory_slider)
            scale = QHBoxLayout()
            scale.addWidget(label("Keep more for me", "metricNote"))
            scale.addStretch(1)
            scale.addWidget(label("Share more", "metricNote"))
            memory_layout.addLayout(scale)
            layout.addWidget(memory_card)

            selection_card, selection_layout = card()
            selection_layout.addLayout(
                self._section_header("Models you want to help", "Turn models on or off. CommunityAI handles the rest.")
            )
            self.contribution_models_layout = QVBoxLayout()
            self.contribution_models_layout.setSpacing(9)
            selection_layout.addLayout(self.contribution_models_layout)
            layout.addWidget(selection_card)

            privacy, privacy_layout = card()
            privacy_layout.addWidget(label("A quick privacy note", "bodyStrong"))
            privacy_text = label(
                "When sharing is on, your computer helps process requests. Their content may be visible to you or "
                "software running on your computer.",
                "bodyMuted",
            )
            privacy_text.setWordWrap(True)
            privacy_layout.addWidget(privacy_text)
            layout.addWidget(privacy)
            layout.addStretch(1)
            self._memory_changed(self.memory_slider.value())
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
            copy = QPushButton("Copy URL")
            copy.setObjectName("primaryButton")
            copy.clicked.connect(self._copy_endpoint)
            endpoint_row.addWidget(copy)
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
            self.connection_banner.setProperty("connectionState", "online" if connected else "offline")
            self.connection_banner.style().unpolish(self.connection_banner)
            self.connection_banner.style().polish(self.connection_banner)
            if connected:
                self.connection_title.setText("Everything is connected")
                self.connection_detail.setText("Your local AI is ready for apps and sharing.")
                self.retry_button.hide()
                self.sidebar_dot.setStyleSheet("color: #5EE1A2;")
                self.sidebar_status.setText("Online")
            else:
                self.connection_title.setText("CommunityAI is still getting ready")
                self.connection_detail.setText("It isn't ready yet. We'll keep trying automatically.")
                self.retry_button.show()
                self.sidebar_dot.setStyleSheet("color: #F3B76A;")
                self.sidebar_status.setText("Getting ready")

        def _set_busy(self, change: int) -> None:
            self._busy += change
            busy = self._busy > 0
            self.retry_button.setDisabled(busy)
            self.create_key_button.setDisabled(busy or self._controller is None)
            self.master_share_button.setDisabled(busy or not self._snapshot.get("workers"))

        def _submit(
            self,
            operation: Callable[[], Any],
            on_result: Callable[[Any], None],
            on_error: Callable[[str], None] | None = None,
        ) -> None:
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
                (on_error or self._connection_failed)(message)

            task.signals.result.connect(finish)
            task.signals.error.connect(fail)
            self._pool.start(task)

        def refresh(self) -> None:
            if self._busy:
                return
            if self._controller is None:
                self.sidebar_status.setText("Connecting")
                self._submit(connect, self._connected)
                return
            self._submit(self._controller.snapshot, self._render, self._snapshot_failed)

        def _connected(self, connected_controller) -> None:  # noqa: ANN001
            self._controller = connected_controller
            self.refresh()

        def _connection_failed(self, message: str) -> None:
            self._set_connection_state(False)
            self.connection_detail.setText(str(message)[:300])
            self.hero_title.setText("Your AI will appear here")
            self.hero_subtitle.setText("CommunityAI connects automatically as soon as it is ready.")

        def _snapshot_failed(self, message: str) -> None:
            self._controller = None
            self._connection_failed(message)

        def _reset_connection(self) -> None:
            self._controller = None
            self.refresh()

        def _render(self, snapshot: Dict[str, Any]) -> None:
            self._snapshot = snapshot
            self._set_busy(0)
            self._set_connection_state(True)
            self.hero_title.setText("Your local AI is ready")
            self.hero_subtitle.setText("Use community models from any compatible app on this computer.")
            endpoint = snapshot["openai_base_url"]
            self.endpoint.setText(endpoint)
            self.api_endpoint.setText(endpoint)

            ready_models = [model for model in snapshot["models"] if model["state"] in ("ready", "known")]
            self.models_metric.setText(str(len(ready_models)))
            network = snapshot["network"]
            self.peers_metric.setText(str(network["peer_count"]))
            self.regions_metric.setText(str(len(network["regions"])))
            self._render_home_models(snapshot["models"])
            self._render_regions(network["regions"])
            self._render_models(snapshot["models"])
            self._render_sharing(snapshot)
            self._render_keys(snapshot["keys"])

        def _model_row(self, model: Dict[str, Any]) -> QFrame:
            row = QFrame()
            row.setObjectName("listRow")
            layout = QHBoxLayout(row)
            layout.setContentsMargins(12, 10, 12, 10)
            layout.setSpacing(12)
            avatar = label(model["id"][:1].upper(), "avatar")
            avatar.setFixedSize(38, 38)
            layout.addWidget(avatar)
            copy = QVBoxLayout()
            copy.setSpacing(2)
            copy.addWidget(label(model["id"], "bodyStrong"))
            peers = model.get("peer_count")
            detail = model["coverage"]
            if isinstance(peers, int):
                availability = "Available now" if model["state"] in ("ready", "known") else "Less available"
                detail = f"{peers} peers  •  {availability}"
            copy.addWidget(label(detail, "bodyMuted"))
            layout.addLayout(copy, 1)
            state = model["state"]
            tone = "good" if state in ("ready", "known") else "warn"
            layout.addWidget(pill("Ready" if tone == "good" else "Limited", tone))
            return row

        def _render_home_models(self, models: list[Dict[str, Any]]) -> None:
            clear_layout(self.home_models_layout)
            if not models:
                self.home_models_layout.addWidget(label("Models will appear when the network is ready.", "bodyMuted"))
                return
            for model in models[:3]:
                self.home_models_layout.addWidget(self._model_row(model))

        def _render_models(self, models: list[Dict[str, Any]]) -> None:
            clear_layout(self.models_list_layout)
            if not models:
                self.models_list_layout.addWidget(label("No models are available yet.", "bodyMuted"))
                return
            for model in models:
                self.models_list_layout.addWidget(self._model_row(model))

        def _render_regions(self, regions: list[Dict[str, Any]]) -> None:
            clear_layout(self.region_layout)
            if not regions:
                self.region_layout.addWidget(label("Region view is warming up.", "bodyMuted"))
                return
            maximum = max((region["peers"] for region in regions), default=1)
            for region in regions:
                line = QVBoxLayout()
                header = QHBoxLayout()
                header.addWidget(label(region["name"], "bodyMuted"))
                header.addStretch(1)
                header.addWidget(label(str(region["peers"]), "bodyStrong"))
                line.addLayout(header)
                bar = QProgressBar()
                bar.setRange(0, maximum)
                bar.setValue(region["peers"])
                bar.setTextVisible(False)
                line.addWidget(bar)
                self.region_layout.addLayout(line)

        def _render_sharing(self, snapshot: Dict[str, Any]) -> None:
            contribution = snapshot["contribution"]
            workers = snapshot["workers"]
            enabled = contribution["enabled"]
            active_models = contribution["active_models"]
            if enabled:
                self.sharing_title.setText(f"You're helping with {', '.join(active_models)}")
                self.sharing_detail.setText("CommunityAI shares the models you selected below.")
                self.master_share_button.setText("Pause sharing")
                self.master_share_button.setObjectName("ghostButton")
            else:
                self.sharing_title.setText("Your GPU is not sharing right now")
                self.sharing_detail.setText("Choose models below, then start whenever you're ready.")
                self.master_share_button.setText("Start sharing")
                self.master_share_button.setObjectName("primaryButton")
            self.master_share_button.style().unpolish(self.master_share_button)
            self.master_share_button.style().polish(self.master_share_button)

            total = contribution.get("gpu_memory_total_bytes")
            gpu_name = contribution.get("gpu_name") or "Your GPU"
            total_text = _gib_text(total)
            self.gpu_label.setText(f"{gpu_name}  •  {total_text}" if total_text else gpu_name)

            clear_layout(self.contribution_models_layout)
            workers_by_model: Dict[str, list[Dict[str, Any]]] = {}
            for worker in workers:
                workers_by_model.setdefault(worker["model"], []).append(worker)
            for model in snapshot["models"]:
                model_workers = workers_by_model.get(model["id"], [])
                row = QFrame()
                row.setObjectName("listRow")
                row_layout = QHBoxLayout(row)
                row_layout.setContentsMargins(14, 12, 14, 12)
                avatar = label(model["id"][:1].upper(), "avatar")
                avatar.setFixedSize(38, 38)
                row_layout.addWidget(avatar)
                copy = QVBoxLayout()
                copy.setSpacing(2)
                copy.addWidget(label(model["id"], "bodyStrong"))
                active = any(worker["desired_running"] for worker in model_workers)
                copy.addWidget(
                    label(
                        "Sharing" if active else ("Not sharing" if model_workers else "Available after sharing setup"),
                        "bodyMuted",
                    )
                )
                row_layout.addLayout(copy, 1)
                toggle = QCheckBox()
                toggle.setChecked(active)
                toggle.setEnabled(bool(model_workers) and self._busy == 0)
                toggle.setAccessibleName(f"Share GPU with {model['id']}")
                worker_ids = [worker["id"] for worker in model_workers]
                toggle.stateChanged.connect(
                    lambda state, ids=worker_ids: self._set_model_sharing(ids, state == Qt.Checked.value)
                )
                row_layout.addWidget(toggle)
                self.contribution_models_layout.addWidget(row)

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

        def _memory_changed(self, value: int) -> None:
            total = self._snapshot.get("contribution", {}).get("gpu_memory_total_bytes")
            total_text = _gib_text(int(total * value / 100)) if total else ""
            self.memory_value.setText(f"{value}%" + (f"  ·  {total_text}" if total_text else ""))

        def _save_memory_preference(self) -> None:
            self._settings.setValue("sharing/gpuMemoryPercent", self.memory_slider.value())

        def _set_model_sharing(self, worker_ids: list[str], enabled: bool) -> None:
            if not worker_ids or self._controller is None or self._busy:
                return
            self._submit(
                lambda: self._controller.set_workers_enabled(worker_ids, enabled), lambda result: self.refresh()
            )

        def _toggle_all_sharing(self) -> None:
            workers = self._snapshot.get("workers", [])
            if not workers or self._controller is None:
                return
            enable = not self._snapshot.get("contribution", {}).get("enabled", False)
            worker_ids = [worker["id"] for worker in workers]
            self._submit(
                lambda: self._controller.set_workers_enabled(worker_ids, enable), lambda result: self.refresh()
            )

        def _copy_endpoint(self) -> None:
            QGuiApplication.clipboard().setText(self.endpoint.text())
            self.connection_detail.setText("Endpoint URL copied")

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
    window = MainWindow()
    window._show_page(max(0, min(3, screenshot_page)))
    window.show()

    if screenshot_path is not None:
        destination = Path(screenshot_path)

        def capture() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not window.grab().save(str(destination)):
                raise RuntimeError(f"could not capture desktop screenshot to {destination}")

        QTimer.singleShot(600, capture)
    if auto_close_seconds is not None:
        QTimer.singleShot(max(1, int(float(auto_close_seconds) * 1000)), application.quit)
    return application.exec()
