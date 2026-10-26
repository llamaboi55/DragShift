"""Real two-clip datamoshing: FFglitch motion vectors + bridge I-frame removal.

Normalize to MPEG-4 part 2 with no intra macroblocks in predicted frames. Edit
vectors only in the bridge, drop its I-frames, retain a recovery I-frame, and
decode/re-encode to ordinary H.264 MP4 for predictable playback in any player.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from . import video
from .util import UserError

PRESETS = ["Melt"]
VOP = b"\x00\x00\x01\xb6"


def _packets(path: Path):
    """Stream MPEG-4 VOP packets, bounding memory to a single encoded frame."""
    with path.open("rb") as f:
        buf = b""
        first = True
        while True:
            chunk = f.read(1024 * 1024)
            buf += chunk
            if first:
                at = buf.find(VOP)
                if at >= 0:
                    yield None, buf[:at]
                    buf = buf[at:]
                    first = False
            if not first:
                while (at := buf.find(VOP, 4)) >= 0:
                    packet, buf = buf[:at], buf[at:]
                    yield packet[4] >> 6, packet
            if not chunk:
                if buf and not first and len(buf) >= 5:
                    yield buf[4] >> 6, buf
                elif first:
                    raise UserError("FFglitch did not produce a usable MPEG-4 stream.")
                break
            if len(buf) > 64 * 1024 * 1024:
                raise UserError("The intermediate video contains an oversized frame. Choose a smaller resolution.")


def remove_keyframes(src: Path, dst: Path, start: int, end: int) -> tuple[int, int]:
    removed = count = 0
    with dst.open("wb") as f:
        for kind, packet in _packets(src):
            video.check_cancel()
            if kind is None:
                f.write(packet)
                continue
            if start <= count < end and kind == 0:
                removed += 1
            else:
                f.write(packet)
            count += 1
    return removed, count


def validate_curve(curve=None):
    points = [(float(x), float(y)) for x, y in (curve if curve is not None else [(0,1),(1,1)])]
    if (not 2 <= len(points) <= 64 or points[0][0] != 0 or points[-1][0] != 1
            or any(not math.isfinite(x) or not math.isfinite(y) or not 0 <= x <= 1 or not 0 <= y <= 1 for x,y in points)
            or any(b[0] <= a[0] for a,b in zip(points,points[1:]))):
        raise UserError('The strength curve needs ordered points from 0 to 100%, with strength between 0 and 100%.')
    return points


def curve_expression(points, total, variable='T'):
    expression = f'{points[-1][1]:.8f}'
    for (x0,y0),(x1,y1) in reversed(list(zip(points,points[1:]))):
        t0,t1=x0*total,x1*total
        line=f'({y0:.8f}+({y1-y0:.8f})*({variable}-{t0:.8f})/{t1-t0:.8f})'
        expression=f'if(lt({variable},{t1:.8f}),{line},{expression})'
    return expression


def transition(first, second, *, tail=4., head=6., duration=2., strength=65,
               preset="Melt", max_side=720, audio=True, fps=30) -> list[Path]:
    if preset != 'Melt':
        raise UserError('Only classic Melt is available.')
    a, b = video.probe(first), video.probe(second)
    values = (tail, head, duration, strength, max_side, fps)
    if (not all(math.isfinite(v) for v in values) or not .2 <= tail <= a.duration + .05
            or not .2 <= head <= b.duration + .05 or not .1 <= duration <= head - .1
            or not 1 <= strength <= 100 or not 64 <= max_side <= 2160
            or preset not in PRESETS or fps not in (24, 25, 30, 60)):
        raise UserError("Choose valid clip ranges. The transition must finish at least 0.1 seconds before clip B ends.")
    ffgac, ffedit = video.executable("ffgac"), video.executable("ffedit")
    tail, head = min(tail, a.duration), min(head, b.duration)
    af, bf, bridge = round(tail * fps), round(head * fps), max(1, round(duration * fps))
    if bf <= bridge + 1:
        raise UserError("Leave at least two clean frames after the transition in clip B.")
    tail, head, duration = af / fps, bf / fps, bridge / fps
    a_start = max(0., a.duration - tail)
    scale = min(1., max_side / max(a.display_width, a.display_height))
    w, h = max(2, round(a.display_width * scale / 2) * 2), max(2, round(a.display_height * scale / 2) * 2)
    total = tail + head
    with video.export_path(first, f"Datamosh {preset}") as (result, outs):
        # Work next to the output: available disk and cleanup belong to this job.
        with tempfile.TemporaryDirectory(prefix=".~mosh-", dir=result.parent) as folder:
            d = Path(folder)
            clean, warped, moshed = d / "clean.m4v", d / "warped.m4v", d / "moshed.m4v"

            def normalize(index, count, label):
                return (f"[{index}:v:0]{video.square_pixels_filter()},fps={fps},"
                        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p,"
                        f"tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame={count},setpts=N/({fps}*TB)[{label}]")

            graph = normalize(0, af, "a") + ";" + normalize(1, bf, "b") + ";[a][b]concat=n=2:v=1:a=0[v]"
            args = video.ffmpeg_args("ffgac") + ["-ss", str(a_start), "-t", str(tail), *video.input_options(first), "-i", str(video.local_file(first)),
                    "-t", str(head), *video.input_options(second), "-i", str(video.local_file(second)), "-filter_complex_threads", "2",
                    "-filter_complex", graph, "-map", "[v]", "-an", "-c:v", "mpeg4",
                    "-mpv_flags", "+nopimb+forcemv", "-q:v", "3", "-fcode", "6", "-g", "max",
                    "-sc_threshold", "max", "-bf", "0", "-force_key_frames", f"{tail:.8f},{tail + duration:.8f}",
                    "-threads", "4", "-r", str(fps), "-f", "rawvideo", str(clean)]
            video.run_process(args, total, "1/4 · Preparing clips…", 0, .4)
            params = json.dumps(dict(start=af, end=af + bridge, strength=strength, preset=preset), separators=(",", ":"))
            script = Path(__file__).with_name("assets") / "datamosh.js"
            video.run_process([ffedit, "-i", str(clean), "-s", str(script), "-sp", params, "-o", str(warped)],
                              message="2/4 · Bending motion vectors…", offset=.4, span=.15, parse_progress=False)
            video.progress(.55, "3/4 · Removing transition keyframes…")
            removed, count = remove_keyframes(warped, moshed, af, af + bridge)
            if removed < 1:
                raise UserError("The transition keyframe was not found. Try a different transition duration.")
            if count < af + bridge + 1:
                raise UserError("The second clip ended before the transition could recover.")
            video.progress(.6, "4/4 · Exporting playable MP4…")
            args = video.ffmpeg_args() + ["-fflags", "+genpts", "-r", str(fps), "-i", str(moshed),
                    "-ss", str(a_start), "-t", str(tail), *video.input_options(first), "-i", str(video.local_file(first)),
                    "-t", str(head), *video.input_options(second), "-i", str(video.local_file(second))]
            graph = (f"[0:v]setpts=N/({fps}*TB),tpad=stop_mode=clone:stop_duration={removed / fps},"
                     f"trim=duration={total}[v]")
            if audio and (a.has_audio or b.has_audio):
                for idx, info, length, label in [(1, a, tail, "aa"), (2, b, head, "bb")]:
                    source = f"[{idx}:a:0]" if info.has_audio else "anullsrc=r=48000:cl=stereo,"
                    graph += (f";{source}atrim=duration={length},asetpts=PTS-STARTPTS,aresample=48000,"
                              f"aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={length}[{label}]")
                graph += ";[aa][bb]concat=n=2:v=0:a=1[audio]"
            args += ["-filter_complex_threads", "2", "-filter_complex", graph, "-map", "[v]"]
            args += ["-map", "[audio]"] if audio and (a.has_audio or b.has_audio) else ["-an"]
            args += video._video_codec("MP4") + ["-t", str(total), str(result)]
            video.run_process(args, total, "4/4 · Exporting playable MP4…", .6, .4)
    return outs
