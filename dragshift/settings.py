"""Persistent settings + the Settings and Welcome windows."""
from __future__ import annotations

import sys

from pathlib import Path

from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, QRectF, QSettings, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QFrame, QHBoxLayout,
                               QLabel, QPushButton, QSlider, QVBoxLayout)

from . import __version__
from .util import CONFIG
from .window import GlassWindow

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")
APP = "DragShift"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"

DEFAULTS = {
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


class Switch(QAbstractButton):
    """An on/off switch with a knob that glides across."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(40, 24)
        self._pos = 0.0
        self._anim = QPropertyAnimation(self, b"pos", self)
        self._anim.setDuration(180)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._glide)

    def _glide(self, on):
        self._anim.stop()
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def setChecked(self, on):
        super().setChecked(on)
        self._anim.stop()
        self._pos = 1.0 if on else 0.0
        self.update()

    def _get_pos(self):
        return self._pos

    def _set_pos(self, v):
        self._pos = v
        self.update()

    pos = Property(float, _get_pos, _set_pos)

    def sizeHint(self):
        return QSize(40, 24)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QColor(255, 255, 255, 60))
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        if self._pos > 0:
            g = QLinearGradient(r.topLeft(), r.bottomRight())
            g.setColorAt(0, QColor(90, 200, 255))
            g.setColorAt(1, QColor(47, 123, 255))
            p.setOpacity(self._pos)
            p.setPen(Qt.NoPen)
            p.setBrush(g)
            p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
            p.setOpacity(1)
        d = r.height() - 6
        x = r.left() + 3 + (r.width() - d - 6) * self._pos
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 50))
        p.drawEllipse(QRectF(x, r.top() + 4, d, d))
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))


def card(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A titled glass card; add rows to the returned layout."""
    frame = QFrame()
    frame.setObjectName("card")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 10, 14, 4)
    lay.setSpacing(0)
    head = QLabel(title.upper())
    head.setObjectName("section")
    lay.addWidget(head)
    return frame, lay


def row(lay: QVBoxLayout, title: str, hint: str, *controls) -> QFrame:
    """One setting: a title and optional hint on the left, its control on the right."""
    frame = QFrame()
    frame.setObjectName("row")
    frame.setProperty("first", lay.count() == 1)
    h = QHBoxLayout(frame)
    h.setContentsMargins(0, 10, 0, 10)
    h.setSpacing(12)
    text = QVBoxLayout()
    text.setSpacing(1)
    t = QLabel(title)
    t.setObjectName("rowTitle")
    text.addWidget(t)
    if hint:
        d = QLabel(hint)
        d.setObjectName("dim")
        d.setWordWrap(True)
        text.addWidget(d)
    h.addLayout(text, 1)
    for c in controls:
        h.addWidget(c)
    lay.addWidget(frame)
    return frame


class SettingsWindow(GlassWindow):
    changed = Signal()

    def __init__(self, settings: Settings):
        super().__init__("Settings", size=(540, 500))
        self.s = settings

        def combo(options, current):
            c = QComboBox()
            for v, label in options:
                c.addItem(label, v)
            idx = max(0, [v for v, _ in options].index(current) if current in [v for v, _ in options] else 0)
            c.setCurrentIndex(idx)
            c.setFixedWidth(210)
            c.setCursor(Qt.PointingHandCursor)
            return c

        # header: icon, name and the one-line promise
        head = QHBoxLayout()
        head.setSpacing(12)
        icon = QLabel()
        icon.setPixmap(QPixmap(str(Path(__file__).with_name("assets") / "dragshift.png"))
                       .scaled(40, 40, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        head.addWidget(icon)
        names = QVBoxLayout()
        names.setSpacing(0)
        name = QLabel("DragShift")
        name.setStyleSheet("font-size: 13pt; font-weight: 700;")
        tag = QLabel("Convert and edit files mid-drag")
        tag.setObjectName("dim")
        names.addWidget(name)
        names.addWidget(tag)
        head.addLayout(names, 1)
        self.body.addLayout(head)
        self.body.addSpacing(4)

        frame, lay = card("Menu")
        self.trigger = combo(TRIGGERS, self.s.get("trigger"))
        self.trigger.currentIndexChanged.connect(lambda _: self._save("trigger", self.trigger.currentData()))
        row(lay, "Open the menu with", "Pressed while you’re dragging a file", self.trigger)
        if IS_WIN or IS_MAC or IS_LINUX:
            self.auto = Switch()
            self.auto.setChecked(autostart_enabled())
            self.auto.toggled.connect(set_autostart)
            row(lay, "Start with Windows" if IS_WIN else "Open at login", "Ready whenever you drag a file", self.auto)
        if IS_MAC:
            from . import platform_mac
            if not platform_mac.has_screen_recording():
                glass_btn = QPushButton("Enable…")
                glass_btn.setToolTip("Lets the glass refract the windows behind it (Screen Recording permission)")
                glass_btn.clicked.connect(platform_mac.request_screen_recording)
                row(lay, "Full liquid glass", "Refract the windows behind the menu", glass_btn)
        self.body.addWidget(frame)

        frame, lay = card("Results")
        self.output = combo(OUTPUTS, self.s.get("output"))
        self.output.currentIndexChanged.connect(lambda _: self._save("output", self.output.currentData()))
        row(lay, "Save results", "Originals are never changed", self.output)
        self.q = QSlider(Qt.Horizontal)
        self.q.setRange(60, 100)
        self.q.setValue(self.s.get("jpg_quality"))
        self.q.setFixedWidth(150)
        self.q.setCursor(Qt.PointingHandCursor)
        self.qv = QLabel(str(self.q.value()))
        self.qv.setObjectName("value")
        self.qv.setFixedWidth(26)
        self.qv.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.q.valueChanged.connect(lambda v: (self.qv.setText(str(v)), self._save("jpg_quality", v)))
        row(lay, "JPG / WebP quality", "Higher looks better, lower is smaller", self.q, self.qv)
        self.dpi = combo(DPIS, self.s.get("pdf_dpi"))
        self.dpi.currentIndexChanged.connect(lambda _: self._save("pdf_dpi", self.dpi.currentData()))
        row(lay, "PDF → image", "Resolution of each page", self.dpi)
        self.body.addWidget(frame)

        self.body.addStretch(1)
        about = QLabel(f"DragShift {__version__}  ·  free and open source")
        about.setObjectName("dim")
        foot = QHBoxLayout()
        foot.addWidget(about)
        foot.addStretch(1)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setCursor(Qt.PointingHandCursor)
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
            ("2", f"Press {trigger_label}", "the glass menu opens right under your cursor"),
            ("3", "Drop it on a slice", "the result is saved next to the original"),
            ("⇄", f"Tap {trigger_label} again", "to switch between Convert and Tools"),
        ]
        for n, head, sub in steps:
            row = QHBoxLayout()
            badge = QLabel(n)
            badge.setAlignment(Qt.AlignCenter)
            badge.setFixedSize(34, 34)
            badge.setStyleSheet("background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #5ac8ff, stop:1 #2f7bff);"
                                "border-radius: 17px; font-weight: 700; font-size: 12pt;")
            row.addWidget(badge)
            col = QVBoxLayout()
            col.setSpacing(0)
            h = QLabel(head)
            h.setStyleSheet("font-size: 12pt; font-weight: 600;")
            d = QLabel(sub)
            d.setObjectName("dim")
            col.addWidget(h)
            col.addWidget(d)
            row.addSpacing(8)
            row.addLayout(col, 1)
            self.body.addLayout(row)
        self.body.addStretch(1)
        if IS_MAC:
            from . import platform_mac
            if not platform_mac.has_screen_recording():
                note = QLabel("Optional: allow Screen Recording so the glass can refract the windows "
                              "behind it (otherwise it shows your wallpaper). DragShift never saves or sends it.")
                note.setObjectName("dim")
                note.setWordWrap(True)
                self.body.addWidget(note)
                b = QPushButton("Allow Screen Recording…")
                b.clicked.connect(platform_mac.request_screen_recording)
                self.body.addWidget(b, 0, Qt.AlignLeft)
        if IS_LINUX:
            from . import platform_linux
            if platform_linux.is_wayland():
                note = QLabel("You're on a Wayland session: the Shift-while-dragging trigger needs X11 (e.g. XFCE). "
                              "Right-click a file → Open With → DragShift still works.")
                note.setObjectName("warn")
                note.setWordWrap(True)
                self.body.addWidget(note)
        foot = QHBoxLayout()
        if IS_WIN or IS_MAC or IS_LINUX:
            self.auto = Switch()
            self.auto.setChecked(True)
            foot.addWidget(self.auto)
            foot.addWidget(QLabel("Start with Windows" if IS_WIN else "Open at login"))
        foot.addStretch(1)
        ok = QPushButton("Got it")
        ok.setObjectName("primary")
        ok.clicked.connect(self._done)
        foot.addWidget(ok)
        self.body.addLayout(foot)

    def _done(self):
        self.s.set("welcomed", True)
        if IS_WIN or IS_MAC or IS_LINUX:
            set_autostart(self.auto.isChecked())
        self.close()
