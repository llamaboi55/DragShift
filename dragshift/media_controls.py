"""Small shared controls for precise media editing, local drops and timelines."""

from .i18n import tr, tr_items
from pathlib import Path
import re

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout,
    QLabel, QListWidget, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from .theme import BORDER, MUTED, SELECTED, TEXT
from .util import resolve_shortcuts


def local_paths(mime):
    if not mime.hasUrls():
        return []
    return [Path(p) for p in resolve_shortcuts([u.toLocalFile() for u in mime.urls() if u.isLocalFile()])
            if Path(p).is_file() or Path(p).is_dir()]


def value_editor(slider, label='', fmt=str):
    value = QSpinBox()
    value.setRange(slider.minimum(), slider.maximum())
    value.setValue(slider.value())
    value.setKeyboardTracking(False)
    value.setAccessibleName(tr(label or slider.accessibleName() or 'Value'))
    value.setMinimumWidth(84)
    suffix = re.sub(r'^[+−\-]?\d+', '', fmt(slider.value()))
    value.setSuffix(tr(suffix))
    slider.valueChanged.connect(value.setValue)
    value.valueChanged.connect(slider.setValue)
    return value


class DropZone(QLabel):
    filesDropped = Signal(list)

    def __init__(self, text, accept=lambda path: True):
        super().__init__(tr(text))
        self.accept_file = accept
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setMinimumHeight(75)
        self.setAccessibleName(tr(text))
        self._style(False)

    def _style(self, active):
        self.setStyleSheet(f'QLabel {{border:1px dashed {TEXT if active else BORDER};border-radius:8px;'
                           f'padding:10px;background:{SELECTED if active else "transparent"};}}')

    def accepted(self, mime):
        files = local_paths(mime)
        return files if files and all(self.accept_file(p) for p in files) else []

    def dragEnterEvent(self, event):
        if self.accepted(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
            self._style(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        self.dragEnterEvent(event)

    def dragLeaveEvent(self, event):
        self._style(False)

    def dropEvent(self, event):
        self._style(False)
        files = self.accepted(event.mimeData())
        if files:
            event.setDropAction(Qt.CopyAction)
            event.accept()
            self.filesDropped.emit(files)
        else:
            event.ignore()


class _SequenceList(QListWidget):
    filesDropped = Signal(list)

    def __init__(self, accept):
        super().__init__()
        self.accept_file = accept
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAccessibleName(tr('Clip order · drag to reorder; drop files to append'))
        self.setMinimumHeight(90)
        self.setMaximumHeight(155)

    def _external(self, event):
        files = local_paths(event.mimeData())
        return files if files and all(self.accept_file(p) for p in files) else []

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls():
            if self._external(event):
                event.setDropAction(Qt.CopyAction)
                event.accept()
            else:
                event.ignore()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            self.dragEnterEvent(event)
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        if event.mimeData().hasUrls():
            files = self._external(event)
            if files:
                event.setDropAction(Qt.CopyAction)
                event.accept()
                self.filesDropped.emit(files)
            else:
                event.ignore()
        else:
            super().dropEvent(event)


class FileSequence(QWidget):
    changed = Signal(list)

    def __init__(self, paths, accept, file_filter):
        super().__init__()
        self.file_filter = file_filter
        self.accept_file = accept
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.list = _SequenceList(accept)
        layout.addWidget(self.list)
        buttons = QHBoxLayout()
        for text, fn in [('Add files…', self.choose), ('↑', lambda: self.move(-1)),
                         ('↓', lambda: self.move(1)), ('Remove', self.remove)]:
            b = QPushButton(tr(text))
            b.clicked.connect(fn)
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.list.filesDropped.connect(self.add)
        self.list.model().rowsMoved.connect(lambda *_: self.changed.emit(self.paths()))
        self.add(paths)

    def paths(self):
        return [Path(self.list.item(i).data(Qt.UserRole)) for i in range(self.list.count())]

    def add(self, paths):
        from PySide6.QtWidgets import QListWidgetItem
        for path in paths:
            if self.accept_file(Path(path)):
                item = QListWidgetItem(Path(path).name)
                item.setToolTip(tr(str(path)))
                item.setData(Qt.UserRole, str(path))
                self.list.addItem(tr(item))
        self.changed.emit(self.paths())

    def choose(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('Add files'), str(self.paths()[0].parent) if self.paths() else '', tr(self.file_filter))
        self.add(paths)

    def move(self, delta):
        row = self.list.currentRow()
        target = row + delta
        if 0 <= row < self.list.count() and 0 <= target < self.list.count():
            self.list.insertItem(target, self.list.takeItem(row))
            self.list.setCurrentRow(target)
            self.changed.emit(self.paths())

    def remove(self):
        row = self.list.currentRow()
        if row >= 0:
            self.list.takeItem(row)
            self.changed.emit(self.paths())


class TrimRange(QWidget):
    changed = Signal(float, float)

    def __init__(self):
        super().__init__()
        self.duration = 1.
        self.start, self.end = 0., 1.
        self.active = 0
        self.dragging = False
        self.setMinimumHeight(48)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr('Trim range · drag start and end handles; arrows adjust selected handle'))
        self.setToolTip(tr('Drag either end. Left/right: 0.01 s; Shift + arrows: 0.1 s. Up/down selects the other handle.'))

    def set_values(self, start, end, duration=None):
        if duration is not None:
            self.duration = max(.04, duration)
        self.start = max(0., min(start, self.duration))
        self.end = max(self.start, min(end, self.duration))
        self.update()

    def _x(self, value):
        return 14 + value / self.duration * max(1, self.width() - 28)

    def _change(self, value):
        if self.active == 0:
            self.start = max(0., min(value, self.end - .04))
        else:
            self.end = min(self.duration, max(value, self.start + .04))
        self.update()
        self.changed.emit(self.start, self.end)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        x = event.position().x()
        self.active = 0 if abs(x - self._x(self.start)) <= abs(x - self._x(self.end)) else 1
        self.dragging = True
        self.setFocus()
        self.mouseMoveEvent(event)

    def mouseMoveEvent(self, event):
        if self.dragging:
            self._change((event.position().x() - 14) / max(1, self.width() - 28) * self.duration)

    def mouseReleaseEvent(self, event):
        self.dragging = False

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Left, Qt.Key_Right):
            step = .1 if event.modifiers() & Qt.ShiftModifier else .01
            self._change((self.start if self.active == 0 else self.end) + step * (1 if event.key() == Qt.Key_Right else -1))
        elif event.key() in (Qt.Key_Up, Qt.Key_Down, Qt.Key_Space):
            self.active = 1 - self.active
            self.update()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y = self.height() / 2
        p.setPen(QPen(QColor(BORDER), 5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(14, y), QPointF(self.width() - 14, y))
        p.setPen(QPen(QColor(TEXT), 5, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(self._x(self.start), y), QPointF(self._x(self.end), y))
        for i, value in enumerate((self.start, self.end)):
            p.setPen(QPen(QColor(TEXT), 2 if i == self.active and self.hasFocus() else 1))
            p.setBrush(QColor('white'))
            p.drawRoundedRect(QRectF(self._x(value)-6, y-12, 12, 24), 4, 4)


class StrengthCurve(QWidget):
    changed = Signal(object)
    selected = Signal(int)

    def __init__(self):
        super().__init__()
        self.points = [(0., 1.), (1., 1.)]
        self.markers = []
        self.index = 0
        self.dragging = False
        self.duration = None
        self.setMinimumHeight(170)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName(tr('Effect strength curve'))
        self.setToolTip(tr('Click to add a point. Drag a point up for more effect, down for less. Right-click or Delete removes it.'))

    def area(self):
        return QRectF(34, 12, max(1, self.width()-50), max(1, self.height()-38))

    def position(self, point):
        r = self.area()
        return QPointF(r.left()+point[0]*r.width(), r.bottom()-point[1]*r.height())

    def _near(self, pos):
        return min(range(len(self.points)), key=lambda i: (self.position(self.points[i])-pos).manhattanLength())

    def set_point(self, x, y):
        i = self.index
        x = 0. if i == 0 else 1. if i == len(self.points)-1 else max(self.points[i-1][0]+.001, min(x, self.points[i+1][0]-.001))
        self.points[i] = (x, max(0., min(1., y)))
        self.update()
        self.changed.emit(list(self.points))

    def mousePressEvent(self, event):
        index = self._near(event.position())
        offset = self.position(self.points[index])-event.position()
        near = offset.x()**2+offset.y()**2 <= 18**2
        if not near:
            if event.button()==Qt.LeftButton:
                self.insert(event.position())
                self.dragging=True
            return
        self.index = index
        self.selected.emit(self.index)
        self.setFocus()
        if event.button() == Qt.RightButton:
            self.remove()
        elif event.button() == Qt.LeftButton:
            self.dragging = True

    def mouseMoveEvent(self, event):
        if self.dragging:
            r = self.area()
            self.set_point((event.position().x()-r.left())/r.width(), (r.bottom()-event.position().y())/r.height())

    def mouseReleaseEvent(self, event):
        self.dragging = False

    def mouseDoubleClickEvent(self, event):
        if event.button()==Qt.LeftButton:
            self.insert(event.position())

    def insert(self,pos):
        self.setFocus()
        if len(self.points) >= 64 or not self.area().contains(pos):
            return
        r = self.area()
        x = max(.001, min(.999, (pos.x()-r.left())/r.width()))
        if any(abs(x-p[0]) < .005 for p in self.points):
            return
        point = (x, max(0., min(1., (r.bottom()-pos.y())/r.height())))
        self.points.append(point)
        self.points.sort()
        self.index = self.points.index(point)
        self.selected.emit(self.index)
        self.changed.emit(list(self.points))
        self.update()

    def remove(self):
        if 0 < self.index < len(self.points)-1:
            self.points.pop(self.index)
            self.index = min(self.index, len(self.points)-1)
            self.selected.emit(self.index)
            self.changed.emit(list(self.points))
            self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            x, y = self.points[self.index]
            self.set_point(x + (.01 if event.key() == Qt.Key_Right else -.01 if event.key() == Qt.Key_Left else 0),
                           y + (.01 if event.key() == Qt.Key_Up else -.01 if event.key() == Qt.Key_Down else 0))
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.area()
        p.setPen(QPen(QColor(BORDER), 1))
        for part in (0, .5, 1):
            y = r.bottom()-part*r.height()
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
        p.setPen(QColor(MUTED))
        for i, marker in enumerate(self.markers):
            x = r.left() + marker*r.width()
            p.setPen(QPen(QColor(BORDER),1,Qt.DashLine))
            p.drawLine(QPointF(x,r.top()),QPointF(x,r.bottom()))
            p.setPen(QColor(MUTED))
            p.drawText(QRectF(x+3,r.top()+2,25,18),Qt.AlignLeft,tr(str(i+2)))
        p.drawText(QRectF(0, r.top()-7, 30, 18), Qt.AlignRight, tr('100%'))
        p.drawText(QRectF(0, r.bottom()-9, 30, 18), Qt.AlignRight, tr('0%'))
        for value in (0,.5,1):
            label=f'{value*self.duration:.1f} s' if self.duration else f'{value*100:g}%'
            x=r.left()+value*r.width()
            align=Qt.AlignLeft if value==0 else Qt.AlignRight if value==1 else Qt.AlignCenter
            p.drawText(QRectF(x if value==0 else x-70 if value==1 else x-35,r.bottom()+4,70,20),align,tr(label))
        path = QPainterPath(self.position(self.points[0]))
        for point in self.points[1:]:
            path.lineTo(self.position(point))
        p.setPen(QPen(QColor(TEXT), 2))
        p.drawPath(path)
        for i, point in enumerate(self.points):
            p.setBrush(QColor(TEXT if i == self.index else 'white'))
            p.drawEllipse(self.position(point), 5, 5)


class CurveEditor(QWidget):
    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        self.curve = StrengthCurve()
        layout.addWidget(self.curve)
        row = QHBoxLayout()
        self.time, self.level = QDoubleSpinBox(), QDoubleSpinBox()
        self.duration = 100.
        for spin, name in ((self.time, 'Curve point position'), (self.level, 'Curve point strength')):
            spin.setRange(0, 100)
            spin.setDecimals(1)
            spin.setSuffix(tr(' %'))
            spin.setKeyboardTracking(False)
            spin.setAccessibleName(tr(name))
        row.addWidget(QLabel(tr('Point')))
        row.addWidget(self.time)
        row.addWidget(QLabel(tr('Strength')))
        row.addWidget(self.level)
        reset = QPushButton(tr('Reset curve'))
        reset.clicked.connect(self.reset)
        row.addWidget(reset)
        layout.addLayout(row)
        self.curve.selected.connect(self.sync)
        self.curve.changed.connect(self.sync)
        self.time.valueChanged.connect(self.typed)
        self.level.valueChanged.connect(self.typed)
        self.sync()

    def points(self):
        return list(self.curve.points)

    def typed(self):
        self.curve.set_point(self.time.value()/self.duration, self.level.value()/100)

    def set_duration(self,seconds):
        self.duration=max(.001,seconds)
        self.curve.duration=seconds
        self.time.blockSignals(True)
        self.time.setRange(0,self.duration)
        self.time.setSuffix(tr(' s'))
        self.time.blockSignals(False)
        self.sync()
        self.curve.update()

    def sync(self, *_):
        point = self.curve.points[self.curve.index]
        for spin, value in ((self.time, point[0]*self.duration), (self.level, point[1]*100)):
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        self.time.setEnabled(0 < self.curve.index < len(self.curve.points)-1)

    def reset(self):
        self.curve.points = [(0.,1.),(1.,1.)]
        self.curve.index = 0
        self.curve.changed.emit(self.points())
        self.curve.update()


class EffectEnvelope(QWidget):
    """Simple time shapes first; drawing points is an optional advanced mode."""
    def __init__(self):
        super().__init__()
        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        row=QHBoxLayout()
        row.addWidget(QLabel(tr('Effect over time')))
        self.shape=QComboBox()
        for name,key in [('Even throughout','constant'),('Gradually stronger','build'),
                         ('Gradually weaker','fade'),('Burst in the middle','pulse'),('Custom points…','custom')]:
            self.shape.addItem(tr(name),key)
        self.shape.setAccessibleName(tr('Effect over time'))
        row.addWidget(self.shape,1)
        layout.addLayout(row)
        self.hint=QLabel(tr('The intensity slider controls the amount of effect.'))
        self.hint.setWordWrap(True)
        self.hint.setObjectName('dim')
        layout.addWidget(self.hint)
        self.editor=CurveEditor()
        self.curve=self.editor.curve
        layout.addWidget(self.editor)
        self.shape.currentIndexChanged.connect(self._shape_changed)
        self._shape_changed()

    def _shape_changed(self,*_):
        key=self.shape.currentData()
        self.editor.setVisible(key=='custom')
        hints={'constant':'The same intensity throughout. No points to draw.',
               'build':'Starts clean and gradually reaches the selected intensity.',
               'fade':'Starts at the selected intensity and gradually becomes clean.',
               'pulse':'Clean at both ends; strongest in the middle.',
               'custom':'Click to add a point. Up = more effect, down = less. Drag to move; right-click to remove.'}
        self.hint.setText(tr(hints[key]))
        if key!='custom':
            self.curve.points={'constant':[(0,1),(1,1)],'build':[(0,0),(1,1)],
                               'fade':[(0,1),(1,0)],'pulse':[(0,0),(.5,1),(1,0)]}[key]
            self.curve.index=0
            self.editor.sync()
            self.curve.update()

    def points(self):
        return self.editor.points()

    def sync(self):
        self.shape.setCurrentIndex(4)
        self.editor.sync()

    def set_duration(self,seconds):
        self.editor.set_duration(seconds)
