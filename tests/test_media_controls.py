import io

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QGridLayout, QSpinBox

from dragshift import video
from dragshift.media_controls import CurveEditor, TrimRange
from dragshift.tools import CropWindow, ToolWindow, slider, slider_row
from dragshift.video_tools import VideoToolWindow, _AsyncTools


def test_keyboard_value_and_enter_do_not_export(tmp_path):
    app = QApplication.instance()
    p = tmp_path/'image.png'
    Image.new('RGB',(40,30),'red').save(p)
    window = ToolWindow('Test', p, lambda *_: None)
    grid = QGridLayout()
    control = slider(-50,100,0)
    numeric = slider_row(grid,0,'Brightness',control,lambda v: f'{v}%')
    window.body.addLayout(grid)
    calls=[]
    window.body.addLayout(window.footer('Save',lambda: calls.append('saved')))
    window.show()
    numeric.setFocus()
    numeric.selectAll()
    QTest.keyClicks(numeric, '42')
    QTest.keyClick(numeric,Qt.Key_Return)
    app.processEvents()
    assert numeric.value()==42 and control.value()==42
    assert not calls
    control.setValue(-12)
    assert numeric.value()==-12
    window.close()


def test_drop_replaces_image_in_same_tool(tmp_path):
    a,b=tmp_path/'a.png',tmp_path/'b.png'
    Image.new('RGB',(60,40),'red').save(a)
    Image.new('RGB',(100,80),'blue').save(b)
    window=CropWindow(a,lambda *_:None)
    opened=[]
    window.replacement_handler=opened.append
    mime=QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(b))])
    event=QDropEvent(QPointF(50,50),Qt.CopyAction|Qt.MoveAction,mime,Qt.LeftButton,Qt.ShiftModifier)
    window.dropEvent(event)
    assert event.isAccepted() and event.dropAction()==Qt.CopyAction
    assert len(opened)==1 and opened[0].path==b and opened[0].full_size==(100,80)
    opened[0].close()
    window.close()


def test_trim_handle_and_numbers_stay_in_sync(monkeypatch,tmp_path):
    info=video.VideoInfo(160,120,10,30,False,'h264',0,{})
    frame=io.BytesIO()
    Image.new('RGB',(160,120),'red').save(frame,'PNG')
    monkeypatch.setattr(_AsyncTools,'lookup',lambda self,fn,cb,failure=None:cb((info,frame.getvalue())))
    window=VideoToolWindow([tmp_path/'a.mp4'],lambda *_:None,'Trim')
    window.trim_range.resize(500,48)
    window.trim_range.active=0
    window.trim_range._change(2)
    assert window.start_time.value()==2
    window.end_time.setValue(8)
    assert window.trim_range.end==8
    window.trim_range.active=1
    QTest.keyClick(window.trim_range,Qt.Key_Left)
    assert window.end_time.value()==pytest.approx(7.99)
    window.close()


def test_curve_direct_and_numeric_editing():
    editor=CurveEditor()
    editor.resize(500,220)
    editor.show()
    app=QApplication.instance()
    app.processEvents()
    r=editor.curve.area()
    position=QPoint(round(r.center().x()),round(r.center().y()))
    QTest.mouseClick(editor.curve,Qt.LeftButton,pos=position)
    QTest.mouseDClick(editor.curve,Qt.LeftButton,pos=position)
    assert len(editor.points())==3
    assert editor.points()[0]==(0.,1.) and editor.points()[-1]==(1.,1.)
    editor.level.setValue(25)
    assert editor.points()[1][1]==.25
    QTest.keyClick(editor.curve,Qt.Key_Delete)
    assert len(editor.points())==2
    editor.close()


def test_transformed_frame_preview(media_engines,tmp_path):
    clip=tmp_path/'source.mp4'
    video.capture([video.executable('ffmpeg'),'-v','error','-f','lavfi','-i',
                   'testsrc2=size=160x120:rate=30:duration=1','-c:v','libx264','-y',str(clip)])
    source=Image.open(io.BytesIO(video.preview_bytes(clip,.2)))
    preview=Image.open(io.BytesIO(video.preview_bytes(clip,.2,rotation=90,brightness=.3)))
    assert preview.size==(120,160)
    assert sum(preview.convert('RGB').resize((1,1)).getpixel((0,0))) > sum(source.convert('RGB').resize((1,1)).getpixel((0,0)))+80
