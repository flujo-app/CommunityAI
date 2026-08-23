"""Generate the small code-native product assets used by desktop packaging."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QLinearGradient, QPainter, QPainterPath


def main() -> int:
    destination = Path(__file__).parent / "src" / "communityai_desktop" / "assets" / "communityai.ico"
    destination.parent.mkdir(parents=True, exist_ok=True)

    image = QImage(256, 256, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)

    background = QPainterPath()
    background.addRoundedRect(QRectF(8, 8, 240, 240), 58, 58)
    gradient = QLinearGradient(32, 24, 228, 240)
    gradient.setColorAt(0, QColor("#9274FF"))
    gradient.setColorAt(0.55, QColor("#7255F4"))
    gradient.setColorAt(1, QColor("#4430B7"))
    painter.fillPath(background, gradient)

    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#FFFFFF"))
    painter.drawEllipse(QRectF(104, 45, 48, 86))
    painter.drawEllipse(QRectF(104, 125, 48, 86))
    painter.drawEllipse(QRectF(45, 104, 86, 48))
    painter.drawEllipse(QRectF(125, 104, 86, 48))
    painter.setBrush(QColor("#6548E4"))
    painter.drawEllipse(QRectF(106, 106, 44, 44))
    painter.end()

    if not image.save(str(destination), "ICO"):
        raise RuntimeError(f"could not write {destination}")
    print(destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
