"""Image and PDF tools: output correctness, promised sizes, privacy guarantees, and metadata
handling (lossless where possible)."""
from __future__ import annotations

import io

import numpy as np
import pymupdf
import pytest
from PIL import Image

from conftest import (camera_exif, date_taken, exif_of, has_gps, icc_desc, marked, photo, psnr, rgb)
from dragshift import convert, imaging


def jpeg_scan(path) -> bytes:
    """Entropy-coded picture data of a JPEG's primary image (identical = not re-encoded)."""
    data = open(path, "rb").read()
    start = convert._jpeg_scan_start(data)
    return data[start:data.find(b"\xff\xd9", start) + 2]


@pytest.fixture
def camera_jpg(tmp_path, p3):
    p = tmp_path / "IMG_0001.jpg"
    photo(1200, 900).save(p, quality=95, exif=camera_exif(), icc_profile=p3)
    return p


# ---------------------------------------------------------------- compress
@pytest.mark.parametrize("ext,fmt", [("jpg", "JPEG"), ("webp", "WEBP"), ("png", "PNG"), ("heic", "HEIF")])
def test_compress_makes_smaller_and_keeps_profile(tmp_path, p3, ext, fmt):
    src = tmp_path / f"a.{ext}"
    kw = {"quality": 97} if fmt != "PNG" else {}
    photo(1200, 900).save(src, fmt, icc_profile=p3, **kw)
    out = imaging.compress_image(src, quality=72)
    assert out.stat().st_size < src.stat().st_size * 0.8
    assert Image.open(out).size == (1200, 900)
    assert psnr(out, src) >= (25 if fmt == "PNG" else 30)  # PNG = 184-colour palette, dithered
    assert icc_desc(out) == "Display P3"


def test_compress_png_keeps_transparency(tmp_path):
    src = tmp_path / "logo.png"
    photo(400, 400, alpha=True).save(src)
    out = imaging.compress_image(src, quality=60)
    o = Image.open(out).convert("RGBA")
    assert o.getpixel((2, 2))[3] == 0 and o.getpixel((200, 200))[3] == 255


@pytest.mark.parametrize("target", ["JPG", "WEBP", "PNG"])
@pytest.mark.parametrize("limit_kb", [500, 200, 100])
def test_compress_to_target_size_is_honoured(tmp_path, p3, target, limit_kb):
    src = tmp_path / "big.jpg"
    photo(3000, 2000).save(src, quality=97, exif=camera_exif(), icc_profile=p3)
    limit = limit_kb * 1024
    im = convert.open_image(src)
    q, side = imaging.plan_target_size(im, target, limit)
    out = imaging.compress_to(src, q, side, target)
    assert out.stat().st_size <= limit, f"{out.stat().st_size} > {limit}"
    assert max(Image.open(out).size) >= 400, "shrunk far more than needed"


# ---------------------------------------------------------------- resize / crop / edit
@pytest.mark.parametrize("mode,value,expect", [("pct", 50, (600, 450)), ("long", 800, (800, 600)),
                                               ("w", 300, (300, 225)), ("h", 90, (120, 90)),
                                               ("pct", 200, (2400, 1800))])
def test_resize(camera_jpg, mode, value, expect):
    dims = imaging.resize_dims((1200, 900), mode, value)
    assert dims == expect
    out = imaging.resize_image(camera_jpg, dims)
    assert Image.open(out).size == expect
    assert icc_desc(out) == "Display P3" and date_taken(out) and has_gps(out)


def test_resize_rotated_photo(tmp_path):
    src = tmp_path / "r.jpg"
    marked(300, 200).save(src, exif=camera_exif(orientation=6), quality=95)
    out = imaging.resize_image(src, imaging.resize_dims((200, 300), "pct", 50))
    o = Image.open(out)
    assert o.size == (100, 150) and exif_of(out).get(0x0112, 1) == 1


def test_crop_via_save_like(camera_jpg):
    im = convert.open_image(camera_jpg).crop((100, 50, 500, 350))
    out = imaging.save_like(im, camera_jpg, "Cropped")
    assert out.name == "IMG_0001 Cropped.jpg"
    assert Image.open(out).size == (400, 300)
    assert icc_desc(out) == "Display P3" and date_taken(out)


@pytest.mark.parametrize("ext", ["gif", "ico"])
def test_edits_of_gif_and_ico_save_as_png(tmp_path, ext):
    src = tmp_path / f"a.{ext}"
    Image.new("RGB", (64, 64), "red").save(src)
    out = imaging.save_like(convert.open_image(src), src, "Edited")
    assert out.suffix == ".png" and Image.open(out).format == "PNG"


def test_adjustments():
    im = photo(300, 200, alpha=True)
    a = imaging.Adjust(rotate=90, flip_h=True, grayscale=True, resize_pct=50)
    out = imaging.apply_adjust(im, a)
    assert out.size == (100, 150) and out.mode == "RGBA"
    r, g, b, _ = [np.asarray(c, float) for c in out.split()]
    assert np.abs(r - g).max() <= 1 and np.abs(g - b).max() <= 1
    assert imaging.apply_adjust(im, imaging.Adjust()).tobytes() == im.tobytes(), "no-op edit changed pixels"
    auto = imaging.auto_enhance(photo())
    assert 0.85 <= auto.brightness <= 1.25 and 0.95 <= auto.contrast <= 1.3


@pytest.mark.parametrize("aspect,ratio", [("1:1", 1.0), ("16:9", 16 / 9), ("9:16", 9 / 16), ("4:5", 0.8), ("Auto", None)])
def test_add_background(aspect, ratio):
    out = imaging.add_background(photo(400, 300), imaging.Background(aspect=aspect))
    assert out.mode == "RGBA"
    if ratio:
        assert abs(out.width / out.height - ratio) < 0.01
    assert out.width > 400 and out.height > 300
    assert out.getpixel((0, 0))[3] == 255  # background is opaque


# ---------------------------------------------------------------- redact
@pytest.mark.parametrize("mode", ["black", "pixelate", "blur"])
@pytest.mark.parametrize("ext", ["jpg", "png"])
def test_redact_hides_region_and_drops_location(tmp_path, p3, ext, mode):
    camera_jpg = tmp_path / f"IMG.{ext}"
    photo(1200, 900).save(camera_jpg, exif=camera_exif().tobytes(), icc_profile=p3,
                          **({"quality": 95} if ext == "jpg" else {}))
    # high-contrast "text" in the region
    im = convert.open_image(camera_jpg)
    from PIL import ImageDraw
    ImageDraw.Draw(im).text((210, 210), "SECRET 4242 4242 4242", fill=(0, 0, 0))
    box = (200, 200, 600, 260)
    out = imaging.save_like(imaging.redact(im, [box], mode), camera_jpg, "Redacted", keep_exif=False)
    o = rgb(out)
    region, before = o[200:260, 200:600], rgb(im)[200:260, 200:600]
    if mode == "black":
        assert region.max() <= 8
    elif mode == "pixelate" and ext == "png":
        same = (np.abs(np.diff(region, axis=1)).sum(axis=2) == 0).mean()
        assert same >= 0.9, "not pixelated into big uniform blocks"
        widths = np.diff(np.flatnonzero(np.abs(np.diff(region[30], axis=0)).sum(axis=1)))
        assert widths.min() >= 12, "pixel blocks too small to be safe"
    elif mode == "pixelate":
        pass  # JPEG noise blurs block edges; geometry is checked on PNG
    else:
        # fine detail is gone: high-frequency energy drops a lot
        hf = lambda a: np.abs(np.diff(a, axis=1)).mean()
        assert hf(region) < hf(before) * 0.35
    outside = (slice(500, 900), slice(0, 1200))
    assert psnr(Image.fromarray(o[outside].astype("uint8")), Image.fromarray(rgb(im)[outside].astype("uint8"))) > 35
    assert not has_gps(out) and not exif_of(out).get(0x010F), "redacted copy still carries EXIF"
    assert icc_desc(out) == "Display P3"


# ---------------------------------------------------------------- metadata
def test_metadata_summary(camera_jpg):
    s = imaging.metadata_summary(camera_jpg)
    assert s["Camera"] == "iPhone 17 Pro" or "iPhone" in s["Camera"]
    assert s["Taken"] == "2026:10:01 11:59:58"
    assert s["Exposure"] == "1/250 s"
    assert s["Location"] == "52.37500, 4.88611"
    n, gps = imaging.count_metadata(camera_jpg)
    assert gps and n >= 8


def test_strip_location_jpeg_is_lossless(camera_jpg):
    out = imaging.strip_location(camera_jpg)
    assert out.name == "IMG_0001 No Location.jpg"
    assert jpeg_scan(out) == jpeg_scan(camera_jpg), "photo was re-encoded"
    assert not has_gps(out)
    assert exif_of(out).get(0x010F) == "Apple" and date_taken(out) == "2026:10:01 11:59:58"
    assert icc_desc(out) == "Display P3"


def test_strip_location_keeps_rotation(tmp_path):
    src = tmp_path / "r.jpg"
    marked().save(src, exif=camera_exif(orientation=6), quality=95)
    out = imaging.strip_location(src)
    assert exif_of(out).get(0x0112) == 6  # untouched pixels, so the tag must stay
    assert convert.open_image(out).size == (200, 300)


def test_strip_location_removes_gps_in_xmp(tmp_path):
    src = tmp_path / "x.jpg"
    xmp = (b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
           b'<rdf:Description xmlns:exif="http://ns.adobe.com/exif/1.0/" exif:GPSLatitude="52,22.5N"/>'
           b'</rdf:RDF></x:xmpmeta>')
    photo().save(src, quality=95, exif=camera_exif(), xmp=xmp)
    out = imaging.strip_location(src)
    assert b"GPSLatitude" not in out.read_bytes()


@pytest.mark.parametrize("ext,fmt", [("png", "PNG"), ("heic", "HEIF"), ("webp", "WEBP"), ("tiff", "TIFF")])
def test_strip_location_other_formats(tmp_path, p3, ext, fmt):
    src = tmp_path / f"a.{ext}"
    photo().save(src, fmt, exif=camera_exif().tobytes(), icc_profile=p3,
                 **({"quality": 95} if fmt in ("HEIF", "WEBP") else {}))
    assert has_gps(src)
    out = imaging.strip_location(src)
    assert out.suffix == "." + ext
    assert not has_gps(out) and exif_of(out).get(0x010F) == "Apple"
    assert icc_desc(out) == "Display P3"
    if fmt in ("PNG", "TIFF"):
        assert psnr(out, src) == float("inf")


def test_strip_all_jpeg_is_lossless_and_complete(tmp_path, p3):
    src = tmp_path / "iphone.jpg"
    xmp = b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>'
    main = photo()
    main.save(src, "MPO", save_all=True, append_images=[photo(160, 120, seed=3)], quality=92,
              exif=camera_exif(orientation=6), icc_profile=p3, xmp=xmp, comment=b"secret note")
    out = imaging.strip_image_metadata(src)
    assert jpeg_scan(out) == jpeg_scan(src), "photo was re-encoded"
    o = Image.open(out)
    assert o.format == "JPEG", "extra MPF pictures left in"
    ex = exif_of(out)
    assert dict(ex) == {0x0112: 6}, f"left behind: {dict(ex)}"
    data = out.read_bytes()
    assert b"secret note" not in data and b"xmpmeta" not in data and b"iPhone" not in data
    assert icc_desc(out) == "Display P3"


@pytest.mark.parametrize("ext,fmt", [("png", "PNG"), ("webp", "WEBP"), ("heic", "HEIF")])
def test_strip_all_other_formats(tmp_path, p3, ext, fmt):
    src = tmp_path / f"a.{ext}"
    photo().save(src, fmt, exif=camera_exif(), icc_profile=p3)
    out = imaging.strip_image_metadata(src)
    assert len(exif_of(out)) == 0
    assert icc_desc(out) == "Display P3"
    assert b"iPhone" not in out.read_bytes()


# ---------------------------------------------------------------- PDF tools
@pytest.fixture
def scan_pdf(tmp_path):
    """Three pages, each a big photo (like a phone scan) plus text."""
    d = pymupdf.open()
    for i in range(3):
        buf = io.BytesIO()
        photo(2400, 1800, seed=i).save(buf, "JPEG", quality=97)
        page = d.new_page()
        page.insert_image(page.rect, stream=buf.getvalue())
        page.insert_text((72, 72), f"Page {i + 1} text")
    d.set_metadata({"author": "Jane Doe", "title": "Private", "creator": "Scanner 3000"})
    d.set_xml_metadata('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF/></x:xmpmeta>')
    p = tmp_path / "Scan.pdf"
    d.save(p)
    return p


def test_compress_pdf(scan_pdf):
    out = imaging.compress_pdf(scan_pdf)
    assert out.stat().st_size < scan_pdf.stat().st_size * 0.6
    d = pymupdf.open(out)
    assert d.page_count == 3 and "Page 2 text" in d[1].get_text()


def test_split_merge_rotate(scan_pdf):
    parts = imaging.split_pdf(scan_pdf)
    assert [p.name for p in parts] == ["Scan page 1.pdf", "Scan page 2.pdf", "Scan page 3.pdf"]
    assert all(pymupdf.open(p).page_count == 1 for p in parts)
    merged = imaging.merge_pdfs([parts[2], parts[0], parts[1]])
    d = pymupdf.open(merged)
    assert [d[i].get_text().strip() for i in range(3)] == ["Page 3 text", "Page 1 text", "Page 2 text"]
    rot = pymupdf.open(imaging.rotate_pdf(scan_pdf))
    assert all(p.rotation == 90 for p in rot)


def test_protect_pdf(scan_pdf):
    out = imaging.protect_pdf(scan_pdf, "correct horse")
    d = pymupdf.open(out)
    assert d.needs_pass
    assert not d.authenticate("wrong")
    assert d.authenticate("correct horse")
    assert "AES" in d.metadata["encryption"] and "256" in d.metadata["encryption"]
    assert "Page 3 text" in d[2].get_text()


def test_extract_images_are_originals(tmp_path):
    jpg = io.BytesIO()
    photo(800, 600).save(jpg, "JPEG", quality=91)
    png_im = photo(300, 300, seed=4)
    png = io.BytesIO()
    png_im.save(png, "PNG")
    d = pymupdf.open()
    page = d.new_page()
    page.insert_image(pymupdf.Rect(0, 0, 300, 200), stream=jpg.getvalue())
    page.insert_image(pymupdf.Rect(0, 300, 200, 500), stream=png.getvalue())
    p = tmp_path / "Pics.pdf"
    d.save(p)
    outs = imaging.extract_pdf_images(p)
    assert len(outs) == 2
    jpg_out = [o for o in outs if o.suffix == ".jpg" or o.suffix == ".jpeg"][0]
    assert jpg_out.read_bytes() == jpg.getvalue(), "JPEG re-encoded on extraction"
    png_out = [o for o in outs if o.suffix == ".png"][0]
    assert psnr(png_out, png_im) == float("inf")


def test_strip_pdf_metadata(scan_pdf):
    out = imaging.strip_pdf_metadata(scan_pdf)
    d = pymupdf.open(out)
    assert not d.metadata.get("author") and not d.metadata.get("title")
    assert not d.get_xml_metadata()
    assert d.page_count == 3 and "Page 1 text" in d[0].get_text()
