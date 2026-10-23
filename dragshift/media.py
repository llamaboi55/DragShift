"""Audio and video conversion through FFmpeg. Same contract as convert.py: every function takes a
source path and returns a list of output paths written next to the source."""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from .util import UserError, sibling

# Anything FFmpeg can read counts as input; the lists below only decide the menu category.
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".aac", ".m4a", ".m4b", ".ogg", ".oga", ".opus", ".wma",
              ".aiff", ".aif", ".aifc", ".ac3", ".eac3", ".dts", ".amr", ".ape", ".wv", ".mka",
              ".mp2", ".mp1", ".au", ".snd", ".caf", ".spx", ".tta", ".mpc", ".w64", ".voc", ".gsm", ".ra",
              ".m4r", ".m4p", ".aax", ".oma", ".tak", ".dsf", ".dff", ".mlp", ".thd", ".sbc", ".adts"}
VIDEO_EXTS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".wmv", ".flv", ".m4v", ".mpg", ".mpeg",
              ".mpe", ".3gp", ".3g2", ".ts", ".mts", ".m2ts", ".vob", ".ogv", ".asf", ".f4v",
              ".rm", ".rmvb", ".divx", ".xvid", ".mxf", ".dv", ".qt", ".yuv", ".y4m", ".mpeg4", ".mp4v",
              ".m1v", ".m2v", ".mpv", ".mpv2", ".mjpeg", ".mjpg", ".h264", ".264", ".h265", ".265",
              ".hevc", ".avc", ".ogm", ".amv", ".wtv", ".dvr-ms", ".trp", ".tp", ".m2t", ".evo", ".gxf",
              ".nut", ".nsv", ".roq", ".bik", ".smk", ".swf", ".vro", ".mod", ".tod", ".mk3d", ".ivf"}
# containers that can hold either: look inside to decide
AMBIGUOUS_EXTS = {".mp4", ".mkv", ".webm", ".ogg", ".3gp", ".3g2", ".asf", ".ts", ".mov", ".m4a",
                  ".mpeg4", ".mp4v", ".ogm", ".nut", ".mod"}
# never worth probing: these are other DragShift categories or plainly not media
_NOT_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".tif", ".tiff", ".avif", ".bmp",
                   ".ico", ".pdf", ".docx", ".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".htm",
                   ".exe", ".dll", ".zip", ".lnk", ".py", ".js", ".iso", ".dmg", ".msi", ".app"}

# target -> (file extension, FFmpeg muxer, codec arguments)
_X264 = ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
         "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:a", "aac", "-b:a", "160k"]
AUDIO_SPECS: dict[str, tuple[str, str, list[str]]] = {
    "MP3":  (".mp3",  "mp3",    ["-c:a", "libmp3lame", "-q:a", "2"]),
    "WAV":  (".wav",  "wav",    ["-c:a", "pcm_s16le"]),
    "FLAC": (".flac", "flac",   ["-c:a", "flac"]),
    "M4A":  (".m4a",  "mp4",    ["-c:a", "aac", "-b:a", "192k"]),
    "AAC":  (".aac",  "adts",   ["-c:a", "aac", "-b:a", "192k"]),
    "OGG":  (".ogg",  "ogg",    ["-c:a", "libvorbis", "-q:a", "5"]),
    "OPUS": (".opus", "opus",   ["-c:a", "libopus", "-b:a", "128k"]),
    "WMA":  (".wma",  "asf",    ["-c:a", "wmav2", "-b:a", "192k"]),
    "AIFF": (".aiff", "aiff",   ["-c:a", "pcm_s16be"]),
}
VIDEO_SPECS: dict[str, tuple[str, str, list[str]]] = {
    "MP4":  (".mp4",  "mp4",      _X264 + ["-movflags", "+faststart"]),
    "MKV":  (".mkv",  "matroska", _X264),
    "WEBM": (".webm", "webm",     ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-row-mt", "1",
                                   "-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p",
                                   "-c:a", "libopus", "-b:a", "128k"]),
    "MOV":  (".mov",  "mov",      _X264),
    "AVI":  (".avi",  "avi",      ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-q:a", "3"]),
    "GIF":  (".gif",  "gif",      ["-an", "-loop", "0", "-vf",
                                   "fps=15,scale='min(480,iw)':-2:flags=lanczos,split[a][b];"
                                   "[a]palettegen[p];[b][p]paletteuse"]),
}
# video -> audio: pull the soundtrack out with the same settings as audio targets
VIDEO_TO_AUDIO = ["MP3", "M4A", "WAV", "FLAC"]
AUDIO_TARGETS = list(AUDIO_SPECS)
VIDEO_TARGETS = list(VIDEO_SPECS) + VIDEO_TO_AUDIO

_ENCODER = {"MP3": "libmp3lame", "WAV": "pcm_s16le", "FLAC": "flac", "M4A": "aac", "AAC": "aac",
            "OGG": "libvorbis", "OPUS": "libopus", "WMA": "wmav2", "AIFF": "pcm_s16be"}
_VIDEO_ENCODER = {"MP4": "libx264", "MKV": "libx264", "MOV": "libx264", "WEBM": "libvpx-vp9",
                  "AVI": "mpeg4", "GIF": "gif"}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # no console flash from the windowed exe


def _log(msg: str) -> None:
    """Diagnostics go to stderr (the app's log file). A file name the console can't encode must never
    abort a conversion, so any failure to print is ignored."""
    try:
        print(msg, file=sys.stderr)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------- locating FFmpeg
@lru_cache(maxsize=1)
def find_ffmpeg() -> str | None:
    """DRAGSHIFT_FFMPEG, then the copy shipped with the app (imageio-ffmpeg), then PATH."""
    env = os.environ.get("DRAGSHIFT_FFMPEG")
    if env and os.path.isfile(env):
        return env
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and os.path.isfile(exe):
            return exe
    except Exception:
        pass
    return shutil.which("ffmpeg")


def available() -> bool:
    return find_ffmpeg() is not None


def _run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    exe = find_ffmpeg()
    if not exe:
        raise UserError("FFmpeg isn't installed. Install it (or `pip install imageio-ffmpeg`) "
                        "to convert audio and video.")
    return subprocess.run([exe, "-hide_banner", "-nostdin", *args], capture_output=True,
                          timeout=timeout, creationflags=_NO_WINDOW)


@lru_cache(maxsize=1)
def _encoders() -> frozenset[str]:
    try:
        text = _run(["-encoders"], timeout=20).stdout.decode("utf-8", "replace")
    except Exception:
        return frozenset()
    return frozenset(m.group(1) for m in re.finditer(r"^\s[VAS][\w.]{5}\s+(\S+)", text, re.M))


# ---------------------------------------------------------------- probing
class Probe:
    def __init__(self, has_video: bool, has_audio: bool, duration: float | None, width: int = 0,
                 height: int = 0, fps: float = 0.0, tags: dict | None = None):
        self.has_video, self.has_audio, self.duration = has_video, has_audio, duration
        self.width, self.height, self.fps = width, height, fps   # as displayed (rotation applied)
        self.tags = tags or {}


_TECH_TAGS = {"major_brand", "minor_version", "compatible_brands", "encoder", "handler_name", "vendor_id"}


def _parse_tags(err: str) -> dict:
    """The file-level Metadata block printed right after 'Input #0'."""
    m = re.search(r"Input #0.*?\n(.*?)\n\s*Duration:", err, re.S)
    tags, in_block = {}, False
    for line in (m.group(1) if m else "").splitlines():
        if re.match(r"^\s{2}Metadata:", line):
            in_block = True
            continue
        if in_block:
            kv = re.match(r"^\s{4}(\S[^:]*?)\s*:\s?(.*)$", line)
            if kv:
                tags[kv.group(1).strip()] = kv.group(2).strip()
    return tags


@lru_cache(maxsize=64)
def _probe(path: str, mtime: float) -> Probe | None:
    try:
        err = _run(["-i", path], timeout=20).stderr.decode("utf-8", "replace")
    except Exception:
        return None
    streams = re.findall(r"Stream #\S+?: (Video|Audio):(.*)", err)
    # cover art is "Video" too, but it's a still image, not a video track
    video = any(k == "Video" and "attached pic" not in rest for k, rest in streams)
    audio = any(k == "Audio" for k, _ in streams)
    dur = None
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if not streams and "Duration" not in err:
        return None
    w = h = 0
    fps = 0.0
    for k, rest in streams:
        if k == "Video" and "attached pic" not in rest:
            sz = re.search(r",\s(\d{2,5})x(\d{2,5})[\s,\[]", rest + " ")
            if sz:
                w, h = int(sz.group(1)), int(sz.group(2))
            f = re.search(r"([\d.]+) fps", rest)
            fps = float(f.group(1)) if f else 0.0
            break
    rot = re.search(r"rotation of (-?[\d.]+) degrees", err)
    if rot and abs(round(float(rot.group(1)))) % 180 == 90:
        w, h = h, w
    return Probe(video, audio, dur, w, h, fps, _parse_tags(err))


def probe(path: str | Path) -> Probe | None:
    p = Path(path)
    try:
        return _probe(str(p), p.stat().st_mtime)
    except OSError:
        return None


def duration(path: str | Path) -> float | None:
    info = probe(path)
    return info.duration if info else None


def kind(path: str | Path) -> str | None:
    """'audio', 'video' or None. Extension first; look inside only for containers that can be both."""
    ext = Path(path).suffix.lower()
    if ext in AMBIGUOUS_EXTS and available():
        info = probe(path)
        if info:
            return "video" if info.has_video else "audio" if info.has_audio else None
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext not in _NOT_MEDIA_EXTS and available():
        # unknown extension (.mpeg4 renamed, .bin, no extension…): let FFmpeg look inside
        try:
            if Path(path).is_file() and Path(path).stat().st_size > 1024:
                info = probe(path)
                if info:
                    return "video" if info.has_video else "audio" if info.has_audio else None
        except OSError:
            pass
    return None


def human_duration(sec: float | None) -> str:
    if not sec:
        return ""
    s = int(round(sec))
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# ---------------------------------------------------------------- converting
def targets_for(cat: str, src_ext: str) -> list[str]:
    """Targets FFmpeg can really produce on this machine, minus the source's own format."""
    if not available():
        return []
    have = _encoders()
    cur = src_ext.lower().lstrip(".").upper()
    cur = {"OGA": "OGG", "AIF": "AIFF", "AIFC": "AIFF", "M4B": "M4A"}.get(cur, cur)
    out = []
    for t in (AUDIO_TARGETS if cat == "audio" else VIDEO_TARGETS):
        need = _ENCODER.get(t) if (cat == "audio" or t in VIDEO_TO_AUDIO) else _VIDEO_ENCODER[t]
        if t != cur and (not have or need in have):
            out.append(t)
    return out


def _fail_message(stderr: str) -> str:
    low = stderr.lower()
    if "does not contain any stream" in low or "matches no streams" in low:
        return "This file has no audio track to extract."
    if "invalid data found" in low or "moov atom not found" in low:
        return "This file is damaged or isn't a media file."
    lines = [l.strip() for l in stderr.splitlines() if l.strip()]
    return lines[-1][:200] if lines else "FFmpeg couldn't convert this file."


def convert_media(src: str | Path, cat: str, target: str) -> list[Path]:
    target = target.upper()
    src = Path(src)
    to_audio = target in AUDIO_SPECS and (cat == "audio" or target in VIDEO_TO_AUDIO)
    specs = AUDIO_SPECS if to_audio else VIDEO_SPECS
    if target not in specs:
        raise ValueError(f"Can't convert {cat} to {target}")
    ext, muxer, codec_args = specs[target]
    info = probe(src)
    if to_audio and info and not info.has_audio:
        raise UserError("This file has no audio track to extract.")
    out = sibling(src, ext=ext)
    tmp = out.with_name(f".~{out.name}.part")

    maps = ["-map", "0:a:0", "-map_metadata", "0"] if to_audio else \
           ["-map", "0:v:0", "-map", "0:a?", "-sn"]
    if to_audio:
        maps.insert(0, "-vn")
    try:
        # MKV takes almost any stream: try a lossless remux first, re-encode only if that fails
        attempts = [["-c", "copy"]] if target == "MKV" and cat == "video" else []
        attempts.append(codec_args)
        if not to_audio:
            attempts.append(codec_args + ["-ac", "2"])  # e.g. 5.1 sound that libopus/mp3 can't take as-is
        last = ""
        for codec in attempts:
            r = _run(["-y", "-i", str(src), *maps, *codec, "-f", muxer, str(tmp)])
            _log(f"[media] {src.name} -> {target} (video={info.has_video if info else '?'}, "
                 f"audio={info.has_audio if info else '?'}) {' '.join(codec[:6])}… exit={r.returncode}")
            ok = r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0
            if ok and not to_audio and target != "GIF" and info and info.has_audio:
                got = _probe(str(tmp), tmp.stat().st_mtime)
                ok = bool(got and got.has_audio)  # never hand back a silent copy of a video with sound
                if not ok:
                    last = "The sound track was lost during conversion."
            if ok:
                os.replace(tmp, out)
                return [out]
            last = r.stderr.decode("utf-8", "replace") or last
        raise UserError(_fail_message(last))
    except subprocess.TimeoutExpired:  # pragma: no cover
        raise UserError("FFmpeg took too long.")
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def silent_wav(seconds: float = 0.3, rate: int = 8000) -> bytes:
    """A tiny valid WAV (a quiet 440 Hz tone), for self-tests."""
    import io
    import math
    import struct
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
                               for i in range(int(seconds * rate))))
    return buf.getvalue()


# ================================================================ editing tools
# Compress, Trim, Edit (volume / speed / fades / rotate / flip / resolution), Merge, strip metadata.
# Each returns the new file's path; the original is never touched.
LOSSY_AUDIO = {".mp3": "MP3", ".m4a": "M4A", ".aac": "AAC", ".ogg": "OGG", ".oga": "OGG",
               ".opus": "OPUS", ".wma": "WMA"}
VIDEO_KEEP = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}      # containers we re-encode into as-is
VIDEO_CRF = {"Light": 20, "Balanced": 23, "Small": 28, "Tiny": 33}
VIDEO_HEIGHTS = {"1080p": 1080, "720p": 720, "480p": 480, "360p": 360}
AUDIO_KBPS = {"Tiny": 64, "Small": 96, "Balanced": 128, "High": 192, "Max": 256}


def audio_target_for(src: str | Path) -> str:
    """The format an edited/compressed audio file is written in: its own if it can be, else MP3."""
    ext = Path(src).suffix.lower()
    t = {".m4b": "M4A", ".aif": "AIFF", ".aifc": "AIFF", ".flac": "FLAC", ".wav": "WAV",
         ".aiff": "AIFF"}.get(ext) or LOSSY_AUDIO.get(ext)
    return t or "MP3"


_COPY_AUDIO = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".flac", ".wma", ".aiff", ".aif", ".mka"}
_COPY_VIDEO = VIDEO_KEEP | {".avi", ".mpg", ".mpeg", ".ts", ".3gp", ".flv", ".wmv", ".ogv", ".m2ts", ".mts"}


def copy_ext_for(src: str | Path, video: bool) -> str:
    """Extension to write when streams are copied as they are: the file's own if FFmpeg knows that
    container, else MP4 for video and MKA (Matroska audio, takes anything) for audio."""
    ext = Path(src).suffix.lower()
    if video:
        return ext if ext in _COPY_VIDEO else ".mp4"
    return ext if ext in _COPY_AUDIO else ".mka"


def video_ext_for(src: str | Path) -> str:
    ext = Path(src).suffix.lower()
    return ext if ext in VIDEO_KEEP else ".mp4"


def has_encoder(name: str) -> bool:
    enc = _encoders()
    return not enc or name in enc


def _video_codec(ext: str, crf: int, hevc: bool = False, kbps: int | None = None,
                 preset: str = "medium", with_audio: bool = True, audio_kbps: int = 128) -> list[str]:
    """Video + audio codec arguments for a re-encode into the container `ext`."""
    if ext == ".webm":
        v = ["-c:v", "libvpx-vp9", "-row-mt", "1", "-deadline", "good", "-cpu-used", "4",
             "-pix_fmt", "yuv420p"] + (["-b:v", f"{kbps}k"] if kbps else ["-crf", str(min(63, crf + 7)), "-b:v", "0"])
        return v + (["-c:a", "libopus", "-b:a", f"{audio_kbps}k"] if with_audio else [])
    lib = "libx265" if hevc and has_encoder("libx265") else "libx264"
    v = ["-c:v", lib, "-preset", preset, "-pix_fmt", "yuv420p"]
    v += ["-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.6)}k", "-bufsize", f"{kbps * 3}k"] if kbps \
        else ["-crf", str(crf + (4 if lib == "libx265" else 0))]
    if lib == "libx265" and ext in (".mp4", ".mov", ".m4v"):
        v += ["-tag:v", "hvc1"]
    if ext in (".mp4", ".mov", ".m4v"):
        v += ["-movflags", "+faststart"]
    return v + (["-c:a", "aac", "-b:a", f"{audio_kbps}k"] if with_audio else [])


def _scale(height: int | None) -> str:
    # never upscale; -2 keeps the width even
    return f"scale=-2:'min({height},ih)'" if height else "scale=trunc(iw/2)*2:trunc(ih/2)*2"


def _finish(src: Path, label: str, ext: str, attempts: list[list[str]], expect_audio: bool = False) -> Path:
    """Run FFmpeg into a hidden temp file, then rename it into place. Tries each argument list in
    turn (so a fancy first attempt can fall back to a plain one)."""
    out = sibling(src, label, ext)
    tmp = out.with_name(f".~{out.stem}.part{out.suffix}")   # same extension: FFmpeg picks the muxer from it
    last = ""
    try:
        for args in attempts:
            r = _run(["-y", *args, str(tmp)])
            _log(f"[media] {src.name} -> {label or 'edit'} exit={r.returncode}")
            if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
                if expect_audio:
                    got = _probe(str(tmp), tmp.stat().st_mtime)
                    if not (got and got.has_audio):
                        last = "The sound track was lost."
                        continue
                os.replace(tmp, out)
                return out
            last = r.stderr.decode("utf-8", "replace") or last
        raise UserError(_fail_message(last))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _need(src: Path, want_audio: bool = False, want_video: bool = False) -> Probe:
    info = probe(src)
    if info is None:
        raise UserError("This file is damaged or isn't a media file.")
    if want_audio and not info.has_audio:
        raise UserError("This file has no audio track.")
    if want_video and not info.has_video:
        raise UserError("This file has no video.")
    return info


def kbps_for_size(target_bytes: int, seconds: float, reserve_kbps: int = 0) -> int:
    return int(target_bytes * 8 / max(seconds, 1) / 1000) - reserve_kbps


# ---------------------------------------------------------------- compress
def compress_audio(src: str | Path, kbps: int | None = None, target_bytes: int | None = None,
                   mono: bool = False) -> Path:
    src = Path(src)
    info = _need(src, want_audio=True)
    if target_bytes:
        if not info.duration:
            raise UserError("Can't tell how long this file is, so a target size isn't possible.")
        kbps = kbps_for_size(target_bytes, info.duration)
    kbps = max(16, min(320, kbps or 128))
    target = audio_target_for(src)
    if target in ("WAV", "FLAC", "AIFF"):    # lossless sources can't just "get smaller": go lossy
        target = "MP3"
    ext = AUDIO_SPECS[target][0]
    codec = {"MP3": "libmp3lame", "M4A": "aac", "AAC": "aac", "OGG": "libvorbis", "OPUS": "libopus",
             "WMA": "wmav2"}[target]
    codec = ["-c:a", codec, "-b:a", f"{kbps}k"]
    if mono:
        codec += ["-ac", "1"]
    base = ["-i", str(src), "-map_metadata", "0"]
    with_art = base + ["-map", "0:a:0", "-map", "0:v?", "-c:v", "copy", "-disposition:v", "attached_pic"] + codec
    plain = base + ["-vn", "-map", "0:a:0"] + codec
    if target in ("MP3", "M4A"):
        return _finish(src, "Compressed", ext, [with_art, plain])
    return _finish(src, "Compressed", ext, [plain])


def compress_video(src: str | Path, preset: str = "Balanced", height: int | None = None,
                   target_bytes: int | None = None, hevc: bool = False) -> Path:
    src = Path(src)
    info = _need(src, want_video=True)
    ext = video_ext_for(src)
    kbps, a_kbps = None, 128
    if target_bytes:
        if not info.duration:
            raise UserError("Can't tell how long this video is, so a target size isn't possible.")
        total = kbps_for_size(target_bytes, info.duration)
        a_kbps = 128 if total >= 600 else 64 if total >= 250 else 32     # small budgets get thinner sound
        kbps = total - (a_kbps if info.has_audio else 0)
        if kbps < 30:
            raise UserError("That target is too small for this video's length. Pick a bigger size.")
    args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?", "-sn", "-map_metadata", "0",
            "-vf", _scale(height), *_video_codec(ext, VIDEO_CRF.get(preset, 23), hevc, kbps, audio_kbps=a_kbps)]
    return _finish(src, "Compressed", ext, [args], expect_audio=info.has_audio)


# ---------------------------------------------------------------- trim
def trim(src: str | Path, start: float, end: float | None, exact: bool = True) -> Path:
    """Keep [start, end] seconds. Audio is cut without re-encoding. Video is re-encoded for a frame-exact
    cut, or (exact=False) copied, which is instant but snaps to the nearest keyframe."""
    src = Path(src)
    info = _need(src)
    if end is not None and end <= start + 0.05:
        raise UserError("The end has to be after the start.")
    if info.duration and start >= info.duration:
        raise UserError("The start is past the end of the file.")
    cut = ["-ss", f"{start:.3f}", "-i", str(src)] + (["-t", f"{end - start:.3f}"] if end is not None else [])
    if not info.has_video:
        ext = copy_ext_for(src, video=False)
        target_ext, _, codec = AUDIO_SPECS[audio_target_for(src)]
        copy = _finish(src, "Trimmed", ext, [cut + ["-map", "0:a:0", "-map_metadata", "0", "-c", "copy"]]) \
            if ext != ".mka" else None
        if copy:
            return copy
        return _finish(src, "Trimmed", target_ext, [cut + ["-vn", "-map", "0:a:0", "-map_metadata", "0", *codec]])
    maps = ["-map", "0:v:0", "-map", "0:a?", "-sn", "-map_metadata", "0"]
    if not exact:
        ext = copy_ext_for(src, video=True)
        return _finish(src, "Trimmed", ext, [cut + maps + ["-c", "copy", "-avoid_negative_ts", "make_zero"]],
                       expect_audio=info.has_audio)
    ext = video_ext_for(src)
    return _finish(src, "Trimmed", ext, [cut + maps + ["-vf", _scale(None), *_video_codec(ext, 18, preset="fast")]],
                   expect_audio=info.has_audio)


# ---------------------------------------------------------------- edit
def _atempo(speed: float) -> str:
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    while speed < 0.5:
        parts.append("atempo=0.5")
        speed /= 0.5
    parts.append(f"atempo={speed:.4f}")
    return ",".join(parts)


def _audio_filters(gain_db: float, normalize: bool, speed: float, fade_in: float, fade_out: float,
                   duration: float | None) -> list[str]:
    f = []
    if abs(speed - 1.0) > 1e-3:
        f.append(_atempo(speed))
    if normalize:
        f.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    elif abs(gain_db) > 0.05:
        f.append(f"volume={gain_db:.1f}dB")
    if fade_in > 0:
        f.append(f"afade=t=in:st=0:d={fade_in:.2f}")
    if fade_out > 0 and duration:
        total = duration / speed
        f.append(f"afade=t=out:st={max(0.0, total - fade_out):.2f}:d={min(fade_out, total):.2f}")
    return f


def edit_audio(src: str | Path, gain_db: float = 0.0, normalize: bool = False, speed: float = 1.0,
               fade_in: float = 0.0, fade_out: float = 0.0, mono: bool = False) -> Path:
    src = Path(src)
    info = _need(src, want_audio=True)
    filters = _audio_filters(gain_db, normalize, speed, fade_in, fade_out, info.duration)
    if not filters and not mono:
        raise UserError("Nothing to change yet.")
    target = audio_target_for(src)
    ext, _, codec = AUDIO_SPECS[target]
    args = ["-i", str(src), "-map_metadata", "0", "-vn", "-map", "0:a:0"]
    if filters:
        args += ["-af", ",".join(filters)]
    args += codec + (["-ac", "1"] if mono else [])
    return _finish(src, "Edited", ext, [args])


def edit_video(src: str | Path, rotate: int = 0, flip_h: bool = False, flip_v: bool = False,
               speed: float = 1.0, gain_db: float = 0.0, mute: bool = False, height: int | None = None,
               normalize: bool = False) -> Path:
    src = Path(src)
    info = _need(src, want_video=True)
    vf = []
    r = rotate % 360
    vf += {0: [], 90: ["transpose=1"], 180: ["hflip", "vflip"], 270: ["transpose=2"]}[r]
    if flip_h:
        vf.append("hflip")
    if flip_v:
        vf.append("vflip")
    if abs(speed - 1.0) > 1e-3:
        vf.append(f"setpts=PTS/{speed:.4f}")
    video_changed = bool(vf) or height is not None
    af = [] if mute or not info.has_audio else _audio_filters(gain_db, normalize, speed, 0, 0, info.duration)
    if not video_changed and not af and not mute:
        raise UserError("Nothing to change yet.")
    ext = video_ext_for(src) if video_changed else copy_ext_for(src, video=True)
    args = ["-i", str(src), "-map", "0:v:0", "-sn", "-map_metadata", "0"]
    if not mute:
        args += ["-map", "0:a?"]
    if video_changed:
        args += ["-vf", ",".join(vf + [_scale(height)]), *_video_codec(ext, 18, preset="fast", with_audio=False)]
    else:
        args += ["-c:v", "copy"]
    if mute:
        args += ["-an"]
    elif af or video_changed:
        if af:
            args += ["-af", ",".join(af)]
        args += ["-c:a", "libopus" if ext == ".webm" else "aac", "-b:a", "128k"]
    else:
        args += ["-c:a", "copy"]
    return _finish(src, "Edited", ext, [args], expect_audio=info.has_audio and not mute)


# ---------------------------------------------------------------- merge
def merge(srcs: list, kind: str) -> Path:
    """Join files end to end (inputs may differ in format, size or frame rate)."""
    srcs = [Path(s) for s in srcs]
    if len(srcs) < 2:
        raise UserError("Pick at least two files to join.")
    infos = [_need(s, want_audio=(kind == "audio"), want_video=(kind == "video")) for s in srcs]
    inputs = [x for s in srcs for x in ("-i", str(s))]
    n = len(srcs)
    if kind == "audio":
        fmt = "aformat=sample_rates=44100:channel_layouts=stereo"
        graph = ";".join(f"[{i}:a:0]{fmt}[a{i}]" for i in range(n))
        graph += ";" + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[a]"
        target = audio_target_for(srcs[0])
        ext, _, codec = AUDIO_SPECS[target]
        args = inputs + ["-filter_complex", graph, "-map", "[a]", *codec]
        return _finish(srcs[0], "Merged", ext, [args])
    first = next(i for i in infos if i.width)
    w, h = first.width - first.width % 2, first.height - first.height % 2
    fps = int(round(first.fps)) if first.fps else 30
    parts = []
    for i, info in enumerate(infos):
        parts.append(f"[{i}:v:0]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                     f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if info.has_audio:
            parts.append(f"[{i}:a:0]aformat=sample_rates=44100:channel_layouts=stereo[a{i}]")
        else:   # a clip without sound gets silence of the same length
            parts.append(f"anullsrc=r=44100:cl=stereo,atrim=duration={info.duration or 1:.3f}[a{i}]")
    parts.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]")
    ext = video_ext_for(srcs[0])
    args = inputs + ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]",
                     *_video_codec(ext, 20, preset="fast")]
    return _finish(srcs[0], "Merged", ext, [args], expect_audio=True)


# ---------------------------------------------------------------- metadata
def read_tags(src: str | Path) -> dict:
    info = probe(src)
    return {k: v for k, v in (info.tags if info else {}).items() if k not in _TECH_TAGS}


def strip_metadata(src: str | Path) -> tuple[Path, list[str]]:
    """Copy without tags, chapters or location (no re-encoding). Returns the file and the tag names removed."""
    src = Path(src)
    tags = list(read_tags(src))
    info = probe(src)
    ext = copy_ext_for(src, video=bool(info and info.has_video))
    args = ["-i", str(src), "-map", "0", "-map_metadata", "-1", "-map_chapters", "-1", "-c", "copy",
            "-fflags", "+bitexact", "-flags:v", "+bitexact", "-flags:a", "+bitexact"]
    plain = ["-i", str(src), "-map", "0:v?", "-map", "0:a?", "-map_metadata", "-1", "-map_chapters", "-1",
             "-c", "copy", "-fflags", "+bitexact"]
    return _finish(src, "Clean", ext, [args, plain]), tags


# ---------------------------------------------------------------- previews for the tool windows
def frame_at(src: str | Path, t: float = 0.0, max_side: int = 480):
    """One video frame as a PIL image (None if it can't be read)."""
    import io
    from PIL import Image
    try:
        r = _run(["-ss", f"{max(t, 0):.3f}", "-i", str(src), "-frames:v", "1", "-an", "-sn",
                  "-vf", f"scale='min({max_side},iw)':-2", "-f", "image2pipe", "-vcodec", "png", "-"],
                 timeout=30)
        if r.returncode == 0 and r.stdout:
            return Image.open(io.BytesIO(r.stdout)).convert("RGB")
    except Exception:
        pass
    return None


def waveform(src: str | Path, bins: int = 400) -> list[float]:
    """Peak level (0..1) per slice of the file, for drawing a waveform."""
    import numpy as np
    try:
        r = _run(["-i", str(src), "-vn", "-ac", "1", "-ar", "4000", "-f", "s16le", "-"], timeout=120)
    except Exception:
        return []
    data = np.frombuffer(r.stdout, dtype="<i2")
    if data.size == 0:
        return []
    chunks = np.array_split(np.abs(data.astype(np.float32)) / 32768.0, min(bins, data.size))
    return [float(c.max()) for c in chunks]


def estimate_video_bytes(src: str | Path, preset: str, height: int | None, hevc: bool) -> int | None:
    """Encode a few seconds from the middle with the chosen settings and scale up to the whole video."""
    import tempfile
    src = Path(src)
    info = probe(src)
    if not info or not info.duration or not info.has_video:
        return None
    ext = video_ext_for(src)
    span = min(4.0, info.duration)
    start = max(0.0, info.duration / 2 - span / 2)
    with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
        out = Path(td) / f"sample{ext}"
        r = _run(["-y", "-ss", f"{start:.2f}", "-i", str(src), "-t", f"{span:.2f}", "-map", "0:v:0", "-map", "0:a?",
                  "-sn", "-vf", _scale(height), *_video_codec(ext, VIDEO_CRF.get(preset, 23), hevc, preset="veryfast"),
                  str(out)])
        if r.returncode != 0 or not out.exists():
            return None
        return int(out.stat().st_size * info.duration / span)


def run_plain(args: list[str]) -> None:
    """Run FFmpeg for its side effect (self-tests)."""
    r = _run(["-y", "-loglevel", "error", *args])
    if r.returncode != 0:
        raise RuntimeError(r.stderr.decode("utf-8", "replace")[-300:])
