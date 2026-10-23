"""DragShift: Shift+drag any file to convert or edit it from a radial liquid-glass menu."""
from __future__ import annotations

from .i18n import tr, tr_items

import json
import os
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QPainter,
                           QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

from . import __version__, convert, glass, imaging, report, tools, video
from .radial import Item, RadialMenu
from .settings import Settings, SettingsWindow, WelcomeWindow, TRIGGERS
from .toasts import ToastStack
from .trigger import DragShiftWatcher
from .util import UserError, category, file_size, human_size, keep, release

APP_NAME = "DragShift"
TIP_URL = "https://cash.app/$AtikaKadesha"
VERSION = __version__
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
from .handoff import ipc_name
IPC_NAME = ipc_name()

GLYPHS = {"Compress": "⇲", "Metadata": "ⓘ", "Redact": "▮", "Crop": "⌗", "Edit": "☼",
          "Annotate": "✎", "Add BG": "▣", "Resize": "⤢", "Split": "✂", "Rotate": "⟳",
          "Merge": "⧉", "Protect": "⚿", "Extract": "⧈", "Distort": "⌁"}

HINTS = {
    "Compress": "Shrink file size", "Metadata": "View · remove GPS", "Redact": "Hide private info",
    "Crop": "Trim to a ratio", "Edit": "Light · colour · rotate", "Annotate": "Arrows · text · steps",
    "Add BG": "Gradient backdrop", "Resize": "Change dimensions", "Split": "One PDF per page",
    "Rotate": "Turn pages 90°", "Merge": "Join into one PDF", "Protect": "Add a password",
    "Extract": "Save embedded images", "Distort": "Content-aware distortion · GIF / video / frame",
}
FORMAT_HINTS = {"JPG": "Small, universal", "PNG": "Lossless · transparency", "WEBP": "Smallest for web",
                "HEIC": "iPhone format", "TIFF": "Print · archival", "AVIF": "Next-gen, tiny",
                "BMP": "Uncompressed", "PDF": "Document", "ICO": "Windows icon", "GIF": "Legacy web",
                "DOCX": "Editable Word", "TXT": "Plain text"}

IMAGE_TOOLS = ["Compress", "Resize", "Crop", "Edit", "Annotate", "Redact", "Add BG", "Distort", "Metadata"]
PDF_TOOLS = ["Compress", "Split", "Rotate", "Protect", "Extract", "Metadata"]
VIDEO_TOOLS = ["Trim", "Compress", "Resize", "Edit", "Mute", "Frame", "Distort", "Metadata", "Datamosh", "Audio"]
VIDEO_GLYPHS = {"Trim": "✂", "Mute": "♪", "Frame": "▣", "Datamosh": "≈", "Audio": "♫"}
VIDEO_HINTS = {"Trim": "Choose in · out", "Compress": "Smaller MP4", "Resize": "Change dimensions",
               "Edit": "Rotate · crop · speed · colour", "Mute": "Remove audio", "Frame": "Save a PNG frame",
               "Metadata": "View · remove tags", "Datamosh": "Melt video A into B", "Audio": "Extract sound · any format",
               "Distort": HINTS['Distort']}
AUDIO_TOOLS = ["Trim", "Edit", "Normalize", "Fade", "Compress", "Reverse", "Datamosh", "Transition", "Metadata"]
AUDIO_HINTS = {"Trim": "Choose in · out", "Edit": "Volume · speed · channels", "Normalize": "Consistent loudness",
               "Fade": "Smooth start · end", "Compress": "Smaller audio file", "Reverse": "Play backwards",
               "Metadata": "View · remove tags", "Transition": "Blend an ordered audio series", "Datamosh":"Glitch the sound · intensity over time"}
FORMAT_HINTS.update({"MP4": "H.264 · universal", "MKV": "Matroska · H.264", "MOV": "QuickTime · H.264",
                     "WEBM": "VP9 · web video", "AVI": "MPEG-4 · legacy", "MP3": "Audio only", "WAV": "Uncompressed audio"})
FORMAT_HINTS.update({"FLAC": "Lossless audio", "M4A": "AAC · phones", "AAC": "Compact audio",
                     "OGG": "Vorbis audio", "OPUS": "Small · speech & music", "AIFF": "Uncompressed audio",
                     "ZIP": "Universal archive", "7Z": "High compression", "TAR": "Archive container"})


# ---------------------------------------------------------------- background jobs
class _Sig(QObject):
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    def __init__(self, action: str, srcs: list, fn, extra: str = ""):
        super().__init__()
        self.action, self.srcs, self.fn, self.extra = action, srcs, fn, extra
        self.sig = _Sig()

    def run(self):
        try:
            outs = self.fn() or []
            self.sig.done.emit(report.summarize(self.action, self.srcs, outs, self.extra))
        except UserError as e:
            self.sig.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.sig.failed.emit(friendly_error(e))


def friendly_error(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    low = msg.lower()
    if isinstance(e, PermissionError) or "permission denied" in low:
        return "No permission to write next to this file. Try a folder you own, or set Settings → Save results → subfolder."
    if "cannot identify image" in low:
        return "This file doesn't look like an image DragShift can read."
    if isinstance(e, FileNotFoundError):
        return "The file was moved or deleted."
    return msg


# ---------------------------------------------------------------- icon
def make_tray_icon() -> QIcon:
    """macOS menu bar wants a monochrome 'template' image; Windows gets the colour icon."""
    if not IS_MAC:
        return make_icon()
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor(0, 0, 0), 4.5))
    p.drawEllipse(QPointF(22, 22), 14, 14)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0))
    p.drawEllipse(QPointF(22, 22), 5, 5)
    for a in range(0, 360, 45):  # petal gaps
        import math
        x, y = 22 + 14 * math.cos(math.radians(a + 22.5)), 22 + 14 * math.sin(math.radians(a + 22.5))
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.end()
    pm.setDevicePixelRatio(2.0)
    icon = QIcon(pm)
    icon.setIsMask(True)
    return icon


def make_icon() -> QIcon:
    ico = Path(__file__).with_name("assets") / "dragshift.ico"
    if ico.exists():
        return QIcon(str(ico))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(glass.iridescent_pen(QPointF(32, 32), 7, 255))
    p.drawEllipse(QPointF(32, 32), 24, 24)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- the app
class DragShiftApp(QObject):
    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.settings = Settings()
        self.settings.apply()
        self.pool = QThreadPool.globalInstance()
        self.windows: list[QWidget] = []
        qapp.aboutToQuit.connect(self._cancel_processing)
        self.toasts = ToastStack()
        self.menu = RadialMenu(self.build_rings, self.describe)
        self.menu.activated.connect(self.run_item)

        # the watcher only manages menus it opened (drag mode); click-mode menus (tray, right-click,
        # Send To) must not be closed just because no mouse button is held
        self.watcher = DragShiftWatcher(lambda: self.menu.isVisible() and not self.menu.click_mode,
                                        lambda: self.menu.got_drag, self)
        self.watcher.set_key(self.settings.get("trigger"))
        self.watcher.show_menu.connect(lambda pos: self.menu.popup(pos))
        self.watcher.toggle_ring.connect(self.menu.next_ring)
        self.watcher.hide_menu.connect(self.menu.close_menu)
        from .selection import SelectionShortcut
        self.selection_shortcut=SelectionShortcut(self)
        self.watcher.selection_menu.connect(lambda pos:self.selection_shortcut.request()
                                             if not self.menu.isVisible() else None)
        self.selection_shortcut.ready.connect(lambda paths:self.show_click_menu(paths)
                                              if not self.menu.isVisible() else None)
        self.watcher.start()

        # files handed over by "Send to", the right-click menu, or a second launch
        self._pending: list[str] = []
        self._pending_timer = QTimer(self, singleShot=True, interval=280, timeout=self._flush_pending)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(IPC_NAME)
        self.server.listen(IPC_NAME)
        self.server.newConnection.connect(self._ipc)

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(tr(f"{APP_NAME} — press {self.trigger_label()} while dragging a file"))
        m = QMenu()
        m.setStyleSheet(glass.QSS)
        self.act_enabled = QAction(tr("Enabled"), m, checkable=True, checked=True)
        self.act_enabled.toggled.connect(lambda on: setattr(self.watcher, "enabled", on))
        m.addAction(tr(self.act_enabled))
        m.addSeparator()
        m.addAction(tr("Settings…"), self.open_settings)
        m.addAction(tr("How to use"), self.show_welcome)
        m.addAction(tr("Tip the developer ♥"), lambda: QDesktopServices.openUrl(QUrl(TIP_URL)))
        m.addSeparator()
        m.addAction(tr(f"Quit {APP_NAME}"), self.qapp.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()

    def trigger_label(self) -> str:
        return dict(TRIGGERS).get(self.settings.get("trigger"), "Shift")

    # ------------------------------------------------------------ IPC
    def _ipc(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return
        buf = bytearray()
        done = {"v": False}

        def consume():
            if done["v"]:
                return
            try:
                buf.extend(bytes(sock.readAll()))
            except RuntimeError:
                pass
            try:
                data = json.loads(buf.decode("utf-8"))
            except Exception:
                return  # incomplete: wait for more
            done["v"] = True
            files = [f for f in data.get("files", []) if os.path.exists(f)]
            print(f"IPC: {len(files)} file(s) handed over", flush=True)
            if files:
                self.queue_files(files)
            elif data.get("show"):
                self.show_ready()
            sock.disconnectFromServer()
            sock.deleteLater()

        # Windows pipes: the sender may already be gone, so read on every signal + a fallback timer
        sock.readyRead.connect(consume)
        sock.disconnected.connect(consume)
        for ms in (0, 50, 200, 600):
            QTimer.singleShot(ms, consume)

    def queue_files(self, files: list[str]):
        """Explorer's right-click menu launches once per file: gather them, then show one menu."""
        for f in files:
            if f not in self._pending:
                self._pending.append(f)
        self._pending_timer.start()

    def _flush_pending(self):
        files, self._pending = self._pending, []
        if files:
            self.show_click_menu(files)

    # ------------------------------------------------------------ menu contents
    def describe(self, paths: list[str]) -> str:
        if len(paths) > 1:
            return f"{len(paths)} items"
        if Path(paths[0]).is_dir():
            return "Folder"
        return human_size(file_size(paths[0]))

    def build_rings(self, paths: list[str]) -> dict[str, list[Item]]:
        pack = Item("Pack", "▣", key="tool:Pack", hint="Create one ZIP or 7Z archive")
        cat = None if Path(paths[0]).is_dir() else category(paths[0])
        if cat is None:
            return {"Tools": [pack]}
        ext = Path(paths[0]).suffix
        same = [p for p in paths if category(p) == cat]
        convert_items = []
        for t in convert.targets_for(cat, ext):
            hint = FORMAT_HINTS.get(t, "")
            if cat == "image" and t == "PDF" and len(same) > 1:
                hint = f"{len(same)} images → 1 PDF"
            convert_items.append(Item(t, key=f"convert:{t}", hint=hint))
        rings: dict[str, list[Item]] = {"Convert": convert_items}
        if cat == 'video' or cat == 'image' and ext.lower() == '.gif':
            convert_items.append(Item('TG Emoji', key='tool:Telegram emoji', hint='100×100 · VP9 · up to 3 s · ≤256 KB'))
        if cat == "image":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in IMAGE_TOOLS]
        elif cat == "pdf":
            names = PDF_TOOLS[:3] + (["Merge"] if len(same) > 1 else []) + PDF_TOOLS[3:]
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in names]
        elif cat == "video":
            for item in convert_items:
                if item.label == "GIF":
                    item.hint = "Animated · 12 fps"
            rings["Tools"] = [Item(n, VIDEO_GLYPHS.get(n, GLYPHS.get(n, "")), key=f"tool:{n}",
                                   hint=VIDEO_HINTS[n]) for n in VIDEO_TOOLS]
        elif cat == "audio":
            glyphs = {"Normalize": "≋", "Fade": "◢", "Reverse": "↶", "Transition": "≈"}
            rings["Tools"] = [Item(n, glyphs.get(n, VIDEO_GLYPHS.get(n, GLYPHS.get(n, ""))),
                                    key=f"tool:{n}", hint=AUDIO_HINTS[n]) for n in AUDIO_TOOLS]
        elif cat == "archive":
            rings["Tools"] = [Item("Extract", "⇩", key="tool:Extract archive", hint="Unpack to a new folder"),
                               Item("Contents", "ⓘ", key="tool:Contents", hint="View files inside")]
        rings.setdefault("Tools", []).append(pack)
        return rings

    # ------------------------------------------------------------ actions
    def run_item(self, item: Item, paths: list[str]):
        if item.key == "tool:Pack":
            from .archive_tools import ArchivePackWindow
            self.open_window(ArchivePackWindow(paths, self.notify))
            return
        cat = category(paths[0])
        paths = [p for p in paths if category(p) == cat]
        kind, name = item.key.split(":", 1)
        if name == 'Distort':
            from .distortion_tools import DistortionWindow
            self.open_window(DistortionWindow(paths,self.notify))
            return
        if name == 'Telegram emoji':
            from .video_tools import TelegramEmojiWindow
            self.open_window(TelegramEmojiWindow(paths, self.notify))
            return
        if kind == "convert":
            if cat in ("video", "audio", "archive"):
                self.submit(f"convert:{name}", paths,
                            lambda: video.batch(paths, lambda p: convert.convert(p, cat, name)), f"Converting to {name}…")
            elif cat == "image" and name == "PDF" and len(paths) > 1:
                self.submit(f"convert:{name}", paths, lambda: convert.images_to_single_pdf(paths),
                            f"Combining {len(paths)} images…")
            else:
                verb = f"Converting to {name}…"
                self.submit(f"convert:{name}", paths,
                            lambda: [o for p in paths for o in convert.convert(p, cat, name)], verb)
            return
        if cat == "video":
            if name=='Resize':
                from .video_geometry import VideoResizeWindow
                self.open_window(VideoResizeWindow(paths,self.notify))
                return
            if name == "Audio":
                from .audio_tools import AudioToolWindow
                self.open_window(AudioToolWindow(paths, self.notify, "Extract"))
                return
            from .video_tools import DatamoshWindow, VideoToolWindow
            self.open_window(DatamoshWindow(paths, self.notify) if name == "Datamosh"
                             else VideoToolWindow(paths, self.notify, name))
            return
        if cat == "audio":
            if name=='Datamosh':
                from .video_tools import DatamoshWindow
                self.open_window(DatamoshWindow(paths,self.notify))
                return
            from .audio_tools import AudioToolWindow, AudioTransitionWindow
            self.open_window(AudioTransitionWindow(paths, self.notify) if name == 'Transition'
                             else AudioToolWindow(paths, self.notify, name))
            return
        if cat == "archive":
            from . import archives
            if name == "Extract archive":
                self.submit("tool:Extract archive", paths, lambda: video.batch(paths, archives.extract), "Extracting archives…")
            elif name == "Contents":
                from .archive_tools import ArchiveContentsWindow
                self.open_window(ArchiveContentsWindow(paths[0], self.notify))
            return
        if cat == "pdf":
            if name == "Merge":
                self.submit("tool:Merge", paths, lambda: [imaging.merge_pdfs(paths)], "Merging…")
            elif name == "Metadata":
                self.open_window(tools.MetadataWindow(paths[0], self.notify, is_pdf=True))
            elif name == "Protect":
                self.open_window(tools.ProtectWindow(paths[0], self.notify))
            else:
                fns = {"Compress": lambda p: [imaging.compress_pdf(p)], "Split": imaging.split_pdf,
                       "Rotate": lambda p: [imaging.rotate_pdf(p)], "Extract": imaging.extract_pdf_images}
                f = fns[name]
                self.submit(f"tool:{name}", paths, lambda: [o for p in paths for o in f(p)],
                            {"Compress": "Compressing…", "Split": "Splitting…", "Rotate": "Rotating…",
                             "Extract": "Extracting images…"}[name])
            return
        windows = {"Crop": tools.CropWindow, "Add BG": tools.AddBgWindow, "Annotate": tools.AnnotateWindow,
                   "Redact": tools.RedactWindow, "Edit": tools.EditWindow, "Metadata": tools.MetadataWindow}
        try:
            if name == "Compress":
                self.open_window(tools.CompressWindow(paths, self.notify))
            elif name == "Resize":
                self.open_window(tools.ResizeWindow(paths, self.notify))
            else:
                self.open_window(windows[name](paths[0], self.notify))
        except UserError as e:
            self.toasts.error(f"Couldn't open {name}", str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.toasts.error(f"Couldn't open {name}", friendly_error(e))

    def open_window(self, w: QWidget):
        if hasattr(w, "submit"):
            w.submit = self.submit
        if isinstance(w, tools.ToolWindow):
            w.replacement_handler = self.open_window
        self.windows.append(w)
        w.destroyed.connect(lambda *_: self.windows.remove(w) if w in self.windows else None)
        if hasattr(w, "present"):
            w.present()
        else:
            w.show()

    def _cancel_processing(self):
        for window in self.windows:
            if getattr(window, "busy", False) and hasattr(window, "job"):
                window.job.control.cancel.set()

    def submit(self, action: str, srcs: list, fn, working: str = "Working…", extra: str = ""):
        """Run in the background. A spinner card appears only if it takes > 150 ms."""
        if action in ("tool:Pack", "tool:Telegram emoji", "tool:Distort") or any(category(p) in ("video", "audio", "archive")
                or str(p).lower().endswith('.gif') for p in srcs):
            from .video_tools import VideoProgressWindow
            window = VideoProgressWindow(action, srcs, fn, lambda r: self.toasts.new().done(r), extra)
            self.open_window(window)
            window.start(self.pool)
            return
        state = {"card": None, "finished": False}
        names = Path(srcs[0]).name if len(srcs) == 1 else f"{len(srcs)} files"

        def show_spinner():
            if not state["finished"]:
                state["card"] = self.toasts.new()
                state["card"].working(working, names)

        QTimer.singleShot(150, show_spinner)
        job = Job(action, srcs, fn, extra)

        def done(r):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.done(r)

        def failed(msg):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.error("Couldn't finish", msg)

        job.sig.done.connect(done)
        job.sig.failed.connect(failed)
        self.pool.start(keep(job))

    def notify(self, action: str, srcs: list, outs: list, extra: str = ""):
        """Called by tool windows after they save."""
        self.toasts.new().done(report.summarize(action, srcs, outs, extra))

    # ------------------------------------------------------------ misc
    def _tray_click(self, reason):
        if not IS_MAC and reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.tray_menu.popup(QCursor.pos())

    def show_ready(self):
        self.toasts.info("DragShift is running", f"Drag a file, then press {self.trigger_label()}.")

    def startup(self, files, *, quiet=False):
        """Stay in the tray until a file is dragged or explicitly handed over."""
        if files:
            QTimer.singleShot(120, lambda: self.queue_files(files))
        elif not quiet:
            QTimer.singleShot(400, self.show_ready)

    def show_click_menu(self, files: list[str]):
        g = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        pos = QCursor.pos() if g.contains(QCursor.pos()) else g.center()
        self.menu.popup(pos, files)
        self.menu.activateWindow()

    def open_settings(self):
        w = SettingsWindow(self.settings)
        w.changed.connect(self._settings_changed)
        self.open_window(w)

    def _settings_changed(self):
        self.watcher.set_key(self.settings.get("trigger"))
        self.tray.setToolTip(tr(f"{APP_NAME} — press {self.trigger_label()} while dragging a file"))

    def show_welcome(self):
        self.open_window(WelcomeWindow(self.settings, self.trigger_label()))


class DragShiftQApplication(QApplication):
    """macOS delivers files from Finder's "Open With", the Dock and `open -a` as FileOpen
    events (not argv)."""
    file_opened = Signal(str)

    def __init__(self, argv):
        super().__init__(argv)
        self.early_files: list[str] = []

    def event(self, e):
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.FileOpen:
            f = e.file()
            if f:
                self.early_files.append(f)
                self.file_opened.emit(f)
            return True
        return super().event(e)


def mac_setup(qapp):
    # fonts: util.ui_family picks a real installed family. (Substituting the private
    # ".AppleSystemUIFont" made labels draw nothing on macOS 26.)
    try:
        from . import platform_mac
        platform_mac.become_accessory()
    except Exception:
        pass


def send_to_running(payload: dict) -> bool:
    s = QLocalSocket()
    s.connectToServer(IPC_NAME)
    if not s.waitForConnected(300):
        return False
    s.write(json.dumps(payload).encode("utf-8"))
    s.flush()
    s.waitForBytesWritten(500)
    s.disconnectFromServer()
    return True


def setup_logging():
    """Errors go to %LOCALAPPDATA%\\DragShift\\log.txt (pythonw / the .exe have no console)."""
    import faulthandler
    if IS_MAC:
        base = Path.home() / "Library" / "Logs" / "DragShift"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DragShift"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "log.txt"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(base / "log.old.txt")
    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n==== DragShift {VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    if sys.stderr is None or getattr(sys, "frozen", False) or sys.executable.lower().endswith("pythonw.exe"):
        sys.stdout = sys.stderr = f
    faulthandler.enable(f)

    def hook(t, v, tb):
        f.write("Unhandled: " + "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        setup_logging()
    except Exception:
        pass
    if IS_WIN:
        try:  # crisp taskbar/tray icon grouping
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DragShift.App")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = DragShiftQApplication(argv)
    if IS_MAC:
        mac_setup(qapp)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(VERSION)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(make_icon())
    from .i18n import install as install_language
    install_language(qapp,Settings().get('language'))

    if "--files-selftest" in argv:
        from .file_check import selftest as file_selftest
        return file_selftest()
    if "--video-selftest" in argv:
        from .video_check import selftest as video_selftest
        return video_selftest()
    if "--selftest" in argv:
        return selftest()
    files = [os.path.abspath(a) for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    # already running? hand over and exit (instant: no second UI start-up)
    if send_to_running({"files": files} if files else {"show": "--quiet" not in argv}):
        return 0
    app = DragShiftApp(qapp)
    qapp.file_opened.connect(lambda f: app.queue_files([f]))
    if qapp.early_files:
        app.queue_files(list(qapp.early_files))
    app.startup(files, quiet="--quiet" in argv)
    return qapp.exec()


def check_text_renders():
    """Labels must really draw: paint text with the UI fonts and count the inked pixels."""
    from PySide6.QtGui import QImage, QPainter, QColor
    from .util import ui_font, ui_family
    for display in (True, False):
        img = QImage(200, 40, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setFont(ui_font(16, True, display))
        p.setPen(QColor(255, 255, 255))
        p.drawText(img.rect(), Qt.AlignCenter, "WEBP 43.8 KB")
        p.end()
        inked = sum(1 for y in range(0, 40, 2) for x in range(0, 200, 2) if img.pixelColor(x, y).alpha() > 60)
        print(f"font {ui_family(display)!r}: {inked} inked samples")
        if inked < 30:
            raise RuntimeError(f"text does not render with font {ui_family(display)!r}")


def selftest() -> int:
    """Used by CI on the frozen .exe: exercise every library and build the UI, then exit."""
    import tempfile
    from PIL import Image
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.png"
            Image.new("RGBA", (64, 48), (255, 100, 80, 255)).save(src)
            for t in convert.targets_for("image", ".png"):
                convert.convert(src, "image", t)
            pdf = convert.convert(src, "image", "PDF")[0]
            for t in convert.PDF_TARGETS:
                convert.convert(pdf, "pdf", t)
            (Path(d) / "n.txt").write_text("hello")
            docx = convert.convert(Path(d) / "n.txt", "text", "DOCX")[0]
            convert.convert(docx, "docx", "TXT")
            imaging.add_background(convert.open_image(src), imaging.Background())
            imaging.compress_pdf(pdf)
            imaging.protect_pdf(pdf, "pw")
            from . import liquid
            liquid.render(Image.new("RGB", (80, 80)), liquid.sdf_circle(80, 80, 40, 40, 30))
            menu = RadialMenu(lambda p: {"Convert": [Item("PNG")]}, lambda p: "1 KB")
            menu._load([str(src)])
            n = lambda *a, **k: None
            for w in (tools.CropWindow(str(src), n), tools.AddBgWindow(str(src), n), tools.EditWindow(str(src), n),
                      tools.AnnotateWindow(str(src), n), tools.RedactWindow(str(src), n),
                      tools.MetadataWindow(str(src), n), tools.CompressWindow([str(src)], n),
                      tools.ResizeWindow([str(src)], n), tools.ProtectWindow(str(pdf), n)):
                w.close()
            ToastStack().info("ok", "ok").dismiss()
            check_text_renders()
        print("SELFTEST OK")
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
