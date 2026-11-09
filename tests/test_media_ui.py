"""The audio/video tool windows and their wiring in the app (offscreen Qt, needs FFmpeg)."""
import os
import subprocess
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PySide6")
from PySide6.QtWidgets import QApplication

from dragshift import media, media_tools, report
from dragshift.app import DragShiftApp

pytestmark = pytest.mark.skipif(not media.available(), reason="FFmpeg not available")


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def ff(*args):
    subprocess.run([media.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def mp3(tmp_path):
    p = tmp_path / "Song.mp3"
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=10", "-c:a", "libmp3lame", "-b:a", "256k",
       "-metadata", "title=Song", str(p))
    return p


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "Clip.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=8", "-f", "lavfi", "-i", "sine=duration=8",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac", "-metadata", "title=Holiday", str(p))
    return p


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, action, srcs, outs, extra=""):
        self.calls.append((action, srcs, outs, extra))


def test_time_helpers():
    assert media_tools.parse_time("83.5") == 83.5 and media_tools.parse_time("1:23.5") == 83.5
    assert media_tools.parse_time("1:02:03") == 3723 and media_tools.parse_time("abc") is None
    assert media_tools.parse_time("-3") is None and media_tools.parse_time("1:2:3:4") is None
    assert media_tools.fmt_time(83.5) == "1:23.5" and media_tools.fmt_time(3723) == "1:02:03.0"


def test_menu_rings(mp3, video):
    rings = DragShiftApp.build_rings(None, [str(mp3)])
    assert [i.label for i in rings["Tools"]] == ["Compress", "Trim", "Edit", "Metadata"]
    assert "MP3" not in [i.label for i in rings["Convert"]] and "WAV" in [i.label for i in rings["Convert"]]
    assert [i.label for i in DragShiftApp.build_rings(None, [str(video), str(video)])["Tools"]] == \
        ["Compress", "Trim", "Edit", "Merge", "Metadata"]


def test_compress_window_audio(qapp, mp3):
    rec = Recorder()
    w = media_tools.MediaCompressWindow([str(mp3)], rec, "audio")
    w.preset.button(0).click()                       # Tiny, 64k
    assert "about" in w.est.text()
    w.save()
    action, srcs, outs, _ = rec.calls[0]
    assert action == "tool:Compress" and outs[0].stat().st_size < mp3.stat().st_size / 2


def test_compress_window_target_size_and_mono(qapp, mp3):
    rec = Recorder()
    w = media_tools.MediaCompressWindow([str(mp3)], rec, "audio")
    w.mode.button(1).click()
    assert w.target.value() == pytest.approx(mp3.stat().st_size / 1048576 / 2, abs=0.02)   # starts at half
    w.target.setValue(5.0)                           # bigger than the 0.3 MB file: refused
    assert not w.primary_btn.isEnabled() and "not smaller" in w.note.text()
    w.target.setValue(0.1)
    assert w.primary_btn.isEnabled() and "about 102" in w.est.text()
    w.save()
    assert rec.calls[0][2][0].stat().st_size < 0.1 * 1048576 * 1.15


def test_compress_window_video_with_resolution(qapp, video):
    rec = Recorder()
    w = media_tools.MediaCompressWindow([str(video)], rec, "video")
    w.preset.button(2).click()                       # Small
    w.res.button(3).click()                          # 480p: never enlarges a 180p clip
    w.save()
    out = rec.calls[0][2][0]
    info = media.probe(out)
    assert info.height == 180 and info.has_audio and out.suffix == ".mp4"


def test_trim_window(qapp, mp3, video):
    rec = Recorder()
    w = media_tools.TrimWindow(str(mp3), rec, "audio")
    assert w.dur == pytest.approx(10.0, abs=0.2) and w.primary_btn.isEnabled()
    w.s_start.setValue(20)
    w.s_end.setValue(70)
    assert w.out.text() == "Keeps 5.0 of 0:10.0".replace("5.0", "0:05.0")
    w.e_start.setText("0:03")
    w.e_start.editingFinished.emit()
    assert w.s_start.value() == 30
    w.s_end.setValue(25)                             # end before start: can't save
    assert not w.primary_btn.isEnabled() and "after the start" in w.msg.text()
    w.s_end.setValue(70)
    w.save()
    assert media.probe(rec.calls[0][2][0]).duration == pytest.approx(4.0, abs=0.2)

    w = media_tools.TrimWindow(str(video), rec, "video")
    assert w.t_start.pixmap() is not None and not w.t_start.pixmap().isNull()   # thumbnails drawn
    w.s_end.setValue(30)
    w.fast.setChecked(True)
    w.save()
    assert rec.calls[1][2][0].exists()


def test_edit_window_audio(qapp, mp3):
    rec = Recorder()
    w = media_tools.MediaEditWindow(str(mp3), rec, "audio")
    assert not w.primary_btn.isEnabled()             # nothing changed yet
    w.speed.button(5).click()                        # 2x
    assert w.primary_btn.isEnabled() and "0:10.0 → 0:05.0" in w.out.text()
    w.fades[0].setValue(1.0)
    w.save()
    assert media.probe(rec.calls[0][2][0]).duration == pytest.approx(5.0, abs=0.3)
    w2 = media_tools.MediaEditWindow(str(mp3), rec, "audio")
    w2.norm.setChecked(True)
    assert not w2.gain.isEnabled()                   # loudness evening replaces the manual volume
    w2._reset()
    assert not w2.primary_btn.isEnabled()


def test_edit_window_video(qapp, video):
    rec = Recorder()
    w = media_tools.MediaEditWindow(str(video), rec, "video")
    assert not w.primary_btn.isEnabled() and w.mute.isEnabled()
    w.rot.button(1).click()                          # 90 clockwise
    w.flip_h.setChecked(True)
    assert "180×320" in w.out.text()
    w.save()
    i = media.probe(rec.calls[0][2][0])
    assert (i.width, i.height) == (180, 320) and i.has_audio
    w = media_tools.MediaEditWindow(str(video), rec, "video")
    w.mute.setChecked(True)
    assert not w.gain.isEnabled()
    w.save()
    assert not media.probe(rec.calls[1][2][0]).has_audio


def fake_app(rec):
    """Just enough of DragShiftApp for run_media_tool: jobs run inline."""
    ns = types.SimpleNamespace(windows=[], errors=[], notify=rec)
    ns.open_window = lambda w: ns.windows.append(w)
    ns.toasts = types.SimpleNamespace(error=lambda *a: ns.errors.append(a))
    ns.submit = lambda action, srcs, fn, working="", extra="": rec(action, srcs, fn(), extra)
    return ns


def test_merge_and_metadata_actions(qapp, video, mp3, tmp_path):
    rec = Recorder()
    app = fake_app(rec)
    DragShiftApp.run_media_tool(app, "Merge", [str(video), str(video)], "video")
    action, srcs, outs, _ = rec.calls[0]
    assert action == "tool:Merge" and media.probe(outs[0]).duration == pytest.approx(16.0, abs=0.6)
    r = report.summarize(action, srcs, outs)
    assert r.title == "Joined 2 files" and "0:16" in r.detail

    DragShiftApp.run_media_tool(app, "Metadata", [str(video)], "video")
    action, srcs, outs, extra = rec.calls[1]
    assert "1 tag removed: title" in extra and not media.read_tags(outs[0])
    assert report.summarize(action, srcs, outs, extra).title == "Metadata removed"

    DragShiftApp.run_media_tool(app, "Trim", [str(mp3)], "audio")
    assert isinstance(app.windows[0], media_tools.TrimWindow)
    DragShiftApp.run_media_tool(app, "Merge", [str(mp3)], "audio")   # one file can't be joined
    assert len(app.errors) == 1 and "at least two" in app.errors[0][1]


def test_summaries(qapp, video, mp3):
    t = media.trim(video, 1, 4)
    r = report.summarize("tool:Trim", [video], [t])
    assert r.title == "Trimmed" and "0:08 → 0:03" in r.detail
    m = media.edit_video(video, mute=True)
    r = report.summarize("tool:Edit", [video], [m])
    assert r.title == "Edited" and "no sound" in r.detail
    c = media.compress_audio(mp3, kbps=64)
    assert "→" in report.summarize("tool:Compress", [mp3], [c]).detail
