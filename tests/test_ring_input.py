"""Only deliberate input switches rings; cursor motion is never a shortcut."""
import pytest
from PySide6.QtCore import QPoint, Qt, QEvent
from PySide6.QtGui import QKeyEvent
from PySide6.QtTest import QTest

from dragshift.radial import RadialMenu, Item
from dragshift.trigger import DragShiftWatcher


def make_ring():
    ring = RadialMenu(lambda _: {'Convert': [Item('ZIP')], 'Tools': [Item('Pack')]}, lambda _: '1 file')
    ring._load(['example.txt'])
    return ring


@pytest.mark.parametrize('click_mode', [False, True])
def test_returning_to_hub_never_switches_ring(qt_session, click_mode):
    ring = make_ring()
    ring.click_mode = click_mode
    try:
        ring._set_hover(0)
        ring._set_hover(-2)
        QTest.qWait(650)  # Old dwell shortcut fired after 420 ms.
        assert ring.ring_idx == 0
        ring._set_hover(-1)
        ring._set_hover(-2)
        QTest.qWait(650)
        assert ring.ring_idx == 0
    finally:
        ring.close()


@pytest.mark.parametrize('key', ['shift', 'ctrl', 'alt'])
def test_watcher_switches_once_per_fresh_trigger_press(qt_session, key, monkeypatch):
    ring = make_ring()
    watcher = DragShiftWatcher(lambda: True, lambda: True)
    watcher.set_key(key)
    watcher.toggle_ring.connect(ring.next_ring)
    state = [True, True, False]
    monkeypatch.setattr(watcher, '_state', lambda: tuple(state))
    watcher._shift_was = True  # Trigger held while the menu appeared.
    for _ in range(50):
        watcher._tick()
    assert ring.ring_idx == 0
    state[1] = False
    watcher._tick()
    state[1] = True
    watcher._tick()
    assert ring.ring_idx == 1
    for _ in range(50):
        watcher._tick()
    assert ring.ring_idx == 1
    state[1] = False
    watcher._tick()
    state[1] = True
    watcher._tick()
    assert ring.ring_idx == 0
    ring.close()


def test_keyboard_repeat_and_drag_key_event_do_not_duplicate_global_trigger(qt_session):
    ring = make_ring()
    repeat = QKeyEvent(QEvent.KeyPress, Qt.Key_Shift, Qt.ShiftModifier, '', True, 1)
    ring.click_mode = True
    ring.keyPressEvent(repeat)
    assert ring.ring_idx == 0
    ring.click_mode = False
    ring.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Shift, Qt.ShiftModifier))
    assert ring.ring_idx == 0
    ring.click_mode = True
    QTest.keyClick(ring, Qt.Key_Shift)
    assert ring.ring_idx == 1
    QTest.mouseClick(ring, Qt.LeftButton, pos=QPoint(200, 200))
    assert ring.ring_idx == 0
    ring.close()
