"""Content-aware distortion inside the existing Shift+drag workflow."""

from .i18n import tr, tr_items
import io
from PySide6.QtCore import Qt,QTimer
from PySide6.QtWidgets import QCheckBox,QComboBox,QFormLayout,QHBoxLayout,QLabel,QSlider,QSpinBox

from . import distortion,video
from .media_controls import TrimRange,value_editor
from .util import category
from .video_tools import _AsyncTools,_field,_preview_label,_scroll_panel,_seconds,_show_frame


class DistortionWindow(_AsyncTools):
    tool_name='Distort'

    def __init__(self,paths,notify):
        super().__init__('Distort',paths,notify,(700,730))
        self.info=None
        self._request=0
        self._preview_busy=False
        self._source_cache=None
        self.details=QLabel(tr('Reading media…'))
        self.details.setObjectName('dim')
        self.body.addWidget(self.details)
        self.preview=_preview_label()
        self.preview.setAccessibleName(tr('Distorted frame preview'))
        self.body.addWidget(self.preview)
        self.scrub=QSlider(Qt.Horizontal)
        self.scrub.setRange(0,1000)
        self.scrub.setAccessibleName(tr('Frame position'))
        self.scrub.setEnabled(False)
        self.body.addWidget(self.scrub)
        self.original=QCheckBox(tr('Show original frame'))
        self.body.addWidget(self.original)
        form=QFormLayout()
        form.setVerticalSpacing(10)
        self.strength=QSlider(Qt.Horizontal)
        self.strength.setRange(0,95)
        self.strength.setValue(40)
        self.strength.setAccessibleName(tr('Distortion intensity'))
        self.strength_number=value_editor(self.strength,'Distortion intensity')
        row=QHBoxLayout()
        row.addWidget(self.strength,1)
        row.addWidget(self.strength_number)
        form.addRow(tr('Intensity'),row)
        self.max_side=QSpinBox()
        self.max_side.setRange(64,1920)
        self.max_side.setValue(640)
        self.max_side.setSuffix(tr(' px'))
        _field(form,'Longest edge (no upscale)',self.max_side)
        self.target=QComboBox()
        self.target.addItems(tr_items(distortion.TARGETS))
        _field(form,'Save as',self.target)
        self.at=_seconds()
        _field(form,'Frame position',self.at)
        self.start,self.end=_seconds(),_seconds()
        self.trim_range=TrimRange()
        self.segment_widgets=[]
        for label,widget in [('Start',self.start),('End',self.end)]:
            _field(form,label,widget)
            self.segment_widgets.extend((form.labelForField(widget),widget))
        form.addRow(tr(self.trim_range))
        self.segment_widgets.append(self.trim_range)
        self.body.addWidget(_scroll_panel(form),1)
        note=QLabel(tr('Liquid Rescale · 0 = original, 95 = strongest. Each frame is distorted separately. '
                    'PNG saves the selected frame; other formats save the segment. Drop media here to replace it.'))
        note.setObjectName('dim')
        note.setWordWrap(True)
        self.body.addWidget(note)
        if len(self.paths)>1:
            batch=QLabel(tr(f'Settings apply to all {len(self.paths)} files. Preview shows the first. '
                        'An unchanged range processes each file in full.'))
            batch.setWordWrap(True)
            self.body.addWidget(batch)
        self.body.addWidget(self.error)
        self.body.addLayout(self.footer('Save',self.save))
        self.primary_btn.setEnabled(False)
        self._preview_timer=QTimer(self,interval=200,singleShot=True)
        self._preview_timer.timeout.connect(self.load_frame)
        for control in (self.strength,self.max_side):
            control.valueChanged.connect(self._schedule_preview)
        self.original.toggled.connect(self._schedule_preview)
        self.target.currentTextChanged.connect(self._target_changed)
        self.at.valueChanged.connect(self._time_changed)
        self.scrub.valueChanged.connect(self._scrub_changed)
        self.start.valueChanged.connect(self._sync_range)
        self.end.valueChanged.connect(self._sync_range)
        self.trim_range.changed.connect(self._range_changed)
        path=self.path
        self.lookup(lambda:distortion.probe(path),self._loaded)

    def accepts_sources(self,paths):
        return bool(paths) and all(category(p) in ('image','video') for p in paths)

    def _loaded(self,info):
        self.info=info
        self.details.setText(tr(f'{info.width} × {info.height} · {video.time_text(info.duration)} · '
                             f'{info.fps:.2f} fps' if info.animated else f'{info.width} × {info.height} · Still image'))
        self.end.setMaximum(info.duration)
        self.start.setMaximum(max(0,info.duration-.02))
        self.end.setValue(info.duration)
        self.at.setMaximum(max(0,info.duration-1/max(1,info.fps)))
        self.scrub.setEnabled(info.animated)
        self.at.setEnabled(info.animated)
        self.target.setCurrentText('GIF' if info.image and info.animated else 'PNG' if info.image else 'MP4')
        self._target_changed()
        self.primary_btn.setEnabled(True)
        self._schedule_preview()

    def _target_changed(self,*_):
        segment=bool(self.info and self.info.animated and self.target.currentText()!='PNG')
        for widget in self.segment_widgets:
            widget.setVisible(segment)
        self.primary_btn.setText(tr('Save PNG frame' if self.target.currentText()=='PNG' else f'Save {self.target.currentText()}'))

    def _sync_range(self,*_):
        if self.info:
            self.trim_range.set_values(self.start.value(),self.end.value(),self.info.duration)

    def _range_changed(self,start,end):
        for spin,value in ((self.start,start),(self.end,end)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        if self.info:
            self.at.setValue(start if self.trim_range.active==0 else max(start,end-1/max(1,self.info.fps)))

    def _scrub_changed(self,value):
        if self.info:
            self.at.setValue(value/1000*self.at.maximum())

    def _time_changed(self,value):
        self.scrub.blockSignals(True)
        self.scrub.setValue(round(1000*value/max(.001,self.at.maximum())))
        self.scrub.blockSignals(False)
        self._schedule_preview()

    def _schedule_preview(self,*_):
        self._request+=1
        self._preview_timer.start()

    def load_frame(self):
        self._preview_timer.stop()
        if not self.info or self._preview_busy:
            return
        self._preview_busy=True
        request=self._request
        path,at=self.path,self.at.value()
        strength=0 if self.original.isChecked() else self.strength.value()
        size=self.max_side.value()
        def complete(data=None,error=None):
            self._preview_busy=False
            if request==self._request:
                if error:
                    self.preview.setText(tr(error))
                else:
                    _show_frame(self.preview,data)
            else:
                self._preview_timer.start(0)
        try:
            stat=path.stat()
        except OSError:
            self._preview_busy=False
            self.preview.setText(tr('The file was moved or deleted.'))
            return
        key=(at,size,stat.st_mtime_ns,stat.st_size)
        cached=self._source_cache
        info=self.info
        def render():
            frame=cached[1] if cached and cached[0]==key else distortion.source_frame(path,at,size,info=info)
            result=distortion.distort_frame(frame,strength=strength,max_side=size)
            data=io.BytesIO()
            result.save(data,'PNG',compress_level=1)
            return frame,data.getvalue()
        def rendered(value):
            frame,data=value
            self._source_cache=(key,frame)
            complete(data=data)
        self.lookup(render,rendered,lambda error:complete(error=error))

    def save(self):
        if not self.info:
            return
        start,end=self.start.value(),self.end.value()
        target=self.target.currentText()
        if target!='PNG' and end-start<.02:
            self.error.setText(tr('End must be after start.'))
            return
        # The default full range means each batch source's own complete duration.
        full=start==0 and abs(end-self.info.duration)<.001
        options=dict(strength=self.strength.value(),max_side=self.max_side.value(),target=target,
                     at=self.at.value(),start=0 if full or target=='PNG' else start,
                     end=None if full or target=='PNG' else end)
        paths=list(self.paths)
        fn=lambda:video.batch(paths,lambda path:distortion.export(path,**options))
        if self.submit:
            self.submit('tool:Distort',paths,fn,'Distorting frames…',f'Intensity {options["strength"]}')
        else:
            try:
                self.notify('tool:Distort',paths,fn())
            except Exception as error:
                self.error.setText(tr(str(error)))
                return
        self.close()

    def closeEvent(self,event):
        self._preview_timer.stop()
        super().closeEvent(event)
