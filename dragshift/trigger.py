"""Watches for "left button held + mouse moved + Shift pressed" anywhere on screen, which
is what a Shift+drag of a file looks like. Polling global input state is simple, needs no
admin rights and can't stall the system the way a broken hook can.
Windows: GetAsyncKeyState. macOS: NSEvent.pressedMouseButtons / modifierFlags (no
Accessibility or Input Monitoring permission required). Linux: X11 pointer state, or the
kernel input devices on Wayland (see platform_wayland)."""
from __future__ import annotations

import ctypes
import sys

from PySide6.QtCore import QObject, QPoint, QTimer, Signal
from PySide6.QtGui import QCursor

VK_LBUTTON, VK_SHIFT, VK_ESCAPE = 0x01, 0x10, 0x1B
DRAG_THRESHOLD = 10   # px the mouse must travel with the button held
NO_DRAG_TIMEOUT = 50  # ticks (≈ 0.75 s) to wait for a file drag to enter before hiding


class DragShiftWatcher(QObject):
    show_menu = Signal(QPoint)
    toggle_ring = Signal()
    hide_menu = Signal()

    def __init__(self, menu_visible, menu_got_drag, parent=None):
        super().__init__(parent)
        self.menu_visible = menu_visible
        self.menu_got_drag = menu_got_drag
        self.enabled = True
        self.vk = VK_SHIFT
        self.key_name = "shift"
        self._down_at: QPoint | None = None
        self._shift_was = False
        self._fired = False
        self._idle_ticks = 0
        self._shown_at = QPoint()
        self._user32 = ctypes.windll.user32 if sys.platform == "win32" else None
        self._mac = None
        if sys.platform == "darwin":
            from . import platform_mac
            self._mac = platform_mac if platform_mac.OK else None
        elif sys.platform.startswith("linux"):
            from . import platform_linux, platform_wayland
            # same interface as platform_mac: left_button_down / key_down / escape_down
            if platform_wayland.is_wayland():
                self._mac = platform_wayland if platform_wayland.ready() else None
            else:
                self._mac = platform_linux if platform_linux.available() else None
        # where the cursor is; on Wayland only relative travel is known
        self._pointer = getattr(self._mac, "pointer", QCursor.pos)
        self.timer = QTimer(self, interval=15, timeout=self._tick)

    def set_key(self, name: str):
        self.key_name = name
        self.vk = {"shift": VK_SHIFT, "ctrl": 0x11, "alt": 0x12}.get(name, VK_SHIFT)

    def start(self):
        if self._user32 is not None or self._mac is not None:
            self.timer.start()

    def _down(self, vk: int) -> bool:
        return bool(self._user32.GetAsyncKeyState(vk) & 0x8000)

    def _state(self) -> tuple[bool, bool, bool]:
        """(left button, trigger key, escape)"""
        if self._mac is not None:
            m = self._mac
            return m.left_button_down(), m.key_down(self.key_name), m.escape_down()
        return self._down(VK_LBUTTON), self._down(self.vk), self._down(VK_ESCAPE)

    def _tick(self):
        lbtn, shift, esc = self._state()
        pos = self._pointer()

        if self.menu_visible():
            if not lbtn or esc:
                # drop happened (or was cancelled) – give the drop event a moment to land
                QTimer.singleShot(120, self.hide_menu.emit)
            elif shift and not self._shift_was and self.menu_got_drag():
                self.toggle_ring.emit()       # tap Shift again to flip Convert ⇄ Tools
            elif not self.menu_got_drag() and (pos - self._shown_at).manhattanLength() > 6:
                # the cursor is moving over us but no file drag arrived: it's a window
                # move or text selection, so get out of the way
                self._idle_ticks += 1
                if self._idle_ticks > NO_DRAG_TIMEOUT:
                    self.hide_menu.emit()
        else:
            if lbtn:
                if self._down_at is None:
                    self._down_at = pos
                    self._fired = False
                moved = (pos - self._down_at).manhattanLength() >= DRAG_THRESHOLD
                if self.enabled and moved and shift and not self._fired:
                    self._fired = True
                    self._idle_ticks = 0
                    self._shown_at = pos
                    self.show_menu.emit(pos)
            else:
                self._down_at = None
                self._fired = False
        self._shift_was = shift
