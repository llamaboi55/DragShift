"""Telegram video emoji profile, verified before atomic output publication.

Official requirements: https://core.telegram.org/stickers/webm-vp9-encoding
100×100, WebM/VP9, no audio stream, at most 3 seconds / 30 fps / 256 KB.
"""
import json
import math

from . import video
from .util import UserError

MAX_BYTES=256_000


def validate_export(path):
    info=video.probe(path)
    data=json.loads(video.capture([video.executable('ffprobe'),'-v','error','-show_streams','-of','json',str(path)]))
    if ((info.width,info.height)!=(100,100) or info.codec!='vp9' or info.has_audio
            or info.duration>3.001 or info.fps>30.001 or path.stat().st_size>MAX_BYTES
            or any(s.get('codec_type')!='video' for s in data.get('streams',[]))):
        raise UserError('The encoded file did not meet Telegram emoji requirements. Try a shorter segment.')
    return info


def export_emoji(src, *, start=0., duration=None, fit='fit'):
    info=video.probe(src)
    duration=min(3.,info.duration-start) if duration is None else duration
    if (not all(math.isfinite(v) for v in (start,duration)) or not 0<duration<=3
            or start<0 or start+duration>info.duration+.001 or fit not in ('fit','crop')):
        raise UserError('Choose a segment within the source, no longer than 3 seconds.')
    source=video.local_file(src)
    decoder=['-c:v','libvpx-vp9'] if source.suffix.lower()=='.webm' and info.codec=='vp9' else []
    vf=video.square_pixels_filter()+',fps=30,format=rgba,'
    if fit=='crop':
        vf+='scale=100:100:force_original_aspect_ratio=increase:flags=lanczos,crop=100:100'
    else:
        vf+='scale=100:100:force_original_aspect_ratio=decrease:flags=lanczos,format=rgba,'
        vf+='pad=100:100:(ow-iw)/2:(oh-ih)/2:color=0x00000000'
    vf+=',setsar=1,format=yuva420p'
    with video.export_path(src,'Telegram emoji','.webm') as (tmp,outs):
        for attempt,quality in enumerate((32,42,52,63)):
            args=video.ffmpeg_args()+['-ss',str(start),*video.input_options(source),*decoder,'-i',str(source),'-t',str(duration),
                '-map',f'0:{info.video_index}','-an','-sn','-dn','-map_metadata','-1','-map_chapters','-1',
                '-vf',vf,'-c:v','libvpx-vp9','-pix_fmt','yuva420p','-b:v','0','-crf',str(quality),
                '-deadline','good','-cpu-used','4','-auto-alt-ref','0','-threads','4',str(tmp)]
            video.run_process(args,duration,'Encoding Telegram emoji…',attempt/4,1/4)
            if tmp.stat().st_size<=MAX_BYTES:
                validate_export(tmp)
                break
        else:
            raise UserError('This segment could not fit within 256 KB. Choose a shorter segment.')
    return outs
