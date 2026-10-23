"""The radial drop menu that pops up under the cursor while you Shift+drag a file.

The background is captured before opening and refracted in a worker. A light
neutral tint keeps the labels readable while retaining the frosted lens edges."""
from __future__ import annotations

from .i18n import tr, tr_items

import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QPointF, QRectF, QRunnable, Qt,
                            QThreadPool, QTimer, QVariantAnimation, Signal, Slot)
from PySide6.QtGui import (QColor, QCursor, QFont, QGuiApplication, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QWidget

from . import glass
from .theme import PANEL, TEXT, MUTED, BORDER, HOVER, SELECTED
from .util import resolve_shortcuts, ui_family


@dataclass
class Item:
    label: str
    glyph: str = ""
    run: Callable[[list[str]], None] | None = None
    key: str = ""
    hint: str = ""


SIZE = 400          # window size, with a clear margin around the ring
R_OUT = 158         # outer radius of petals
R_IN = 60           # inner radius of petals
R_HUB = 50          # hub radius
GAP_DEG = 3.2


def sector_path(c: QPointF, r0: float, r1: float, a0: float, a1: float) -> QPainterPath:
    """Annular sector between angles a0..a1 (degrees, Qt convention: 0 = 3 o'clock, CCW)."""
    path = QPainterPath()
    outer = QRectF(c.x() - r1, c.y() - r1, 2 * r1, 2 * r1)
    inner = QRectF(c.x() - r0, c.y() - r0, 2 * r0, 2 * r0)
    path.arcMoveTo(outer, a0)
    path.arcTo(outer, a0, a1 - a0)
    path.arcTo(inner, a1, -(a1 - a0))
    path.closeSubpath()
    return path


def angles(n: int, i: int) -> tuple[float, float]:
    span = 360.0 / n
    start = 90 + span / 2 - (i + 1) * span   # first petal centred at 12 o'clock, clockwise
    return start + GAP_DEG / 2, start + span - GAP_DEG / 2


class _RenderSig(QObject):
    done = Signal(int, int, QImage)


class _RenderJob(QRunnable):
    def __init__(self, serial: int, n: int, backdrop):
        super().__init__()
        self.serial, self.n, self.backdrop = serial, n, backdrop
        self.sig = _RenderSig()

    def run(self):
        image = QImage()
        try:
            from . import liquid
            center = SIZE / 2
            fields = [liquid.sdf_circle(SIZE, SIZE, center, center, R_HUB)]
            for i in range(self.n):
                a0, a1 = angles(self.n, i)
                fields.append(liquid.sdf_ring_sector(SIZE, SIZE, center, center,
                                                     R_IN, R_OUT, a0, a1, 0))
            rendered, _ = liquid.render(self.backdrop, liquid.union(*fields),
                                        glass.neutral_style(), tint=glass.neutral_tint())
            image = glass.qimage_from_pil(rendered)
        except Exception:  # The neutral painted surface remains usable if rendering fails.
            import traceback
            traceback.print_exc()
        try:
            self.sig.done.emit(self.serial, self.n, image)
        except RuntimeError:
            pass


class RadialMenu(QWidget):
    activated = Signal(object, list)     # Item, paths
    dismissed = Signal()

    def __init__(self, build_rings: Callable[[list[str]], dict[str, list[Item]]],
                 describe: Callable[[list[str]], str]):
        super().__init__(None)
        self.build_rings = build_rings
        self.describe = describe
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.WindowDoesNotAcceptFocus | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.resize(SIZE, SIZE)

        self.paths: list[str] = []
        self.rings: dict[str, list[Item]] = {}
        self.ring_names: list[str] = []
        self.ring_idx = 0
        self.hover = -1          # petal index, -2 = hub, -1 = none
        self.cursor_pos = QPointF(SIZE / 2, SIZE / 2)
        self.hub_text = ""
        self.backdrop: QPixmap | None = None
        self._raw_backdrop = None
        self.liquid: dict[int, QImage] = {}
        self._serial = 0
        self.got_drag = False
        self.click_mode = False  # opened from tray/menu instead of a drag
        self._t = 1.0            # pop-in animation 0..1
        self._swap = 1.0         # ring-swap animation 0..1
        self.spring: list[float] = []   # per-petal hover amount 0..1 (eased every frame)
        self.hub_spring = 0.0

        self.anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=150,
                                      easingCurve=QEasingCurve.OutCubic)
        self.anim.valueChanged.connect(self._set_t)
        self.swap_anim = QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=220,
                                           easingCurve=QEasingCurve.OutCubic)
        self.swap_anim.valueChanged.connect(self._set_swap)
        self.frame = QTimer(self, interval=16, timeout=self._tick)   # 60 fps springs

    # ------------------------------------------------------------ show / hide
    def popup(self, pos: QPoint | None = None, paths: list[str] | None = None):
        pos = pos or QCursor.pos()
        tl = pos - QPoint(SIZE // 2, SIZE // 2)
        scr = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        g = scr.geometry()
        tl.setX(min(max(tl.x(), g.left()), g.right() - SIZE))
        tl.setY(min(max(tl.y(), g.top()), g.bottom() - SIZE))
        self._serial += 1
        self.liquid = {}
        self._capture(tl, pos, paths)
        self.move(tl)
        self.hover = -1
        self.got_drag = False
        self.click_mode = paths is not None
        early = []
        if paths is None and sys.platform == "darwin":
            from . import platform_mac
            early = platform_mac.drag_files()
            if early:
                print(f"menu: {len(early)} file(s) from the drag pasteboard", flush=True)
        if paths is not None:
            self._load(paths)
        elif early:
            self._load(early)           # slices show at once; drop still comes via dragEnter
        else:
            self.paths, self.rings, self.ring_names = [], {}, []
            self.hub_text = "Drop here"
            for n in (4, 7):
                self._render(n)
        self._t = 0.0
        self._shape()
        self.show()
        self.raise_()
        self.anim.start()
        self.frame.start()
        if self.click_mode:
            # behave like a popup: a click anywhere outside dismisses it
            QTimer.singleShot(0, self.grabMouse)

    def close_menu(self):
        if self.isVisible():
            self.hide()
            self.frame.stop()
            self.dismissed.emit()

    def hideEvent(self, e):
        self.frame.stop()
        if QWidget.mouseGrabber() is self:
            self.releaseMouse()
        super().hideEvent(e)

    def _capture(self, top_left: QPoint, pos: QPoint, paths):
        """Capture before showing, without freezing Explorer's drag image into the lens."""
        self.backdrop, self._raw_backdrop = None, None
        try:
            if sys.platform == "darwin":
                raw = glass.grab_screen(top_left.x(), top_left.y(), SIZE, SIZE)
                if raw is not None and paths is None:
                    cx, cy = pos.x() - top_left.x(), pos.y() - top_left.y()
                    raw = glass.inpaint_rects(raw, [(cx - 70, cy - 64, cx + 70, cy + 64)])
            else:
                with glass.hidden_drag_images() as hidden:
                    raw = glass.grab_screen(top_left.x(), top_left.y(), SIZE, SIZE)
                if raw is not None:
                    rects = hidden.rects
                    if sys.platform.startswith("linux") and paths is None:
                        rects = [(pos.x() - 60, pos.y() - 60, pos.x() + 60, pos.y() + 60)]
                    if rects:
                        local = [(x0 - top_left.x(), y0 - top_left.y(),
                                  x1 - top_left.x(), y1 - top_left.y()) for x0, y0, x1, y1 in rects]
                        raw = glass.inpaint_rects(raw, local)
            if raw is not None:
                self._raw_backdrop = raw
                self.backdrop = glass.blur_pixmap(QPixmap.fromImage(glass.qimage_from_pil(raw)), 12)
        except Exception:
            self.backdrop, self._raw_backdrop = None, None

    def _render(self, n: int):
        if n <= 0 or n in self.liquid or self._raw_backdrop is None:
            return
        self.liquid[n] = QImage()
        job = _RenderJob(self._serial, n, self._raw_backdrop)
        from .util import keep, release
        job.sig.done.connect(lambda *_, j=job: release(j))
        job.sig.done.connect(self._rendered)
        QThreadPool.globalInstance().start(keep(job))

    @Slot(int, int, QImage)
    def _rendered(self, serial: int, n: int, image: QImage):
        if serial == self._serial:
            self.liquid[n] = image
            self.update()

    def _load(self, paths: list[str]):
        paths = resolve_shortcuts(paths)
        self.paths = paths
        self.rings = {k: v for k, v in self.build_rings(paths).items() if v}
        self.ring_names = list(self.rings)
        self.ring_idx = 0
        self.hub_text = self.describe(paths)
        for items in self.rings.values():
            self._render(len(items))
        self.spring = [0.0] * len(self.items)
        self._shape()
        self.update()

    def _shape(self):
        """No compositor (XFCE etc.): cut the window to the menu's outline."""
        if not glass.no_compositor():
            return
        c = QPointF(SIZE / 2, SIZE / 2)
        path = QPainterPath()
        path.addEllipse(c, R_HUB + 2, R_HUB + 2)
        n = len(self.items) or 0
        if n:
            for i in range(n):
                a0, a1 = angles(n, i)
                path = path.united(sector_path(c, R_IN - 2, R_OUT + 14, a0 - 1, a1 + 1))
        else:
            outer = QPainterPath()
            outer.addEllipse(c, R_OUT + 14, R_OUT + 14)
            path = path.united(outer)       # united, not added: odd-even fill would punch a hole
        glass.apply_shape(self, path)

    def next_ring(self):
        if len(self.ring_names) > 1:
            self.ring_idx = (self.ring_idx + 1) % len(self.ring_names)
            self.hover = -1
            self.spring = [0.0] * len(self.items)
            self._swap = 0.0
            self.swap_anim.start()
            self._shape()
            self.update()

    def _set_t(self, v):
        self._t = float(v)
        self.update()

    def _set_swap(self, v):
        self._swap = float(v)
        self.update()

    def _tick(self):
        moved = False
        for i in range(len(self.spring)):
            target = 1.0 if i == self.hover else 0.0
            v = self.spring[i]
            nv = v + (target - v) * 0.32
            if abs(nv - target) < 0.003:
                nv = target
            if nv != v:
                self.spring[i] = nv
                moved = True
        ht = 1.0 if self.hover == -2 else 0.0
        nh = self.hub_spring + (ht - self.hub_spring) * 0.3
        if abs(nh - ht) < 0.003:
            nh = ht
        if nh != self.hub_spring:
            self.hub_spring = nh
            moved = True
        if moved:
            self.update()

    # ------------------------------------------------------------ geometry
    @property
    def items(self) -> list[Item]:
        if not self.ring_names:
            return []
        return self.rings[self.ring_names[self.ring_idx]]

    def _angles(self, n: int, i: int) -> tuple[float, float]:
        return angles(n, i)

    def hit(self, pos: QPointF) -> int:
        c = QPointF(SIZE / 2, SIZE / 2)
        dx, dy = pos.x() - c.x(), c.y() - pos.y()
        d = math.hypot(dx, dy)
        if d <= R_HUB + 4:
            return -2
        if d > R_OUT + 30 or not self.items:
            return -1
        ang = math.degrees(math.atan2(dy, dx)) % 360
        n = len(self.items)
        rel = (90 + 180.0 / n - ang) % 360
        return int(rel // (360.0 / n)) % n

    def _set_hover(self, h: int, pos: QPointF | None = None):
        if pos is not None:
            self.cursor_pos = QPointF(pos)
        if h != self.hover:
            self.hover = h
        self.update()

    # ------------------------------------------------------------ drag & drop
    @staticmethod
    def _local_paths(md) -> list[str]:
        if not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        paths = self._local_paths(e.mimeData())
        if not paths:
            e.ignore()
            return
        self.got_drag = True
        if resolve_shortcuts(paths) != self.paths:
            self._load(paths)
        e.setDropAction(Qt.CopyAction)  # never let a Shift(=move) drop delete the source
        e.accept()

    def dragMoveEvent(self, e):
        self._set_hover(self.hit(e.position()), e.position())
        e.setDropAction(Qt.CopyAction)
        e.accept()

    def dragLeaveEvent(self, e):
        self._set_hover(-1)

    def dropEvent(self, e):
        idx = self.hit(e.position())
        items = self.items
        # The final OLE payload is authoritative. A cached dragEnter payload can
        # belong to an earlier drag; never dispatch it after the file is released.
        paths = resolve_shortcuts(self._local_paths(e.mimeData()))
        item = items[idx] if 0 <= idx < len(items) else None
        if item and paths:
            available = [candidate for ring in self.build_rings(paths).values() for candidate in ring]
            if not any(candidate.key == item.key for candidate in available):
                e.ignore()
                self.close_menu()
                return
            e.setDropAction(Qt.CopyAction)
            e.accept()
            paths = list(paths)
            self.hide()
            # return to Explorer's drag loop immediately; do the work after
            QTimer.singleShot(0, lambda: self.activated.emit(item, paths))
        else:
            e.ignore()
            self.close_menu()

    # ------------------------------------------------------------ click mode
    def mouseMoveEvent(self, e):
        if self.click_mode:
            self._set_hover(self.hit(e.position()), e.position())

    def mouseReleaseEvent(self, e):
        if not self.click_mode:
            return
        if e.button() == Qt.RightButton:
            self.close_menu()
            return
        idx = self.hit(e.position())
        if idx == -2:
            self.next_ring()
        elif 0 <= idx < len(self.items):
            item, paths = self.items[idx], list(self.paths)
            self.hide()
            QTimer.singleShot(0, lambda: self.activated.emit(item, paths))
        else:
            self.close_menu()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.close_menu()
        elif (not e.isAutoRepeat() and
              (e.key() == Qt.Key_Tab or e.key() == Qt.Key_Shift and self.click_mode)):
            # The global watcher owns Shift during a drag. Handling it here too
            # can switch twice if a platform also delivers a widget key event.
            self.next_ring()

    def leaveEvent(self, e):
        if self.click_mode:
            self._set_hover(-1)

    # ------------------------------------------------------------ painting
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        c = QPointF(SIZE / 2, SIZE / 2)
        t = self._t
        p.setOpacity(min(1.0, max(0.0, t * 1.5)))
        p.translate(c)
        s = 0.96 + 0.04 * t
        p.scale(s, s)
        p.translate(-c)

        items = self.items
        n = len(items)
        image = self.liquid.get(n)
        self._paint_surface(p, c, n, image if image is not None and not image.isNull() else None)

        fg, fg_dim = QColor(TEXT), QColor(MUTED)
        for i, it in enumerate(items):
            a0, a1 = angles(n, i)
            h = self.spring[i] if i < len(self.spring) else 0.0
            if h > 0.001:
                self._paint_hover(p, c, a0, a1, h)
            self._petal_label(p, c, it, (a0 + a1) / 2, (R_IN + R_OUT) / 2, fg)
        if not n and self.paths:
            self._paint_unsupported(p, c, fg, fg_dim)

        self._paint_hub(p, c, fg, fg_dim)

    def _paint_unsupported(self, p: QPainter, c: QPointF, fg: QColor, fg_dim: QColor):
        """Nothing to offer: say so in the top band, clear of the cursor and the drag image."""
        ext = Path(self.paths[0]).suffix.lower() or "this file"
        f = QFont(ui_family(True))
        f.setPixelSize(13)
        f.setBold(True)
        p.setFont(f)
        r_mid = (R_IN + R_OUT) / 2
        self._text(p, QRectF(c.x() - R_OUT, c.y() - r_mid - 18, 2 * R_OUT, 18),
                   f"Can't convert {ext}", fg)
        f.setPixelSize(10)
        f.setBold(False)
        p.setFont(f)
        self._text(p, QRectF(c.x() - R_OUT, c.y() - r_mid + 1, 2 * R_OUT, 16),
                   "Images · Video · Audio · Documents · Archives", fg_dim)

    def _paint_surface(self, p: QPainter, c: QPointF, n: int, image: QImage | None):
        hub = QPainterPath()
        hub.addEllipse(c, R_HUB, R_HUB)
        paths = [hub]
        for i in range(n):
            a0, a1 = angles(n, i)
            paths.append(sector_path(c, R_IN, R_OUT, a0, a1))
        if not n:
            outer, inner = QPainterPath(), QPainterPath()
            outer.addEllipse(c, R_OUT, R_OUT)
            inner.addEllipse(c, R_IN, R_IN)
            paths.append(outer.subtracted(inner))
        if image is not None:
            p.drawImage(0, 0, image)
        for path in paths:
            if image is None:
                p.save()
                p.setClipPath(path)
                if self.backdrop is not None:
                    p.drawPixmap(0, 0, self.backdrop)
                    tint = QColor(PANEL)
                    tint.setAlphaF(glass.neutral_tint()[3])
                    p.fillPath(path, tint)
                else:
                    p.fillPath(path, QColor(PANEL))
                p.restore()
            p.setPen(QPen(QColor(BORDER), 1))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)

    def _paint_hover(self, p: QPainter, c: QPointF, a0: float, a1: float, h: float):
        p.save()
        p.setOpacity(p.opacity() * h)
        p.setPen(QPen(QColor(BORDER), 1))
        color = QColor(SELECTED)
        if self._raw_backdrop is not None:
            color.setAlpha(100)
        p.setBrush(color)
        p.drawPath(sector_path(c, R_IN, R_OUT, a0, a1))
        p.restore()

    def _paint_hub(self, p: QPainter, c: QPointF, fg: QColor, fg_dim: QColor):
        p.setPen(QPen(QColor(BORDER), 1))
        if self.hover == -2:
            color = QColor(HOVER)
            color.setAlpha(80)
            p.setBrush(color)
        else:
            p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, R_HUB - 0.5, R_HUB - 0.5)

        items = self.items
        hov = items[self.hover] if 0 <= self.hover < len(items) else None
        if hov is not None:
            top, sub = hov.label if not hov.glyph else hov.label.title(), hov.hint or ""
        else:
            ring_name = self.ring_names[self.ring_idx] if self.ring_names else ""
            top = self.hub_text
            if len(self.ring_names) > 1:
                other = self.ring_names[(self.ring_idx + 1) % len(self.ring_names)]
                sub = f"Shift · {other}"
            else:
                sub = ring_name
        f = QFont(ui_family(True))
        f.setPixelSize(14)
        f.setBold(True)
        p.setFont(f)
        rect_top = QRectF(c.x() - R_HUB + 4, c.y() - 21, 2 * R_HUB - 8, 20)
        self._text(p, rect_top, top, fg)
        f.setPixelSize(10)
        f.setBold(False)
        p.setFont(f)
        self._text(p, QRectF(c.x() - R_HUB + 6, c.y() + 2, 2 * R_HUB - 12, 28), sub, fg_dim,
                   Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap)

    @staticmethod
    def _text(p: QPainter, r: QRectF, text: str, col: QColor,
              flags=Qt.AlignCenter):
        p.setPen(col)
        p.drawText(r, flags, tr(text))

    def _petal_label(self, p: QPainter, c: QPointF, it: Item, ang: float, r: float, fg: QColor):
        rad = math.radians(ang)
        pt = QPointF(c.x() + r * math.cos(rad), c.y() - r * math.sin(rad))
        p.save()
        p.setOpacity(p.opacity() * (0.25 + 0.75 * self._swap))
        f = QFont(ui_family(True))
        if it.glyph:
            f.setPixelSize(18)
            p.setFont(f)
            self._text(p, QRectF(pt.x() - 30, pt.y() - 24, 60, 24), it.glyph + "\ufe0e", fg,
                       Qt.AlignHCenter | Qt.AlignBottom)
            f.setPixelSize(11)
            f.setBold(True)
            p.setFont(f)
            self._text(p, QRectF(pt.x() - 42, pt.y() + 1, 84, 16), it.label, fg)
        else:
            f.setPixelSize(14)
            f.setBold(True)
            f.setLetterSpacing(QFont.AbsoluteSpacing, 0.3)
            p.setFont(f)
            self._text(p, QRectF(pt.x() - 42, pt.y() - 12, 84, 24), it.label, fg)
        p.restore()
