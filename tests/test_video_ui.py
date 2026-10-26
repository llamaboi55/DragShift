import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from dragshift import video
from dragshift.app import DragShiftApp
from dragshift.video_tools import DatamoshWindow, VideoProgressWindow, VideoToolWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def wait_for(qapp, predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "Qt background operation did not complete"


@pytest.fixture(scope="module")
def clip(tmp_path_factory, media_engines):
    import subprocess
    p = tmp_path_factory.mktemp("ui") / "clip.mp4"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "testsrc2=size=160x120:rate=24:duration=2", "-c:v", "libx264", "-y", str(p)],
        check=True, creationflags=video.CREATION_FLAGS)
    return p


def test_video_rings(qapp):
    rings = DragShiftApp.build_rings(None, ["a.mp4", "b.mov"])
    assert {i.key for i in rings["Convert"]} >= {"convert:WEBM", "convert:GIF", "convert:MP3"}
    assert {i.key for i in rings["Tools"]} == {"tool:" + t for t in
        ["Trim", "Compress", "Resize", "Edit", "Mute", "Frame", "Distort", "Metadata", "Datamosh", "Audio", "Pack"]}


@pytest.mark.parametrize("mode", ["Trim", "Compress", "Resize", "Edit", "Mute", "Frame", "Metadata"])
def test_video_tools_load(qapp, clip, mode):
    w = VideoToolWindow([str(clip)], lambda *args: None, mode)
    wait_for(qapp, lambda: w.info is not None or bool(w.error.text()))
    assert w.info and w.primary_btn.isEnabled(), w.error.text()
    if mode == "Trim":
        w.start_time.setValue(1.5)
        w.end_time.setValue(.5)
        assert not w.validate()
        assert "start" in w.error.text().lower()
    w.close()
    qapp.processEvents()


def test_datamosh_load_and_preview(qapp, clip):
    w = DatamoshWindow([str(clip), str(clip)], lambda *args: None)
    wait_for(qapp, lambda: w.first_info is not None and w.second_info is not None)
    assert w.preset.currentText() == "Melt"
    assert w.primary_btn.isEnabled()
    w.duration.setValue(2)
    w.head.setValue(.5)
    assert not w.validate()
    w.close()
    qapp.processEvents()


def test_progress_and_cancel(qapp, clip):
    results = []
    w = VideoProgressWindow("convert:MP4", [str(clip)], lambda: video.convert_video(clip, "MP4"), results.append)
    w.start()
    wait_for(qapp, lambda: bool(results))
    assert results[0].outs[0].is_file()
    def cancellable():
        while True:
            video.check_cancel()
            time.sleep(.02)
    other = VideoProgressWindow("tool:Edit", [str(clip)], cancellable, results.append)
    other.start()
    other.cancel_work()
    wait_for(qapp, lambda: not other.busy)
    assert len(results) == 1 and "cancel" in other.detail.text().lower()
    other.close()
    qapp.processEvents()


def test_close_while_glass_is_rendering(qapp, monkeypatch):
    import sys
    import shiboken6
    from PIL import Image
    from PySide6.QtCore import QRect
    from PySide6.QtWidgets import QWidget
    from dragshift import glass
    from dragshift.util import ALIVE
    errors = []
    monkeypatch.setattr(sys, "excepthook", lambda *args: errors.append(args))
    monkeypatch.setattr(glass, "grab_screen", lambda *args: Image.new("RGB", (120, 80), "blue"))
    widget = QWidget()
    backing = glass.LiquidBacking(widget, 18)
    backing.capture_at(QRect(0, 0, 120, 80))
    shiboken6.delete(widget)
    wait_for(qapp, lambda: not ALIVE)
    assert not errors
