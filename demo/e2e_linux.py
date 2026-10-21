"""End-to-end test on a real X11 desktop: Xvfb + XFCE's window manager (xfwm4, compositor OFF,
like many Debian/XFCE machines) + the Thunar file manager.

xdotool drives real XTest mouse/keyboard input: drag a file out of a Thunar window, press Shift,
drop on a slice (press Enter in tool windows = their main button), then every output is verified.
Runs against the *installed* .deb (`dragshift` on PATH) unless DRAGSHIFT_CMD is set.
Writes e2e_linux_out/: results.md / results.json, log.txt, screenshots, app_log.txt, video."""
from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
import sys
import time
import traceback
import types
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo"))
# the shared sample-file makers and verifiers live in e2e_windows; stub its Windows-only imports
_wk = types.ModuleType("winkit")
_wk.ROOT = Path("/tmp/dragshift_e2e_unused")
_wk.ROOT.mkdir(parents=True, exist_ok=True)
(_wk.ROOT / "e2e_out" / "shots").mkdir(parents=True, exist_ok=True)
_wk.desktop_dir = lambda: Path("/tmp/dragshift_e2e_unused")
sys.modules.setdefault("winkit", _wk)
sys.modules.setdefault("pyautogui", types.ModuleType("pyautogui"))
import dragshift.convert  # noqa: E402,F401  (HEIC support for verifiers)
from e2e_windows import (make_files, img_ok, v_format, v_pages_to_images,  # noqa: E402
                         v_pdf_n, v_rotated, v_pdf_compress, v_merged, v_protected)

OUT = ROOT / "e2e_linux_out"
(OUT / "shots").mkdir(parents=True, exist_ok=True)
BASE = Path.home() / "DragShiftTest"
LOGF = open(OUT / "log.txt", "w", encoding="utf-8")
RESULTS: list[dict] = []
T0 = time.time()
R_PETAL = 109
WIN = (40, 40, 600, 540)           # Thunar window x, y, w, h
CMD = shlex.split(os.environ.get("DRAGSHIFT_CMD", "dragshift"))


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOGF.write(f"[{time.time() - T0:7.2f}] {msg}\n")
    LOGF.flush()


def sh(*cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def xdo(*a):
    return sh("xdotool", *a)


def grab() -> Image.Image:
    from PIL import ImageGrab
    return ImageGrab.grab(xdisplay=os.environ.get("DISPLAY", ":99"))


def shot(name: str) -> Path:
    p = OUT / "shots" / f"{name}.png"
    grab().save(p)
    return p


def cursor() -> tuple[int, int]:
    o = xdo("getmouselocation", "--shell").stdout
    d = dict(line.split("=") for line in o.split() if "=" in line)
    return int(d["X"]), int(d["Y"])


def move(x, y, dur=0.35):
    x0, y0 = cursor()
    steps = max(2, int(dur * 50))
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        xdo("mousemove", int(x0 + (x - x0) * t), int(y0 + (y - y0) * t))
        time.sleep(dur / steps)


def screen_size():
    w, h = xdo("getdisplaygeometry").stdout.split()
    return int(w), int(h)


def menu_center(cur, size=400):
    sw, shh = screen_size()
    half = size // 2
    return min(max(cur[0], half), sw - half), min(max(cur[1], half), shh - half)


def petal_xy(c, n, i, r=R_PETAL):
    a = math.radians(90 - i * 360 / n)
    return int(c[0] + r * math.cos(a)), int(c[1] - r * math.sin(a))


# ------------------------------------------------------------------ Thunar
def open_folder(folder: Path, files: list[str]) -> list[tuple[int, int]]:
    """Show the (empty) folder in a fresh Thunar window at a known place, copy the files in, and
    locate their icons by diffing screenshots before/after (independent of theme and version)."""
    sh("thunar", "--quit")
    time.sleep(0.6)
    subprocess.Popen(["thunar", str(folder)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    wid = ""
    for _ in range(40):
        wid = xdo("search", "--onlyvisible", "--class", "Thunar").stdout.split()
        if wid:
            wid = wid[-1]
            break
        time.sleep(0.25)
    if not wid:
        raise AssertionError("Thunar window did not appear")
    xdo("windowmove", wid, WIN[0], WIN[1])
    xdo("windowsize", wid, WIN[2], WIN[3])
    xdo("windowactivate", "--sync", wid)
    xdo("mousemove", 900, 700)
    time.sleep(1.2)
    empty = grab()
    for f in files:
        shutil.copy(BASE / "src" / f, folder / f)
    time.sleep(2.0)                       # Thunar picks up new files + makes thumbnails
    return find_icons(empty, grab(), len(files))


def find_icons(empty: Image.Image, full: Image.Image, n: int) -> list[tuple[int, int]]:
    x0, y0, w, h = WIN
    box = (x0, y0 + 30, x0 + w, y0 + h)
    a = np.asarray(empty.crop(box).convert("L")).astype(int)
    b = np.asarray(full.crop(box).convert("L")).astype(int)
    m = np.abs(a - b) > 30
    # ignore the status bar text that changes too ("1 file: …")
    m[-40:] = False
    cols = np.nonzero(m.sum(axis=0) > 3)[0]
    if not len(cols):
        raise AssertionError("no icons appeared in the Thunar window")
    groups, start = [], cols[0]
    for c0, c1 in zip(cols, cols[1:]):
        if c1 - c0 > 10:
            groups.append((start, c0))
            start = c1
    groups.append((start, cols[-1]))
    centres = []
    for g0, g1 in groups:
        ys = np.nonzero(m[:, g0:g1 + 1].any(axis=1))[0]
        if len(ys) < 10:
            continue
        top, bot = ys.min(), ys.max()
        centres.append((int(box[0] + (g0 + g1) / 2), int(box[1] + top + (bot - top) * 0.3)))
    if len(centres) < n:
        raise AssertionError(f"found {len(centres)} icons, expected {n}")
    return centres[:n]


# ------------------------------------------------------------------ scenario
def ring_index(paths, ring, label):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, paths)
    items = rings[ring]
    return list(rings).index(ring), [i.label for i in items].index(label), len(items)


def snapshot(folder):
    return {str(p.relative_to(folder)) for p in folder.rglob("*")}


def new_files(folder, before, timeout=15):
    end = time.time() + timeout

    def current():
        return {n: (folder / n).stat().st_size for n in snapshot(folder) - before
                if (folder / n).is_file() and not Path(n).name.startswith(".~")}
    while time.time() < end:
        a = current()
        if a:
            time.sleep(0.8)
            b = current()
            if a == b:
                return sorted(folder / n for n in b)
        time.sleep(0.25)
    return []


def run(name, files, ring, label, verify, after_drop=None):
    k = len(RESULTS) + 1
    folder = BASE / f"{k:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    srcs = [str(folder / f) for f in sorted(files)]
    tag = f"{k:02d}_{''.join(ch if ch.isalnum() else '_' for ch in name)}"
    t0 = time.time()
    status, msg = "PASS", ""
    try:
        icons = open_folder(folder, sorted(files))
        ring_i, idx, n = ring_index(srcs, ring, label)
        before = snapshot(folder)
        if len(files) > 1:          # select all with Ctrl-click, then drag the last
            for j, (x, y) in enumerate(icons):
                move(x, y, 0.2)
                if j:
                    xdo("keydown", "ctrl")
                xdo("click", "1")
                if j:
                    xdo("keyup", "ctrl")
                time.sleep(0.15)
        x, y = icons[-1]
        move(x, y)
        time.sleep(0.2)
        xdo("mousedown", "1")
        time.sleep(0.15)
        for s in range(1, 10):
            xdo("mousemove", x + s * 4, y + s * 2)
            time.sleep(0.02)
        move(x + 560, min(screen_size()[1] - 260, y + 160), 0.5)
        time.sleep(0.25)
        xdo("keydown", "shift")
        time.sleep(0.4)
        cur = cursor()
        xdo("mousemove_relative", "--", "3", "3")
        time.sleep(0.05)
        xdo("mousemove", *cur)
        time.sleep(0.45)
        c = menu_center(cur)
        if ring_i == 1:              # tap Shift again: Convert → Tools
            xdo("keyup", "shift")
            time.sleep(0.25)
            xdo("keydown", "shift")
            time.sleep(0.12)
            xdo("keyup", "shift")
            time.sleep(0.45)
        tx, ty = petal_xy(c, n, idx)
        move(tx, ty, 0.4)
        time.sleep(0.4)
        shot(f"{tag}_menu")
        xdo("mouseup", "1")
        time.sleep(0.1)
        xdo("keyup", "shift")
        if after_drop:
            after_drop(tag)
        outs = new_files(folder, before)
        if not outs:
            raise AssertionError("no output file appeared")
        msg = verify(outs, srcs) or ", ".join(o.name for o in outs[:3])
    except Exception as e:
        status, msg = "FAIL", f"{type(e).__name__}: {e}"
        log(traceback.format_exc())
        shot(f"{tag}_FAIL")
        xdo("mouseup", "1")
        xdo("keyup", "shift")
        xdo("key", "Escape")
    dt = time.time() - t0
    RESULTS.append({"name": name, "where": "thunar (xfwm4, no compositor)", "status": status,
                    "detail": msg, "seconds": round(dt, 1)})
    log(f"{status:4} {name:30} {dt:5.1f}s  {msg}")
    time.sleep(0.6)


def enter_after(delay=2.5, text=None):
    def f(tag):
        time.sleep(delay)            # the window opens on its preview
        shot(f"{tag}_window")
        if text:
            xdo("type", "--delay", "40", text)
            time.sleep(0.2)
        xdo("key", "Return")
    return f


def v_dims(fn):
    def v(outs, srcs):
        with Image.open(srcs[-1]) as a:
            sa = a.size
        sb = img_ok(outs[0])
        assert fn(sa, sb), f"{sa} → {sb}"
        return f"{sa[0]}×{sa[1]} → {sb[0]}×{sb[1]}"
    return v


def v_smaller(outs, srcs):
    s0, s1 = Path(srcs[0]).stat().st_size, outs[0].stat().st_size
    img_ok(outs[0])
    assert s1 < s0
    return f"{s0 // 1024} KB → {s1 // 1024} KB · {100 - s1 * 100 // s0}% smaller"


def v_no_meta(outs, srcs):
    from dragshift import imaging
    n, gps = imaging.count_metadata(outs[0])
    assert n == 0 and not gps, f"{n} fields remain"
    return "0 metadata fields left"


def open_with_test():
    """Right-click → Open With DragShift: the app is launched with the file as an argument."""
    k = len(RESULTS) + 1
    folder = BASE / f"{k:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    src = folder / "D-Photo.jpg"
    t0 = time.time()
    status, msg = "PASS", ""
    try:
        open_folder(folder, ["D-Photo.jpg"])
        before = snapshot(folder)
        xdo("mousemove", 640, 400)
        subprocess.Popen(CMD + [str(src)])          # what the .desktop entry's Exec runs
        time.sleep(1.8)
        shot(f"{k:02d}_open_with_menu")
        ring_i, idx, n = ring_index([str(src)], "Convert", "AVIF")
        tx, ty = petal_xy((640, 400), n, idx)
        move(tx, ty, 0.3)
        time.sleep(0.3)
        xdo("click", "1")
        outs = new_files(folder, before)
        if not outs:
            raise AssertionError("no output file appeared")
        msg = v_format("AVIF", ".avif")(outs, [str(src)]) or outs[0].name
    except Exception as e:
        status, msg = "FAIL", f"{type(e).__name__}: {e}"
        log(traceback.format_exc())
        xdo("key", "Escape")
    dt = time.time() - t0
    RESULTS.append({"name": "Open With → AVIF (click)", "where": "thunar", "status": status,
                    "detail": msg, "seconds": round(dt, 1)})
    log(f"{status:4} {'Open With → AVIF (click)':30} {dt:5.1f}s  {msg}")


def main():
    log("display", os.environ.get("DISPLAY"), "screen", screen_size(), "cmd", CMD)
    shutil.rmtree(BASE, ignore_errors=True)
    BASE.mkdir(parents=True, exist_ok=True)
    make_files(BASE / "src", "D-")
    cfg = Path.home() / ".config" / "DragShift"
    cfg.mkdir(parents=True, exist_ok=True)
    (cfg / "DragShift.conf").write_text("[General]\nwelcomed=true\n")
    env = dict(os.environ, DRAGSHIFT_RECORDING="1")
    applog = open(OUT / "app_log.txt", "w")
    app = subprocess.Popen(CMD + ["--quiet"], env=env, stdout=applog, stderr=subprocess.STDOUT)
    time.sleep(5)
    rec = None
    if shutil.which("ffmpeg"):
        w, h = screen_size()
        rec = subprocess.Popen(["ffmpeg", "-y", "-f", "x11grab", "-draw_mouse", "1", "-framerate", "25",
                                "-video_size", f"{w}x{h}", "-i", os.environ.get("DISPLAY", ":99"),
                                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                                str(OUT / "e2e_linux.mp4")],
                               stdin=subprocess.PIPE, stdout=open(OUT / "ffmpeg.txt", "w"), stderr=subprocess.STDOUT)
        time.sleep(1.5)
    try:
        run("JPG → PNG", ["D-Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png"))
        run("JPG → WEBP", ["D-Photo.jpg"], "Convert", "WEBP", v_format("WEBP", ".webp"))
        run("JPG → HEIC", ["D-Photo.jpg"], "Convert", "HEIC", v_format("HEIC", ".heic"))
        run("JPG → PDF", ["D-Photo.jpg"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("PNG → JPG", ["D-Graphic.png"], "Convert", "JPG", v_format("JPEG", ".jpg"))
        run("PDF → JPG (3 pages)", ["D-Report.pdf"], "Convert", "JPG", v_pages_to_images(3, ".jpg"))
        run("PDF → TXT", ["D-Report.pdf"], "Convert", "TXT", v_format("TXT", ".txt"))
        run("DOCX → PDF", ["D-Letter.docx"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("TXT → PDF", ["D-Notes.txt"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("PDF Split", ["D-Report.pdf"], "Tools", "Split", v_pdf_n(3))
        run("PDF Rotate", ["D-Report.pdf"], "Tools", "Rotate", v_rotated)
        run("PDF Compress", ["D-Report.pdf"], "Tools", "Compress", v_pdf_compress)
        run("Merge 2 PDFs", ["D-Appendix.pdf", "D-Report.pdf"], "Tools", "Merge", v_merged, enter_after())
        run("Compress (window, Enter)", ["D-Photo.jpg"], "Tools", "Compress", v_smaller, enter_after())
        run("Resize 50% (window, Enter)", ["D-Photo.jpg"], "Tools", "Resize",
            v_dims(lambda a, b: b == (a[0] // 2, a[1] // 2)), enter_after())
        run("Add BG (window, Enter)", ["D-Graphic.png"], "Tools", "Add BG",
            v_dims(lambda a, b: b[0] > a[0] and b[1] > a[1]), enter_after(3.0))
        run("Metadata: remove all (Enter)", ["D-Photo.jpg"], "Tools", "Metadata", v_no_meta, enter_after())
        run("PDF Protect (password)", ["D-Report.pdf"], "Tools", "Protect", v_protected,
            enter_after(2.0, text="keywest2026\tkeywest2026"))
        open_with_test()
    except Exception:
        log("ABORTED\n" + traceback.format_exc())
    finally:
        if rec:
            try:
                rec.stdin.write(b"q")
                rec.stdin.flush()
                rec.wait(timeout=60)
            except Exception:
                rec.terminate()
        app.terminate()
        passed = sum(r["status"] == "PASS" for r in RESULTS)
        (OUT / "results.json").write_text(json.dumps(RESULTS, indent=1))
        lines = [f"# DragShift Debian/XFCE end-to-end — {passed}/{len(RESULTS)} passed", "",
                 "Xvfb + xfwm4 (compositor off) + Thunar, driving the installed .deb.", "",
                 "| # | Scenario | Result | Detail | Time |", "|---|---|---|---|---|"]
        for i, r in enumerate(RESULTS, 1):
            lines.append(f"| {i} | {r['name']} | {'✅' if r['status'] == 'PASS' else '❌'} | {r['detail']} | {r['seconds']}s |")
        (OUT / "results.md").write_text("\n".join(lines), encoding="utf-8")
        log(f"DONE {passed}/{len(RESULTS)}")
        sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
