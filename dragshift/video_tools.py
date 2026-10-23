"""Video editors and cancellable progress windows in DragShift's glass style."""
from __future__ import annotations

from .i18n import tr, tr_items

import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal, Slot
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
    QSlider, QSpinBox, QVBoxLayout, QWidget)

from . import datamosh, report, video
from .tools import ToolWindow, section, slider, slider_row
from .theme import BORDER, ERROR, HOVER, MUTED, SELECTED, TEXT
from .util import UserError, keep, release
from .window import GlassWindow
from .media_controls import TrimRange, value_editor

VIDEO_STYLE = f"""
QDoubleSpinBox, QPlainTextEdit {{ color: {TEXT}; background-color: #ffffff;
 border: 1px solid {BORDER}; border-radius: 6px; padding: 5px 8px; }}
QDoubleSpinBox QLineEdit {{ color: {TEXT}; background-color: #ffffff; border: none; }}
QDoubleSpinBox:focus, QComboBox:focus, QPushButton:focus {{ border-color: {TEXT}; }}
QProgressBar {{ color: {TEXT}; border: 1px solid {BORDER}; border-radius: 6px;
 background: {HOVER}; text-align: center; min-height: 24px; }}
QProgressBar::chunk {{ background: {SELECTED}; border-radius: 5px; }}
QLabel#videoError {{ color: {ERROR}; }}
"""
SPIN_STYLE = (f"QDoubleSpinBox {{background-color:#ffffff;color:{TEXT};border:1px solid {BORDER};"
              f"border-radius:6px;padding:5px 28px 5px 8px;}} QLineEdit {{background-color:#ffffff;color:{TEXT};border:none;}}"
              f"QDoubleSpinBox:disabled, QLineEdit:disabled {{color:{MUTED};background-color:{HOVER};}}")


class _LookupSignals(QObject):
    done = Signal(int, object)
    failed = Signal(int, str)
    finished = Signal(object)


class _Lookup(QRunnable):
    def __init__(self, serial, fn):
        super().__init__()
        self.serial, self.fn = serial, fn
        self.sig = _LookupSignals()

    def run(self):
        try:
            self.sig.done.emit(self.serial, self.fn())
        except Exception as e:
            self.sig.failed.emit(self.serial, str(e))
        finally:
            self.sig.finished.emit(self)


class _AsyncTools(ToolWindow):
    """Only Qt slots touch widgets; lookup jobs own no GUI objects."""
    def __init__(self, title, paths, notify, size):
        super().__init__(title, paths, notify, size)
        self.setStyleSheet(self.styleSheet() + VIDEO_STYLE)
        self._serial = 0
        self._callbacks = {}
        self.error = QLabel()
        self.error.setObjectName("videoError")
        self.error.setWordWrap(True)
        self.error.setAccessibleName(tr("Video error"))

    def lookup(self, fn, callback, failure=None):
        self._serial += 1
        job = _Lookup(self._serial, fn)
        self._callbacks[job.serial] = (callback, failure)
        job.sig.done.connect(self._lookup_done)
        job.sig.failed.connect(self._lookup_failed)
        job.sig.finished.connect(release)
        QThreadPool.globalInstance().start(keep(job))
        return job.serial

    @Slot(int, object)
    def _lookup_done(self, serial, value):
        callbacks = self._callbacks.pop(serial, None)
        if callbacks:
            callbacks[0](value)

    @Slot(int, str)
    def _lookup_failed(self, serial, message):
        callbacks = self._callbacks.pop(serial, None)
        if callbacks:
            if callbacks[1]:
                callbacks[1](message)
            else:
                self.error.setText(tr(message))

    def closeEvent(self, e):
        self._callbacks.clear()
        super().closeEvent(e)


def _seconds(value=0.):
    spin = QDoubleSpinBox()
    spin.setStyleSheet(SPIN_STYLE)
    spin.setRange(0, 1_000_000)
    spin.setDecimals(3)
    spin.setSingleStep(.1)
    spin.setSuffix(tr(" s"))
    spin.setValue(value)
    return spin


def _field(form: QFormLayout, label: str, widget):
    widget.setAccessibleName(tr(label.replace("&", "")))
    form.addRow(tr(label), widget)


def _preview_label():
    label = QLabel(tr("Reading video…"))
    label.setAlignment(Qt.AlignCenter)
    label.setMinimumHeight(110)
    label.setMaximumHeight(180)
    label.setAccessibleName(tr("Video frame preview"))
    return label


def _show_frame(label, data):
    pix = QPixmap()
    if pix.loadFromData(data):
        height = min(176, label.maximumHeight()-18)
        label.setPixmap(pix.scaled(max(120,label.width()-20),max(50,height),Qt.KeepAspectRatio,Qt.SmoothTransformation))
    else:
        label.setText(tr("Frame preview unavailable"))


def _scroll_panel(form, note=None):
    content = QWidget()
    content.setObjectName("mediaForm")
    layout = QVBoxLayout(content)
    layout.setContentsMargins(0, 0, 4, 0)
    layout.addLayout(form)
    if note:
        layout.addWidget(note)
    layout.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.viewport().setObjectName("mediaViewport")
    scroll.setStyleSheet("QScrollArea, QWidget#mediaForm, QWidget#mediaViewport { background: transparent; border: none; }")
    scroll.viewport().setAutoFillBackground(False)
    content.setAutoFillBackground(False)
    scroll.setMinimumHeight(170)
    scroll.setWidget(content)
    return scroll


class VideoToolWindow(_AsyncTools):
    def __init__(self, paths, notify, mode="Edit"):
        super().__init__(f"Video · {mode}", paths, notify, (740, 730 if mode == "Edit" else 560))
        self.tool_name = mode
        self.mode = mode
        self.source_kwargs = {'mode': mode}
        self.info = None
        self.preview_serial = 0
        self._preview_busy = False
        self.details = QLabel(tr("Reading video information…"))
        self.details.setObjectName("dim")
        self.details.setWordWrap(True)
        self.body.addWidget(self.details)
        self.preview = _preview_label()
        self.body.addWidget(self.preview)
        self.scrub = QSlider(Qt.Horizontal)
        self.scrub.setRange(0, 1000)
        self.scrub.setAccessibleName(tr("Preview position"))
        self.scrub.setEnabled(False)
        self.timestamp = QLabel(tr("00:00.000"))
        self.timestamp.setMinimumWidth(90)
        row = QHBoxLayout()
        row.addWidget(self.scrub, 1)
        row.addWidget(self.timestamp)
        self.body.addLayout(row)
        self._preview_timer = QTimer(self, interval=220, singleShot=True)
        self._preview_timer.timeout.connect(self.load_frame)
        self.scrub.valueChanged.connect(self._scrub_changed)
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(18)
        self.form.setVerticalSpacing(10)
        if mode == "Trim":
            self.start_time, self.end_time = _seconds(), _seconds()
            _field(self.form, "&Start", self.start_time)
            _field(self.form, "&End", self.end_time)
            self.start_time.valueChanged.connect(self._start_preview)
            self.trim_range = TrimRange()
            self.form.addRow(tr(self.trim_range))
            self.trim_range.changed.connect(self._range_changed)
            self.start_time.valueChanged.connect(self._sync_range)
            self.end_time.valueChanged.connect(self._sync_range)
        elif mode in ("Compress", "Resize"):
            self.max_side = QSpinBox()
            self.max_side.setRange(16, 7680)
            self.max_side.setValue(1280)
            self.max_side.setSuffix(tr(" px"))
            _field(self.form, "&Longest edge (no upscale)", self.max_side)
            if mode == "Compress":
                self.quality = QComboBox()
                for text, value in [("High quality", 23), ("Balanced", 28), ("Smaller file", 33), ("Smallest file", 38)]:
                    self.quality.addItem(tr(text), value)
                self.quality.setCurrentIndex(1)
                _field(self.form, "&Compression", self.quality)
        elif mode == "Edit":
            self.rotation = QComboBox()
            for angle in (0, 90, 180, 270):
                self.rotation.addItem(tr(f"{angle}° clockwise"), angle)
            self.crop = QComboBox()
            for text, ratio in [("Keep original", None), ("Square · 1:1", 1), ("Landscape · 16:9", 16/9),
                                ("Portrait · 9:16", 9/16), ("Classic · 4:3", 4/3)]:
                self.crop.addItem(tr(text), ratio)
            self.flip = QCheckBox(tr("Mirror horizontally"))
            self.speed = QDoubleSpinBox()
            self.speed.setStyleSheet(SPIN_STYLE)
            self.speed.setRange(.25, 4)
            self.speed.setSingleStep(.25)
            self.speed.setValue(1)
            self.speed.setSuffix(tr(" ×"))
            _field(self.form, "&Rotation", self.rotation)
            _field(self.form, "Centre &crop", self.crop)
            _field(self.form, "&Speed (audio pitch preserved)", self.speed)
            self.form.addRow(tr(""), self.flip)
            for key, label, lo, hi, value in [("brightness", "Brightness", -50, 50, 0),
                    ("contrast", "Contrast", 10, 300, 100), ("saturation", "Saturation", 0, 300, 100)]:
                s = slider(lo, hi, value)
                s.setAccessibleName(tr(label))
                setattr(self, key, s)
                value_label = value_editor(s, label)
                layout = QHBoxLayout()
                layout.addWidget(s, 1)
                layout.addWidget(value_label)
                self.form.addRow(tr(label), layout)
        elif mode == "Frame":
            self.frame_time = _seconds()
            _field(self.form, "&Frame position", self.frame_time)
            self.frame_time.valueChanged.connect(self._frame_time_changed)
        elif mode == "Metadata":
            self.metadata = QPlainTextEdit()
            self.metadata.setReadOnly(True)
            self.metadata.setMaximumHeight(140)
            self.metadata.setAccessibleName(tr("Video metadata"))
            self.form.addRow(tr(self.metadata))
        elif mode == "Mute":
            hint = QLabel(tr("Save a new MP4 without an audio track."))
            hint.setWordWrap(True)
            self.form.addRow(tr(hint))
        self.body.addWidget(_scroll_panel(self.form), 1)
        note = QLabel(tr("Edits save a new MP4. The original stays unchanged." if mode != "Frame"
                      else "Save the selected frame as a full-resolution PNG."))
        note.setWordWrap(True)
        note.setObjectName("dim")
        self.output_note = note
        self.body.addWidget(note)
        if len(self.paths) > 1:
            batch = QLabel(tr(f"Settings apply to all {len(self.paths)} videos. Preview shows the first."))
            batch.setWordWrap(True)
            self.body.addWidget(batch)
        if mode == "Edit":
            hint = QLabel(tr("Preview updates with rotation, crop, mirror and colour changes."))
            hint.setObjectName("dim")
            self.body.addWidget(hint)
            for control in (self.rotation, self.crop):
                control.currentIndexChanged.connect(self._schedule_preview)
            self.flip.toggled.connect(self._schedule_preview)
            for control in (self.brightness, self.contrast, self.saturation, self.speed):
                control.valueChanged.connect(self._schedule_preview)
        if mode in ('Compress', 'Resize'):
            self.max_side.valueChanged.connect(self._schedule_preview)
        self.body.addWidget(self.error)
        text = "Save PNG" if mode == "Frame" else "Save without metadata" if mode == "Metadata" else "Save MP4"
        self.body.addLayout(self.footer(text, self.save))
        self.primary_btn.setEnabled(False)
        path = self.path
        self.lookup(lambda: (video.probe(path), video.preview_bytes(path)), self._loaded)

    def _loaded(self, data):
        self.info, frame = data
        i = self.info
        self.details.setText(tr(f"{i.width} × {i.height} · {video.time_text(i.duration)} · {i.fps:.2f} fps · "
                             f"{i.codec.upper()} · {'Audio' if i.has_audio else 'No audio'}"))
        _show_frame(self.preview, frame)
        self.primary_btn.setEnabled(True)
        self.scrub.setEnabled(True)
        if self.mode == "Trim":
            self.start_time.setMaximum(i.duration)
            self.end_time.setMaximum(i.duration)
            self.end_time.setValue(i.duration)
            self.trim_range.set_values(0, i.duration, i.duration)
        if self.mode == "Frame":
            self.frame_time.setMaximum(max(0, i.duration - 1 / i.fps))
        if self.mode in ("Compress", "Resize"):
            self.max_side.setValue(min(1280, max(i.width, i.height)))
        if self.mode == "Metadata":
            self.metadata.setPlainText("\n".join(f"{k}: {v}" for k, v in i.tags.items()) or tr("No descriptive metadata"))

    def _scrub_changed(self, value):
        if self.info:
            at = min(self.info.duration - 1 / self.info.fps, value / 1000 * self.info.duration)
            self.timestamp.setText(tr(video.time_text(at)))
            if self.mode == "Frame":
                self.frame_time.blockSignals(True)
                self.frame_time.setValue(at)
                self.frame_time.blockSignals(False)
            self._schedule_preview()

    def _start_preview(self, value):
        if self.info:
            self.scrub.setValue(round(value / self.info.duration * 1000))

    def _range_changed(self, start, end):
        for spin, value in ((self.start_time, start), (self.end_time, end)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self._start_preview(start if self.trim_range.active == 0 else max(start, end - 1/self.info.fps))

    def _sync_range(self, *_):
        if self.info:
            self.trim_range.set_values(self.start_time.value(), self.end_time.value(), self.info.duration)

    def _schedule_preview(self, *_):
        self.preview_serial += 1  # invalidate before the debounce timer fires
        self._preview_timer.start()

    def preview_options(self):
        if self.mode == 'Edit':
            return dict(rotation=self.rotation.currentData(), crop_ratio=self.crop.currentData(),
                        flip=self.flip.isChecked(), brightness=self.brightness.value()/100,
                        contrast=self.contrast.value()/100, saturation=self.saturation.value()/100)
        if self.mode in ('Resize', 'Compress'):
            return dict(max_side=self.max_side.value())
        return {}

    def _frame_time_changed(self, value):
        self._start_preview(value)

    def load_frame(self):
        self._preview_timer.stop()
        if not self.info or self._preview_busy:
            return
        self._preview_busy = True
        at = min(self.info.duration - 1 / self.info.fps, self.scrub.value() / 1000 * self.info.duration)
        path = self.path
        options = self.preview_options()
        serial = self.preview_serial
        def complete(error=None):
            self._preview_busy = False
            if serial != self.preview_serial:
                self._preview_timer.start(0)
            elif error:
                self.preview.setText(tr("Frame preview unavailable"))
        def show(data):
            complete()
            if serial == self.preview_serial:
                _show_frame(self.preview, data)
        self.lookup(lambda: video.preview_bytes(path, at, **options), show,complete)

    def validate(self):
        self.error.clear()
        if self.info is None:
            return False
        if self.mode == "Trim":
            try:
                video.validate_range(self.info, self.start_time.value(), self.end_time.value())
            except UserError as e:
                self.error.setText(tr(str(e)))
                self.start_time.setFocus()
                return False
        return True

    def save(self):
        if not self.validate():
            return
        opts = {"label": self.mode + ("med" if self.mode == "Trim" else "")}
        if self.mode == "Trim":
            opts.update(start=self.start_time.value(), end=self.end_time.value())
        elif self.mode in ("Compress", "Resize"):
            opts["max_side"] = self.max_side.value()
            if self.mode == "Compress":
                opts["crf"] = self.quality.currentData()
        elif self.mode == "Edit":
            opts.update(rotation=self.rotation.currentData(), crop_ratio=self.crop.currentData(),
                flip=self.flip.isChecked(), speed=self.speed.value(), brightness=self.brightness.value()/100,
                contrast=self.contrast.value()/100, saturation=self.saturation.value()/100)
        elif self.mode == "Mute":
            opts["mute"] = True
        elif self.mode == "Metadata":
            opts["strip_metadata"] = True
        paths = list(self.paths)
        if self.mode == "Frame":
            at = self.frame_time.value()
            fn = lambda p: video.snapshot(p, at)
        else:
            fn = lambda p: video.transform(p, **opts)
        self.run(lambda: video.batch(paths, fn), "Processing video…")


class TelegramEmojiWindow(VideoToolWindow):
    tool_name = 'Telegram emoji'

    def __init__(self, paths, notify):
        super().__init__(paths, notify, 'Trim')
        self.tool_name = 'Telegram emoji'
        self.source_kwargs = {}
        self.setWindowTitle(tr('Telegram emoji — DragShift'))
        self.title_label.setText(tr('Telegram emoji'))
        self.resize(740,650)
        self.fit = QComboBox()
        self.fit.addItem(tr('Fit whole frame · transparent padding'), 'fit')
        self.fit.addItem(tr('Fill square · centre crop'), 'crop')
        _field(self.form, 'Square framing', self.fit)
        self.fit.currentIndexChanged.connect(self._schedule_preview)
        hint = QLabel(tr('WebM · VP9 · 100×100 · no sound · up to 3 seconds / 30 fps / 256 KB. '
                      'Select the segment on the range slider. The exported file is checked automatically.'))
        hint.setWordWrap(True)
        self.form.addRow(tr(hint))
        self.primary_btn.setText(tr('Save Telegram emoji'))
        self.output_note.setText(tr('Saves a new WebM ready for Telegram custom emoji.'))

    def accepts_sources(self, paths):
        from .util import category
        return bool(paths) and all(category(p)=='video' or p.suffix.lower()=='.gif' for p in paths)

    def _loaded(self, data):
        super()._loaded(data)
        self.end_time.setValue(min(self.info.duration,3))

    def _range_changed(self,start,end):
        super()._range_changed(start,min(end,start+3))
        self._sync_range()

    def preview_options(self):
        return dict(crop_ratio=1 if hasattr(self,'fit') and self.fit.currentData()=='crop' else None)

    def save(self):
        from . import telegram
        if not self.validate():
            return
        length=self.end_time.value()-self.start_time.value()
        if length>3.001:
            self.error.setText(tr('Choose a segment no longer than 3 seconds.'))
            return
        paths=list(self.paths)
        start,fit=self.start_time.value(),self.fit.currentData()
        self.run(lambda:video.batch(paths,lambda p:telegram.export_emoji(p,start=start,duration=length,fit=fit)),
                 'Creating Telegram emoji…')


class DatamoshWindow(_AsyncTools):
    tool_name = 'Datamosh'

    def __init__(self, paths, notify):
        from .media_controls import EffectEnvelope, DropZone, FileSequence
        from .util import category
        super().__init__('Datamosh · Melt', paths, notify, (800, 860))
        selected_paths=list(paths)
        audio_mode=category(paths[0])=='audio'
        self.media_mode=QComboBox()
        self.media_mode.addItem(tr('Video · Melt'),'video')
        self.media_mode.addItem(tr('Audio · Glitch'),'audio')
        self.media_mode.setAccessibleName(tr('Datamosh media'))
        self.body.addWidget(self.media_mode)
        outer_body=self.body
        self.video_panel=QWidget()
        self.body=QVBoxLayout(self.video_panel)
        self.body.setContentsMargins(0,0,0,0)
        outer_body.addWidget(self.video_panel,1)
        paths=[] if audio_mode else paths
        self.infos = []
        self.first_info = self.second_info = None
        self.second_path = None
        self._load_generation = 0
        self.accept_media = lambda p: category(p) == 'video' or p.suffix.lower() == '.gif'
        intro = QLabel(tr('Classic Melt carries one clip into the next. Drop clips below and drag rows to set their order.'))
        intro.setWordWrap(True)
        self.body.addWidget(intro)
        audio_button = QPushButton(tr('Audio file transitions…'))
        audio_button.clicked.connect(self.open_audio_transition)
        self.sequence = FileSequence(paths, self.accept_media, video.VIDEO_FILTER)
        self.sequence.list.setMaximumHeight(100)
        self.body.addWidget(self.sequence)
        row = QHBoxLayout()
        self.previews = []
        self.names = []
        for index, title in enumerate(('First clip · drop to replace', 'Next clip · drop to replace')):
            zone = DropZone(title, self.accept_media)
            zone.setMaximumHeight(90)
            zone.filesDropped.connect(lambda files, i=index: self.replace_clip(i, files))
            row.addWidget(zone, 1)
            self.previews.append(zone)
        self.body.addLayout(row)
        self.form = QFormLayout()
        self.form.setVerticalSpacing(8)
        self.strength = slider(0,100,65)
        self.strength.setAccessibleName(tr('Datamosh intensity'))
        row = QHBoxLayout()
        row.addWidget(self.strength,1)
        row.addWidget(value_editor(self.strength,'Datamosh intensity',lambda v:f'{v}%'))
        self.form.addRow(tr('Intensity'),row)
        self.curve = EffectEnvelope()
        self.form.addRow(self.curve)
        self.excerpts = QCheckBox(tr('Use only the end of A and beginning of B (two clips)'))
        self.excerpts.setChecked(len(paths) <= 2)
        self.form.addRow(tr(self.excerpts))
        self.tail, self.head, self.duration = _seconds(4), _seconds(6), _seconds(2)
        self.tail.setMinimum(.2)
        self.head.setMinimum(.2)
        self.duration.setMinimum(.1)
        _field(self.form, 'Last seconds of A', self.tail)
        _field(self.form, 'First seconds of B', self.head)
        _field(self.form, 'Transition duration at each cut', self.duration)
        self.preset = QComboBox()
        self.preset.addItem(tr('Melt'))
        self.preset.setEnabled(False)
        self.resolution = QComboBox()
        for px in (360,720,1080,1440,2160):
            self.resolution.addItem(tr(f'{px} px · longest edge'),px)
        self.resolution.setCurrentIndex(1)
        self.fps = QComboBox()
        for rate in (24,25,30,60):
            self.fps.addItem(tr(f'{rate} fps'),rate)
        self.fps.setCurrentIndex(2)
        _field(self.form,'Resolution',self.resolution)
        _field(self.form,'Frame rate',self.fps)
        self.audio = QCheckBox(tr('Keep audio from selected clips'))
        self.audio.setChecked(True)
        self.form.addRow(tr(self.audio))
        note = QLabel(tr('Intensity sets the effect strength. Choose a simple shape below to vary it over time. '
                      'Melt acts at clip changes, then returns to a clean image. '
                      'Preview uses the same timeline and curve at 480 px; open it from the result notification.'))
        note.setWordWrap(True)
        note.setObjectName('dim')
        self.body.addWidget(_scroll_panel(self.form,note),1)
        self.body.addWidget(self.error)
        self.preview_btn = QPushButton(tr('Render preview'))
        self.preview_btn.clicked.connect(lambda:self.save(preview=True))
        self.body.addLayout(self.footer('Create Melt video',self.save,extra_buttons=(audio_button,self.preview_btn)))
        self.sequence.changed.connect(self.reload_sequence)
        self.excerpts.toggled.connect(self._mode_changed)
        self.tail.valueChanged.connect(self._timeline)
        self.head.valueChanged.connect(self._timeline)
        self.reload_sequence(self.sequence.paths())
        from .audio_glitch_tools import AudioGlitchPanel
        self.audio_panel=AudioGlitchPanel(self,selected_paths)
        outer_body.addWidget(self.audio_panel,1)
        self.body=outer_body
        self.media_mode.currentIndexChanged.connect(self._media_mode_changed)
        self.media_mode.setCurrentIndex(1 if audio_mode else 0)
        self._media_mode_changed()
        self.resize(800,790)

    def _media_mode_changed(self,*_):
        audio_mode=self.media_mode.currentData()=='audio'
        self.video_panel.setVisible(not audio_mode)
        self.audio_panel.setVisible(audio_mode)
        self.title_label.setText(tr('Datamosh · Audio glitch' if audio_mode else 'Datamosh · Melt'))

    def _enter(self):
        if hasattr(self,'audio_panel') and self.media_mode.currentData()=='audio':
            video_button=self.primary_btn
            self.primary_btn=self.audio_panel.save_btn
            try:
                super()._enter()
            finally:
                self.primary_btn=video_button
        else:
            super()._enter()

    def accepts_sources(self, paths):
        from .util import category
        return bool(paths) and (all(category(p)=='audio' for p in paths) or all(self.accept_media(p) for p in paths))

    def open_audio_transition(self):
        from . import audio
        from .audio_tools import AudioTransitionWindow
        paths,_ = QFileDialog.getOpenFileNames(self,tr('Choose sounds for a transition'),str(self.path.parent),tr(audio.AUDIO_FILTER))
        if not paths:
            return
        window = AudioTransitionWindow(paths,self.notify)
        window.submit = self.submit
        self._audio_window = window
        if self.replacement_handler:
            self.replacement_handler(window)
        else:
            window.present()

    def receive_files(self, paths):
        from .util import category
        if category(paths[0])=='audio' or self.media_mode.currentData()=='audio':
            self.audio_panel.load(paths)
            self.media_mode.setCurrentIndex(1)
        else:
            self.sequence.add(paths)

    def replace_clip(self, index, files):
        from PySide6.QtWidgets import QListWidgetItem
        if index < self.sequence.list.count():
            item = self.sequence.list.item(index)
            item.setText(tr(files[0].name))
            item.setToolTip(tr(str(files[0])))
            item.setData(Qt.UserRole,str(files[0]))
            if len(files)>1:
                self.sequence.add(files[1:])
            else:
                self.reload_sequence(self.sequence.paths())
        else:
            self.sequence.add(files)

    def reload_sequence(self, paths):
        self._load_generation += 1
        generation = self._load_generation
        self.paths = list(paths)
        self.infos = []
        self.first_info = self.second_info = None
        self.primary_btn.setEnabled(False)
        self.preview_btn.setEnabled(False)
        self.error.clear()
        self.excerpts.setEnabled(len(paths)==2)
        if len(paths)!=2:
            self.excerpts.setChecked(False)
        if not paths:
            self.error.setText(tr('Drop at least two clips here.'))
            return
        self.path = paths[0]
        self.subtitle.setText(tr(f'{len(paths)} clips · ' + ' → '.join(p.name for p in paths)))
        if len(paths)>16:
            self.error.setText(tr('Use up to 16 clips in one series.'))
            return
        def load():
            infos = [video.probe(p) for p in paths]
            frames = [video.preview_bytes(paths[0],max(0,infos[0].duration-.15))]
            if len(paths)>1:
                frames.append(video.preview_bytes(paths[1]))
            return infos,frames
        def done(data):
            if generation!=self._load_generation:
                return
            self.infos,frames = data
            self.first_info = self.infos[0]
            self.second_info = self.infos[1] if len(self.infos)>1 else None
            self.second_path = paths[1] if len(paths)>1 else None
            for zone,frame,path in zip(self.previews,frames,paths):
                _show_frame(zone,frame)
                zone.setToolTip(tr(str(path)+' · Drop to replace'))
            self.tail.setMaximum(self.infos[0].duration)
            self.tail.setValue(min(4,self.infos[0].duration))
            if self.second_info:
                self.head.setMaximum(self.second_info.duration)
                self.head.setValue(min(6,self.second_info.duration))
                self.duration.setValue(min(2,max(.1,min(i.duration for i in self.infos[1:])/2)))
            self._mode_changed()
            self.primary_btn.setEnabled(len(paths)>=2)
            self.preview_btn.setEnabled(len(paths)>=2)
        self.lookup(load,done,lambda msg:self.error.setText(tr(msg)) if generation==self._load_generation else None)

    def _mode_changed(self,*_):
        self.tail.setEnabled(self.excerpts.isChecked())
        self.head.setEnabled(self.excerpts.isChecked())
        self._timeline()

    def _timeline(self,*_):
        if len(self.infos)<2:
            return
        lengths = [self.tail.value(),self.head.value()] if self.excerpts.isChecked() else [i.duration for i in self.infos]
        total=sum(lengths)
        self.curve.curve.markers=[sum(lengths[:i])/total for i in range(1,len(lengths))]
        self.curve.set_duration(total)
        self.curve.curve.update()

    def validate(self):
        self.error.clear()
        if len(self.infos)<2:
            return False
        limit = self.head.value() if self.excerpts.isChecked() else min(i.duration for i in self.infos[1:])
        if self.duration.value()>limit-3/self.fps.currentData():
            self.error.setText(tr('Leave at least two clean frames after each transition. Shorten the transition or choose longer clips.'))
            return False
        return True

    def save(self,checked=False,*,preview=False):
        if not self.validate():
            return
        paths=list(self.paths)
        opts=dict(duration=self.duration.value(),strength=self.strength.value(),curve=self.curve.points(),
                  excerpts=self.excerpts.isChecked(),tail=self.tail.value(),head=self.head.value(),
                  max_side=480 if preview else self.resolution.currentData(),audio=self.audio.isChecked(),fps=self.fps.currentData())
        fn=lambda:datamosh.sequence(paths,**opts)
        if self.submit:
            self.submit('tool:Datamosh',[str(p) for p in paths],fn,'Creating Melt series…','Preview · Melt' if preview else 'Melt')
            if not preview:
                self.close()
        else:
            self.notify('tool:Datamosh',[str(p) for p in paths],fn(),'Melt')


class _RenderSignals(QObject):
    done = Signal(object)
    failed = Signal(str, bool)
    progress = Signal(float, str)


class _Render(QRunnable):
    def __init__(self, action, srcs, fn, extra):
        super().__init__()
        self.action, self.srcs, self.fn, self.extra = action, srcs, fn, extra
        self.sig = _RenderSignals()
        self.control = video.Control(progress=self.sig.progress.emit)

    def run(self):
        try:
            with video.job_context(self.control):
                outs = self.fn()
                # Once published, cancellation must not hide a completed result.
                result = report.summarize(self.action, self.srcs, outs, self.extra)
            self.sig.done.emit(result)
        except video.Cancelled as e:
            self.sig.failed.emit(str(e), True)
        except Exception as e:
            traceback.print_exc()
            self.sig.failed.emit(str(e), False)


class VideoProgressWindow(GlassWindow):
    def __init__(self, action, srcs, fn, on_done, extra=""):
        super().__init__("File processing", size=(580, 260))
        self.setStyleSheet(self.styleSheet() + VIDEO_STYLE)
        self.busy = False
        self.on_done = on_done
        self.job = _Render(action, srcs, fn, extra)
        self.job.sig.done.connect(self._done)
        self.job.sig.failed.connect(self._failed)
        self.job.sig.progress.connect(self._progress)
        self.status = QLabel(tr("Preparing…"))
        self.status.setWordWrap(True)
        self.body.addWidget(self.status)
        self.detail = QLabel(tr(" → ".join(Path(p).name for p in srcs)))
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.body.addWidget(self.detail)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setAccessibleName(tr("Video export progress"))
        self.body.addWidget(self.bar)
        self.button = QPushButton(tr("Cancel"))
        self.button.clicked.connect(self.cancel_work)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self.button)
        self.body.addLayout(row)

    def start(self, pool=None):
        if self.busy:
            return
        self.busy = True
        (pool or QThreadPool.globalInstance()).start(keep(self.job))

    @Slot(float, str)
    def _progress(self, value, text):
        self.bar.setValue(round(value * 100))
        self.status.setText(tr(text))

    @Slot(object)
    def _done(self, result):
        release(self.job)
        self.busy = False
        self.on_done(result)
        self.close()

    @Slot(str, bool)
    def _failed(self, message, cancelled):
        release(self.job)
        self.busy = False
        self.status.setText(tr("Cancelled" if cancelled else "Couldn't finish"))
        self.detail.setText(tr(message))
        self.button.setText(tr("Close"))
        self.button.setEnabled(True)

    def cancel_work(self):
        if self.busy:
            self.job.control.cancel.set()
            self.button.setText(tr("Cancelling…"))
            self.button.setEnabled(False)
        else:
            self.close()

    def closeEvent(self, e):
        if self.busy:
            self.cancel_work()
            e.ignore()
        else:
            super().closeEvent(e)
