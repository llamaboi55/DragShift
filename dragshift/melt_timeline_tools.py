"""A compact single-track editor around the classic Melt renderer."""
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget)

from . import video
from .i18n import tr
from .media_controls import DropZone, value_editor
from .melt_timeline import Timeline, render
from .theme import BORDER, HOVER, MUTED, PANEL, SELECTED, TEXT
from .util import UserError, category


class TimelineView(QWidget):
    selected = Signal(str, int)
    changed = Signal()
    seek = Signal(float)

    def __init__(self, model):
        super().__init__()
        self.model = model
        self.scale = 45.
        self.playhead = 0.
        self.selection = ('clip', 0)
        self.dragging = None
        self.setFixedHeight(154)
        self.setMinimumWidth(620)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setAccessibleName(tr('Video timeline · select, drag clips to reorder, drag edges to trim'))
        self.setToolTip(tr('Click a clip to edit its source range. Drag its centre to reorder, or its edges to trim. Click a Melt marker to edit that transition. Click the ruler to position the playhead.'))

    def refresh(self):
        self.setMinimumWidth(max(620, min(2_000_000, round(self.model.duration*self.scale)+36)))
        self.resize(self.minimumWidth(), self.height())
        self.playhead = min(self.playhead, self.model.duration)
        self.update()

    def rectangles(self):
        x = 18.
        result = []
        for clip in self.model.clips:
            result.append(QRectF(x, 36, clip.duration*self.scale, 64))
            x += clip.duration*self.scale
        return result

    def select(self, kind, index):
        self.selection = kind, index
        self.selected.emit(kind, index)
        self.update()

    def set_playhead(self, value):
        self.playhead = max(0., min(value, self.model.duration))
        self.seek.emit(self.playhead)
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self.setFocus()
        point = event.position()
        if point.y() < 30:
            self.set_playhead((point.x()-18)/self.scale)
            self.dragging = ('seek',)
            return
        rectangles = self.rectangles()
        for index, rect in enumerate(rectangles[:-1]):
            if QRectF(rect.right()-24, 108, 48, 26).contains(point):
                self.select('cut', index)
                return
        for index, rect in enumerate(rectangles):
            if rect.adjusted(-4,0,4,0).contains(point):
                self.select('clip', index)
                clip = self.model.clips[index]
                kind = 'start' if abs(point.x()-rect.left()) <= 7 else 'end' if abs(point.x()-rect.right()) <= 7 else 'move'
                self.dragging = (kind,index,point.x(),clip.start,clip.end)
                return

    def mouseMoveEvent(self, event):
        if not self.dragging:
            return
        kind = self.dragging[0]
        if kind == 'seek':
            self.set_playhead((event.position().x()-18)/self.scale)
            return
        kind,index,x,start,end = self.dragging
        if kind == 'move':
            self.setCursor(Qt.ClosedHandCursor)
            return
        clip = self.model.clips[index]
        delta = (event.position().x()-x)/self.scale
        if kind == 'start':
            clip.start = max(0., min(end-.2, start+delta))
        else:
            clip.end = min(clip.source_duration, max(start+.2, end+delta))
        self.refresh()
        self.changed.emit()

    def mouseReleaseEvent(self, event):
        if self.dragging and self.dragging[0] == 'move':
            _,index,x,_,_ = self.dragging
            if abs(event.position().x()-x) > 8:
                rectangles = self.rectangles()
                destination = min(range(len(rectangles)), key=lambda n: abs(rectangles[n].center().x()-event.position().x()))
                self.model.move(index, destination)
                self.select('clip', destination)
                self.changed.emit()
        self.dragging = None
        self.unsetCursor()
        self.refresh()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left,Qt.Key_Right):
            self.set_playhead(self.playhead+(-1 if event.key()==Qt.Key_Left else 1)*(.5 if event.modifiers()&Qt.ShiftModifier else 1/30))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(PANEL))
        painter.setPen(QColor(MUTED))
        step = max(1, round(85/self.scale))
        visible = QRectF(event.rect())
        begin = max(0,int((visible.left()-18)/self.scale)//step*step)
        end = min(int(self.model.duration)+1,int((visible.right()-18)/self.scale)+2)
        for second in range(begin,end,step):
            x = 18+second*self.scale
            painter.drawLine(QPointF(x,25),QPointF(x,30))
            painter.drawText(QRectF(x+2,3,80,20),Qt.AlignLeft,video.time_text(second))
        for index, rect in enumerate(self.rectangles()):
            painter.setPen(QPen(QColor(TEXT if self.selection==('clip',index) else BORDER), 2 if self.selection==('clip',index) else 1))
            painter.setBrush(QColor(SELECTED if self.selection==('clip',index) else HOVER))
            painter.drawRoundedRect(rect.adjusted(1,0,-1,0),5,5)
            painter.save()
            painter.setClipRect(rect.adjusted(8,3,-8,-3))
            painter.setPen(QColor(TEXT))
            clip = self.model.clips[index]
            painter.drawText(rect.adjusted(10,6,-10,-24),Qt.AlignLeft|Qt.AlignVCenter,f'{index+1}. {clip.path.name}')
            painter.setPen(QColor(MUTED))
            painter.drawText(rect.adjusted(10,31,-10,-4),Qt.AlignLeft|Qt.AlignVCenter,f'{clip.start:.2f} → {clip.end:.2f} '+tr('s'))
            painter.restore()
            painter.setPen(QPen(QColor(MUTED),2))
            for x in (rect.left()+4,rect.right()-4):
                painter.drawLine(QPointF(x,60),QPointF(x,78))
            if index+1 < len(self.model.clips):
                cut = self.model.cut(index)
                badge = QRectF(rect.right()-24,108,48,26)
                painter.setBrush(QColor(SELECTED if self.selection==('cut',index) else PANEL))
                painter.setPen(QPen(QColor(TEXT if self.selection==('cut',index) else BORDER),1))
                painter.drawRoundedRect(badge,5,5)
                painter.setPen(QColor(TEXT))
                painter.drawText(badge,Qt.AlignCenter,'Melt' if cut.enabled else '—')
        x = 18+self.playhead*self.scale
        painter.setPen(QPen(QColor(TEXT),1))
        painter.drawLine(QPointF(x,23),QPointF(x,142))


class MeltTimelinePanel(QWidget):
    def __init__(self, owner, paths):
        from .video_tools import _field,_seconds,_scroll_panel
        super().__init__(owner)
        self.owner = owner
        self.model = Timeline()
        self.initial = list(paths)
        self.activated = False
        self.pending = []
        self.loading = False
        self.selection = ('clip',0)
        self.preview_request = None
        self.preview_busy = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.drop = DropZone('Drop videos here to add them to the timeline', self.accept)
        self.drop.setMaximumHeight(58)
        self.drop.setMinimumHeight(48)
        self.drop.filesDropped.connect(self.add)
        layout.addWidget(self.drop)
        toolbar = QHBoxLayout()
        self.edit_buttons = {}
        for title,fn in [('Add files…',self.choose),('←',lambda:self.move(-1)),('→',lambda:self.move(1)),
                         ('Split at playhead',self.split),('Remove clip',self.remove)]:
            button = QPushButton(tr(title))
            button.clicked.connect(fn)
            toolbar.addWidget(button)
            self.edit_buttons[title] = button
        toolbar.addStretch()
        toolbar.addWidget(QLabel(tr('Zoom')))
        zoom = QSlider(Qt.Horizontal)
        zoom.setRange(10,250)
        zoom.setValue(45)
        zoom.setMaximumWidth(130)
        zoom.setAccessibleName(tr('Timeline zoom'))
        zoom.valueChanged.connect(self.zoom)
        toolbar.addWidget(zoom)
        layout.addLayout(toolbar)
        self.view = TimelineView(self.model)
        scroll = QScrollArea()
        scroll.setWidget(self.view)
        scroll.setWidgetResizable(False)
        scroll.setFixedHeight(179)
        scroll.setAccessibleName(tr('Video timeline'))
        layout.addWidget(scroll)
        self.position = QLabel('00:00.000')
        layout.addWidget(self.position)
        self.preview_label = QLabel(tr('Source frame preview'))
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumHeight(60)
        self.preview_label.setMaximumHeight(115)
        layout.addWidget(self.preview_label)
        form = QFormLayout()
        self.parameter_form = form
        self.selected_label = QLabel()
        self.selected_label.setWordWrap(True)
        form.addRow(self.selected_label)
        self.start,self.end = _seconds(),_seconds()
        _field(form,'Source start',self.start)
        _field(form,'Source end',self.end)
        self.enabled = QCheckBox(tr('Melt at this cut'))
        form.addRow(self.enabled)
        self.duration = _seconds(2)
        self.duration.setRange(.1,60)
        _field(form,'Transition duration',self.duration)
        self.strength = QSlider(Qt.Horizontal)
        self.strength.setRange(1,100)
        self.strength.setValue(65)
        row = QHBoxLayout()
        row.addWidget(self.strength,1)
        row.addWidget(value_editor(self.strength,'Datamosh intensity'))
        self.strength_row = row
        form.addRow(tr('Intensity'),row)
        self.resolution = QComboBox()
        for px in (360,720,1080,1440,2160): self.resolution.addItem(tr(str(px)+' px'),px)
        self.resolution.setCurrentIndex(1)
        _field(form,'Resolution',self.resolution)
        self.fps = QComboBox()
        for rate in (24,25,30,60): self.fps.addItem(tr(str(rate)+' fps'),rate)
        self.fps.setCurrentIndex(2)
        _field(form,'Frame rate',self.fps)
        self.audio = QCheckBox(tr('Keep audio from selected clips'))
        self.audio.setChecked(True)
        form.addRow(self.audio)
        note = QLabel(tr('One track: move clips to reorder; drag edges to trim. Select a Melt marker to change only that transition. Split uses the playhead. No clean-video overlay is mixed into the effect.'))
        note.setWordWrap(True)
        parameters = _scroll_panel(form,note)
        parameters.setMinimumHeight(80)
        layout.addWidget(parameters,1)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setObjectName('videoError')
        layout.addWidget(self.error)
        buttons = QHBoxLayout()
        self.preview_btn = QPushButton(tr('Preview selected transition'))
        self.preview_btn.clicked.connect(lambda:self.save(preview=True))
        buttons.addWidget(self.preview_btn)
        buttons.addStretch()
        cancel = QPushButton(tr('Cancel'))
        cancel.clicked.connect(owner.close)
        buttons.addWidget(cancel)
        self.save_btn = QPushButton(tr('Create Melt video'))
        self.save_btn.setObjectName('primary')
        self.save_btn.clicked.connect(self.save)
        buttons.addWidget(self.save_btn)
        layout.addLayout(buttons)
        self.view.selected.connect(self.select)
        self.view.changed.connect(self.refresh)
        self.view.seek.connect(self.seek)
        self.start.editingFinished.connect(self.trim)
        self.end.editingFinished.connect(self.trim)
        for widget in (self.enabled,self.duration,self.strength):
            (widget.toggled if isinstance(widget,QCheckBox) else widget.valueChanged).connect(self.edit_cut)
        self._preview_timer = QTimer(self,interval=100,singleShot=True,timeout=self.load_preview)
        self.refresh()

    @staticmethod
    def accept(path):
        return category(path)=='video' or path.suffix.lower()=='.gif'

    def activate(self):
        if not self.activated:
            self.activated = True
            self.add(self.initial)

    def choose(self):
        paths,_ = QFileDialog.getOpenFileNames(self,tr('Add files'),'',tr(video.VIDEO_FILTER))
        self.add(paths)

    def add(self, paths):
        self.pending.extend(Path(p) for p in paths if self.accept(Path(p)))
        self._load_pending()

    def _load_pending(self):
        if self.loading or not self.pending:
            return
        paths,self.pending = self.pending,[]
        self.loading = True
        self.error.setText(tr('Reading video information…'))
        def finish(data=None,error=None):
            self.loading = False
            if error:
                self.error.setText(tr(error))
            else:
                try:
                    model = self.model.snapshot()
                    for path,info in data: model.add(path,info.duration)
                    self.model.clips,self.model.cuts = model.clips,model.cuts
                    self.select('clip',len(self.model.clips)-len(data))
                    self.error.clear()
                except UserError as problem:
                    self.error.setText(tr(str(problem)))
            self.refresh()
            self._load_pending()
        self.owner.lookup(lambda:[(path,video.probe(path)) for path in paths],finish,lambda error:finish(error=error))

    def zoom(self, value):
        self.view.scale = value
        self.view.refresh()

    def select(self, kind, index):
        self.selection = kind,index
        self.view.selection = self.selection
        self.refresh()
        if self.model.clips:
            clip = self.model.clips[index]
            self.view.set_playhead(sum(c.duration for c in self.model.clips[:index])+(clip.duration-.04 if kind=='cut' else 0))

    def refresh(self):
        kind,index = self.selection
        if self.model.clips:
            index = min(index,max(0,len(self.model.clips)-(2 if kind=='cut' else 1)))
            self.selection = kind,index
        active = bool(self.model.clips)
        # Show the controls for the selected item first, without inactive clip
        # fields pushing a cut's strength and duration below the scroll area.
        for field in (self.start, self.end):
            self.parameter_form.setRowVisible(field, kind == 'clip')
        for field in (self.enabled, self.duration, self.strength_row):
            self.parameter_form.setRowVisible(field, kind == 'cut')
        for key in ('←','→','Remove clip'):
            self.edit_buttons[key].setEnabled(active and kind=='clip')
        self.edit_buttons['Split at playhead'].setEnabled(active)
        for widget in (self.start,self.end): widget.setEnabled(active and kind=='clip')
        for widget in (self.enabled,self.duration,self.strength): widget.setEnabled(active and kind=='cut' and len(self.model.clips)>1)
        if active:
            clip = self.model.clips[index]
            self.selected_label.setText(tr('Clip {0} · {1}').format(index+1,clip.path.name) if kind=='clip'
                else tr('Transition {0} → {1} · classic Melt').format(index+1,index+2))
            for widget,value in ((self.start,clip.start),(self.end,clip.end)):
                widget.blockSignals(True)
                widget.setMaximum(clip.source_duration)
                widget.setValue(value)
                widget.blockSignals(False)
            if kind=='cut' and index+1<len(self.model.clips):
                cut = self.model.cut(index)
                for widget,value in ((self.enabled,cut.enabled),(self.duration,cut.duration),(self.strength,cut.strength)):
                    widget.blockSignals(True)
                    widget.setChecked(value) if isinstance(widget,QCheckBox) else widget.setValue(value)
                    widget.blockSignals(False)
        else:
            self.selected_label.setText(tr('Drop at least two clips here.'))
        self.save_btn.setEnabled(len(self.model.clips)>=2 and not self.loading)
        self.preview_btn.setEnabled(self.save_btn.isEnabled())
        self.view.selection = self.selection
        self.view.refresh()

    def trim(self):
        if self.selection[0]!='clip' or not self.model.clips: return
        try:
            self.model.trim(self.selection[1],self.start.value(),self.end.value())
            self.error.clear()
        except UserError as error:
            self.error.setText(tr(str(error)))
        self.refresh()

    def edit_cut(self, *_):
        if self.selection[0]!='cut' or len(self.model.clips)<2: return
        cut = self.model.cut(self.selection[1])
        cut.duration,cut.strength,cut.enabled = self.duration.value(),self.strength.value(),self.enabled.isChecked()
        self.view.update()

    def move(self, delta):
        if not self.model.clips: return
        self.select('clip',self.model.move(self.selection[1],self.selection[1]+delta))

    def remove(self):
        if not self.model.clips: return
        self.model.remove(self.selection[1])
        self.selection = 'clip',max(0,self.selection[1]-1)
        self.refresh()

    def split(self):
        if not self.model.clips: return
        try:
            index,time = self.model.locate(self.view.playhead)
            self.select('clip',self.model.split(index,time))
            self.error.clear()
        except UserError as error:
            self.error.setText(tr(str(error)))

    def seek(self, value):
        self.position.setText(tr('Timeline {0} / {1} · source frame').format(video.time_text(value),video.time_text(self.model.duration)))
        if not self.model.clips: return
        index,at = self.model.locate(value)
        clip = self.model.clips[index]
        self.preview_request = clip.path,min(at,clip.end-.02)
        self._preview_timer.start()

    def load_preview(self):
        if self.preview_busy or not self.preview_request: return
        from .video_tools import _show_frame
        request = self.preview_request
        self.preview_busy = True
        def finish(data=None,error=None):
            self.preview_busy = False
            if request==self.preview_request:
                if error:self.preview_label.setText(tr('Frame preview unavailable'))
                else:_show_frame(self.preview_label,data)
            else:
                self._preview_timer.start(0)
        self.owner.lookup(lambda:video.preview_bytes(*request,max_side=480),finish,lambda error:finish(error=error))

    def save(self, checked=False, *, preview=False):
        try:
            self.model.validate(self.fps.currentData())
            model = self.model.snapshot()
            if preview:
                kind,index = self.selection
                index = index if kind=='cut' else max(0,index-1)
                model = model.preview(min(index,len(model.clips)-2))
            model.validate(self.fps.currentData())
        except UserError as error:
            self.error.setText(tr(str(error)))
            return
        max_side,fps,audio = 480 if preview else self.resolution.currentData(),self.fps.currentData(),self.audio.isChecked()
        fn = lambda:render(model,max_side=max_side,fps=fps,audio=audio)
        paths = [str(c.path) for c in model.clips]
        if self.owner.submit:
            self.owner.submit('tool:Datamosh',paths,fn,'Creating Datamosh transition…','Preview · Melt' if preview else 'Melt')
            if not preview:self.owner.close()
        else:
            self.owner.notify('tool:Datamosh',paths,fn(),'Melt')
