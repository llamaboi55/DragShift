import io,time,hashlib
import pytest
from PIL import Image,ImageChops
from PySide6.QtWidgets import QApplication
from dragshift import video
from dragshift.video_geometry import VideoResizeWindow
from dragshift.util import UserError


@pytest.fixture
def clip(tmp_path,media_engines):
    source=tmp_path/'source.mp4'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
        'testsrc2=size=320x240:rate=10:duration=1','-f','lavfi','-i','sine=frequency=440:duration=1',
        '-c:v','libx264','-c:a','aac','-y',str(source)])
    return source


def wait(predicate):
    end=time.monotonic()+15
    while time.monotonic()<end:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def test_pixel_resize_and_crop_matches_preview(clip):
    before=clip.read_bytes()
    options=dict(crop_box=(.125,.1,.875,.9),output_size=(160,128))
    out=video.transform(clip,**options)[0]
    info=video.probe(out)
    assert (info.width,info.height)==(160,128) and info.has_audio and info.duration==pytest.approx(1,abs=.05)
    expected=Image.open(io.BytesIO(video.preview_bytes(clip,.2,**options))).convert('RGB')
    actual=Image.open(io.BytesIO(video.preview_bytes(out,.2))).convert('RGB')
    assert actual.size==expected.size==(160,128)
    assert sum(ImageChops.difference(actual,expected).resize((1,1)).getpixel((0,0)))<25
    assert clip.read_bytes()==before
    for bad in ({'output_size':(161,100)},{'crop_box':(.9,0,.2,1)},{'output_size':(8000,100)}):
        with pytest.raises(UserError):
            video.transform(clip,**bad)


def test_resize_ui_dimensions_proportions_crop_and_export(clip,qt_session):
    window=VideoResizeWindow([clip],lambda *_:None)
    window.show()
    wait(lambda:window.info is not None)
    window.width_px.setValue(160)
    assert window.height_px.value()==120
    window.edges['Left'].setValue(40)
    window.edges['Right'].setValue(40)
    assert window.height_px.value()==160
    window.lock.setChecked(False)
    window.height_px.setValue(96)
    calls=[]
    window.submit=lambda *args:calls.append(args)
    window.save()
    out=calls[0][2]()[0]
    assert (video.probe(out).width,video.probe(out).height)==(160,96)
    from dragshift.util import ALIVE
    wait(lambda:not ALIVE)


@pytest.mark.parametrize('kind',['resize','edit'])
def test_scrubbing_never_queues_multiple_frame_decoders(clip,qt_session,monkeypatch,kind):
    from dragshift.video_tools import VideoToolWindow
    window=VideoResizeWindow([clip],lambda *_:None) if kind=='resize' else VideoToolWindow([clip],lambda *_:None,'Edit')
    try:
        wait(lambda:window.info is not None and not window._callbacks)
        calls=[]
        monkeypatch.setattr(window,'lookup',lambda fn,done,failure=None:calls.append((fn,done,failure)))
        load=window._load_frame if kind=='resize' else window.load_frame
        window.scrub.setValue(200)
        load()
        window.scrub.setValue(800)
        load()
        assert len(calls)==1
        calls[0][2]('stale preview failed')
        wait(lambda:len(calls)==2)
        calls[1][1](video.preview_bytes(clip,.8))
        assert not window._preview_busy
    finally:
        window.close()


def test_resize_reset_restores_dimensions_after_unlocked_edit(clip,qt_session):
    window=VideoResizeWindow([clip],lambda *_:None)
    try:
        wait(lambda:window.info is not None)
        window.lock.setChecked(False)
        window.width_px.setValue(160)
        window.height_px.setValue(96)
        window.edges['Left'].setValue(40)
        window.reset()
        assert (window.width_px.value(),window.height_px.value())==(320,240)
        assert window.lock.isChecked() and not any(spin.value() for spin in window.edges.values())
    finally:
        window.close()
