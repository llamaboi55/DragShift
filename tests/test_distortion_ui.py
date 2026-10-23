import io,time
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dragshift.app import DragShiftApp
from dragshift.distortion_tools import DistortionWindow


@pytest.fixture(autouse=True)
def drain_workers(qt_session):
    yield
    from dragshift.util import ALIVE
    wait(lambda:not ALIVE)


def wait(predicate):
    deadline=time.monotonic()+15
    while time.monotonic()<deadline:
        QApplication.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def source(tmp_path):
    path=tmp_path/'first.gif'
    Image.new('RGB',(100,80),'red').save(path,save_all=True,
        append_images=[Image.new('RGB',(100,80),'blue')],duration=[200,300],loop=0)
    return path


def ready(tmp_path):
    window=DistortionWindow([source(tmp_path)],lambda *_:None)
    wait(lambda:window.info is not None and not window._preview_busy and not window.preview.pixmap().isNull())
    return window


def test_radial_routing_and_keyboard_parameters(qt_session,tmp_path):
    for filename in ('file.gif','file.mp4','file.png'):
        rings=DragShiftApp.build_rings(None,[filename])
        assert 'tool:Distort' in {item.key for item in rings['Tools']}
    window=ready(tmp_path)
    window.show()
    window.strength_number.setFocus()
    window.strength_number.selectAll()
    QTest.keyClicks(window.strength_number,'73')
    QTest.keyClick(window.strength_number,Qt.Key_Return)
    assert window.strength.value()==73
    assert window.isVisible()
    window.at.setValue(.25)
    window.target.setCurrentText('PNG')
    submissions=[]
    window.submit=lambda *args:submissions.append(args)
    window.save()
    assert submissions[0][0]=='tool:Distort'
    outputs=submissions[0][2]()
    with Image.open(outputs[0]) as frame:
        assert frame.getpixel((50,40))[:3]==(0,0,255)
    wait(lambda:not window._callbacks)


def test_drop_replaces_actual_source_and_preserves_tool(qt_session,tmp_path):
    window=ready(tmp_path)
    replacement=tmp_path/'chosen.png'
    Image.new('RGB',(90,70),'green').save(replacement)
    assert window.accepts_sources([replacement]) and not window.accepts_sources([tmp_path/'audio.mp3'])
    window.receive_files([str(replacement)])
    other=window._replacement
    wait(lambda:other.info is not None)
    assert other.path==replacement and other.target.currentText()=='PNG'
    other.close()
    wait(lambda:not other._callbacks)


def test_preview_coalesces_and_ignores_old_result(qt_session,tmp_path,monkeypatch):
    window=ready(tmp_path)
    calls=[]
    monkeypatch.setattr(window,'lookup',lambda fn,done,failure=None:calls.append((fn,done,failure)))
    window.strength.setValue(20)
    window.load_frame()
    assert len(calls)==1
    window.strength.setValue(80)
    window.load_frame()
    assert len(calls)==1
    calls[0][1]((Image.new('RGBA',(20,20)),b'old invalid image'))
    wait(lambda:len(calls)==2)
    assert window.preview.text()!='Frame preview unavailable'
    data=io.BytesIO()
    Image.new('RGB',(20,20),'red').save(data,'PNG')
    calls[1][1]((Image.new('RGBA',(20,20)),data.getvalue()))
    assert not window._preview_busy and not window.preview.pixmap().isNull()
    window.close()


def test_strength_preview_reuses_decoded_source(qt_session,tmp_path,monkeypatch):
    from dragshift import distortion
    window=ready(tmp_path)
    reads=[]
    original=distortion.source_frame
    def read(*args,**kwargs):
        reads.append(args)
        return original(*args,**kwargs)
    monkeypatch.setattr(distortion,'source_frame',read)
    window.strength.setValue(85)
    window.load_frame()
    wait(lambda:not window._preview_busy)
    assert not reads
    window.at.setValue(.25)
    window.load_frame()
    wait(lambda:not window._preview_busy)
    assert len(reads)==1
    window.close()
