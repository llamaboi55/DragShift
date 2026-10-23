"""Real codec integration tests; tiny generated fixtures, no user media."""
import hashlib
import subprocess
import threading
from pathlib import Path

import pytest

from dragshift import convert, video
from dragshift.util import CONFIG, UserError, category


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    try:
        video.executable("ffmpeg")
        video.executable("ffprobe")
    except UserError as e:
        pytest.skip(str(e))
    p = tmp_path_factory.mktemp("video") / "Тест 'video'.mp4"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=24:duration=2", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-metadata", "comment=private", "-y", str(p)], check=True,
                   creationflags=video.CREATION_FLAGS)
    return p


def test_categories_and_targets():
    for ext in ["MP4", "mkv", "mov", "avi", "webm", "mts", "m2ts", "wmv", "flv", "3gp"]:
        assert category("file." + ext) == "video"
    assert category("a.gif") == "image"
    assert set(convert.targets_for("video", ".mov")) == set(video.TARGETS) - {"MOV"}


def test_probe(clip):
    i = video.probe(clip)
    assert (i.width, i.height) == (160, 120)
    assert i.has_audio and abs(i.duration - 2) < .15 and i.fps == 24


@pytest.mark.parametrize("target", ["MP4", "MKV", "MOV", "WEBM", "AVI", "GIF", "MP3", "WAV"])
def test_conversion(clip, target):
    before = hashlib.sha256(clip.read_bytes()).digest()
    out = convert.convert(clip, "video", target)[0]
    assert out != clip and out.stat().st_size > 100
    assert out.suffix == "." + target.lower()
    if target not in ("GIF", "MP3", "WAV"):
        info = video.probe(out)
        assert info.has_audio and abs(info.duration - 2) < .3
    elif target == "GIF":
        from PIL import Image
        with Image.open(out) as im:
            assert im.is_animated and im.n_frames > 10
    assert hashlib.sha256(clip.read_bytes()).digest() == before


def test_transforms_and_snapshot(clip):
    out = video.transform(clip, start=.25, end=1.75, max_side=80, rotation=90, speed=2)[0]
    info = video.probe(out)
    assert (info.width, info.height) == (60, 80)
    assert info.has_audio and abs(info.duration - .75) < .2
    out = video.transform(clip, crop_ratio=1, mute=True, flip=True, brightness=.1,
                          contrast=1.2, saturation=1.5)[0]
    info = video.probe(out)
    assert info.width == info.height and not info.has_audio
    from PIL import Image
    with Image.open(video.snapshot(clip, .5)[0]) as im:
        assert im.size == (160, 120)


def test_silent_and_metadata(clip):
    silent = video.transform(clip, mute=True)[0]
    assert video.probe(silent).has_audio is False
    assert video.probe(video.convert_video(silent, "WEBM")[0]).has_audio is False
    with pytest.raises(UserError, match="audio"):
        video.convert_video(silent, "MP3")
    cleaned = video.transform(clip, strip_metadata=True)[0]
    assert "private" not in str(video.probe(cleaned).tags)


@pytest.mark.parametrize("options", [{"start": 2}, {"start": 1, "end": .5},
    {"end": 10}, {"speed": 0}, {"max_side": 1}, {"rotation": 45}, {"start": float("nan")}])
def test_validation(clip, options):
    with pytest.raises(UserError):
        video.transform(clip, **options)


def test_cancel_cleanup_and_output_folder(clip, tmp_path):
    cancel = threading.Event()
    cancel.set()
    with video.job_context(video.Control(cancel=cancel)):
        with pytest.raises(video.Cancelled):
            video.convert_video(clip, "MP4")
    assert not list(clip.parent.glob(".~video-*"))
    CONFIG["output"] = "folder"
    try:
        a = video.convert_video(clip, "MP4")[0]
        b = video.convert_video(clip, "MP4")[0]
        assert a.parent.name == "DragShift" and a != b
    finally:
        CONFIG["output"] = "next"
    bad = tmp_path / "broken.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(UserError):
        video.probe(bad)


def test_live_process_cancellation(clip):
    cancel = threading.Event()
    def update(value, text):
        if value > .01:
            cancel.set()
    before = set(clip.parent.iterdir())
    with video.job_context(video.Control(cancel=cancel, progress=update)):
        with pytest.raises(video.Cancelled):
            with video.export_path(clip, "Cancelled") as (tmp, _):
                args = video.ffmpeg_args() + ["-re", "-f", "lavfi", "-i",
                    "testsrc2=size=160x120:rate=24:duration=15", "-c:v", "libx264", "-threads", "1", str(tmp)]
                video.run_process(args, 15)
    assert set(clip.parent.iterdir()) == before


def test_batch_failure_rolls_back(clip, tmp_path):
    before = set(clip.parent.iterdir())
    missing = tmp_path / "missing.mp4"
    with pytest.raises(UserError):
        video.batch([clip, missing], lambda p: video.convert_video(p, "MP4"))
    assert set(clip.parent.iterdir()) == before


def test_missing_engine(monkeypatch):
    monkeypatch.setattr(video.shutil, "which", lambda _: None)
    with pytest.raises(UserError, match="not found"):
        video.executable("no-such-engine")


@pytest.fixture(params=[0, 90], ids=["anamorphic", "rotated-anamorphic"])
def anamorphic_clip(clip, tmp_path, request, rotation_options):
    encoded = tmp_path / "wide-pixels.mp4"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(clip),
        "-vf", "setsar=2/1", "-c:v", "libx264", "-c:a", "copy", "-y", str(encoded)],
        check=True, creationflags=video.CREATION_FLAGS)
    if request.param:
        rotated = tmp_path / "rotated-wide-pixels.mp4"
        before_input, after_input = rotation_options
        subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", *before_input,
            "-i", str(encoded), "-c", "copy", *after_input, "-y", str(rotated)],
            check=True, creationflags=video.CREATION_FLAGS)
        encoded = rotated
    return encoded, (3 / 8 if request.param else 8 / 3)


def test_anamorphic_preview_and_probe(anamorphic_clip):
    import io
    from PIL import Image
    src, aspect = anamorphic_clip
    with Image.open(io.BytesIO(video.preview_bytes(src))) as image:
        assert image.width / image.height == pytest.approx(aspect, abs=.02)
    info = video.probe(src)
    assert info.display_width / info.display_height == pytest.approx(aspect)


def test_anamorphic_exports_preserve_display_geometry(anamorphic_clip):
    from PIL import Image
    src, aspect = anamorphic_clip
    for target in ["MP4", "GIF"]:
        out = video.convert_video(src, target)[0]
        if target == "GIF":
            with Image.open(out) as image:
                assert image.width / image.height == pytest.approx(aspect, abs=.02)
        else:
            info = video.probe(out)
            assert info.width / info.height == pytest.approx(aspect, abs=.02)
    resized = video.probe(video.transform(src, max_side=160)[0])
    assert max(resized.width, resized.height) <= 160
    assert resized.width / resized.height == pytest.approx(aspect, abs=.02)
    square = video.probe(video.transform(src, crop_ratio=1)[0])
    assert square.display_width / square.display_height == pytest.approx(1)
    with Image.open(video.snapshot(src, .5)[0]) as image:
        assert image.width / image.height == pytest.approx(aspect, abs=.02)


def test_process_returns_successful_stderr():
    import sys
    data = video.run_process([sys.executable, "-c", "import sys; sys.stderr.write('measured audio')"],
                             parse_progress=False)
    assert data == b"measured audio"


def test_process_can_set_working_directory(tmp_path):
    import sys
    data = video.run_process([sys.executable, "-c", "import os,sys; sys.stderr.write(os.getcwd())"],
                             parse_progress=False, cwd=tmp_path)
    assert Path(data.decode()).resolve() == tmp_path.resolve()


@pytest.mark.parametrize("output_mode", ["next", "folder"])
def test_batch_directory_failure_rolls_back(tmp_path, monkeypatch, output_mode):
    monkeypatch.setitem(CONFIG, "output", output_mode)
    a, b = tmp_path / "a.7z", tmp_path / "b.7z"
    a.write_bytes(b"original A")
    b.write_bytes(b"original B")
    out = (tmp_path / "DragShift" if output_mode == "folder" else tmp_path) / "extracted"
    def extract(path):
        if path == b:
            raise UserError("archive B failed")
        out.mkdir(parents=True)
        (out / "nested.txt").write_text("new extracted file")
        return [out]
    with pytest.raises(UserError, match="archive B failed"):
        video.batch([a, b], extract)
    assert not out.exists()
    assert a.read_bytes() == b"original A" and b.read_bytes() == b"original B"


def test_batch_cleanup_preserves_original_error(tmp_path, monkeypatch):
    a, b = tmp_path / "a.mp4", tmp_path / "b.mp4"
    out = tmp_path / "new.mp4"
    def convert_one(path):
        if path == b:
            raise UserError("conversion B failed")
        out.write_bytes(b"new result")
        return [out]
    def locked(*args, **kwargs):
        raise OSError("result is locked")
    monkeypatch.setattr(Path, "unlink", locked)
    with pytest.raises(UserError, match="conversion B failed"):
        video.batch([a, b], convert_one)
    assert out.read_bytes() == b"new result"


def test_batch_never_removes_input_ancestor(tmp_path):
    folder = tmp_path / "originals"
    folder.mkdir()
    a, b = folder / "a.7z", tmp_path / "b.7z"
    a.write_bytes(b"original A")
    b.write_bytes(b"original B")
    def accidental_output(path):
        if path == b:
            raise UserError("archive B failed")
        return [folder]
    with pytest.raises(UserError, match="archive B failed"):
        video.batch([a, b], accidental_output)
    assert a.read_bytes() == b"original A" and b.read_bytes() == b"original B"


def test_batch_cleanup_refuses_directory_outside_result_parent(tmp_path):
    a, b = tmp_path / "a.7z", tmp_path / "b.7z"
    outside = tmp_path / "other" / "keep"
    outside.mkdir(parents=True)
    original = outside / "original.txt"
    original.write_text("keep this")
    def accidental_output(path):
        if path == b:
            raise UserError("archive B failed")
        return [outside]
    with pytest.raises(UserError, match="archive B failed"):
        video.batch([a, b], accidental_output)
    assert original.read_text() == "keep this"

