"""Pure image/PDF processing used by the tools (no UI here, so it's testable)."""
from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageOps
from PIL.ExifTags import GPSTAGS, TAGS

from .convert import (atomic_save, clean_exif, fitz, flatten, icc_fits, open_image, open_pdf,
                      save_image, source_meta)
from .util import sibling

EXT_TARGET = {".jpg": "JPG", ".jpeg": "JPG", ".png": "PNG", ".webp": "WEBP", ".tif": "TIFF",
              ".tiff": "TIFF", ".bmp": "BMP", ".heic": "HEIC", ".heif": "HEIC",
              ".avif": "AVIF", ".gif": "GIF", ".ico": "ICO"}


def target_for(path: str | Path) -> str:
    return EXT_TARGET.get(Path(path).suffix.lower(), "PNG")


def save_like(im: Image.Image, src: str | Path, label: str, force_ext: str | None = None,
              keep_exif: bool = True) -> Path:
    """Save `im` next to `src` as '<name> <label>.<ext>' in the source's format, with the
    source's colour profile (and EXIF unless keep_exif=False)."""
    ext = force_ext or Path(src).suffix.lower()
    if ext in (".gif", ".ico"):
        ext = ".png"
    out = sibling(src, label, ext)
    icc, exif = source_meta(src)
    save_image(im, out, target_for(out), icc=icc, exif=exif if keep_exif else None, keep_meta=False)
    return out


def _quantize_png(im: Image.Image, quality: int, dst) -> None:
    """Lossy PNG: quantise to a palette (like pngquant), keeps transparency and colour profile."""
    colors = max(16, min(256, int(quality * 2.56)))
    # fast octree: the size search and live preview run this many times (max-coverage is
    # ~2 dB better but 500× slower)
    q = im.convert("RGBA").quantize(colors=colors, method=Image.Quantize.FASTOCTREE,
                                    dither=Image.Dither.FLOYDSTEINBERG)
    icc = im.info.get("icc_profile")
    kw = {"icc_profile": icc} if icc_fits(icc, "RGB") else {}
    q.save(dst, "PNG", optimize=True, **kw)


# ---------------------------------------------------------------- compress
def compress_image(src: str | Path, quality: int = 72, max_side: int | None = None,
                   to_webp: bool = False) -> Path:
    im = open_image(src)
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    ext = ".webp" if to_webp else Path(src).suffix.lower()
    out = sibling(src, "Compressed", ext)
    tgt = target_for(out)
    if tgt == "PNG":
        atomic_save(lambda dst: _quantize_png(im, quality, dst), out)
    elif tgt in ("BMP", "TIFF", "GIF", "ICO"):
        out = sibling(src, "Compressed", ".jpg")
        save_image(im, out, "JPG", quality=quality)
    else:
        save_image(im, out, tgt, quality=quality)
    return out


def compress_pdf(src: str | Path, image_dpi: int = 150, quality: int = 70) -> Path:
    out = sibling(src, "Compressed", ".pdf")
    with open_pdf(src) as doc:
        try:
            doc.rewrite_images(dpi_threshold=image_dpi + 10, dpi_target=image_dpi,
                               quality=quality, lossy=True, lossless=True, bitonal=True,
                               color=True, gray=True, set_to_gray=False)
        except Exception:
            pass  # older PyMuPDF: still get structural compression below
        doc.save(out, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True,
                 clean=True, use_objstms=1)
    return out


def encode_size(im: Image.Image, target: str, quality: int) -> int:
    """Bytes `im` would take saved as `target` at `quality` (PNG = palette-quantised)."""
    buf = io.BytesIO()
    if target == "PNG":
        _quantize_png(im, quality, buf)
    else:
        save_image(im, buf, target, quality=quality)
    return buf.tell()


def lossy_target(src: str | Path, to_webp: bool = False) -> str:
    """Format a compressed copy is written in (BMP/TIFF/GIF/ICO become JPG)."""
    if to_webp:
        return "WEBP"
    t = target_for(src)
    return t if t in ("JPG", "PNG", "WEBP", "AVIF", "HEIC") else "JPG"


def plan_target_size(im: Image.Image, target: str, max_bytes: int) -> tuple[int, int | None]:
    """Find (quality, max_side) that fits under max_bytes: binary-search quality, then shrink."""
    side = None
    work = im
    for _ in range(8):
        lo, hi, best = 30, 92, None
        while lo <= hi:
            q = (lo + hi) // 2
            if encode_size(work, target, q) <= max_bytes:
                best, lo = q, q + 1
            else:
                hi = q - 1
        if best is not None:
            return best, side
        side = int(max(work.size) * 0.8)
        work = im.copy()
        work.thumbnail((side, side), Image.LANCZOS)
        if side < 200:
            return 30, side
    return 30, side


def compress_to(src: str | Path, quality: int, max_side: int | None, target: str) -> Path:
    im = open_image(src)
    if max_side and max(im.size) > max_side:
        im.thumbnail((max_side, max_side), Image.LANCZOS)
    out = sibling(src, "Compressed", "." + ("jpg" if target == "JPG" else target.lower()))
    if target == "PNG":
        atomic_save(lambda dst: _quantize_png(im, quality, dst), out)
    else:
        save_image(im, out, target, quality=quality)
    return out


# ---------------------------------------------------------------- resize
def resize_dims(size: tuple[int, int], mode: str, value: int) -> tuple[int, int]:
    """mode: 'pct' (value %), 'long' (longest side px), 'w' (width px), 'h' (height px)."""
    w, h = size
    if mode == "pct":
        k = value / 100
    elif mode == "long":
        k = value / max(w, h)
    elif mode == "w":
        k = value / w
    else:
        k = value / h
    return max(1, round(w * k)), max(1, round(h * k))


def resize_image(src: str | Path, dims: tuple[int, int]) -> Path:
    im = open_image(src)
    im = im.resize(dims, Image.LANCZOS)
    return save_like(im, src, f"{dims[0]}x{dims[1]}")


# ---------------------------------------------------------------- metadata
def read_image_metadata(src: str | Path) -> dict[str, str]:
    info: dict[str, str] = {}
    with Image.open(src) as im:
        info["Dimensions"] = f"{im.width} × {im.height}"
        info["Format"] = str(im.format)
        info["Mode"] = im.mode
        exif = im.getexif()
        for k, v in exif.items():
            info[TAGS.get(k, str(k))] = _fmt(v)
        try:
            for k, v in exif.get_ifd(0x8769).items():  # Exif sub-IFD (camera settings)
                info[TAGS.get(k, str(k))] = _fmt(v)
            gps = exif.get_ifd(0x8825)
            if gps:
                for k, v in gps.items():
                    info["GPS " + GPSTAGS.get(k, str(k))] = _fmt(v)
        except Exception:
            pass
        for k, v in (im.info or {}).items():
            if k in ("exif", "icc_profile", "xmp"):
                info[k.upper()] = f"{len(v)} bytes" if hasattr(v, "__len__") else "yes"
            elif isinstance(v, (str, int, float)):
                info[k] = str(v)
    return info


def _fmt(v) -> str:
    if isinstance(v, bytes):
        return f"<{len(v)} bytes>"
    s = str(v)
    return s if len(s) < 120 else s[:117] + "…"


def metadata_summary(src: str | Path) -> dict[str, str]:
    """The handful of fields people actually care about, human-readable."""
    out: dict[str, str] = {}
    with Image.open(src) as im:
        out["Size"] = f"{im.width} × {im.height}"
        exif = im.getexif()
        sub = {}
        gps = {}
        try:
            sub = exif.get_ifd(0x8769)
            gps = exif.get_ifd(0x8825)
        except Exception:
            pass
        make, model = str(exif.get(0x010F, "")).strip(), str(exif.get(0x0110, "")).strip()
        if make or model:
            out["Camera"] = model if make and model.startswith(make) else f"{make} {model}".strip()
        taken = sub.get(0x9003) or exif.get(0x0132)
        if taken:
            out["Taken"] = str(taken)
        if exif.get(0x0131):
            out["Software"] = str(exif.get(0x0131))
        if sub.get(0x829A):
            t = float(sub[0x829A])
            out["Exposure"] = f"1/{round(1 / t)} s" if 0 < t < 1 else f"{t:g} s"
        if sub.get(0x829D):
            out["Aperture"] = f"f/{float(sub[0x829D]):.1f}"
        if sub.get(0x8827):
            out["ISO"] = str(sub[0x8827])
        loc = gps_decimal(gps)
        if loc:
            out["Location"] = f"{loc[0]:.5f}, {loc[1]:.5f}"
    return out


def gps_decimal(gps) -> tuple[float, float] | None:
    try:
        def conv(v, ref):
            d, m, s_ = (float(x) for x in v)
            val = d + m / 60 + s_ / 3600
            return -val if ref in ("S", "W") else val
        return conv(gps[2], gps[1]), conv(gps[4], gps[3])
    except Exception:
        return None


def count_metadata(src: str | Path) -> tuple[int, bool]:
    """(number of EXIF/XMP/ICC entries, has GPS)."""
    with Image.open(src) as im:
        exif = im.getexif()
        n = len(exif)
        has_gps = False
        try:
            n += len(exif.get_ifd(0x8769))
            g = exif.get_ifd(0x8825)
            has_gps = bool(g)
            n += len(g)
        except Exception:
            pass
        n += sum(1 for k in ("xmp", "XML:com.adobe.xmp", "comment") if k in (im.info or {}))
    return n, has_gps


META_KEEP_EXT = (".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".heic", ".heif", ".avif")


def _exif_segment(exif: bytes) -> bytes | None:
    if len(exif) + 2 > 0xFFFF:
        return None
    return b"\xff\xe1" + (len(exif) + 2).to_bytes(2, "big") + exif


def rewrite_jpeg(src: str | Path, out, exif: bytes | None, drop_xmp: bool, primary_only: bool) -> bool:
    """Change a JPEG's metadata without re-encoding the picture (zero quality loss).
    The EXIF segment is replaced by `exif` (or removed); drop_xmp also drops XMP, IPTC and
    comments; primary_only drops the extra MPF pictures (depth maps, HDR gain maps).
    Returns False when the file can't be rewritten safely (caller re-encodes instead)."""
    data = Path(src).read_bytes()
    if data[:2] != b"\xff\xd8":
        return False
    segs, i = [], 2
    while True:
        if i + 4 > len(data) or data[i] != 0xFF:
            return False
        m = data[i + 1]
        if m == 0xFF:
            i += 1
            continue
        if m == 0xDA:
            break
        seg = data[i:i + 2 + int.from_bytes(data[i + 2:i + 4], "big")]
        segs.append((m, seg))
        i += len(seg)
    scan = data[i:]
    if primary_only:
        end = scan.find(b"\xff\xd9")
        if end < 0:
            return False
        scan = scan[:end + 2]
    new_exif = _exif_segment(exif) if exif else None
    if exif and new_exif is None:
        return False
    out_segs: list[bytes] = []
    seen_mpf = placed = False
    for m, seg in segs:
        body = seg[4:]
        is_exif = m == 0xE1 and body.startswith(b"Exif\x00\x00")
        is_mpf = m == 0xE2 and body.startswith(b"MPF\x00")
        drop = is_exif or (drop_xmp and (m in (0xED, 0xFE) or (m == 0xE1 and not is_exif))) \
            or (primary_only and is_mpf)
        if drop and seen_mpf and not primary_only:
            return False  # removing bytes after the MPF index would break its offsets
        seen_mpf = seen_mpf or is_mpf
        if is_exif and new_exif and not placed:
            out_segs.append(new_exif)
            placed = True
        elif not drop:
            out_segs.append(seg)
    if new_exif and not placed:
        at = 1 if segs and segs[0][0] == 0xE0 else 0  # after JFIF APP0, else first
        out_segs.insert(at, new_exif)
    atomic_save(lambda dst: Path(dst).write_bytes(b"\xff\xd8" + b"".join(out_segs) + scan), out)
    return True


def _is_jpeg(src: str | Path) -> bool:
    try:
        with Image.open(src) as im:
            return im.format in ("JPEG", "MPO")
    except Exception:
        return False


def _xmp_has_gps(src: str | Path) -> bool:
    with Image.open(src) as im:
        xmp = im.info.get("xmp") or im.info.get("XML:com.adobe.xmp") or b""
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "ignore")
    return b"GPS" in xmp


def strip_location(src: str | Path) -> Path:
    """Remove only GPS tags, keep camera info, date taken, colour profile and everything else."""
    ext = Path(src).suffix.lower()
    out = sibling(src, "No Location", ext if ext in META_KEEP_EXT else ".jpg")
    with Image.open(src) as im:  # read everything while open: TIFF loads sub-IFDs lazily
        raw = im.getexif()
        as_is = None  # EXIF minus GPS, for a lossless JPEG rewrite (orientation unchanged)
        if raw:
            ex = Image.Exif()
            ex.load(raw.tobytes())
            if 0x8825 in ex:
                del ex[0x8825]
            as_is = ex.tobytes() if len(ex) else None
        upright = clean_exif(raw, drop_gps=True) if raw else None
    if _is_jpeg(src) and out.suffix.lower() in (".jpg", ".jpeg"):
        if rewrite_jpeg(src, out, as_is, drop_xmp=_xmp_has_gps(src), primary_only=False):
            return out
    im = open_image(src)  # pixels are rotated upright here, so the orientation tag resets to 1
    save_image(im, out, target_for(out), icc=im.info.get("icc_profile"), exif=upright, keep_meta=False)
    return out


def strip_image_metadata(src: str | Path) -> Path:
    """Remove EXIF/XMP/IPTC/comments. The colour profile stays (it isn't personal and dropping
    it changes the colours). JPEGs are rewritten without re-encoding."""
    if _is_jpeg(src) and Path(src).suffix.lower() in (".jpg", ".jpeg"):
        with Image.open(src) as im:
            orient = im.getexif().get(0x0112, 1)
        keep = None
        if orient not in (1, None):  # keep only the rotation, or the photo shows sideways
            ex = Image.Exif()
            ex[0x0112] = orient
            keep = ex.tobytes()
        out = sibling(src, "Clean", Path(src).suffix.lower())
        if rewrite_jpeg(src, out, keep, drop_xmp=True, primary_only=True):
            return out
    im = open_image(src)  # exif_transpose applied, so orientation is baked in
    clean = Image.new(im.mode, im.size)
    clean.paste(im)
    icc = im.info.get("icc_profile")
    ext = Path(src).suffix.lower()
    out = sibling(src, "Clean", ".png" if ext in (".gif", ".ico") else ext)
    save_image(clean, out, target_for(out), icc=icc, keep_meta=False)
    return out


def read_pdf_metadata(src: str | Path) -> dict[str, str]:
    with open_pdf(src) as doc:
        info = {k: str(v) for k, v in (doc.metadata or {}).items() if v}
        info["Pages"] = str(doc.page_count)
        if doc.get_xml_metadata():
            info["XMP"] = "present"
    return info


def strip_pdf_metadata(src: str | Path) -> Path:
    out = sibling(src, "Clean", ".pdf")
    with open_pdf(src) as doc:
        doc.set_metadata({})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        doc.save(out, garbage=3, deflate=True)
    return out


# ---------------------------------------------------------------- pdf extras
def split_pdf(src: str | Path) -> list[Path]:
    outs = []
    with open_pdf(src) as doc:
        for i in range(doc.page_count):
            nd = fitz.open()
            nd.insert_pdf(doc, from_page=i, to_page=i)
            out = sibling(src, f"page {i + 1}", ".pdf")
            nd.save(out)
            nd.close()
            outs.append(out)
    return outs


def merge_pdfs(srcs: list[str | Path]) -> Path:
    out = sibling(srcs[0], "Merged", ".pdf")
    nd = fitz.open()
    for s in srcs:
        with open_pdf(s) as d:
            nd.insert_pdf(d)
    nd.save(out, garbage=3, deflate=True)
    nd.close()
    return out


def protect_pdf(src: str | Path, password: str) -> Path:
    out = sibling(src, "Protected", ".pdf")
    with open_pdf(src) as doc:
        perms = int(fitz.PDF_PERM_ACCESSIBILITY | fitz.PDF_PERM_PRINT | fitz.PDF_PERM_COPY)
        doc.save(out, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw=password, user_pw=password,
                 permissions=perms, garbage=3, deflate=True)
    return out


def extract_pdf_images(src: str | Path) -> list[Path]:
    from .util import output_dir, unique_path
    folder = unique_path(output_dir(src) / f"{Path(src).stem} images")
    outs: list[Path] = []
    seen: set[int] = set()
    with open_pdf(src) as doc:
        for pno, page in enumerate(doc):
            for img in page.get_images(full=True):
                xref = img[0]
                if xref in seen:
                    continue
                seen.add(xref)
                try:
                    info = doc.extract_image(xref)
                except Exception:
                    continue
                if not info or info.get("width", 0) < 16:
                    continue
                folder.mkdir(exist_ok=True)
                ext = info.get("ext", "png")
                if ext not in ("jpg", "jpeg", "png"):
                    pix = fitz.Pixmap(doc, xref)
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    p = folder / f"page {pno + 1} image {len(outs) + 1}.png"
                    pix.save(p)
                else:
                    p = folder / f"page {pno + 1} image {len(outs) + 1}.{ext}"
                    p.write_bytes(info["image"])
                outs.append(p)
    if not outs:
        from .util import UserError
        raise UserError("No images found in this PDF.")
    return outs


def pdf_page_count(src: str | Path) -> int:
    with fitz.open(src) as d:
        return d.page_count


def rotate_pdf(src: str | Path, degrees: int = 90) -> Path:
    out = sibling(src, "Rotated", ".pdf")
    with open_pdf(src) as doc:
        for p in doc:
            p.set_rotation((p.rotation + degrees) % 360)
        doc.save(out)
    return out


# ---------------------------------------------------------------- redact
def redact(im: Image.Image, rects: list[tuple[int, int, int, int]], mode: str = "black") -> Image.Image:
    im = im.copy()
    for (x0, y0, x1, y1) in rects:
        box = (max(0, min(x0, x1)), max(0, min(y0, y1)),
               min(im.width, max(x0, x1)), min(im.height, max(y0, y1)))
        if box[2] - box[0] < 1 or box[3] - box[1] < 1:
            continue
        if mode == "black":
            ImageDraw.Draw(im).rectangle(box, fill=(0, 0, 0, 255) if im.mode == "RGBA" else 0)
        else:
            region = im.crop(box)
            if mode == "pixelate":
                w, h = region.size
                # blocks of at least a third of the box (≥ 12 px): small blocks over text can be
                # reversed by matching candidate strings (e.g. Depix)
                f = max(12, min(w, h) // 3)
                region = region.resize((max(1, w // f), max(1, h // f)), Image.BILINEAR)
                region = region.resize((w, h), Image.NEAREST)
            else:  # blur – strong enough that text can't be recovered
                region = region.filter(ImageFilter.GaussianBlur(max(12, min(region.size) / 3)))
            im.paste(region, box[:2])
    return im


# ---------------------------------------------------------------- edit
@dataclass
class Adjust:
    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    sharpness: float = 1.0
    warmth: float = 0.0       # -1 … 1
    rotate: int = 0           # multiples of 90
    flip_h: bool = False
    flip_v: bool = False
    grayscale: bool = False
    resize_pct: int = 100


def auto_enhance(im: Image.Image) -> Adjust:
    """Pick gentle adjustments from the histogram (like a phone's 'auto' button)."""
    from PIL import ImageStat
    g = im.convert("L")
    st = ImageStat.Stat(g)
    mean, std = st.mean[0], st.stddev[0]
    a = Adjust()
    a.brightness = max(0.85, min(1.25, 1 + (118 - mean) / 400))
    a.contrast = max(0.95, min(1.3, 1 + (58 - std) / 160))
    sat = ImageStat.Stat(im.convert("HSV").split()[1]).mean[0]
    a.saturation = max(1.0, min(1.3, 1 + (110 - sat) / 400))
    a.sharpness = 1.15
    return a


def apply_adjust(im: Image.Image, a: Adjust) -> Image.Image:
    alpha = im.split()[3] if im.mode == "RGBA" else None
    rgb = im.convert("RGB")
    if a.brightness != 1.0:
        rgb = ImageEnhance.Brightness(rgb).enhance(a.brightness)
    if a.contrast != 1.0:
        rgb = ImageEnhance.Contrast(rgb).enhance(a.contrast)
    if a.saturation != 1.0:
        rgb = ImageEnhance.Color(rgb).enhance(a.saturation)
    if a.sharpness != 1.0:
        rgb = ImageEnhance.Sharpness(rgb).enhance(a.sharpness)
    if a.warmth:
        r, g, b = rgb.split()
        k = a.warmth * 25
        r = r.point(lambda v: max(0, min(255, v + k)))
        b = b.point(lambda v: max(0, min(255, v - k)))
        rgb = Image.merge("RGB", (r, g, b))
    if a.grayscale:
        rgb = ImageOps.grayscale(rgb).convert("RGB")
    out = rgb
    if alpha is not None:
        out = rgb.convert("RGBA")
        out.putalpha(alpha)
    if a.flip_h:
        out = ImageOps.mirror(out)
    if a.flip_v:
        out = ImageOps.flip(out)
    if a.rotate % 360:
        out = out.rotate(-a.rotate, expand=True)
    if a.resize_pct != 100:
        w = max(1, round(out.width * a.resize_pct / 100))
        h = max(1, round(out.height * a.resize_pct / 100))
        out = out.resize((w, h), Image.LANCZOS)
    return out


# ---------------------------------------------------------------- add background
@dataclass
class Background:
    colors: list[str] = field(default_factory=lambda: ["#ff9a5a", "#ff3d6e", "#7b5cff"])
    angle: float = 135.0       # degrees, CSS-style
    padding: float = 0.12      # fraction of the longer image side
    radius: int = 24           # px on the source image scale
    shadow: float = 0.5        # 0 … 1
    aspect: str = "Auto"       # Auto, 1:1, 4:3, 16:9, 9:16, 4:5


def _hex(c: str) -> tuple[int, int, int]:
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore


def gradient(size: tuple[int, int], colors: list[str], angle: float) -> Image.Image:
    import math
    w, h = size
    if len(colors) == 1:
        return Image.new("RGB", size, _hex(colors[0]))
    # Build a 1-px ramp, then stretch + rotate a big square and crop the centre.
    stops = [_hex(c) for c in colors]
    n = 512
    ramp = Image.new("RGB", (n, 1))
    px = ramp.load()
    for i in range(n):
        t = i / (n - 1) * (len(stops) - 1)
        j = min(int(t), len(stops) - 2)
        f = t - j
        a, b = stops[j], stops[j + 1]
        px[i, 0] = tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))
    diag = int(math.hypot(w, h)) + 2
    big = ramp.resize((diag, diag), Image.BILINEAR)
    big = big.rotate(-(angle - 90), resample=Image.BICUBIC)
    left, top = (diag - w) // 2, (diag - h) // 2
    return big.crop((left, top, left + w, top + h))


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    scale = 4  # supersample for smooth corners
    m = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, m.width - 1, m.height - 1),
                                        radius=radius * scale, fill=255)
    return m.resize(size, Image.LANCZOS)


ASPECTS = {"1:1": 1.0, "4:3": 4 / 3, "16:9": 16 / 9, "9:16": 9 / 16, "4:5": 4 / 5, "3:2": 1.5}


def add_background(im: Image.Image, bg: Background) -> Image.Image:
    im = im.convert("RGBA")
    w, h = im.size
    pad = int(max(w, h) * bg.padding)
    cw, ch = w + 2 * pad, h + 2 * pad
    if bg.aspect in ASPECTS:
        r = ASPECTS[bg.aspect]
        if cw / ch < r:
            cw = int(round(ch * r))
        else:
            ch = int(round(cw / r))
    canvas = gradient((cw, ch), bg.colors, bg.angle).convert("RGBA")
    x, y = (cw - w) // 2, (ch - h) // 2
    radius = min(bg.radius, min(w, h) // 2)
    mask = rounded_mask((w, h), radius) if radius > 0 else Image.new("L", (w, h), 255)
    if im.mode == "RGBA":
        mask = ImageChops.multiply(mask, im.split()[3])
    if bg.shadow > 0:
        blur = max(4, int(max(w, h) * 0.035))
        sh = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
        smask = Image.new("L", (cw, ch), 0)
        smask.paste(mask, (x, y + int(blur * 0.6)))
        smask = smask.filter(ImageFilter.GaussianBlur(blur))
        smask = smask.point(lambda v: int(v * 0.65 * bg.shadow))
        sh.putalpha(smask)
        canvas = Image.alpha_composite(canvas, sh)
    layer = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    layer.paste(im, (x, y), mask)
    return Image.alpha_composite(canvas, layer)


# ---------------------------------------------------------------- Qt bridge
def pil_to_png_bytes(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue()
