"""Neutral frosted surfaces retain capture, radial and result interactions."""
from pathlib import Path
import time

import pytest
from PySide6.QtCore import QMimeData, QPoint, QPointF, QRect, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dragshift import glass, radial, toasts
from dragshift.report import Result


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def menu():
    return radial.RadialMenu(lambda paths: {
        "Convert": [radial.Item("MP4", key="convert:MP4"), radial.Item("WEBM", key="convert:WEBM")],
        "Tools": [radial.Item("Trim", "✂", key="tool:Trim", hint="Keep a section")],
    }, lambda paths: "1 video")


def test_popup_and_toast_capture_and_render_neutral_frosted_surfaces(qapp, monkeypatch):
    from PIL import Image, ImageDraw
    from dragshift.theme import PANEL
    captures = []

    def grab(x, y, width, height, below_window=0):
        captures.append((width, height))
        image = Image.new("RGB", (width, height), (75, 75, 75))
        painter = ImageDraw.Draw(image)
        for line in range(0, width, 32):
            painter.rectangle((line, 0, line + 12, height), fill=(170, 170, 170))
        return image

    monkeypatch.setattr(glass, "grab_screen", grab)
    monkeypatch.setattr(glass, "exclude_from_capture", lambda _: True)
    ring = menu()
    ring.popup(QPoint(250, 250), ["clip.mp4"])
    card = toasts.ToastCard(toasts.ToastStack())
    card.appear(QRect(20, 20, toasts.W, toasts.H))
    card.relocate(QRect(30, 30, toasts.W, toasts.H))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        qapp.processEvents()
        if (all(not image.isNull() for image in ring.liquid.values())
                and card.liquid.image is not None):
            break
        time.sleep(.01)
    assert ring._raw_backdrop is not None
    assert ring.liquid and all(not image.isNull() for image in ring.liquid.values())
    assert card.liquid.image is not None and not card.liquid.image.isNull()
    ring.anim.stop()
    ring._t = 1.0
    color = ring.grab().toImage().pixelColor(200, 50)
    assert color.name() != QColor(PANEL).name()  # The snapshot remains visible through the tint.
    assert min(color.red(), color.green(), color.blue()) > 180
    assert max(color.red(), color.green(), color.blue()) - min(color.red(), color.green(), color.blue()) < 15
    ring.close_menu()
    card.dismiss()
    assert (radial.SIZE, radial.SIZE) in captures and (toasts.W, toasts.H) in captures


def test_radial_neutral_fallback_hover_and_click_shift_actions(qapp):
    from dragshift.theme import PANEL, SELECTED
    ring = menu()
    ring._load(["clip.mp4"])
    ring.click_mode = True
    image = ring.grab().toImage()
    assert image.pixelColor(200, 50).name() == QColor(PANEL).name()
    ring.hover = 0
    ring.spring[0] = 1.0
    assert ring.grab().toImage().pixelColor(200, 50).name() == QColor(SELECTED).name()
    activated = []
    ring.activated.connect(lambda item, paths: activated.append((item.key, paths)))
    QTest.keyClick(ring, Qt.Key_Shift)
    assert ring.ring_idx == 1
    QTest.mouseClick(ring, Qt.LeftButton, pos=QPoint(200, 90))
    qapp.processEvents()
    assert activated == [("tool:Trim", ["clip.mp4"])]
    ring.close_menu()


def test_radial_can_be_deleted_while_refraction_is_rendering(qapp, monkeypatch):
    import sys
    import shiboken6
    from PIL import Image
    from dragshift import liquid
    from dragshift.util import ALIVE
    errors = []
    render = liquid.render

    def slow_render(*args, **kwargs):
        time.sleep(.03)
        return render(*args, **kwargs)

    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(liquid, "render", slow_render)
    ring = menu()
    ring._raw_backdrop = Image.new("RGB", (radial.SIZE, radial.SIZE), "grey")
    ring._load(["clip.mp4"])
    shiboken6.delete(ring)
    deadline = time.monotonic() + 10
    while ALIVE and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.01)
    assert not ALIVE and not errors


def test_radial_drag_and_drop_stays_copy_action(qapp, tmp_path):
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"original")
    ring = menu()
    data = QMimeData()
    data.setUrls([QUrl.fromLocalFile(str(source))])
    enter = QDragEnterEvent(QPoint(200, 200), Qt.CopyAction | Qt.MoveAction,
                           data, Qt.LeftButton, Qt.ShiftModifier)
    ring.dragEnterEvent(enter)
    assert enter.isAccepted() and enter.dropAction() == Qt.CopyAction
    activated = []
    ring.activated.connect(lambda item, paths: activated.append((item.key, paths)))
    drop = QDropEvent(QPointF(200, 90), Qt.CopyAction | Qt.MoveAction,
                      data, Qt.LeftButton, Qt.ShiftModifier)
    ring.dropEvent(drop)
    qapp.processEvents()
    assert drop.isAccepted() and drop.dropAction() == Qt.CopyAction
    assert len(activated) == 1 and activated[0][0] == "convert:MP4"
    assert [Path(path) for path in activated[0][1]] == [source]
    assert source.read_bytes() == b"original"


def test_toast_neutral_fallback_states_and_result_actions(qapp, tmp_path, monkeypatch):
    from dragshift.theme import PANEL
    source = tmp_path / "source.wav"
    result = tmp_path / "result.mp3"
    result.write_bytes(b"result")
    card = toasts.ToastCard(toasts.ToastStack())
    card.working("Converting audio", "source.wav")
    assert card.grab().toImage().pixelColor(20, 90).name() == QColor(PANEL).name()
    card.error("Conversion failed", "Choose another file")
    assert card.grab().toImage().pixelColor(20, 90).name() == QColor(PANEL).name()
    calls = []
    monkeypatch.setattr(card, "dismiss", lambda: calls.append(("dismiss", None)))
    monkeypatch.setattr(toasts, "open_file", lambda path: calls.append(("Open", path)))
    monkeypatch.setattr(toasts, "reveal", lambda path: calls.append(("Show", path)))
    monkeypatch.setattr(toasts, "trash", lambda paths: calls.append(("Undo", paths)) or 1)
    r = Result("Audio converted", "result.mp3", [result], [source])
    for action in ("Open", "Show", "Undo"):
        card.done(r)
        card.grab()
        assert [name for name, _ in card.buttons] == ["Open", "Show", "Undo"]
        card._act(action)
    assert [(action, value) for action, value in calls if action != "dismiss"] == [
        ("Open", result), ("Show", result), ("Undo", [result])]
    assert card.state == "info" and card.result is None
    card.close()


@pytest.mark.parametrize("next_state", ["working", "error", "info"])
def test_toast_state_change_clears_old_result(qapp, tmp_path, next_state):
    card = toasts.ToastCard(toasts.ToastStack())
    card.done(Result("Audio converted", "track.mp3", [tmp_path / "track.mp3"], good="75% smaller"))
    card.grab()
    assert card.good and card.buttons
    getattr(card, next_state)("New operation", "New detail")
    assert card.result is None and card.thumb is None and card.good == "" and card.buttons == []
    card.close()
