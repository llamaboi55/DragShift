"""PDF, Word and text conversions: content survives, layout features survive, encodings decode."""
from __future__ import annotations

import io
import re
from pathlib import Path

import pymupdf
import pytest
from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor
from PIL import Image

from conftest import photo
from dragshift import convert


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def word_doc(tmp_path) -> Path:
    d = Document()
    d.add_heading("Quarterly Report", level=1)
    p = d.add_paragraph("Revenue grew ")
    p.add_run("strongly").bold = True
    p.add_run(" this quarter, ").italic = True
    r = p.add_run("in red 14pt.")
    r.font.size, r.font.color.rgb = Pt(14), RGBColor(0xCC, 0, 0)
    c = d.add_paragraph("Centred line")
    c.alignment = WD_ALIGN_PARAGRAPH.CENTER
    d.add_heading("Steps", level=2)
    for s in ("First step", "Second step", "Third step"):
        d.add_paragraph(s, style="List Number")
    for s in ("Apples", "Pears"):
        d.add_paragraph(s, style="List Bullet")
    t = d.add_table(rows=3, cols=3)
    for i, row in enumerate(t.rows):
        for j, cell in enumerate(row.cells):
            cell.text = f"R{i}C{j}"
    d.add_paragraph("After the table.")
    buf = io.BytesIO()
    photo(400, 300).save(buf, "PNG")
    buf.seek(0)
    d.add_picture(buf, width=Inches(3))
    d.add_paragraph("Last paragraph.")
    out = tmp_path / "Report.docx"
    d.save(out)
    return out


@pytest.fixture
def table_pdf(tmp_path) -> Path:
    """Two-page PDF with a heading, body text, a real ruled table and an image."""
    html = """<h1>Invoice 2026-17</h1><p>Thanks for your order. Payment within 14 days.</p>
    <table border="1"><tr><td>Item</td><td>Qty</td><td>Price</td></tr>
    <tr><td>Widget</td><td>2</td><td>10.00</td></tr><tr><td>Gadget</td><td>1</td><td>25.50</td></tr></table>
    <p>Total due: 45.50</p>"""
    story = pymupdf.Story(html=html, user_css="td{border:1px solid black;padding:4px} table{border-collapse:collapse}")
    out = tmp_path / "Invoice.pdf"
    w = pymupdf.DocumentWriter(str(out))
    rect = pymupdf.paper_rect("a4")
    more = True
    while more:
        dev = w.begin_page(rect)
        more, _ = story.place(rect + (50, 50, -50, -50))
        story.draw(dev)
        w.end_page()
    w.close()
    doc = pymupdf.open(out)
    buf = io.BytesIO()
    photo(300, 200).save(buf, "PNG")
    page = doc.new_page(width=rect.width, height=rect.height)
    page.insert_text((72, 72), "Page two text", fontsize=12)
    page.insert_image(pymupdf.Rect(72, 100, 372, 300), stream=buf.getvalue())
    doc.saveIncr()
    return out


def pdf_text(path) -> str:
    return "\n".join(p.get_text() for p in pymupdf.open(path))


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


@pytest.fixture
def builtin_only(monkeypatch):
    """Force the built-in Word renderer (no Word / Pages / LibreOffice)."""
    for name in ("_word_to_pdf", "_pages_to_pdf", "_libreoffice_to_pdf"):
        monkeypatch.setattr(convert, name, lambda *a: False)


# ---------------------------------------------------------------- Word → PDF
def test_docx_to_pdf_builtin_keeps_content_in_order(word_doc, builtin_only):
    pdf = convert.convert(word_doc, "docx", "PDF")[0]
    text = norm(pdf_text(pdf))
    order = ["Quarterly Report", "Revenue grew strongly this quarter, in red 14pt.", "Centred line", "Steps",
             "First step", "Second step", "Third step", "Apples", "Pears", "R0C0", "R2C2",
             "After the table.", "Last paragraph."]
    pos = [text.find(s) for s in order]
    assert all(p >= 0 for p in pos), [s for s, p in zip(order, pos) if p < 0]
    assert pos == sorted(pos), "content out of order"


def test_docx_to_pdf_builtin_numbered_list_and_image(word_doc, builtin_only):
    pdf = convert.convert(word_doc, "docx", "PDF")[0]
    text = pdf_text(pdf)
    assert re.search(r"1\.\s*First step", text) and re.search(r"3\.\s*Third step", text), "numbering lost"
    assert not re.search(r"\d\.\s*Apples", text), "bullet list got numbers"
    imgs = [im for p in pymupdf.open(pdf) for im in p.get_images()]
    assert imgs, "embedded picture dropped"


@pytest.mark.parametrize("paper,size", [("a4", (595, 842)), ("letter", (612, 792))])
def test_docx_to_pdf_builtin_paper_size(word_doc, builtin_only, monkeypatch, paper, size):
    monkeypatch.setattr(convert, "paper", lambda: paper)
    page = pymupdf.open(convert.convert(word_doc, "docx", "PDF")[0])[0]
    assert (round(page.rect.width), round(page.rect.height)) == size


@pytest.mark.office
@pytest.mark.skipif(not Path("/Applications/Pages.app").exists(), reason="Pages not installed")
def test_docx_to_pdf_with_pages_full_fidelity(word_doc):
    if convert.automation_permission(convert.PAGES_ID, ask=False) != 0:
        pytest.skip("not allowed to control Pages: System Settings → Privacy & Security → Automation")
    pdf = convert.convert(word_doc, "docx", "PDF")[0]
    d = pymupdf.open(pdf)
    assert "mupdf" not in (d.metadata.get("producer") or "").lower(), "fell back to the built-in renderer"
    text = norm(pdf_text(pdf))
    for s in ("Quarterly Report", "strongly", "R1C1", "Last paragraph.", "Third step"):
        assert s in text
    # the page setup comes from the document itself (python-docx's default is US Letter)
    assert (round(d[0].rect.width), round(d[0].rect.height)) == (612, 792)
    assert any(p.get_images() for p in d), "picture missing"
    fonts = {f[3] for p in d for f in p.get_fonts()}
    assert len(fonts) >= 2, "expected real document fonts, got " + str(fonts)


def test_broken_docx_fails_fast_with_clear_message(tmp_path):
    from dragshift.util import UserError
    p = tmp_path / "broken.docx"
    p.write_bytes(b"PK\x03\x04 truncated")
    with pytest.raises(UserError, match="readable Word document"):
        convert.convert(p, "docx", "PDF")


@pytest.mark.parametrize("first,after_asking,launched", [(-1743, None, False),   # declined earlier
                                                          (-1744, -1743, False),  # asked now: "Don't Allow"
                                                          (-1744, 0, True)])      # asked now: "OK"
def test_pages_permission_flow(word_doc, tmp_path, monkeypatch, first, after_asking, launched):
    import subprocess
    from conftest import REAL_PAGES_TO_PDF
    if not Path("/Applications/Pages.app").exists():
        pytest.skip("Pages not installed")
    asked = []

    def perm(bundle_id, ask):
        asked.append(ask)
        return after_asking if ask else first
    calls = []
    monkeypatch.setattr(convert, "automation_permission", perm)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a) or
                        subprocess.CompletedProcess(a, 1, "", "stub"))
    assert REAL_PAGES_TO_PDF(word_doc, tmp_path / "o.pdf") is False  # stub "fails" the export
    assert bool(calls) == launched, "talked to Pages without permission" if calls else "never tried Pages"
    assert asked == ([False] if first == -1743 else [False, True]), "prompted again after a 'no'"


def test_docx_to_txt_keeps_table_in_place(word_doc):
    txt = convert.convert(word_doc, "docx", "TXT")[0].read_text("utf-8")
    assert txt.index("Pears") < txt.index("R0C0\tR0C1\tR0C2") < txt.index("After the table.")


# ---------------------------------------------------------------- PDF → Word / text / images
def test_pdf_to_docx_keeps_table_text_and_image(table_pdf):
    out = convert.convert(table_pdf, "pdf", "DOCX")[0]
    d = Document(out)
    body = norm(" ".join(p.text for p in d.paragraphs))
    assert "Invoice 2026-17" in body and "Total due: 45.50" in body and "Page two text" in body
    assert d.tables, "table became loose text"
    cells = [[norm(c.text) for c in r.cells] for r in d.tables[0].rows]
    assert ["Item", "Qty", "Price"] in cells and ["Gadget", "1", "25.50"] in cells
    assert d.inline_shapes or any("graphic" in r.element.xml for r in d.element.body.iter()
                                  if hasattr(r, "element")) or "pic:pic" in d.element.xml, "image dropped"


def test_pdf_to_docx_falls_back_when_layout_engine_fails(table_pdf, monkeypatch):
    import pdf2docx

    class Boom:
        def __init__(self, *a, **k):
            raise RuntimeError("layout failed")
    monkeypatch.setattr(pdf2docx, "Converter", Boom)
    out = convert.convert(table_pdf, "pdf", "DOCX")[0]
    assert "Total due: 45.50" in " ".join(p.text for p in Document(out).paragraphs)
    assert not list(table_pdf.parent.glob(".~*"))


def test_pdf_to_txt(table_pdf):
    txt = convert.convert(table_pdf, "pdf", "TXT")[0].read_text("utf-8")
    assert "Invoice 2026-17" in txt and "Page two text" in txt
    assert txt.count("\f") == 1  # page break between the two pages


@pytest.mark.parametrize("target,dpi", [("PNG", 200), ("JPG", 200), ("PNG", 300), ("JPG", 150)])
def test_pdf_to_images_resolution_and_names(table_pdf, target, dpi):
    from dragshift.util import CONFIG
    CONFIG["pdf_dpi"] = dpi
    outs = convert.convert(table_pdf, "pdf", target)
    assert [o.name for o in outs] == [f"Invoice page {i}.{target.lower()}" for i in (1, 2)]
    im = Image.open(outs[0])
    assert im.size == (round(595.28 * dpi / 72), round(841.89 * dpi / 72)) or \
        abs(im.width - 595 * dpi / 72) <= 2


def test_single_page_pdf_has_no_page_suffix(tmp_path):
    d = pymupdf.open()
    d.new_page().insert_text((72, 72), "hi")
    d.save(tmp_path / "One.pdf")
    assert convert.convert(tmp_path / "One.pdf", "pdf", "PNG")[0].name == "One.png"


def test_password_pdf_gives_clear_message(tmp_path):
    from dragshift.util import UserError
    d = pymupdf.open()
    d.new_page()
    d.save(tmp_path / "Locked.pdf", encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="pw", owner_pw="pw")
    for target in convert.PDF_TARGETS:
        with pytest.raises(UserError, match="password"):
            convert.convert(tmp_path / "Locked.pdf", "pdf", target)


# ---------------------------------------------------------------- text
SAMPLE = "Café crème, naïve résumé — “quotes” €5\nSecond line\n"


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "utf-32",
                                      "cp1252"])
def test_text_encodings_decode(tmp_path, encoding):
    p = tmp_path / f"t-{encoding}.txt"
    p.write_bytes(SAMPLE.encode(encoding))
    assert convert.read_text(p) == SAMPLE


def test_latin1_only_bytes(tmp_path):
    p = tmp_path / "l.txt"
    p.write_bytes("Ñandú\n".encode("latin-1"))
    assert convert.read_text(p) == "Ñandú\n"


def test_text_to_pdf_loses_nothing(tmp_path):
    lines = [f"Line {i}: " + "word " * (i % 40) for i in range(1500)] + ["x" * 3000]
    src = tmp_path / "long.txt"
    src.write_text("\n".join(lines), "utf-8")
    pdf = convert.convert(src, "text", "PDF")[0]
    got = re.sub(r"[\s\u200b]", "", pdf_text(pdf))
    assert got == re.sub(r"\s", "", "\n".join(lines)), "text cut off or lost"


def test_text_to_pdf_non_latin(tmp_path):
    src = tmp_path / "intl.txt"
    s = "日本語 中文 한국어 Ελληνικά Привет עברית"
    src.write_text(s, "utf-8")
    assert norm(pdf_text(convert.convert(src, "text", "PDF")[0])) == s


def test_text_to_docx(tmp_path):
    src = tmp_path / "notes.md"
    src.write_bytes(SAMPLE.encode("cp1252"))
    d = Document(convert.convert(src, "text", "DOCX")[0])
    assert [p.text for p in d.paragraphs] == SAMPLE.splitlines()
