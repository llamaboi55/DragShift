import hashlib,io,subprocess,threading
from pathlib import Path

import pytest
from PIL import Image,ImageChops,ImageDraw,ImageSequence

from dragshift import video
from dragshift.util import UserError


@pytest.fixture(scope='module',autouse=True)
def liquid_engine():
    try:
        engine=video.executable('magick')
    except UserError as error:
        pytest.skip(str(error))
    version=video.capture([engine,'-version']).decode()
    if ' lqr ' not in version:
        pytest.skip('ImageMagick was built without Liquid Rescale')


def picture(color='red'):
    image=Image.new('RGBA',(160,120),(225,225,225,255))
    draw=ImageDraw.Draw(image)
    for x in range(10,160,25):
        draw.ellipse((x,20,x+15,100),fill=color)
    draw.rectangle((60,10,85,110),fill='black')
    return image


def test_frame_matches_reference_liquid_rescale_command(tmp_path):
    from dragshift import distortion
    source=tmp_path/'input.png'
    expected=tmp_path/'reference.png'
    image=picture()
    image.save(source)
    video.capture([video.executable('magick'),str(source),'-liquid-rescale','60x60%!',
                   '-resize','160x120!',str(expected)])
    result=distortion.distort_frame(image,strength=40,max_side=160)
    with Image.open(expected) as reference:
        assert ImageChops.difference(result.convert('RGB'),reference.convert('RGB')).getbbox() is None
    assert ImageChops.difference(result,image).convert('RGB').getbbox() is not None
    assert ImageChops.difference(distortion.distort_frame(image,strength=0),image).getbbox() is None


def test_strength_changes_geometry_not_just_uniform_zoom():
    from dragshift import distortion
    image=picture()
    a=distortion.distort_frame(image,strength=20)
    b=distortion.distort_frame(image,strength=75)
    assert a.size==b.size==image.size
    assert a.tobytes()!=b.tobytes()
    for invalid in (-1,96,float('nan'),'bad'):
        with pytest.raises(UserError):
            distortion.distort_frame(image,strength=invalid)


def test_gif_animation_timing_alpha_and_original_preserved(tmp_path):
    from dragshift import distortion
    source=tmp_path/'moving.gif'
    frames=[picture(c) for c in ('red','blue','green')]
    for frame in frames:
        frame.putpixel((0,0),(0,0,0,0))
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=[100,200,300],loop=2,disposal=2)
    before=source.read_bytes()
    out=distortion.export(source,strength=60,target='GIF',max_side=160)[0]
    with Image.open(out) as result:
        assert result.n_frames==3 and result.size==(160,120) and result.info['loop']==2
        assert [frame.info['duration'] for frame in ImageSequence.Iterator(result)]==[100,200,300]
    assert source.read_bytes()==before


def test_video_frames_audio_and_selected_frame(tmp_path,media_engines):
    from dragshift import distortion
    source=tmp_path/'video.mp4'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
        'testsrc2=size=160x120:rate=10:duration=0.6','-f','lavfi','-i','sine=frequency=440:duration=0.6',
        '-c:v','libx264','-c:a','aac','-y',str(source)])
    before=hashlib.sha256(source.read_bytes()).hexdigest()
    out=distortion.export(source,strength=65,target='MP4',max_side=160)[0]
    info=video.probe(out)
    assert (info.width,info.height)==(160,120) and info.has_audio
    assert info.duration==pytest.approx(.6,abs=.06)
    assert video.preview_bytes(out,0)!=video.preview_bytes(out,.3)
    frame=distortion.export(source,strength=65,target='PNG',at=.3,max_side=160)[0]
    assert Image.open(frame).size==(160,120)
    assert hashlib.sha256(source.read_bytes()).hexdigest()==before


def test_cancel_does_not_publish_or_leave_staging(tmp_path):
    from dragshift import distortion
    source=tmp_path/'moving.gif'
    picture('red').save(source,save_all=True,append_images=[picture('blue')],duration=100)
    before=set(tmp_path.iterdir())
    cancel=threading.Event()
    def update(value,message):
        if value>=.2:
            cancel.set()
    with video.job_context(video.Control(cancel=cancel,progress=update)):
        with pytest.raises(video.Cancelled):
            distortion.export(source,strength=60,target='GIF',max_side=160)
    assert set(tmp_path.iterdir())==before


def test_cancel_during_parallel_render_joins_workers_and_cleans(tmp_path,monkeypatch):
    from dragshift import distortion
    monkeypatch.setattr(distortion.os,'cpu_count',lambda:8)
    source=tmp_path/'series.gif'
    frames=[picture((n*10,40,255-n*10)).resize((320,240)) for n in range(24)]
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=100,disposal=2)
    before=set(tmp_path.iterdir())
    cancel=threading.Event()
    processed=[]
    def update(value,message):
        if .2<value<.8:
            processed.append(value)
            cancel.set()
    with video.job_context(video.Control(cancel=cancel,progress=update)):
        with pytest.raises(video.Cancelled):
            distortion.export(source,strength=75,target='GIF',max_side=320)
    assert processed
    assert not any(t.name.startswith('distort_') for t in threading.enumerate())
    assert set(tmp_path.iterdir())==before


def test_high_frame_rate_and_portrait_bound(tmp_path,media_engines):
    import json
    from dragshift import distortion
    source=tmp_path/'portrait.mp4'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
        'testsrc2=size=120x240:rate=60:duration=0.3','-c:v','libx264','-y',str(source)])
    assert max(distortion.source_frame(source,max_side=160).size)<=160
    out=distortion.export(source,strength=0,target='MP4',max_side=160)[0]
    data=json.loads(video.capture([video.executable('ffprobe'),'-v','error','-count_frames',
        '-select_streams','v:0','-show_entries','stream=nb_read_frames','-of','json',str(out)]))
    assert int(data['streams'][0]['nb_read_frames'])==18
    assert video.probe(out).duration==pytest.approx(.3,abs=.02)
    gif=distortion.export(source,strength=0,target='GIF',max_side=160)[0]
    with Image.open(gif) as animation:
        assert sum(frame.info['duration'] for frame in ImageSequence.Iterator(animation))==300


def test_gif_selected_frame_webm_alpha_and_clip_timing(tmp_path,media_engines):
    from dragshift import distortion
    source=tmp_path/'transparent.gif'
    frames=[]
    for color in ('red','blue','green'):
        image=Image.new('RGBA',(160,120),(0,0,0,0))
        ImageDraw.Draw(image).rectangle((40,30,120,90),fill=color)
        frames.append(image)
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=[100,200,300],disposal=2,loop=0)
    png=distortion.export(source,strength=0,target='PNG',at=.15,max_side=160)[0]
    with Image.open(png) as image:
        assert image.getpixel((80,60))[:3]==(0,0,255)
    out=distortion.export(source,strength=40,target='WEBM',start=.1,end=.6,max_side=160)[0]
    info=video.probe(out)
    assert info.codec=='vp9' and not info.has_audio
    assert info.duration==pytest.approx(.5,abs=.11)
    frame=distortion.source_frame(out,0,max_side=160)
    assert frame.getchannel('A').getextrema()==(0,255)
