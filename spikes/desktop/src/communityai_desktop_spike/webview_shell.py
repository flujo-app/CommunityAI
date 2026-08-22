"""pywebview implementation of the fixed desktop-spike workflow."""

from __future__ import annotations

import threading
import time
from importlib.metadata import version
from importlib.resources import files
from typing import Any, Callable, Dict


def check_runtime() -> Dict[str, str]:
    import webview  # noqa: F401

    return {
        "shell": "webview",
        "framework": "pywebview",
        "version": version("pywebview"),
    }


class _Bridge:
    def __init__(self, controller):  # noqa: ANN001
        self._controller = controller
        self._lock = threading.Lock()

    def _call(self, operation: Callable[[], Any]) -> Dict[str, Any]:
        try:
            with self._lock:
                return {"ok": True, "value": operation()}
        except Exception as exc:  # pywebview RPC boundary: return an inert error string.
            return {"ok": False, "error": str(exc)}

    def snapshot(self) -> Dict[str, Any]:
        return self._call(self._controller.snapshot)

    def worker_action(self, worker_id: str, action: str) -> Dict[str, Any]:
        return self._call(lambda: self._controller.worker_action(worker_id, action))

    def create_client_key(self, label: str) -> Dict[str, Any]:
        return self._call(lambda: self._controller.create_client_key(label))


def run(controller, *, auto_close_seconds=None) -> int:  # noqa: ANN001
    import webview

    html = files("communityai_desktop_spike").joinpath("webview.html").read_text(encoding="utf-8")
    window = webview.create_window(
        "CommunityAI desktop shell spike — webview",
        html=html,
        js_api=_Bridge(controller),
        width=1000,
        height=720,
        min_size=(720, 520),
    )
    if auto_close_seconds is None:
        webview.start(debug=False)
    else:

        def close_after_load():
            time.sleep(max(0.1, float(auto_close_seconds)))
            window.destroy()

        webview.start(close_after_load, debug=False)
    return 0
