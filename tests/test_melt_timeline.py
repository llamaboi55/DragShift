import threading
import pytest
from dragshift import video
from dragshift.melt_timeline import Timeline, render
from dragshift.util import UserError


def test_trim_split_reorder_preserve_source_ranges_and_independent_cuts():
    model=Timeline()
    for name in ('A','B','C'):
        model.add(name+'.mp4',10)
    model.trim(0,2,8)
    model.cut(0).strength=23
    model.cut(1).duration=.7
    model.cut(1).strength=92
    right=model.split(1,4)
    assert [(c.start,c.end) for c in model.clips]==[(2,8),(0,4),(4,10),(0,10)]
    assert not model.cut(1).enabled
    assert model.cut(2).duration==.7 and model.cut(2).strength==92
    model.move(3,0)
    assert [c.path.stem for c in model.clips]==['C','A','B','B']
    assert model.cut(1).strength==23
    assert model.locate(17)==(2,1)
    clone=model.snapshot()
    clone.trim(0,1,5)
    assert model.clips[0].start==0
    with pytest.raises(UserError):model.split(0,.05)
    model.validate()


def test_short_preview_only_contains_selected_cut_and_limited_excerpts():
    model=Timeline()
    for name in ('A','B','C'):
        model.add(name+'.mp4',60,10,50)
    model.cut(1).duration=1.2
    model.cut(1).strength=81
    preview=model.preview(1)
    assert [c.path.stem for c in preview.clips]==['B','C']
    assert [(c.start,c.end) for c in preview.clips]==[(48.5,50),(10,11.95)]
    assert preview.cut(0).strength==81
    assert preview.duration==pytest.approx(3.45)
    preview.trim(1,10,10.2)
    with pytest.raises(UserError,match='Transition 1'):
        preview.validate()


@pytest.fixture(scope='module')
def sources(tmp_path_factory, media_engines):
    folder=tmp_path_factory.mktemp('pure-melt-timeline')
    paths=[]
    for index,kind in enumerate(('testsrc2','testsrc','testsrc2')):
        path=folder/f'part-{index}.mp4'
        video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
            f'{kind}=size=160x120:rate=30:duration=3','-f','lavfi','-i',
            f'sine=frequency={330+330*index}:duration=3','-c:v','libx264','-pix_fmt','yuv420p',
            '-c:a','aac','-y',str(path)])
        paths.append(path)
    return paths


def test_real_multi_clip_melt_has_no_clean_blend_and_retains_audio_timing(sources,monkeypatch):
    model=Timeline()
    for path in sources:model.add(path,3,.25,2.75)
    model.cut(0).duration=.5
    model.cut(0).strength=25
    model.cut(1).duration=1
    model.cut(1).strength=95
    commands=[]
    original=video.run_process
    def run(args,*positional,**kwargs):
        commands.append(args)
        return original(args,*positional,**kwargs)
    monkeypatch.setattr(video,'run_process',run)
    before=[p.read_bytes() for p in sources]
    out=render(model,max_side=160)[0]
    info=video.probe(out)
    assert info.duration==pytest.approx(7.5,abs=.06) and info.has_audio
    assert info.width==160 and info.height==120
    assert len(commands)==3 and not any('blend=' in str(arg) for cmd in commands for arg in cmd)
    import json
    params=json.loads(commands[1][commands[1].index('-sp')+1])
    assert params['ranges']==[dict(start=75,end=90,strength=25),dict(start=150,end=180,strength=95)]
    assert [p.read_bytes() for p in sources]==before
    from dragshift import audio
    assert audio.probe(out).duration==pytest.approx(7.5,abs=.06)


def test_timeline_cancel_never_leaves_partial_result(sources):
    model=Timeline()
    for path in sources:model.add(path,3)
    before=set(sources[0].parent.iterdir())
    cancel=threading.Event()
    def progress(value,message):
        if value>=.4:cancel.set()
    with video.job_context(video.Control(cancel=cancel,progress=progress)):
        with pytest.raises(video.Cancelled):render(model,max_side=160)
    assert set(sources[0].parent.iterdir())==before


def test_timeline_ui_trims_splits_drags_and_keeps_each_cut_independent(sources,qt_session):
    import time
    from PySide6.QtCore import Qt,QPoint
    from PySide6.QtTest import QTest
    from dragshift.video_tools import DatamoshWindow
    from dragshift.util import ALIVE
    def wait(predicate):
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            qt_session.processEvents()
            if predicate():return
            time.sleep(.005)
        assert predicate()
    window=DatamoshWindow(sources,lambda *_:None)
    window.show()
    panel=window.timeline_panel
    wait(lambda:len(panel.model.clips)==3 and not panel.loading)
    assert window.timeline_toggle.isChecked() and not window.classic_panel.isVisible()
    assert window.first_info is None and not window.audio_panel.paths
    panel.select('cut',0)
    assert panel.start.isHidden() and panel.end.isHidden()
    assert panel.duration.isVisible() and panel.strength.isVisible()
    panel.duration.setValue(.4)
    panel.strength.setValue(27)
    panel.select('cut',1)
    panel.duration.setValue(.8)
    panel.strength.setValue(91)
    panel.select('cut',0)
    assert panel.duration.value()==.4 and panel.strength.value()==27
    assert panel.model.cut(1).duration==.8 and panel.model.cut(1).strength==91
    panel.select('clip',1)
    assert panel.start.isVisible() and panel.end.isVisible()
    assert panel.duration.isHidden() and panel.strength.isHidden()
    panel.start.setValue(.5)
    panel.end.setValue(2.5)
    panel.end.editingFinished.emit()
    assert (panel.model.clips[1].start,panel.model.clips[1].end)==(.5,2.5)
    panel.view.set_playhead(4)
    panel.edit_buttons['Split at playhead'].click()
    assert len(panel.model.clips)==4 and not panel.model.cut(1).enabled
    assert panel.model.cut(2).strength==91
    qt_session.processEvents()
    view=panel.view
    uid=panel.model.clips[-1].uid
    rectangles=view.rectangles()
    QTest.mousePress(view,Qt.LeftButton,pos=rectangles[-1].center().toPoint())
    QTest.mouseMove(view,rectangles[0].center().toPoint())
    QTest.mouseRelease(view,Qt.LeftButton,pos=rectangles[0].center().toPoint())
    assert panel.model.clips[0].uid==uid
    rect=view.rectangles()[0]
    before=panel.model.clips[0].end
    handle=QPoint(round(rect.right()-2),70)
    QTest.mousePress(view,Qt.LeftButton,pos=handle)
    QTest.mouseMove(view,handle-QPoint(22,0))
    QTest.mouseRelease(view,Qt.LeftButton,pos=handle-QPoint(22,0))
    assert panel.model.clips[0].end<before-.3
    calls=[]
    window.submit=lambda *args:calls.append(args)
    panel.select('cut',0)
    panel.duration.setValue(.3)
    panel.save(preview=True)
    assert calls and calls[0][0]=='tool:Datamosh' and len(calls[0][1])==2
    info=video.probe(calls[0][2]()[0])
    assert info.duration<3 and info.has_audio
    window.resize(800,700)
    qt_session.processEvents()
    assert window.height()<=700
    window.close()
    wait(lambda:not ALIVE)
