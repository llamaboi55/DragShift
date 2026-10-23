"""Contrast, requested glass refraction and editor lifecycle checks."""
import time
import sys

import pytest
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPalette, QPixmap
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLineEdit, QPushButton, QWidget

from dragshift import glass, theme
from dragshift.window import GlassWindow, ImageView


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def luminance(color):
    rgb = QColor(color).getRgbF()[:3]
    linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in rgb]
    return sum(a * b for a, b in zip(linear, (.2126, .7152, .0722)))


@pytest.mark.parametrize("text", [theme.TEXT, theme.MUTED, theme.GOOD, theme.ERROR])
def test_small_text_has_readable_contrast(text):
    assert (luminance(theme.PANEL) + .05) / (luminance(text) + .05) >= 4.5


def test_panel_retains_backdrop_with_neutral_tint_and_no_color_gradients(qapp):
    image = QImage(100, 80, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    backdrop = QPixmap(100, 80)
    backdrop.fill(QColor("#ff00ff"))
    painter = QPainter(image)
    path = glass.rounded_rect_path(QRectF(.5, .5, 99, 79), 12)
    glass.paint_glass(painter, path, backdrop, rainbow_alpha=255)
    painter.end()
    colors = {image.pixelColor(x, y).name() for x, y in [(20, 20), (50, 40), (80, 60)]}
    assert len(colors) == 1 and colors != {theme.PANEL}  # the backdrop remains visible
    assert image.pixelColor(50, 40).green() > 190  # light tint keeps dark text readable
    assert glass.iridescent_pen(QPointF(), 1).brush().gradient() is None
    assert glass.iridescent_brush(QRectF(0, 0, 10, 10)).gradient() is None


def test_backing_preserves_requested_capture_and_refraction(qapp, monkeypatch):
    from PIL import Image
    from dragshift.util import ALIVE
    captures = []
    def capture(*args):
        captures.append(args)
        return Image.new("RGB", (100, 80), "blue")
    monkeypatch.setattr(glass, "grab_screen", capture)
    widget = QWidget()
    backing = glass.LiquidBacking(widget, 12)
    backing.capture_at(QRect(0, 0, 100, 80))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and (backing.image is None or ALIVE):
        qapp.processEvents()
        time.sleep(.005)
    assert captures and backing.image is not None and not ALIVE
    assert backing.image.pixelColor(50, 40).blue() > backing.image.pixelColor(50, 40).red()
    assert backing.image.pixelColor(50, 40).red() > 190
    widget.close()


@pytest.mark.parametrize("native_blur", [False, True])
def test_window_preserves_controls_escape_and_glass_capture(qapp, monkeypatch, native_blur):
    from dragshift import window as window_module
    monkeypatch.setattr(window_module, "IS_WIN", True)
    monkeypatch.setattr(glass, "enable_acrylic", lambda hwnd: native_blur)
    captures = []
    monkeypatch.setattr(glass.LiquidBacking, "capture_at", lambda *args: captures.append(args))
    monkeypatch.setattr(glass, "exclude_from_capture", lambda *args: True)
    window = GlassWindow("Neutral controls", size=(420, 280))
    field = QLineEdit("File name")
    window.body.addWidget(field)
    button = QPushButton("Save")
    button.setObjectName("primary")
    window.body.addWidget(button)
    disabled = QPushButton("Unavailable")
    disabled.setEnabled(False)
    window.body.addWidget(disabled)
    window.present()
    qapp.processEvents()
    assert window._acrylic is native_blur and window.radius == 12 and len(captures) == 1
    assert field.palette().color(QPalette.Text).name() == theme.TEXT
    assert field.palette().color(QPalette.Base).name() == "#ffffff"
    assert disabled.palette().color(QPalette.ButtonText).name() == theme.MUTED
    if sys.platform == "win32":
        assert field.font().family() == "Segoe UI"
    QTest.keyClick(window, Qt.Key_Escape)
    qapp.processEvents()


def test_image_mapping_is_unchanged_by_neutral_backing(qapp):
    image = QImage(160, 120, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    view = ImageView(image)
    view.resize(400, 300)
    point = QPointF(50.25, 80.5)
    restored = view.to_image(view.to_widget(point))
    assert restored.x() == pytest.approx(point.x())
    assert restored.y() == pytest.approx(point.y())
    view.close()
