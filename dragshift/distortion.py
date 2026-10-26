"""Liquid-rescale → ordinary resize, as documented by OpenDistortBot.

Uses the real ImageMagick/liblqr engine, not a visual approximation. Pixel
identity with the current hosted @DistortBot has not been established.
Only decoded PNG frames are passed to ImageMagick; original media stays local.
"""
import io
import math
import os
import shutil
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor,as_completed
from dataclasses import dataclass
from pathlib import Path

from PIL import Image,ImageOps

from . import video
from .util import UserError,category

MAX_FRAMES=15000
TARGETS=('GIF','MP4','WEBM','PNG')


@dataclass(frozen=True)
class MediaInfo:
    width:int
    height:int
    duration:float
    fps:float
    animated:bool
    image:bool
    has_audio:bool=False
    codec:str=''


def probe(src):
    if category(src)=='image':
        with Image.open(src) as im:
            w,h=ImageOps.exif_transpose(im).size
            durations=[]
            for n in range(getattr(im,'n_frames',1)):
                im.seek(n)
                durations.append(max(10,int(im.info.get('duration',100))))
            animated=len(durations)>1
            length=sum(durations)/1000 if animated else 1.
            return MediaInfo(w,h,length,len(durations)/length if animated else 1.,animated,True)
    info=video.probe(src)
    return MediaInfo(round(info.display_width),round(info.display_height),info.duration,info.fps,True,False,info.has_audio,info.codec)


def _settings(strength,max_side):
    if (isinstance(strength,bool) or not isinstance(strength,(int,float)) or not math.isfinite(strength)
            or int(strength)!=strength or not 0<=strength<=95):
        raise UserError('Choose distortion intensity from 0 to 95.')
    if isinstance(max_side,bool) or not isinstance(max_side,int) or not 64<=max_side<=1920:
        raise UserError('Choose a longest edge from 64 to 1920 pixels.')
    return int(strength),max_side


def _environment(folder):
    return dict(os.environ,MAGICK_CONFIGURE_PATH=str(Path(__file__).with_name('assets')/'distortion-policy'),
                MAGICK_TEMPORARY_PATH=str(folder),MAGICK_THREAD_LIMIT='2')


def _resize(image,max_side):
    image=image.convert('RGBA')
    image.thumbnail((max_side,max_side),Image.Resampling.LANCZOS)
    if min(image.size)<2:
        raise UserError('This image is too narrow to distort.')
    return image


def _arguments(w,h,strength):
    # ImageMagick percentages use round-half-up, not Python's banker's rounding.
    width=max(2,int(w*(100-strength)/100+.5))
    height=max(2,int(h*(100-strength)/100+.5))
    return ['-liquid-rescale',f'{width}x{height}!', '-resize',f'{w}x{h}!', '-depth','8']


def distort_frame(image,*,strength=40,max_side=640):
    strength,max_side=_settings(strength,max_side)
    frame=_resize(image,max_side)
    video.check_cancel()
    if strength==0:
        return frame
    with tempfile.TemporaryDirectory(prefix='dragshift-distort-frame-') as directory:
        folder=Path(directory)
        source,out=folder/'input.png',folder/'output.png'
        frame.save(source,'PNG',compress_level=1)
        args=[video.executable('magick'),str(source),*_arguments(*frame.size,strength),
              '-define','png:color-type=6','-define','png:compression-level=1','PNG:'+str(out)]
        video.run_process(args,message='Distorting frame…',parse_progress=False,env=_environment(folder))
        with Image.open(out) as result:
            return result.convert('RGBA')


def source_frame(src,at=0.,max_side=640,*,info=None):
    info=info or probe(src)
    if not math.isfinite(at) or not 0<=at<info.duration:
        raise UserError('Choose a frame within the source duration.')
    if info.image:
        with Image.open(src) as im:
            elapsed=0.
            for n in range(getattr(im,'n_frames',1)):
                im.seek(n)
                elapsed+=max(10,int(im.info.get('duration',100)))/1000
                if elapsed>at or n==im.n_frames-1:
                    return _resize(ImageOps.exif_transpose(im),max_side)
    decoder=['-c:v','libvpx-vp9'] if info.codec=='vp9' else []
    args=[video.executable('ffmpeg'),'-v','error','-nostdin','-ss',str(at),*decoder,'-i',str(video.local_file(src)),
          '-frames:v','1','-vf',video.square_pixels_filter()+
          f",scale=w='min({max_side},iw)':h='min({max_side},ih)':force_original_aspect_ratio=decrease",
          '-f','image2pipe','-c:v','png','-pix_fmt','rgba','-threads','1','pipe:1']
    with Image.open(io.BytesIO(video.capture(args))) as im:
        return im.convert('RGBA')


def preview_bytes(src,at=0.,*,strength=40,max_side=640):
    frame=distort_frame(source_frame(src,at,max_side),strength=strength,max_side=max_side)
    data=io.BytesIO()
    frame.save(data,'PNG')
    return data.getvalue()


def export(src,*,strength=40,max_side=640,target=None,start=0.,end=None,at=0.):
    strength,max_side=_settings(strength,max_side)
    info=probe(src)
    target=(target or ('GIF' if info.image and info.animated else 'PNG' if info.image else 'MP4')).upper()
    if target not in TARGETS:
        raise UserError('Choose GIF, MP4, WebM or a PNG frame.')
    end=info.duration if end is None else end
    if (not all(math.isfinite(v) for v in (start,end,at)) or start<0 or end>info.duration+.001 or end-start<.02):
        raise UserError('Choose a valid segment inside the source.')
    length=end-start
    with video.export_path(src,'Distorted','.'+target.lower()) as (result,outs):
        if target=='PNG':
            frame=distort_frame(source_frame(src,at,max_side),strength=strength,max_side=max_side)
            frame.save(result,'PNG')
            video.progress(1,'Frame ready')
        else:
            _render(src,info,result,start,end,strength,max_side,target)
    return outs


def _render(src,info,result,start,end,strength,max_side,target):
    scale=min(1.,max_side/max(info.width,info.height))
    w,h=max(2,round(info.width*scale)),max(2,round(info.height*scale))
    # FFmpeg video encoders need even dimensions; GIF retains its source canvas.
    if not info.image:
        w,h=max(2,w//2*2),max(2,h//2*2)
    if not 0<info.fps<=240 or (end-start)*info.fps>MAX_FRAMES:
        raise UserError('Select a shorter segment (up to 15,000 frames).')
    estimate=math.ceil((end-start)*info.fps+1)*w*h*8+100_000_000
    if shutil.disk_usage(result.parent).free<estimate:
        raise UserError('Not enough space for temporary frames. Shorten the segment or reduce its size.')
    with tempfile.TemporaryDirectory(prefix='.~distort-',dir=result.parent) as directory:
        folder=Path(directory)
        raw,warped=folder/'raw',folder/'warped'
        raw.mkdir()
        warped.mkdir()
        durations=[]
        loop=0
        if info.image:
            elapsed=0.
            with Image.open(src) as source:
                loop=source.info.get('loop')
                for n in range(getattr(source,'n_frames',1)):
                    video.check_cancel()
                    source.seek(n)
                    seconds=max(10,int(source.info.get('duration',100)))/1000 if info.animated else end-start
                    left,right=max(start,elapsed),min(end,elapsed+seconds)
                    if right>left:
                        frame=_resize(ImageOps.exif_transpose(source),max_side)
                        frame.save(raw/f'frame{len(durations):08d}.png','PNG',compress_level=1)
                        durations.append(round((right-left)*1000))
                    elapsed+=seconds
                    if elapsed>=end:
                        break
            frames=sorted(raw.glob('frame*.png'))
        else:
            fps=min(info.fps,100) if target=='GIF' else info.fps
            vf=video.square_pixels_filter()+f',scale={w}:{h},fps={fps:.8f},format=rgba'
            decoder=['-c:v','libvpx-vp9'] if info.codec=='vp9' else []
            video.run_process(video.ffmpeg_args()+['-ss',str(start),'-t',str(end-start),*decoder,'-i',str(video.local_file(src)),
                '-map','0:v:0','-an','-vf',vf,'-frames:v',str(MAX_FRAMES+1),'-threads','1','-compression_level','1',str(raw/'frame%08d.png')],
                end-start,'Decoding source frames…',0,.15)
            frames=sorted(raw.glob('frame*.png'))
            durations=[1000/fps]*len(frames)
        if not frames or len(frames)>MAX_FRAMES:
            raise UserError('No usable frames, or too many frames. Select a shorter segment.')
        with Image.open(frames[0]) as first:
            w,h=first.size
        video.progress(.2,'Distorting frames…')
        _warp_frames(frames,warped,folder,w,h,strength)
        outputs=[warped/path.name for path in frames]
        if any(not p.is_file() for p in outputs):
            raise UserError('The distortion engine did not write every frame.')
        if target=='GIF':
            images=[]
            pixels=0
            for path in outputs:
                video.check_cancel()
                with Image.open(path) as image:
                    pixels+=image.width*image.height
                    if pixels>200_000_000:
                        raise UserError('This GIF is too large. Choose MP4/WebM or a shorter segment.')
                    images.append(image.convert('RGBA'))
            # GIF stores centiseconds. Carry rounding error across frames rather
            # than truncating every delay (which speeds up 24/30/60 fps video).
            elapsed=0.
            ticks=0
            gif_delays=[]
            for ms in durations:
                elapsed+=ms/10
                next_ticks=max(ticks+1,round(elapsed))
                gif_delays.append((next_ticks-ticks)*10)
                ticks=next_ticks
            options=dict(save_all=True,append_images=images[1:],duration=gif_delays,disposal=2,optimize=False)
            if loop is not None:
                options['loop']=loop
            images[0].save(result,'GIF',**options)
            video.progress(1,'GIF ready')
        else:
            # Safe internal basenames only; per-frame durations preserve GIF timing.
            manifest=warped/'frames.ffconcat'
            lines=['ffconcat version 1.0']
            for path,ms in zip(outputs,durations):
                lines.extend([f"file '{path.name}'",'option framerate 1000',f'duration {ms/1000:.8f}'])
            lines.extend([f"file '{outputs[-1].name}'",'option framerate 1000'])
            manifest.write_text('\n'.join(lines)+'\n',encoding='utf-8')
            # Only app-generated basenames/options reach this private manifest.
            args=video.ffmpeg_args()+['-f','concat','-safe','0','-i',str(manifest)]
            if info.has_audio:
                args+=['-ss',str(start),'-t',str(end-start),'-i',str(video.local_file(src)), '-map','0:v:0','-map','1:a:0']
            else:
                args+=['-map','0:v:0','-an']
            vf='scale=trunc(iw/2)*2:trunc(ih/2)*2,setsar=1'
            codec=(['-c:v','libvpx-vp9','-pix_fmt','yuva420p','-b:v','0','-crf','32',
                    '-deadline','good','-cpu-used','4','-auto-alt-ref','0','-c:a','libopus']
                   if target=='WEBM' else video._video_codec(target))
            if info.image:
                from .animation import timing_rate
                rate=timing_rate(durations)
                # Expand the concat frame durations before output -t sees EOF;
                # output -r alone can truncate a long final still frame.
                vf+=f',fps={rate:.8f}'
            else:
                rate=info.fps
            args+=['-vf',vf,'-r',f'{rate:.8f}',*(['-fps_mode','cfr'] if info.image else []),'-t',str(end-start),*codec,str(result)]
            video.run_process(args,end-start,'Encoding distorted animation…',.8,.2)


def _warp_frames(frames,warped,folder,w,h,strength):
    """Independent exact liblqr jobs; bound CPU use and cancel all active engines."""
    chunks=[frames[n:n+4] for n in range(0,len(frames),4)]
    workers=min(4,max(1,(os.cpu_count() or 2)//2),len(chunks))
    parent=video._control.get()
    abort=threading.Event()
    class Cancel:
        def is_set(self):
            return abort.is_set() or bool(parent and parent.cancel.is_set())
    def run(chunk):
        with video.job_context(video.Control(cancel=Cancel())):
            video.check_cancel()
            if strength==0:
                for path in chunk:
                    video.check_cancel()
                    shutil.copy2(path,warped/path.name)
            else:
                args=[video.executable('magick'),'mogrify','-path',str(warped),*_arguments(w,h,strength),
                      '-define','png:color-type=6','-define','png:compression-level=1',*[str(p) for p in chunk]]
                env=_environment(folder)
                env['MAGICK_THREAD_LIMIT']='1'
                video.run_process(args,message='Distorting frames…',parse_progress=False,env=env)
        return len(chunk)
    pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='distort')
    futures=[pool.submit(run,chunk) for chunk in chunks]
    completed=0
    try:
        for future in as_completed(futures):
            video.check_cancel()
            completed+=future.result()
            video.progress(.2+.6*completed/len(frames),'Distorting frames…')
    finally:
        abort.set()
        pool.shutdown(wait=True,cancel_futures=True)
