"""Quiet frosted result notifications, bottom-right, stacked.

A card appears immediately while work runs (spinner), then turns into the result:
thumbnail, what changed (e.g. "2.4 MB → 610 KB · 75% smaller") and actions:
Open · Show in folder · Undo (moves the new files to the Recycle Bin)."""
from __future__ import annotations

from .i18n import tr, tr_items

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import (QEasingCurve, QFile, QRect, QRectF, Qt, QTimer, QUrl,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QDesktopServices, QFont, QFontMetrics,
                           QGuiApplication, QPainter, QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QWidget

from . import glass
from .theme import PANEL, TEXT, MUTED, BORDER, HOVER, SELECTED, GOOD, ERROR
from .util import ui_family
from .report import Result

W, H = 380, 104
GAP = 10
MARGIN = 18


def reveal(path: str | Path):
    path = str(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R", path])
    else:
        from . import platform_linux
        if not platform_linux.reveal(path):
            subprocess.Popen(["xdg-open", str(Path(path).parent)])


def open_file(path: str | Path):
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def trash(paths: list[Path]) -> int:
    n = 0
    for p in paths:
        p = Path(p)
        if p.is_file() or p.is_dir():
            result = QFile.moveToTrash(str(p))
            moved = result[0] if isinstance(result, tuple) else result
            if moved:
                n += 1
    # remove now-empty output folders we created (e.g. "x images/")
    for d in {Path(p).parent for p in paths}:
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    return n


class ToastCard(QWidget):
    closed = Signal(object)

    def __init__(self, stack: "ToastStack"):
        super().__init__(None)
        self.stack = stack
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setMouseTracking(True)
        self.resize(W, H)
        self.state = "working"     # working | done | error | info
        self.title = ""
        self.detail = ""
        self.good = ""
        self.result: Result | None = None
        self.thumb: QPixmap | None = None
        self.spin = 0.0
        self.hover_btn = -1
        self.buttons: list[tuple[str, QRectF]] = []
        self._fade = 0.0
        self.liquid = glass.LiquidBacking(self, 12, glass.neutral_style())
        self.liquid.changed.connect(self.update)
        self.fade = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=200,
                                      easingCurve=QEasingCurve.OutCubic)
        self.fade.valueChanged.connect(self._set_fade)
        self.spinner = QTimer(self, interval=16, timeout=self._spin)
        self.life = QTimer(self, singleShot=True, timeout=self.dismiss)

    # ------------------------------------------------------------ state
    def _clear_result(self):
        self.result = None
        self.thumb = None
        self.good = ""
        self.buttons = []
        self.hover_btn = -1

    def working(self, title: str, detail: str = ""):
        self._clear_result()
        self.life.stop()
        self.state, self.title, self.detail = "working", title, detail
        self.spinner.start()
        self.update()

    def done(self, r: Result):
        self.spinner.stop()
        self.state, self.result = "done", r
        self.title, self.detail, self.good = r.title, r.detail, r.good
        self.thumb = None
        if r.thumb is not None:
            pm = QPixmap(str(r.thumb))
            if not pm.isNull():
                self.thumb = pm.scaled(64, 64, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
        self.life.start(6500)
        self.update()

    def error(self, title: str, detail: str):
        self._clear_result()
        self.spinner.stop()
        self.state, self.title, self.detail = "error", title, detail
        self.life.start(9000)
        self.update()

    def info(self, title: str, detail: str, ms: int = 4500):
        self._clear_result()
        self.spinner.stop()
        self.state, self.title, self.detail = "info", title, detail
        self.life.start(ms)
        self.update()

    def _spin(self):
        self.spin = (self.spin + 7) % 360
        self.update()

    def _set_fade(self, v):
        self._fade = float(v)
        self.setWindowOpacity(self._fade)
        self.update()

    def appear(self, rect: QRect):
        self.setGeometry(rect)
        glass.apply_shape(self, glass.rounded_rect_path(QRectF(0, 0, W, H), 12))
        self.liquid.capture_at(rect)
        self.setWindowOpacity(0.0)
        self.show()
        glass.allow_capture(int(self.winId()))
        self.fade.start()

    def relocate(self, rect: QRect):
        if rect == self.geometry():
            return
        self.setGeometry(rect)
        self.liquid.moved()
        self.update()

    def dismiss(self):
        self.spinner.stop()
        self.life.stop()
        self.hide()
        self.closed.emit(self)
        self.deleteLater()

    # ------------------------------------------------------------ input
    def enterEvent(self, e):
        self.life.stop()

    def leaveEvent(self, e):
        self.hover_btn = -1
        if self.state in ("done", "info"):
            self.life.start(2500)
        elif self.state == "error":
            self.life.start(4000)
        self.update()

    def mouseMoveEvent(self, e):
        hb = -1
        for i, (_, r) in enumerate(self.buttons):
            if r.contains(e.position()):
                hb = i
        if hb != self.hover_btn:
            self.hover_btn = hb
            self.setCursor(Qt.PointingHandCursor if hb >= 0 else Qt.ArrowCursor)
            self.update()

    def mouseReleaseEvent(self, e):
        if QRectF(W - 30, 6, 24, 24).contains(e.position()):
            self.dismiss()
            return
        for name, r in self.buttons:
            if r.contains(e.position()):
                self._act(name)
                return
        if self.state == "done" and self.result and self.result.outs:
            reveal(self.result.outs[0])
            self.dismiss()

    def _act(self, name: str):
        r = self.result
        if not r:
            return
        if name == "Open":
            open_file(r.outs[0])
            self.dismiss()
        elif name == "Show":
            reveal(r.outs[0])
            self.dismiss()
        elif name == "Undo":
            n = trash(r.outs)
            self.result = None
            self.thumb = None
            self.good = ""
            bin_name = "Recycle Bin" if sys.platform == "win32" else "Trash"
            self.info("Undone", f"Moved {n} file{'s' if n != 1 else ''} to the {bin_name}", 2500)

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = glass.rounded_rect_path(r, 12)
        err = self.state == "error"
        image = self.liquid.image
        has_glass = image is not None and not image.isNull() and image.size() == self.size()
        if has_glass:
            p.drawImage(0, 0, image)
        p.setPen(QPen(QColor(BORDER), 1))
        p.setBrush(Qt.NoBrush if has_glass else QColor(PANEL))
        p.drawPath(path)

        x = 16
        # leading visual: spinner / thumbnail / icon
        lead = QRectF(14, (H - 56) / 2 - 8, 56, 56)
        if self.state == "working":
            p.setPen(QPen(QColor(BORDER), 2))
            p.drawEllipse(lead.center(), 16, 16)
            pen = QPen(QColor(TEXT), 2)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(QRectF(lead.center().x() - 16, lead.center().y() - 16, 32, 32), int(-self.spin * 16), 100 * 16)
            x = 82
        elif self.thumb is not None:
            clip = QPainterPath()
            clip.addRoundedRect(lead, 6, 6)
            p.save()
            p.setClipPath(clip)
            p.drawPixmap(lead.toRect(), self.thumb,
                         QRect((self.thumb.width() - 64) // 2, (self.thumb.height() - 64) // 2, 64, 64))
            p.restore()
            p.setPen(QPen(QColor(BORDER), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(lead, 6, 6)
            x = 82
        else:
            icon = {"done": "✓", "error": "!", "info": "↺"}.get(self.state, "✓")
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(HOVER))
            p.drawEllipse(lead.center(), 22, 22)
            f = QFont(ui_family(False))
            f.setPixelSize(22)
            f.setBold(True)
            p.setFont(f)
            p.setPen(QColor(GOOD if self.state == "done" else ERROR if err else MUTED))
            p.drawText(QRectF(lead.center().x() - 22, lead.center().y() - 22, 44, 44), Qt.AlignCenter, tr(icon))
            x = 82

        tw = W - x - 34
        f = QFont(ui_family(True))
        f.setPixelSize(15)
        f.setBold(True)
        p.setFont(f)
        fm = QFontMetrics(f)
        title = fm.elidedText(tr(self.title), Qt.ElideRight, int(tw))
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(x, 13, tw, 20), Qt.AlignLeft | Qt.AlignVCenter, tr(title))
        # highlighted figure chip (e.g. "75% smaller")
        if self.good:
            gx = x + fm.horizontalAdvance(tr(title)) + 8
            f2 = QFont(ui_family(False))
            f2.setPixelSize(11)
            f2.setBold(True)
            fm2 = QFontMetrics(f2)
            gw = fm2.horizontalAdvance(tr(self.good)) + 14
            if gx + gw < W - 34:
                chip = QRectF(gx, 14, gw, 18)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(SELECTED))
                p.drawRoundedRect(chip, 4, 4)
                p.setFont(f2)
                p.setPen(QColor(GOOD))
                p.drawText(chip, Qt.AlignCenter, tr(self.good))
        f.setPixelSize(12)
        f.setBold(False)
        p.setFont(f)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(x, 35, tw + 18, 34), Qt.AlignLeft | Qt.AlignTop | Qt.TextWordWrap,
                   tr(QFontMetrics(f).elidedText(tr(self.detail), Qt.ElideMiddle, int(tw * 1.9))))

        # close ×
        f.setPixelSize(13)
        p.setFont(f)
        p.setPen(QColor(MUTED))
        p.drawText(QRectF(W - 30, 6, 24, 24), Qt.AlignCenter, tr("✕"))

        # action buttons
        self.buttons = []
        if self.state == "done" and self.result and self.result.outs:
            names = ["Open", "Show", "Undo"]
            labels = {"Open": "Open", "Show": "Show in folder", "Undo": "Undo"}
            f.setPixelSize(12)
            f.setBold(True)
            p.setFont(f)
            fm = QFontMetrics(f)
            bx = x
            for i, nme in enumerate(names):
                bw = fm.horizontalAdvance(tr(labels[nme])) + 22
                br = QRectF(bx, H - 32, bw, 22)
                self.buttons.append((nme, br))
                p.setPen(QPen(QColor(BORDER), 1))
                p.setBrush(QColor(HOVER if self.hover_btn == i else PANEL))
                p.drawRoundedRect(br, 5, 5)
                p.setPen(QColor(TEXT))
                p.drawText(br, Qt.AlignCenter, tr(labels[nme]))
                bx += bw + 6


class ToastStack:
    """Owns the cards and keeps them stacked bottom-right of the active screen."""

    def __init__(self):
        self.cards: list[ToastCard] = []

    def _screen_rect(self) -> QRect:
        scr = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        return scr.availableGeometry()

    def _slot(self, i: int) -> QRect:
        g = self._screen_rect()
        x = g.right() - W - MARGIN
        y = g.bottom() - MARGIN - H - i * (H + GAP)
        return QRect(x, y, W, H)

    def new(self) -> ToastCard:
        c = ToastCard(self)
        c.closed.connect(self._closed)
        while len(self.cards) >= 4:
            self.cards[0].dismiss()
        self.cards.append(c)
        c.appear(self._slot(len(self.cards) - 1))
        return c

    def _closed(self, card):
        if card in self.cards:
            self.cards.remove(card)
        for i, c in enumerate(self.cards):
            c.relocate(self._slot(i))

    # conveniences
    def info(self, title: str, detail: str, ms: int = 4500) -> ToastCard:
        c = self.new()
        c.info(title, detail, ms)
        return c

    def error(self, title: str, detail: str) -> ToastCard:
        c = self.new()
        c.error(title, detail)
        return c
