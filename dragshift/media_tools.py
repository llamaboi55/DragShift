"""Tool windows for audio and video: Compress, Trim, Edit.

Same look and flow as the image tools (tools.py): pick options, the window closes, the work runs in
the background and a notification reports the result. Merge and Metadata have no window; the app
runs them directly."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDoubleSpinBox, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QSlider, QVBoxLayout, QWidget)

from . import media
from .glass import qimage_from_pil
from .tools import Notify, ToolWindow, section, slider, slider_row, toggle_row
from .util import file_size, human_size, keep, release

SPEEDS = [("0.5×", 0.5), ("0.75×", 0.75), ("1×", 1.0), ("1.25×", 1.25), ("1.5×", 1.5), ("2×", 2.0)]
HEIGHTS = [("Original", None), ("1080p", 1080), ("720p", 720), ("480p", 480), ("360p", 360)]


def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:04.1f}" if h else f"{int(m)}:{s:04.1f}"


def parse_time(text: str) -> float | None:
    """'83.5', '1:23.5' or '1:02:03' -> seconds."""
    try:
        parts = [float(p) for p in text.strip().split(":")]
    except ValueError:
        return None
    if not parts or len(parts) > 3 or any(p < 0 for p in parts):
        return None
    sec = 0.0
    for p in parts:
        sec = sec * 60 + p
    return sec


def pixmap_from(im: Image.Image, w: int, h: int):
    from PySide6.QtGui import QPixmap
    im = ImageOps.contain(im, (w, h))
    return QPixmap.fromImage(qimage_from_pil(im.convert("RGBA")))


def thumb_label(w: int, h: int) -> QLabel:
    lab = QLabel()
    lab.setFixedSize(w, h)
    lab.setAlignment(Qt.AlignCenter)
    lab.setStyleSheet("border-radius: 12px; background: rgba(0,0,0,0.25);")
    return lab


class _Sig(QObject):
    done = Signal(int, object)


class _Task(QRunnable):
    def __init__(self, serial: int, fn):
        super().__init__()
        self.serial, self.fn, self.sig = serial, fn, _Sig()

    def run(self):
        try:
            result = self.fn()
        except Exception:  # noqa: BLE001
            result = None
        try:
            self.sig.done.emit(self.serial, result)
        except RuntimeError:  # window closed meanwhile
            pass


class Background:
    """Runs the latest request off the UI thread and drops answers that are already out of date."""

    def __init__(self, on_result):
        self.serial = 0
        self.on_result = on_result

    def start(self, fn):
        self.serial += 1
        task = _Task(self.serial, fn)

        def done(serial, result, task=task):
            release(task)
            if serial == self.serial:
                self.on_result(result)
        task.sig.done.connect(done)
        QThreadPool.globalInstance().start(keep(task))


def check_row(text: str, checked: bool = False) -> QCheckBox:
    c = QCheckBox(text)
    c.setChecked(checked)
    return c


def speed_label(speed: float) -> str:
    return f"{speed:g}×"


# ================================================================ COMPRESS
class MediaCompressWindow(ToolWindow):
    tool_name = "Compress"
    AUDIO = [("Tiny", 64), ("Small", 96), ("Balanced", 128), ("High", 192), ("Max", 256)]
    VIDEO = [("Light", "Light"), ("Balanced", "Balanced"), ("Small", "Small"), ("Tiny", "Tiny")]
    SHARES = [("½ of the original", 0.5), ("¼", 0.25), ("⅒", 0.1)]

    def __init__(self, paths, notify: Notify, kind: str):
        super().__init__("Compress", paths, notify, size=(640, 560))
        self.kind = kind
        self.infos = [media.probe(p) for p in self.paths]
        self.single = len(self.paths) == 1
        self.dur = (self.infos[0].duration if self.infos[0] else None) or 0
        self.orig_total = sum(file_size(p) for p in self.paths)

        modes = ["Quality", "Target size"] if self.single and self.dur else ["Quality"]
        row, self.mode = toggle_row(modes)
        self.body.addLayout(row)
        self.mode.idClicked.connect(self._mode)

        self.q_box = QWidget()
        ql = QVBoxLayout(self.q_box)
        ql.setContentsMargins(0, 0, 0, 0)
        ql.addWidget(section("Quality" if kind == "video" else "Bitrate"))
        presets = self.VIDEO if kind == "video" else [(f"{n} · {k}k", k) for n, k in self.AUDIO]
        r, self.preset = toggle_row([p[0] for p in presets], 1 if kind == "video" else 2)
        ql.addLayout(r)
        self.presets = presets
        self.preset.idClicked.connect(lambda _: self._changed())
        self.body.addWidget(self.q_box)

        self.t_box = QWidget()
        tl = QVBoxLayout(self.t_box)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.addWidget(section("Make the file under"))
        tr = QHBoxLayout()
        self.target = QDoubleSpinBox()
        self.target.setDecimals(2)
        self.target.setRange(0.05, 100000)
        self.target.setSuffix(" MB")
        self.target.setFixedWidth(130)
        self.target.setValue(max(0.05, round(self.orig_total / 1048576 * 0.5, 2)))
        self.target.valueChanged.connect(lambda _: self._changed())
        tr.addWidget(self.target)
        for text, share in self.SHARES:
            b = QPushButton(text)
            b.clicked.connect(lambda _=False, s=share: self.target.setValue(max(0.05, round(self.orig_total / 1048576 * s, 2))))
            tr.addWidget(b)
        tr.addStretch(1)
        tl.addLayout(tr)
        self.t_box.setVisible(False)
        self.body.addWidget(self.t_box)

        if kind == "video":
            self.body.addWidget(section("Resolution (never enlarges)"))
            r, self.res = toggle_row([h[0] for h in HEIGHTS])
            self.res.idClicked.connect(lambda _: self._changed())
            self.body.addLayout(r)
            self.hevc = check_row("H.265: smaller files, slower, some old players can't open it")
            self.hevc.setEnabled(media.has_encoder("libx265"))
            self.hevc.toggled.connect(lambda _: self._changed())
            self.body.addWidget(self.hevc)
            self.mono = None
        else:
            self.mono = check_row("Mix down to mono (about half the size, fine for speech)")
            self.mono.toggled.connect(lambda _: self._changed())
            self.body.addWidget(self.mono)
            self.hevc = None

        self.fmt = QLabel()
        self.fmt.setObjectName("dim")
        self.est = QLabel()
        self.est.setObjectName("big")
        self.note = QLabel()
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        for w in (self.fmt, self.est, self.note):
            self.body.addWidget(w)
        self.body.addStretch(1)
        label = "Compress" if self.single else f"Compress {len(self.paths)} files"
        self.body.addLayout(self.footer(label, self.save))

        self._bg = Background(self._estimated)
        self._timer = QTimer(self, singleShot=True, interval=250, timeout=self._estimate)
        self._changed()

    # -- options
    def _target_bytes(self) -> int | None:
        if self.mode.checkedId() == 1 and self.single:
            return int(self.target.value() * 1048576)
        return None

    def _opts(self) -> dict:
        idx = self.preset.checkedId()
        target = self._target_bytes()
        if self.kind == "video":
            return {"preset": self.presets[idx][1], "height": HEIGHTS[self.res.checkedId()][1],
                    "target_bytes": target, "hevc": self.hevc.isChecked()}
        return {"kbps": self.presets[idx][1], "target_bytes": target, "mono": self.mono.isChecked()}

    def _mode(self, i):
        self.q_box.setVisible(i == 0)
        self.t_box.setVisible(i == 1)
        self._changed()

    def _changed(self):
        o = self._opts()
        if self.kind == "audio":
            target = media.audio_target_for(self.path)
            lossless = target in ("WAV", "FLAC", "AIFF")
            self.fmt.setText(f"Saved as MP3 (from {self.path.suffix.lstrip('.').upper()})" if lossless
                             else f"Saved as {self.path.suffix.lstrip('.').upper()}")
        else:
            ext = media.video_ext_for(self.path)
            self.fmt.setText(f"Saved as {ext.lstrip('.').upper()}" +
                             ("" if ext == self.path.suffix.lower() else f" (from {self.path.suffix.lstrip('.').upper()})"))
        self.note.setText("")
        if o["target_bytes"]:
            self.est.setText(f"{human_size(self.orig_total)}  →  about {human_size(o['target_bytes'])}")
            too_big = o["target_bytes"] >= self.orig_total
            self.primary_btn.setEnabled(not too_big)
            if too_big:
                self.note.setText("That's not smaller than the original. Pick a smaller size.")
            return
        self.primary_btn.setEnabled(True)
        if self.kind == "audio":
            kbps = o["kbps"]
            if o["mono"]:
                kbps = max(16, kbps // 2)
            total = sum((i.duration or 0) for i in self.infos if i) * kbps * 1000 / 8
            self.est.setText(f"{human_size(self.orig_total)}  →  about {human_size(total)}" if total else "")
            return
        if len(self.paths) > 1 or not self.dur:
            self.est.setText("")
            self.note.setText("The new size is shown when each file is done.")
            return
        self.est.setText("Estimating…")
        self._timer.start()

    def _estimate(self):
        o = self._opts()
        src = self.path
        self._bg.start(lambda: media.estimate_video_bytes(src, o["preset"], o["height"], o["hevc"]))

    def _estimated(self, nbytes):
        if nbytes is None:
            self.est.setText("")
            return
        self.est.setText(f"{human_size(self.orig_total)}  →  about {human_size(nbytes)}")
        if nbytes >= self.orig_total * 0.95:
            self.note.setText("This file is already well compressed. Try Small or Tiny, or a lower resolution.")

    def save(self):
        o, paths = self._opts(), list(self.paths)
        fn = media.compress_video if self.kind == "video" else media.compress_audio
        self.run(lambda: [fn(p, **o) for p in paths], "Compressing…")


# ================================================================ TRIM
class WaveformView(QWidget):
    """Waveform with the kept range highlighted."""

    def __init__(self, peaks: list[float]):
        super().__init__()
        self.peaks = peaks
        self.a, self.b = 0.0, 1.0
        self.setFixedHeight(92)
        self.setMinimumWidth(360)

    def set_range(self, a: float, b: float):
        self.a, self.b = a, b
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 64))
        p.drawRoundedRect(0, 0, w, h, 12, 12)
        n = len(self.peaks)
        if not n:
            p.setPen(QColor(255, 255, 255, 120))
            p.drawText(self.rect(), Qt.AlignCenter, "Waveform unavailable")
            return
        mid, bw = h / 2, (w - 16) / n
        for i, v in enumerate(self.peaks):
            f = i / n
            inside = self.a <= f <= self.b
            p.setPen(QPen(QColor(255, 255, 255, 235 if inside else 70), max(1.0, bw * 0.7), Qt.SolidLine, Qt.RoundCap))
            x = 8 + i * bw + bw / 2
            half = max(1.5, v * (h / 2 - 8))
            p.drawLine(x, mid - half, x, mid + half)


class TrimWindow(ToolWindow):
    tool_name = "Trim"

    def __init__(self, path, notify: Notify, kind: str):
        super().__init__("Trim", path, notify, size=(660, 560 if kind == "video" else 470))
        self.kind = kind
        info = media.probe(self.path)
        self.dur = (info.duration if info else None) or 0.0
        self.steps = max(1, int(self.dur * 10))
        if self.dur:
            self.subtitle.setText(f"{self.subtitle.text()}  ·  {fmt_time(self.dur)}")

        if kind == "video":
            tr = QHBoxLayout()
            self.t_start, self.t_end = thumb_label(300, 170), thumb_label(300, 170)
            for lab, cap in ((self.t_start, "Starts here"), (self.t_end, "Ends here")):
                col = QVBoxLayout()
                col.addWidget(lab)
                c = QLabel(cap)
                c.setObjectName("dim")
                col.addWidget(c, 0, Qt.AlignHCenter)
                tr.addLayout(col)
            self.body.addLayout(tr)
            self.wave = None
        else:
            self.wave = WaveformView(media.waveform(self.path))
            self.body.addWidget(self.wave)

        g = QGridLayout()
        self.s_start = slider(0, self.steps, 0)
        self.s_end = slider(0, self.steps, self.steps)
        self.e_start, self.e_end = QLineEdit(), QLineEdit()
        for r, (name, s, e) in enumerate((("Start", self.s_start, self.e_start), ("End", self.s_end, self.e_end))):
            e.setFixedWidth(86)
            e.setAlignment(Qt.AlignRight)
            g.addWidget(QLabel(name), r, 0)
            g.addWidget(s, r, 1)
            g.addWidget(e, r, 2)
        self.body.addLayout(g)

        self.fast = None
        if kind == "video":
            self.fast = check_row("Fast cut, no re-encoding (instant, but starts at the nearest keyframe)")
            self.body.addWidget(self.fast)
        self.out = QLabel()
        self.out.setObjectName("big")
        self.msg = QLabel()
        self.msg.setObjectName("dim")
        self.body.addWidget(self.out)
        self.body.addWidget(self.msg)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Trim", self.save))

        self._timer = QTimer(self, singleShot=True, interval=140, timeout=self._thumbs)
        self.s_start.valueChanged.connect(lambda v: self._slid("start", v))
        self.s_end.valueChanged.connect(lambda v: self._slid("end", v))
        self.e_start.editingFinished.connect(lambda: self._typed("start"))
        self.e_end.editingFinished.connect(lambda: self._typed("end"))
        self._refresh()
        self._thumbs()

    def start(self) -> float:
        return self.s_start.value() / 10

    def end(self) -> float:
        return self.s_end.value() / 10

    def _slid(self, which, v):
        self._refresh()
        self._timer.start()

    def _typed(self, which):
        edit, s = (self.e_start, self.s_start) if which == "start" else (self.e_end, self.s_end)
        t = parse_time(edit.text())
        if t is not None:
            s.setValue(min(self.steps, int(round(t * 10))))
        self._refresh()
        self._timer.start()

    def _refresh(self):
        a, b = self.start(), self.end()
        for edit, v in ((self.e_start, a), (self.e_end, b)):
            if not edit.hasFocus():
                edit.setText(fmt_time(v))
        ok = b > a + 0.1
        self.primary_btn.setEnabled(ok)
        self.out.setText(f"Keeps {fmt_time(b - a)} of {fmt_time(self.dur)}" if ok else "")
        self.msg.setText("" if ok else "The end has to be after the start.")
        if self.wave is not None and self.dur:
            self.wave.set_range(a / self.dur, b / self.dur)

    def _thumbs(self):
        if self.kind != "video":
            return
        for lab, t in ((self.t_start, self.start()), (self.t_end, max(self.start(), self.end() - 0.2))):
            im = media.frame_at(self.path, t, 600)
            if im is not None:
                lab.setPixmap(pixmap_from(im, lab.width(), lab.height()))

    def save(self):
        a, b, path = self.start(), self.end(), self.path
        end = None if b >= self.dur - 0.05 else b
        exact = not (self.fast is not None and self.fast.isChecked())
        self.run(lambda: [media.trim(path, a, end, exact=exact)], "Trimming…")


# ================================================================ EDIT
class MediaEditWindow(ToolWindow):
    tool_name = "Edit"

    def __init__(self, path, notify: Notify, kind: str):
        super().__init__("Edit", path, notify, size=(660, 660 if kind == "video" else 540))
        self.kind = kind
        self.info = media.probe(self.path)
        self.dur = (self.info.duration if self.info else None) or 0.0
        self.has_audio = bool(self.info and self.info.has_audio)
        self.frame = None

        if kind == "video":
            self.preview = thumb_label(360, 200)
            self.body.addWidget(self.preview, 0, Qt.AlignHCenter)
            self.frame = media.frame_at(self.path, self.dur * 0.1 if self.dur else 0, 800)
            self.body.addWidget(section("Rotate and flip"))
            r, self.rot = toggle_row(["None", "90° ⟳", "180°", "90° ⟲"])
            self.rot.idClicked.connect(lambda _: self._changed())
            self.body.addLayout(r)
            fr = QHBoxLayout()
            self.flip_h, self.flip_v = check_row("Mirror left-right"), check_row("Flip upside-down")
            for c in (self.flip_h, self.flip_v):
                c.toggled.connect(lambda _: self._changed())
                fr.addWidget(c)
            fr.addStretch(1)
            self.body.addLayout(fr)
            self.body.addWidget(section("Resolution (never enlarges)"))
            r, self.res = toggle_row([h[0] for h in HEIGHTS])
            self.res.idClicked.connect(lambda _: self._changed())
            self.body.addLayout(r)
        else:
            self.preview = None

        self.body.addWidget(section("Speed (pitch is kept)"))
        r, self.speed = toggle_row([s[0] for s in SPEEDS], 2)
        self.speed.idClicked.connect(lambda _: self._changed())
        self.body.addLayout(r)

        self.body.addWidget(section("Sound"))
        g = QGridLayout()
        self.gain = slider(-20, 20, 0)
        slider_row(g, 0, "Volume", self.gain, lambda v: f"{v:+d} dB")
        self.gain.valueChanged.connect(lambda _: self._changed())
        self.body.addLayout(g)
        sr = QHBoxLayout()
        self.norm = check_row("Even out loudness")
        self.norm.toggled.connect(lambda on: (self.gain.setEnabled(not on), self._changed()))
        sr.addWidget(self.norm)
        self.mute = None
        self.mono = None
        if kind == "video":
            self.mute = check_row("Remove sound")
            self.mute.toggled.connect(lambda on: (self.gain.setEnabled(not on and not self.norm.isChecked()),
                                                  self.norm.setEnabled(not on), self._changed()))
            sr.addWidget(self.mute)
        else:
            self.mono = check_row("Mix down to mono")
            self.mono.toggled.connect(lambda _: self._changed())
            sr.addWidget(self.mono)
        sr.addStretch(1)
        self.body.addLayout(sr)
        for w in (self.gain, self.norm):
            w.setEnabled(self.has_audio)
        if self.mute is not None:
            self.mute.setEnabled(self.has_audio)

        self.fades = []
        if kind == "audio":
            fr = QHBoxLayout()
            for text in ("Fade in", "Fade out"):
                fr.addWidget(QLabel(text))
                sp = QDoubleSpinBox()
                sp.setRange(0, 30)
                sp.setSingleStep(0.5)
                sp.setSuffix(" s")
                sp.valueChanged.connect(lambda _: self._changed())
                fr.addWidget(sp)
                self.fades.append(sp)
            fr.addStretch(1)
            self.body.addLayout(fr)

        self.out = QLabel()
        self.out.setObjectName("big")
        self.body.addWidget(self.out)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Save", self.save, on_reset=self._reset))
        self._changed()

    def _reset(self):
        for grp, i in ((getattr(self, "rot", None), 0), (getattr(self, "res", None), 0), (self.speed, 2)):
            if grp is not None:
                grp.button(i).setChecked(True)
        self.gain.setValue(0)
        for c in (self.norm, self.mute, self.mono, getattr(self, "flip_h", None), getattr(self, "flip_v", None)):
            if c is not None:
                c.setChecked(False)
        for sp in self.fades:
            sp.setValue(0)
        self._changed()

    def _opts(self) -> dict:
        speed = SPEEDS[self.speed.checkedId()][1]
        if self.kind == "audio":
            return {"gain_db": float(self.gain.value()), "normalize": self.norm.isChecked(), "speed": speed,
                    "fade_in": self.fades[0].value(), "fade_out": self.fades[1].value(),
                    "mono": self.mono.isChecked()}
        return {"rotate": (0, 90, 180, 270)[self.rot.checkedId()], "flip_h": self.flip_h.isChecked(),
                "flip_v": self.flip_v.isChecked(), "speed": speed, "gain_db": float(self.gain.value()),
                "mute": self.mute.isChecked(), "height": HEIGHTS[self.res.checkedId()][1],
                "normalize": self.norm.isChecked()}

    def _changed(self):
        o = self._opts()
        if self.kind == "audio":
            dirty = (o["gain_db"] != 0 or o["normalize"] or o["speed"] != 1 or o["fade_in"] or o["fade_out"]
                     or o["mono"])
        else:
            dirty = (o["rotate"] or o["flip_h"] or o["flip_v"] or o["speed"] != 1 or o["gain_db"] != 0
                     or o["mute"] or o["height"] or o["normalize"])
        self.primary_btn.setEnabled(bool(dirty))
        bits = []
        if self.dur:
            new = self.dur / o["speed"]
            bits.append(f"{fmt_time(self.dur)} → {fmt_time(new)}" if o["speed"] != 1 else fmt_time(self.dur))
        if self.kind == "video" and o["rotate"] in (90, 270) and self.info and self.info.width:
            bits.append(f"{self.info.height}×{self.info.width}")
        self.out.setText("  ·  ".join(bits))
        if self.preview is not None and self.frame is not None:
            im = self.frame
            im = {0: im, 90: im.rotate(-90, expand=True), 180: im.rotate(180), 270: im.rotate(90, expand=True)}[o["rotate"]]
            if o["flip_h"]:
                im = ImageOps.mirror(im)
            if o["flip_v"]:
                im = ImageOps.flip(im)
            self.preview.setPixmap(pixmap_from(im, 360, 200))

    def save(self):
        o, path = self._opts(), self.path
        fn = media.edit_video if self.kind == "video" else media.edit_audio
        self.run(lambda: [fn(path, **o)], "Editing…")
