from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from dragshift.radial import Item, RadialMenu


def test_final_drop_payload_wins_over_stale_drag_entry(tmp_path):
    app = QApplication.instance() or QApplication([])
    first, chosen = tmp_path / 'neighbor.mp4', tmp_path / 'chosen.mp4'
    first.touch()
    chosen.touch()
    ring = RadialMenu(lambda paths: {'Convert': [Item('WEBM', key='convert:WEBM')]}, lambda paths: paths[0])
    ring._load([str(first)])
    got = []
    ring.activated.connect(lambda item, paths: got.append(paths))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(chosen))])
    event = QDropEvent(QPointF(200, 90), Qt.CopyAction | Qt.MoveAction, mime, Qt.LeftButton, Qt.ShiftModifier)
    ring.dropEvent(event)
    app.processEvents()
    assert len(got) == 1 and [__import__('pathlib').Path(p) for p in got[0]] == [chosen]
    assert event.dropAction() == Qt.CopyAction
    ring.close()


def test_missing_payload_never_runs_cached_file():
    app = QApplication.instance() or QApplication([])
    ring = RadialMenu(lambda paths: {'Convert': [Item('WEBM', key='convert:WEBM')]}, lambda paths: '')
    ring._load(['neighbor.mp4'])
    got = []
    ring.activated.connect(lambda *args: got.append(args))
    mime = QMimeData()  # QDropEvent borrows it; retain until dispatch completes.
    event = QDropEvent(QPointF(200, 90), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    ring.dropEvent(event)
    app.processEvents()
    assert not got and not event.isAccepted()
    ring.close()
