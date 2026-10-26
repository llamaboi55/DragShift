"""Single-track source ranges and independent classic Melt boundaries."""
from dataclasses import dataclass, field
from pathlib import Path
import copy
import math
import uuid

from .util import UserError


@dataclass
class Clip:
    path: Path
    start: float
    end: float
    source_duration: float
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)

    @property
    def duration(self):
        return self.end - self.start


@dataclass
class Cut:
    duration: float = 2.
    strength: int = 65
    enabled: bool = True


class Timeline:
    def __init__(self):
        self.clips = []
        self.cuts = {}

    @property
    def duration(self):
        return sum(clip.duration for clip in self.clips)

    def snapshot(self):
        return copy.deepcopy(self)

    def add(self, path, source_duration, start=0., end=None):
        if len(self.clips) >= 16:
            raise UserError('Use up to 16 clips in one series.')
        clip = Clip(Path(path), start, source_duration if end is None else end, source_duration)
        self._range(clip, clip.start, clip.end)
        self.clips.append(clip)
        return len(self.clips)-1

    @staticmethod
    def _range(clip, start, end):
        if (not all(math.isfinite(v) for v in (start, end, clip.source_duration))
                or start < 0 or end > clip.source_duration+.001 or end-start < .199):
            raise UserError('Choose a source range of at least 0.2 seconds within the clip.')

    def trim(self, index, start, end):
        clip = self.clips[index]
        self._range(clip, start, end)
        clip.start, clip.end = start, min(end, clip.source_duration)

    def cut(self, index):
        left, right = self.clips[index:index+2]
        key = left.uid, right.uid
        if key not in self.cuts:
            self.cuts[key] = Cut(duration=min(2., max(.1, right.duration/2)))
        return self.cuts[key]

    def move(self, index, destination):
        if not 0 <= destination < len(self.clips):
            return index
        self.clips.insert(destination, self.clips.pop(index))
        return destination

    def remove(self, index):
        del self.clips[index]

    def split(self, index, source_time):
        if len(self.clips) >= 16:
            raise UserError('Use up to 16 clips in one series.')
        clip = self.clips[index]
        self._range(clip, clip.start, source_time)
        self._range(clip, source_time, clip.end)
        old_cut = copy.deepcopy(self.cut(index)) if index+1 < len(self.clips) else None
        right = Clip(clip.path, source_time, clip.end, clip.source_duration)
        clip.end = source_time
        self.clips.insert(index+1, right)
        # A split alone does not add an effect to a continuous source shot.
        self.cuts[clip.uid, right.uid] = Cut(duration=.1, enabled=False)
        if old_cut:
            self.cuts[right.uid, self.clips[index+2].uid] = old_cut
        return index+1

    def locate(self, time):
        position = 0.
        for index, clip in enumerate(self.clips):
            if time < position+clip.duration or index == len(self.clips)-1:
                return index, clip.start+max(0., min(clip.duration, time-position))
            position += clip.duration
        raise UserError('Drop at least two clips here.')

    def validate(self, fps=30):
        if not 2 <= len(self.clips) <= 16 or fps not in (24,25,30,60):
            raise UserError('Choose 2–16 clips and a supported frame rate.')
        counts = []
        for clip in self.clips:
            self._range(clip, clip.start, clip.end)
            counts.append(round(clip.duration*fps))
        for index in range(len(self.clips)-1):
            cut = self.cut(index)
            if (not math.isfinite(cut.duration) or not .1 <= cut.duration <= 60
                    or not math.isfinite(cut.strength) or not 1 <= cut.strength <= 100):
                raise UserError('Choose a transition duration from 0.1 to 60 seconds and intensity from 1 to 100.')
            if cut.enabled and round(cut.duration*fps)+3 > counts[index+1]:
                raise UserError(f'Transition {index+1}: shorten the effect or lengthen the following clip to leave two clean frames.')
        return counts

    def preview(self, index):
        preview = Timeline()
        left, right = self.clips[index:index+2]
        cut = copy.deepcopy(self.cut(index))
        cut.duration = min(cut.duration, 3.)
        preview.add(left.path, left.source_duration, max(left.start, left.end-1.5), left.end)
        preview.add(right.path, right.source_duration, right.start, min(right.end, right.start+cut.duration+.75))
        preview.cuts[preview.clips[0].uid, preview.clips[1].uid] = cut
        return preview


def render(timeline, *, max_side=720, fps=30, audio=True):
    """One MPEG-4 sequence, classic vector edits and I-frame removal; no clean blend."""
    import json
    import tempfile
    from . import datamosh, video
    counts = timeline.validate(fps)
    if not math.isfinite(max_side) or not 64 <= max_side <= 2160:
        raise UserError('Choose a resolution from 64 to 2160 pixels.')
    clips = timeline.clips
    infos = [video.probe(c.path) for c in clips]
    for clip, info in zip(clips, infos):
        if clip.end > info.duration+.05:
            raise UserError('The source duration changed. Reload this clip before exporting.')
    lengths = [count/fps for count in counts]
    total = sum(lengths)
    boundaries, ranges, position = [], [], 0
    for index, count in enumerate(counts[:-1]):
        position += count
        boundaries.append(position)
        cut = timeline.cut(index)
        if cut.enabled:
            ranges.append(dict(start=position, end=position+round(cut.duration*fps), strength=cut.strength))
    first = infos[0]
    scale = min(1., max_side/max(first.display_width, first.display_height))
    width, height = [max(2, round(side*scale/2)*2) for side in (first.display_width,first.display_height)]
    with video.export_path(clips[0].path, 'Datamosh Melt') as (result, outs):
        with tempfile.TemporaryDirectory(prefix='.~mosh-', dir=result.parent) as temporary:
            folder = Path(temporary)
            clean, warped, moshed = [folder/name for name in ('clean.m4v','warped.m4v','moshed.m4v')]
            inputs, graph = [], []
            for index, (clip, length, count) in enumerate(zip(clips,lengths,counts)):
                inputs += ['-ss',str(clip.start),'-t',str(length),*video.input_options(clip.path),'-i',str(video.local_file(clip.path))]
                graph.append(f'[{index}:v:0]{video.square_pixels_filter()},fps={fps},'
                    f'scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,'
                    f'setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=0.1,trim=end_frame={count},setpts=N/({fps}*TB)[v{index}]')
            graph.append(''.join(f'[v{i}]' for i in range(len(clips)))+f'concat=n={len(clips)}:v=1:a=0[v]')
            keys = sorted({n/fps for n in boundaries}|{r['end']/fps for r in ranges})
            args = video.ffmpeg_args('ffgac')+inputs+['-filter_complex_threads','2','-filter_complex',';'.join(graph),
                '-map','[v]','-an','-c:v','mpeg4','-mpv_flags','+nopimb+forcemv','-q:v','3','-fcode','6',
                '-g','max','-sc_threshold','max','-bf','0','-force_key_frames',','.join(f'{t:.8f}' for t in keys),
                '-threads','4','-r',str(fps),'-f','rawvideo',str(clean)]
            video.run_process(args,total,'1/4 · Preparing ordered clips…',0,.4)
            script = Path(datamosh.__file__).with_name('assets')/'datamosh.js'
            video.run_process([video.executable('ffedit'),'-i',str(clean),'-s',str(script),'-sp',
                json.dumps(dict(ranges=ranges),separators=(',',':')),'-o',str(warped)],
                message='2/4 · Bending motion vectors…',offset=.4,span=.15,parse_progress=False)
            removed, count = [], 0
            with moshed.open('wb') as stream:
                for kind, packet in datamosh._packets(warped):
                    video.check_cancel()
                    if kind is None:
                        stream.write(packet)
                        continue
                    if kind == 0 and any(r['start'] <= count < r['end'] for r in ranges):
                        removed.append(count)
                    else:
                        stream.write(packet)
                    count += 1
            if len(removed) < len(ranges) or count < sum(counts)-1:
                raise UserError('A transition keyframe or clean recovery frame is missing. Try a shorter transition.')
            gaps = ''.join(f'+gte(N,{n-i})' for i,n in enumerate(removed))
            graph = [f"[0:v]setpts='(N{gaps})/({fps}*TB)',fps={fps},tpad=stop_mode=clone:stop_duration=0.1,trim=duration={total}[v]"]
            has_audio = audio and any(info.has_audio for info in infos)
            if has_audio:
                for index,(info,length) in enumerate(zip(infos,lengths)):
                    source = f'[{index+1}:a:0]' if info.has_audio else 'anullsrc=r=48000:cl=stereo,'
                    graph.append(f'{source}atrim=duration={length},asetpts=PTS-STARTPTS,aresample=48000,'
                                 f'aformat=sample_fmts=fltp:channel_layouts=stereo,apad,atrim=duration={length}[a{index}]')
                graph.append(''.join(f'[a{i}]' for i in range(len(clips)))+f'concat=n={len(clips)}:v=0:a=1[audio]')
            args = video.ffmpeg_args()+['-fflags','+genpts','-r',str(fps),'-i',str(moshed)]+inputs+[
                '-filter_complex_threads','2','-filter_complex',';'.join(graph),'-map','[v]']
            args += ['-map','[audio]'] if has_audio else ['-an']
            args += video._video_codec('MP4')+['-t',str(total),str(result)]
            video.run_process(args,total,'4/4 · Exporting playable MP4…',.6,.4)
    return outs
