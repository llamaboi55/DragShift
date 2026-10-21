"""End-to-end test on a real Windows desktop.

Every scenario: real mouse drag of a file (from File Explorer or the Desktop) → press Shift →
drop on a slice → (drive the tool window) → verify the output file on disk.
Writes e2e_out/results.json, results.md, log.txt, screenshots and a screen recording."""
from __future__ import annotations

import json
import shutil
import time
import traceback
from pathlib import Path

import pyautogui
from PIL import Image, ImageDraw

import winkit as wk
import dragshift.convert  # noqa: F401  (registers HEIC/AVIF support for the verifiers)

OUT = wk.ROOT / "e2e_out"
OUT.mkdir(exist_ok=True)
(OUT / "shots").mkdir(exist_ok=True)
EXP = Path(r"C:\DragShiftTest")
DESK = wk.desktop_dir()
STASH = Path(r"C:\DragShiftTest_outputs")
RESULTS: list[dict] = []


# ------------------------------------------------------------------ sample files
def make_photo(path: Path, size=(2400, 1600), gps=True):
    import random
    random.seed(3)
    im = Image.new("RGB", size)
    d = ImageDraw.Draw(im)
    for y in range(size[1]):
        t = y / size[1]
        d.line([(0, y), (size[0], y)], fill=(int(255 - 120 * t), int(170 - 90 * t), int(90 + 110 * t)))
    for _ in range(250):
        x, y = random.randrange(size[0]), random.randrange(size[1])
        r = random.randrange(10, 90)
        d.ellipse((x, y, x + r, y + r), fill=tuple(random.randrange(256) for _ in range(3)))
    ex = Image.Exif()
    ex[0x010F] = "Apple"
    ex[0x0110] = "iPhone 15 Pro"
    if gps:
        ex[0x8825] = {1: "N", 2: (24.0, 33.0, 18.0), 3: "W", 4: (81.0, 47.0, 0.0)}
    im.save(path, quality=95, exif=ex)


def make_pdf(path: Path, img: Path, pages=3, title="Report"):
    import pymupdf
    doc = pymupdf.open()
    for i in range(pages):
        pg = doc.new_page()
        pg.insert_text((72, 80), f"{title} — page {i + 1}", fontsize=22)
        for j in range(12):
            pg.insert_text((72, 120 + j * 18), "Lorem ipsum dolor sit amet, consectetur adipiscing.", fontsize=11)
        pg.insert_image(pymupdf.Rect(72, 360, 420, 600), filename=str(img))
    doc.set_metadata({"author": "Jimmy", "title": title})
    doc.save(path)


def make_files(folder: Path, prefix=""):
    folder.mkdir(exist_ok=True)
    make_photo(folder / f"{prefix}Photo.jpg")
    make_photo(folder / f"{prefix}Second.jpg", size=(1200, 900), gps=False)
    Image.open(folder / f"{prefix}Photo.jpg").resize((800, 533)).save(folder / f"{prefix}Graphic.png")
    make_pdf(folder / f"{prefix}Report.pdf", folder / f"{prefix}Second.jpg")
    make_pdf(folder / f"{prefix}Appendix.pdf", folder / f"{prefix}Second.jpg", pages=2, title="Appendix")
    from docx import Document
    d = Document()
    d.add_heading("Letter", 1)
    d.add_paragraph("Hello from the DragShift end-to-end test.")
    d.save(folder / f"{prefix}Letter.docx")
    (folder / f"{prefix}Notes.txt").write_text("Shopping list\n- coffee\n- limes\n", encoding="utf-8")


def snapshot(folder: Path) -> set[str]:
    return {str(p.relative_to(folder)) for p in folder.rglob("*")}


def new_files(folder: Path, before: set[str], timeout=15) -> list[Path]:
    """New files (ignoring in-progress '.~*.part'), once their sizes stop changing."""
    end = time.time() + timeout

    def current():
        now = snapshot(folder) - before
        return {n: (folder / n).stat().st_size for n in now
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


def stash(folder: Path, before: set[str]):
    """Move outputs out of the way so the folder only holds the sources again."""
    STASH.mkdir(exist_ok=True)
    for n in sorted(snapshot(folder) - before, key=len, reverse=True):
        p = folder / n
        try:
            if p.is_file():
                dest = STASH / f"{int(time.time() * 1000)}_{p.name}"
                shutil.move(str(p), dest)
            elif p.is_dir():
                p.rmdir()
        except Exception:
            pass


SOURCES = ["Photo.jpg", "Second.jpg", "Graphic.png", "Report.pdf", "Appendix.pdf", "Letter.docx", "Notes.txt"]


def restore_sources(folder: Path):
    """If an earlier failure let Windows move a file away (Shift-drop without the menu = move),
    put it back so one failure can't cascade into the rest of the run."""
    prefix = "" if folder == EXP else "D-"
    missing = [n for n in SOURCES if not (folder / (prefix + n)).exists()]
    if missing:
        wk.log("restoring sources:", missing)
        for n in missing:
            stray = DESK / (prefix + n) if folder == EXP else None
            if stray is not None and stray.exists():
                stray.unlink()
        make_files(folder, prefix)
        time.sleep(1.0)
    # stray copies of Explorer files dropped on the desktop
    for n in SOURCES:
        if (DESK / n).exists():
            (DESK / n).unlink()


# ------------------------------------------------------------------ verification helpers
def img_ok(p: Path, fmt=None):
    with Image.open(p) as im:
        im.load()
        if fmt:
            assert im.format == fmt, f"{p.name}: format {im.format} != {fmt}"
        return im.size


def pdf_pages(p: Path) -> int:
    import pymupdf
    with pymupdf.open(p) as d:
        return d.page_count


# ------------------------------------------------------------------ menu layout (from the app itself)
def ring_index(paths: list[str], ring: str, label: str) -> tuple[int, int, int]:
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, paths)
    names = list(rings)
    items = rings[ring]
    labels = [i.label for i in items]
    return names.index(ring), labels.index(label), len(items)


# ------------------------------------------------------------------ scenario runner
def safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in name).replace(" ", "_")


def run(name: str, where: str, files: list[str], ring: str, label: str, verify, tool=None, select_extra=None):
    folder = EXP if where == "explorer" else DESK
    srcs = [str(folder / f) for f in files]
    restore_sources(folder)
    before = snapshot(folder)
    t0 = time.time()
    status, msg = "PASS", ""
    try:
        ring_i, idx, n = ring_index(srcs, ring, label)
        container = EX if where == "explorer" else wk.desktop_view()
        if where == "desktop":
            pyautogui.hotkey("win", "d") if not DESKTOP_SHOWN[0] else None
            DESKTOP_SHOWN[0] = True
        else:
            if DESKTOP_SHOWN[0]:
                EX.set_focus()
                DESKTOP_SHOWN[0] = False
                time.sleep(0.6)
        # select extra files first (multi-select), then drag the last one
        if select_extra:
            first = wk.item_center(container, files[0])
            wk.glide(*first, 0.4)
            pyautogui.click()
            for f in files[1:]:
                c = wk.item_center(container, f)
                wk.glide(*c, 0.35)
                pyautogui.keyDown("ctrl")
                pyautogui.click()
                pyautogui.keyUp("ctrl")
            src_xy = wk.item_center(container, files[-1])
        else:
            src_xy = wk.item_center(container, files[0])
        sw, sh = pyautogui.size()
        dy = 260 if src_xy[1] < 300 else (60 if src_xy[1] < sh - 400 else -260)
        nudge = (min(640, sw - src_xy[0] - 260), dy)
        c = wk.drag_open_menu(src_xy, nudge)
        if ring_i == 1:
            wk.switch_ring()
        target = wk.petal_xy(c, n, idx)
        wk.glide(*target, 0.4)
        time.sleep(0.35)
        wk.shot(OUT / "shots" / f"{len(RESULTS) + 1:02d}_{safe(name)}_menu.png")
        wk.drop()
        if tool:
            tool()
        outs = new_files(folder, before)
        if not outs:
            raise AssertionError("no output file appeared")
        note = verify(outs, srcs)
        msg = note or ", ".join(o.name for o in outs[:3])
    except Exception as e:
        status = "FAIL"
        msg = f"{type(e).__name__}: {e}"
        wk.log(traceback.format_exc())
        wk.shot(OUT / "shots" / f"{len(RESULTS) + 1:02d}_FAIL_{safe(name)}.png")
        pyautogui.mouseUp()
        pyautogui.keyUp("shift")
        pyautogui.press("esc")
        time.sleep(0.5)
        # close any stray DragShift window
        try:
            for w in wk.uia().windows(title_re=".*— DragShift"):
                w.close()
        except Exception:
            pass
    dt = time.time() - t0
    RESULTS.append({"name": name, "where": where, "status": status, "detail": msg, "seconds": round(dt, 1),
                    "t": round(time.time() - wk.T0, 1)})
    wk.log(f"{status:4} {where:8} {name:34} {dt:5.1f}s  {msg}")
    stash(folder, before)
    time.sleep(0.6)


# ------------------------------------------------------------------ tool drivers
def tool_win(title_re):
    return wk.find_window(title_re, 12)


def drive_combination():
    w = tool_win("(Merge PDFs|Combine images into PDF).*")
    wk.click_title(w, "Create PDF")


def drive_compress():
    w = tool_win("Compress.*")
    time.sleep(0.5)
    wk.click_title(w, "Target size")
    wk.click_title(w, "200 KB", 0.8)
    btns = [b for b in w.descendants(control_type="Button") if b.window_text().startswith("Compress")]
    r = btns[-1].rectangle()
    wk.glide((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    pyautogui.click()


def drive_resize():
    w = tool_win("Resize.*")
    wk.click_title(w, "50%", 0.5)
    btns = [b for b in w.descendants(control_type="Button") if b.window_text().startswith("Resize")]
    r = btns[-1].rectangle()
    wk.glide((r.left + r.right) // 2, (r.top + r.bottom) // 2)
    pyautogui.click()


def drive_crop():
    w = tool_win("Crop Image.*")
    time.sleep(0.4)
    wk.click_title(w, "1:1", 0.6)
    wk.click_title(w, "Apply")


def drive_edit():
    w = tool_win("Edit.*")
    time.sleep(0.4)
    wk.click_title(w, "✦ Auto", 0.6)
    wk.click_title(w, "⟳", 0.6)
    wk.click_title(w, "Save")


def _canvas(w):
    r = wk.image_area(w)
    if r is None:
        wr = w.rectangle()
        return (wr.left + 60, wr.top + 80, wr.right - 60, wr.top + int(wr.height() * 0.6))
    return (r.left, r.top, r.right, r.bottom)


def drive_annotate():
    w = tool_win("Annotate.*")
    time.sleep(0.6)
    x0, y0, x1, y1 = _canvas(w)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    wk.glide(cx - 150, cy + 80)
    pyautogui.mouseDown()
    wk.glide(cx + 120, cy - 60, 0.5)
    pyautogui.mouseUp()
    wk.click_title(w, "① Step", 0.3)
    wk.glide(cx + 160, cy - 80)
    pyautogui.click()
    time.sleep(0.4)
    wk.click_title(w, "Save")


def drive_redact():
    w = tool_win("Redact.*")
    time.sleep(0.6)
    x0, y0, x1, y1 = _canvas(w)
    cx, cy = (x0 + x1) // 2, (y0 + y1) // 2
    wk.glide(cx - 120, cy - 60)
    pyautogui.mouseDown()
    wk.glide(cx + 120, cy + 60, 0.5)
    pyautogui.mouseUp()
    time.sleep(0.5)
    wk.click_title(w, "Save")


def drive_addbg():
    w = tool_win("Add BG.*")
    time.sleep(0.7)
    wk.click_title(w, "1:1", 0.6)
    wk.click_title(w, "Save with Background")


def drive_meta_location():
    w = tool_win("Metadata.*")
    time.sleep(0.5)
    wk.click_title(w, "Remove location only")


def drive_meta_all():
    w = tool_win("Metadata.*")
    time.sleep(0.5)
    wk.click_title(w, "Remove all metadata")


def drive_protect():
    w = tool_win("Password-protect.*")
    time.sleep(0.4)
    edits = w.descendants(control_type="Edit")
    for e in edits[:2]:
        r = e.rectangle()
        wk.glide((r.left + r.right) // 2, (r.top + r.bottom) // 2, 0.3)
        pyautogui.click()
        pyautogui.typewrite("keywest2026", interval=0.02)
    time.sleep(0.3)
    wk.click_title(w, "Protect")


# ------------------------------------------------------------------ verifiers
def v_format(fmt, ext):
    def v(outs, srcs):
        o = outs[0]
        assert o.suffix.lower() == ext, f"extension {o.suffix}"
        if fmt == "PDF":
            assert pdf_pages(o) >= 1
            return f"{o.name} · {pdf_pages(o)} page(s)"
        if fmt in ("DOCX",):
            from docx import Document
            assert Document(o).paragraphs
            return o.name
        if fmt == "TXT":
            assert o.read_text(encoding="utf-8").strip()
            return o.name
        size = img_ok(o, None if fmt in ("HEIC", "AVIF") else fmt)
        return f"{o.name} · {size[0]}×{size[1]}"
    return v


def v_pages_to_images(n, ext):
    def v(outs, srcs):
        imgs = [o for o in outs if o.suffix.lower() == ext]
        assert len(imgs) == n, f"{len(imgs)} images, expected {n}"
        for o in imgs:
            img_ok(o)
        return f"{n} × {ext}"
    return v


def v_compress(limit):
    def v(outs, srcs):
        o = outs[0]
        s0, s1 = Path(srcs[0]).stat().st_size, o.stat().st_size
        assert s1 <= limit, f"{s1} bytes > {limit}"
        img_ok(o)
        return f"{s0 // 1024} KB → {s1 // 1024} KB ({(1 - s1 / s0) * 100:.0f}% smaller)"
    return v


def v_dims(fn):
    def v(outs, srcs):
        with Image.open(srcs[0]) as a:
            sa = a.size
        sb = img_ok(outs[0])
        assert fn(sa, sb), f"{sa} → {sb}"
        return f"{sa[0]}×{sa[1]} → {sb[0]}×{sb[1]}"
    return v


def v_exists(outs, srcs):
    img_ok(outs[0])
    return outs[0].name


def v_redact(outs, srcs):
    with Image.open(outs[0]) as im:
        im = im.convert("RGB")
        w, h = im.size
        px = im.getpixel((w // 2, h // 2))
    assert sum(px) < 40, f"centre pixel {px} not blacked out"
    return f"{outs[0].name} · centre blacked out"


def v_no_gps_keep_camera(outs, srcs):
    from dragshift import imaging
    s = imaging.metadata_summary(outs[0])
    assert "Location" not in s, "GPS still present"
    assert "Camera" in s, "camera info was removed"
    return f"GPS removed, kept {s['Camera']}"


def v_no_meta(outs, srcs):
    from dragshift import imaging
    n, gps = imaging.count_metadata(outs[0])
    assert n == 0 and not gps, f"{n} fields remain"
    return "0 metadata fields left"


def v_pdf_n(n):
    def v(outs, srcs):
        pdfs = [o for o in outs if o.suffix.lower() == ".pdf"]
        assert len(pdfs) == n, f"{len(pdfs)} PDFs"
        return f"{n} PDFs"
    return v


def v_merged(outs, srcs):
    total = sum(pdf_pages(Path(s)) for s in srcs)
    got = pdf_pages(outs[0])
    assert got == total, f"{got} pages, expected {total}"
    return f"{got} pages"


def v_rotated(outs, srcs):
    import pymupdf
    with pymupdf.open(outs[0]) as d:
        assert all(p.rotation == 90 for p in d)
    return "all pages at 90°"


def v_protected(outs, srcs):
    import pymupdf
    d = pymupdf.open(outs[0])
    assert d.needs_pass and d.authenticate("keywest2026")
    return "AES-256, password works"


def v_pdf_meta(outs, srcs):
    import pymupdf
    with pymupdf.open(outs[0]) as d:
        assert not (d.metadata or {}).get("author")
    return "author/title removed"


def v_pdf_compress(outs, srcs):
    s0, s1 = Path(srcs[0]).stat().st_size, outs[0].stat().st_size
    assert pdf_pages(outs[0]) == pdf_pages(Path(srcs[0]))
    return f"{s0 // 1024} KB → {s1 // 1024} KB"


def v_extract(outs, srcs):
    assert outs and all(o.parent.name.endswith("images") for o in outs)
    for o in outs:
        img_ok(o)
    return f"{len(outs)} image(s) in {outs[0].parent.name}/"


def v_combined(outs, srcs):
    assert pdf_pages(outs[0]) == len(srcs)
    return f"{len(srcs)} images → {pdf_pages(outs[0])}-page PDF"


# ------------------------------------------------------------------ IPC / Send To path
def run_ipc(name: str, file: str, label: str, verify):
    """Launch a second copy with a file (what 'Send to' / right-click does) → menu → click."""
    import subprocess
    import sys
    folder = EXP
    before = snapshot(folder)
    t0 = time.time()
    status, msg = "PASS", ""
    try:
        sw, sh = pyautogui.size()
        wk.glide(int(sw * 0.62), int(sh * 0.45), 0.4)
        c = wk.menu_center(pyautogui.position())
        pyw = Path(sys.executable).with_name("pythonw.exe")
        env = dict(__import__("os").environ, DRAGSHIFT_RECORDING="1")
        subprocess.Popen([str(pyw), "-m", "dragshift", str(folder / file)], cwd=str(wk.ROOT), env=env)
        time.sleep(2.5)  # instant hand-off to the running copy, menu opens
        _, idx, n = ring_index([str(folder / file)], "Convert", label)
        wk.glide(*wk.petal_xy(c, n, idx), 0.4)
        time.sleep(0.3)
        wk.shot(OUT / "shots" / f"{len(RESULTS) + 1:02d}_{safe(name)}_menu.png")
        pyautogui.click()
        outs = new_files(folder, before)
        if not outs:
            raise AssertionError("no output file appeared")
        msg = verify(outs, [str(folder / file)])
    except Exception as e:
        status, msg = "FAIL", f"{type(e).__name__}: {e}"
        wk.log(traceback.format_exc())
        pyautogui.press("esc")
    dt = time.time() - t0
    RESULTS.append({"name": name, "where": "send-to", "status": status, "detail": msg, "seconds": round(dt, 1),
                    "t": round(time.time() - wk.T0, 1)})
    wk.log(f"{status:4} send-to  {name:34} {dt:5.1f}s  {msg}")
    stash(folder, before)


# ------------------------------------------------------------------ main
EX = None
DESKTOP_SHOWN = [False]


def main():
    global EX
    wk.set_log(OUT / "log.txt")
    wk.prepare_desktop()
    for f in DESK.glob("*"):
        if f.is_file() and f.suffix.lower() in (".jpg", ".png", ".pdf", ".docx", ".txt"):
            f.unlink()
    shutil.rmtree(EXP, ignore_errors=True)
    shutil.rmtree(STASH, ignore_errors=True)
    make_files(EXP)
    make_files(DESK, "D-")
    app = wk.start_app()
    time.sleep(5)
    EX = wk.open_explorer(EXP)
    rec = wk.start_recording(OUT / "e2e.mp4")
    time.sleep(1.5)
    wk.T0 = time.time()
    try:
        # ---- conversions from Explorer
        for fmt in ["PNG", "WEBP", "HEIC", "TIFF", "AVIF", "BMP", "PDF", "ICO", "GIF"]:
            ext = {"TIFF": ".tiff", "HEIC": ".heic"}.get(fmt, "." + fmt.lower())
            run(f"JPG → {fmt}", "explorer", ["Photo.jpg"], "Convert", fmt, v_format(fmt, ext))
        run("PNG → JPG", "explorer", ["Graphic.png"], "Convert", "JPG", v_format("JPEG", ".jpg"))
        run("PDF → JPG (3 pages)", "explorer", ["Report.pdf"], "Convert", "JPG", v_pages_to_images(3, ".jpg"))
        run("PDF → PNG (3 pages)", "explorer", ["Report.pdf"], "Convert", "PNG", v_pages_to_images(3, ".png"))
        run("PDF → DOCX", "explorer", ["Report.pdf"], "Convert", "DOCX", v_format("DOCX", ".docx"))
        run("PDF → TXT", "explorer", ["Report.pdf"], "Convert", "TXT", v_format("TXT", ".txt"))
        run("DOCX → PDF", "explorer", ["Letter.docx"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("DOCX → TXT", "explorer", ["Letter.docx"], "Convert", "TXT", v_format("TXT", ".txt"))
        run("TXT → PDF", "explorer", ["Notes.txt"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("TXT → DOCX", "explorer", ["Notes.txt"], "Convert", "DOCX", v_format("DOCX", ".docx"))
        run("2 images → 1 PDF", "explorer", ["Photo.jpg", "Second.jpg"], "Convert", "PDF", v_combined, drive_combination,
            select_extra=True)
        # ---- image tools
        run("Compress to 200 KB", "explorer", ["Photo.jpg"], "Tools", "Compress", v_compress(200_000), drive_compress)
        run("Resize 50%", "explorer", ["Photo.jpg"], "Tools", "Resize",
            v_dims(lambda a, b: b == (a[0] // 2, a[1] // 2)), drive_resize)
        run("Crop 1:1", "explorer", ["Photo.jpg"], "Tools", "Crop", v_dims(lambda a, b: b[0] == b[1] == min(a)), drive_crop)
        run("Edit: auto + rotate", "explorer", ["Photo.jpg"], "Tools", "Edit",
            v_dims(lambda a, b: b == (a[1], a[0])), drive_edit)
        run("Annotate", "explorer", ["Photo.jpg"], "Tools", "Annotate", v_exists, drive_annotate)
        run("Redact", "explorer", ["Photo.jpg"], "Tools", "Redact", v_redact, drive_redact)
        run("Add BG 1:1", "explorer", ["Photo.jpg"], "Tools", "Add BG", v_dims(lambda a, b: b[0] == b[1]), drive_addbg)
        run("Metadata: remove location", "explorer", ["Photo.jpg"], "Tools", "Metadata", v_no_gps_keep_camera,
            drive_meta_location)
        run("Metadata: remove all", "explorer", ["Photo.jpg"], "Tools", "Metadata", v_no_meta, drive_meta_all)
        # ---- PDF tools
        run("PDF Compress", "explorer", ["Report.pdf"], "Tools", "Compress", v_pdf_compress)
        run("PDF Split", "explorer", ["Report.pdf"], "Tools", "Split", v_pdf_n(3))
        run("PDF Rotate", "explorer", ["Report.pdf"], "Tools", "Rotate", v_rotated)
        run("PDF Protect", "explorer", ["Report.pdf"], "Tools", "Protect", v_protected, drive_protect)
        run("PDF Extract images", "explorer", ["Report.pdf"], "Tools", "Extract", v_extract)
        run("PDF Metadata", "explorer", ["Report.pdf"], "Tools", "Metadata", v_pdf_meta, drive_meta_all)
        run("Merge 2 PDFs", "explorer", ["Appendix.pdf", "Report.pdf"], "Tools", "Merge", v_merged, drive_combination,
            select_extra=True)
        # ---- second launch with a file (Send To / right-click path)
        run_ipc("Send To → PNG", "Photo.jpg", "PNG", v_format("PNG", ".png"))
        # ---- from the Desktop
        run("Desktop: JPG → PNG", "desktop", ["D-Photo.jpg"], "Convert", "PNG", v_format("PNG", ".png"))
        run("Desktop: PDF → TXT", "desktop", ["D-Report.pdf"], "Convert", "TXT", v_format("TXT", ".txt"))
        run("Desktop: DOCX → PDF", "desktop", ["D-Letter.docx"], "Convert", "PDF", v_format("PDF", ".pdf"))
        run("Desktop: Compress", "desktop", ["D-Photo.jpg"], "Tools", "Compress", v_compress(200_000), drive_compress)
        run("Desktop: Crop", "desktop", ["D-Photo.jpg"], "Tools", "Crop", v_dims(lambda a, b: b[0] == b[1]), drive_crop)
        run("Desktop: Add BG", "desktop", ["D-Graphic.png"], "Tools", "Add BG", v_dims(lambda a, b: b[0] == b[1]),
            drive_addbg)
        run("Desktop: 2 PDFs merge", "desktop", ["D-Appendix.pdf", "D-Report.pdf"], "Tools", "Merge", v_merged, drive_combination,
            select_extra=True)
    except Exception:
        wk.log("E2E ABORTED\n" + traceback.format_exc())
    finally:
        pyautogui.mouseUp()
        pyautogui.keyUp("shift")
        time.sleep(1)
        wk.stop_recording(rec)
        app.kill()
        wk.copy_app_log(OUT / "app_log.txt")
        try:  # the drag picture must never be baked into the glass
            log_text = (OUT / "app_log.txt").read_text(encoding="utf-8", errors="replace")
            n = log_text.count("drag image(s) during capture")
            RESULTS.append({"name": "Drag picture kept out of the glass", "where": "all",
                            "status": "PASS" if n > 0 else "FAIL",
                            "detail": f"hidden during {n} menu captures", "seconds": 0, "t": 0})
        except Exception as e:
            wk.log("drag image check failed", e)
        passed = sum(r["status"] == "PASS" for r in RESULTS)
        (OUT / "results.json").write_text(json.dumps(RESULTS, indent=1))
        lines = [f"# DragShift end-to-end test — {passed}/{len(RESULTS)} passed", "",
                 "| # | Where | Scenario | Result | Detail | Time |", "|---|---|---|---|---|---|"]
        for i, r in enumerate(RESULTS, 1):
            mark = "✅" if r["status"] == "PASS" else "❌"
            lines.append(f"| {i} | {r['where']} | {r['name']} | {mark} | {r['detail']} | {r['seconds']}s |")
        (OUT / "results.md").write_text("\n".join(lines), encoding="utf-8")
        wk.log(f"DONE {passed}/{len(RESULTS)} passed")


if __name__ == "__main__":
    main()
