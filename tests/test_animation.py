import io

import pytest
from PIL import Image, ImageSequence

from dragshift import convert, imaging, video


@pytest.fixture
def gif(tmp_path):
    path = tmp_path / 'animated.gif'
    frames = [Image.new('RGBA', (80, 60), color) for color in ('red', 'green', 'blue')]
    frames[0].save(path, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=2, disposal=2)
    return path


def test_crop_gif_keeps_frames_timing_loop_and_format(gif):
    before = gif.read_bytes()
    out = imaging.crop_image(gif, (10, 10, 50, 40))
    assert out.suffix == '.gif'
    with Image.open(out) as result:
        assert result.format == 'GIF' and result.n_frames == 3
        assert result.size == (40, 30) and result.info['loop'] == 2
        frames = [(f.convert('RGB').getpixel((5, 5)), f.info['duration']) for f in ImageSequence.Iterator(result)]
        assert [x[1] for x in frames] == [100, 200, 300]
        assert frames[0][0][0] > 240 and frames[-1][0][2] > 240
    assert gif.read_bytes() == before


@pytest.mark.parametrize('target', ['WEBP', 'PNG', 'TIFF', 'JPG', 'BMP', 'HEIC', 'AVIF', 'ICO'])
def test_gif_image_conversion_extracts_every_frame_beside_source(gif, target, monkeypatch):
    from dragshift.util import CONFIG
    monkeypatch.setitem(CONFIG, 'output', 'folder')
    out = convert.image_to(gif, target)[0]
    assert out.is_dir() and out.parent == gif.parent
    images = sorted(out.iterdir())
    assert [p.name for p in images] == [f'frame-{n:06d}.{target.lower()}' for n in (1,2,3)]
    colors = []
    for path in images:
        with Image.open(path) as result:
            assert getattr(result,'n_frames',1) == 1
            colors.append(result.convert('RGB').getpixel((5,5)))
    assert colors[0][0] > 210 and colors[-1][2] > 210


def test_gif_frames_keep_existing_folders_and_cancel_atomically(gif, monkeypatch):
    import threading
    first=convert.image_to(gif,'PNG')[0]
    (first/'keep.txt').write_text('keep')
    second=convert.image_to(gif,'PNG')[0]
    assert first!=second and (first/'keep.txt').read_text()=='keep'
    before=set(gif.parent.iterdir())
    cancel=threading.Event()
    def update(value,message):
        if value>0:
            cancel.set()
    with video.job_context(video.Control(cancel=cancel,progress=update)):
        with pytest.raises(video.Cancelled):
            convert.image_to(gif,'JPG')
    assert set(gif.parent.iterdir())==before


def test_extracted_gif_frames_use_composited_disposal_and_transparency(tmp_path):
    from PIL import ImageChops,ImageDraw
    source=tmp_path/'moving.gif'
    frames=[]
    for x in (0,20,40):
        frame=Image.new('RGBA',(60,40),(0,0,0,0))
        ImageDraw.Draw(frame).rectangle((x,10,x+10,25),fill='red')
        frames.append(frame)
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=[80,120,240],loop=0,disposal=2)
    folder=convert.image_to(source,'PNG')[0]
    with Image.open(source) as original:
        for index,path in enumerate(sorted(folder.iterdir())):
            original.seek(index)
            with Image.open(path) as actual:
                assert ImageChops.difference(original.convert('RGBA'),actual.convert('RGBA')).getbbox() is None


def test_gif_to_webm_retains_animation(gif, media_engines):
    out = convert.convert(gif, 'image', 'WEBM')[0]
    info = video.probe(out)
    assert info.codec == 'vp9' and abs(info.duration - .6) < .08
    frames = [Image.open(io.BytesIO(video.preview_bytes(out, at))).convert('RGB').getpixel((10, 10)) for at in (.05, .45)]
    assert frames[0][0] > 220 and frames[1][2] > 220


def test_short_gif_frames_keep_their_timestamps(tmp_path,media_engines):
    import json
    source=tmp_path/'quick.gif'
    frames=[Image.new('RGB',(80,60),c) for c in ('red','green','blue')]
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=[10,30,460],disposal=2)
    from dragshift import distortion
    for out in (convert.convert(source,'image','WEBM')[0],distortion.export(source,strength=0,target='WEBM')[0]):
        assert video.probe(out).duration==pytest.approx(.5,abs=.02)
        data=json.loads(video.capture([video.executable('ffprobe'),'-v','error','-show_frames','-select_streams','v:0',
                                      '-show_entries','frame=pts_time','-of','json',str(out)]))
        times=[float(f['pts_time']) for f in data['frames']]
        assert all(any(abs(t-expected)<.005 for t in times) for expected in (0,.01,.04))
        raw=video.capture([video.executable('ffmpeg'),'-v','error','-i',str(out),'-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
        colors=[]
        for expected in (0,.01,.04):
            index=next(n for n,t in enumerate(times) if abs(t-expected)<.005)
            offset=index*80*60*3+(30*80+40)*3
            colors.append(raw[offset:offset+3])
        assert colors[0][0]>220 and colors[1][1]>110 and colors[2][2]>220


def test_resize_gif_retains_animation(gif):
    out = imaging.resize_image(gif, (40, 30))
    with Image.open(out) as result:
        assert result.format == 'GIF' and result.n_frames == 3 and result.size == (40, 30)


def test_transparent_disposal_frames_are_not_merged_or_ghosted(tmp_path):
    from PIL import ImageDraw
    source=tmp_path/'moving.gif'
    frames=[]
    for x in (0,20,40):
        frame=Image.new('RGBA',(60,40),(0,0,0,0))
        ImageDraw.Draw(frame).rectangle((x,10,x+10,25),fill='red')
        frames.append(frame)
    frames[0].save(source,save_all=True,append_images=frames[1:],duration=[80,120,240],loop=0,disposal=2)
    out=imaging.crop_image(source,(0,5,60,35))
    with Image.open(source) as original, Image.open(out) as result:
        assert result.n_frames==3
        for i in range(3):
            original.seek(i)
            result.seek(i)
            from PIL import ImageChops
            expected=original.convert('RGBA').crop((0,5,60,35))
            actual=result.convert('RGBA')
            assert ImageChops.difference(expected,actual).getbbox() is None


def test_edit_and_compress_gif_remain_animated(gif):
    for out in (imaging.edit_image(gif,imaging.Adjust(brightness=1.2)),
                imaging.compress_to(gif,70,40,'GIF'),imaging.strip_image_metadata(gif)):
        with Image.open(out) as image:
            assert image.format=='GIF' and image.n_frames==3
