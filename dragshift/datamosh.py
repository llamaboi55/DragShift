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
               preset='Melt', max_side=720, audio=True, fps=30, curve=None):
    if preset != 'Melt':
        raise UserError('Only classic Melt is available.')
    return sequence([first,second], tail=tail, head=head, duration=duration, strength=strength,
                    max_side=max_side, audio=audio, fps=fps, curve=curve, excerpts=True)


def sequence(paths, *, duration=2., strength=65, max_side=720, audio=True, fps=30,
             curve=None, excerpts=False, tail=4., head=6.):
    """Melt ordered clips once each; excerpt mode retains the A-tail/B-head tool."""
    paths = [video.local_file(p) for p in paths]
    if not 2 <= len(paths) <= 16 or excerpts and len(paths) != 2:
        raise UserError('Choose 2–16 clips. Excerpts are available for exactly two clips.')
    if (not all(math.isfinite(v) for v in (duration,strength,max_side,fps,tail,head))
            or not .1 <= duration <= 60 or not 0 <= strength <= 100 or not 64 <= max_side <= 2160
            or fps not in (24,25,30,60)):
        raise UserError('Choose valid duration, strength, resolution and frame rate.')
    curve = validate_curve(curve)
    infos = [video.probe(p) for p in paths]
    lengths = [i.duration for i in infos]
    starts = [0.] * len(paths)
    if excerpts:
        if not .2 <= tail <= lengths[0]+.05 or not .2 <= head <= lengths[1]+.05:
            raise UserError('Choose clip ranges within the source duration.')
        lengths = [min(tail,lengths[0]), min(head,lengths[1])]
        starts[0] = max(0.,infos[0].duration-lengths[0])
    counts = [max(1,round(length*fps)) for length in lengths]
    bridge = max(1,round(duration*fps))
    if any(count < bridge+3 for count in counts[1:]):
        raise UserError('The transition must finish at least two clean frames before each following clip ends.')
    lengths = [n/fps for n in counts]
    total_frames, boundaries = sum(counts), []
    for n in range(1,len(paths)):
        boundaries.append(sum(counts[:n]))
    total = total_frames/fps
    a=infos[0]
    scale=min(1.,max_side/max(a.display_width,a.display_height))
    w,h=max(2,round(a.display_width*scale/2)*2),max(2,round(a.display_height*scale/2)*2)
    with video.export_path(paths[0], 'Datamosh Melt') as (result,outs):
        with tempfile.TemporaryDirectory(prefix='.~mosh-',dir=result.parent) as folder:
            d=Path(folder)
            clean,warped,moshed=d/'clean.m4v',d/'warped.m4v',d/'moshed.m4v'
            inputs=[]
            graph=[]
            for i,(path,start,length,count) in enumerate(zip(paths,starts,lengths,counts)):
                inputs += ['-ss',str(start),'-t',str(length),*video.input_options(path),'-i',str(path)]
                graph.append(f'[{i}:v:0]{video.square_pixels_filter()},fps={fps},'
                    f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,'
                    f'setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=0.2,'
                    f'trim=end_frame={count},setpts=N/({fps}*TB)[v{i}]')
            graph.append(''.join(f'[v{i}]' for i in range(len(paths)))+f'concat=n={len(paths)}:v=1:a=0[v]')
            keys=sorted({n/fps for n in boundaries}|{(n+bridge)/fps for n in boundaries})
            args=video.ffmpeg_args('ffgac')+inputs+['-filter_complex_threads','2','-filter_complex',';'.join(graph),
                '-map','[v]','-an','-c:v','mpeg4','-mpv_flags','+nopimb+forcemv','-q:v','3','-fcode','6',
                '-g','max','-sc_threshold','max','-bf','0','-force_key_frames',','.join(f'{t:.8f}' for t in keys),
                '-threads','4','-r',str(fps),'-f','rawvideo',str(clean)]
            video.run_process(args,total,'1/4 · Preparing ordered clips…',0,.4)
            params=json.dumps(dict(ranges=[dict(start=n,end=n+bridge) for n in boundaries],
                strength=round(strength*1000),curve=[[round(x*100000),round(y*100000)] for x,y in curve],
                total=total_frames),separators=(',',':'))
            script=Path(__file__).with_name('assets')/'datamosh.js'
            video.run_process([video.executable('ffedit'),'-i',str(clean),'-s',str(script),'-sp',params,'-o',str(warped)],
                message='2/4 · Melting motion vectors…',offset=.4,span=.15,parse_progress=False)
            removed=[]
            count=0
            with moshed.open('wb') as stream:
                for kind,packet in _packets(warped):
                    video.check_cancel()
                    if kind is None:
                        stream.write(packet)
                        continue
                    if kind==0 and any(n <= count < n+bridge for n in boundaries):
                        removed.append(count)
                    else:
                        stream.write(packet)
                    count+=1
            if len(removed)<len(boundaries) or count<total_frames-1:
                raise UserError('A transition keyframe or clean recovery frame is missing. Try a shorter transition.')
            video.progress(.6,'4/4 · Exporting Melt series…')
            # Fill the removed frame at its original position, preserving later cuts/audio.
            gaps=''.join(f'+gte(N,{n-i})' for i,n in enumerate(removed))
            graph=[f"[0:v]setpts='(N{gaps})/({fps}*TB)',fps={fps},"
                   f'tpad=stop_mode=clone:stop_duration=0.2,trim=duration={total}[glitch]',
                   f'[1:v]setpts=N/({fps}*TB),trim=duration={total}[clean]']
            gain=curve_expression(curve,total)
            # Blending with clean video makes zero on the curve truly clean.
            gain=f'({gain})*{min(1.,strength/65):.8f}'
            graph.append(f"[glitch][clean]blend=all_expr='A*({gain})+B*(1-({gain}))':shortest=1[v]")
            has_audio=audio and any(i.has_audio for i in infos)
            if has_audio:
                for idx,(info,length) in enumerate(zip(infos,lengths)):
                    source=f'[{idx+2}:a:0]' if info.has_audio else 'anullsrc=r=48000:cl=stereo,'
                    graph.append(f'{source}atrim=duration={length},asetpts=PTS-STARTPTS,aresample=48000,'
                        f'aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={length}[a{idx}]')
                graph.append(''.join(f'[a{i}]' for i in range(len(paths)))+f'concat=n={len(paths)}:v=0:a=1[audio]')
            args=video.ffmpeg_args()+['-fflags','+genpts','-r',str(fps),'-i',str(moshed),
                '-r',str(fps),'-i',str(clean)]+inputs+['-filter_complex_threads','2','-filter_complex',';'.join(graph),'-map','[v]']
            args += ['-map','[audio]'] if has_audio else ['-an']
            args += video._video_codec('MP4')+['-t',str(total),str(result)]
            video.run_process(args,total,'4/4 · Exporting Melt series…',.6,.4)
    return outs
