"""Digital crunch through FFmpeg's acrusher, with a time-dependent dry/wet mix."""
from . import audio,datamosh,video
from .util import UserError


def export(src,*,strength=65,curve=None,start=0.,end=None,target='WAV',preview_seconds=None):
    video.check_cancel()
    info=audio.probe(src)
    target=audio._target(target)
    strength=audio._setting(strength,'glitch intensity',0,100)
    start=audio._setting(start,'start time',0,info.duration)
    end=info.duration if end is None else audio._setting(end,'end time',0,info.duration)
    if end-start<.02:
        raise UserError('Choose a start before the end of the audio.')
    try:
        points=datamosh.validate_curve(curve)
    except (ValueError,TypeError) as error:
        raise UserError('Choose a valid effect shape.') from error
    total=end-start
    length=total if preview_seconds is None else min(total,audio._setting(preview_seconds,'preview duration',.02,10))
    amount=f'({strength/100:.8f})*({datamosh.curve_expression(points,total,variable="if(isnan(t),0,t)")})'
    graph=(f'[0:a:0]asetpts=PTS-STARTPTS,asetnsamples=n=256:p=0,asplit=2[dry][wet];'
           f"[dry]volume='1-clip({amount},0,1)':eval=frame[d];"
           # Real sample-and-hold and bit reduction, not a volume-only effect.
           f"[wet]acrusher=bits=4:samples=16:aa=0:mix=1,volume='clip({amount},0,1)':eval=frame[w];"
           '[d][w]amix=inputs=2:normalize=0:duration=first[out]')
    with video.export_path(src,'Audio glitch preview' if preview_seconds else 'Audio glitch','.'+target.lower()) as (tmp,outs):
        args=video.ffmpeg_args()+['-ss',str(start),'-t',str(length),'-i',str(audio._file(src)),
            '-filter_complex_threads','2','-filter_complex',graph,'-map','[out]','-vn','-sn','-dn',
            '-map_metadata','0','-map_chapters','-1',
            *audio._codec(target,audio._rate(target,info.sample_rate),info.channels),str(tmp)]
        video.run_process(args,length,'Creating audio glitch…')
    return outs
