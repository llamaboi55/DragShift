"""Linux integration for Wayland sessions (niri, Sway, Hyprland, KDE…).

Wayland gives no app the global pointer/keyboard state, so the pieces X11 gets for free are
rebuilt from lower layers:

* Trigger: button + modifier state read straight from the kernel input devices (evdev).
  Read-only, never grabbed, only booleans are kept. Needs the user in the `input` group.
* Placement: Wayland apps can't position their windows or ask where the cursor is. The menu,
  toasts and drop "catchers" are wlr-layer-shell surfaces (via LayerShellQt), which can be
  anchored with margins; the cursor position comes from the drag itself entering a
  transparent full-screen catcher.
* Screen capture for the glass: `grim` (wlr-screencopy), since QScreen.grabWindow is blank.
* Focused output: niri's IPC socket when available.
"""
from __future__ import annotations

import ctypes
import glob
import json
import os
import selectors
import shutil
import socket
import subprocess
import threading
import time

try:
    import evdev
    from evdev import ecodes
except Exception:  # pragma: no cover - optional dependency
    evdev = None
    ecodes = None


def is_wayland() -> bool:
    """True when Qt itself talks Wayland (not XWayland via QT_QPA_PLATFORM=xcb)."""
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.instance() is not None:
        return QGuiApplication.platformName().startswith("wayland")
    return os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY"))


# ---------------------------------------------------------------- input state (evdev)
class _Input:
    RESCAN_S = 2.0

    def __init__(self):
        self.ok = False
        self.problem = ""
        self.pressed: dict[str, set[int]] = {}   # device path -> held key/button codes
        self.motion = [0, 0]                     # accumulated pointer travel (device units)
        self._abs_last: dict[tuple[str, int], int] = {}
        self._devs: dict[str, object] = {}
        self._sel = selectors.DefaultSelector()
        self._lock = threading.Lock()
        if evdev is None:
            self.problem = "python-evdev is not installed."
            return
        self._wanted = {ecodes.BTN_LEFT, ecodes.KEY_LEFTSHIFT, ecodes.KEY_ESC}
        self._scan()
        if not self._devs:
            paths = glob.glob("/dev/input/event*")
            if paths and not any(os.access(p, os.R_OK) for p in paths):
                self.problem = ("Can't read input devices. Add yourself to the 'input' group "
                                "(sudo usermod -aG input $USER) and log in again.")
            else:
                self.problem = "No mouse or keyboard found under /dev/input."
            return
        self.ok = True
        threading.Thread(target=self._run, name="dragshift-evdev", daemon=True).start()

    def _scan(self):
        for path in evdev.list_devices():
            if path in self._devs:
                continue
            try:
                dev = evdev.InputDevice(path)
                keys = set(dev.capabilities().get(ecodes.EV_KEY, []))
                if not keys & self._wanted:
                    dev.close()
                    continue
                held = set(dev.active_keys())
            except OSError:
                continue
            with self._lock:
                self._devs[path] = dev
                self.pressed[path] = held
            self._sel.register(dev.fileno(), selectors.EVENT_READ, path)

    def _drop(self, path: str):
        dev = self._devs.pop(path, None)
        with self._lock:
            self.pressed.pop(path, None)
        if dev is not None:
            try:
                self._sel.unregister(dev.fileno())
                dev.close()
            except Exception:
                pass

    def _run(self):
        next_scan = time.monotonic() + self.RESCAN_S
        while True:
            for key, _ in self._sel.select(timeout=self.RESCAN_S):
                path = key.data
                dev = self._devs.get(path)
                if dev is None:
                    continue
                try:
                    for ev in dev.read():
                        self._event(path, ev)
                except BlockingIOError:
                    pass
                except OSError:      # unplugged
                    self._drop(path)
            if time.monotonic() >= next_scan:
                next_scan = time.monotonic() + self.RESCAN_S
                self._scan()

    def _event(self, path: str, ev):
        if ev.type == ecodes.EV_KEY:
            held = self.pressed.get(path)
            if held is None:
                return
            if ev.value:          # 1 = press, 2 = autorepeat
                held.add(ev.code)
            else:
                held.discard(ev.code)
                if ev.code == ecodes.BTN_TOUCH:   # finger lifted: next touch starts fresh
                    self._abs_last = {k: v for k, v in self._abs_last.items() if k[0] != path}
        elif ev.type == ecodes.EV_REL:
            if ev.code == ecodes.REL_X:
                self.motion[0] += abs(ev.value)
            elif ev.code == ecodes.REL_Y:
                self.motion[1] += abs(ev.value)
        elif ev.type == ecodes.EV_ABS and ev.code in (ecodes.ABS_X, ecodes.ABS_Y):
            # touchpads / tablets: count travel between samples
            k = (path, ev.code)
            last = self._abs_last.get(k)
            self._abs_last[k] = ev.value
            if last is not None:
                self.motion[0 if ev.code == ecodes.ABS_X else 1] += abs(ev.value - last)

    def any_down(self, *codes: int) -> bool:
        with self._lock:
            return any(c in held for held in self.pressed.values() for c in codes)


_INPUT: _Input | None = None


def _input() -> _Input:
    global _INPUT
    if _INPUT is None:
        _INPUT = _Input()
    return _INPUT


def available() -> bool:
    return _input().ok


def problem() -> str:
    return _input().problem


if ecodes is not None:
    KEY_CODES = {"shift": (ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT),
                 "ctrl": (ecodes.KEY_LEFTCTRL, ecodes.KEY_RIGHTCTRL),
                 "alt": (ecodes.KEY_LEFTALT, ecodes.KEY_RIGHTALT)}
else:
    KEY_CODES = {}


def left_button_down() -> bool:
    return _input().any_down(ecodes.BTN_LEFT)


def key_down(name: str) -> bool:
    return _input().any_down(*KEY_CODES.get(name, KEY_CODES["shift"]))


def escape_down() -> bool:
    return _input().any_down(ecodes.KEY_ESC)


def pointer():
    """Accumulated pointer travel. Not a screen position (Wayland has none to give), but
    differences between two reads say how far the mouse moved, which is all the trigger needs."""
    from PySide6.QtCore import QPoint
    m = _input().motion
    return QPoint(m[0], m[1])


# ---------------------------------------------------------------- focused output
def _niri(request) -> dict | None:
    path = os.environ.get("NIRI_SOCKET")
    if not path:
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            s.connect(path)
            s.sendall((json.dumps(request) + "\n").encode())
            buf = b""
            while not buf.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
        return json.loads(buf).get("Ok")
    except Exception:
        return None


def focused_screen():
    """The QScreen the compositor considers active (niri), else the primary screen."""
    from PySide6.QtGui import QGuiApplication
    r = _niri("FocusedOutput")
    name = ((r or {}).get("FocusedOutput") or {}).get("name")
    if name:
        for s in QGuiApplication.screens():
            if s.name() == name:
                return s
    return QGuiApplication.primaryScreen()


# ---------------------------------------------------------------- screen capture
def capture(x: int, y: int, w: int, h: int):
    """Pixels under a rect in global logical coordinates, as a PIL image of size w×h."""
    if not shutil.which("grim") or w <= 0 or h <= 0:
        return None
    try:
        r = subprocess.run(["grim", "-s", "1", "-t", "ppm", "-g", f"{x},{y} {w}x{h}", "-"],
                           capture_output=True, timeout=2)
        if r.returncode != 0 or not r.stdout:
            return None
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(r.stdout)).convert("RGB")
        return im if im.size == (w, h) else im.resize((w, h))
    except Exception:
        return None


# ---------------------------------------------------------------- layer shell (LayerShellQt)
ANCHOR_TOP, ANCHOR_BOTTOM, ANCHOR_LEFT, ANCHOR_RIGHT = 1, 2, 4, 8
ANCHOR_ALL = 15
LAYER_BACKGROUND, LAYER_BOTTOM, LAYER_TOP, LAYER_OVERLAY = 0, 1, 2, 3
KEYBOARD_NONE, KEYBOARD_EXCLUSIVE, KEYBOARD_ON_DEMAND = 0, 1, 2


class _QMargins(ctypes.Structure):
    _fields_ = [("left", ctypes.c_int), ("top", ctypes.c_int),
                ("right", ctypes.c_int), ("bottom", ctypes.c_int)]


class _LayerShellQt:
    """LayerShellQt has no Python bindings; its LayerShellQt::Window methods are plain
    non-virtual C++ members, so they're called directly with `this` as the first argument."""
    SYMS = {
        "get": ("_ZN12LayerShellQt6Window3getEP7QWindow", [ctypes.c_void_p], ctypes.c_void_p),
        "anchors": ("_ZN12LayerShellQt6Window10setAnchorsE6QFlagsINS0_6AnchorEE",
                    [ctypes.c_void_p, ctypes.c_int], None),
        "zone": ("_ZN12LayerShellQt6Window16setExclusiveZoneEi", [ctypes.c_void_p, ctypes.c_int], None),
        "margins": ("_ZN12LayerShellQt6Window10setMarginsERK8QMargins",
                    [ctypes.c_void_p, ctypes.POINTER(_QMargins)], None),
        "keyboard": ("_ZN12LayerShellQt6Window24setKeyboardInteractivityENS0_21KeyboardInteractivityE",
                     [ctypes.c_void_p, ctypes.c_int], None),
        "layer": ("_ZN12LayerShellQt6Window8setLayerENS0_5LayerE", [ctypes.c_void_p, ctypes.c_int], None),
    }

    def __init__(self):
        self.ok = False
        self.f = {}
        self.problem = ""
        if os.environ.get("QT_QPA_PLATFORM", "").startswith(("offscreen", "xcb")):
            return
        if "GNOME" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            self.problem = "GNOME doesn't support layer-shell surfaces, which the menu needs."
            return
        if _bundled_qt():
            # the distro's LayerShellQt links the distro's Qt; loading it next to a second
            # (pip wheel / PyInstaller) Qt in the same process ends in a crash
            self.problem = ("This build ships its own Qt. Run from source with your distro's PySide6 "
                            "and layer-shell-qt packages.")
            return
        try:
            lib = ctypes.CDLL("libLayerShellQtInterface.so.6")
            for k, (sym, args, res) in self.SYMS.items():
                fn = getattr(lib, sym)
                fn.argtypes, fn.restype = args, res
                self.f[k] = fn
            self.ok = True
        except (OSError, AttributeError):
            self.problem = "Install layer-shell-qt so the menu can open under the cursor."


def _bundled_qt() -> bool:
    """True if PySide6 runs on its own copy of Qt (pip wheel, frozen app) rather than the system's."""
    import sys
    if getattr(sys, "frozen", False):
        return True
    try:
        import PySide6
        return os.path.isdir(os.path.join(os.path.dirname(PySide6.__file__), "Qt", "lib"))
    except Exception:
        return True


_LSQ: _LayerShellQt | None = None


def _lsq() -> _LayerShellQt:
    global _LSQ
    if _LSQ is None:
        _LSQ = _LayerShellQt()
    return _LSQ


def layer_shell_ok() -> bool:
    return is_wayland() and _lsq().ok


def ready() -> bool:
    """Everything the Shift+drag trigger needs on Wayland."""
    return available() and layer_shell_ok()


def setup_problem() -> str:
    return problem() if not available() else _lsq().problem


class Layer:
    """Turns a not-yet-shown top-level QWidget into a layer surface on `screen`."""

    def __init__(self, widget, layer=LAYER_OVERLAY, anchors=ANCHOR_TOP | ANCHOR_LEFT,
                 keyboard=KEYBOARD_NONE):
        import shiboken6
        widget.winId()                                    # create the QWindow
        self.widget = widget
        self.win = widget.windowHandle()
        self.ptr = _LSQ.f["get"](ctypes.c_void_p(shiboken6.getCppPointer(self.win)[0]))
        _LSQ.f["layer"](self.ptr, layer)
        _LSQ.f["anchors"](self.ptr, anchors)
        _LSQ.f["zone"](self.ptr, -1)                      # ignore panels: place on the full output
        _LSQ.f["keyboard"](self.ptr, keyboard)

    def keyboard(self, mode: int):
        _LSQ.f["keyboard"](self.ptr, mode)

    def place(self, screen, x: int = 0, y: int = 0):
        """Put the surface's top-left at global logical (x, y) on `screen` (anchor top-left),
        or just pick the screen (full-screen anchors)."""
        if self.win.screen() is not screen:
            visible = self.widget.isVisible()
            if visible:
                self.widget.hide()                        # the output is fixed per surface
            self.win.setScreen(screen)
            if visible:
                self.widget.show()
        g = screen.geometry()
        m = _QMargins(max(0, x - g.x()), max(0, y - g.y()), 0, 0)
        _LSQ.f["margins"](self.ptr, ctypes.byref(m))


def make_layer(widget, **kw) -> Layer | None:
    if not layer_shell_ok():
        return None
    try:
        return Layer(widget, **kw)
    except Exception:
        import traceback
        traceback.print_exc()
        return None
