import io
import json
import pytest
from PIL import Image

from dragshift import video


@pytest.mark.parametrize('kind',['gif','mp4'])
def test_telegram_emoji_profile_and_motion(tmp_path,media_engines,kind):
    from dragshift import telegram
    source=tmp_path/('animation.'+kind)
    if kind=='gif':
        frames=[Image.new('RGBA',(80,60),c) for c in ('red','blue')]
        frames[0].save(source,save_all=True,append_images=frames[1:],duration=[500,500],loop=0)
    else:
        video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
                       'testsrc2=size=160x120:rate=60:duration=4','-f','lavfi','-i',
                       'sine=frequency=440:duration=4','-c:v','libx264','-c:a','aac','-y',str(source)])
    before=source.read_bytes()
    out=telegram.export_emoji(source)[0]
    assert out.suffix=='.webm' and out.stat().st_size<=256000
    info=video.probe(out)
    assert (info.width,info.height,info.codec)==(100,100,'vp9')
    assert info.duration<=3.001 and info.fps<=30 and not info.has_audio
    raw=json.loads(video.capture([video.executable('ffprobe'),'-v','error','-show_streams','-of','json',str(out)]))
    assert [s['codec_type'] for s in raw['streams']]==['video']
    a=video.preview_bytes(out,.1)
    b=video.preview_bytes(out,.7)
    assert a!=b
    assert source.read_bytes()==before


def test_invalid_emoji_range_publishes_nothing(tmp_path,media_engines):
    from dragshift import telegram
    from dragshift.util import UserError
    source=tmp_path/'clip.gif'
    Image.new('RGB',(100,100),'red').save(source,save_all=True,
        append_images=[Image.new('RGB',(100,100),'blue')],duration=500)
    before=set(tmp_path.iterdir())
    with pytest.raises(UserError):
        telegram.export_emoji(source,start=.9,duration=2)
    assert set(tmp_path.iterdir())==before
