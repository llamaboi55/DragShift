"""Tool windows: Compress, Resize, Crop, Edit, Annotate, Redact, Add BG, Metadata, Protect.

Every window opens on a fast, reduced-size preview. Saving runs on the full-resolution
image in the background (the window closes immediately; a notification reports the result)."""
from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Callable

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QGuiApplication, QImage, QKeySequence,
                           QPainter, QPainterPath, QPen, QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QColorDialog, QComboBox, QFrame,
                               QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget)

from . import imaging
from .util import ui_family
from .convert import open_image, open_preview
from .glass import pil_from_qimage, qimage_from_pil
from .util import file_size, human_size, sibling
from .window import GlassWindow, ImageView

Notify = Callable[..., None]


# ---------------------------------------------------------------- small widgets
def section(text: str) -> QLabel:
    l = QLabel(text.upper())
    l.setObjectName("section")
    return l


def slider(lo: int, hi: int, val: int) -> QSlider:
    s = QSlider(Qt.Horizontal)
    s.setRange(lo, hi)
    s.setValue(val)
    return s


def toggle_row(labels: list[str], checked: int = 0) -> tuple[QHBoxLayout, QButtonGroup]:
    row = QHBoxLayout()
    row.setSpacing(6)
    grp = QButtonGroup(row)
    grp.setExclusive(True)
    for i, t in enumerate(labels):
        b = QPushButton(t)
        b.setCheckable(True)
        b.setChecked(i == checked)
        grp.addButton(b, i)
        row.addWidget(b)
    row.addStretch(1)
    return row, grp


def slider_row(grid: QGridLayout, r: int, label: str, s: QSlider, fmt: Callable[[int], str]) -> QLabel:
    grid.addWidget(QLabel(label), r, 0)
    grid.addWidget(s, r, 1)
    val = QLabel(fmt(s.value()))
    val.setObjectName("dim")
    val.setMinimumWidth(56)
    val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
    s.valueChanged.connect(lambda v: val.setText(fmt(v)))
    grid.addWidget(val, r, 2)
    return val


def dims_text(w: int, h: int) -> str:
    return f"{w} × {h}"


class ToolWindow(GlassWindow):
    tool_name = ""

    def __init__(self, title: str, paths, notify: Notify, size=(820, 680)):
        super().__init__(title, size=size)
        self.paths = [Path(p) for p in (paths if isinstance(paths, (list, tuple)) else [paths])]
        self.path = self.paths[0]
        self.notify = notify
        self.submit = None   # set by the app: run work in the background with a spinner
        if len(self.paths) == 1:
            sub = f"{self.path.name}  ·  {human_size(file_size(self.path))}"
        else:
            total = sum(file_size(p) for p in self.paths)
            sub = f"{len(self.paths)} files  ·  {human_size(total)}"
        self.subtitle = QLabel(sub)
        self.subtitle.setObjectName("dim")
        self.body.addWidget(self.subtitle)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._enter)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self._enter)
        self.primary_btn: QPushButton | None = None

    def _enter(self):
        from PySide6.QtWidgets import QApplication
        fw = QApplication.focusWidget()
        if isinstance(fw, QLineEdit) and isinstance(fw.parent(), ImageView):
            fw.returnPressed.emit()   # Enter in an on-canvas text box finishes the text
            return
        if self.primary_btn is not None and self.primary_btn.isEnabled():
            self.primary_btn.click()

    def footer(self, primary_text: str, on_primary, on_reset=None, extra_buttons=()) -> QHBoxLayout:
        row = QHBoxLayout()
        if on_reset:
            reset = QPushButton("Reset")
            reset.clicked.connect(on_reset)
            row.addWidget(reset)
        for b in extra_buttons:
            row.addWidget(b)
        row.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        row.addWidget(cancel)
        ok = QPushButton(primary_text)
        ok.setObjectName("primary")
        ok.clicked.connect(on_primary)
        row.addWidget(ok)
        self.primary_btn = ok
        return row

    def run(self, fn: Callable[[], list], working: str, extra: str = ""):
        """Save in the background (window closes now) or, without the app, synchronously."""
        action = f"tool:{self.tool_name}"
        if self.submit is not None:
            self.submit(action, [str(p) for p in self.paths], fn, working, extra)
            self.close()
        else:
            outs = fn()
            self.notify(action, [str(p) for p in self.paths], outs, extra)
            self.close()


# ================================================================ CROP
class CropView(ImageView):
    HANDLE = 9

    def __init__(self, image: QImage, k: float):
        super().__init__(image)
        self.k = k
        self.ratio: float | None = None
        self.sel = QRectF(0, 0, image.width(), image.height())
        self._mode: str | None = None
        self._start = QPointF()
        self._orig = QRectF()

    def set_ratio(self, ratio: float | None):
        self.ratio = ratio
        if ratio:
            W, H = self.image.width(), self.image.height()
            w, h = W, W / ratio
            if h > H:
                h, w = H, H * ratio
            self.sel = QRectF((W - w) / 2, (H - h) / 2, w, h)
        self.update()
        self.changed.emit()

    def reset_sel(self):
        self.sel = QRectF(0, 0, self.image.width(), self.image.height())
        if self.ratio:
            self.set_ratio(self.ratio)
        self.update()
        self.changed.emit()

    def _hit(self, pos: QPointF) -> str | None:
        r = self.rect_to_widget(self.sel)
        h = self.HANDLE + 4
        near = lambda a, b: abs(a - b) <= h
        l, t, rt, b = near(pos.x(), r.left()), near(pos.y(), r.top()), near(pos.x(), r.right()), near(pos.y(), r.bottom())
        inside_x = r.left() - h <= pos.x() <= r.right() + h
        inside_y = r.top() - h <= pos.y() <= r.bottom() + h
        if t and l: return "tl"
        if t and rt: return "tr"
        if b and l: return "bl"
        if b and rt: return "br"
        if t and inside_x: return "t"
        if b and inside_x: return "b"
        if l and inside_y: return "l"
        if rt and inside_y: return "r"
        if r.contains(pos): return "move"
        return None

    CURSORS = {"tl": Qt.SizeFDiagCursor, "br": Qt.SizeFDiagCursor, "tr": Qt.SizeBDiagCursor,
               "bl": Qt.SizeBDiagCursor, "t": Qt.SizeVerCursor, "b": Qt.SizeVerCursor,
               "l": Qt.SizeHorCursor, "r": Qt.SizeHorCursor, "move": Qt.SizeAllCursor}

    def mousePressEvent(self, e):
        self._mode = self._hit(e.position())
        if self._mode is None and self.target_rect().contains(e.position()):
            self._mode = "new"
        self._start = self.clamp_image(self.to_image(e.position()))
        self._orig = QRectF(self.sel)

    def mouseDoubleClickEvent(self, e):
        self.reset_sel()

    def mouseMoveEvent(self, e):
        if not self._mode:
            m = self._hit(e.position())
            self.setCursor(self.CURSORS.get(m, Qt.CrossCursor))
            return
        p = self.clamp_image(self.to_image(e.position()))
        W, H = self.image.width(), self.image.height()
        r = QRectF(self._orig)
        if self._mode == "move":
            d = p - self._start
            r.translate(d)
            r.moveLeft(min(max(r.left(), 0), W - r.width()))
            r.moveTop(min(max(r.top(), 0), H - r.height()))
        elif self._mode == "new":
            r = QRectF(self._start, p).normalized()
            if self.ratio and r.height() > 0:
                r.setHeight(r.width() / self.ratio)
        else:
            if "l" in self._mode: r.setLeft(p.x())
            if "r" in self._mode: r.setRight(p.x())
            if "t" in self._mode: r.setTop(p.y())
            if "b" in self._mode: r.setBottom(p.y())
            r = r.normalized()
            if self.ratio:
                if self._mode in ("t", "b"):
                    r.setWidth(r.height() * self.ratio)
                else:
                    nh = r.width() / self.ratio
                    if "t" in self._mode:
                        r.setTop(r.bottom() - nh)
                    else:
                        r.setHeight(nh)
        r = r.intersected(QRectF(0, 0, W, H))
        if r.width() >= 4 and r.height() >= 4:
            self.sel = r
        self.update()
        self.changed.emit()

    def mouseReleaseEvent(self, e):
        self._mode = None

    def paint_overlay(self, p: QPainter, t: QRectF):
        r = self.rect_to_widget(self.sel)
        shade = QPainterPath()
        shade.addRect(t)
        hole = QPainterPath()
        hole.addRect(r)
        p.fillPath(shade.subtracted(hole), QColor(0, 0, 0, 140))
        p.setPen(QPen(QColor(255, 255, 255, 230), 1.5))
        p.drawRect(r)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        for i in (1, 2):  # rule of thirds
            x = r.left() + r.width() * i / 3
            y = r.top() + r.height() * i / 3
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#ff6a4d"))
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            p.drawEllipse(pt, 5.5, 5.5)
        # live output size badge
        txt = dims_text(round(self.sel.width() * self.k), round(self.sel.height() * self.k))
        f = QFont(ui_family(False))
        f.setPixelSize(12)
        f.setBold(True)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(txt) + 18
        badge = QRectF(r.center().x() - w / 2, r.bottom() - 30, w, 22)
        if badge.top() < r.top() + 4:
            badge.moveTop(r.bottom() + 6)
        p.setBrush(QColor(20, 16, 32, 190))
        p.drawRoundedRect(badge, 11, 11)
        p.setPen(QColor(255, 255, 255))
        p.drawText(badge, Qt.AlignCenter, txt)


class CropWindow(ToolWindow):
    tool_name = "Crop"
    RATIOS = [("Free", None), ("Original", "orig"), ("1:1", 1.0), ("4:3", 4 / 3), ("3:2", 1.5),
              ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:5", 0.8)]

    def __init__(self, path, notify):
        super().__init__("Crop Image", path, notify)
        pv = open_preview(path, 1400)
        with Image.open(path) as im:
            fw, fh = im.size
        from PIL import ImageOps
        with Image.open(path) as im:
            o = im.getexif().get(0x0112, 1)
        if o in (5, 6, 7, 8):
            fw, fh = fh, fw
        self.full_size = (fw, fh)
        self.rot = 0
        self.base = pv
        self.view = CropView(qimage_from_pil(pv), fw / pv.width)
        self.body.addWidget(self.view, 1)

        self.body.addWidget(section("Aspect ratio"))
        row, self.grp = toggle_row([n for n, _ in self.RATIOS])
        self.grp.idClicked.connect(self._ratio)
        for txt, d in (("⟲", -90), ("⟳", 90)):
            b = QPushButton(txt)
            b.setToolTip("Rotate 90°")
            b.clicked.connect(lambda _=False, d=d: self._rotate(d))
            row.addWidget(b)
        self.body.addLayout(row)

        dims = QHBoxLayout()
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 100000)
            s.setFixedWidth(96)
        dims.addWidget(QLabel("W"))
        dims.addWidget(self.w)
        dims.addWidget(QLabel("H"))
        dims.addWidget(self.h)
        dims.addWidget(QLabel("px"))
        dims.addStretch(1)
        tip = QLabel("Double-click the image to reset")
        tip.setObjectName("dim")
        dims.addWidget(tip)
        self.body.addLayout(dims)
        self.w.editingFinished.connect(self._from_spin)
        self.h.editingFinished.connect(self._from_spin)
        self.view.changed.connect(self._to_spin)
        self._to_spin()
        self.body.addLayout(self.footer("Apply", self.apply, self.reset))

    def _ratio(self, i):
        r = self.RATIOS[i][1]
        if r == "orig":
            r = self.view.image.width() / self.view.image.height()
        self.view.set_ratio(r)

    def _rotate(self, d):
        self.rot = (self.rot + d) % 360
        img = self.base.rotate(-self.rot, expand=True)
        k = self.view.k
        self.view.set_image(qimage_from_pil(img))
        self.view.k = k
        self.view.reset_sel()

    def _to_spin(self):
        s = self.view.sel
        k = self.view.k
        for box, v in ((self.w, s.width()), (self.h, s.height())):
            box.blockSignals(True)
            box.setValue(round(v * k))
            box.blockSignals(False)

    def _from_spin(self):
        k = self.view.k
        s = QRectF(self.view.sel)
        s.setWidth(self.w.value() / k)
        s.setHeight(self.h.value() / k)
        self.view.sel = s.intersected(QRectF(0, 0, self.view.image.width(), self.view.image.height()))
        self.view.update()
        self._to_spin()

    def reset(self):
        self.grp.button(0).setChecked(True)
        self.view.ratio = None
        if self.rot:
            self.rot = 0
            self.view.set_image(qimage_from_pil(self.base))
        self.view.reset_sel()

    def apply(self):
        s, k, rot, path = self.view.sel, self.view.k, self.rot, self.path
        fw, fh = (self.full_size[1], self.full_size[0]) if rot % 180 else self.full_size
        x0, y0 = round(s.left() * k), round(s.top() * k)
        w, h = round(s.width() * k), round(s.height() * k)
        # preview → full-size rounding: snap to the image edges when within 3 px
        if x0 <= 3:
            w, x0 = w + x0, 0
        if y0 <= 3:
            h, y0 = h + y0, 0
        snap_w = abs(x0 + w - fw) <= 3
        snap_h = abs(y0 + h - fh) <= 3
        if snap_w:
            w = fw - x0
        if snap_h:
            h = fh - y0
        if self.view.ratio:  # exact ratio (no 1600×1599)
            r = self.view.ratio
            if snap_h and not snap_w:
                w = round(h * r)
            else:
                h = round(w / r)
            if y0 + h > fh:
                h = fh - y0
                w = round(h * r)
            if x0 + w > fw:
                w = fw - x0
                h = round(w / r)
        box = (x0, y0, x0 + w, y0 + h)

        def work():
            im = open_image(path)
            if rot:
                im = im.rotate(-rot, expand=True)
            return [imaging.save_like(im.crop(box), path, "Cropped")]
        extra = f"rotated {rot}°" if rot else ""
        self.run(work, "Cropping…", extra)


# ================================================================ ADD BG
PRESETS = [
    ("Sunset", ["#ffb36b", "#ff5f6d", "#a24bff"]),
    ("Prism", ["#ff6b9d", "#ffb36b", "#ffe66b", "#7dffb0", "#6bd5ff", "#8a7bff"]),
    ("Peach", ["#ffd6a5", "#ff9a8b"]),
    ("Aurora", ["#00d2ff", "#7a5cff", "#ff5fd2"]),
    ("Mint", ["#a8ffce", "#3ad6c6"]),
    ("Ocean", ["#43cbff", "#2058d6"]),
    ("Dusk", ["#2b1055", "#d53a9d"]),
    ("Lemon", ["#fff59d", "#ffb74d"]),
    ("Night", ["#0f2027", "#2c5364"]),
    ("Rose", ["#ffdde1", "#ee9ca7"]),
]
SOLIDS = ["#ffffff", "#f2f2f7", "#d1d1d6", "#8e8e93", "#1c1c1e", "#000000", "#ff3b30",
          "#ff9500", "#ffcc00", "#34c759", "#00c7be", "#007aff", "#5856d6", "#af52de"]


class AddBgWindow(ToolWindow):
    tool_name = "Add BG"
    ASPECTS = ["Auto", "1:1", "4:3", "16:9", "9:16", "4:5"]

    def __init__(self, path, notify):
        super().__init__("Add BG", path, notify, size=(880, 780))
        self.small = open_preview(path, 760)
        with Image.open(path) as im:
            self.full_w = max(im.size) if im.getexif().get(0x0112, 1) in (5, 6, 7, 8) else im.width
            self.full_size = im.size
        self.colors = PRESETS[0][1]

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 300)
        self.body.addWidget(self.preview, 1)
        self.out_label = QLabel()
        self.out_label.setObjectName("dim")
        self.out_label.setAlignment(Qt.AlignCenter)
        self.body.addWidget(self.out_label)

        self.body.addWidget(section("Background"))
        sw = QGridLayout()
        sw.setSpacing(6)
        self.sw_grp = QButtonGroup(self)
        items = [("g", c) for _, c in PRESETS] + [("s", [c]) for c in SOLIDS]
        for i, (_, cols) in enumerate(items):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setStyleSheet(self._swatch_css(cols))
            b.setChecked(i == 0)
            b.setToolTip(PRESETS[i][0] if i < len(PRESETS) else cols[0])
            b.clicked.connect(lambda _=False, c=cols: self._pick(c))
            self.sw_grp.addButton(b, i)
            sw.addWidget(b, i // 12, i % 12)
        custom = QPushButton("＋")
        custom.setObjectName("swatch")
        custom.setToolTip("Custom colour")
        custom.clicked.connect(self._custom)
        sw.addWidget(custom, len(items) // 12, len(items) % 12)
        self.body.addLayout(sw)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.pad = slider(0, 40, 12)
        self.rad = slider(0, 120, 24)
        self.sha = slider(0, 100, 50)
        self.ang = slider(0, 360, 135)
        slider_row(grid, 0, "Padding", self.pad, lambda v: f"{v}%")
        slider_row(grid, 1, "Corners", self.rad, lambda v: f"{v} px")
        slider_row(grid, 2, "Shadow", self.sha, lambda v: f"{v}%")
        slider_row(grid, 3, "Angle", self.ang, lambda v: f"{v}°")
        self.body.addLayout(grid)

        self.body.addWidget(section("Size"))
        row, self.asp = toggle_row(self.ASPECTS)
        self.body.addLayout(row)

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setToolTip("Copy the result to the clipboard")
        self.copy_btn.clicked.connect(self.copy)
        self.body.addLayout(self.footer("Save with Background", self.save, self.reset, [self.copy_btn]))

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        for s in (self.pad, self.rad, self.sha, self.ang):
            s.valueChanged.connect(lambda _: self._timer.start())
        self.asp.idClicked.connect(lambda _: self._timer.start())
        self.render()

    @staticmethod
    def _swatch_css(cols: list[str]) -> str:
        if len(cols) == 1:
            return f"background: {cols[0]};"
        stops = ", ".join(f"stop:{i / (len(cols) - 1):.2f} {c}" for i, c in enumerate(cols))
        return f"background: qlineargradient(x1:0,y1:0,x2:1,y2:1, {stops});"

    def _pick(self, cols):
        self.colors = cols
        self.render()

    def _custom(self):
        c = QColorDialog.getColor(QColor(self.colors[0]), self, "Background colour")
        if c.isValid():
            self.colors = [c.name()]
            self.render()

    def _params(self, scale: float) -> imaging.Background:
        return imaging.Background(colors=self.colors, angle=self.ang.value(),
                                  padding=self.pad.value() / 100,
                                  radius=int(self.rad.value() * scale),
                                  shadow=self.sha.value() / 100,
                                  aspect=self.ASPECTS[self.asp.checkedId()])

    def _full_scale(self) -> float:
        return max(1.0, max(self.full_size) / 1000)

    def render(self):
        k = max(self.small.size) / max(self.full_size)
        out = imaging.add_background(self.small, self._params(k * self._full_scale()))
        pm = QPixmap.fromImage(qimage_from_pil(out))
        self.preview.setPixmap(pm.scaled(self.preview.size() - QSize(10, 10), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))
        fw = round(out.width / k)
        fh = round(out.height / k)
        self.out_label.setText(f"Output  {dims_text(fw, fh)} px  ·  PNG")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_timer"):
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.render)

    def reset(self):
        self.pad.setValue(12); self.rad.setValue(24); self.sha.setValue(50); self.ang.setValue(135)
        self.asp.button(0).setChecked(True)
        self.sw_grp.button(0).setChecked(True)
        self._pick(PRESETS[0][1])

    def _compose_full(self) -> Image.Image:
        return imaging.add_background(open_image(self.path), self._params(self._full_scale()))

    def copy(self):
        QGuiApplication.clipboard().setImage(qimage_from_pil(self._compose_full()))
        self.copy_btn.setText("Copied ✓")
        QTimer.singleShot(1400, lambda: self.copy_btn.setText("Copy"))

    def save(self):
        params, path = self._params(self._full_scale()), self.path

        def work():
            out_im = imaging.add_background(open_image(path), params)
            out = sibling(path, "With Background", ".png")
            from .convert import save_image, source_meta
            save_image(out_im, out, "PNG", icc=source_meta(path)[0], keep_meta=False)
            return [out]
        self.run(work, "Adding background…")


# ================================================================ ANNOTATE
class Shape:
    def __init__(self, kind, color, width, pts=None, text=""):
        self.kind, self.color, self.width = kind, QColor(color), width
        self.pts: list[QPointF] = pts or []
        self.text = text


def draw_shape(p: QPainter, s: Shape, map_pt, scale: float):
    pts = [map_pt(q) for q in s.pts]
    w = max(1.0, s.width * scale)
    col = QColor(s.color)
    if s.kind == "Highlight":
        col.setAlpha(110)
        w *= 4
    pen = QPen(col, w, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if s.kind in ("Pen", "Highlight") and len(pts) > 1:
        path = QPainterPath(pts[0])
        for q in pts[1:]:
            path.lineTo(q)
        p.drawPath(path)
    elif s.kind == "Arrow" and len(pts) == 2:
        a, b = pts
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        head = max(12.0, w * 4.2)
        base = QPointF(b.x() - head * 0.8 * math.cos(ang), b.y() - head * 0.8 * math.sin(ang))
        p.drawLine(a, base)
        left = QPointF(b.x() - head * math.cos(ang - 0.42), b.y() - head * math.sin(ang - 0.42))
        right = QPointF(b.x() - head * math.cos(ang + 0.42), b.y() - head * math.sin(ang + 0.42))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawPolygon(QPolygonF([b, left, right]))
    elif s.kind == "Rect" and len(pts) == 2:
        p.drawRoundedRect(QRectF(pts[0], pts[1]).normalized(), w, w)
    elif s.kind == "Ellipse" and len(pts) == 2:
        p.drawEllipse(QRectF(pts[0], pts[1]).normalized())
    elif s.kind == "Text" and pts:
        f = QFont(ui_family(False))
        f.setPixelSize(max(8, int(s.width * 5 * scale)))
        f.setBold(True)
        p.setFont(f)
        # outline for legibility on any background
        outline = QPainterPath()
        outline.addText(pts[0], f, s.text)
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QPen(QColor(0, 0, 0, 160) if lum > 128 else QColor(255, 255, 255, 190), max(2.0, w * 0.6)))
        p.drawPath(outline)
        p.fillPath(outline, col)
    elif s.kind == "Step" and pts:
        r = max(10.0, s.width * 3.2 * scale)
        c = pts[0]
        p.setPen(QPen(QColor(255, 255, 255), max(1.5, r * 0.12)))
        p.setBrush(col)
        p.drawEllipse(c, r, r)
        f = QFont(ui_family(False))
        f.setPixelSize(int(r * 1.15))
        f.setBold(True)
        p.setFont(f)
        lum = col.red() * 0.3 + col.green() * 0.59 + col.blue() * 0.11
        p.setPen(QColor(20, 20, 20) if lum > 170 else QColor(255, 255, 255))
        p.drawText(QRectF(c.x() - r, c.y() - r, 2 * r, 2 * r), Qt.AlignCenter, s.text)


class AnnotateView(ImageView):
    def __init__(self, image: QImage, owner: "AnnotateWindow"):
        super().__init__(image)
        self.owner = owner
        self.shapes: list[Shape] = []
        self.redo_stack: list[Shape] = []
        self.cur: Shape | None = None
        self.editor: QLineEdit | None = None
        self.setCursor(Qt.CrossCursor)

    def next_step(self) -> int:
        return sum(1 for s in self.shapes if s.kind == "Step") + 1

    def _commit_text(self):
        ed = self.editor
        if ed is None:
            return
        self.editor = None
        text = ed.text().strip()
        if text:
            self.add(Shape("Text", ed.property("color"), ed.property("w"), [ed.property("pt")], text))
        ed.deleteLater()

    def add(self, s: Shape):
        self.shapes.append(s)
        self.redo_stack.clear()
        self.update()

    def mousePressEvent(self, e):
        self._commit_text()
        if e.button() != Qt.LeftButton or not self.target_rect().contains(e.position()):
            return
        o = self.owner
        p = self.to_image(e.position())
        width = o.stroke.value() / self.scale()  # keep on-screen thickness consistent
        if o.tool == "Text":
            ed = QLineEdit(self)
            ed.setPlaceholderText("Type, then Enter")
            ed.setProperty("pt", p)
            ed.setProperty("color", o.color)
            ed.setProperty("w", width)
            ed.move(int(e.position().x()), int(e.position().y()) - 16)
            ed.resize(220, 30)
            ed.show()
            ed.setFocus()
            ed.returnPressed.connect(self._commit_text)
            self.editor = ed
            return
        if o.tool == "Step":
            self.add(Shape("Step", o.color, width, [p], str(self.next_step())))
            return
        self.cur = Shape(o.tool, o.color, width, [p, p] if o.tool not in ("Pen", "Highlight") else [p])

    def mouseMoveEvent(self, e):
        if not self.cur:
            return
        p = self.clamp_image(self.to_image(e.position()))
        if self.cur.kind in ("Pen", "Highlight"):
            self.cur.pts.append(p)
        else:
            self.cur.pts[1] = p
        self.update()

    def mouseReleaseEvent(self, e):
        if self.cur:
            self.add(self.cur)
            self.cur = None

    def paint_overlay(self, p, t):
        s = self.scale()
        for sh in self.shapes + ([self.cur] if self.cur else []):
            draw_shape(p, sh, self.to_widget, s)


class AnnotateWindow(ToolWindow):
    tool_name = "Annotate"
    TOOLS = ["Arrow", "Rect", "Ellipse", "Pen", "Highlight", "Text", "Step"]
    COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#007aff", "#af52de", "#ffffff", "#000000"]

    def __init__(self, path, notify):
        super().__init__("Annotate", path, notify, size=(900, 720))
        pv = open_preview(path, 1600)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / pv.width
        self.tool, self.color = "Arrow", self.COLORS[0]
        self.view = AnnotateView(qimage_from_pil(pv), self)
        self.body.addWidget(self.view, 1)

        row, grp = toggle_row(["➜ Arrow", "▢ Box", "◯ Circle", "✎ Pen", "▬ Marker", "T Text", "① Step"])
        grp.idClicked.connect(lambda i: setattr(self, "tool", self.TOOLS[i]))
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        redo = QPushButton("Redo")
        redo.clicked.connect(self.redo)
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        row.addWidget(undo)
        row.addWidget(redo)
        self.body.addLayout(row)

        crow = QHBoxLayout()
        cgrp = QButtonGroup(self)
        for i, c in enumerate(self.COLORS):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setChecked(i == 0)
            b.setStyleSheet(f"background: {c};")
            b.clicked.connect(lambda _=False, c=c: setattr(self, "color", c))
            cgrp.addButton(b)
            crow.addWidget(b)
        crow.addSpacing(16)
        crow.addWidget(QLabel("Size"))
        self.stroke = slider(1, 20, 4)
        self.stroke.setFixedWidth(160)
        crow.addWidget(self.stroke)
        crow.addStretch(1)
        self.body.addLayout(crow)
        self.body.addLayout(self.footer("Save", self.save))

    def undo(self):
        if self.view.shapes:
            self.view.redo_stack.append(self.view.shapes.pop())
            self.view.update()

    def redo(self):
        if self.view.redo_stack:
            self.view.shapes.append(self.view.redo_stack.pop())
            self.view.update()

    def save(self):
        self.view._commit_text()
        shapes = list(self.view.shapes)
        if not shapes:
            self.close()
            return
        k, path = self.k, self.path
        counts: dict[str, int] = {}
        for s in shapes:
            counts[s.kind] = counts.get(s.kind, 0) + 1
        names = {"Arrow": "arrow", "Rect": "box", "Ellipse": "circle", "Pen": "stroke", "Highlight": "highlight",
                 "Text": "label", "Step": "step"}
        extra = ", ".join(f"{n} {names[t]}{'s' if n > 1 else ''}" for t, n in counts.items())

        def work():
            img = qimage_from_pil(open_image(path).convert("RGBA"))
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            p.setRenderHint(QPainter.TextAntialiasing)
            for sh in shapes:
                draw_shape(p, sh, lambda q: QPointF(q.x() * k, q.y() * k), k)
            p.end()
            return [imaging.save_like(pil_from_qimage(img), path, "Annotated")]
        self.run(work, "Saving annotations…", extra)


# ================================================================ REDACT
class RedactView(ImageView):
    def __init__(self, image: QImage, owner: "RedactWindow"):
        super().__init__(image)
        self.owner = owner
        self.rects: list[QRectF] = []
        self.cur: QRectF | None = None
        self._a = QPointF()
        self.setCursor(Qt.CrossCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self.target_rect().contains(e.position()):
            self._a = self.clamp_image(self.to_image(e.position()))
            self.cur = QRectF(self._a, self._a)

    def mouseMoveEvent(self, e):
        if self.cur is not None:
            self.cur = QRectF(self._a, self.clamp_image(self.to_image(e.position()))).normalized()
            self.update()

    def mouseReleaseEvent(self, e):
        if self.cur is not None and self.cur.width() > 2 and self.cur.height() > 2:
            self.rects.append(self.cur)
            self.owner.refresh()
        self.cur = None
        self.update()

    def paint_overlay(self, p, t):
        if self.cur is not None:
            p.setPen(QPen(QColor("#ff5f6d"), 1.5, Qt.DashLine))
            p.setBrush(QColor(0, 0, 0, 90))
            p.drawRect(self.rect_to_widget(self.cur))


class RedactWindow(ToolWindow):
    tool_name = "Redact"
    MODES = ["Black box", "Pixelate", "Blur"]

    def __init__(self, path, notify):
        super().__init__("Redact", path, notify)
        self.base = open_preview(path, 1400)
        with Image.open(path) as im:
            full = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.k = full[0] / self.base.width
        self.view = RedactView(qimage_from_pil(self.base), self)
        self.body.addWidget(self.view, 1)
        self.hint = QLabel("Drag over anything you want hidden. It's burned into the pixels — it can't be undone in the copy.")
        self.hint.setObjectName("dim")
        self.body.addWidget(self.hint)
        row, self.mode = toggle_row(self.MODES)
        self.mode.idClicked.connect(lambda _: self.refresh())
        undo = QPushButton("Undo")
        undo.clicked.connect(self.undo)
        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        row.addWidget(undo)
        self.body.addLayout(row)
        self.body.addLayout(self.footer("Save", self.save))

    def _mode(self):
        return ["black", "pixelate", "blur"][self.mode.checkedId()]

    def _boxes(self, k: float):
        return [tuple(round(v * k) for v in (r.left(), r.top(), r.right(), r.bottom())) for r in self.view.rects]

    def refresh(self):
        im = imaging.redact(self.base, self._boxes(1.0), self._mode())
        self.view.set_image(qimage_from_pil(im))
        n = len(self.view.rects)
        self.subtitle.setText(f"{self.path.name}  ·  {n} area{'s' if n != 1 else ''} hidden" if n else self.path.name)

    def undo(self):
        if self.view.rects:
            self.view.rects.pop()
            self.refresh()

    def save(self):
        boxes, mode, path = self._boxes(self.k), self._mode(), self.path
        if not boxes:
            self.close()
            return
        label = {"black": "black boxes", "pixelate": "pixelated", "blur": "blurred"}[mode]

        def work():
            return [imaging.save_like(imaging.redact(open_image(path), boxes, mode), path, "Redacted", keep_exif=False)]
        self.run(work, "Redacting…", f"{len(boxes)} area{'s' if len(boxes) != 1 else ''} {label}")


# ================================================================ EDIT
class EditWindow(ToolWindow):
    tool_name = "Edit"

    def __init__(self, path, notify):
        super().__init__("Edit", path, notify, size=(880, 780))
        self.small = open_preview(path, 900)
        with Image.open(path) as im:
            self.full_size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
        self.adj = imaging.Adjust()
        self.comparing = False

        self.preview = QLabel()
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumSize(360, 300)
        self.body.addWidget(self.preview, 1)

        grid = QGridLayout()
        grid.setHorizontalSpacing(12)
        self.sl = {
            "brightness": slider(-100, 100, 0), "contrast": slider(-100, 100, 0),
            "saturation": slider(-100, 100, 0), "warmth": slider(-100, 100, 0),
            "sharpness": slider(-100, 100, 0),
        }
        for i, (k, s) in enumerate(self.sl.items()):
            slider_row(grid, i, k.capitalize(), s, lambda v: f"{v:+d}")
            s.valueChanged.connect(lambda _: self._timer.start())
        self.size_sl = slider(5, 100, 100)
        slider_row(grid, len(self.sl), "Size", self.size_sl, lambda v: f"{v}%")
        self.size_sl.valueChanged.connect(self._size_label)
        self.dim_label = QLabel()
        self.dim_label.setObjectName("dim")
        grid.addWidget(self.dim_label, len(self.sl) + 1, 1)
        self.body.addLayout(grid)

        row = QHBoxLayout()
        auto = QPushButton("✦ Auto")
        auto.setToolTip("Automatic light and colour")
        auto.clicked.connect(self.auto)
        row.addWidget(auto)
        for text, fn in (("⟲", lambda: self._rot(-90)), ("⟳", lambda: self._rot(90)),
                         ("⇋ Flip", lambda: self._flip("h")), ("⇵ Flip", lambda: self._flip("v"))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        self.gray = QPushButton("B&&W")
        self.gray.setCheckable(True)
        self.gray.toggled.connect(lambda _: self._timer.start())
        row.addWidget(self.gray)
        row.addStretch(1)
        cmp_btn = QPushButton("Hold to compare")
        cmp_btn.pressed.connect(lambda: self._compare(True))
        cmp_btn.released.connect(lambda: self._compare(False))
        row.addWidget(cmp_btn)
        self.body.addLayout(row)
        self.body.addLayout(self.footer("Save", self.save, self.reset))

        self._timer = QTimer(self, singleShot=True, interval=16, timeout=self.render)
        self._size_label()
        self.render()

    def _compare(self, on: bool):
        self.comparing = on
        self.render()

    def auto(self):
        a = imaging.auto_enhance(self.small)
        vals = {"brightness": a.brightness - 1, "contrast": a.contrast - 1, "saturation": a.saturation - 1,
                "sharpness": (a.sharpness - 1) / 2}
        for k, v in vals.items():
            self.sl[k].setValue(int(round(v * 100)))

    def _size_label(self):
        p = self.size_sl.value() / 100
        w, h = self.full_size
        if self.adj.rotate % 180:
            w, h = h, w
        self.dim_label.setText(f"{round(w * p)} × {round(h * p)} px")

    def _rot(self, d):
        self.adj.rotate = (self.adj.rotate + d) % 360
        self._size_label()
        self.render()

    def _flip(self, axis):
        if axis == "h":
            self.adj.flip_h = not self.adj.flip_h
        else:
            self.adj.flip_v = not self.adj.flip_v
        self.render()

    def _collect(self, resize: bool) -> imaging.Adjust:
        f = lambda k: 1 + self.sl[k].value() / 100
        a = self.adj
        a.brightness, a.contrast, a.saturation = f("brightness"), f("contrast"), f("saturation")
        a.sharpness = 1 + self.sl["sharpness"].value() / 50
        a.warmth = self.sl["warmth"].value() / 100
        a.grayscale = self.gray.isChecked()
        a.resize_pct = self.size_sl.value() if resize else 100
        return a

    def render(self):
        out = self.small if self.comparing else imaging.apply_adjust(self.small, self._collect(False))
        pm = QPixmap.fromImage(qimage_from_pil(out))
        self.preview.setPixmap(pm.scaled(self.preview.size() - QSize(10, 10), Qt.KeepAspectRatio,
                                         Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "_timer"):
            self._timer.start()

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self.render)

    def reset(self):
        for s in self.sl.values():
            s.setValue(0)
        self.size_sl.setValue(100)
        self.gray.setChecked(False)
        self.adj = imaging.Adjust()
        self._size_label()
        self.render()

    def _summary(self) -> str:
        bits = [f"{k} {s.value():+d}" for k, s in self.sl.items() if s.value()]
        if self.adj.rotate:
            bits.append(f"rotated {self.adj.rotate}°")
        if self.adj.flip_h or self.adj.flip_v:
            bits.append("flipped")
        if self.gray.isChecked():
            bits.append("B&W")
        return ", ".join(bits[:4])

    def save(self):
        adj = imaging.Adjust(**vars(self._collect(True)))
        path = self.path

        def work():
            return [imaging.save_like(imaging.apply_adjust(open_image(path), adj), path, "Edited")]
        self.run(work, "Applying edits…", self._summary())


# ================================================================ METADATA
class MetadataWindow(ToolWindow):
    tool_name = "Metadata"

    def __init__(self, path, notify, is_pdf: bool = False):
        super().__init__("Metadata", path, notify, size=(640, 380))
        self.is_pdf = is_pdf
        info = imaging.read_pdf_metadata(path) if is_pdf else imaging.read_image_metadata(path)
        summary = {} if is_pdf else imaging.metadata_summary(path)
        self.n_fields, self.has_gps = (len(info), False) if is_pdf else imaging.count_metadata(path)

        card = QFrame()
        card.setObjectName("card")
        cl = QGridLayout(card)
        cl.setContentsMargins(14, 12, 14, 12)
        cl.setHorizontalSpacing(16)
        rows = summary or {k: v for k, v in info.items() if k in ("title", "author", "creator", "producer", "Pages")}
        if not rows:
            rows = {"": "No personal metadata found."}
        for i, (k, v) in enumerate(rows.items()):
            kl = QLabel(k)
            kl.setObjectName("dim")
            cl.addWidget(kl, i, 0)
            vl = QLabel(v)
            vl.setTextInteractionFlags(Qt.TextSelectableByMouse)
            cl.addWidget(vl, i, 1)
            if k == "Location":
                vl.setObjectName("warn")
                mb = QPushButton("Open map")
                lat, lon = v.split(", ")
                mb.clicked.connect(lambda _=False, a=lat, b=lon: QDesktopServices.openUrl(
                    QUrl(f"https://www.openstreetmap.org/?mlat={a}&mlon={b}#map=15/{a}/{b}")))
                cl.addWidget(mb, i, 2)
        self.body.addWidget(card)
        if self.has_gps:
            warn = QLabel("⚠  This photo reveals where it was taken.")
            warn.setObjectName("warn")
            self.body.addWidget(warn)

        self.toggle = QPushButton(f"Show all {self.n_fields} fields")
        self.toggle.setCheckable(True)
        self.body.addWidget(self.toggle, 0, Qt.AlignLeft)
        t = QTableWidget(len(info), 2)
        t.setHorizontalHeaderLabels(["Field", "Value"])
        t.verticalHeader().setVisible(False)
        t.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        t.horizontalHeader().setStretchLastSection(True)
        t.setEditTriggers(QTableWidget.NoEditTriggers)
        for r, (k, v) in enumerate(info.items()):
            t.setItem(r, 0, QTableWidgetItem(k))
            t.setItem(r, 1, QTableWidgetItem(v))
        t.setVisible(False)

        def show_all(on):
            t.setVisible(on)
            self.resize(self.width(), 640 if on else 380)
        self.toggle.toggled.connect(show_all)
        self.body.addWidget(t, 1)
        self.body.addStretch(0)

        extra = []
        if self.has_gps:
            loc = QPushButton("Remove location only")
            loc.clicked.connect(self.strip_location)
            extra.append(loc)
        self.body.addLayout(self.footer("Remove all metadata", self.strip, None, extra))

    def strip(self):
        path, is_pdf = self.path, self.is_pdf
        detail = f"removed {self.n_fields} field{'s' if self.n_fields != 1 else ''}" + (" incl. GPS location" if self.has_gps else "")

        def work():
            return [imaging.strip_pdf_metadata(path) if is_pdf else imaging.strip_image_metadata(path)]
        self.run(work, "Removing metadata…", detail)

    def strip_location(self):
        path = self.path
        self.run(lambda: [imaging.strip_location(path)], "Removing location…", "GPS removed, camera info kept")


# ================================================================ COMPRESS
class CompressWindow(ToolWindow):
    tool_name = "Compress"
    SIDES = [("Original size", None), ("Max 3840 px", 3840), ("Max 2560 px", 2560),
             ("Max 1920 px", 1920), ("Max 1280 px", 1280), ("Max 800 px", 800)]
    TARGETS = [("2 MB", 2_000_000), ("1 MB", 1_000_000), ("500 KB", 500_000), ("200 KB", 200_000), ("100 KB", 100_000)]

    def __init__(self, paths, notify):
        super().__init__("Compress", paths, notify, size=(640, 580))
        self.orig_total = sum(file_size(p) for p in self.paths)
        self.pv = open_preview(self.path, 1600)
        with Image.open(self.path) as im:
            self.full_px = im.width * im.height
            self.full_long = max(im.size)

        row, self.mode = toggle_row(["Quality", "Target size"])
        self.body.addLayout(row)

        self.q_box = QWidget()
        g = QGridLayout(self.q_box)
        g.setContentsMargins(0, 0, 0, 0)
        self.q = slider(10, 95, 72)
        slider_row(g, 0, "Quality", self.q, lambda v: f"{v}")
        self.body.addWidget(self.q_box)

        self.t_box = QWidget()
        tl = QHBoxLayout(self.t_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(QLabel("Make each file under"))
        self.tgrp = QButtonGroup(self)
        for i, (lbl, _) in enumerate(self.TARGETS):
            b = QPushButton(lbl)
            b.setCheckable(True)
            b.setChecked(i == 2)
            self.tgrp.addButton(b, i)
            tl.addWidget(b)
        tl.addStretch(1)
        self.t_box.setVisible(False)
        self.body.addWidget(self.t_box)

        g2 = QGridLayout()
        g2.addWidget(QLabel("Size"), 0, 0)
        self.side = QComboBox()
        for name, _ in self.SIDES:
            self.side.addItem(name)
        g2.addWidget(self.side, 0, 1)
        self.webp = QCheckBox("Convert to WebP (usually the smallest)")
        g2.addWidget(self.webp, 1, 1)
        self.body.addLayout(g2)

        # before / after at 100%
        pr = QHBoxLayout()
        self.before = QLabel()
        self.after = QLabel()
        for lab, cap in ((self.before, "Original"), (self.after, "Compressed")):
            col = QVBoxLayout()
            lab.setFixedSize(270, 170)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("border-radius: 12px; background: rgba(0,0,0,0.25);")
            c = QLabel(cap)
            c.setObjectName("dim")
            col.addWidget(lab)
            col.addWidget(c, 0, Qt.AlignHCenter)
            pr.addLayout(col)
        self.body.addLayout(pr)

        self.est = QLabel()
        self.est.setObjectName("big")
        self.saving = QLabel()
        self.saving.setObjectName("good")
        self.body.addWidget(self.est)
        self.body.addWidget(self.saving)
        self.body.addStretch(1)
        label = "Compress" if len(self.paths) == 1 else f"Compress {len(self.paths)} files"
        self.body.addLayout(self.footer(label, self.save))

        self._timer = QTimer(self, singleShot=True, interval=60, timeout=self.estimate)
        self.q.valueChanged.connect(lambda _: self._timer.start())
        self.side.currentIndexChanged.connect(lambda _: self._timer.start())
        self.webp.toggled.connect(lambda _: self._timer.start())
        self.tgrp.idClicked.connect(lambda _: self._timer.start())
        self.mode.idClicked.connect(self._mode)
        self._set_before()
        self.estimate()

    def _mode(self, i):
        self.q_box.setVisible(i == 0)
        self.t_box.setVisible(i == 1)
        self._timer.start()

    def _detail_box(self, im: Image.Image) -> tuple[int, int, int, int]:
        """The 270×170 window with the most detail (where compression artefacts show)."""
        from PIL import ImageFilter, ImageStat
        w, h = 270, 170
        if im.width <= w or im.height <= h:
            return (0, 0, min(w, im.width), min(h, im.height))
        edges = im.convert("L").filter(ImageFilter.FIND_EDGES)
        best, box = -1.0, (0, 0, w, h)
        for gx in range(5):
            for gy in range(5):
                x = int((im.width - w) * gx / 4)
                y = int((im.height - h) * gy / 4)
                v = ImageStat.Stat(edges.crop((x, y, x + w, y + h))).mean[0]
                if v > best:
                    best, box = v, (x, y, x + w, y + h)
        return box

    def _crop_100(self, im: Image.Image) -> QPixmap:
        if not hasattr(self, "_box"):
            self._box = self._detail_box(self.pv)
        return QPixmap.fromImage(qimage_from_pil(im.crop(self._box)))

    def _set_before(self):
        self.before.setPixmap(self._crop_100(self.pv))

    def _settings(self):
        side = self.SIDES[self.side.currentIndex()][1]
        target = imaging.lossy_target(self.path, self.webp.isChecked())
        return side, target

    def estimate(self):
        side, target = self._settings()
        im = self.pv
        # scale factor from preview pixels to output pixels
        out_long = min(self.full_long, side) if side else self.full_long
        px_ratio = (out_long / max(im.size)) ** 2
        if self.mode.checkedId() == 1:
            limit = self.TARGETS[self.tgrp.checkedId()][1]
            q = self._quality_for(im, target, limit, px_ratio)
            note = f"quality ≈ {q}"
        else:
            q = self.q.value()
            note = ""
        n = int(imaging.encode_size(im, target, q) * px_ratio)
        buf = io.BytesIO()
        if target == "PNG":
            im.convert("RGBA").quantize(colors=max(16, min(256, int(q * 2.56)))).save(buf, "PNG")
        else:
            from .convert import save_image
            save_image(im, buf, target, quality=q)
        buf.seek(0)
        try:
            self.after.setPixmap(self._crop_100(Image.open(buf).convert("RGB")))
        except Exception:
            pass
        orig1 = file_size(self.path)
        if len(self.paths) > 1:
            total = int(n * self.orig_total / max(1, orig1))
            self.est.setText(f"{human_size(self.orig_total)}  →  ≈ {human_size(total)}")
            n_cmp, o_cmp = total, self.orig_total
        else:
            self.est.setText(f"{human_size(orig1)}  →  ≈ {human_size(n)}")
            n_cmp, o_cmp = n, orig1
        pct = (1 - n_cmp / max(1, o_cmp)) * 100
        fmt = target if target != "JPG" else "JPG"
        if pct >= 1:
            self.saving.setObjectName("good")
            self.saving.setText(f"≈ {pct:.0f}% smaller · {fmt}" + (f" · {note}" if note else ""))
        else:
            self.saving.setObjectName("warn")
            self.saving.setText("Already well compressed — try a smaller size or WebP")
        self.saving.style().unpolish(self.saving)
        self.saving.style().polish(self.saving)

    @staticmethod
    def _quality_for(im, target, limit, px_ratio) -> int:
        lo, hi, best = 30, 92, 30
        while lo <= hi:
            q = (lo + hi) // 2
            if imaging.encode_size(im, target, q) * px_ratio <= limit:
                best, lo = q, q + 1
            else:
                hi = q - 1
        return best

    def save(self):
        side, target = self._settings()
        paths = list(self.paths)
        if self.mode.checkedId() == 1:
            limit = self.TARGETS[self.tgrp.checkedId()][1]
            label = self.TARGETS[self.tgrp.checkedId()][0]

            def work():
                outs = []
                for p in paths:
                    im = open_image(p)
                    if side and max(im.size) > side:
                        im.thumbnail((side, side), Image.LANCZOS)
                    q, s2 = imaging.plan_target_size(im, target, limit)
                    outs.append(imaging.compress_to(p, q, min(x for x in (side, s2) if x) if (side or s2) else None, target))
                return outs
            self.run(work, "Compressing…", f"target {label}")
        else:
            q = self.q.value()
            self.run(lambda: [imaging.compress_to(p, q, side, target) for p in paths], "Compressing…", f"quality {q}")


# ================================================================ RESIZE
class ResizeWindow(ToolWindow):
    tool_name = "Resize"
    PRESETS = [("25%", "pct", 25), ("50%", "pct", 50), ("75%", "pct", 75), ("3840", "long", 3840),
               ("2560", "long", 2560), ("1920", "long", 1920), ("1280", "long", 1280), ("800", "long", 800)]

    def __init__(self, paths, notify):
        super().__init__("Resize", paths, notify, size=(600, 380))
        with Image.open(self.path) as im:
            w, h = im.size
            if im.getexif().get(0x0112, 1) in (5, 6, 7, 8):
                w, h = h, w
        self.orig = (w, h)
        self.body.addWidget(section("Presets — percent or longest side in px"))
        row, self.grp = toggle_row([p[0] for p in self.PRESETS], 1)
        self.grp.idClicked.connect(self._preset)
        self.body.addLayout(row)
        self.body.addWidget(section("Exact size"))
        er = QHBoxLayout()
        self.w = QSpinBox()
        self.h = QSpinBox()
        for s in (self.w, self.h):
            s.setRange(1, 30000)
            s.setFixedWidth(100)
        self.lock = QCheckBox("Keep proportions")
        self.lock.setChecked(True)
        er.addWidget(QLabel("W"))
        er.addWidget(self.w)
        er.addWidget(QLabel("H"))
        er.addWidget(self.h)
        er.addWidget(QLabel("px"))
        er.addSpacing(10)
        er.addWidget(self.lock)
        er.addStretch(1)
        self.body.addLayout(er)
        self.out = QLabel()
        self.out.setObjectName("big")
        self.body.addWidget(self.out)
        self.body.addStretch(1)
        label = "Resize" if len(self.paths) == 1 else f"Resize {len(self.paths)} files"
        self.body.addLayout(self.footer(label, self.save))
        self.w.valueChanged.connect(lambda v: self._typed("w", v))
        self.h.valueChanged.connect(lambda v: self._typed("h", v))
        self._preset(1)

    def _set(self, w, h):
        for box, v in ((self.w, w), (self.h, h)):
            box.blockSignals(True)
            box.setValue(v)
            box.blockSignals(False)
        self.out.setText(f"{dims_text(*self.orig)}  →  {dims_text(w, h)}")

    def _preset(self, i):
        _, mode, val = self.PRESETS[i]
        self.mode = (mode, val)
        self._set(*imaging.resize_dims(self.orig, mode, val))

    def _typed(self, which, v):
        b = self.grp.checkedButton()
        if b:
            self.grp.setExclusive(False)
            b.setChecked(False)
            self.grp.setExclusive(True)
        if self.lock.isChecked():
            w, h = imaging.resize_dims(self.orig, which, v)
        else:
            w, h = self.w.value(), self.h.value()
        self.mode = (which, v) if self.lock.isChecked() else ("exact", (w, h))
        self._set(w, h)

    def save(self):
        mode, val = self.mode
        paths = list(self.paths)

        def dims_for(p):
            with Image.open(p) as im:
                size = im.size if im.getexif().get(0x0112, 1) not in (5, 6, 7, 8) else im.size[::-1]
            if mode == "exact":
                return val
            return imaging.resize_dims(size, mode, val)
        self.run(lambda: [imaging.resize_image(p, dims_for(p)) for p in paths], "Resizing…")


# ================================================================ PROTECT (PDF)
class ProtectWindow(ToolWindow):
    tool_name = "Protect"

    def __init__(self, path, notify):
        super().__init__("Password-protect PDF", path, notify, size=(520, 330))
        g = QGridLayout()
        self.pw = QLineEdit()
        self.pw2 = QLineEdit()
        for e in (self.pw, self.pw2):
            e.setEchoMode(QLineEdit.Password)
        g.addWidget(QLabel("Password"), 0, 0)
        g.addWidget(self.pw, 0, 1)
        g.addWidget(QLabel("Confirm"), 1, 0)
        g.addWidget(self.pw2, 1, 1)
        show = QCheckBox("Show")
        show.toggled.connect(lambda on: [e.setEchoMode(QLineEdit.Normal if on else QLineEdit.Password)
                                         for e in (self.pw, self.pw2)])
        g.addWidget(show, 0, 2)
        self.body.addLayout(g)
        self.msg = QLabel("AES-256 encryption. Anyone opening the PDF will need this password.")
        self.msg.setObjectName("dim")
        self.msg.setWordWrap(True)
        self.body.addWidget(self.msg)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Protect", self.save))
        self.pw.textChanged.connect(self._check)
        self.pw2.textChanged.connect(self._check)
        self._check()
        QTimer.singleShot(0, self.pw.setFocus)

    def _check(self):
        a, b = self.pw.text(), self.pw2.text()
        ok = len(a) >= 4 and a == b
        self.primary_btn.setEnabled(ok)
        if a and len(a) < 4:
            self.msg.setText("Use at least 4 characters.")
        elif b and a != b:
            self.msg.setText("Passwords don't match yet.")
        elif ok:
            strength = "strong" if len(a) >= 12 else "okay" if len(a) >= 8 else "weak"
            self.msg.setText(f"Looks good · {strength} password")
        else:
            self.msg.setText("AES-256 encryption. Anyone opening the PDF will need this password.")

    def save(self):
        pw, path = self.pw.text(), self.path
        self.run(lambda: [imaging.protect_pdf(path, pw)], "Encrypting…")
