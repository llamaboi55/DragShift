"""Image conversion: every source format × every target, with pixel-quality, colour-profile,
EXIF, orientation, alpha, bit-depth and animation checks."""
from __future__ import annotations

import io

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageCms

from conftest import (camera_exif, date_taken, exif_of, has_gps, icc_desc, marked, photo, psnr,
                      reencode, rgb)
from dragshift import convert

SOURCES = {  # extension → (Pillow format, save kwargs)
    "png": ("PNG", {}), "jpg": ("JPEG", {"quality": 95}), "webp": ("WEBP", {"lossless": True}),
    "heic": ("HEIF", {"quality": 95}), "tiff": ("TIFF", {}), "avif": ("AVIF", {"quality": 95}),
    "bmp": ("BMP", {}), "gif": ("GIF", {}), "ico": ("ICO", {}),
}
PIL_FMT = {"PNG": "PNG", "JPG": "JPEG", "WEBP": "WEBP", "HEIC": "HEIF", "TIFF": "TIFF",
           "AVIF": "AVIF", "BMP": "BMP", "GIF": "GIF", "ICO": "ICO"}
LOSSLESS = {"PNG", "TIFF", "BMP"}
LOSSY = {"JPG": "JPEG", "WEBP": "WEBP", "HEIC": "HEIF", "AVIF": "AVIF"}


def make_source(tmp_path, ext: str, im: Image.Image | None = None):
    fmt, kw = SOURCES[ext]
    im = im or (photo(128, 128) if ext == "ico" else photo())
    p = tmp_path / f"src.{ext}"
    im.save(p, fmt, **kw)
    return p


def pdf_page_image(pdf) -> tuple[Image.Image, dict]:
    d = pymupdf.open(pdf)
    xref = d[0].get_images(full=True)[0][0]
    info = d.extract_image(xref)
    return Image.open(io.BytesIO(info["image"])), info


# ---------------------------------------------------------------- the full matrix
@pytest.mark.parametrize("src_ext", list(SOURCES))
@pytest.mark.parametrize("target", convert.IMAGE_TARGETS)
def test_conversion_matrix(tmp_path, src_ext, target):
    src = make_source(tmp_path, src_ext)
    if target not in convert.targets_for("image", "." + src_ext):
        pytest.skip("same format isn't offered")
    ref = convert.open_image(src)  # what the source decodes to: the best any target can do
    outs = convert.convert(src, "image", target)
    assert len(outs) == 1
    out = outs[0]
    assert out.exists() and out.suffix == "." + target.lower()
    assert not list(tmp_path.glob(".~*")), "temp file left behind"

    if target == "PDF":
        page_im, _ = pdf_page_image(out)
        assert page_im.size == ref.size
        assert psnr(page_im, ref) >= 40
        return
    o = Image.open(out)
    assert o.format == PIL_FMT[target]
    if target == "ICO":
        assert max(o.size) <= 256
        assert psnr(o.resize(ref.size), ref) >= 25
        return
    assert o.size == ref.size
    if target in LOSSLESS:
        assert psnr(o, ref) == float("inf"), "lossless target changed pixels"
    elif target == "GIF":
        assert psnr(o, ref) >= 25
    else:
        # must clearly beat an ordinary quality-75 encode of the same pixels
        q75 = reencode(convert.flatten(ref), LOSSY[target], quality=75)
        assert psnr(o, ref) >= psnr(q75, ref) - 0.05, "lossy output below quality-75 level"
        assert psnr(o, ref) >= (30 if src_ext == "gif" else 35)  # dithered GIF pixels compress worse


def test_jpeg_high_quality_uses_full_colour_resolution(tmp_path):
    p = tmp_path / "a.png"
    photo().save(p)
    out = convert.convert(p, "image", "JPG")[0]
    from PIL import JpegImagePlugin
    assert JpegImagePlugin.get_sampling(Image.open(out)) == 0  # 4:4:4


def test_quality_setting_is_respected(tmp_path):
    from dragshift.util import CONFIG
    p = tmp_path / "a.png"
    photo().save(p)
    CONFIG["jpg_quality"] = 60
    small = convert.convert(p, "image", "JPG")[0].stat().st_size
    CONFIG["jpg_quality"] = 95
    big = convert.convert(p, "image", "JPG")[0].stat().st_size
    assert big > small * 1.5


# ---------------------------------------------------------------- alpha
@pytest.mark.parametrize("target", ["PNG", "WEBP", "TIFF", "AVIF", "HEIC", "GIF", "ICO"])
def test_transparency_kept(tmp_path, target):
    p = tmp_path / "a.png"
    photo(256, 256, alpha=True).save(p)
    o = Image.open(convert.convert(p, "image", target)[0]).convert("RGBA")
    corner, centre = o.getpixel((2, 2)), o.getpixel((o.width // 2, o.height // 2))
    assert corner[3] == 0, "transparent corner became opaque"
    assert centre[3] == 255


@pytest.mark.parametrize("target", ["JPG", "BMP", "PDF"])
def test_transparency_flattened_on_white(tmp_path, target):
    p = tmp_path / "a.png"
    photo(256, 256, alpha=True).save(p)
    out = convert.convert(p, "image", target)[0]
    o = pdf_page_image(out)[0] if target == "PDF" else Image.open(out)
    assert all(v >= 250 for v in o.convert("RGB").getpixel((2, 2)))


# ---------------------------------------------------------------- colour profiles
@pytest.mark.parametrize("src_ext", ["jpg", "png", "heic", "webp", "tiff", "avif"])
@pytest.mark.parametrize("target", sorted(convert.ICC_TARGETS))
def test_display_p3_profile_kept(tmp_path, p3, src_ext, target):
    fmt, kw = SOURCES[src_ext]
    src = tmp_path / f"p3.{src_ext}"
    photo().save(src, fmt, icc_profile=p3, **kw)
    assert icc_desc(src) == "Display P3"
    if target not in convert.targets_for("image", "." + src_ext):
        pytest.skip("same format")
    out = convert.convert(src, "image", target)[0]
    assert icc_desc(out) == "Display P3", f"{src_ext}→{target} lost the colour profile"


@pytest.mark.parametrize("target", ["GIF", "BMP", "ICO"])
def test_p3_converted_to_srgb_for_formats_without_profiles(tmp_path, p3, target):
    src = tmp_path / "p3.png"
    Image.new("RGB", (64, 64), (255, 0, 0)).save(src, icc_profile=p3)  # P3 red: outside sRGB
    expected = ImageCms.profileToProfile(Image.new("RGB", (1, 1), (255, 0, 0)),
                                         ImageCms.ImageCmsProfile(io.BytesIO(p3)),
                                         ImageCms.createProfile("sRGB")).getpixel((0, 0))
    o = Image.open(convert.convert(src, "image", target)[0]).convert("RGB")
    got = o.getpixel((o.width // 2, o.height // 2))
    assert all(abs(a - b) <= 6 for a, b in zip(got, expected)), (got, expected)


def test_p3_kept_in_pdf(tmp_path, p3):
    src = tmp_path / "p3.png"
    photo().save(src, icc_profile=p3)
    _, info = pdf_page_image(convert.convert(src, "image", "PDF")[0])
    d = pymupdf.open(tmp_path / "p3.pdf")
    assert d[0].get_images(full=True)[0][5] == "ICCBased"


def test_cmyk_jpeg_converts_to_correct_rgb(tmp_path):
    src = tmp_path / "cmyk.jpg"
    Image.new("CMYK", (64, 64), (0, 255, 255, 0)).save(src, quality=95)
    for target in ("PNG", "WEBP", "PDF"):
        out = convert.convert(src, "image", target)[0]
        o = pdf_page_image(out)[0] if target == "PDF" else Image.open(out)
        r, g, b = o.convert("RGB").getpixel((32, 32))
        assert r > 230 and g < 30 and b < 30, (target, (r, g, b))
        if target != "PDF":
            assert icc_desc(out) is None  # a CMYK profile on RGB pixels would be wrong


# ---------------------------------------------------------------- EXIF + orientation
@pytest.mark.parametrize("target", sorted(convert.EXIF_TARGETS - {"JPG"}) + ["JPG"])
def test_camera_exif_kept_and_orientation_applied(tmp_path, target):
    src = tmp_path / ("photo.png" if target == "JPG" else "photo.jpg")
    # stored sideways (300×200) with orientation 6 = "rotate 90° clockwise to display"
    marked().save(src, exif=camera_exif(orientation=6), quality=95)
    out = convert.convert(src, "image", target)[0]
    o = Image.open(out)
    assert o.size == (200, 300), "orientation not applied"
    # after a 90° clockwise turn, the stored top-left corner shows top-right
    r, g, b = o.convert("RGB").getpixel((195, 5))
    assert r > 200 and g < 60, "pixels not rotated the right way"
    ex = exif_of(out)
    assert ex.get(0x0112, 1) == 1, "orientation tag not reset: viewers would rotate twice"
    assert ex.get(0x010F) == "Apple"
    assert date_taken(out) == "2026:10:01 11:59:58"
    assert has_gps(out), "conversion shouldn't silently drop location (Metadata tool does that)"


@pytest.mark.parametrize("orientation,size", [(1, (300, 200)), (3, (300, 200)), (6, (200, 300)),
                                              (8, (200, 300)), (2, (300, 200)), (5, (200, 300))])
def test_all_orientations(tmp_path, orientation, size):
    src = tmp_path / "o.jpg"
    marked().save(src, exif=camera_exif(orientation=orientation), quality=95)
    o = Image.open(convert.convert(src, "image", "PNG")[0])
    assert o.size == size
    assert exif_of(o.filename).get(0x0112, 1) == 1


def test_rotated_jpeg_to_pdf_is_upright(tmp_path):
    src = tmp_path / "r.jpg"
    marked().save(src, exif=camera_exif(orientation=6), quality=95)
    im, _ = pdf_page_image(convert.convert(src, "image", "PDF")[0])
    assert im.size == (200, 300)


# ---------------------------------------------------------------- bit depth / unusual modes
def _write_cv(path, arr):
    import cv2
    assert cv2.imwrite(str(path), arr)


@pytest.mark.parametrize("name,make,expect", [
    ("gray16.png", lambda p: Image.fromarray(np.full((64, 64), 32896, np.uint16)).save(p), (128, 128, 128)),
    ("gray16.tiff", lambda p: Image.fromarray(np.full((64, 64), 32896, np.uint16)).save(p), (128, 128, 128)),
    ("rgb48.png", lambda p: _write_cv(p, np.dstack([np.full((64, 64), v, np.uint16) for v in (8224, 32896, 61680)])),
     (240, 128, 32)),  # OpenCV writes BGR
    ("rgb48.tiff", lambda p: _write_cv(p, np.dstack([np.full((64, 64), v, np.uint16) for v in (8224, 32896, 61680)])),
     (240, 128, 32)),
    ("bilevel.png", lambda p: Image.new("1", (64, 64), 0).save(p), (0, 0, 0)),
    ("la.png", lambda p: Image.new("LA", (64, 64), (90, 255)).save(p), (90, 90, 90)),
    ("palette.gif", lambda p: Image.new("RGB", (64, 64), (10, 200, 30)).convert("P", palette=Image.ADAPTIVE).save(p),
     (10, 200, 30)),
])
@pytest.mark.parametrize("target", ["JPG", "PNG", "WEBP", "PDF"])
def test_bit_depths_and_modes(tmp_path, name, make, expect, target):
    src = tmp_path / name
    make(src)
    out = convert.convert(src, "image", target)[0]
    o = pdf_page_image(out)[0] if target == "PDF" else Image.open(out)
    got = o.convert("RGB").getpixel((32, 32))
    assert all(abs(a - b) <= 4 for a, b in zip(got, expect)), (name, target, got, expect)


# ---------------------------------------------------------------- animation & pages
def _anim(path, fmt, n=4):
    frames = [Image.new("RGB", (48, 48), c) for c in ("red", "lime", "blue", "yellow")[:n]]
    frames[0].save(path, fmt, save_all=True, append_images=frames[1:], duration=[80, 120, 160, 200][:n], loop=0)


@pytest.mark.parametrize("src_ext,src_fmt", [("gif", "GIF"), ("webp", "WEBP"), ("png", "PNG")])
@pytest.mark.parametrize("target", ["GIF", "WEBP", "PNG", "AVIF"])
def test_animation_kept(tmp_path, src_ext, src_fmt, target):
    if target == src_fmt:
        pytest.skip("same format")
    src = tmp_path / f"anim.{src_ext}"
    _anim(src, src_fmt)
    o = Image.open(convert.convert(src, "image", target)[0])
    assert getattr(o, "n_frames", 1) == 4, "animation flattened to one frame"
    colours = []
    for i in range(4):
        o.seek(i)
        colours.append(o.convert("RGB").getpixel((24, 24)))
    assert colours[0][0] > 200 and colours[1][1] > 200 and colours[2][2] > 200
    if target != "AVIF":
        durs = []
        for i in range(4):
            o.seek(i)
            o.load()  # WebP fills in the frame duration on load
            durs.append(o.info.get("duration"))
        assert durs == [80, 120, 160, 200]


def test_animation_to_still_format_uses_first_frame(tmp_path):
    src = tmp_path / "anim.gif"
    _anim(src, "GIF")
    o = Image.open(convert.convert(src, "image", "JPG")[0])
    r, g, b = o.getpixel((24, 24))
    assert r > 200 and g < 60


def test_multipage_tiff(tmp_path):
    src = tmp_path / "scan.tiff"
    _anim(src, "TIFF", 3)
    pdf = convert.convert(src, "image", "PDF")[0]
    assert pymupdf.open(pdf).page_count == 3
    jpgs = convert.convert(src, "image", "JPG")
    assert [p.name for p in jpgs] == ["scan page 1.jpg", "scan page 2.jpg", "scan page 3.jpg"]
    assert Image.open(jpgs[2]).getpixel((24, 24))[2] > 200


# ---------------------------------------------------------------- image → PDF
def test_jpeg_goes_into_pdf_untouched(tmp_path, p3):
    src = tmp_path / "a.jpg"
    photo().save(src, quality=92, icc_profile=p3)
    pdf = convert.convert(src, "image", "PDF")[0]
    d = pymupdf.open(pdf)
    xref = d[0].get_images(full=True)[0][0]
    assert d.xref_stream_raw(xref) == src.read_bytes(), "JPEG was re-encoded"
    assert d[0].get_images(full=True)[0][5] == "ICCBased"


def test_png_goes_into_pdf_lossless(tmp_path):
    src = tmp_path / "a.png"
    photo().save(src)
    im, _ = pdf_page_image(convert.convert(src, "image", "PDF")[0])
    assert psnr(im, Image.open(src)) == float("inf")


def test_combined_pdf_keeps_order_and_sizes(tmp_path):
    paths = []
    for i, (w, h) in enumerate([(300, 200), (200, 300), (500, 500)]):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", (w, h), (i * 100, 0, 0)).save(p)
        paths.append(p)
    pdf = convert.images_to_single_pdf(paths)[0]
    d = pymupdf.open(pdf)
    assert d.page_count == 3
    for page, (w, h) in zip(d, [(300, 200), (200, 300), (500, 500)]):
        assert round(page.rect.width) == round(w * 72 / 150)
        assert round(page.rect.height) == round(h * 72 / 150)


def test_mpo_extra_images_not_in_pdf(tmp_path):
    """iPhone-style JPEG with an appended secondary picture (MPF)."""
    src = tmp_path / "iphone.jpg"
    main, extra = photo(), photo(160, 120, seed=7)
    main.save(src, "MPO", save_all=True, append_images=[extra], quality=92)
    assert Image.open(src).format == "MPO"
    im, _ = pdf_page_image(convert.convert(src, "image", "PDF")[0])
    assert im.size == main.size
