"""Frameless liquid-glass window + an image view used by all editors."""
from __future__ import annotations

import sys

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QPushButton, QSizeGrip, QVBoxLayout,
                               QWidget)

from . import glass

IS_WIN = sys.platform == "win32"


def WAYLAND() -> bool:
    """The compositor places our windows and won't say where, so there's nothing to capture."""
    if not sys.platform.startswith("linux"):
        return False
    from . import platform_wayland
    return platform_wayland.is_wayland()


class GlassWindow(QWidget):
    """Top-level glass window with its own title bar. Put content in `self.body`."""

    def __init__(self, title: str, parent=None, size=(760, 640)):
        super().__init__(parent)
        self.setWindowTitle(f"{title} — DragShift")
        self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setObjectName("glassRoot")
        self.setStyleSheet(glass.QSS)
        self.resize(*size)
        self._drag: QPoint | None = None
        self._acrylic = False
        self.radius = 18
        self.liquid = glass.LiquidBacking(self, self.radius)
        self.liquid.changed.connect(self.update)
        self._fade = 0.0

        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 12)
        outer.setSpacing(8)

        bar = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setObjectName("title")
        bar.addWidget(self.title_label)
        bar.addStretch(1)
        close = QPushButton("✕")
        close.setObjectName("chrome")
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        outer.addLayout(bar)

        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)

        grip_row = QHBoxLayout()
        grip_row.addStretch(1)
        grip = QSizeGrip(self)
        grip.setFixedSize(14, 14)
        grip_row.addWidget(grip)
        grip_row.setContentsMargins(0, 0, 0, 0)
        outer.addLayout(grip_row)

    # ---- window behaviour
    def present(self):
        """Centre on the cursor's screen, capture what's behind, then show."""
        from PySide6.QtGui import QCursor, QGuiApplication
        scr = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(min(self.width(), scr.width() - 40), min(self.height(), scr.height() - 40))
        self.move(scr.center() - self.rect().center())
        self._placed = True
        if not WAYLAND():
            self.liquid.capture_at(self.frameGeometry())
        self.show()
        if sys.platform == "darwin":
            from . import platform_mac
            platform_mac.activate()   # menu-bar apps aren't brought forward automatically
        self.raise_()
        self.activateWindow()

    def showEvent(self, e):
        super().showEvent(e)
        if not getattr(self, "_placed", False):
            scr = self.screen().availableGeometry()
            self.move(scr.center() - self.rect().center())
            self._placed = True
            if not WAYLAND():
                self.liquid.capture_at(self.frameGeometry())
        self.liquid.excluded = glass.exclude_from_capture(int(self.winId()))

    def moveEvent(self, e):
        super().moveEvent(e)
        self._stale()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        glass.apply_shape(self, glass.rounded_rect_path(QRectF(self.rect()), self.radius))
        self._stale()

    def _stale(self):
        if not self.isVisible():
            return
        if self.liquid.excluded:
            self.liquid.moved()
        elif self.liquid.image is not None:
            # can't re-capture behind ourselves: switch to the system blur instead
            self.liquid.image = None
            if IS_WIN and not self._acrylic:
                self._acrylic = glass.enable_acrylic(int(self.winId()))
            self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and e.position().y() < 40:
            handle = self.windowHandle()
            if handle is not None and handle.startSystemMove():
                return  # native move: snapping etc.
            self._drag = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag is not None:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = glass.rounded_rect_path(r, self.radius)
        img = self.liquid.image
        if img is not None and not img.isNull() and img.size() == self.size():
            p.drawImage(0, 0, img)
            # large surfaces need more opacity than a menu so content stays readable
            glass.paint_glass(p, path, tint=QColor(22, 17, 34, 150), rainbow_alpha=150)
        else:
            tint = QColor(30, 24, 44, 125) if self._acrylic else QColor(38, 30, 56, 228)
            glass.paint_glass(p, path, tint=tint, rainbow_alpha=150)


class ImageView(QWidget):
    """Shows an image scaled to fit, with helpers to map between widget and image coords.
    Subclasses override `paint_overlay` and the mouse hooks."""

    changed = Signal()

    def __init__(self, image: QImage, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.setMouseTracking(True)
        self.set_image(image)

    def set_image(self, image: QImage):
        self.image = image
        self.pix = QPixmap.fromImage(image)
        self.update()

    # geometry
    def target_rect(self) -> QRectF:
        if self.image.isNull():
            return QRectF()
        avail = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        s = min(avail.width() / self.image.width(), avail.height() / self.image.height())
        w, h = self.image.width() * s, self.image.height() * s
        return QRectF(avail.center().x() - w / 2, avail.center().y() - h / 2, w, h)

    def scale(self) -> float:
        t = self.target_rect()
        return t.width() / max(1, self.image.width())

    def to_image(self, pt: QPointF) -> QPointF:
        t = self.target_rect()
        s = self.scale() or 1
        return QPointF((pt.x() - t.left()) / s, (pt.y() - t.top()) / s)

    def to_widget(self, pt: QPointF) -> QPointF:
        t = self.target_rect()
        s = self.scale()
        return QPointF(t.left() + pt.x() * s, t.top() + pt.y() * s)

    def rect_to_widget(self, r: QRectF) -> QRectF:
        a = self.to_widget(r.topLeft())
        b = self.to_widget(r.bottomRight())
        return QRectF(a, b)

    def clamp_image(self, pt: QPointF) -> QPointF:
        return QPointF(min(max(pt.x(), 0), self.image.width()), min(max(pt.y(), 0), self.image.height()))

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.setRenderHint(QPainter.Antialiasing)
        t = self.target_rect()
        # checkerboard so transparency is visible
        p.fillRect(t, QColor(255, 255, 255, 30))
        p.drawPixmap(t, self.pix, QRectF(self.pix.rect()))
        self.paint_overlay(p, t)

    def paint_overlay(self, p: QPainter, t: QRectF):
        pass
