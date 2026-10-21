"""Paths, names, settings, big inputs, concurrency, and failure cleanup."""
from __future__ import annotations

import threading

import pytest
from PIL import Image

from conftest import photo, psnr
from dragshift import convert, imaging
from dragshift.app import friendly_error
from dragshift.util import CONFIG, UserError, category, sibling


def test_never_overwrites(tmp_path):
    src = tmp_path / "Holiday.png"
    photo(64, 64).save(src)
    names = [convert.convert(src, "image", "JPG")[0].name for _ in range(3)]
    assert names == ["Holiday.jpg", "Holiday (1).jpg", "Holiday (2).jpg"]
    assert src.exists() and psnr(src, photo(64, 64)) == float("inf"), "original changed"


@pytest.mark.parametrize("name", ["Fötö 📷 (1).png", "spaces   in  name.png", "日本語.png", ".hidden.png",
                                  "dots.in.name.v2.png", "x" * 200 + ".png"])
def test_awkward_names(tmp_path, name):
    src = tmp_path / name
    photo(64, 64).save(src)
    out = convert.convert(src, "image", "WEBP")[0]
    assert out.exists() and out.parent == tmp_path and out.suffix == ".webp"


def test_output_subfolder_setting(tmp_path):
    CONFIG["output"] = "folder"
    src = tmp_path / "a.png"
    photo(64, 64).save(src)
    outs = [convert.convert(src, "image", "JPG")[0], imaging.resize_image(src, (32, 32)),
            imaging.strip_image_metadata(src)]
    assert all(o.parent == tmp_path / "DragShift" for o in outs)


def test_uppercase_and_odd_extensions(tmp_path):
    for name, fmt in [("A.JPG", "JPEG"), ("B.JPEG", "JPEG"), ("C.Tif", "TIFF"), ("D.HEIF", "HEIF")]:
        p = tmp_path / name
        photo(64, 64).save(p, fmt)
        assert category(p) == "image"
        assert convert.convert(p, "image", "PNG")[0].exists()


def test_wrong_extension_still_converts(tmp_path):
    p = tmp_path / "really_a_png.jpg"
    photo(64, 64).save(p, "PNG")
    out = convert.convert(p, "image", "WEBP")[0]
    assert psnr(out, p) > 30


def test_200_megapixel_photo(tmp_path):
    src = tmp_path / "huge.jpg"
    Image.new("RGB", (16320, 12240), (40, 90, 160)).save(src, quality=85)  # 200 MP sensor
    out = convert.convert(src, "image", "PNG")[0]
    with Image.open(out) as o:
        assert o.size == (16320, 12240)


def test_parallel_conversions(tmp_path):
    srcs = []
    for i in range(8):
        p = tmp_path / f"p{i}.png"
        photo(600, 400, seed=i).save(p)
        srcs.append(p)
    errors, outs = [], []

    def work(p):
        try:
            outs.extend(convert.convert(p, "image", "AVIF"))
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=work, args=(p,)) for p in srcs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and len(outs) == 8 and all(o.exists() for o in outs)


def test_failed_save_leaves_no_files(tmp_path, monkeypatch):
    src = tmp_path / "a.png"
    photo(200, 200).save(src)
    real = Image.Image.save

    def boom(self, fp, *a, **k):
        real(self, fp, *a, **k)  # write a partial file first, then fail like a full disk
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(Image.Image, "save", boom)
    with pytest.raises(OSError):
        convert.convert(src, "image", "JPG")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png"], "partial output left behind"


def test_failed_pdf_build_leaves_no_files(tmp_path):
    good, bad = tmp_path / "a.png", tmp_path / "b.png"
    photo(100, 100).save(good)
    bad.write_bytes(b"not an image")
    with pytest.raises(Exception):
        convert.images_to_single_pdf([good, bad])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.png", "b.png"]


@pytest.mark.parametrize("exc,expect", [
    (PermissionError(13, "Permission denied"), "No permission"),
    (FileNotFoundError(2, "gone"), "moved or deleted"),
    (MemoryError(), "too large"),
    (OSError("image file is truncated (3 bytes not processed)"), "damaged"),
    (OSError(28, "No space left on device"), "disk is full"),
])
def test_friendly_errors(exc, expect):
    assert expect in friendly_error(exc)


def test_unsupported_and_encrypted_inputs(tmp_path):
    assert category(tmp_path / "x.zip") is None
    p = tmp_path / "x.docx"
    p.write_bytes(b"PK\x03\x04 not really a docx")
    with pytest.raises(Exception):
        convert.convert(p, "docx", "PDF")
    assert not [q for q in tmp_path.iterdir() if q.suffix == ".pdf"]


def test_sibling_label_and_ext():
    from pathlib import Path
    assert sibling(Path("/nonexistent/a b.jpg"), "Cropped").name == "a b Cropped.jpg"
    assert sibling(Path("/nonexistent/a.jpg"), ext="png").name == "a.png"


def test_metadata_tools_refuse_nothing_silently(tmp_path):
    """No EXIF at all: tools still work and don't invent data."""
    src = tmp_path / "plain.jpg"
    photo(100, 100).save(src)
    assert imaging.count_metadata(src) == (0, False)
    assert imaging.strip_location(src).exists()
    assert imaging.strip_image_metadata(src).exists()
    with pytest.raises(UserError):
        import pymupdf
        d = pymupdf.open()
        d.new_page().insert_text((50, 50), "no images")
        d.save(tmp_path / "t.pdf")
        imaging.extract_pdf_images(tmp_path / "t.pdf")
