"""Read Explorer's explicit selection for the no-drag Shift-tap shortcut.

This never synthesizes clicks, changes selection or substitutes drag payloads.
Other platforms continue using their native drag hand-off.
"""
import sys
from pathlib import Path
from PySide6.QtCore import QObject,QRunnable,QThreadPool,Signal,Slot
from .util import keep,release


def selected_paths(view):
    return [str(Path(item.Path)) for item in view.Document.SelectedItems()
            if item.IsFileSystem and Path(item.Path).exists()]


def foreground_explorer():
    if sys.platform!='win32':
        return 0
    import win32gui
    hwnd=win32gui.GetForegroundWindow()
    return hwnd if win32gui.GetClassName(hwnd) in ('CabinetWClass','ExploreWClass') else 0


def read_selection(hwnd):
    import pythoncom,win32com.client
    pythoncom.CoInitialize()
    try:
        shell=win32com.client.Dispatch('Shell.Application')
        matches=[]
        for window in shell.Windows():
            try:
                if int(window.HWND)==hwnd:
                    matches.append(selected_paths(window))
            except (AttributeError,pythoncom.com_error):
                continue
        # Some tabbed Explorer versions expose multiple views with one HWND.
        # Never guess which tab supplied the selection.
        return matches[0] if matches and all(paths==matches[0] for paths in matches) else []
    finally:
        pythoncom.CoUninitialize()


class _Signals(QObject):
    done=Signal(int,list)
    finished=Signal(object)


class _SelectionJob(QRunnable):
    def __init__(self,hwnd):
        super().__init__()
        self.hwnd=hwnd
        self.sig=_Signals()

    def run(self):
        try:
            paths=read_selection(self.hwnd)
        except Exception:
            paths=[]
        self.sig.done.emit(self.hwnd,paths)
        self.sig.finished.emit(self)


class SelectionShortcut(QObject):
    ready=Signal(list)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.busy=False

    def request(self,*_):
        if self.busy:
            return
        hwnd=foreground_explorer()
        if not hwnd:
            return
        self.busy=True
        job=_SelectionJob(hwnd)
        job.sig.done.connect(self._done)
        job.sig.finished.connect(release)
        QThreadPool.globalInstance().start(keep(job))

    @Slot(int,list)
    def _done(self,hwnd,paths):
        self.busy=False
        if foreground_explorer()==hwnd and len(paths)>1:
            self.ready.emit(paths)
