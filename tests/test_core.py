"""Run with: python -m pytest -q"""
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from dragshift import convert, imaging
from dragshift.util import category, sibling


@pytest.fixture
def img(tmp_path) -> Path:
    im = Image.new("RGBA", (640, 420), (255, 255, 255, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 600, 380), fill=(255, 120, 80, 255))
    d.text((60, 60), "DragShift test", fill=(0, 0, 0, 255))
    p = tmp_path / "Page 001.png"
    exif = Image.Exif()
    exif[0x010F] = "TestCam"  # Make
    im.save(p, exif=exif)
    return p


@pytest.fixture
def pdf(tmp_path, img) -> Path:
    doc = convert.fitz.open()
    for i in range(2):
        page = doc.new_page()
        page.insert_text((72, 72), f"Hello page {i + 1}", fontsize=24)
        page.insert_text((72, 120), "Body text line for extraction.", fontsize=11)
        page.insert_image(convert.fitz.Rect(72, 150, 300, 300), filename=str(img))
    doc.set_metadata({"author": "Jimmy", "title": "Demo"})
    p = tmp_path / "Doc.pdf"
    doc.save(p)
    return p


@pytest.mark.parametrize("target", convert.targets_for("image", ".png"))
def test_image_targets(img, target):
    out = convert.convert(img, "image", target)[0]
    assert out.exists() and out.stat().st_size > 0
    if target != "PDF":
        Image.open(out).load()


def test_pdf_conversions(pdf):
    imgs = convert.convert(pdf, "pdf", "JPG")
    assert len(imgs) == 2 and all(p.exists() for p in imgs)
    txt = convert.convert(pdf, "pdf", "TXT")[0]
    assert "Hello page 2" in txt.read_text()
    docx = convert.convert(pdf, "pdf", "DOCX")[0]
    from docx import Document
    d = Document(docx)
    assert any("Hello page 1" in p.text for p in d.paragraphs)
    assert d.inline_shapes, "images should be carried over"


def test_docx_and_text(tmp_path, pdf):
    docx = convert.convert(pdf, "pdf", "DOCX")[0]
    out_pdf = convert.convert(docx, "docx", "PDF")[0]
    with convert.fitz.open(out_pdf) as d:
        assert "Hello page 1" in d[0].get_text()
    assert "Hello" in convert.convert(docx, "docx", "TXT")[0].read_text()
    t = tmp_path / "notes.txt"
    t.write_text("line one\nline two <b>not bold</b>\n" * 80)
    tp = convert.convert(t, "text", "PDF")[0]
    with convert.fitz.open(tp) as d:
        assert d.page_count >= 2 and "<b>not bold</b>" in d[0].get_text()
    assert convert.convert(t, "text", "DOCX")[0].exists()


def test_tools(img, pdf):
    im = convert.open_image(img)
    assert imaging.save_like(im.crop((0, 0, 100, 100)), img, "Cropped").name == "Page 001 Cropped.png"
    bg = imaging.add_background(im, imaging.Background(aspect="16:9"))
    assert abs(bg.width / bg.height - 16 / 9) < 0.01
    red = imaging.redact(im, [(40, 40, 200, 200)], "black")
    assert red.getpixel((100, 100))[:3] == (0, 0, 0)
    for m in ("pixelate", "blur"):
        imaging.redact(im, [(40, 40, 200, 200)], m)
    ed = imaging.apply_adjust(im, imaging.Adjust(brightness=1.2, rotate=90, flip_h=True, warmth=0.5, resize_pct=50))
    assert ed.size == (210, 320)
    assert "Make" in imaging.read_image_metadata(img)
    clean = imaging.strip_image_metadata(img)
    assert "Make" not in imaging.read_image_metadata(clean)
    c = imaging.compress_image(img, 60)
    assert c.exists()
    assert imaging.compress_image(img, 60, 300, to_webp=True).suffix == ".webp"
    assert imaging.read_pdf_metadata(pdf)["author"] == "Jimmy"
    assert "author" not in imaging.read_pdf_metadata(imaging.strip_pdf_metadata(pdf))
    assert len(imaging.split_pdf(pdf)) == 2
    assert imaging.compress_pdf(pdf).exists()
    assert imaging.rotate_pdf(pdf).exists()
    assert imaging.merge_pdfs([pdf, pdf]).exists()
    assert convert.images_to_single_pdf([img, img])[0].exists()


def test_paths(tmp_path):
    f = tmp_path / "a.jpg"
    f.write_bytes(b"x")
    assert sibling(f, ext=".jpg").name == "a (1).jpg"
    assert category("x.HEIC") == "image" and category("x.pdf") == "pdf" and category("x.exe") is None


@pytest.mark.skipif(__import__("sys").platform != "win32", reason="Windows shortcuts")
def test_shortcut_resolves_to_target(img, tmp_path):
    import win32com.client
    from dragshift.util import resolve_shortcuts
    lnk = tmp_path / "pic.lnk"
    sc = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(lnk))
    sc.TargetPath = str(img)
    sc.Save()
    out = resolve_shortcuts([str(lnk)])
    assert Path(out[0]) == img and category(out[0]) == "image"
    assert resolve_shortcuts([str(img)]) == [str(img)]
