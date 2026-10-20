"""End-to-end test on a real macOS desktop (GitHub Actions macOS runner).

Real Quartz mouse/keyboard events drag files on the Finder desktop, press Shift, drop on a
slice, (press Enter in tool windows = their main button), and every output is verified.
Writes e2e_mac_out/: results.md / results.json, log.txt, screenshots, app_log.txt, video."""
from __future__ import annotations

import json
import math
import os
import shutil
import signal
import subprocess
import sys
import time
import traceback
from pathlib import Path

import Quartz
from PIL import Image, ImageChops, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dragshift.convert  # noqa: E402,F401  (HEIC support for verifiers)
from e2e_windows import (make_files, img_ok, pdf_pages, v_format, v_pages_to_images,  # noqa: E402
                         v_combined, v_pdf_n, v_rotated, v_pdf_compress, v_merged)

OUT = ROOT / "e2e_mac_out"
(OUT / "shots").mkdir(parents=True, exist_ok=True)
DESK = Path.home() / "DragShiftTest"   # a Finder window on this folder
LOGF = open(OUT / "log.txt", "w", encoding="utf-8")
RESULTS: list[dict] = []
T0 = time.time()
R_PETAL = 109
SHIFT_KEY = 56


def log(*a):
    msg = " ".join(str(x) for x in a)
    print(msg, flush=True)
    LOGF.write(f"[{time.time() - T0:7.2f}] {msg}\n")
    LOGF.flush()


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, **kw)


def osa(script: str) -> str:
    r = sh(["osascript", "-e", script])
    if r.returncode:
        log("osascript error:", r.stderr.strip())
    return r.stdout.strip()


def shot(name: str) -> Path:
    p = OUT / "shots" / f"{name}.png"
    sh(["screencapture", "-x", str(p)])
    return p


# ------------------------------------------------------------------ Quartz input
def post_mouse(kind, x, y, flags=0):
    ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def post_key(code, down, flags=0):
    ev = Quartz.CGEventCreateKeyboardEvent(None, code, down)
    if flags:
        Quartz.CGEventSetFlags(ev, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def cursor():
    loc = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return int(loc.x), int(loc.y)


def move(x, y, dur=0.4, dragging=False, flags=0):
    x0, y0 = cursor()
    steps = max(2, int(dur * 60))
    kind = Quartz.kCGEventLeftMouseDragged if dragging else Quartz.kCGEventMouseMoved
    for i in range(1, steps + 1):
        t = i / steps
        t = t * t * (3 - 2 * t)
        post_mouse(kind, x0 + (x - x0) * t, y0 + (y - y0) * t, flags)
        time.sleep(dur / steps)


def screen_size():
    b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
    return int(b.size.width), int(b.size.height)


def menu_center(cur, size=400):
    sw, shh = screen_size()
    half = size // 2
    return min(max(cur[0], half), sw - half), min(max(cur[1], half), shh - half)


def petal_xy(c, n, i, r=R_PETAL):
    a = math.radians(90 - i * 360 / n)
    return int(c[0] + r * math.cos(a)), int(c[1] - r * math.sin(a))


def press_enter():
    post_key(36, True)
    time.sleep(0.05)
    post_key(36, False)


# ------------------------------------------------------------------ desktop icons
WIN = {"left": 60, "top": 60, "right": 640, "bottom": 720}


def open_finder_window():
    DESK.mkdir(exist_ok=True)
    osa('tell application "Finder" to close every window')
    osa(f'tell application "Finder" to open POSIX file "{DESK}"')
    time.sleep(1.5)
    osa(f"""tell application "Finder"
        activate
        set w to front window
        set current view of w to icon view
        set toolbar visible of w to false
        set statusbar visible of w to false
        set bounds of w to {{{WIN['left']}, {WIN['top']}, {WIN['right']}, {WIN['bottom']}}}
        set arrangement of icon view options of w to not arranged
        set icon size of icon view options of w to 64
    end tell""")
    time.sleep(1.0)


def arrange(names: list[str]):
    """Place icons in the Finder window at known spots; convert to screen coordinates."""
    for i, n in enumerate(names):
        osa(f'tell application "Finder" to set position of item "{n}" of front window to {{80, {60 + i * 115}}}')
    time.sleep(0.8)
    b = osa('tell application "Finder" to get bounds of front window')
    try:
        WIN["left"], WIN["top"], WIN["right"], WIN["bottom"] = (int(v) for v in b.split(","))
    except Exception:
        pass
    pos = {}
    for n in names:
        out = osa(f'tell application "Finder" to get position of item "{n}" of front window')
        try:
            x, y = (int(v.strip()) for v in out.split(","))
            pos[n] = (WIN["left"] + x, WIN["top"] + 28 + y)   # 28 = title bar
        except Exception:
            log("no position for", n, repr(out))
    log("icon positions (screen):", pos)
    return pos


ICON_OFFSET = [0, 0]   # calibrated at start-up from a screenshot


def icon_center(name: str, pos: dict) -> tuple[int, int]:
    x, y = pos[name]
    return x + ICON_OFFSET[0], y + ICON_OFFSET[1]


def calibrate(pos: dict, empty: Image.Image, full: Image.Image):
    """Find where icons really are relative to Finder's reported positions: diff a screenshot
    of the bare desktop against one with the icons, and take the blob nearest each position."""
    a, b = empty.convert("L"), full.convert("L")
    k = a.width / screen_size()[0]                      # screenshot px per point (Retina = 2)
    diff = ImageChops.difference(a, b).point(lambda v: 255 if v > 40 else 0)
    import numpy as np
    m = np.asarray(diff) > 0
    offs = []
    for n, (x, y) in pos.items():
        # search a window around the reported point for the icon's changed pixels
        x0, y0 = int((x - 70) * k), int((y - 70) * k)
        x1, y1 = int((x + 70) * k), int((y + 70) * k)
        sub = m[max(0, y0):y1, max(0, x0):x1]
        ys, xs = np.nonzero(sub)
        if len(xs) < 50:
            continue
        # icon image = upper part of the blob (label text is below it)
        top = ys.min()
        img_rows = ys < top + (ys.max() - top) * 0.6
        cx = (xs[img_rows].mean() + max(0, x0)) / k
        cy = (ys[img_rows].mean() + max(0, y0)) / k
        offs.append((cx - x, cy - y))
    # Finder's reported position is the icon centre (confirmed on screenshots); only log the
    # measured blob offset for diagnostics
    if offs:
        log("measured blob offset (not applied):", int(sum(o[0] for o in offs) / len(offs)),
            int(sum(o[1] for o in offs) / len(offs)))


# ------------------------------------------------------------------ scenario
def ring_index(paths, ring, label):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, paths)
    items = rings[ring]
    return list(rings).index(ring), [i.label for i in items].index(label), len(items)


def snapshot() -> set[str]:
    return {p.name for p in DESK.iterdir()} | {f"{p.parent.name}/{p.name}" for p in DESK.glob("* images/*")}


def new_files(before, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        now = snapshot() - before
        files = [DESK / n for n in now if (DESK / n).is_file() and not n.startswith(".")]
        if files:
            time.sleep(1.0)
            now = snapshot() - before
            return sorted(DESK / n for n in now if (DESK / n).is_file() and not n.startswith("."))
        time.sleep(0.25)
    return []


def stash(before):
    for n in sorted(snapshot() - before, key=len, reverse=True):
        p = DESK / n
        try:
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
        except Exception:
            pass


def run(name, files, ring, label, verify, after_drop=None, multi=False):
    global POS
    srcs = [str(DESK / f) for f in files]
    before = snapshot()
    t0 = time.time()
    status, msg = "PASS", ""
    try:
        ring_i, idx, n = ring_index(srcs, ring, label)
        if multi:  # select both with Cmd-click, then drag the last
            x, y = icon_center(files[0], POS)
            move(x, y)
            post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
            post_mouse(Quartz.kCGEventLeftMouseUp, x, y)
            for f in files[1:]:
                x, y = icon_center(f, POS)
                move(x, y)
                post_mouse(Quartz.kCGEventLeftMouseDown, x, y, Quartz.kCGEventFlagMaskCommand)
                post_mouse(Quartz.kCGEventLeftMouseUp, x, y, Quartz.kCGEventFlagMaskCommand)
            time.sleep(0.3)
        x, y = icon_center(files[-1], POS)
        move(x, y, 0.4)
        time.sleep(0.2)
        post_mouse(Quartz.kCGEventLeftMouseDown, x, y)
        time.sleep(0.15)
        for k in range(1, 10):
            post_mouse(Quartz.kCGEventLeftMouseDragged, x + k * 4, y + k * 2)
            time.sleep(0.02)
        sw, shh = screen_size()
        move(x + 620, min(shh - 260, y + 120), 0.6, dragging=True)
        time.sleep(0.2)
        post_key(SHIFT_KEY, True, Quartz.kCGEventFlagMaskShift)
        time.sleep(0.4)
        if not RESULTS:                       # first run: menu before the mouse moves at all
            time.sleep(0.6)
            shot("01_still_before_moving")
        cur = cursor()
        move(cur[0] + 3, cur[1] + 3, 0.05, dragging=True, flags=Quartz.kCGEventFlagMaskShift)
        move(cur[0], cur[1], 0.05, dragging=True, flags=Quartz.kCGEventFlagMaskShift)
        time.sleep(0.35)
        c = menu_center(cur)
        if ring_i == 1:
            post_key(SHIFT_KEY, False)
            time.sleep(0.25)
            post_key(SHIFT_KEY, True, Quartz.kCGEventFlagMaskShift)
            time.sleep(0.12)
            post_key(SHIFT_KEY, False)
            time.sleep(0.45)
        tx, ty = petal_xy(c, n, idx)
        move(tx, ty, 0.4, dragging=True)
        time.sleep(0.35)
        shot(f"{len(RESULTS) + 1:02d}_{''.join(ch if ch.isalnum() else '_' for ch in name)}_menu")
        post_mouse(Quartz.kCGEventLeftMouseUp, tx, ty)
        time.sleep(0.1)
        post_key(SHIFT_KEY, False)
        if after_drop:
            after_drop()
        outs = new_files(before)
        if not outs:
            raise AssertionError("no output file appeared")
        msg = verify(outs, srcs) or ", ".join(o.name for o in outs[:3])
    except Exception as e:
        status, msg = "FAIL", f"{type(e).__name__}: {e}"
        log(traceback.format_exc())
        shot(f"{len(RESULTS) + 1:02d}_FAIL")
        post_mouse(Quartz.kCGEventLeftMouseUp, *cursor())
        post_key(SHIFT_KEY, False)
        post_key(53, True)
        post_key(53, False)
    dt = time.time() - t0
    RESULTS.append({"name": name, "where": "mac desktop", "status": status, "detail": msg, "seconds": round(dt, 1)})
    log(f"{status:4} {name:30} {dt:5.1f}s  {msg}")
    stash(before)
    POS = arrange(SOURCES)
    time.sleep(0.5)


def enter_after(delay=2.0):
    def f():
        time.sleep(delay)   # window opens on its preview
        press_enter()
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
    return f"{s0 // 1024} KB → {s1 // 1024} KB"


def v_no_meta(outs, srcs):
    from dragshift import imaging
    n, gps = imaging.count_metadata(outs[0])
    assert n == 0 and not gps, f"{n} fields remain"
    return "0 metadata fields left"


SOURCES = ["D-Photo.jpg", "D-Report.pdf", "D-Appendix.pdf", "D-Letter.docx", "D-Graphic.png"]
POS: dict = {}


def main():
    global POS
    log("macOS", sh(["sw_vers", "-productVersion"]).stdout.strip(), "screen", screen_size())
    # clean desktop + sample files
    for p in DESK.glob("D-*"):
        shutil.rmtree(p) if p.is_dir() else p.unlink()
    open_finder_window()
    empty = Image.open(shot("00_empty"))
    make_files(DESK, "D-")
    for p in DESK.glob("D-*"):
        if p.name not in SOURCES:
            p.unlink()
    # wallpaper like the Windows runs
    from dragshift.imaging import gradient
    wall = Path("/tmp/dragshift_wall.png")
    gradient((1920, 1080), ["#ffb36b", "#ff5f6d", "#c2508f", "#7b8cff"], 160).save(wall)
    osa(f'tell application "System Events" to tell every desktop to set picture to "{wall}"')
    osa('tell application "System Events" to set visible of every process whose name is not "Finder" to false')
    sh(["defaults", "write", "com.dragshift.DragShift", "welcomed", "-bool", "true"])
    sh(["defaults", "write", "DragShift", "welcomed", "-string", "true"])
    env = dict(os.environ, DRAGSHIFT_RECORDING="1")
    app = subprocess.Popen([sys.executable, "-u", "-m", "dragshift", "--quiet"], cwd=str(ROOT), env=env,
                           stdout=open(OUT / "app_stdout.txt", "w"), stderr=subprocess.STDOUT)
    time.sleep(6)
    POS = arrange(SOURCES)
    full = Image.open(shot("00_desktop"))
    calibrate(POS, empty, full)
    # debug image: where we will grab each icon
    dbg = full.convert("RGB")
    d = ImageDraw.Draw(dbg)
    k = dbg.width / screen_size()[0]
    for n in POS:
        cx, cy = icon_center(n, POS)
        d.ellipse((cx * k - 8, cy * k - 8, cx * k + 8, cy * k + 8), outline=(0, 255, 0), width=3)
    dbg.save(OUT / "shots" / "00_targets.png")

    # screen recording (needs the runner's Screen Recording grant; skipped if unavailable)
    rec = None
    try:
        import imageio_ffmpeg
        rec = subprocess.Popen([imageio_ffmpeg.get_ffmpeg_exe(), "-y", "-f", "avfoundation", "-framerate", "30",
                                "-capture_cursor", "1", "-i", "Capture screen 0:none", "-c:v", "libx264", "-preset", "veryfast",
                                "-crf", "23", "-pix_fmt", "yuv420p", str(OUT / "e2e_mac.mp4")],
                               stdin=subprocess.PIPE, stdout=open(OUT / "ffmpeg.txt", "w"), stderr=subprocess.STDOUT)
        time.sleep(2)
    except Exception as e:
        log("no recording:", e)
    try:
        run("JPG → PNG", ["D-Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png"))
        run("JPG → WEBP", ["D-Photo.jpg"], "Convert", "WEBP", v_format("WEBP", ".webp"))
        run("JPG → HEIC", ["D-Photo.jpg"], "Convert", "HEIC", v_format("HEIC", ".heic"))
        run("JPG → PDF", ["D-Photo.jpg"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("PNG → JPG", ["D-Graphic.png"], "Convert", "JPG", v_format("JPEG", ".jpg"))
        run("PDF → JPG (3 pages)", ["D-Report.pdf"], "Convert", "JPG", v_pages_to_images(3, ".jpg"))
        run("PDF → TXT", ["D-Report.pdf"], "Convert", "TXT", v_format("TXT", ".txt"))
        run("DOCX → PDF", ["D-Letter.docx"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("PDF Split", ["D-Report.pdf"], "Tools", "Split", v_pdf_n(3))
        run("PDF Rotate", ["D-Report.pdf"], "Tools", "Rotate", v_rotated)
        run("PDF Compress", ["D-Report.pdf"], "Tools", "Compress", v_pdf_compress)
        run("Merge 2 PDFs", ["D-Appendix.pdf", "D-Report.pdf"], "Tools", "Merge", v_merged, enter_after(), multi=True)
        run("Compress (window, Enter)", ["D-Photo.jpg"], "Tools", "Compress", v_smaller, enter_after())
        run("Resize 50% (window, Enter)", ["D-Photo.jpg"], "Tools", "Resize",
            v_dims(lambda a, b: b == (a[0] // 2, a[1] // 2)), enter_after())
        run("Add BG (window, Enter)", ["D-Graphic.png"], "Tools", "Add BG",
            v_dims(lambda a, b: b[0] > a[0] and b[1] > a[1]), enter_after(2.5))
        run("Metadata: remove all (Enter)", ["D-Photo.jpg"], "Tools", "Metadata", v_no_meta, enter_after())
    except Exception:
        log("ABORTED\n" + traceback.format_exc())
    finally:
        if rec:
            try:
                rec.stdin.write(b"q")
                rec.stdin.flush()
                rec.wait(timeout=60)
            except Exception:
                rec.send_signal(signal.SIGINT)
        app.terminate()
        logp = Path.home() / "Library" / "Logs" / "DragShift" / "log.txt"
        if logp.exists():
            shutil.copy(logp, OUT / "app_log.txt")
        if True:
            txt = (logp.read_text(errors="replace") if logp.exists() else "") + \
                (OUT / "app_stdout.txt").read_text(errors="replace")
            ok = "from the drag pasteboard" in txt
            RESULTS.append({"name": "Slices filled before the mouse moves", "where": "mac desktop",
                            "status": "PASS" if ok else "FAIL",
                            "detail": "files read from the drag pasteboard" if ok else "pasteboard not read",
                            "seconds": 0})
        passed = sum(r["status"] == "PASS" for r in RESULTS)
        (OUT / "results.json").write_text(json.dumps(RESULTS, indent=1))
        lines = [f"# DragShift macOS end-to-end — {passed}/{len(RESULTS)} passed", "",
                 "| # | Scenario | Result | Detail | Time |", "|---|---|---|---|---|"]
        for i, r in enumerate(RESULTS, 1):
            lines.append(f"| {i} | {r['name']} | {'✅' if r['status'] == 'PASS' else '❌'} | {r['detail']} | {r['seconds']}s |")
        (OUT / "results.md").write_text("\n".join(lines), encoding="utf-8")
        log(f"DONE {passed}/{len(RESULTS)}")


if __name__ == "__main__":
    main()
