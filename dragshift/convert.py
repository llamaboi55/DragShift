"""Format conversions. Every function takes a source path and returns a list of
output paths written next to the source."""
from __future__ import annotations

import html
import io
import sys
from pathlib import Path

from PIL import Image, ImageOps

from .util import UserError, sibling

try:  # HEIC / HEIF support
    import pillow_heif
    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

try:
    import pymupdf as fitz
except ImportError:  # pragma: no cover
    import fitz  # type: ignore

IMAGE_TARGETS = ["PNG", "JPG", "WEBP", "HEIC", "TIFF", "AVIF", "BMP", "PDF", "ICO", "GIF"]
PDF_TARGETS = ["JPG", "PNG", "DOCX", "TXT"]
DOCX_TARGETS = ["PDF", "TXT"]
TEXT_TARGETS = ["PDF", "DOCX"]

PIL_FORMAT = {"PNG": "PNG", "JPG": "JPEG", "WEBP": "WEBP", "HEIC": "HEIF", "TIFF": "TIFF",
              "AVIF": "AVIF", "BMP": "BMP", "PDF": "PDF", "ICO": "ICO", "GIF": "GIF"}
NO_ALPHA = {"JPG", "PDF", "BMP"}


# ---------------------------------------------------------------- images
# Files are ones the user picked on their own disk, so Pillow's anti-"decompression bomb" cap
# (179 MP) only gets in the way: 200 MP phone photos and stitched panoramas are normal.
Image.MAX_IMAGE_PIXELS = 1_000_000_000

ANIM_FORMATS = {"GIF", "WEBP", "PNG", "AVIF"}   # sources whose extra frames are an animation
ANIM_TARGETS = {"GIF", "WEBP", "PNG", "AVIF"}   # targets that can store one
ICC_TARGETS = {"JPG", "PNG", "WEBP", "TIFF", "AVIF", "HEIC"}
EXIF_TARGETS = {"JPG", "PNG", "WEBP", "TIFF", "AVIF", "HEIC"}
LOSSY_SOURCES = {"JPEG", "MPO", "HEIF", "AVIF", "WEBP"}
# TIFF image-layout tags (size, compression, strips/tiles, samples...): they show up in a TIFF's
# getexif() but describe that file's pixel layout, so they must not be copied to another file
TIFF_STRUCTURE = {254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277, 278, 279, 280, 281, 284,
                  288, 289, 290, 291, 292, 293, 296, 317, 320, 322, 323, 324, 325, 338, 339, 340, 341,
                  347, 530, 531, 532, 32997, 32998, 33421, 33422}


def _to_8bit(im: Image.Image) -> Image.Image:
    """16/32-bit greyscale (scanners, Lightroom/Photoshop exports) → 8-bit L, scaled, not clipped."""
    import numpy as np
    a = np.asarray(im)
    if im.mode == "F":
        a = a * 255 if a.max() <= 1.0 else a
    elif im.mode.startswith("I;16") or a.max() > 255:
        a = a.astype("float64") / 257
    out = Image.fromarray(np.clip(np.rint(a), 0, 255).astype("uint8"), "L")
    out.info = dict(im.info)
    return out


def normalize(im: Image.Image) -> Image.Image:
    """Any decoded frame → RGB, RGBA or L, keeping the colour profile and EXIF in .info."""
    info = dict(im.info)
    if im.mode in ("I", "I;16", "I;16L", "I;16B", "I;16N", "F"):
        return _to_8bit(im)
    if im.mode == "CMYK":
        info.pop("icc_profile", None)  # it describes the CMYK data, not our RGB
        icc = im.info.get("icc_profile")
        out = None
        if icc:
            try:
                from PIL import ImageCms
                out = ImageCms.profileToProfile(im, ImageCms.ImageCmsProfile(io.BytesIO(icc)),
                                                ImageCms.createProfile("sRGB"), outputMode="RGB")
            except Exception:
                out = None
        out = out or im.convert("RGB")
        out.info = info
        return out
    if im.mode in ("P", "LA", "PA", "La", "RGBa"):
        out = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA", "L"):
        out = im.convert("RGB")
    else:
        return im
    out.info = info
    return out


def open_image(path: str | Path) -> Image.Image:
    im = Image.open(path)
    im.seek(0)
    im = ImageOps.exif_transpose(im)  # honour camera rotation
    return normalize(im)


def open_preview(path: str | Path, max_side: int = 1400) -> Image.Image:
    """Fast, reduced-size decode for on-screen previews (JPEG decodes at 1/2, 1/4, 1/8 scale)."""
    im = Image.open(path)
    im.seek(0)
    if im.format == "JPEG":
        im.draft("RGB", (max_side, max_side))
    im = normalize(ImageOps.exif_transpose(im))
    if max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    return im


def open_pdf(path: str | Path):
    doc = fitz.open(path)
    if doc.needs_pass:
        doc.close()
        raise UserError("This PDF is password-protected. Remove the password first.")
    return doc


def flatten(im: Image.Image, bg=(255, 255, 255)) -> Image.Image:
    if im.mode == "RGBA":
        base = Image.new("RGB", im.size, bg)
        base.paste(im, mask=im.split()[3])
        base.info = dict(im.info)
        return base
    return im.convert("RGB") if im.mode != "RGB" else im


def _profile(icc: bytes):
    from PIL import ImageCms
    return ImageCms.ImageCmsProfile(io.BytesIO(icc))


def icc_fits(icc: bytes | None, mode: str) -> bool:
    """An RGB profile on an L image (or the reverse) makes a broken file; only keep matching ones."""
    if not icc:
        return False
    try:
        space = _profile(icc).profile.xcolor_space.strip()
    except Exception:
        return False
    return space == ("GRAY" if mode == "L" else "RGB") and mode in ("L", "RGB", "RGBA", "P")


def is_srgb(icc: bytes | None) -> bool:
    if not icc:
        return True
    try:
        return "srgb" in _profile(icc).profile.profile_description.lower()
    except Exception:
        return True


def to_srgb(im: Image.Image) -> Image.Image:
    """For formats that can't carry a profile (GIF, BMP, ICO): convert wide-gamut pixels so a
    Display P3 photo doesn't come out dull."""
    icc = im.info.get("icc_profile")
    if is_srgb(icc) or im.mode not in ("RGB", "RGBA") or not icc_fits(icc, im.mode):
        return im
    from PIL import ImageCms
    try:
        out = ImageCms.profileToProfile(im, _profile(icc), ImageCms.createProfile("sRGB"),
                                        outputMode=im.mode)
    except Exception:
        return im
    out.info = {k: v for k, v in im.info.items() if k != "icc_profile"}
    return out


def clean_exif(im_or_exif, drop_gps: bool = False) -> bytes | None:
    """EXIF to write with already-rotated pixels: orientation reset to 1 (or it rotates twice)."""
    ex = im_or_exif.getexif() if isinstance(im_or_exif, Image.Image) else im_or_exif
    if not ex:
        return None
    try:  # work on a copy: getexif() is the source image's own object
        copy = Image.Exif()
        copy.load(ex.tobytes())
        ex = copy
    except Exception:
        return None
    for tag in TIFF_STRUCTURE & set(ex):  # a TIFF's own layout tags aren't metadata
        del ex[tag]
    if 0x0112 in ex:
        ex[0x0112] = 1
    if drop_gps and 0x8825 in ex:
        del ex[0x8825]
    try:
        return ex.tobytes()
    except Exception:
        return None


def source_meta(src: str | Path) -> tuple[bytes | None, bytes | None]:
    """(colour profile, EXIF) of a source file, for saving an edited copy of it."""
    try:
        with Image.open(src) as im:
            icc = None if im.mode == "CMYK" else im.info.get("icc_profile")
            return icc, clean_exif(im)
    except Exception:
        return None, None


def _save_kwargs(target: str, quality: int) -> dict:
    kw: dict = {}
    if target in ("JPG", "WEBP", "AVIF", "HEIC"):
        kw["quality"] = quality
    if target == "JPG":
        # full-resolution colour at high quality: 4:2:0 smears red/blue edges and text
        kw.update(optimize=True, progressive=True, subsampling=0 if quality >= 90 else 2)
    if target == "WEBP":
        kw["method"] = 5
    if target == "PNG":
        kw["optimize"] = True
    return kw


def save_image(im: Image.Image, out, target: str, quality: int | None = None, exif=None,
               icc=None, keep_meta: bool = True) -> None:
    """Save one image. With keep_meta, the colour profile and EXIF ride along (from `icc`/`exif`
    or the image's own .info), so photos keep their colours, date taken and camera info."""
    from .util import CONFIG
    quality = quality or int(CONFIG.get("jpg_quality", 90))
    target = target.upper()
    fmt = PIL_FORMAT[target]
    if keep_meta:
        icc = icc if icc is not None else im.info.get("icc_profile")
        exif = exif if exif is not None else clean_exif(im)
    if target not in ICC_TARGETS and target != "PDF":
        im = to_srgb(im) if icc is None else to_srgb(_with_icc(im, icc))
        icc = None
    if target in NO_ALPHA:
        im = flatten(im)
    kw = _save_kwargs(target, quality)
    if target == "ICO":
        side = min(256, max(im.size))
        im = ImageOps.contain(im.convert("RGBA"), (side, side))
        kw["sizes"] = [(s, s) for s in (16, 24, 32, 48, 64, 128, 256) if s <= side]
    if target == "PDF":
        atomic_save(lambda dst: images_pdf([_with_icc(im, icc)], dst), out)
        return
    if target in ICC_TARGETS and icc_fits(icc, im.mode):
        kw["icc_profile"] = icc
    if exif and target in EXIF_TARGETS:
        kw["exif"] = exif
    atomic_save(lambda dst: im.save(dst, fmt, **kw), out)


def _with_icc(im: Image.Image, icc: bytes | None) -> Image.Image:
    if icc and im.info.get("icc_profile") != icc:
        im = im.copy()
        im.info["icc_profile"] = icc
    return im


def atomic_save(write, out):
    """Write to a hidden temp file, then rename: Explorer never shows a half-written file."""
    import os
    if not isinstance(out, (str, Path)):
        write(out)  # in-memory buffer
        return
    out = Path(out)
    tmp = out.with_name(f".~{out.name}.part")
    try:
        write(tmp)
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _frames(src: str | Path):
    """(kind, frames): kind 'anim' (GIF/WebP/APNG/AVIF animation), 'pages' (multi-page TIFF)
    or 'single'. Frames are normalised and rotated; animation frames carry their duration."""
    from PIL import ImageSequence
    im = Image.open(src)
    n = getattr(im, "n_frames", 1)
    if n > 1 and im.format in ANIM_FORMATS and getattr(im, "is_animated", False):
        frames = []
        for f in ImageSequence.Iterator(im):
            fr = normalize(f.convert("RGBA") if f.mode in ("P", "PA", "LA", "L") else f.copy())
            fr.info["duration"] = f.info.get("duration", 100)
            frames.append(fr)
        if any(f.mode == "RGBA" for f in frames):  # encoders need every frame in one mode
            frames = [_keep_info(f, f.convert("RGBA")) if f.mode != "RGBA" else f for f in frames]
        elif any(f.mode != "RGB" for f in frames):
            frames = [_keep_info(f, f.convert("RGB")) if f.mode != "RGB" else f for f in frames]
        frames[0].info.setdefault("loop", im.info.get("loop", 0))
        return "anim", frames
    if n > 1 and im.format == "TIFF":
        return "pages", [normalize(ImageOps.exif_transpose(f.copy())) for f in ImageSequence.Iterator(im)]
    return "single", [open_image(src)]


def _keep_info(src: Image.Image, out: Image.Image) -> Image.Image:
    out.info = dict(src.info)
    return out


def _is_lossless_webp(path) -> bool:
    """WebP can be either; lossless files use VP8L chunks."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096)
    except OSError:
        return False
    return head[12:16] == b"VP8L" or (head[12:16] == b"VP8X" and b"VP8L" in head and b"VP8 " not in head)


def _save_animation(frames: list[Image.Image], out: Path, target: str) -> None:
    from .util import CONFIG
    fmt = PIL_FORMAT[target]
    durations = [int(f.info.get("duration", 100)) for f in frames]
    kw: dict = dict(save_all=True, append_images=frames[1:], duration=durations,
                    loop=int(frames[0].info.get("loop", 0)))
    if target in ("WEBP", "AVIF"):
        kw["quality"] = int(CONFIG.get("jpg_quality", 90))
    if target == "GIF":
        kw["disposal"] = 2
        frames = [to_srgb(f) for f in frames]
        kw["append_images"] = frames[1:]
    elif icc_fits(frames[0].info.get("icc_profile"), frames[0].mode):
        kw["icc_profile"] = frames[0].info["icc_profile"]
    atomic_save(lambda dst: frames[0].save(dst, fmt, **kw), out)


def image_to(src: str | Path, target: str) -> list[Path]:
    target = target.upper()
    if target == "PDF":
        out = sibling(src, ext=".pdf")
        atomic_save(lambda dst: images_pdf([src], dst), out)
        return [out]
    kind, frames = _frames(src)
    if kind == "anim" and target in ANIM_TARGETS:
        out = sibling(src, ext="." + target.lower())
        _save_animation(frames, out, target)
        return [out]
    if kind == "pages" and target != "TIFF":
        outs = []
        for i, f in enumerate(frames):
            out = sibling(src, f"page {i + 1}", "." + target.lower())
            save_image(f, out, target)
            outs.append(out)
        return outs
    out = sibling(src, ext="." + target.lower())
    if kind == "pages":  # TIFF → TIFF never happens (same format isn't offered); keep pages anyway
        atomic_save(lambda dst: frames[0].save(dst, "TIFF", save_all=True, append_images=frames[1:]), out)
    else:
        save_image(frames[0], out, target)
    return [out]


def _pdf_image_stream(im: Image.Image, lossy_source: bool) -> bytes:
    """Encode one page image for a PDF: JPEG q95 for photos that were lossy already, else
    lossless PNG (Flate). The colour profile is embedded and MuPDF writes it as ICCBased."""
    icc = im.info.get("icc_profile")
    im = flatten(im) if im.mode == "RGBA" else im
    if im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    kw = {"icc_profile": icc} if icc_fits(icc, im.mode) else {}
    buf = io.BytesIO()
    if lossy_source:
        im.save(buf, "JPEG", quality=95, subsampling=0, **kw)
    else:
        im.save(buf, "PNG", **kw)
    return buf.getvalue()


def _pdf_sources(item) -> list[tuple[Image.Image | None, bytes, tuple[int, int]]]:
    """One (image, stream, pixel size) per page for an image file or an in-memory image.
    JPEGs that need no rotation go in byte-for-byte: zero quality loss."""
    if isinstance(item, Image.Image):
        return [(item, _pdf_image_stream(item, False), item.size)]
    with Image.open(item) as probe:
        fmt, mode, n = probe.format, probe.mode, getattr(probe, "n_frames", 1)
        orient = probe.getexif().get(0x0112, 1)
        size = probe.size
    if fmt in ("JPEG", "MPO") and mode in ("RGB", "L") and orient in (1, None):
        raw = Path(item).read_bytes()
        if fmt == "MPO":  # keep only the primary picture (drop depth / HDR gain-map images)
            raw = raw[:raw.find(b"\xff\xd9", _jpeg_scan_start(raw)) + 2]
        return [(None, raw, size)]
    kind, frames = _frames(item)
    if kind == "anim":
        frames = frames[:1]
    lossy = fmt in LOSSY_SOURCES and not (fmt == "WEBP" and _is_lossless_webp(item))
    return [(f, _pdf_image_stream(f, lossy), f.size) for f in frames]


def _jpeg_scan_start(data: bytes) -> int:
    """Offset of the first SOS marker (where entropy-coded data begins)."""
    i = 2
    while i + 4 <= len(data):
        if data[i] != 0xFF:
            raise ValueError("not a JPEG")
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xDA:
            return i
        if 0xD0 <= m <= 0xD7 or m == 0x01:
            i += 2
            continue
        i += 2 + int.from_bytes(data[i + 2:i + 4], "big")
    raise ValueError("no image data")


def images_pdf(items: list, out) -> None:
    """Build a PDF with one page per image (files or PIL images), page size = pixels at 150 dpi."""
    doc = fitz.open()
    for item in items:
        for _, stream, (w, h) in _pdf_sources(item):
            page = doc.new_page(width=w * 72 / 150, height=h * 72 / 150)
            page.insert_image(page.rect, stream=stream)
    if not doc.page_count:
        raise UserError("Nothing to put in the PDF.")
    doc.save(out, garbage=3, deflate=True)
    doc.close()


def images_to_single_pdf(srcs: list[str | Path]) -> list[Path]:
    out = sibling(srcs[0], "Combined", ".pdf")
    atomic_save(lambda dst: images_pdf(list(srcs), dst), out)
    return [out]


# ---------------------------------------------------------------- PDF
def pdf_to_images(src: str | Path, target: str, dpi: int | None = None) -> list[Path]:
    from .util import CONFIG
    dpi = dpi or int(CONFIG.get("pdf_dpi", 200))
    target = target.upper()
    outs = []
    with open_pdf(src) as doc:
        n = doc.page_count
        for i, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi, alpha=False)
            im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            label = f"page {i + 1}" if n > 1 else ""
            out = sibling(src, label, "." + target.lower())
            save_image(im, out, target)
            outs.append(out)
    return outs


def pdf_to_txt(src: str | Path) -> list[Path]:
    with open_pdf(src) as doc:
        text = "\n\f\n".join(page.get_text("text") for page in doc)
    out = sibling(src, ext=".txt")
    out.write_text(text, encoding="utf-8")
    return [out]


def pdf_to_docx(src: str | Path) -> list[Path]:
    """Layout-aware conversion (pdf2docx: tables, columns, fonts, images, page setup); falls
    back to plain paragraphs + images if that fails on an unusual PDF."""
    with open_pdf(src):
        pass  # password check with a friendly message
    out = sibling(src, ext=".docx")
    try:
        import logging
        from pdf2docx import Converter

        def write(dst):
            logging.disable(logging.WARNING)  # pdf2docx logs every step to the root logger
            try:
                cv = Converter(str(src))
                try:
                    cv.convert(str(dst), multi_processing=False)
                finally:
                    cv.close()
            finally:
                logging.disable(logging.NOTSET)
        atomic_save(write, out)
        _check_docx(out)
        return [out]
    except UserError:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        if out.exists():
            out.unlink()
    return _pdf_to_docx_basic(src, out)


def _check_docx(path: Path) -> None:
    from docx import Document
    Document(path)  # raises if pdf2docx wrote something Word can't open


def _pdf_to_docx_basic(src: str | Path, out: Path) -> list[Path]:
    """Text blocks become paragraphs, embedded images are kept, one Word page per PDF page."""
    from docx import Document
    from docx.enum.text import WD_BREAK
    from docx.shared import Inches

    d = Document()
    with open_pdf(src) as doc:
        for pno, page in enumerate(doc):
            if pno:
                d.add_paragraph().add_run().add_break(WD_BREAK.PAGE)
            items = []
            for b in page.get_text("dict")["blocks"]:
                y = b["bbox"][1]
                if b.get("type") == 0:
                    lines = []
                    size = 11.0
                    bold = False
                    for ln in b["lines"]:
                        spans = ln["spans"]
                        if spans:
                            size = max(size, max(s["size"] for s in spans)) if not lines else size
                            bold = bold or any("Bold" in s["font"] for s in spans)
                        lines.append("".join(s["text"] for s in spans))
                    txt = " ".join(l.strip() for l in lines).strip()
                    if txt:
                        items.append((y, "text", (txt, size, bold)))
                elif b.get("type") == 1 and b.get("image"):
                    items.append((y, "img", (b["image"], b["bbox"])))
            items.sort(key=lambda t: t[0])
            for _, kind, data in items:
                if kind == "text":
                    txt, size, bold = data
                    if size >= 16:
                        d.add_heading(txt, level=1 if size >= 20 else 2)
                    else:
                        p = d.add_paragraph()
                        r = p.add_run(txt)
                        r.bold = bold and len(txt) < 120
                else:
                    raw, bbox = data
                    width_in = min(6.5, max(0.5, (bbox[2] - bbox[0]) / 72))
                    try:
                        d.add_picture(io.BytesIO(raw), width=Inches(width_in))
                    except Exception:
                        pass
    atomic_save(lambda dst: d.save(dst), out)
    return [out]


# ---------------------------------------------------------------- DOCX
def paper() -> str:
    """'letter' in the US/Canada/Mexico/Philippines/most of Latin America, else 'a4'."""
    try:
        from PySide6.QtCore import QLocale
        T = QLocale.Territory
        letter = {T.UnitedStates, T.Canada, T.Mexico, T.Philippines, T.Chile, T.Colombia,
                  T.Venezuela, T.CostaRica, T.Guatemala, T.ElSalvador, T.Panama,
                  T.DominicanRepublic, T.PuertoRico, T.Nicaragua, T.Honduras, T.Bolivia}
        return "letter" if QLocale.system().territory() in letter else "a4"
    except Exception:
        return "a4"


def _word_to_pdf(src: Path, out: Path) -> bool:
    """Use Microsoft Word on Windows (if installed) for a perfect-fidelity export."""
    if sys.platform != "win32":
        return False
    try:
        import win32com.client  # type: ignore
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        try:
            doc = word.Documents.Open(str(src.resolve()), ReadOnly=True)
            doc.SaveAs(str(out.resolve()), FileFormat=17)  # wdFormatPDF
            doc.Close(False)
        finally:
            word.Quit()
        return out.exists()
    except Exception:
        return False


PAGES_SCRIPT = '''
on run argv
    set src to POSIX file (item 1 of argv)
    set dst to POSIX file (item 2 of argv)
    set wasRunning to application "Pages" is running
    try
        with timeout of 90 seconds
            tell application "Pages"
                set d to open src
                export d to dst as PDF
                close d saving no
            end tell
        end timeout
    on error msg number n
        if not wasRunning then
            with timeout of 10 seconds
                tell application "Pages" to quit saving no
            end timeout
        end if
        error msg number n
    end try
    if not wasRunning then tell application "Pages" to quit
end run
'''
PAGES_ID = "com.apple.iWork.Pages"


def automation_permission(bundle_id: str, ask: bool) -> int:
    """macOS Automation (Apple Events) permission for controlling another app, without sending it
    anything: 0 = allowed, -1743 = the user said no, -1744 = macOS would ask. With ask=True macOS
    shows its one-time prompt and this blocks until it's answered."""
    import ctypes

    class AEDesc(ctypes.Structure):
        _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]
    try:
        cs = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
        fcc = lambda s: int.from_bytes(s.encode(), "big")  # noqa: E731
        cs.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(AEDesc)]
        cs.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(AEDesc), ctypes.c_uint32,
                                                             ctypes.c_uint32, ctypes.c_bool]
        cs.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
        desc, bid = AEDesc(), bundle_id.encode()
        if cs.AECreateDesc(fcc("bund"), bid, len(bid), ctypes.byref(desc)) != 0:
            return 0
        try:
            return int(cs.AEDeterminePermissionToAutomateTarget(ctypes.byref(desc), fcc("****"),
                                                                fcc("****"), ask))
        finally:
            cs.AEDisposeDesc(ctypes.byref(desc))
    except Exception:
        return 0  # can't tell: just try


def _pages_to_pdf(src: Path, out: Path) -> bool:
    """macOS: let Pages lay the document out (real fonts, images, headers, page setup)."""
    if sys.platform != "darwin" or not Path("/Applications/Pages.app").exists():
        return False
    status = automation_permission(PAGES_ID, ask=False)
    if status == -1743:
        return False  # declined before: use the built-in renderer straight away, no waiting
    if status != 0 and automation_permission(PAGES_ID, ask=True) != 0:
        return False
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d) / "out.pdf"
        try:
            r = subprocess.run(["osascript", "-e", PAGES_SCRIPT, str(src.resolve()), str(tmp)],
                               capture_output=True, text=True, timeout=150)
        except Exception:
            return False
        if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            if r.stderr:
                print("Pages export failed:", r.stderr.strip())
            return False
        import shutil
        atomic_save(lambda dst: shutil.copyfile(tmp, dst), out)
    return True


def _soffice() -> str | None:
    import shutil
    for c in ("soffice", "libreoffice"):
        if shutil.which(c):
            return shutil.which(c)
    for c in ("/Applications/LibreOffice.app/Contents/MacOS/soffice",
              r"C:\Program Files\LibreOffice\program\soffice.exe"):
        if Path(c).exists():
            return c
    return None


def _libreoffice_to_pdf(src: Path, out: Path) -> bool:
    exe = _soffice()
    if not exe:
        return False
    import shutil
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        try:
            subprocess.run([exe, f"-env:UserInstallation=file://{Path(d).as_posix()}/profile",
                            "--headless", "--convert-to", "pdf", "--outdir", d, str(src.resolve())],
                           capture_output=True, timeout=180)
        except Exception:
            return False
        tmp = Path(d) / (src.stem + ".pdf")
        if not tmp.exists() or tmp.stat().st_size == 0:
            return False
        atomic_save(lambda dst: shutil.copyfile(tmp, dst), out)
    return True


def office_engine() -> str:
    """Which renderer Word → PDF will use here (shown in tests / logs)."""
    if sys.platform == "win32":
        return "word-or-builtin"
    if sys.platform == "darwin" and Path("/Applications/Pages.app").exists() \
            and automation_permission(PAGES_ID, ask=False) != -1743:
        return "pages"
    return "libreoffice" if _soffice() else "builtin"


def _para_style(p) -> str:
    css = []
    try:
        al = p.alignment
        if al is not None:
            name = str(al).split(".")[-1].split(" ")[0].lower()
            if name in ("center", "right", "justify"):
                css.append(f"text-align:{name}")
    except Exception:
        pass
    return f' style="{";".join(css)}"' if css else ""


def _run_html(r, images: dict) -> str:
    out = []
    # pictures inside the run (inline images)
    for blip in r._element.xpath(".//a:blip"):
        rid = blip.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed")
        part = r.part.related_parts.get(rid) if rid else None
        if part is None:
            continue
        name = f"img{len(images) + 1}.{(part.partname.ext or 'png').lower()}"
        images[name] = part.blob
        width = ""
        ext = r._element.xpath(".//wp:extent")
        if ext:
            width = f' width="{int(ext[0].get("cx")) / 12700:.0f}"'  # EMU → pt
        out.append(f'<img src="{name}"{width}/>')
    t = html.escape(r.text)
    if t:
        style = []
        try:
            if r.font.size:
                style.append(f"font-size:{r.font.size.pt:g}pt")
            if r.font.color is not None and r.font.color.rgb is not None:
                style.append(f"color:#{r.font.color.rgb}")
        except Exception:
            pass
        if style:
            t = f'<span style="{";".join(style)}">{t}</span>'
        if r.bold:
            t = f"<b>{t}</b>"
        if r.italic:
            t = f"<i>{t}</i>"
        if r.underline:
            t = f"<u>{t}</u>"
        out.append(t)
    return "".join(out)


def _list_kind(p, d) -> str | None:
    """'ol' / 'ul' for list paragraphs (Word numbering or a List style), else None."""
    style = (p.style.name or "").lower() if p.style is not None else ""
    numpr = p._p.pPr.numPr if p._p.pPr is not None else None
    if numpr is None or numpr.numId is None:
        if "list number" in style:
            return "ol"
        return "ul" if "list" in style else None
    try:
        num_id = numpr.numId.val
        ilvl = numpr.ilvl.val if numpr.ilvl is not None else 0
        numbering = d.part.numbering_part.element
        ns = numbering.nsmap
        w = "{%s}" % ns["w"]
        abstract = None
        for num in numbering.findall(f"{w}num"):
            if num.get(f"{w}numId") == str(num_id):
                abstract = num.find(f"{w}abstractNumId").get(f"{w}val")
        for an in numbering.findall(f"{w}abstractNum"):
            if an.get(f"{w}abstractNumId") == abstract:
                for lvl in an.findall(f"{w}lvl"):
                    if lvl.get(f"{w}ilvl") == str(ilvl):
                        fmt = lvl.find(f"{w}numFmt")
                        return "ul" if fmt is None or fmt.get(f"{w}val") in ("bullet", "none") else "ol"
    except Exception:
        pass
    return "ul"


def _paragraph_html(p, images: dict) -> str:
    body = "".join(_run_html(r, images) for r in p.runs) or "&nbsp;"
    style = (p.style.name or "").lower() if p.style is not None else ""
    if style.startswith("heading"):
        lvl = "".join(c for c in style if c.isdigit()) or "1"
        lvl = str(min(int(lvl), 6))
        return f"<h{lvl}{_para_style(p)}>{body}</h{lvl}>"
    if style == "title":
        return f"<h1{_para_style(p)}>{body}</h1>"
    return f"<p{_para_style(p)}>{body}</p>"


def _docx_blocks(d):
    """Body paragraphs and tables in document order."""
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    for child in d.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            yield Paragraph(child, d)
        elif tag == "tbl":
            yield Table(child, d)


def docx_to_html(src: str | Path, images: dict | None = None) -> str:
    from docx import Document
    from docx.table import Table

    images = {} if images is None else images
    d = Document(src)
    out: list[str] = []
    open_list = None
    for block in _docx_blocks(d):
        kind = None if isinstance(block, Table) else _list_kind(block, d)
        if kind != open_list:
            if open_list:
                out.append(f"</{open_list}>")
            if kind:
                out.append(f"<{kind}>")
            open_list = kind
        if isinstance(block, Table):
            rows = []
            for row in block.rows:
                cells = "".join(f"<td>{'<br/>'.join(html.escape(p.text) for p in c.paragraphs)}</td>"
                                for c in row.cells)
                rows.append(f"<tr>{cells}</tr>")
            out.append("<table>" + "".join(rows) + "</table>")
        elif kind:
            out.append(f"<li>{''.join(_run_html(r, images) for r in block.runs)}</li>")
        else:
            out.append(_paragraph_html(block, images))
    if open_list:
        out.append(f"</{open_list}>")
    return "\n".join(out)


def docx_to_txt(src: str | Path) -> list[Path]:
    from docx import Document
    from docx.table import Table
    d = Document(src)
    lines = []
    for block in _docx_blocks(d):  # tables stay where they are in the document
        if isinstance(block, Table):
            for row in block.rows:
                lines.append("\t".join(c.text for c in row.cells))
        else:
            lines.append(block.text)
    out = sibling(src, ext=".txt")
    atomic_save(lambda dst: Path(dst).write_text("\n".join(lines), encoding="utf-8"), out)
    return [out]


CSS = """
* { font-family: sans-serif; }
body { font-size: 11pt; line-height: 1.35; }
h1 { font-size: 20pt; } h2 { font-size: 16pt; } h3 { font-size: 13pt; }
p { margin: 0 0 6pt 0; }
pre { font-family: monospace; font-size: 9.5pt; white-space: pre-wrap; }
table { border-collapse: collapse; margin: 6pt 0; }
td { border: 1px solid #888; padding: 3pt 5pt; font-size: 10pt; }
img { max-width: 100%; }
"""


def html_to_pdf(body_html: str, out, images: dict | None = None) -> None:
    archive = fitz.Archive()
    for name, blob in (images or {}).items():
        archive.add(blob, name)
    story = fitz.Story(html=body_html, user_css=CSS, archive=archive)
    writer = fitz.DocumentWriter(str(out))
    mediabox = fitz.paper_rect(paper())
    where = mediabox + (54, 54, -54, -54)
    more = True
    while more:
        dev = writer.begin_page(mediabox)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()


def docx_to_pdf(src: str | Path) -> list[Path]:
    src = Path(src)
    from docx import Document
    try:
        Document(src)  # a broken file fails here, fast, instead of hanging an office app
    except Exception:
        raise UserError("This file isn't a readable Word document (.docx).") from None
    out = sibling(src, ext=".pdf")
    if _word_to_pdf(src, out) or _pages_to_pdf(src, out) or _libreoffice_to_pdf(src, out):
        return [out]
    images: dict = {}
    body = docx_to_html(src, images)
    atomic_save(lambda dst: html_to_pdf(body, dst, images), out)
    return [out]


# ---------------------------------------------------------------- text
def read_text(src: str | Path) -> str:
    """Decode a text file: BOMs first, then UTF-8, BOM-less UTF-16, then Windows-1252."""
    raw = Path(src).read_bytes()
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32"), (b"\x00\x00\xfe\xff", "utf-32"),
                     (b"\xef\xbb\xbf", "utf-8-sig"), (b"\xff\xfe", "utf-16"), (b"\xfe\xff", "utf-16")):
        if raw.startswith(bom):
            return raw.decode(enc, "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    if len(raw) >= 4 and len(raw) % 2 == 0:  # UTF-16 without BOM: NULs in every other byte
        half = len(raw) // 2
        if raw[1::2].count(0) > half * 0.3 and raw[0::2].count(0) < half * 0.05:
            return raw.decode("utf-16-le", "replace")
        if raw[0::2].count(0) > half * 0.3 and raw[1::2].count(0) < half * 0.05:
            return raw.decode("utf-16-be", "replace")
    try:
        return raw.decode("cp1252")
    except UnicodeDecodeError:
        return raw.decode("latin-1")


def _breakable(text: str) -> str:
    """Long runs without spaces (minified JSON, base64, URLs) would run off the page and be cut:
    give them invisible break points so they wrap at the page edge."""
    import re
    return re.sub(r"\S{40,}", lambda m: "\u200b".join(m.group(0)[i:i + 20]
                                                     for i in range(0, len(m.group(0)), 20)), text)


def text_to_pdf(src: str | Path) -> list[Path]:
    out = sibling(src, ext=".pdf")
    body = f"<pre>{html.escape(_breakable(read_text(src)))}</pre>"
    atomic_save(lambda dst: html_to_pdf(body, dst), out)
    return [out]


def text_to_docx(src: str | Path) -> list[Path]:
    from docx import Document
    d = Document()
    for line in read_text(src).splitlines():
        d.add_paragraph(line)
    out = sibling(src, ext=".docx")
    atomic_save(lambda dst: d.save(dst), out)
    return [out]


# ---------------------------------------------------------------- dispatch
def targets_for(cat: str, src_ext: str) -> list[str]:
    src_ext = src_ext.lower().lstrip(".")
    same = {"jpeg": "JPG", "jpg": "JPG", "tif": "TIFF", "tiff": "TIFF", "heif": "HEIC"}
    cur = same.get(src_ext, src_ext.upper())
    if cat == "image":
        t = [x for x in IMAGE_TARGETS if x != cur]
        if not HEIF_OK:
            t = [x for x in t if x != "HEIC"]
        return t
    return {"pdf": PDF_TARGETS, "docx": DOCX_TARGETS, "text": TEXT_TARGETS}.get(cat, [])


def convert(src: str | Path, cat: str, target: str) -> list[Path]:
    target = target.upper()
    if cat == "image":
        return image_to(src, target)
    if cat == "pdf":
        if target in ("JPG", "PNG"):
            return pdf_to_images(src, target)
        if target == "TXT":
            return pdf_to_txt(src)
        if target == "DOCX":
            return pdf_to_docx(src)
    if cat == "docx":
        return docx_to_pdf(src) if target == "PDF" else docx_to_txt(src)
    if cat == "text":
        return text_to_pdf(src) if target == "PDF" else text_to_docx(src)
    raise ValueError(f"Can't convert {cat} to {target}")
