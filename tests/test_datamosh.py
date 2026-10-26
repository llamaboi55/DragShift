import subprocess
from pathlib import Path

import pytest

from dragshift import datamosh, video
from dragshift.util import UserError


def test_only_transition_keyframes_are_removed(tmp_path):
    marker = b"\x00\x00\x01\xb6"
    packets = [marker + bytes([kind << 6]) + b"content" for kind in [0, 1, 0, 1, 0, 1]]
    src, dst = tmp_path / "source.m4v", tmp_path / "out.m4v"
    src.write_bytes(b"HEADER" + b"".join(packets))
    removed, count = datamosh.remove_keyframes(src, dst, 2, 4)
    assert removed == 1 and count == 6
    assert dst.read_bytes() == b"HEADER" + b"".join(packets[:2] + packets[3:])


@pytest.fixture(scope="module")
def pair(tmp_path_factory):
    try:
        for engine in ("ffmpeg", "ffprobe", "ffgac", "ffedit"):
            video.executable(engine)
    except UserError as e:
        pytest.skip(str(e))
    folder = tmp_path_factory.mktemp("mosh")
    a, b = folder / "Начало A.mp4", folder / "Конец B.mp4"
    for p, source, freq in [(a, "testsrc2=size=160x120:rate=24:duration=3", 330),
                             (b, "testsrc=size=192x108:rate=30:duration=3", 660)]:
        subprocess.run([video.executable("ffmpeg"), "-v", "error", "-f", "lavfi", "-i", source,
                        "-f", "lavfi", "-i", f"sine=frequency={freq}:duration=3", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", "-y", str(p)], check=True,
                       creationflags=video.CREATION_FLAGS)
    return a, b


@pytest.mark.parametrize("preset", ["Melt"])
def test_real_ffglitch_transition(pair, preset):
    a, b = pair
    before = a.read_bytes(), b.read_bytes()
    result = datamosh.transition(a, b, tail=1, head=2, duration=.75,
                                 strength=70, preset=preset, max_side=160, audio=True)[0]
    info = video.probe(result)
    assert (info.width, info.height) == (160, 120)
    assert abs(info.duration - 3) < .15 and info.has_audio
    assert (a.read_bytes(), b.read_bytes()) == before
    from PIL import Image, ImageChops, ImageStat
    import io
    # The bridge differs from clean B. After recovery, B's recognizable image returns.
    bridge = Image.open(io.BytesIO(video.preview_bytes(result, 1.4))).convert("RGB")
    recovered = Image.open(io.BytesIO(video.preview_bytes(result, 2.5))).convert("RGB")
    assert sum(ImageStat.Stat(ImageChops.difference(bridge, recovered)).mean) / 3 > 12


def test_range_errors_and_silent_audio(pair):
    a, b = pair
    with pytest.raises(UserError):
        datamosh.transition(a, b, tail=4)
    with pytest.raises(UserError):
        datamosh.transition(a, b, tail=1, head=1, duration=2)
    silent = video.transform(b, mute=True)[0]
    out = datamosh.transition(a, silent, tail=.5, head=1.5, duration=.5, max_side=160)[0]
    assert video.probe(out).has_audio


def test_bridge_is_glitched_and_then_recovers(pair):
    import io
    from PIL import Image, ImageChops, ImageStat
    a, b = pair
    out = datamosh.transition(a, b, tail=1, head=2, duration=.75, max_side=160,
                              preset="Melt", strength=80)[0]
    def frame(path, at):
        return Image.open(io.BytesIO(video.preview_bytes(path, at, 160))).convert("RGB")
    def difference(x, y):
        return sum(ImageStat.Stat(ImageChops.difference(x, y)).mean) / 3
    # B is letterboxed from 16:9 into A's 4:3 canvas. One bridge I-frame was dropped.
    bridge = frame(out, 1.3).crop((0, 15, 160, 105))
    recovered = frame(out, 2.5).crop((0, 15, 160, 105))
    assert difference(bridge, frame(b, .3 + 1/30)) > 20
    assert difference(recovered, frame(b, 1.5 + 1/30)) < 15


def test_datamosh_cancellation_removes_intermediates(pair):
    import threading
    a, b = pair
    before = set(a.parent.iterdir())
    cancel = threading.Event()
    def update(value, text):
        if value >= .4:
            cancel.set()
    with video.job_context(video.Control(cancel=cancel, progress=update)):
        with pytest.raises(video.Cancelled):
            datamosh.transition(a, b, tail=1, head=2, duration=.75, max_side=160)
    assert set(a.parent.iterdir()) == before


@pytest.mark.parametrize("rotation, aspect", [(0, 8 / 3), (90, 3 / 8)])
def test_anamorphic_canvas_preserves_display_aspect(pair, tmp_path, rotation, aspect, rotation_options):
    a, b = pair
    encoded = tmp_path / "anamorphic.mp4"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", "-i", str(a),
        "-vf", "setsar=2/1", "-c:v", "libx264", "-c:a", "copy", "-y", str(encoded)],
        check=True, creationflags=video.CREATION_FLAGS)
    if rotation:
        rotated = tmp_path / "rotated.mp4"
        before_input, after_input = rotation_options
        subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", *before_input,
            "-i", str(encoded), "-c", "copy", *after_input, "-y", str(rotated)],
            check=True, creationflags=video.CREATION_FLAGS)
        encoded = rotated
    out = video.probe(datamosh.transition(encoded, b, tail=.5, head=1.5, duration=.5, max_side=160)[0])
    assert max(out.width, out.height) <= 160
    assert out.width / out.height == pytest.approx(aspect, abs=.02)


def test_anamorphic_second_clip_is_letterboxed(pair, tmp_path):
    import io
    from PIL import Image
    a, _ = pair
    b = tmp_path / "wide-red.mp4"
    subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", "-f", "lavfi", "-i",
        "color=c=red:s=160x120:r=30:d=2", "-vf", "setsar=2/1", "-c:v", "libx264", "-y", str(b)],
        check=True, creationflags=video.CREATION_FLAGS)
    out = datamosh.transition(a, b, tail=.5, head=1.5, duration=.5, max_side=160)[0]
    with Image.open(io.BytesIO(video.preview_bytes(out, 1.5))) as image:
        image = image.convert("RGB")
        red_rows = [y for y in range(image.height) if image.getpixel((image.width // 2, y))[0] > 180]
        assert len(red_rows) == pytest.approx(60, abs=4)


def test_melt_is_the_only_preset(pair):
    assert datamosh.PRESETS == ['Melt']
    with pytest.raises(UserError):
        datamosh.transition(*pair, preset='Psychedelic')


def test_classic_melt_never_blends_clean_video_over_effect(pair, monkeypatch):
    commands=[]
    original=video.run_process
    def run(args,*positional,**kwargs):
        commands.append(args)
        return original(args,*positional,**kwargs)
    monkeypatch.setattr(video,'run_process',run)
    result=datamosh.transition(*pair,tail=1,head=2,duration=.75,max_side=160)[0]
    assert video.probe(result).duration==pytest.approx(3,abs=.15)
    assert len(commands)==3
    assert not any('blend=' in str(arg) for args in commands for arg in args)
    # The playable export decodes one moshed video and the two original audio inputs.
    assert commands[-1].count('-i')==3
