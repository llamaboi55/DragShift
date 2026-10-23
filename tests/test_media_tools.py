"""Compress / Trim / Edit / Merge / strip-metadata for audio and video (needs FFmpeg)."""
import subprocess

import pytest

from dragshift import media
from dragshift.util import UserError

pytestmark = pytest.mark.skipif(not media.available(), reason="FFmpeg not available")


def ff(*args):
    subprocess.run([media.find_ffmpeg(), "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


@pytest.fixture
def mp3(tmp_path):
    p = tmp_path / "Song.mp3"
    ff("-f", "lavfi", "-i", "sine=frequency=440:duration=6", "-c:a", "libmp3lame", "-b:a", "256k",
       "-metadata", "title=My Song", "-metadata", "artist=Me", str(p))
    return p


@pytest.fixture
def wav(tmp_path):
    p = tmp_path / "Tone.wav"
    ff("-f", "lavfi", "-i", "sine=frequency=330:duration=4", str(p))
    return p


@pytest.fixture
def video(tmp_path):
    p = tmp_path / "Clip.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=320x180:rate=25:duration=6", "-f", "lavfi", "-i",
       "sine=frequency=440:duration=6", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", "25", "-c:a", "aac",
       "-metadata", "title=Holiday", "-metadata", "location=+38.7-009.1/", str(p))
    return p


def vol(p):
    r = subprocess.run([media.find_ffmpeg(), "-hide_banner", "-i", str(p), "-vn", "-af", "volumedetect",
                        "-f", "null", "-"], capture_output=True, text=True).stderr
    import re
    return float(re.search(r"mean_volume: (\S+) dB", r).group(1))


def test_probe_reads_size_fps_and_tags(video, mp3):
    i = media.probe(video)
    assert (i.width, i.height) == (320, 180) and round(i.fps) == 25 and i.tags["title"] == "Holiday"
    assert media.read_tags(mp3) == {"title": "My Song", "artist": "Me"}


# ---------------------------------------------------------------- compress
def test_audio_compress_by_bitrate(mp3):
    out = media.compress_audio(mp3, kbps=64)
    assert out.name == "Song Compressed.mp3" and out.stat().st_size < mp3.stat().st_size / 2
    assert media.read_tags(out).get("title") == "My Song"          # tags survive


def test_audio_compress_target_size_and_lossless_source(wav):
    out = media.compress_audio(wav, target_bytes=20_000)
    assert out.suffix == ".mp3" and 8_000 < out.stat().st_size < 30_000   # WAV can't shrink as WAV
    assert media.compress_audio(wav, kbps=96, mono=True).exists()


def test_video_compress(video):
    out = media.compress_video(video, "Small", height=90)
    i = media.probe(out)
    assert i.has_audio and i.height == 90 and out.suffix == ".mp4"
    out2 = media.compress_video(video, target_bytes=60_000)
    assert media.probe(out2).has_video
    with pytest.raises(UserError, match="too small"):
        media.compress_video(video, target_bytes=500)


def test_compress_never_upscales(video):
    assert media.probe(media.compress_video(video, "Balanced", height=1080)).height == 180


# ---------------------------------------------------------------- trim
def test_audio_trim(mp3):
    out = media.trim(mp3, 1.0, 3.5)
    assert out.name == "Song Trimmed.mp3" and media.probe(out).duration == pytest.approx(2.5, abs=0.15)
    assert media.read_tags(out).get("title") == "My Song"
    assert media.probe(media.trim(mp3, 2.0, None)).duration == pytest.approx(4.0, abs=0.15)


def test_video_trim_exact_and_fast(video):
    exact = media.trim(video, 1.0, 3.0)
    i = media.probe(exact)
    assert i.duration == pytest.approx(2.0, abs=0.2) and i.has_audio
    fast = media.trim(video, 1.0, 4.0, exact=False)
    assert media.probe(fast).duration == pytest.approx(3.0, abs=1.2)   # snaps to keyframes


def test_trim_validation(mp3):
    with pytest.raises(UserError, match="after the start"):
        media.trim(mp3, 3.0, 2.0)
    with pytest.raises(UserError, match="past the end"):
        media.trim(mp3, 30.0, None)


# ---------------------------------------------------------------- edit
def test_audio_edit_volume_speed_fade(wav):
    quiet = media.edit_audio(wav, gain_db=-12)
    assert vol(quiet) == pytest.approx(vol(wav) - 12, abs=1.0)
    fast = media.edit_audio(wav, speed=2.0)
    assert media.probe(fast).duration == pytest.approx(2.0, abs=0.2)
    slow = media.edit_audio(wav, speed=0.25)           # needs two atempo stages
    assert media.probe(slow).duration == pytest.approx(16.0, abs=0.5)
    faded = media.edit_audio(wav, fade_in=1.0, fade_out=1.0)
    assert vol(faded) < vol(wav)
    assert media.edit_audio(wav, normalize=True).exists()
    with pytest.raises(UserError, match="Nothing to change"):
        media.edit_audio(wav)


def test_video_edit_rotate_flip_speed(video):
    rot = media.edit_video(video, rotate=90)
    i = media.probe(rot)
    assert (i.width, i.height) == (180, 320) and i.has_audio
    assert media.probe(media.edit_video(video, flip_h=True, flip_v=True)).width == 320
    fast = media.edit_video(video, speed=2.0)
    assert media.probe(fast).duration == pytest.approx(3.0, abs=0.3)
    small = media.edit_video(video, height=90)
    assert media.probe(small).height == 90


def test_video_mute_and_volume_dont_reencode_video(video):
    muted = media.edit_video(video, mute=True)
    i = media.probe(muted)
    assert i.has_video and not i.has_audio
    loud = media.edit_video(video, gain_db=6)
    assert media.probe(loud).has_audio and vol(loud) > vol(video) + 3
    with pytest.raises(UserError, match="Nothing to change"):
        media.edit_video(video)


# ---------------------------------------------------------------- merge
def test_merge_audio_of_different_formats(mp3, wav):
    out = media.merge([mp3, wav], "audio")
    assert out.name == "Song Merged.mp3" and media.probe(out).duration == pytest.approx(10.0, abs=0.4)


def test_merge_videos_with_different_sizes_and_missing_sound(video, tmp_path):
    other = tmp_path / "Other.mp4"
    ff("-f", "lavfi", "-i", "testsrc=size=160x120:rate=15:duration=2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
       str(other))                                    # smaller, slower, and no audio at all
    out = media.merge([video, other], "video")
    i = media.probe(out)
    assert (i.width, i.height) == (320, 180) and i.has_audio
    assert i.duration == pytest.approx(8.0, abs=0.5)
    with pytest.raises(UserError, match="at least two"):
        media.merge([video], "video")


# ---------------------------------------------------------------- metadata
def test_strip_metadata(video):
    out, removed = media.strip_metadata(video)
    assert "title" in removed and "location" in removed
    assert not media.read_tags(out) and media.probe(out).has_video and media.probe(out).has_audio


def test_originals_are_never_touched(mp3, video):
    before = (mp3.read_bytes(), video.read_bytes())
    media.compress_audio(mp3, kbps=64)
    media.trim(video, 0, 2)
    media.edit_video(video, rotate=180)
    assert (mp3.read_bytes(), video.read_bytes()) == before


def test_unusual_extension_still_works(video, tmp_path):
    odd = tmp_path / "Download.mpg4"                  # a real MP4 with a made-up extension
    odd.write_bytes(video.read_bytes())
    fast = media.trim(odd, 1, 4, exact=False)
    assert fast.suffix == ".mp4" and media.probe(fast).has_video
    assert media.edit_video(odd, gain_db=3).suffix == ".mp4"
    out, _ = media.strip_metadata(odd)
    assert out.suffix == ".mp4" and not media.read_tags(out)
    audio = tmp_path / "voice.xyz"
    ff("-f", "lavfi", "-i", "sine=duration=3", "-c:a", "libmp3lame", "-f", "mp3", str(audio))
    assert media.probe(media.trim(audio, 0.5, 2.0)).has_audio
    assert media.strip_metadata(audio)[0].suffix == ".mka"


def test_odd_file_names_and_unprintable_console(video, tmp_path, monkeypatch):
    import sys
    name = tmp_path / "Férias 日本語 😀.mp4"
    name.write_bytes(video.read_bytes())
    class Broken:                                    # a console that can't encode what we print
        def write(self, s):
            raise UnicodeEncodeError("cp1252", s, 0, 1, "can't encode")
        def flush(self):
            pass
    monkeypatch.setattr(sys, "stderr", Broken())
    out = media.trim(name, 0, 2)
    assert out.exists() and media.probe(out).has_video
    assert media.compress_audio(name, kbps=64).exists()
