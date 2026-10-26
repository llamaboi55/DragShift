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
    monkeypatch.setattr(glass, "allow_capture", lambda *args: True)
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


def test_macos_offscreen_winid_is_never_treated_as_objc_pointer(qapp, monkeypatch):
    from types import SimpleNamespace
    from dragshift import platform_mac
    calls=[]
    monkeypatch.setattr(platform_mac,'OK',True)
    monkeypatch.setitem(sys.modules,'objc',SimpleNamespace(objc_object=lambda **kw:calls.append(kw)))
    widget=QWidget()
    assert platform_mac.window_number(widget)==0
    assert calls==[]
    widget.close()


@pytest.mark.parametrize('capture_fails',[False,True])
def test_background_capture_only_excludes_window_during_grab(monkeypatch,capture_fails):
    from PIL import Image
    from types import SimpleNamespace
    calls=[]
    widget=SimpleNamespace(isVisible=lambda:True,winId=lambda:42)
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(glass,'_capture_exclusion_available',lambda:True,raising=False)
    monkeypatch.setattr(glass,'_set_capture_affinity',
        lambda hwnd,value:calls.append(('affinity',hwnd,value)) or True,raising=False)
    monkeypatch.setattr(glass,'_flush_compositor',lambda:calls.append(('flush',)) or True,raising=False)
    def grab(*args):
        calls.append(('grab',args))
        if capture_fails:
            raise OSError('capture unavailable')
        return Image.new('RGB',(100,80),'blue')
    monkeypatch.setattr(glass,'grab_screen',grab)
    result=glass.capture_background(widget,QRect(10,20,100,80))
    assert calls==[('affinity',42,0x11),('flush',),
                   ('grab',(10,20,100,80,0)),('affinity',42,0),('flush',)]
    assert (result is None)==capture_fails


def test_failed_exclusion_never_grabs_visible_window(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    widget=SimpleNamespace(isVisible=lambda:True,winId=lambda:42)
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(glass,'_capture_exclusion_available',lambda:True,raising=False)
    monkeypatch.setattr(glass,'_set_capture_affinity',
        lambda hwnd,value:calls.append(value) or value==0,raising=False)
    monkeypatch.setattr(glass,'_flush_compositor',lambda:None,raising=False)
    monkeypatch.setattr(glass,'grab_screen',lambda *_:pytest.fail('recursive capture'))
    assert glass.capture_background(widget,QRect(0,0,100,80)) is None
    assert calls==[0x11,0]


def test_moving_without_clean_capture_keeps_cached_refraction(qapp,monkeypatch):
    from PIL import Image
    from dragshift.util import ALIVE
    monkeypatch.setattr(glass,'capture_background',lambda *_:Image.new('RGB',(100,80),'blue'),raising=False)
    widget=QWidget()
    widget.resize(100,80)
    backing=glass.LiquidBacking(widget,12)
    backing.capture_at(QRect(0,0,100,80))
    deadline=time.monotonic()+5
    while ALIVE and time.monotonic()<deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert backing.image is not None
    before=backing.image.copy()
    backing.moved()
    assert backing.image==before
    monkeypatch.setattr(glass,'capture_background',lambda *_:None)
    backing.capture_at(QRect(30,30,120,90))
    deadline=time.monotonic()+5
    while ALIVE and time.monotonic()<deadline:
        qapp.processEvents()
        time.sleep(.005)
    assert backing.image is not None and (backing.image.width(),backing.image.height())==(120,90)
    assert backing.image.pixelColor(60,45).blue()>backing.image.pixelColor(60,45).red()
    widget.close()


@pytest.mark.parametrize('surface',['editor','toast','ring'])
def test_showing_surfaces_keeps_normal_capture_affinity(qapp,monkeypatch,surface):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from dragshift import radial,toasts
    values=[]
    monkeypatch.setattr(glass,'_set_capture_affinity',lambda hwnd,value:values.append(value) or True)
    monkeypatch.setattr(glass,'_flush_compositor',lambda:True)
    monkeypatch.setattr(glass,'enable_acrylic',lambda *_:False)
    monkeypatch.setattr(glass.LiquidBacking,'capture_at',lambda *_:None)
    monkeypatch.setattr(glass,'capture_background',lambda *_:None)
    monkeypatch.setattr(glass,'hidden_drag_images',lambda:nullcontext(SimpleNamespace(rects=[])))
    if surface=='editor':
        widget=GlassWindow('Capture controls',size=(420,280))
        widget.show()
    elif surface=='toast':
        widget=toasts.ToastCard(toasts.ToastStack())
        widget.appear(QRect(0,0,toasts.W,toasts.H))
    else:
        widget=radial.RadialMenu(lambda _: {},lambda _:'File')
        widget.popup(paths=['example.txt'])
    assert values==[0]
    widget.close()


def test_capture_restore_is_retried_without_another_screen_read(monkeypatch):
    from PIL import Image
    from types import SimpleNamespace
    values=[]
    reads=[]
    widget=SimpleNamespace(isVisible=lambda:True,winId=lambda:42)
    monkeypatch.setattr(sys,'platform','win32')
    monkeypatch.setattr(glass,'_capture_exclusion_available',lambda:True)
    def affinity(hwnd,value):
        values.append(value)
        return len(values)!=2
    monkeypatch.setattr(glass,'_set_capture_affinity',affinity)
    monkeypatch.setattr(glass,'_flush_compositor',lambda:True)
    monkeypatch.setattr(glass,'grab_screen',lambda *args:reads.append(args) or Image.new('RGB',(100,80)))
    assert glass.capture_background(widget,QRect(0,0,100,80)) is not None
    assert values==[0x11,0,0] and len(reads)==1
