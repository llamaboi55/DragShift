"""Local FFmpeg processing, shared by conversion, editors and Datamosh.

No shell invocation, no network input, no writes to the original. Each worker has
its own progress/cancel context; a failed/cancelled export never publishes a file.
"""
from __future__ import annotations

import contextlib
import contextvars
from functools import lru_cache
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .util import CONFIG, UserError, sibling, unique_path

TARGETS = ["MP4", "MKV", "MOV", "WEBM", "AVI", "GIF", "MP3", "WAV"]
VIDEO_FILTER = "Video (*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.wmv *.flv *.ts *.mts *.m2ts *.mpeg *.mpg *.3gp *.ogv *.vob);;All files (*)"
CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
_publish_lock = threading.Lock()


class Cancelled(UserError):
    pass


@dataclass
class Control:
    cancel: threading.Event = field(default_factory=threading.Event)
    progress: Callable[[float, str], None] = lambda *_: None

    def check(self):
        if self.cancel.is_set():
            raise Cancelled("Cancelled. The original file is unchanged.")


_control = contextvars.ContextVar("video_control", default=None)


@contextlib.contextmanager
def job_context(control: Control):
    token = _control.set(control)
    try:
        yield
    finally:
        _control.reset(token)


def check_cancel():
    if (c := _control.get()) is not None:
        c.check()


def progress(value: float, message: str):
    check_cancel()
    if (c := _control.get()) is not None:
        c.progress(max(0., min(1., value)), message)


def executable(name: str) -> str:
    """Explicit override, bundled sidecar, then PATH. Never download at runtime."""
    env = os.environ.get("DRAGSHIFT_" + name.upper())
    suffix = ".exe" if os.name == "nt" else ""
    roots = [Path(sys.executable).parent] if getattr(sys, "frozen", False) else []
    roots += [Path(__file__).resolve().parent.parent]
    candidates = ([Path(env)] if env else []) + [r / "bin" / (name + suffix) for r in roots]
    for p in candidates:
        if p.is_file():
            return str(p.resolve())
    found = shutil.which(name)
    if found:
        return found
    package = ("ImageMagick with Liquid Rescale" if name == 'magick' else
               "FFglitch (ffgac and ffedit)" if name in ("ffgac", "ffedit") else "FFmpeg (ffmpeg and ffprobe)")
    raise UserError(f"{name} was not found. Install {package} and add its bin folder to PATH, or put it in DragShift/bin.")


def local_file(src) -> Path:
    p = Path(src).resolve()
    if not p.is_file():
        raise UserError("The selected video was moved or deleted. Choose the file again.")
    return p


def capture(args: list[str], timeout: int = 25) -> bytes:
    check_cancel()
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           stdin=subprocess.DEVNULL, timeout=timeout, creationflags=CREATION_FLAGS)
    except subprocess.TimeoutExpired as e:
        raise UserError("Reading this video took too long. Try a local, playable video file.") from e
    check_cancel()
    if p.returncode:
        detail = p.stderr.decode("utf-8", "replace").strip()[-1000:]
        raise UserError("Couldn't read this video. Check that it plays normally.\n" + detail)
    return p.stdout


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    duration: float
    fps: float
    has_audio: bool
    codec: str
    video_index: int
    tags: dict
    sample_aspect_ratio: float = 1.

    @property
    def display_width(self) -> float:
        """Square-pixel width after FFmpeg's automatic container rotation."""
        return self.width * self.sample_aspect_ratio

    @property
    def display_height(self) -> float:
        return float(self.height)


def _number(value, default=0.) -> float:
    try:
        n = float(value)
        return n if math.isfinite(n) else default
    except (TypeError, ValueError):
        return default


def probe(src) -> VideoInfo:
    raw = capture([executable("ffprobe"), "-v", "error", *input_options(src), "-show_format", "-show_streams", "-of", "json", str(local_file(src))])
    try:
        data = json.loads(raw)
        streams = data.get("streams", [])
        stream = next((s for s in streams if s.get("codec_type") == "video"
                       and not s.get("disposition", {}).get("attached_pic")), None)
        if stream is None:
            raise UserError("This file has no readable video stream.")
        w, h = int(stream["width"]), int(stream["height"])
        sar_num, _, sar_den = str(stream.get("sample_aspect_ratio", "1:1")).partition(":")
        sar = _number(sar_num, 1) / (_number(sar_den, 1) or 1)
        if sar <= 0:
            sar = 1.
        rotation = _number(stream.get("tags", {}).get("rotate"))
        for side in stream.get("side_data_list", []):
            rotation = _number(side.get("rotation"), rotation)
        if round(rotation) % 180 == 90:
            w, h = h, w
            sar = 1 / sar
        dur = _number(stream.get("duration")) or _number(data.get("format", {}).get("duration"))
        num, _, den = stream.get("avg_frame_rate", "0/1").partition("/")
        fps = _number(num) / (_number(den, 1) or 1)
        if min(w, h) < 2 or dur <= 0:
            raise UserError("This video has no usable duration or dimensions. Try a different file.")
        return VideoInfo(w, h, dur, fps or 30, any(s.get("codec_type") == "audio" for s in streams),
                         stream.get("codec_name", "unknown"), stream["index"],
                         {**data.get("format", {}).get("tags", {}), **stream.get("tags", {})}, sar)
    except (ValueError, KeyError, TypeError) as e:
        raise UserError("The video information could not be read.") from e


def run_process(args: list[str], duration: float = 0, message="Processing video…",
                offset: float = 0, span: float = 1, parse_progress: bool = True,
                *, cwd: str | Path | None = None, env: dict | None = None):
    """Drain stdout in a reader thread so cancellation works even with no output."""
    check_cancel()
    progress(offset, message)
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=err, creationflags=CREATION_FLAGS, cwd=cwd, env=env)
        lines = queue.Queue()

        def read():
            try:
                for line in iter(proc.stdout.readline, b""):
                    # ffedit may write logs rather than progress. Always drain them.
                    if parse_progress:
                        lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            ended = False
            while not ended or proc.poll() is None:
                check_cancel()
                try:
                    line = lines.get(timeout=.1)
                except queue.Empty:
                    continue
                if line is None:
                    ended = True
                elif duration and line.startswith(b"out_time_us="):
                    seconds = _number(line.split(b"=", 1)[1]) / 1_000_000
                    progress(offset + span * min(.999, seconds / duration), message)
            code = proc.wait()
            check_cancel()
            if code:
                err.seek(0, os.SEEK_END)
                end = err.tell()
                err.seek(max(0, end - 2000))
                detail = err.read().decode("utf-8", "replace").strip()
                raise UserError("Video processing failed. Try a different format or smaller resolution.\n" + detail)
            progress(offset + span, message)
            err.seek(0)
            return err.read()
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            reader.join(timeout=2)
            proc.stdout.close()


def ffmpeg_args(name="ffmpeg") -> list[str]:
    return [executable(name), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-progress", "pipe:1", "-stats_period", "0.25", "-filter_threads", "2"]


@lru_cache(maxsize=8)
def _precise_timing_supported(path: str, size: int, modified_ns: int) -> bool:
    version = capture([path, "-version"]).decode("utf-8", "replace")
    major = re.search(r"libavformat\s+(\d+)\.", version)
    return major is not None and int(major.group(1)) >= 61


def require_precise_timing():
    path = Path(executable("ffmpeg")).resolve()
    info = path.stat()
    if not _precise_timing_supported(str(path), info.st_size, info.st_mtime_ns):
        raise UserError("Precise video editing requires FFmpeg 7 or newer. Reinstall DragShift with its bundled media engines or select a newer FFmpeg build.")


def input_options(src):
    # FFmpeg otherwise replaces sub-20 ms GIF delays with its 100 ms default.
    return ['-min_delay','0'] if Path(src).suffix.lower()=='.gif' else []


@contextlib.contextmanager
def export_path(src, label: str = "", ext: str = ".mp4", *, directory: Path | None = None):
    if directory is None:
        out = sibling(src, label, ext)
    else:
        stem = f"{Path(src).stem} {label}".strip() if label else Path(src).stem
        out = unique_path(Path(directory) / (stem + ext))
    fd, name = tempfile.mkstemp(prefix=".~video-", suffix=ext, dir=out.parent)
    os.close(fd)
    tmp = Path(name)
    published = []
    try:
        yield tmp, published
        check_cancel()
        if not tmp.is_file() or tmp.stat().st_size == 0:
            raise UserError("No video was written. Try a different source file.")
        with _publish_lock:
            out = unique_path(out)
            # os.link atomically refuses to overwrite an existing result.
            try:
                os.link(tmp, out)
            except FileExistsError:
                raise UserError("Another program created this output. Please try again.")
            except OSError:
                if os.name != "nt":
                    raise
                os.rename(tmp, out)  # Windows rename also refuses an existing destination.
            published.append(out)
    finally:
        tmp.unlink(missing_ok=True)


def _video_codec(target, crf=23) -> list[str]:
    if target == "WEBM":
        return ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-deadline", "good", "-cpu-used", "4",
                "-c:a", "libopus", "-b:a", "128k", "-pix_fmt", "yuv420p", "-threads", "4"]
    if target == "AVI":
        return ["-c:v", "mpeg4", "-q:v", "4", "-c:a", "libmp3lame", "-b:a", "128k", "-pix_fmt", "yuv420p", "-threads", "4"]
    args = ["-c:v", "libx264", "-preset", "fast", "-crf", str(crf), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-threads", "4"]
    if target in ("MP4", "MOV"):
        args += ["-movflags", "+faststart"]
    return args


def square_pixels_filter() -> str:
    """Preserve displayed proportions before geometric edits or image export.

    FFmpeg autorotates before user filters and reciprocates SAR for a quarter
    turn. Use that filtered SAR rather than the container's unrotated value.
    An absent/zero SAR means square pixels. Even dimensions support YUV420.
    """
    return ("scale=w='max(2,trunc(iw*if(gt(sar,0),sar,1)/2)*2)':"
            "h='max(2,trunc(ih/2)*2)',setsar=1")


def convert_video(src, target: str) -> list[Path]:
    target = target.upper()
    if target not in TARGETS:
        raise UserError("Unsupported video export format.")
    info = probe(src)
    audio_only = target in ("MP3", "WAV")
    if audio_only and not info.has_audio:
        raise UserError("This video has no audio track to extract.")
    with export_path(src, ext="." + target.lower()) as (tmp, outs):
        args = ffmpeg_args() + [*input_options(src),"-i", str(local_file(src))]
        if target == "GIF":
            # Separate palette pass avoids buffering every decoded frame while
            # palettegen waits for EOF (unbounded RAM for long videos).
            with tempfile.TemporaryDirectory(prefix=".~gif-", dir=tmp.parent) as folder:
                palette = Path(folder) / "palette.png"
                vf = square_pixels_filter() + ",fps=12,scale=w='min(640,iw)':h=-2:flags=lanczos"
                first = args + ["-map", f"0:{info.video_index}", "-vf", vf + ",palettegen=stats_mode=diff",
                                "-frames:v", "1", "-threads", "1", str(palette)]
                run_process(first, info.duration, "Building GIF palette…", 0, .45)
                second = args + ["-i", str(palette), "-filter_complex_threads", "2", "-filter_complex",
                    f"[0:{info.video_index}]{vf}[v];[v][1:v]paletteuse=dither=sierra2_4a[out]",
                    "-map", "[out]", "-an", "-loop", "0", str(tmp)]
                run_process(second, info.duration, "Exporting GIF…", .45, .55)
        elif audio_only:
            args += ["-map", "0:a:0", "-vn"]
            args += ["-c:a", "libmp3lame", "-q:a", "2"] if target == "MP3" else ["-c:a", "pcm_s16le"]
        else:
            gif_filter=''
            if Path(src).suffix.lower()=='.gif':
                from .animation import gif_rate
                gif_filter=f',fps={gif_rate(src):.8f}'
            args += ["-map", f"0:{info.video_index}", "-map", "0:a:0?", "-vf",
                     square_pixels_filter() + gif_filter, *_video_codec(target)]
        if target != "GIF":
            args += [str(tmp)]
            run_process(args, info.duration, f"Converting to {target}…")
    return outs


def validate_range(info: VideoInfo, start: float, end: float | None):
    end = info.duration if end is None else end
    if not all(math.isfinite(v) for v in (start, end)) or start < 0 or end > info.duration + .05 or end - start < .04:
        raise UserError(f"Choose a start before the end, between 0 and {info.duration:.2f} seconds.")
    return start, min(end, info.duration)


def edit_filters(*, max_side=None, crop_ratio=None, crop_box=None, output_size=None, rotation=0, flip=False,
                 brightness=0., contrast=1., saturation=1.):
    """One geometric/colour pipeline for both preview frames and saved movies."""
    vf = [square_pixels_filter()]
    if crop_box is not None:
        left,top,right,bottom=crop_box
        vf.append(f"crop=w='max(2,round(iw*{right-left}))':h='max(2,round(ih*{bottom-top}))':"
                  f"x='min(iw-out_w,round(iw*{left}))':y='min(ih-out_h,round(ih*{top}))':exact=1")
    if crop_ratio:
        vf.append(f"crop=w='trunc(min(iw,ih*{crop_ratio})/2)*2':h='trunc(min(ih,iw/{crop_ratio})/2)*2'")
    if rotation == 90:
        vf.append("transpose=1")
    elif rotation == 180:
        vf += ["hflip", "vflip"]
    elif rotation == 270:
        vf.append("transpose=2")
    if flip:
        vf.append("hflip")
    if output_size is not None:
        vf.append(f'scale={output_size[0]}:{output_size[1]}:flags=lanczos')
    if max_side:
        vf.append(f"scale=w='trunc(iw*min(1,{max_side}/max(iw,ih))/2)*2':h='trunc(ih*min(1,{max_side}/max(iw,ih))/2)*2'")
    else:
        vf.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")
    vf.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")
    return vf


def transform(src, *, start=0., end=None, max_side=None, crf=23, crop_ratio=None, crop_box=None, output_size=None,
              rotation=0, flip=False, speed=1., brightness=0., contrast=1., saturation=1.,
              mute=False, strip_metadata=False, label="Edited") -> list[Path]:
    info = probe(src)
    start, end = validate_range(info, start, end)
    validate_geometry(crop_box,output_size)
    if (not math.isfinite(speed) or not .25 <= speed <= 4 or rotation not in (0, 90, 180, 270)
            or not 16 <= crf <= 40 or (max_side is not None and not 16 <= max_side <= 7680)
            or not -.5 <= brightness <= .5 or not .1 <= contrast <= 3 or not 0 <= saturation <= 3
            or (crop_ratio is not None and not .1 <= crop_ratio <= 10)):
        raise UserError("Choose valid video settings (speed 0.25–4×, size 16–7680 px, rotation 0/90/180/270°).")
    require_precise_timing()
    vf = edit_filters(max_side=max_side, crop_ratio=crop_ratio,crop_box=crop_box,output_size=output_size,rotation=rotation, flip=flip,
                      brightness=brightness, contrast=contrast, saturation=saturation)
    with export_path(src, label) as (tmp, outs):
        args = ffmpeg_args() + ["-ss", str(start), "-t", str(end - start), *input_options(src), "-i", str(local_file(src)),
                                "-map", f"0:{info.video_index}", "-vf", ",".join(vf)]
        if mute:
            args += ["-an"]
        else:
            args += ["-map", "0:a:0?"]
            if info.has_audio:
                rates = []
                rate = speed
                while rate < .5:
                    rates.append(.5)
                    rate /= .5
                while rate > 2:
                    rates.append(2.)
                    rate /= 2
                rates.append(rate)
                args += ["-af", "asetpts=PTS-STARTPTS," + ",".join(f"atempo={r}" for r in rates)]
        if strip_metadata:
            args += ["-map_metadata", "-1", "-map_metadata:s", "-1", "-map_chapters", "-1"]
        # FFmpeg 7's setpts clears frame durations and rate metadata, shortening
        # the final frame. Rescale encoded PTS, DTS and durations together;
        # passthrough preserves irregular frame intervals and every source frame.
        # Keep timestamp precision consistent across the bundled engines.
        args += ["-fps_mode", "passthrough", "-enc_time_base:v", "1:1000000", "-bsf:v",
                 f"setts=pts=(PTS-STARTPTS)/{speed}:dts=(DTS-STARTPTS)/{speed}:duration=DURATION/{speed}"]
        # B-frame reordering can lose VFR durations in the MP4 encoding path.
        args += _video_codec("MP4", crf) + ["-bf", "0", str(tmp)]
        run_process(args, (end - start) / speed, f"{label}…")
    return outs


def preview_bytes(src, at=0., max_side=720, *, crop_ratio=None,crop_box=None,output_size=None,rotation=0, flip=False,
                  brightness=0., contrast=1., saturation=1.) -> bytes:
    # Preview avoids a second probe; caller supplies a validated local file/time.
    validate_geometry(crop_box,output_size)
    vf = ','.join(edit_filters(max_side=max_side, crop_ratio=crop_ratio,crop_box=crop_box,output_size=output_size,rotation=rotation, flip=flip,
                              brightness=brightness, contrast=contrast, saturation=saturation))
    return capture([executable("ffmpeg"), "-v", "error", "-nostdin", "-ss", str(max(0., at)),*input_options(src),
                    "-i", str(local_file(src)), "-frames:v", "1", "-vf", vf,
                    "-f", "image2pipe", "-c:v", "png", "-threads", "1", "pipe:1"])


def validate_geometry(crop_box=None,output_size=None):
    if crop_box is not None:
        if (len(crop_box)!=4 or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in crop_box)
                or not 0<=crop_box[0]<crop_box[2]<=1 or not 0<=crop_box[1]<crop_box[3]<=1):
            raise UserError('Choose a crop rectangle within the video frame.')
    if output_size is not None and (len(output_size)!=2 or any(
            isinstance(v,bool) or not isinstance(v,int) or not 16<=v<=7680 or v%2 for v in output_size)):
        raise UserError('Choose even video dimensions from 16 to 7680 pixels.')


def snapshot(src, at=0.) -> list[Path]:
    info = probe(src)
    if not math.isfinite(at) or not 0 <= at < info.duration:
        raise UserError("Choose a frame time within the video.")
    with export_path(src, f"Frame {at:.2f}s", ".png") as (tmp, outs):
        args = ffmpeg_args() + ["-ss", str(at), *input_options(src), "-i", str(local_file(src)), "-map", f"0:{info.video_index}",
                                "-frames:v", "1", "-vf", square_pixels_filter(), "-threads", "1", str(tmp)]
        run_process(args, 0, "Saving frame…")
    return outs


def time_text(seconds: float) -> str:
    m, s = divmod(max(0., seconds), 60)
    h, m = divmod(int(m), 60)
    return f"{h:02d}:{m:02d}:{s:06.3f}" if h else f"{m:02d}:{s:06.3f}"


def batch(paths, fn) -> list[Path]:
    """One cancellable transaction: do not leave unreported earlier batch results."""
    outs = []
    parent = _control.get()
    try:
        for i, path in enumerate(paths):
            check_cancel()
            if parent:
                def update(value, text, index=i, name=Path(path).name):
                    parent.progress((index + value) / len(paths), f"{index + 1}/{len(paths)} · {name} · {text}")
                with job_context(Control(cancel=parent.cancel, progress=update)):
                    outs.extend(fn(path))
            else:
                outs.extend(fn(path))
        return outs
    except Exception:
        # Cleanup itself must never hide the original processing/cancel error.
        with contextlib.suppress(Exception):
            inputs = {Path(path).resolve() for path in paths}
            permitted_parents = {path.parent for path in inputs}
            if CONFIG.get("output") == "folder":
                permitted_parents |= {path.parent / "DragShift" for path in inputs}
            for out in outs:
                with contextlib.suppress(Exception):
                    path = Path(out)
                    # Never follow a link or remove an input or its ancestors.
                    if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                        continue
                    target = path.resolve()
                    if (target.parent not in permitted_parents
                            or any(target == source or target in source.parents for source in inputs)):
                        continue
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink(missing_ok=True)
        raise
