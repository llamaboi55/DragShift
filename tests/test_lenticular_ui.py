import time
import json
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from dragshift import lenticular as lens, report
from dragshift.app import DragShiftApp
from dragshift.lenticular_tools import LenticularWindow


@pytest.fixture(scope='module')
def qapp():
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    return app


def wait(qapp, predicate):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


@pytest.fixture
def images(tmp_path):
    paths = []
    for index, size in enumerate([(120, 80), (80, 120), (150, 90)]):
        path = tmp_path / f'{index}.png'
        Image.new('RGB', size, ('red', 'green', 'blue')[index]).save(path)
        paths.append(path)
    return paths


def test_radial_routes_for_images_and_saved_projects(qapp):
    assert 'tool:Interlace' in {item.key for item in DragShiftApp.build_rings(None, ['a.png', 'b.jpg'])['Tools']}
    rings = DragShiftApp.build_rings(None, ['project.dshift-print'])
    assert set(rings) == {'Tools'}
    assert rings['Tools'][0].key == 'tool:Interlace'


def test_drop_reorder_metadata_crop_and_export(qapp, images):
    results = []
    window = LenticularWindow(images[:2], lambda *args: results.append(args))
    window.present()
    assert window.width() <= 1000 and window.height() <= 740
    wait(qapp, lambda: window.primary_btn.isEnabled() and not window.preview_busy)
    # Drop the real MIME list, then reorder an unselected, uncached image first.
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(images[2]))])
    event = QDropEvent(QPointF(20, 20), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    window.dropEvent(event)
    assert event.isAccepted() and len(window.project.frames) == 3
    window.move_frame(-1)
    window.move_frame(-1)
    wait(qapp, lambda: window.primary_btn.isEnabled() and window.project.frames[0].path in window.sizes)
    assert window.project.frames[0].path == images[2]
    window.edges['Left'].setValue(70)
    window.edges['Right'].setValue(70)
    assert not window.primary_btn.isEnabled()
    window.print_width.setValue(25.4)
    assert not window.primary_btn.isEnabled()  # Other edits cannot hide an invalid crop.
    window.edges['Right'].setValue(10)
    assert window.primary_btn.isEnabled()
    assert window.current.crop == pytest.approx((.7, 0, .9, 1))
    window.fit.setCurrentIndex(window.fit.findData('contain'))
    window.apply_all()
    assert all(frame.fit == 'contain' for frame in window.project.frames)
    window.quality.setValue(1)
    window.save()
    assert results[0][0] == 'tool:Interlace'
    assert all(Path(path).is_file() for path in results[0][2])
    qapp.processEvents()


def test_project_save_reload_preserves_order_crop_and_grid(qapp, images, tmp_path, monkeypatch):
    from dragshift import lenticular_tools as ui
    path = tmp_path / 'тест.dshift-print'
    window = LenticularWindow(images, lambda *args: None)
    wait(qapp, lambda: window.primary_btn.isEnabled())
    window.reverse()
    window.columns.setValue(2)
    window.gap.setValue(.5)
    window.edges['Top'].setValue(20)
    monkeypatch.setattr(ui.QFileDialog, 'getSaveFileName', lambda *args: (str(path), ''))
    window.save_project()
    expected = window.project.snapshot()
    window.close()
    qapp.processEvents()
    restored = LenticularWindow([path], lambda *args: None)
    wait(qapp, lambda: restored.primary_btn.isEnabled())
    assert restored.project == expected
    assert restored.columns.value() == 2
    assert restored.edges['Top'].value() == 20
    restored.close()
    qapp.processEvents()


def test_print_uses_device_dpi_and_closes_printer(qapp, images, monkeypatch):
    from dragshift import lenticular_tools as ui
    sent = []
    class Printer:
        dpi = (600, 600)
        def emit(self, image, **kwargs):
            sent.append((image.size, kwargs))
        def close(self):
            sent.append('closed')
    monkeypatch.setattr(ui.native_print, 'supported', lambda: True)
    monkeypatch.setattr(ui.native_print, 'choose', lambda owner: (Printer(), False))
    results = []
    window = LenticularWindow(images[:2], lambda *args: results.append(args))
    wait(qapp, lambda: window.primary_btn.isEnabled())
    window.print_width.setValue(25.4)
    window.quality.setValue(1)
    window.print_result()
    assert sent == [((600, 400), {'output': None}), 'closed']
    assert results[0][0] == 'tool:Print interlace'
    assert report.summarize(*results[0]).title == 'Print job submitted'
    window.close()
    qapp.processEvents()


def test_oversized_source_is_rejected_before_preview_decode(qapp, images, monkeypatch):
    from dragshift import lenticular_tools as ui
    from dragshift.util import UserError
    decoded = []
    def oversized(path):
        raise UserError('The print raster is too large. Reduce its size or resolution.')
    monkeypatch.setattr(ui.lens,'source_size',oversized)
    monkeypatch.setattr(ui,'open_preview',lambda *args,**kwargs:decoded.append(args))
    window = LenticularWindow(images,lambda *args:None)
    wait(qapp,lambda:not window.preview_busy and not window.metadata_busy)
    assert not decoded and not window.primary_btn.isEnabled()
    assert 'too large' in window.error.text()
    window.close()
    qapp.processEvents()


def test_new_large_project_automatically_selects_usable_sampling(qapp, images):
    window = LenticularWindow((images*4)[:10], lambda *args: None)
    wait(qapp, lambda: window.primary_btn.isEnabled())
    window.print_width.setValue(2000)
    window.auto_height.setChecked(False)
    window.print_height.setValue(2000)
    window.lpi.setValue(75)
    assert window.auto_quality.isChecked() and window.project.settings.auto_quality
    assert window.quality.value() == window.project.settings.quality == 1
    assert window.primary_btn.isEnabled()
    assert window.project.geometry().sheet == (59050, 59060)
    assert window.project.geometry().dpi == 750
    window.close()
    qapp.processEvents()


def test_legacy_project_preserves_its_explicit_sampling(qapp, images, tmp_path):
    path = tmp_path/'legacy.dshift-print'
    project = lens.Project([lens.Frame(value) for value in images], lens.Settings(quality=4, lpi=75.5))
    lens.save_project(project, path)
    saved = json.loads(path.read_text(encoding='utf-8'))
    saved['settings'].pop('auto_quality', None)
    path.write_text(json.dumps(saved), encoding='utf-8')
    window = LenticularWindow([path], lambda *args: None)
    wait(qapp, lambda: window.primary_btn.isEnabled())
    assert not window.auto_quality.isChecked() and not window.project.settings.auto_quality
    assert window.quality.value() == window.project.settings.quality == 4
    window.lpi.setValue(60)
    window.receive_files(images[:1])
    assert window.quality.value() == window.project.settings.quality == 4
    window.close()
    qapp.processEvents()


def test_manual_sampling_keyboard_edit_disables_auto_and_persists(qapp, images):
    window = LenticularWindow(images, lambda *args: None)
    window.present()
    wait(qapp, lambda: window.primary_btn.isEnabled() and not window.preview_busy)
    assert window.auto_quality.isChecked()
    window.quality.setFocus()
    window.quality.lineEdit().selectAll()
    QTest.keyClicks(window.quality.lineEdit(), '6')
    QTest.keyClick(window.quality.lineEdit(), Qt.Key_Return)
    assert not window.auto_quality.isChecked() and not window.project.settings.auto_quality
    assert window.quality.value() == window.project.settings.quality == 6
    window.lpi.setValue(75)
    window.receive_files(images[:1])
    assert window.quality.value() == window.project.settings.quality == 6
    window.auto_quality.setChecked(True)
    assert window.quality.value() == window.project.settings.quality == 3
    assert window.project.settings.auto_quality
    window.close()
    qapp.processEvents()
