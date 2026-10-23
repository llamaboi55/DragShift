"""Small audio editors using FFmpeg; no separate editor application required."""

from .i18n import tr, tr_items
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QLabel, QPlainTextEdit, QPushButton

from . import audio, video
from .video_tools import _AsyncTools, _field, _scroll_panel, _seconds, SPIN_STYLE
from .media_controls import FileSequence
from .util import category


class AudioToolWindow(_AsyncTools):
    def __init__(self, paths, notify, mode="Edit"):
        super().__init__(f"Audio · {mode}", paths, notify, (720, 670 if mode == "Edit" else 500))
        self.tool_name = mode
        self.mode = mode
        self.source_kwargs = {'mode': mode}
        self.info = None
        self.details = QLabel(tr("Reading audio information…"))
        self.details.setWordWrap(True)
        self.body.addWidget(self.details)
        listen = QPushButton(tr("Listen to original"))
        listen.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        self.body.addWidget(listen)
        self.form = QFormLayout()
        self.form.setVerticalSpacing(10)
        self.start_time, self.end_time = _seconds(), _seconds()
        if mode in ("Trim", "Edit"):
            _field(self.form, "&Start", self.start_time)
            _field(self.form, "&End", self.end_time)
        if mode == "Edit":
            self.volume = QDoubleSpinBox()
            self.volume.setStyleSheet(SPIN_STYLE)
            self.volume.setRange(-60, 24)
            self.volume.setSuffix(tr(" dB"))
            self.speed = QDoubleSpinBox()
            self.speed.setStyleSheet(SPIN_STYLE)
            self.speed.setRange(.25, 4)
            self.speed.setSingleStep(.25)
            self.speed.setValue(1)
            self.speed.setSuffix(tr(" ×"))
            self.mono = QCheckBox(tr("Convert to mono"))
            self.reverse = QCheckBox(tr("Play in reverse"))
            self.normalize = QCheckBox(tr("Normalize loudness · −16 LUFS / −1.5 dBTP"))
            _field(self.form, "&Volume", self.volume)
            _field(self.form, "&Speed (pitch preserved)", self.speed)
            self.form.addRow(tr(""), self.mono)
            self.form.addRow(tr(""), self.reverse)
            self.form.addRow(tr(""), self.normalize)
        if mode in ("Fade", "Edit"):
            self.fade_in, self.fade_out = _seconds(.5 if mode == "Fade" else 0), _seconds(.5 if mode == "Fade" else 0)
            _field(self.form, "Fade &in", self.fade_in)
            _field(self.form, "Fade &out", self.fade_out)
        if mode == "Normalize":
            text = QLabel(tr("Measure the selected audio, then normalize to −16 LUFS with a −1.5 dBTP peak limit."))
            text.setWordWrap(True)
            self.form.addRow(tr(text))
        if mode == "Reverse":
            text = QLabel(tr("Create a new file that plays backwards."))
            self.form.addRow(tr(text))
        self.target = QComboBox()
        self.target.addItems(tr_items(audio.TARGETS))
        current = self.path.suffix[1:].upper()
        current = {"AIF": "AIFF", "OGA": "OGG"}.get(current, current)
        self.target.setCurrentText(current if current in audio.TARGETS else "M4A")
        if mode == "Compress":
            self.target.setCurrentText("MP3")
        _field(self.form, "Save &as", self.target)
        self.bitrate = QComboBox()
        for rate in (64, 96, 128, 160, 192, 256, 320):
            self.bitrate.addItem(tr(f"{rate} kb/s"), rate)
        self.bitrate.setCurrentText("128 kb/s" if mode == "Compress" else "192 kb/s")
        _field(self.form, "Lossy audio &bitrate", self.bitrate)
        self.rate = QComboBox()
        self.rate.addItem(tr("Keep source rate"), None)
        for value in (16000, 22050, 32000, 44100, 48000, 96000):
            self.rate.addItem(tr(f"{value:,} Hz"), value)
        _field(self.form, "Sample &rate", self.rate)
        self.target.currentTextChanged.connect(self._format_changed)
        self.metadata = None
        if mode == "Metadata":
            self.metadata = QPlainTextEdit()
            self.metadata.setReadOnly(True)
            self.metadata.setMaximumHeight(130)
            self.form.addRow(tr(self.metadata))
        note = QLabel(tr("Processing stays on this computer and saves a new file.\n"
                      "WAV/AIFF/FLAC use lossless encoding. OGG uses VBR quality 5."))
        note.setWordWrap(True)
        note.setObjectName("dim")
        self.body.addWidget(_scroll_panel(self.form, note), 1)
        if len(self.paths) > 1:
            hint = QLabel(tr(f"The same settings apply to all {len(self.paths)} selected files."))
            hint.setWordWrap(True)
            self.body.addWidget(hint)
        self.body.addWidget(self.error)
        self.body.addLayout(self.footer("Save audio", self.save))
        self.primary_btn.setEnabled(False)
        path = self.path
        self.lookup(lambda: audio.probe(path), self._loaded)
        self._format_changed()

    def _format_changed(self, *_):
        self.bitrate.setEnabled(self.target.currentText() not in ("WAV", "AIFF", "FLAC", "OGG"))

    def _loaded(self, info):
        self.info = info
        self.details.setText(tr(f"{video.time_text(info.duration)} · {info.codec.upper()} · "
            f"{info.sample_rate:,} Hz · {info.channels} channel{'s' if info.channels != 1 else ''}"))
        self.start_time.setMaximum(info.duration)
        self.end_time.setMaximum(info.duration)
        self.end_time.setValue(info.duration)
        if self.mode in ("Fade", "Edit"):
            self.fade_in.setMaximum(info.duration)
            self.fade_out.setMaximum(info.duration)
            self.fade_in.setValue(min(.5, info.duration / 4) if self.mode == "Fade" else 0)
            self.fade_out.setValue(min(.5, info.duration / 4) if self.mode == "Fade" else 0)
        if self.metadata is not None:
            self.metadata.setPlainText("\n".join(f"{k}: {v}" for k, v in info.tags.items()) or "No descriptive metadata")
        self.primary_btn.setEnabled(True)

    def options(self):
        opts = dict(target=self.target.currentText(), bitrate=self.bitrate.currentData(),
                    sample_rate=self.rate.currentData(), label=self.mode)
        if self.mode in ("Trim", "Edit"):
            opts.update(start=self.start_time.value(), end=self.end_time.value())
            if self.mode == "Edit" and self.info and abs(self.end_time.value() - self.info.duration) < .002:
                opts["end"] = None  # retain each file's own full length in a batch
        if self.mode == "Edit":
            opts.update(volume_db=self.volume.value(), speed=self.speed.value(), mono=self.mono.isChecked(),
                        reverse=self.reverse.isChecked(), normalize=self.normalize.isChecked())
        if self.mode in ("Fade", "Edit"):
            opts.update(fade_in=self.fade_in.value(), fade_out=self.fade_out.value())
        if self.mode == "Normalize":
            opts["normalize"] = True
        if self.mode == "Reverse":
            opts["reverse"] = True
        if self.mode == "Metadata":
            opts["strip_metadata"] = True
        return opts

    def validate(self):
        self.error.clear()
        if self.info is None:
            return False
        opts = self.options()
        start = opts.get("start", 0.)
        end = self.info.duration if opts.get("end") is None else opts["end"]
        if end - start < .04:
            self.error.setText(tr("Choose a start before the end of the audio."))
            self.start_time.setFocus()
            return False
        length = (end - start) / opts.get("speed", 1)
        if opts.get("fade_in", 0) + opts.get("fade_out", 0) > length:
            self.error.setText(tr("The combined fades must fit inside the processed audio duration."))
            self.fade_in.setFocus()
            return False
        return True

    def save(self):
        if not self.validate():
            return
        paths, opts = list(self.paths), self.options()
        self.run(lambda: video.batch(paths, lambda p: audio.transform(p, **opts)), "Processing audio…")


class AudioTransitionWindow(_AsyncTools):
    tool_name='Audio transition'

    def __init__(self,paths,notify):
        super().__init__('Audio transitions',paths,notify,(720,530))
        self.infos=[]
        self._generation=0
        intro=QLabel(tr('Blend the end of each sound into the beginning of the next. Drop files below and drag rows to set their order.'))
        intro.setWordWrap(True)
        self.body.addWidget(intro)
        self.sequence=FileSequence(paths,lambda p:category(p)=='audio',audio.AUDIO_FILTER)
        self.body.addWidget(self.sequence)
        self.details=QLabel()
        self.details.setWordWrap(True)
        self.body.addWidget(self.details)
        form=QFormLayout()
        self.duration=_seconds(.75)
        self.duration.setMinimum(.05)
        self.duration.setMaximum(60)
        _field(form,'Transition at each cut',self.duration)
        self.target=QComboBox()
        self.target.addItems(tr_items(audio.TARGETS))
        self.target.setCurrentText('M4A')
        _field(form,'Save as',self.target)
        self.body.addLayout(form)
        self.body.addStretch()
        self.body.addWidget(self.error)
        self.body.addLayout(self.footer('Create audio transitions',self.save))
        self.sequence.changed.connect(self.load)
        self.duration.valueChanged.connect(self.update_details)
        self.load(self.sequence.paths())

    def accepts_sources(self,paths):
        return bool(paths) and all(category(p)=='audio' for p in paths)

    def receive_files(self,paths):
        self.sequence.add(paths)

    def load(self,paths):
        self._generation+=1
        generation=self._generation
        self.paths=list(paths)
        self.infos=[]
        self.primary_btn.setEnabled(False)
        self.error.clear()
        if not paths:
            self.details.setText(tr('Drop at least two audio files.'))
            return
        self.path=paths[0]
        def done(infos):
            if generation!=self._generation:
                return
            self.infos=infos
            limit=min(i.duration/(2 if 0<n<len(infos)-1 else 1) for n,i in enumerate(infos))-.03
            self.duration.setValue(min(.75,max(.05,limit)))
            self.primary_btn.setEnabled(2<=len(paths)<=16)
            self.update_details()
        self.lookup(lambda:[audio.probe(p) for p in paths],done,
                    lambda msg:self.error.setText(tr(msg)) if generation==self._generation else None)

    def update_details(self,*_):
        if self.infos:
            length=sum(i.duration for i in self.infos)-self.duration.value()*(len(self.infos)-1)
            self.details.setText(tr(f'{len(self.infos)} clips → {video.time_text(length)} · stereo · overlapping crossfades'))

    def save(self):
        if len(self.infos)<2:
            return
        duration=self.duration.value()
        if any(i.duration<=duration*(2 if 0<n<len(self.infos)-1 else 1)+.02 for n,i in enumerate(self.infos)):
            self.error.setText(tr('Shorten the crossfade: middle clips need room for two fades.'))
            return
        paths,target=list(self.paths),self.target.currentText()
        self.run(lambda:audio.transition(paths,duration=duration,target=target),'Creating audio transitions…')
