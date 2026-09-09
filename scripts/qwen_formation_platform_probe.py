"""Reject hosts that cannot persist ordinary desktop settings atomically."""

import tempfile
from pathlib import Path

from drift.node.policy_store import _exchange_paths


def main():
    with tempfile.TemporaryDirectory(prefix="formation-platform-", dir="/srv/q38") as directory:
        left, right = Path(directory) / "left", Path(directory) / "right"
        left.write_text("before")
        right.write_text("after")
        _exchange_paths(left, right)
        if (left.read_text(), right.read_text()) != ("after", "before"):
            raise RuntimeError("Host does not preserve atomic settings exchange")
    from PySide6.QtWidgets import QApplication, QWidget

    app = QApplication([])
    window = QWidget()
    window.show()
    app.processEvents()
    if not window.isVisible():
        raise RuntimeError("Host cannot show the real desktop")


if __name__ == "__main__":
    main()
