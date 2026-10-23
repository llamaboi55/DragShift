"""Real FFmpeg audio integration tests, using only synthetic sounds."""
import array
import hashlib
import json
import math
import subprocess
import threading

import pytest

from dragshift import audio, video
from dragshift.util import CONFIG, UserError


def ffmpeg(*args):
    return subprocess.run([video.executable("ffmpeg"), "-v", "error", "-nostdin", "-y", *map(str, args)],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                          creationflags=video.CREATION_FLAGS)


@pytest.fixture(scope="module")
def sound(tmp_path_factory):
    try:
        video.executable("ffmpeg")
        video.executable("ffprobe")
    except UserError as e:
        pytest.skip(str(e))
    p = tmp_path_factory.mktemp("audio") / "Тест 'audio'.wav"
    # Different channels and two frequencies also expose stream/downmix mistakes.
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=0.12*sin(2*PI*440*t)|0.08*sin(2*PI*660*t):s=44100:d=4",
           "-c:a", "pcm_s16le", "-metadata", "title=Audio fixture", "-metadata", "comment=private", p)
    return p


def pcm(path):
    raw = ffmpeg("-i", path, "-map", "0:a:0", "-ac", "1", "-ar", "44100", "-f", "f32le", "pipe:1").stdout
    values = array.array("f")
    values.frombytes(raw)
    return values


def rms(values):
    return math.sqrt(sum(v * v for v in values) / len(values))


def test_probe_and_metadata(sound):
    info = audio.probe(sound)
    assert info.duration == pytest.approx(4, abs=.02)
    assert (info.sample_rate, info.channels, info.codec) == (44100, 2, "pcm_s16le")
    assert info.bitrate > 0
    assert info.tags.get("title") == "Audio fixture"


@pytest.mark.parametrize("target,codec", [("MP3", "mp3"), ("WAV", "pcm_s16le"), ("FLAC", "flac"),
    ("M4A", "aac"), ("AAC", "aac"), ("OGG", "vorbis"), ("OPUS", "opus"), ("AIFF", "pcm_s16be")])
def test_all_output_formats_preserve_original(sound, target, codec):
    before = hashlib.sha256(sound.read_bytes()).digest()
    out = audio.convert_audio(sound, target)[0]
    assert out != sound and out.suffix == "." + target.lower()
    info = audio.probe(out)
    assert info.codec == codec and info.channels == 2
    assert info.duration == pytest.approx(4, abs=.25)
    assert hashlib.sha256(sound.read_bytes()).digest() == before


def test_cover_art_and_multiple_audio_streams(sound, tmp_path):
    cover = tmp_path / "cover.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=red:s=32x32:d=0.1", "-frames:v", "1", cover)
    covered = tmp_path / "covered.mp3"
    ffmpeg("-i", sound, "-i", cover, "-map", "0:a:0", "-map", "1:v:0", "-c:a", "libmp3lame",
           "-c:v", "copy", "-id3v2_version", "3", "-disposition:v:0", "attached_pic", covered)
    assert audio.probe(covered).channels == 2
    out = audio.convert_audio(covered, "M4A")[0]
    streams = json.loads(video.capture([video.executable("ffprobe"), "-v", "error", "-show_streams",
                                        "-of", "json", str(out)]))["streams"]
    assert len(streams) == 1 and streams[0]["codec_type"] == "audio"
    multi = tmp_path / "two-tracks.mka"
    ffmpeg("-i", sound, "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
           "-map", "0:a:0", "-map", "1:a:0", "-c:a", "flac", multi)
    assert audio.probe(audio.convert_audio(multi, "WAV")[0]).duration == pytest.approx(4, abs=.03)


def test_trim_speed_mono_resample_and_metadata(sound):
    out = audio.transform(sound, start=.5, end=3.5, speed=2, mono=True, sample_rate=22050,
                          bitrate=96, strip_metadata=True)[0]
    info = audio.probe(out)
    assert out.suffix == ".m4a" and info.codec == "aac"
    assert info.duration == pytest.approx(1.5, abs=.15)
    assert info.channels == 1 and info.sample_rate == 22050
    assert "private" not in str(info.tags) and "Audio fixture" not in str(info.tags)
    kept = audio.transform(sound)[0]
    assert audio.probe(kept).tags.get("title") == "Audio fixture"


@pytest.mark.parametrize("target", audio.TARGETS)
def test_edit_exports_directly_to_requested_format(sound, target):
    out = audio.transform(sound, start=.5, end=2.5, target=target, volume_db=-3)[0]
    assert out.suffix == "." + target.lower()
    assert audio.probe(out).duration == pytest.approx(2, abs=.25)


def test_low_sample_rate_conversion(tmp_path, media_engines):
    low = tmp_path / "phone.wav"
    ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=1", low)
    for target in audio.TARGETS:
        out = audio.convert_audio(low, target)[0]
        assert audio.probe(out).duration == pytest.approx(1, abs=.25)


def test_volume_fades_and_reverse(sound, tmp_path):
    original = pcm(audio.transform(sound, mono=True)[0])
    quiet = pcm(audio.transform(sound, mono=True, volume_db=-12)[0])
    assert 20 * math.log10(rms(quiet) / rms(original)) == pytest.approx(-12, abs=.7)
    faded = pcm(audio.transform(sound, fade_in=1, fade_out=1, mono=True)[0])
    assert rms(faded[:4410]) < rms(faded[44100:48510]) * .15
    assert rms(faded[-4410:]) < rms(faded[44100:48510]) * .15
    ramp = tmp_path / "ramp.wav"
    ffmpeg("-f", "lavfi", "-i", "aevalsrc=(0.02+0.12*t)*sin(2*PI*440*t):s=44100:d=2", ramp)
    forward = pcm(audio.transform(ramp)[0])
    backward = pcm(audio.transform(ramp, reverse=True)[0])
    assert rms(forward[:22050]) < rms(forward[-22050:]) * .4
    assert rms(backward[:22050]) > rms(backward[-22050:]) * 2.5


def test_two_pass_normalization_reaches_target(sound, monkeypatch):
    calls = []
    original_run = video.run_process
    def track(args, *a, **kw):
        calls.append(args)
        return original_run(args, *a, **kw)
    monkeypatch.setattr(video, "run_process", track)
    out = audio.transform(sound, normalize=True)[0]
    assert any("measured_I=" in item for args in calls for item in args)
    result = subprocess.run([video.executable("ffmpeg"), "-hide_banner", "-nostdin", "-i", str(out),
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, creationflags=video.CREATION_FLAGS)
    log = result.stderr.decode("utf-8", "replace")
    measure = json.loads(log[log.rfind("{"):log.rfind("}") + 1])
    assert float(measure["input_i"]) == pytest.approx(-16, abs=1)


def test_normalization_handles_silence(tmp_path, media_engines):
    silent = tmp_path / "silence.wav"
    ffmpeg("-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", "1", silent)
    out = audio.transform(silent, normalize=True)[0]
    assert audio.probe(out).duration == pytest.approx(1, abs=.1)
    assert rms(pcm(out)) < .0001


@pytest.mark.parametrize("options", [{"start": 4}, {"start": -1}, {"start": float("nan")}, {"end": .0},
    {"end": 10}, {"start": 2, "end": 1}, {"speed": .1}, {"speed": 5}, {"speed": float("inf")},
    {"volume_db": float("nan")}, {"volume_db": 50}, {"fade_in": -1}, {"fade_out": 5},
    {"sample_rate": 12345}, {"sample_rate": 44100.5}, {"bitrate": 1}, {"bitrate": 192.5},
    {"start": "bad"}, {"speed": None}, {"target": "MOV"}, {"target": "MP3", "bitrate": 512},
    {"target": "MP3", "sample_rate": 96000}, {"target": "OPUS", "sample_rate": 44100}])
def test_invalid_settings(sound, options):
    with pytest.raises(UserError):
        audio.transform(sound, **options)


def test_invalid_inputs(sound, tmp_path):
    with pytest.raises(UserError, match="format"):
        audio.convert_audio(sound, "EXE")
    broken = tmp_path / "broken.mp3"
    broken.write_bytes(b"not audio")
    with pytest.raises(UserError):
        audio.probe(broken)
    picture = tmp_path / "picture.png"
    ffmpeg("-f", "lavfi", "-i", "color=c=blue:s=32x32:d=0.1", "-frames:v", "1", picture)
    with pytest.raises(UserError, match="audio"):
        audio.probe(picture)


def test_cancel_cleanup_batch_rollback_and_unique_outputs(sound, tmp_path):
    before = set(sound.parent.iterdir())
    cancel = threading.Event()
    def update(value, text):
        if value > .01:
            cancel.set()
    with video.job_context(video.Control(cancel=cancel, progress=update)):
        with pytest.raises(video.Cancelled):
            audio.transform(sound, normalize=True)
    assert set(sound.parent.iterdir()) == before
    with pytest.raises(UserError):
        video.batch([sound, tmp_path / "missing.wav"], lambda p: audio.convert_audio(p, "FLAC"))
    assert set(sound.parent.iterdir()) == before
    CONFIG["output"] = "folder"
    try:
        a = audio.convert_audio(sound, "WAV")[0]
        b = audio.convert_audio(sound, "WAV")[0]
        assert a != b and a.parent.name == "DragShift"
    finally:
        CONFIG["output"] = "next"
