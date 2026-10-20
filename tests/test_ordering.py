"""Headless Qt checks for the combination confirmation flow."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import pytest
from PySide6.QtWidgets import QApplication
from PySide6.QtCore import QModelIndex
from PySide6.QtGui import QPalette
from dragshift.ordering import CombineOrderWindow
from dragshift import imaging, convert

@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])

def test_order_is_preserved_until_confirmed_and_can_be_changed(app, tmp_path):
    calls = []
    paths = [str(tmp_path / name) for name in ("C.pdf", "A.pdf", "B.pdf")]
    w = CombineOrderWindow(paths, "Merge PDFs", calls.append)
    assert not calls
    assert w.ordered_paths() == paths
    w.files.setCurrentRow(2)
    w.move_selected(-1)
    w.move_selected(-1)
    assert w.ordered_paths() == [paths[2], paths[0], paths[1]]
    assert not w.up.isEnabled()
    w.move_selected(-1)
    w.accept_order()
    w.accept_order()
    assert calls == [[paths[2], paths[0], paths[1]]]
    assert paths == [str(tmp_path / name) for name in ("C.pdf", "A.pdf", "B.pdf")]

def test_cancel_never_submits(app):
    calls = []
    w = CombineOrderWindow(["a.pdf", "b.pdf"], "Merge PDFs", calls.append)
    w.close()
    assert calls == []

def test_confirmed_order_reaches_pdf_content_and_first_source_folder(app, tmp_path):
    paths = []
    for name in ("first", "second"):
        folder = tmp_path / name
        folder.mkdir()
        src = folder / "same-name.pdf"
        with convert.fitz.open() as doc:
            doc.new_page().insert_text((72, 72), name)
            doc.save(src)
        paths.append(str(src))
    outputs = []
    w = CombineOrderWindow(paths, "Merge PDFs", lambda ordered: outputs.append(imaging.merge_pdfs(ordered)))
    w.files.setCurrentRow(1)
    w.move_selected(-1)
    assert not outputs
    w.accept_order()
    assert outputs[0].parent == tmp_path / "second"
    with convert.fitz.open(outputs[0]) as result:
        assert result.page_count == 2
        assert "second" in result[0].get_text()
        assert "first" in result[1].get_text()
    assert all(__import__('pathlib').Path(p).exists() for p in paths)


def test_drag_model_move_renumbers_and_keeps_paths(app):
    w = CombineOrderWindow(["a.pdf", "b.pdf", "c.pdf"], "Merge PDFs", lambda _: None)
    model = w.files.model()
    assert model.moveRow(QModelIndex(), 0, QModelIndex(), 3)
    assert w.ordered_paths() == ["b.pdf", "c.pdf", "a.pdf"]
    assert w.files.item(0).text() == "1. b.pdf"
    assert w.files.item(2).text() == "3. a.pdf"
    w.close()


def test_file_list_keeps_readable_text_on_light_application_palette(app):
    previous = app.palette()
    light = QPalette()
    light.setColor(QPalette.Base, "white")
    light.setColor(QPalette.Text, "black")
    app.setPalette(light)
    try:
        w = CombineOrderWindow(["a.pdf", "b.pdf"], "Merge PDFs", lambda _: None)
        w.ensurePolished()
        w.files.ensurePolished()
        assert w.files.palette().color(QPalette.Base).lightness() < 80
        assert w.files.palette().color(QPalette.Text).lightness() > 200
        w.close()
    finally:
        app.setPalette(previous)


@pytest.mark.parametrize("paths,key,engine", [
    (["b.pdf", "a.pdf"], "tool:Merge", "merge"),
    (["b.png", "a.png"], "convert:PDF", "images"),
])
def test_controller_waits_for_confirmation_then_submits_confirmed_order(app, monkeypatch, paths, key, engine):
    from dragshift.app import DragShiftApp
    from dragshift.radial import Item
    from types import SimpleNamespace
    submitted, windows, converted = [], [], []
    def submit(action, sources, fn, working):
        submitted.append((action, sources.copy()))
        fn()
    controller = SimpleNamespace(open_window=windows.append, submit=submit)
    monkeypatch.setattr(imaging, "merge_pdfs", lambda srcs: converted.append(srcs.copy()))
    monkeypatch.setattr(convert, "images_to_single_pdf", lambda srcs: converted.append(srcs.copy()))
    DragShiftApp.run_item(controller, Item("Combine", key=key), paths)
    assert not submitted and not converted
    w = windows[0]
    w.files.setCurrentRow(1)
    w.move_selected(-1)
    w.accept_order()
    assert submitted == [(key, list(reversed(paths)))]
    assert converted == [list(reversed(paths))]
