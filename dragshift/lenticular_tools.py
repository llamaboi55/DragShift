"""Lenticular preparation opened from the radial image tools."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, Qt, QTimer, Signal, QSignalBlocker
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QSpinBox, QVBoxLayout)

from . import lenticular as lens, native_print
from .convert import open_preview
from .glass import qimage_from_pil
from .i18n import tr
from .media_controls import DropZone, local_paths
from .tools import CropView
from .util import UserError, category, unique_path
from .video_tools import _AsyncTools, _field, _scroll_panel, SPIN_STYLE

IMAGE_FILTER = 'Images (*.png *.jpg *.jpeg *.webp *.heic *.heif *.tif *.tiff *.avif *.bmp *.gif *.ico)'
PROJECT_FILTER = 'Print projects (*.dshift-print)'


class FrameList(QListWidget):
    filesDropped = Signal(list)

    def dragEnterEvent(self, event):
        if local_paths(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dragEnterEvent(event)

    def dragMoveEvent(self, event):
        if local_paths(event.mimeData()):
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dragMoveEvent(event)

    def dropEvent(self, event):
        paths = local_paths(event.mimeData())
        if paths:
            self.filesDropped.emit(paths)
            event.setDropAction(Qt.CopyAction)
            event.accept()
        else:
            super().dropEvent(event)


def number(value, minimum, maximum, decimals=2, suffix=''):
    spin = QDoubleSpinBox()
    spin.setStyleSheet(SPIN_STYLE)
    spin.setRange(minimum, maximum)
    spin.setDecimals(decimals)
    spin.setSingleStep(.01 if decimals >= 2 else .1)
    spin.setValue(value)
    spin.setSuffix(tr(suffix))
    spin.setKeyboardTracking(False)
    return spin


class LenticularWindow(_AsyncTools):
    tool_name = 'Interlace'

    def __init__(self, paths, notify):
        super().__init__('Lenticular print', paths, notify, (940, 700))
        self.project = lens.Project(settings=lens.Settings(auto_quality=True))
        self.frame_map = {}
        self.sizes = {}
        self.current = None
        self.raw_preview = None
        self.invalid_crop = False
        self.metadata_busy = False
        self.metadata_error = None
        self.selection_serial = 0
        self.preview_busy = False
        self.refreshing = False
        self.project_path = None
        self.timer = QTimer(self, interval=100, singleShot=True, timeout=self.update_preview)
        toolbar = QHBoxLayout()
        for title, callback in [('Add images…', self.choose_images), ('Open project…', self.open_project),
                                ('Save project…', self.save_project)]:
            button = QPushButton(tr(title))
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        self.body.addLayout(toolbar)
        middle = QHBoxLayout()
        left = QVBoxLayout()
        self.list = FrameList()
        self.list.setMinimumWidth(160)
        self.list.setMaximumWidth(240)
        self.list.setAccessibleName(tr('Source image order'))
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        left.addWidget(self.list, 1)
        order = QGridLayout()
        for index, (title, callback) in enumerate([('↑', lambda: self.move_frame(-1)), ('↓', lambda: self.move_frame(1)),
                                ('Reverse order', self.reverse), ('Remove', self.remove)]):
            button = QPushButton(tr(title))
            button.clicked.connect(callback)
            order.addWidget(button, index//2, index%2)
        left.addLayout(order)
        self.drop = DropZone('Drop images or a print project here', self.accept_file)
        self.drop.setMinimumHeight(48)
        self.drop.setMaximumHeight(56)
        self.drop.filesDropped.connect(self.receive_files)
        left.addWidget(self.drop)
        middle.addLayout(left, 1)
        right = QVBoxLayout()
        instruction = QLabel(tr('Crop the selected source · drag the border'))
        instruction.setWordWrap(True)
        right.addWidget(instruction)
        blank = QImage(320, 220, QImage.Format_RGB32)
        blank.fill(Qt.white)
        self.crop = CropView(blank, 1)
        self.crop.setMinimumHeight(130)
        self.crop.setMaximumHeight(175)
        self.crop.setAccessibleName(tr('Source image crop'))
        right.addWidget(self.crop, 1)
        self.preview = QLabel(tr('Prepared frame preview'))
        self.preview.setAlignment(Qt.AlignCenter)
        self.preview.setMinimumHeight(65)
        self.preview.setMaximumHeight(110)
        self.preview.setAccessibleName(tr('Prepared frame preview'))
        right.addWidget(self.preview)
        middle.addLayout(right, 3)
        self.body.addLayout(middle, 2)
        forms = QHBoxLayout()
        global_form = QFormLayout()
        global_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.print_width = number(100, 1, 2500, suffix=' mm')
        self.print_height = number(100, 1, 2500, suffix=' mm')
        self.lpi = number(60, 1, 1000, decimals=3)
        self.quality = QSpinBox()
        self.quality.setRange(1, 100)
        self.quality.setValue(5)
        self.quality.setKeyboardTracking(False)
        self.auto_quality = QCheckBox(tr('Automatic sampling · about 900 PPI'))
        self.auto_quality.setChecked(True)
        self.auto_height = QCheckBox(tr('Height from the first image'))
        self.auto_height.setChecked(True)
        self.orientation = QComboBox()
        self.orientation.addItem(tr('Vertical lenses'), 'vertical')
        self.orientation.addItem(tr('Horizontal lenses'), 'horizontal')
        for title, widget in [('Print width', self.print_width), ('Print height', self.print_height), ('Lens pitch · LPI', self.lpi),
                              ('Pixels per frame in a lens', self.quality), ('Lens direction', self.orientation)]:
            _field(global_form, title, widget)
        global_form.addRow(self.auto_quality)
        global_form.addRow(self.auto_height)
        self.columns, self.rows = QSpinBox(), QSpinBox()
        grid = QHBoxLayout()
        for title, widget in [('Columns', self.columns), ('Rows', self.rows)]:
            widget.setRange(1, 20)
            widget.setKeyboardTracking(False)
            widget.setAccessibleName(tr(title))
            grid.addWidget(QLabel(tr(title)))
            grid.addWidget(widget)
        global_form.addRow(tr('Copies on the sheet'), grid)
        self.gap, self.margin = number(0, 0, 100, suffix=' mm'), number(0, 0, 100, suffix=' mm')
        _field(global_form, 'Gap between copies', self.gap)
        _field(global_form, 'Sheet margin', self.margin)
        self.resample_export = QCheckBox(tr('Export at printer resolution'))
        self.export_dpi = QSpinBox()
        self.export_dpi.setRange(72, 9600)
        self.export_dpi.setValue(600)
        self.export_dpi.setSuffix(' DPI')
        self.export_dpi.setKeyboardTracking(False)
        self.resample_export.setToolTip(tr('Uses the same raster sampling as direct printing. Printing from another program still depends on its scaling and driver settings.'))
        global_form.addRow(self.resample_export)
        _field(global_form, 'Printer resolution', self.export_dpi)
        global_panel = _scroll_panel(global_form)
        global_panel.setMinimumHeight(120)
        forms.addWidget(global_panel, 1)
        frame_form = QFormLayout()
        frame_form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        self.fit = QComboBox()
        for title, value in [('Fill and crop', 'cover'), ('Fit with white margins', 'contain'), ('Stretch to size', 'stretch')]:
            self.fit.addItem(tr(title), value)
        _field(frame_form, 'Image fitting', self.fit)
        self.align_x, self.align_y = number(50, 0, 100, 1, ' %'), number(50, 0, 100, 1, ' %')
        _field(frame_form, 'Horizontal position', self.align_x)
        _field(frame_form, 'Vertical position', self.align_y)
        edges = QGridLayout()
        self.edges = {}
        for index, name in enumerate(('Left', 'Top', 'Right', 'Bottom')):
            spin = number(0, 0, 99.9, 1, ' %')
            spin.setAccessibleName(tr('Crop '+name.lower()))
            self.edges[name] = spin
            edges.addWidget(QLabel(tr(name)), index//2, index%2*2)
            edges.addWidget(spin, index//2, index%2*2+1)
            spin.valueChanged.connect(self.typed_crop)
        frame_form.addRow(tr('Crop borders'), edges)
        apply_all = QPushButton(tr('Apply fitting to all frames'))
        apply_all.clicked.connect(self.apply_all)
        frame_form.addRow(apply_all)
        reset = QPushButton(tr('Reset selected frame'))
        reset.clicked.connect(self.reset_frame)
        frame_form.addRow(reset)
        self.format = QComboBox()
        for target in ('TIFF', 'PNG', 'BMP'):
            self.format.addItem(target, target)
        _field(frame_form, 'Lossless output', self.format)
        frame_panel = _scroll_panel(frame_form)
        frame_panel.setMinimumHeight(120)
        forms.addWidget(frame_panel, 1)
        self.body.addLayout(forms, 2)
        self.metrics = QLabel()
        self.metrics.setWordWrap(True)
        self.body.addWidget(self.metrics)
        note = QLabel(tr('The sheet size follows complete lenses. Gaps and margins also snap to complete lenses. '
                         'Preview shows source fitting; the exported file contains the interlace. Direct printing uses device pixels without fitting to the page.'))
        note.setWordWrap(True)
        note.setObjectName('dim')
        self.body.addWidget(note)
        self.body.addWidget(self.error)
        self.print_btn = QPushButton(tr('Print…'))
        self.print_btn.clicked.connect(self.print_result)
        self.print_btn.setEnabled(native_print.supported())
        if not native_print.supported():
            self.print_btn.setToolTip(tr('Calibrated direct printing is currently available on Windows. Export a printer raster on this platform.'))
        self.body.addLayout(self.footer('Save interlace', self.save, extra_buttons=(self.print_btn,)))
        self.list.currentRowChanged.connect(self.select)
        self.list.model().rowsMoved.connect(self.reordered)
        self.list.filesDropped.connect(self.receive_files)
        self.crop.changed.connect(self.cropped)
        self.fit.currentIndexChanged.connect(self.fit_changed)
        self.align_x.valueChanged.connect(self.fit_changed)
        self.align_y.valueChanged.connect(self.fit_changed)
        for widget in (self.print_width, self.print_height, self.lpi, self.columns, self.rows, self.gap, self.margin):
            widget.valueChanged.connect(self.parameters_changed)
        self.quality.valueChanged.connect(self.quality_changed)
        self.auto_quality.toggled.connect(self.parameters_changed)
        self.auto_height.toggled.connect(self.auto_height_changed)
        self.orientation.currentIndexChanged.connect(self.parameters_changed)
        self.resample_export.toggled.connect(self.parameters_changed)
        self.export_dpi.valueChanged.connect(self.parameters_changed)
        if len(paths) == 1 and Path(paths[0]).suffix.lower() == '.dshift-print':
            self.load_project(Path(paths[0]))
        else:
            self.receive_files(paths)
        self.parameters_changed()

    @staticmethod
    def accept_file(path):
        return category(path) == 'image' or Path(path).suffix.lower() == '.dshift-print'

    def accepts_sources(self, paths):
        return bool(paths) and all(self.accept_file(path) for path in paths)

    def receive_files(self, paths):
        paths = list(map(Path, paths))
        if len(paths) == 1 and paths[0].suffix.lower() == '.dshift-print':
            self.load_project(paths[0])
            return
        if not all(category(path) == 'image' for path in paths):
            self.error.setText(tr('Drop source images or one print project.'))
            return
        if len(self.project.frames) + len(paths) > 30:
            self.error.setText(tr('Choose between 2 and 30 source images.'))
            return
        self.project.frames.extend(lens.Frame(path) for path in paths)
        self.rebuild(len(self.project.frames)-len(paths))

    def choose_images(self):
        paths, _ = QFileDialog.getOpenFileNames(self, tr('Add images'), '', tr(IMAGE_FILTER))
        if paths:
            self.receive_files(paths)

    def rebuild(self, selected=0):
        self.refreshing = True
        self.list.clear()
        self.frame_map = {}
        for frame in self.project.frames:
            self.frame_map[id(frame)] = frame
            item = QListWidgetItem(frame.path.name)
            item.setToolTip(str(frame.path))
            item.setData(Qt.UserRole, id(frame))
            self.list.addItem(item)
        self.list.setCurrentRow(min(selected, len(self.project.frames)-1))
        self.refreshing = False
        self.paths = [frame.path for frame in self.project.frames]
        self.select(self.list.currentRow())

    def reordered(self, *_):
        if self.refreshing:
            return
        self.project.frames = [self.frame_map[self.list.item(i).data(Qt.UserRole)] for i in range(self.list.count())]
        self.paths = [frame.path for frame in self.project.frames]
        self.parameters_changed()

    def move_frame(self, delta):
        index = self.list.currentRow()
        destination = index+delta
        if 0 <= index < len(self.project.frames) and 0 <= destination < len(self.project.frames):
            self.project.frames.insert(destination, self.project.frames.pop(index))
            self.rebuild(destination)

    def reverse(self):
        self.project.frames.reverse()
        self.rebuild()

    def remove(self):
        index = self.list.currentRow()
        if index >= 0:
            self.project.frames.pop(index)
            self.rebuild(max(0, index-1))

    def select(self, index):
        if self.refreshing:
            return
        self.selection_serial += 1
        self.invalid_crop = False
        self.raw_preview = None
        self.current = self.project.frames[index] if 0 <= index < len(self.project.frames) else None
        for widget in (self.fit, self.align_x, self.align_y, self.crop, *self.edges.values()):
            widget.setEnabled(self.current is not None)
        if not self.current:
            self.preview.clear()
            self.parameters_changed()
            return
        self.sync_frame_controls()
        self.parameters_changed()
        self._load_selected()

    def _load_first_size(self):
        if self.metadata_busy or not self.project.frames:
            return
        path = self.project.frames[0].path
        if path in self.sizes or self.metadata_error and self.metadata_error[0] == path:
            return
        self.metadata_busy = True
        def done(size=None, error=None):
            self.metadata_busy = False
            if error:
                self.metadata_error = path, error
            else:
                self.sizes[path] = size
            self.parameters_changed()
        self.lookup(lambda: lens.source_size(path), done, lambda error: done(error=error))

    def _load_selected(self):
        if not self.current or self.preview_busy:
            return
        serial, path = self.selection_serial, self.current.path
        self.preview_busy = True
        def done(data=None, error=None):
            self.preview_busy = False
            if serial != self.selection_serial:
                self._load_selected()
                return
            if error:
                self.error.setText(tr(error))
                self.preview.setText(tr('Image preview unavailable'))
                return
            self.raw_preview, size = data
            self.sizes[path] = size
            self.crop.set_image(qimage_from_pil(self.raw_preview))
            self.sync_crop_view()
            self.parameters_changed()
        def read():
            size = lens.source_size(path)
            return open_preview(path, max_side=850), size
        self.lookup(read, done, lambda error: done(error=error))

    def sync_frame_controls(self):
        self.refreshing = True
        frame = self.current
        self.fit.setCurrentIndex(self.fit.findData(frame.fit))
        self.align_x.setValue(frame.align_x*100)
        self.align_y.setValue(frame.align_y*100)
        left, top, right, bottom = frame.crop
        for name, value in zip(('Left', 'Top', 'Right', 'Bottom'), (left, top, 1-right, 1-bottom)):
            self.edges[name].setValue(value*100)
        self.refreshing = False

    def sync_crop_view(self):
        if self.current and self.raw_preview:
            left, top, right, bottom = self.current.crop
            width, height = self.crop.image.width(), self.crop.image.height()
            self.crop.sel = QRectF(left*width, top*height, (right-left)*width, (bottom-top)*height)
            self.crop.update()

    def cropped(self):
        if self.refreshing or not self.current or not self.raw_preview:
            return
        rect = self.crop.sel
        width, height = self.crop.image.width(), self.crop.image.height()
        self.current.crop = (max(0., rect.left()/width), max(0., rect.top()/height),
                             min(1., rect.right()/width), min(1., rect.bottom()/height))
        self.sync_frame_controls()
        self.invalid_crop = False
        self.parameters_changed()

    def typed_crop(self):
        if self.refreshing or not self.current:
            return
        left, top, right, bottom = (self.edges[name].value()/100 for name in ('Left', 'Top', 'Right', 'Bottom'))
        if left+right >= .999 or top+bottom >= .999:
            self.invalid_crop = True
            self.parameters_changed()
            return
        self.invalid_crop = False
        self.current.crop = left, top, 1-right, 1-bottom
        self.sync_crop_view()
        self.parameters_changed()

    def fit_changed(self, *_):
        if self.refreshing or not self.current:
            return
        self.current.fit = self.fit.currentData()
        self.current.align_x, self.current.align_y = self.align_x.value()/100, self.align_y.value()/100
        self.parameters_changed()

    def parameters_changed(self, *_):
        if self.refreshing:
            return
        settings = self.project.settings
        settings.auto_quality = self.auto_quality.isChecked()
        if settings.auto_quality:
            count = max(1, len(self.project.frames))
            quality = max(1, min(100, round(900/(self.lpi.value()*count))))
            with QSignalBlocker(self.quality):
                self.quality.setValue(quality)
        for name, widget in [('width_mm', self.print_width), ('height_mm', self.print_height), ('lpi', self.lpi),
                ('quality', self.quality), ('columns', self.columns), ('rows', self.rows), ('gap_mm', self.gap), ('margin_mm', self.margin)]:
            if name == 'height_mm' and self.auto_height.isChecked():
                continue
            setattr(settings, name, widget.value())
        settings.auto_height, settings.orientation = self.auto_height.isChecked(), self.orientation.currentData()
        settings.export_dpi = self.export_dpi.value() if self.resample_export.isChecked() else 0
        self.export_dpi.setEnabled(self.resample_export.isChecked())
        self.print_height.setEnabled(not settings.auto_height)
        self.error.clear()
        self._load_first_size()
        ready = len(self.project.frames) >= 2 and self.project.frames[0].path in self.sizes
        if self.invalid_crop:
            ready = False
            self.error.setText(tr('Leave some image area inside the crop.'))
        elif self.project.frames and self.metadata_error and self.metadata_error[0] == self.project.frames[0].path:
            self.error.setText(tr(self.metadata_error[1]))
        if ready:
            try:
                geometry = self.project.geometry(self.sizes[self.project.frames[0].path])
                if settings.auto_height:
                    with QSignalBlocker(self.print_height):
                        self.print_height.setValue(geometry.tile[1]*25.4/geometry.dpi)
                width, height = geometry.size_mm
                self.metrics.setText(tr('Actual sheet: {0:.3f} × {1:.3f} mm · {2} × {3} px · {4:.3f} PPI · {5} frames').format(
                    width, height, *geometry.sheet, geometry.dpi, len(self.project.frames)))
            except UserError as error:
                ready = False
                self.metrics.clear()
                self.error.setText(tr(str(error)))
        elif not self.invalid_crop:
            self.metrics.setText(tr('Add at least two images. Drag rows to set the frame order.'))
        self.primary_btn.setEnabled(ready)
        self.print_btn.setEnabled(ready and native_print.supported())
        self.timer.start()

    def quality_changed(self, *_):
        if self.refreshing:
            return
        with QSignalBlocker(self.auto_quality):
            self.auto_quality.setChecked(False)
        self.parameters_changed()

    def auto_height_changed(self):
        if not self.refreshing and not self.auto_height.isChecked():
            with QSignalBlocker(self.print_height):
                self.print_height.setValue(self.project.settings.height_mm)
        self.parameters_changed()

    def update_preview(self):
        if not self.current or not self.raw_preview:
            return
        try:
            if len(self.project.frames) >= 2 and self.project.frames[0].path in self.sizes:
                geometry = self.project.geometry(self.sizes[self.project.frames[0].path])
                aspect = geometry.aspect
            else:
                aspect = self.raw_preview.width/self.raw_preview.height
            width, height = max(1, round(100*aspect)), 100
            if width > 600:
                width, height = 600, max(1, round(600/aspect))
            image = lens.fit_image(self.raw_preview, self.current, (width, height), aspect=aspect)
            self.preview.setPixmap(QPixmap.fromImage(qimage_from_pil(image)))
        except UserError as error:
            self.error.setText(tr(str(error)))

    def apply_all(self):
        if not self.current or self.invalid_crop:
            return
        for frame in self.project.frames:
            frame.fit, frame.crop = self.current.fit, self.current.crop
            frame.align_x, frame.align_y = self.current.align_x, self.current.align_y
        self.parameters_changed()

    def reset_frame(self):
        if self.current:
            self.invalid_crop = False
            self.current.fit, self.current.crop = 'cover', (0., 0., 1., 1.)
            self.current.align_x = self.current.align_y = .5
            self.sync_frame_controls()
            self.sync_crop_view()
            self.parameters_changed()

    def open_project(self):
        path, _ = QFileDialog.getOpenFileName(self, tr('Open print project'), '', tr(PROJECT_FILTER))
        if path:
            self.load_project(path)

    def load_project(self, path):
        try:
            project = lens.load_project(path)
        except UserError as error:
            self.error.setText(tr(str(error)))
            return
        self.project, self.project_path = project, Path(path)
        self.refreshing = True
        settings = project.settings
        for name, widget in [('width_mm', self.print_width), ('height_mm', self.print_height), ('lpi', self.lpi),
                ('quality', self.quality), ('columns', self.columns), ('rows', self.rows), ('gap_mm', self.gap), ('margin_mm', self.margin)]:
            widget.setValue(getattr(settings, name))
        self.orientation.setCurrentIndex(self.orientation.findData(settings.orientation))
        with QSignalBlocker(self.auto_quality):
            self.auto_quality.setChecked(settings.auto_quality)
        self.auto_height.setChecked(settings.auto_height)
        self.resample_export.setChecked(bool(settings.export_dpi))
        self.export_dpi.setValue(settings.export_dpi or 600)
        self.refreshing = False
        self.sizes.clear()
        self.metadata_error = None
        self.rebuild()

    def save_project(self):
        if self.invalid_crop:
            return
        initial = str(self.project_path) if self.project_path else str(self.path.with_suffix('.dshift-print'))
        path, _ = QFileDialog.getSaveFileName(self, tr('Save print project'), initial, tr(PROJECT_FILTER))
        if path:
            try:
                lens.save_project(self.project, path)
                self.project_path = Path(path)
                self.error.setText(tr('Project saved'))
            except (UserError, OSError) as error:
                self.error.setText(tr(str(error)))

    def save(self):
        if not self.primary_btn.isEnabled():
            return
        project, target = self.project.snapshot(), self.format.currentData()
        self.run(lambda: lens.export(project, target), 'Preparing lenticular interlace…')

    def print_result(self):
        if not self.print_btn.isEnabled():
            return
        printer = None
        try:
            self.project.validate()
            project = self.project.snapshot()
            printer, to_file = native_print.choose(int(self.winId()))
            if printer is None:
                return
            output = None
            if to_file:
                selected, _ = QFileDialog.getSaveFileName(self, tr('Save printer job'), str(self.path.with_suffix('.prn')), 'Printer job (*.prn)')
                if not selected:
                    printer.close()
                    return
                output = unique_path(selected)
            def work():
                try:
                    geometry = project.geometry()
                    size = tuple(round(p/geometry.dpi*dpi) for p, dpi in zip(geometry.sheet, printer.dpi))
                    if lens.needs_streaming(geometry.sheet) or lens.needs_streaming(size):
                        from . import lenticular_stream
                        if any(a > b for a, b in zip(size, printer.printable)):
                            raise UserError('The image does not fit the printable area. Reduce its size or choose a larger paper size; automatic fitting is disabled.')
                        with lenticular_stream.prepared(project, dpi=printer.dpi) as (raster, _):
                            printer.emit(raster, output=output)
                        return [output] if output else []
                    image, geometry = lens.render(project)
                    raster = lens.printer_raster(image, geometry, printer.dpi)
                    printer.emit(raster, output=output)
                    return [output] if output else []
                finally:
                    printer.close()
            self.paths = [frame.path for frame in project.frames]
            if self.submit:
                self.submit('tool:Print interlace', list(map(str, self.paths)), work, 'Preparing print raster…', 'Print job submitted')
                self.close()
            else:
                self.notify('tool:Print interlace', self.paths, work(), 'Print job submitted')
        except (UserError, OSError) as error:
            if printer:
                printer.close()
            self.error.setText(tr(str(error)))

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
