import subprocess
import time

import pytest
from PySide6.QtWidgets import QApplication

from dragshift import audio, convert, video
from dragshift.app import DragShiftApp
from dragshift.audio_tools import AudioToolWindow
from dragshift.util import category


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture(scope="module")
def sound(tmp_path_factory, media_engines):
    path = tmp_path_factory.mktemp("audio-ui") / "Тест.wav"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=440:duration=2", "-ar", "44100", "-y", str(path)], check=True,
        creationflags=video.CREATION_FLAGS)
    return path


def wait_for(qapp, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_audio_categories_and_ring(qapp):
    for ext in ("MP3", "wav", "flac", "m4a", "aac", "aiff", "opus", "wma", "m4b"):
        assert category("audio." + ext) == "audio"
    rings = DragShiftApp.build_rings(None, ["a.wav"])
    assert {x.label for x in rings["Convert"]} >= {"MP3", "FLAC", "M4A", "OPUS"}
    assert {x.label for x in rings["Tools"]} >= {"Trim", "Edit", "Normalize", "Fade", "Reverse"}
    assert category("a.rar") == "archive"
    assert "ZIP" in convert.targets_for("archive", ".rar")


@pytest.mark.parametrize("mode", ["Trim", "Edit", "Normalize", "Fade", "Compress", "Reverse", "Metadata", "Extract"])
def test_audio_editors(qapp, sound, mode):
    window = AudioToolWindow([str(sound)], lambda *_: None, mode)
    wait_for(qapp, lambda: window.info is not None or bool(window.error.text()))
    assert window.info is not None and window.primary_btn.isEnabled(), window.error.text()
    if mode == "Edit":
        window.target.setCurrentText("FLAC")
        window.end_time.setValue(1)
        window.speed.setValue(2)
        window.fade_in.setValue(.5)
        window.fade_out.setValue(.5)
        assert not window.validate()
        window.fade_in.setValue(.1)
        window.fade_out.setValue(.1)
        assert window.validate()
        captured = []
        window.submit = lambda action, srcs, fn, *extra: captured.append((action, fn))
        window.save()
        assert audio.probe(captured[0][1]()[0]).codec == "flac"
    else:
        assert window.validate()
        window.close()
    qapp.processEvents()


def test_audio_batch_edit_keeps_full_lengths_and_has_no_implicit_fades(qapp, sound, tmp_path):
    longer = tmp_path / "longer.wav"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
        "sine=frequency=660:duration=3", "-y", str(longer)], check=True, creationflags=video.CREATION_FLAGS)
    window = AudioToolWindow([str(sound), str(longer)], lambda *_: None, "Edit")
    wait_for(qapp, lambda: window.info is not None)
    opts = window.options()
    assert opts["end"] is None
    assert opts["fade_in"] == opts["fade_out"] == 0
    window.target.setCurrentText("FLAC")
    captured = []
    window.submit = lambda action, srcs, fn, *extra: captured.append(fn)
    window.save()
    outs = captured[0]()
    assert [round(audio.probe(p).duration) for p in outs] == [2, 3]
    qapp.processEvents()
