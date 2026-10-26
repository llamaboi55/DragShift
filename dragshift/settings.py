"""Persistent settings + the Settings and Welcome windows."""
from __future__ import annotations

from .i18n import tr, tr_items

import sys

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSlider, QVBoxLayout)

from . import __version__
from .util import CONFIG
from .theme import HOVER, TEXT
from .window import GlassWindow

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
APP = "DragShift"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULTS = {
    "language": "auto",
    "output": "next",
    "jpg_quality": 90,
    "pdf_dpi": 200,
    "trigger": "shift",
    "welcomed": False,
}


class Settings:
    def __init__(self):
        self.q = QSettings(APP, APP)

    def get(self, key):
        v = self.q.value(key, DEFAULTS[key])
        d = DEFAULTS[key]
        if isinstance(d, bool):
            return v in (True, "true", "1", 1)
        if isinstance(d, int):
            try:
                return int(v)
            except (TypeError, ValueError):
                return d
        return v

    def set(self, key, value):
        self.q.setValue(key, value)
        self.apply()

    def apply(self):
        for k in ("output", "jpg_quality", "pdf_dpi"):
            CONFIG[k] = self.get(k)


# ---------------------------------------------------------------- autostart (Windows)
def launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --quiet'
    return f'"{sys.executable}" -m dragshift --quiet'


def autostart_enabled() -> bool:
    if IS_LINUX:
        from . import platform_linux
        return platform_linux.autostart_enabled()
    if IS_MAC:
        from . import platform_mac
        return platform_mac.autostart_enabled()
    if not IS_WIN:
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, APP)
            return True
    except OSError:
        return False


def set_autostart(on: bool):
    if IS_LINUX:
        from . import platform_linux
        platform_linux.set_autostart(on)
        return
    if IS_MAC:
        from . import platform_mac
        platform_mac.set_autostart(on)
        return
    if not IS_WIN:
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if on:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass


TRIGGERS = ([("shift", "Shift ⇧"), ("ctrl", "Control ⌃"), ("alt", "Option ⌥")] if IS_MAC
            else [("shift", "Shift"), ("ctrl", "Ctrl"), ("alt", "Alt")])
OUTPUTS = [("next", "Next to the original"), ("folder", "In a “DragShift” subfolder")]
DPIS = [(150, "150 dpi · small"), (200, "200 dpi · balanced"), (300, "300 dpi · print")]


class SettingsWindow(GlassWindow):
    changed = Signal()

    def __init__(self, settings: Settings):
        super().__init__("Settings", size=(600, 540))
        self.s = settings
        grid = QGridLayout()
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(12)
        r = 0

        def combo(options, current):
            c = QComboBox()
            for v, label in options:
                c.addItem(tr(label), v)
            idx = max(0, [v for v, _ in options].index(current) if current in [v for v, _ in options] else 0)
            c.setCurrentIndex(idx)
            return c

        grid.addWidget(QLabel(tr('Language')),r,0)
        self.language=combo([('auto','System language'),('ru','Русский'),('en','English')],self.s.get('language'))
        self.language.currentIndexChanged.connect(lambda:self._save('language',self.language.currentData()))
        grid.addWidget(self.language,r,1)
        r+=1
        language_hint=QLabel(tr('Restart DragShift to apply the language.'))
        language_hint.setObjectName('dim')
        language_hint.setWordWrap(True)
        grid.addWidget(language_hint,r,1)
        r+=1

        grid.addWidget(QLabel(tr("Open the menu with")), r, 0)
        self.trigger = combo(TRIGGERS, self.s.get("trigger"))
        self.trigger.currentIndexChanged.connect(lambda _: self._save("trigger", self.trigger.currentData()))
        grid.addWidget(self.trigger, r, 1)
        r += 1
        hint = QLabel(tr("…pressed while you’re dragging a file"))
        hint.setObjectName("dim")
        grid.addWidget(hint, r, 1)
        r += 1

        grid.addWidget(QLabel(tr("Save results")), r, 0)
        self.output = combo(OUTPUTS, self.s.get("output"))
        self.output.currentIndexChanged.connect(lambda _: self._save("output", self.output.currentData()))
        grid.addWidget(self.output, r, 1)
        r += 1

        grid.addWidget(QLabel(tr("JPG / WebP quality")), r, 0)
        row = QHBoxLayout()
        self.q = QSlider(Qt.Horizontal)
        self.q.setRange(60, 100)
        self.q.setValue(self.s.get("jpg_quality"))
        from .media_controls import value_editor
        self.qv = value_editor(self.q,'JPG / WebP quality')
        self.q.valueChanged.connect(lambda v:self._save('jpg_quality',v))
        row.addWidget(self.q)
        row.addWidget(self.qv)
        grid.addLayout(row, r, 1)
        r += 1

        grid.addWidget(QLabel(tr("PDF → image resolution")), r, 0)
        self.dpi = combo(DPIS, self.s.get("pdf_dpi"))
        self.dpi.currentIndexChanged.connect(lambda _: self._save("pdf_dpi", self.dpi.currentData()))
        grid.addWidget(self.dpi, r, 1)
        r += 1

        if IS_WIN or IS_MAC or IS_LINUX:
            self.auto = QCheckBox(tr("Start DragShift with Windows" if IS_WIN else "Open DragShift at login"))
            self.auto.setChecked(autostart_enabled())
            self.auto.toggled.connect(set_autostart)
            grid.addWidget(self.auto, r, 1)
            r += 1
        self.body.addLayout(grid)
        self.body.addStretch(1)
        about = QLabel(tr(f"DragShift {__version__}  ·  originals are never modified"))
        about.setObjectName("dim")
        foot = QHBoxLayout()
        foot.addWidget(about)
        foot.addStretch(1)
        done = QPushButton(tr("Done"))
        done.setObjectName("primary")
        done.clicked.connect(self.close)
        foot.addWidget(done)
        self.body.addLayout(foot)

    def _save(self, key, value):
        self.s.set(key, value)
        self.changed.emit()


class WelcomeWindow(GlassWindow):
    def __init__(self, settings: Settings, trigger_label: str = "Shift"):
        super().__init__("Welcome to DragShift", size=(560, 430))
        self.s = settings
        steps = [
            ("1", "Start dragging any file", "in Finder or on the desktop" if IS_MAC else
             "in your file manager (Thunar, Nautilus…)" if IS_LINUX else "in File Explorer or on the desktop"),
            ("2", f"Press {trigger_label}", "the quick menu opens under your cursor"),
            ("3", "Drop it on a slice", "the result is saved next to the original"),
            ("⇄", f"Tap {trigger_label} again", "to switch between Convert and Tools"),
        ]
        for n, head, sub in steps:
            row = QHBoxLayout()
            badge = QLabel(tr(n))
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(34, 34)
            badge.setStyleSheet(f"background: {HOVER}; color: {TEXT};"
                                "border-radius: 6px; font-weight: 600; font-size: 11pt;")
            row.addWidget(badge)
            col = QVBoxLayout()
            col.setSpacing(0)
            h = QLabel(tr(head))
            h.setStyleSheet("font-size: 12pt; font-weight: 600;")
            d = QLabel(tr(sub))
            d.setObjectName("dim")
            col.addWidget(h)
            col.addWidget(d)
            row.addSpacing(8)
            row.addLayout(col, 1)
            self.body.addLayout(row)
        self.body.addStretch(1)
        if IS_LINUX:
            from . import platform_linux
            if platform_linux.is_wayland():
                note = QLabel(tr("You're on a Wayland session: the Shift-while-dragging trigger needs X11 (e.g. XFCE). "
                              "Right-click a file → Open With → DragShift still works."))
                note.setObjectName("warn")
                note.setWordWrap(True)
                self.body.addWidget(note)
        foot = QHBoxLayout()
        if IS_WIN or IS_MAC or IS_LINUX:
            self.auto = QCheckBox(tr("Start with Windows" if IS_WIN else "Open at login"))
            self.auto.setChecked(True)
            foot.addWidget(self.auto)
        foot.addStretch(1)
        ok = QPushButton(tr("Got it"))
        ok.setObjectName("primary")
        ok.clicked.connect(self._done)
        foot.addWidget(ok)
        self.body.addLayout(foot)

    def _done(self):
        self.s.set("welcomed", True)
        if IS_WIN or IS_MAC or IS_LINUX:
            set_autostart(self.auto.isChecked())
        self.close()
