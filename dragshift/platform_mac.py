"""macOS integration (only imported on darwin).

* Trigger: NSEvent.pressedMouseButtons / modifierFlags give the *global* mouse and
  modifier state, with no Accessibility or Input Monitoring permission needed.
* Capture: CGWindowListCreateImage can capture "everything below window N", which keeps
  Finder's drag picture and our own windows out of the glass (live refresh like Windows'
  capture exclusion). Seeing other apps' windows needs the Screen Recording permission;
  without it macOS returns just the wallpaper, which still makes a pleasant glass.
* Start at login: a LaunchAgent plist.
"""
from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

try:
    import AppKit  # pyobjc-framework-Cocoa
    import Quartz  # pyobjc-framework-Quartz
    OK = True
except Exception:  # pragma: no cover
    OK = False

# NSEventModifierFlags
SHIFT, CONTROL, OPTION, COMMAND = 1 << 17, 1 << 18, 1 << 19, 1 << 20
KEY_FLAGS = {"shift": SHIFT, "ctrl": CONTROL, "alt": OPTION}


# ---------------------------------------------------------------- input state
def left_button_down() -> bool:
    return bool(OK and AppKit.NSEvent.pressedMouseButtons() & 1)


def key_down(name: str) -> bool:
    return bool(OK and AppKit.NSEvent.modifierFlags() & KEY_FLAGS.get(name, SHIFT))


def escape_down() -> bool:
    if not OK:
        return False
    try:
        return bool(Quartz.CGEventSourceKeyState(Quartz.kCGEventSourceStateCombinedSessionState, 53))
    except Exception:
        return False


# ---------------------------------------------------------------- app behaviour
def become_accessory():
    """Menu-bar app: no Dock icon (the bundle also sets LSUIElement)."""
    if OK:
        AppKit.NSApplication.sharedApplication().setActivationPolicy_(
            AppKit.NSApplicationActivationPolicyAccessory)


def activate():
    """Bring our windows to the front (accessory apps aren't activated automatically)."""
    if OK:
        AppKit.NSApplication.sharedApplication().activateIgnoringOtherApps_(True)


def window_number(widget) -> int:
    """CGWindowID of a Qt top-level widget (winId() is an NSView*)."""
    if not OK:
        return 0
    from PySide6.QtGui import QGuiApplication
    if QGuiApplication.platformName() != 'cocoa':
        # Headless Qt IDs are small synthetic integers, not Objective-C pointers.
        return 0
    try:
        import objc
        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        return int(win.windowNumber()) if win is not None else 0
    except Exception:
        return 0


# ---------------------------------------------------------------- screen capture
LAST_SWEEP: list[str] = []


def drag_window_ids(cursor: tuple[int, int] | None = None) -> list[int]:
    """The drag picture under the cursor: windows at the dragging level, or any small
    window of another app sitting right on the cursor above normal windows."""
    LAST_SWEEP.clear()
    if not OK:
        return []
    try:
        level = Quartz.CGWindowLevelForKey(Quartz.kCGDraggingWindowLevelKey)
        infos = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID) or []
        me = os.getpid()
        ids = []
        for i in infos:
            layer = int(i.get("kCGWindowLayer", 0))
            b = i.get("kCGWindowBounds", {}) or {}
            x, y, w, h = (float(b.get(k, 0)) for k in ("X", "Y", "Width", "Height"))
            near = cursor is not None and x - 40 <= cursor[0] <= x + w + 40 and y - 40 <= cursor[1] <= y + h + 40
            if near:
                LAST_SWEEP.append(f"{i.get('kCGWindowOwnerName')} layer={layer} {int(w)}x{int(h)}")
            if int(i.get("kCGWindowOwnerPID", 0)) == me:
                continue
            if layer >= level or (near and layer > 0 and w < 700 and h < 700):
                ids.append(int(i["kCGWindowNumber"]))
        return ids
    except Exception as e:
        LAST_SWEEP.append(f"error {e}")
        return []


def capture(x: int, y: int, w: int, h: int, below_window: int = 0):
    """Capture a screen rect (global points, top-left origin) as a PIL RGB image.
    below_window: capture only what is beneath that window (excludes it and anything above)."""
    if not OK:
        return None
    from PIL import Image
    rect = Quartz.CGRectMake(x, y, w, h)
    if below_window:
        opt, wid = Quartz.kCGWindowListOptionOnScreenBelowWindow, below_window
    else:
        loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
        drags = drag_window_ids((int(loc.x), int(loc.y)))
        if drags:
            # everything below the lowest drag window = no drag picture in the glass
            opt, wid = Quartz.kCGWindowListOptionOnScreenBelowWindow, min(drags)
        else:
            opt, wid = Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID
    try:
        img = Quartz.CGWindowListCreateImage(rect, opt, wid, Quartz.kCGWindowImageBestResolution)
    except Exception:
        return None
    if img is None:
        return None
    width, height = Quartz.CGImageGetWidth(img), Quartz.CGImageGetHeight(img)
    bpr = Quartz.CGImageGetBytesPerRow(img)
    data = Quartz.CGDataProviderCopyData(Quartz.CGImageGetDataProvider(img))
    im = Image.frombuffer("RGBA", (width, height), bytes(data), "raw", "BGRA", bpr, 1)
    return im.convert("RGB").resize((w, h), Image.LANCZOS)


def has_screen_recording() -> bool:
    try:
        return bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        return True


def request_screen_recording() -> bool:
    try:
        return bool(Quartz.CGRequestScreenCaptureAccess())
    except Exception:
        return False


# ---------------------------------------------------------------- start at login
AGENT = Path.home() / "Library" / "LaunchAgents" / "com.dragshift.app.plist"


def _program() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--quiet"]
    return [sys.executable, "-m", "dragshift", "--quiet"]


def autostart_enabled() -> bool:
    return AGENT.exists()


def set_autostart(on: bool):
    if on:
        AGENT.parent.mkdir(parents=True, exist_ok=True)
        with open(AGENT, "wb") as f:
            plistlib.dump({"Label": "com.dragshift.app", "ProgramArguments": _program(),
                           "RunAtLoad": True, "ProcessType": "Interactive"}, f)
    elif AGENT.exists():
        AGENT.unlink()


# ---------------------------------------------------------------- drag pasteboard
def drag_files() -> list[str]:
    """Files being dragged right now, read from the system drag pasteboard.
    macOS only tells a window about a drag once the cursor moves over it; reading the pasteboard
    lets the menu show its slices the moment it opens, even if the mouse is held perfectly still."""
    try:
        import AppKit
        from AppKit import NSPasteboard, NSURL
        name = getattr(AppKit, "NSPasteboardNameDrag", None) or getattr(AppKit, "NSDragPboard", "Apple CFPasteboard drag")
        pb = NSPasteboard.pasteboardWithName_(name)
        urls = pb.readObjectsForClasses_options_([NSURL], {"NSPasteboardURLReadingFileURLsOnlyKey": True}) or []
        out = [str(u.path()) for u in urls if u.isFileURL()]
        return [p for p in out if os.path.exists(p)]
    except Exception:
        return []
