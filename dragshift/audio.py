"""Local audio conversion and editing through the bundled FFmpeg engines.

The first audio track is used; pictures, video, subtitles and chapters are not
exported. Edits default to AAC in M4A. Set ``target='WAV'`` or ``'FLAC'`` for a
lossless edited master. WAV/AIFF exports use widely compatible 16-bit PCM.
Normalization uses two-pass EBU R128 loudnorm at -16 LUFS, -1.5 dBTP, LRA 11.
Vorbis uses VBR quality 5, including low-rate speech that cannot encode at 192k.
Fades are measured before normalization; an optional volume gain follows it.
FFmpeg's reverse filter buffers the selected audio in memory.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import video
from .util import UserError

TARGETS = ["MP3", "WAV", "FLAC", "M4A", "AAC", "OGG", "OPUS", "AIFF"]
AUDIO_ENCODERS = {"MP3": "libmp3lame", "WAV": "pcm_s16le", "FLAC": "flac", "M4A": "aac",
                  "AAC": "aac", "OGG": "libvorbis", "OPUS": "libopus", "AIFF": "pcm_s16be"}
SAMPLE_RATES = (8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000, 64000, 88200, 96000)
AUDIO_FILTER = "Audio (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.opus *.aiff *.aif *.wma *.ape *.alac);;All files (*)"
_AAC_RATES = SAMPLE_RATES
_MP3_RATES = SAMPLE_RATES[:9]
_NORMALIZE = "loudnorm=I=-16:TP=-1.5:LRA=11"


@dataclass(frozen=True)
class AudioInfo:
    duration: float
    sample_rate: int
    channels: int
    codec: str
    bitrate: int
    tags: dict


def _number(value, default=0.) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _file(src) -> Path:
    try:
        return video.local_file(src)
    except UserError as e:
        raise UserError("The selected audio file was moved or deleted. Choose it again.") from e


def probe(src) -> AudioInfo:
    """Read the first audio stream, ignoring attached cover artwork."""
    raw = video.capture([video.executable("ffprobe"), "-v", "error", "-show_format", "-show_streams",
                         "-of", "json", str(_file(src))])
    try:
        data = json.loads(raw)
        stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
        if stream is None:
            raise UserError("This file has no readable audio track.")
        container = data.get("format", {})
        duration = _number(stream.get("duration")) or _number(container.get("duration"))
        rate, channels = int(stream.get("sample_rate", 0)), int(stream.get("channels", 0))
        if duration <= 0 or rate <= 0 or channels <= 0:
            raise UserError("This audio has no usable duration, sample rate or channels. Try another file.")
        bitrate = int(_number(stream.get("bit_rate")) or _number(container.get("bit_rate")))
        return AudioInfo(duration, rate, channels, stream.get("codec_name", "unknown"), bitrate,
                         {**container.get("tags", {}), **stream.get("tags", {})})
    except (ValueError, KeyError, TypeError, OverflowError) as e:
        raise UserError("The audio information could not be read.") from e


def _target(target) -> str:
    if not isinstance(target, str) or target.upper() not in TARGETS:
        raise UserError("Unsupported audio export format. Choose MP3, WAV, FLAC, M4A, AAC, OGG, OPUS or AIFF.")
    return target.upper()


def _rate(target: str, desired: int, explicit=False) -> int:
    # Opus always decodes at 48 kHz; avoid promising a different output rate.
    choices = (48000,) if target == "OPUS" else _MP3_RATES if target == "MP3" else _AAC_RATES if target in ("AAC", "M4A") else None
    if choices is None or desired in choices:
        return desired
    if explicit:
        raise UserError(f"{target} cannot use {desired} Hz. Choose " + ("48000 Hz." if target == "OPUS" else "a supported sample rate up to 48000 Hz."))
    return min(choices, key=lambda r: abs(r - desired))


@lru_cache(maxsize=16)
def _engine_capabilities(path: str, size: int, modified_ns: int, kind: str) -> frozenset[str]:
    """Read compiled features once per executable revision, not its configure claims."""
    text = video.capture([path, "-hide_banner", "-" + kind]).decode("utf-8", "replace")
    # Recent FFmpeg lists two filter flags; older builds (including FFglitch)
    # include the third command-support flag.
    flags = r"[TSC.]{2,3}" if kind == "filters" else r"[VAS][A-Z.]{5}"
    return frozenset(re.findall(r"^\s*" + flags + r"\s+(\w[\w-]*)\s+", text, re.MULTILINE))


def engine_capabilities(kind="encoders", name="ffmpeg") -> frozenset[str]:
    """Compiled encoder/decoder/filter names, shared with the release staging gate."""
    if kind not in ("encoders", "decoders", "filters"):
        raise ValueError("Choose encoders, decoders or filters.")
    path = Path(video.executable(name)).resolve()
    info = path.stat()
    return _engine_capabilities(str(path), info.st_size, info.st_mtime_ns, kind)


def _codec(target: str, rate: int, channels: int, bitrate=192) -> list[str]:
    encoder = AUDIO_ENCODERS[target]
    if encoder not in engine_capabilities():
        raise UserError(f"Export to {target} is unavailable: this media engine is missing '{encoder}'. "
                        "Choose a full FFmpeg build or reinstall DragShift with its bundled media engines.")
    recipes = {
        "MP3": ["-b:a", f"{bitrate}k"],
        "WAV": [],
        "FLAC": [],
        "M4A": ["-b:a", f"{bitrate}k", "-movflags", "+faststart"],
        "AAC": ["-b:a", f"{bitrate}k"],
        "OGG": ["-q:a", "5"],
        "OPUS": ["-b:a", f"{bitrate}k"],
        "AIFF": [],
    }
    args = ["-c:a", encoder] + recipes[target] + ["-ar", str(rate)]
    # MP3 has a maximum of two channels. Other formats retain the source layout.
    if target == "MP3" and channels > 2:
        args += ["-ac", "2"]
    return args


def convert_audio(src, target: str) -> list[Path]:
    target = _target(target)
    info = probe(src)
    with video.export_path(src, ext="." + target.lower()) as (tmp, outs):
        args = video.ffmpeg_args() + ["-i", str(_file(src)), "-map", "0:a:0", "-vn", "-sn", "-dn",
                                     "-map_metadata", "0", "-map_chapters", "-1"]
        args += _codec(target, _rate(target, info.sample_rate), info.channels) + [str(tmp)]
        video.run_process(args, info.duration, f"Converting audio to {target}…")
    return outs


def transition(paths, *, duration=.75, target='M4A', bitrate=192):
    """Join an ordered audio series with real overlapping crossfades."""
    paths=[_file(p) for p in paths]
    if not 2 <= len(paths) <= 16:
        raise UserError('Choose 2–16 audio files in the desired order.')
    target=_target(target)
    duration=_setting(duration,'crossfade duration',.05,60)
    infos=[probe(p) for p in paths]
    if any(info.duration <= duration*(2 if 0<i<len(infos)-1 else 1)+.02 for i,info in enumerate(infos)):
        raise UserError('The crossfades must fit inside every file. Middle clips need room for two fades.')
    if bitrate not in (64,96,128,160,192,256,320):
        raise UserError('Choose a supported audio bitrate.')
    inputs=[]
    graph=[]
    for i,path in enumerate(paths):
        inputs+=['-i',str(path)]
        graph.append(f'[{i}:a:0]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]')
    previous='a0'
    for i in range(1,len(paths)):
        label=f'mix{i}'
        graph.append(f'[{previous}][a{i}]acrossfade=d={duration}:c1=tri:c2=tri[{label}]')
        previous=label
    total=sum(i.duration for i in infos)-duration*(len(paths)-1)
    rate=_rate(target,48000)
    with video.export_path(paths[0],'Audio transition','.'+target.lower()) as (tmp,outs):
        args=video.ffmpeg_args()+inputs+['-filter_complex_threads','2','-filter_complex',';'.join(graph),
            '-map',f'[{previous}]','-vn',*_codec(target,rate,2,bitrate),str(tmp)]
        video.run_process(args,total,'Creating audio transitions…')
    return outs


def _setting(value, name: str, minimum: float, maximum: float) -> float:
    try:
        n = float(value)
    except (TypeError, ValueError, OverflowError) as e:
        raise UserError(f"Choose a valid {name}.") from e
    if isinstance(value, bool) or not math.isfinite(n) or not minimum <= n <= maximum:
        raise UserError(f"Choose {name} between {minimum:g} and {maximum:g}.")
    return n


def _tempo(speed: float) -> list[str]:
    rates = []
    while speed < .5:
        rates.append(.5)
        speed /= .5
    while speed > 2:
        rates.append(2.)
        speed /= 2
    rates.append(speed)
    return [f"atempo={r:g}" for r in rates]


def _normalization(args: list[str], filters: list[str], duration: float) -> str | None:
    """Measure with the same edits as the encode pass; shared runner can cancel."""
    analysis = args + ["-loglevel", "info", "-af", ",".join(filters + [_NORMALIZE + ":print_format=json"]),
                       "-f", "null", "-"]
    log = video.run_process(analysis, duration, "Measuring audio loudness (1/2)…", 0, .45)
    try:
        text = log.decode("utf-8", "replace")
        measure = json.loads(text[text.rfind("{"):text.rfind("}") + 1])
        values = [float(measure[k]) for k in ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")]
    except (AttributeError, ValueError, KeyError, TypeError) as e:
        raise UserError("Audio loudness could not be measured. Try exporting without normalization.") from e
    # Silence / clips shorter than EBU's measurement gate have no finite LUFS.
    # Passing -inf into measured_I would make FFmpeg reject an otherwise valid file.
    if not all(math.isfinite(v) for v in values):
        return None
    keys = ("measured_I", "measured_TP", "measured_LRA", "measured_thresh", "offset")
    return _NORMALIZE + ":" + ":".join(f"{key}={value:g}" for key, value in zip(keys, values)) + ":linear=true"


def transform(src, *, start=0., end=None, volume_db=0., normalize=False, fade_in=0., fade_out=0.,
              speed=1., reverse=False, mono=False, sample_rate=None, bitrate=192,
              strip_metadata=False, label="Edited", target="M4A") -> list[Path]:
    """Trim, change tempo without pitch, reverse, fade and adjust local audio.

    Normalization targets -16 LUFS / -1.5 dBTP before optional volume gain.
    ``target`` chooses the final codec directly, avoiding an intermediate lossy
    conversion. Explicit sample rates incompatible with that codec are rejected.
    """
    target = _target(target)
    info = probe(src)
    start = _setting(start, "start time", 0, info.duration)
    end = info.duration if end is None else _setting(end, "end time", 0, info.duration + .05)
    end = min(end, info.duration)
    if end - start < .02:
        raise UserError(f"Choose a start before the end, between 0 and {info.duration:.2f} seconds.")
    speed = _setting(speed, "speed", .25, 4)
    volume_db = _setting(volume_db, "volume gain (dB)", -60, 24)
    duration = (end - start) / speed
    fade_in = _setting(fade_in, "fade-in duration", 0, duration)
    fade_out = _setting(fade_out, "fade-out duration", 0, duration)
    bitrate_value = _setting(bitrate, "audio bitrate (kbps)", 32, 512)
    if not bitrate_value.is_integer():
        raise UserError("Choose a whole-number audio bitrate in kbps.")
    bitrate = int(bitrate_value)
    if target == "MP3" and bitrate > 320:
        raise UserError("MP3 supports a bitrate up to 320 kbps.")
    if sample_rate is not None:
        rate_value = _setting(sample_rate, "sample rate (Hz)", min(SAMPLE_RATES), max(SAMPLE_RATES))
        if not rate_value.is_integer() or int(rate_value) not in SAMPLE_RATES:
            raise UserError("Choose a standard audio sample rate, such as 22050, 44100 or 48000 Hz.")
        rate = _rate(target, int(rate_value), explicit=True)
    else:
        rate = _rate(target, info.sample_rate)
    filters = ["asetpts=PTS-STARTPTS"]
    if speed != 1:
        filters += _tempo(speed)
    if reverse:
        filters.append("areverse")
    if mono:
        filters.append("aformat=channel_layouts=mono")
    elif target == "MP3" and info.channels > 2:
        filters.append("aformat=channel_layouts=stereo")
    filters.append(f"aresample={rate}")
    if fade_in:
        filters.append(f"afade=t=in:st=0:d={fade_in:g}")
    if fade_out:
        filters.append(f"afade=t=out:st={duration - fade_out:g}:d={fade_out:g}")
    args = video.ffmpeg_args() + ["-ss", f"{start:g}", "-t", f"{end - start:g}", "-i", str(_file(src)),
                                 "-map", "0:a:0", "-vn", "-sn", "-dn"]
    if normalize:
        if measured := _normalization(args, filters, duration):
            filters.append(measured)
    if volume_db:
        filters.append(f"volume={volume_db:g}dB")
    with video.export_path(src, label, "." + target.lower()) as (tmp, outs):
        args += ["-af", ",".join(filters)]
        args += ["-map_metadata", "-1", "-map_metadata:s", "-1"] if strip_metadata else ["-map_metadata", "0"]
        args += ["-map_chapters", "-1"] + _codec(target, rate, 1 if mono else info.channels, bitrate) + [str(tmp)]
        video.run_process(args, duration, f"{label} audio…", .45 if normalize else 0, .55 if normalize else 1)
    return outs
