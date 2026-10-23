"""Neutral frosted refraction and compatibility helpers for existing editors."""
from __future__ import annotations

import ctypes
import os
import sys

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QPainter, QPainterPath, QPen, QPixmap

from . import theme

# Old imports keep working while all surfaces use the same neutral palette.
IRIDESCENT = [theme.BORDER] * 8
TEXT = theme.TEXT
TEXT_DIM = theme.MUTED
ACCENT_A = ACCENT_B = ACCENT_C = theme.ACCENT


def iridescent_pen(center: QPointF, width: float, alpha: int = 200, angle: float = 30) -> QPen:
    color = QColor(theme.ACCENT)
    color.setAlpha(alpha)
    return QPen(color, width)


def iridescent_brush(rect: QRectF, alpha: int = 60) -> QBrush:
    color = QColor(theme.HOVER)
    color.setAlpha(alpha)
    return QBrush(color)


def blur_pixmap(pm: QPixmap, radius: int = 18) -> QPixmap:
    """Fast frosted blur: downscale, light box blur via repeated scaling, upscale."""
    if pm.isNull():
        return pm
    w, h = pm.width(), pm.height()
    f = max(2, radius // 3)
    small = pm.scaled(max(1, w // f), max(1, h // f), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    small = small.scaled(max(1, w // (f * 2)), max(1, h // (f * 2)), Qt.IgnoreAspectRatio,
                         Qt.SmoothTransformation)
    return small.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def paint_glass(p: QPainter, path: QPainterPath, backdrop: QPixmap | None = None,
                backdrop_offset: QPointF = QPointF(0, 0), tint: QColor | None = None,
                rim: bool = True, rainbow_alpha: int = 170, highlight: float = 1.0) -> None:
    """Paint neutral frosting, retaining the captured backdrop when supplied."""
    p.save()
    p.setRenderHint(QPainter.Antialiasing)
    p.setClipPath(path)
    if backdrop is not None and not backdrop.isNull():
        p.drawPixmap(backdrop_offset, backdrop)
    fill = QColor(theme.PANEL)
    if tint is not None:
        fill.setAlpha(tint.alpha())
    elif backdrop is not None and not backdrop.isNull():
        fill.setAlpha(round(neutral_tint()[3] * 255))
    p.fillPath(path, fill)
    if rim:
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawPath(path)
    p.restore()


# ---------------------------------------------------------------- Qt stylesheet

def qss() -> str:
    return theme.qss()


def __getattr__(name):
    if name == "QSS":
        return qss()
    raise AttributeError(name)


# ---------------------------------------------------------------- Windows acrylic
class _ACCENT(ctypes.Structure):
    _fields_ = [("AccentState", ctypes.c_int), ("AccentFlags", ctypes.c_int),
                ("GradientColor", ctypes.c_uint), ("AnimationId", ctypes.c_int)]


class _WCAD(ctypes.Structure):
    _fields_ = [("Attribute", ctypes.c_int), ("Data", ctypes.c_void_p), ("SizeOfData", ctypes.c_size_t)]


def enable_acrylic(hwnd: int, tint_abgr: int = 0xB8FAFBFB) -> bool:
    """Native neutral blur fallback; returns False when unavailable."""
    if sys.platform != "win32":
        return False
    try:
        user32 = ctypes.windll.user32
        accent = _ACCENT(4, 2, tint_abgr, 0)  # 4 = ACCENT_ENABLE_ACRYLICBLURBEHIND
        data = _WCAD(19, ctypes.cast(ctypes.pointer(accent), ctypes.c_void_p), ctypes.sizeof(accent))
        ok = bool(user32.SetWindowCompositionAttribute(ctypes.c_void_p(hwnd), ctypes.byref(data)))
        try:  # rounded corners on Windows 11
            pref = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33,
                                                       ctypes.byref(pref), ctypes.sizeof(pref))
        except Exception:
            pass
        return ok
    except Exception:
        return False


def qimage_from_pil(im) -> QImage:
    im = im.convert("RGBA")
    data = im.tobytes("raw", "RGBA")
    qi = QImage(data, im.width, im.height, im.width * 4, QImage.Format_RGBA8888)
    return qi.copy()  # detach from the Python buffer


def pil_from_qimage(qi: QImage):
    from PIL import Image
    qi = qi.convertToFormat(QImage.Format_RGBA8888)
    ptr = qi.constBits()
    return Image.frombuffer("RGBA", (qi.width(), qi.height()), bytes(ptr), "raw", "RGBA",
                            qi.bytesPerLine(), 1).copy()


def rounded_rect_path(r: QRectF, radius: float) -> QPainterPath:
    path = QPainterPath()
    path.addRoundedRect(r, radius, radius)
    return path


# ---------------------------------------------------------------- live liquid backing
def exclude_from_capture(hwnd: int) -> bool:
    """Allow refreshing the background without capturing the glass itself."""
    if sys.platform == "darwin":
        from . import platform_mac
        return platform_mac.OK
    if sys.platform != "win32" or os.environ.get("DRAGSHIFT_RECORDING"):
        return False
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(hwnd), 0x11))
    except Exception:
        return False


from PySide6.QtCore import QObject, QRect, QRunnable, QThreadPool, QTimer, Signal, Slot  # noqa: E402


def neutral_style():
    from .liquid import GlassStyle
    return GlassStyle(blur=12, strength=14, bezel=18, dispersion=.03, saturation=.60, spec=.20)


def neutral_tint():
    return (251, 251, 250, .82)


class _LiquidSig(QObject):
    done = Signal(int, QImage, bool)
    finished = Signal(object)


class _LiquidJob(QRunnable):
    def __init__(self, serial, backdrop, w, h, radius, style):
        super().__init__()
        self.args = (serial, backdrop, w, h, radius, style)
        self.sig = _LiquidSig()

    def run(self):
        serial, backdrop, w, h, radius, style = self.args
        try:
            from . import liquid
            sdf = liquid.sdf_rounded_rect(w, h, .5, .5, w - .5, h - .5, radius)
            image, light = liquid.render(backdrop, sdf, style, tint=neutral_tint())
            self.sig.done.emit(serial, qimage_from_pil(image), light)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            self.sig.finished.emit(self)


def grab_screen(x: int, y: int, w: int, h: int, below_window: int = 0):
    """Screen pixels under a rect (logical coords) as a PIL image of size w×h.
    macOS: native capture that leaves out the drag picture (and `below_window` and above)."""
    if sys.platform == "darwin":
        from . import platform_mac
        if platform_mac.OK:
            im = platform_mac.capture(x, y, w, h, below_window)
            if im is not None:
                return im
    from PySide6.QtCore import QPoint
    from PySide6.QtGui import QGuiApplication
    scr = QGuiApplication.screenAt(QPoint(x + w // 2, y + h // 2)) or QGuiApplication.primaryScreen()
    g = scr.geometry()
    try:
        shot = scr.grabWindow(0, x - g.x(), y - g.y(), w, h)
    except Exception:
        return None
    if shot.isNull():
        return None
    return pil_from_qimage(shot.toImage().scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))


class LiquidBacking(QObject):
    """Keeps a refracted local snapshot of the background behind a window."""
    changed = Signal()

    def __init__(self, widget, radius: float, style=None):
        super().__init__(widget)
        self.w = widget
        self.radius = radius
        self.style = style or neutral_style()
        self.image: QImage | None = None
        self.is_light = True
        self.excluded = False
        self._serial = 0
        self._debounce = QTimer(self, singleShot=True, interval=140, timeout=self.refresh)

    def capture_at(self, rect: QRect):
        below = 0
        if sys.platform == "darwin" and self.w.isVisible():
            from . import platform_mac
            below = platform_mac.window_number(self.w)
        backdrop = grab_screen(rect.x(), rect.y(), rect.width(), rect.height(), below)
        if backdrop is None:
            return
        self._serial += 1
        job = _LiquidJob(self._serial, backdrop, rect.width(), rect.height(), self.radius, self.style)
        from .util import keep, release
        # A bound Qt slot disconnects when the window closes during a render.
        job.sig.done.connect(self._done)
        job.sig.finished.connect(release)
        QThreadPool.globalInstance().start(keep(job))

    @Slot(int, QImage, bool)
    def _done(self, serial, image, light):
        if serial == self._serial:
            self.image, self.is_light = image, light
            self.changed.emit()

    def moved(self):
        self._serial += 1  # discard a render captured at the old location or size
        if self.excluded:
            if self.image is not None and self.image.size() != self.w.size():
                self.image = None
            self._debounce.start()
        else:
            self.image = None  # never re-capture a visible window without exclusion

    def refresh(self):
        if self.excluded and self.w.isVisible():
            self.capture_at(self.w.frameGeometry())


# ---------------------------------------------------------------- drag-image hiding
class hidden_drag_images:
    """While dragging, Windows draws a translucent picture of the file under the cursor.
    Hide it for the instant we capture the screen behind the menu, so it doesn't get frozen
    into the glass, then show it again. We hide every visible top-level window of another
    process that sits under the cursor and is layered + click-through (that is what drag
    images are), plus the known drag-image classes."""

    CLASSES = ("SysDragImage", "DragImageWindow", "DragWindow")
    WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020

    def __init__(self, point=None):
        self.point = point
        self.hwnds: list[int] = []
        self.info: list[str] = []
        self.rects: list[tuple[int, int, int, int]] = []   # screen px, for inpainting

    def __enter__(self):
        if sys.platform != "win32":
            return self
        try:
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.GetWindowLongW.restype = ctypes.c_long
            pt = wintypes.POINT()
            user32.GetCursorPos(ctypes.byref(pt))
            mypid = os.getpid()
            found = []
            WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

            def cb(hwnd, _):
                h = ctypes.c_void_p(hwnd)
                if not user32.IsWindowVisible(h):
                    return True
                r = wintypes.RECT()
                user32.GetWindowRect(h, ctypes.byref(r))
                near = r.left - 40 <= pt.x <= r.right + 40 and r.top - 40 <= pt.y <= r.bottom + 40
                if not near or (r.right - r.left) > 900 or (r.bottom - r.top) > 900:
                    return True
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(h, ctypes.byref(pid))
                if pid.value == mypid:
                    return True
                buf = ctypes.create_unicode_buffer(64)
                user32.GetClassNameW(h, buf, 64)
                ex = user32.GetWindowLongW(h, -20)  # GWL_EXSTYLE
                drag_like = (ex & self.WS_EX_LAYERED) and (ex & self.WS_EX_TRANSPARENT)
                if buf.value in self.CLASSES or drag_like:
                    found.append(hwnd)
                    self.rects.append((r.left, r.top, r.right, r.bottom))
                    self.info.append(f"{buf.value} ex=0x{ex & 0xffffffff:08x} {r.right - r.left}x{r.bottom - r.top}")
                return True
            user32.EnumWindows(WNDENUMPROC(cb), 0)
            for h in found:
                user32.ShowWindow(ctypes.c_void_p(h), 0)  # SW_HIDE
            self.hwnds = found
            if found:
                ctypes.windll.gdi32.GdiFlush()
                ctypes.windll.dwmapi.DwmFlush()  # wait for the compositor to drop them
                ctypes.windll.dwmapi.DwmFlush()
        except Exception as e:
            self.info.append(f"error: {e}")
        return self

    def __exit__(self, *exc):
        if self.hwnds:
            try:
                for h in self.hwnds:
                    ctypes.windll.user32.ShowWindow(ctypes.c_void_p(h), 8)  # SW_SHOWNA
            except Exception:
                pass
        return False


def inpaint_rects(im, rects, pad: int = 6):
    """Fill rectangles of a PIL image from their surroundings (normalised convolution):
    a smooth, colour-matched patch — invisible once refracted and blurred by the glass."""
    import numpy as np
    from PIL import Image, ImageFilter
    if not rects:
        return im
    rgb = im.convert("RGB")
    w, h = rgb.size
    mask = Image.new("L", (w, h), 255)
    from PIL import ImageDraw
    d = ImageDraw.Draw(mask)
    any_hit = False
    for (x0, y0, x1, y1) in rects:
        x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
        if x1 > x0 and y1 > y0:
            d.rectangle((x0, y0, x1, y1), fill=0)
            any_hit = True
    if not any_hit:
        return im
    a = np.asarray(rgb, np.float32)
    m = np.asarray(mask, np.float32)[..., None] / 255
    radius = 28
    num = np.asarray(Image.fromarray(np.clip(a * m, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(radius)), np.float32)
    den = np.asarray(mask.filter(ImageFilter.GaussianBlur(radius)), np.float32)[..., None] / 255
    fill = num / np.maximum(den, 1e-3)
    fill = np.asarray(Image.fromarray(np.clip(fill, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(6)), np.float32)
    # feather the seam: the glass bezel magnifies any hard edge into a visible line
    soft = np.asarray(mask.filter(ImageFilter.GaussianBlur(12)), np.float32)[..., None] / 255
    soft = np.minimum(soft, m)  # never let the drag picture bleed back in
    out = a * soft + fill * (1 - soft)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


# ---------------------------------------------------------------- no-compositor mode (XFCE etc.)
_NO_COMP = None


def no_compositor() -> bool:
    """Linux/X11 without a compositing manager: translucency would render black, so glass
    surfaces are cut to their exact shape with a window mask instead (XShape)."""
    global _NO_COMP
    if _NO_COMP is None:
        _NO_COMP = False
        if sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen"):
            try:
                from . import platform_linux
                _NO_COMP = not platform_linux.composited()
            except Exception:
                _NO_COMP = False
    return _NO_COMP


def apply_shape(widget, path: QPainterPath):
    """Clip a top-level window to `path` when there's no compositor (no-op otherwise)."""
    if not no_compositor():
        return
    from PySide6.QtGui import QRegion
    region = QRegion()
    for poly in path.simplified().toFillPolygons():   # one polygon per separate piece
        region = region.united(QRegion(poly.toPolygon()))
    widget.setMask(region)
