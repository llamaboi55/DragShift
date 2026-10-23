"""Pixel resize and edge crop on the actual displayed video frame."""

from .i18n import tr, tr_items
from PySide6.QtCore import QRectF,Qt,QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QCheckBox,QFormLayout,QGridLayout,QHBoxLayout,QLabel,QSlider,QSpinBox
from . import video
from .tools import CropView
from .video_tools import _AsyncTools,_field,_scroll_panel


class VideoResizeWindow(_AsyncTools):
    tool_name='Resize'
    def __init__(self,paths,notify):
        super().__init__('Video · Resize and crop',paths,notify,(820,790))
        self.info=None
        self.serial=0
        self._preview_busy=False
        self.details=QLabel(tr('Reading video…'))
        self.details.setWordWrap(True)
        self.body.addWidget(self.details)
        blank=QImage(320,240,QImage.Format_RGB32)
        blank.fill(Qt.black)
        self.view=CropView(blank,1)
        self.view.setMinimumHeight(160)
        self.body.addWidget(self.view,1)
        self.scrub=QSlider(Qt.Horizontal)
        self.scrub.setRange(0,1000)
        self.scrub.setAccessibleName(tr('Frame position'))
        self.body.addWidget(self.scrub)
        form=QFormLayout()
        self.width_px,self.height_px=QSpinBox(),QSpinBox()
        for spin in (self.width_px,self.height_px):
            spin.setRange(16,7680)
            spin.setSingleStep(2)
            spin.setSuffix(tr(' px'))
            spin.setKeyboardTracking(False)
        dimensions=QHBoxLayout()
        for label,spin in [('Width',self.width_px),('Height',self.height_px)]:
            dimensions.addWidget(QLabel(tr(label)))
            spin.setAccessibleName(tr('Output '+label.lower()))
            dimensions.addWidget(spin,1)
        form.addRow(tr('Output size'),dimensions)
        self.lock=QCheckBox(tr('Keep proportions'))
        self.lock.setChecked(True)
        form.addRow(tr(self.lock))
        self.edges={}
        crop_fields=QGridLayout()
        for index,name in enumerate(('Left','Right','Top','Bottom')):
            spin=QSpinBox()
            spin.setRange(0,100000)
            spin.setSuffix(tr(' px'))
            spin.setKeyboardTracking(False)
            self.edges[name]=spin
            spin.setAccessibleName(tr(f'Crop {name.lower()}'))
            crop_fields.addWidget(QLabel(tr(name)),index//2,(index%2)*2)
            crop_fields.addWidget(spin,index//2,(index%2)*2+1)
            spin.valueChanged.connect(self._typed_crop)
        form.addRow(tr('Crop borders'),crop_fields)
        parameters=_scroll_panel(form)
        parameters.setMinimumHeight(140)
        self.body.addWidget(parameters,1)
        self.output_hint=QLabel()
        self.body.addWidget(self.output_hint)
        note=QLabel(tr('Drag the frame edges to crop, or enter the amounts above. Double-click the picture to reset. '
                    'Output dimensions are even for MP4. Settings apply to every selected video.'))
        note.setWordWrap(True)
        note.setObjectName('dim')
        self.body.addWidget(note)
        self.body.addWidget(self.error)
        self.body.addLayout(self.footer('Save MP4',self.save,on_reset=self.reset))
        self.primary_btn.setEnabled(False)
        self.width_px.valueChanged.connect(lambda:self._size_changed('width'))
        self.height_px.valueChanged.connect(lambda:self._size_changed('height'))
        self.lock.toggled.connect(lambda:self._size_changed('width'))
        self.view.changed.connect(self._crop_changed)
        self.scrub.valueChanged.connect(self._schedule)
        self.timer=QTimer(self,interval=200,singleShot=True)
        self.timer.timeout.connect(self._load_frame)
        path=self.path
        self.lookup(lambda:(video.probe(path),video.preview_bytes(path,max_side=1000)),self._loaded)

    def _loaded(self,data):
        self.info,frame=data
        self.full_w=max(2,int(self.info.display_width)//2*2)
        self.full_h=max(2,int(self.info.display_height)//2*2)
        self.details.setText(tr(f'{self.full_w} × {self.full_h} · {video.time_text(self.info.duration)} · '
                             'Drag the crop border; set the final size below.'))
        image=QImage.fromData(frame)
        self.view.set_image(image)
        self.view.k=self.full_w/image.width()
        self.view.reset_sel()
        self.reset()
        self.primary_btn.setEnabled(True)

    def crop_box(self):
        r=self.view.sel
        return (r.left()/self.view.image.width(),r.top()/self.view.image.height(),
                r.right()/self.view.image.width(),r.bottom()/self.view.image.height())

    def _crop_changed(self):
        if not self.info:
            return
        self.error.clear()
        self.primary_btn.setEnabled(True)
        left,top,right,bottom=self.crop_box()
        values={'Left':left*self.full_w,'Right':(1-right)*self.full_w,
                'Top':top*self.full_h,'Bottom':(1-bottom)*self.full_h}
        for name,spin in self.edges.items():
            spin.blockSignals(True)
            spin.setValue(round(values[name]))
            spin.blockSignals(False)
        self._size_changed('width')

    def _typed_crop(self):
        if not self.info:
            return
        left,right,top,bottom=[self.edges[n].value() for n in ('Left','Right','Top','Bottom')]
        if left+right>self.full_w-2 or top+bottom>self.full_h-2:
            self.error.setText(tr('Leave at least two pixels inside the crop.'))
            self.primary_btn.setEnabled(False)
            return
        self.error.clear()
        self.primary_btn.setEnabled(True)
        w,h=self.view.image.width(),self.view.image.height()
        self.view.sel=QRectF(left/self.full_w*w,top/self.full_h*h,
                            (self.full_w-left-right)/self.full_w*w,(self.full_h-top-bottom)/self.full_h*h)
        self.view.update()
        self._size_changed('width')

    def _size_changed(self,source):
        if not self.info:
            return
        crop_w=self.view.sel.width()/self.view.image.width()*self.full_w
        crop_h=self.view.sel.height()/self.view.image.height()*self.full_h
        ratio=crop_w/max(.001,crop_h)
        width=max(16,self.width_px.value()//2*2)
        height=max(16,self.height_px.value()//2*2)
        if self.lock.isChecked():
            if source=='width':
                height=max(16,min(7680,round(width/ratio/2)*2))
            else:
                width=max(16,min(7680,round(height*ratio/2)*2))
        for spin,value in ((self.width_px,width),(self.height_px,height)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.output_hint.setText(tr(f'Output: {width} × {height} px'))

    def reset(self):
        if self.info:
            self.lock.setChecked(True)
            self.view.reset_sel()
            scale=min(1,7680/self.full_w,7680/self.full_h)
            self.width_px.setValue(max(16,int(self.full_w*scale)//2*2))
            self._size_changed('width')

    def _schedule(self,*_):
        self.serial+=1
        self.timer.start()

    def _load_frame(self):
        self.timer.stop()
        if not self.info or self._preview_busy:
            return
        self._preview_busy=True
        at=self.scrub.value()/1000*max(0,self.info.duration-1/self.info.fps)
        path=self.path
        serial=self.serial
        def complete(error=None):
            self._preview_busy=False
            if serial!=self.serial:
                self.timer.start(0)
            elif error:
                self.error.setText(tr('Frame preview unavailable'))
        def done(data):
            complete()
            if serial!=self.serial:
                return
            box=self.crop_box()
            image=QImage.fromData(data)
            if image.isNull():
                self.error.setText(tr('Frame preview unavailable'))
                return
            self.view.set_image(image)
            self.view.sel=QRectF(box[0]*image.width(),box[1]*image.height(),
                                (box[2]-box[0])*image.width(),(box[3]-box[1])*image.height())
            self.view.update()
        self.lookup(lambda:video.preview_bytes(path,at,max_side=1000),done,complete)

    def save(self):
        if not self.info or not self.primary_btn.isEnabled():
            return
        paths=list(self.paths)
        options=dict(crop_box=self.crop_box(),output_size=(self.width_px.value(),self.height_px.value()),label='Resized')
        self.run(lambda:video.batch(paths,lambda path:video.transform(path,**options)),'Resizing video…')

    def closeEvent(self,event):
        self.timer.stop()
        super().closeEvent(event)
