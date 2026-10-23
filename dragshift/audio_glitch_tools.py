"""Inline audio glitch controls hosted by the Datamosh window."""

from .i18n import tr, tr_items
from pathlib import Path
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QComboBox,QFileDialog,QFormLayout,QHBoxLayout,QLabel,QPushButton,QSlider,QVBoxLayout,QWidget

from . import audio,audio_glitch,video
from .media_controls import DropZone,EffectEnvelope,TrimRange,value_editor
from .util import category
from .video_tools import _field,_seconds,_scroll_panel


class AudioGlitchPanel(QWidget):
    def __init__(self,owner,paths):
        super().__init__(owner)
        self.owner=owner
        self.paths=[Path(p) for p in paths]
        self.info=None
        self.generation=0
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        hint=QLabel(tr('Crunch and break up the sound itself. Choose the strength and how it changes over time.'))
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.source=DropZone('Drop audio or a video with sound here',self.accept)
        self.source.filesDropped.connect(self.load)
        layout.addWidget(self.source)
        choose=QPushButton(tr('Choose audio…'))
        choose.clicked.connect(self.choose)
        layout.addWidget(choose)
        self.details=QLabel()
        self.details.setWordWrap(True)
        layout.addWidget(self.details)
        form=QFormLayout()
        self.strength=QSlider(Qt.Horizontal)
        self.strength.setRange(0,100)
        self.strength.setValue(65)
        row=QHBoxLayout()
        row.addWidget(self.strength,1)
        row.addWidget(value_editor(self.strength,'Audio glitch intensity',lambda n:f'{n}%'))
        form.addRow(tr('Intensity'),row)
        self.start,self.end=_seconds(),_seconds()
        _field(form,'Start',self.start)
        _field(form,'End',self.end)
        self.trim=TrimRange()
        form.addRow(tr(self.trim))
        self.target=QComboBox()
        self.target.addItems(tr_items(audio.TARGETS))
        self.target.setCurrentText('WAV')
        _field(form,'Save as',self.target)
        self.envelope=EffectEnvelope()
        form.addRow(self.envelope)
        note=QLabel(tr('Bit crushing and sample hold change the timbre. Zero is clean audio. '
                    'Saves an audio file; a video source contributes its first audio track.'))
        note.setObjectName('dim')
        note.setWordWrap(True)
        layout.addWidget(_scroll_panel(form,note),1)
        self.error=QLabel()
        self.error.setObjectName('videoError')
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        row=QHBoxLayout()
        self.preview_btn=QPushButton(tr('Preview first 10 seconds'))
        self.preview_btn.clicked.connect(lambda:self.save(preview=True))
        row.addWidget(self.preview_btn)
        row.addStretch()
        cancel=QPushButton(tr('Cancel'))
        cancel.clicked.connect(owner.close)
        row.addWidget(cancel)
        self.save_btn=QPushButton(tr('Save glitched audio'))
        self.save_btn.setObjectName('primary')
        self.save_btn.clicked.connect(self.save)
        row.addWidget(self.save_btn)
        layout.addLayout(row)
        self.trim.changed.connect(self._range_changed)
        self.start.valueChanged.connect(self._sync_range)
        self.end.valueChanged.connect(self._sync_range)
        self.load(self.paths)

    @staticmethod
    def accept(path):
        return category(path) in ('audio','video')

    def choose(self):
        paths,_=QFileDialog.getOpenFileNames(self,tr('Choose audio'),str(self.paths[0].parent) if self.paths else '',tr(audio.AUDIO_FILTER))
        if paths:
            self.load(paths)

    def load(self,paths):
        self.generation+=1
        generation=self.generation
        self.paths=[Path(p) for p in paths]
        self.info=None
        self.error.clear()
        self.save_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        if not paths:
            self.details.setText(tr('Choose an audio file or drop it above.'))
            return
        path=self.paths[0]
        self.source.setText(path.name if len(paths)==1 else tr('{0} audio sources · {1}').format(len(paths),path.name))
        def done(info):
            if generation!=self.generation:
                return
            self.info=info
            self.details.setText(tr(f'{video.time_text(info.duration)} · {info.sample_rate:,} Hz · {info.channels} ch'))
            self.start.setMaximum(max(0,info.duration-.02))
            self.start.setValue(0)
            self.end.setMaximum(info.duration)
            self.end.setValue(info.duration)
            self._sync_range()
            self.save_btn.setEnabled(True)
            self.preview_btn.setEnabled(True)
        self.owner.lookup(lambda:audio.probe(path),done,
                          lambda error:self.error.setText(tr(error)) if generation==self.generation else None)

    def _sync_range(self,*_):
        if self.info:
            self.trim.set_values(self.start.value(),self.end.value(),self.info.duration)
            self.envelope.set_duration(max(.02,self.end.value()-self.start.value()))

    def _range_changed(self,start,end):
        for spin,value in ((self.start,start),(self.end,end)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._sync_range()

    def save(self,checked=False,*,preview=False):
        if not self.info:
            return
        start,end=self.start.value(),self.end.value()
        if end-start<.02:
            self.error.setText(tr('Choose a start before the end of the audio.'))
            return
        full=start==0 and abs(end-self.info.duration)<.001
        paths=list(self.paths[:1] if preview else self.paths)
        options=dict(start=start,end=None if full else end,strength=self.strength.value(),
                     curve=self.envelope.points(),target='WAV' if preview else self.target.currentText(),
                     preview_seconds=10 if preview else None)
        fn=lambda:video.batch(paths,lambda path:audio_glitch.export(path,**options))
        if self.owner.submit:
            self.owner.submit('tool:Audio glitch',paths,fn,'Creating audio glitch…','10-second preview' if preview else '')
            if not preview:
                self.owner.close()
        else:
            try:
                self.owner.notify('tool:Audio glitch',paths,fn())
            except Exception as error:
                self.error.setText(tr(str(error)))
