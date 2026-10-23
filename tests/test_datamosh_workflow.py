import time
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest
from dragshift import audio,video
from dragshift.app import DragShiftApp
from dragshift.media_controls import EffectEnvelope
from dragshift.video_tools import DatamoshWindow


def wait(predicate):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture(autouse=True)
def drain(qt_session):
    yield
    from dragshift.util import ALIVE
    wait(lambda:not ALIVE)


def test_simple_shape_does_not_require_drawing_points():
    widget=EffectEnvelope()
    widget.show()
    QApplication.processEvents()
    assert widget.shape.currentData()=='constant' and not widget.editor.isVisible()
    for key,expected in [('build',[(0,0),(1,1)]),('fade',[(0,1),(1,0)]),('pulse',[(0,0),(.5,1),(1,0)])]:
        widget.shape.setCurrentIndex(widget.shape.findData(key))
        assert widget.points()==expected and not widget.editor.isVisible()
    widget.shape.setCurrentIndex(widget.shape.findData('custom'))
    widget.set_duration(12)
    QApplication.processEvents()
    assert widget.editor.isVisible() and widget.editor.time.suffix()==' s'
    curve=widget.curve
    point=curve.position((.25,.6)).toPoint()
    QTest.mouseClick(curve,Qt.LeftButton,pos=point)
    assert len(widget.points())==4
    assert widget.editor.time.value()==pytest.approx(3,abs=.1)
    widget.close()


def test_audio_glitch_is_inside_datamosh_and_switch_keeps_settings(tmp_path,media_engines):
    source=tmp_path/'sound.wav'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i','sine=frequency=997:duration=1',
                   '-y',str(source)])
    assert 'tool:Datamosh' in {item.key for item in DragShiftApp.build_rings(None,[str(source)])['Tools']}
    window=DatamoshWindow([source],lambda *_:None)
    window.show()
    panel=window.audio_panel
    wait(lambda:panel.info is not None)
    assert window.media_mode.currentData()=='audio'
    assert panel.window() is window and panel.isVisible() and not window.video_panel.isVisible()
    panel.strength.setValue(83)
    panel.envelope.shape.setCurrentIndex(1)
    window.media_mode.setCurrentIndex(0)
    window.media_mode.setCurrentIndex(1)
    assert panel.strength.value()==83 and panel.envelope.shape.currentData()=='build'
    panel.envelope.shape.setCurrentIndex(4)
    window.resize(800,700)
    QApplication.processEvents()
    assert window.height()<=700
    panel.envelope.shape.setCurrentIndex(1)
    calls=[]
    window.submit=lambda *args:calls.append(args)
    panel.save()
    assert calls[0][0]=='tool:Audio glitch'
    assert audio.probe(calls[0][2]()[0]).duration==pytest.approx(1,abs=.01)


def test_custom_point_hit_area_does_not_jump_or_silently_change_selection():
    envelope=EffectEnvelope()
    envelope.shape.setCurrentIndex(4)
    envelope.resize(600,330)
    envelope.show()
    QApplication.processEvents()
    curve=envelope.curve
    before=list(curve.points)
    point=curve.position(curve.points[0]).toPoint()+QPoint(12,6)
    QTest.mouseClick(curve,Qt.LeftButton,pos=point)
    assert curve.points==before and curve.index==0
    point=curve.position((.55,.3)).toPoint()
    QTest.mouseClick(curve,Qt.LeftButton,pos=point)
    assert len(curve.points)==3 and curve.index==1
    QTest.keyClick(curve,Qt.Key_Delete)
    assert curve.points==before
    envelope.close()
