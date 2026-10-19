"""Linux integration (X11 — XFCE, MATE, Cinnamon, KDE/GNOME on Xorg).

* Trigger: XQueryPointer gives the global button + modifier state; XQueryKeymap gives Escape.
  No extra packages or permissions needed (plain libX11 via ctypes).
* Compositing: XFCE often runs without a compositor (xfwm4 compositing off, or slow GPUs).
  Then translucent windows would show black corners, so callers shape windows with masks.
* Wayland sessions don't expose global input state, so the Shift trigger is unavailable
  there; right-click → Open With DragShift still works.
* Start at login: ~/.config/autostart/dragshift.desktop
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

BUTTON1, SHIFT, CONTROL, MOD1 = 1 << 8, 1 << 0, 1 << 2, 1 << 3
KEY_MASKS = {"shift": SHIFT, "ctrl": CONTROL, "alt": MOD1}


class _X11:
    def __init__(self):
        self.ok = False
        if os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen"):
            return
        path = ctypes.util.find_library("X11")
        if not path or not os.environ.get("DISPLAY"):
            return
        try:
            x = ctypes.cdll.LoadLibrary(path)
            x.XOpenDisplay.restype = ctypes.c_void_p
            x.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x.XDefaultRootWindow.restype = ctypes.c_ulong
            x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            x.XQueryPointer.argtypes = [ctypes.c_void_p, ctypes.c_ulong] + [ctypes.c_void_p] * 7
            x.XQueryKeymap.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
            x.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x.XKeysymToKeycode.restype = ctypes.c_ubyte
            x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x.XInternAtom.restype = ctypes.c_ulong
            x.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x.XGetSelectionOwner.restype = ctypes.c_ulong
            x.XDefaultScreen.argtypes = [ctypes.c_void_p]
            self.x = x
            self.dpy = x.XOpenDisplay(None)       # our own connection: thread-safe vs Qt's
            if not self.dpy:
                return
            self.root = x.XDefaultRootWindow(self.dpy)
            self.esc = x.XKeysymToKeycode(self.dpy, 0xFF1B)
            self.ok = True
        except Exception:
            self.ok = False

    def mask(self) -> int:
        w = ctypes.c_ulong()
        i = ctypes.c_int()
        m = ctypes.c_uint()
        self.x.XQueryPointer(self.dpy, self.root, ctypes.byref(w), ctypes.byref(w), ctypes.byref(i),
                             ctypes.byref(i), ctypes.byref(i), ctypes.byref(i), ctypes.byref(m))
        return m.value

    def escape(self) -> bool:
        keys = ctypes.create_string_buffer(32)
        self.x.XQueryKeymap(self.dpy, keys)
        return bool(keys.raw[self.esc // 8] & (1 << (self.esc % 8)))

    def composited(self) -> bool:
        scr = self.x.XDefaultScreen(self.dpy)
        atom = self.x.XInternAtom(self.dpy, f"_NET_WM_CM_S{scr}".encode(), 0)
        return bool(self.x.XGetSelectionOwner(self.dpy, atom))


_X = None


def x11() -> _X11:
    global _X
    if _X is None:
        _X = _X11()
    return _X


def is_wayland() -> bool:
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or (
        bool(os.environ.get("WAYLAND_DISPLAY")) and not os.environ.get("DISPLAY"))


def available() -> bool:
    return x11().ok


# ---------------------------------------------------------------- input state
def left_button_down() -> bool:
    return bool(x11().mask() & BUTTON1)


def key_down(name: str) -> bool:
    return bool(x11().mask() & KEY_MASKS.get(name, SHIFT))


def escape_down() -> bool:
    return x11().escape()


# ---------------------------------------------------------------- compositing
def composited() -> bool:
    """True if a compositing manager runs (translucent windows work)."""
    if os.environ.get("DRAGSHIFT_FORCE_NO_COMPOSITOR"):
        return False
    if is_wayland():
        return True
    x = x11()
    return x.composited() if x.ok else True


# ---------------------------------------------------------------- file manager
def reveal(path: str) -> bool:
    """Select the file in the file manager (Thunar, Nautilus, Dolphin… via FileManager1)."""
    uri = Path(path).resolve().as_uri()
    for cmd in (["dbus-send", "--session", "--print-reply", "--dest=org.freedesktop.FileManager1",
                 "/org/freedesktop/FileManager1", "org.freedesktop.FileManager1.ShowItems",
                 f"array:string:{uri}", "string:"],
                ["thunar", str(Path(path).parent)],
                ["xdg-open", str(Path(path).parent)]):
        if shutil.which(cmd[0]):
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=3)
                if r.returncode == 0:
                    return True
            except Exception:
                continue
    return False


# ---------------------------------------------------------------- start at login
AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "dragshift.desktop"


def _exec() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --quiet'
    src = Path(__file__).resolve().parent.parent   # running from a checkout: make -m find it
    return f'env PYTHONPATH="{src}" "{sys.executable}" -m dragshift --quiet'


def autostart_enabled() -> bool:
    return AUTOSTART.exists()


def set_autostart(on: bool):
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text("[Desktop Entry]\nType=Application\nName=DragShift\n"
                             f"Exec={_exec()}\nIcon=dragshift\nX-GNOME-Autostart-enabled=true\n"
                             "Comment=Shift+drag file converter\n", encoding="utf-8")
    elif AUTOSTART.exists():
        AUTOSTART.unlink()
