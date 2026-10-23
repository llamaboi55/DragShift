"""Everything that isn't an image, audio or video: Office files, spreadsheets, slides, e-books,
Markdown / HTML, data files, SVG, archives and subtitles.

Same contract as convert.py: every converter takes a source path and returns the output paths
written next to it. Each category has a route table (target -> function). Routes that need
LibreOffice or FFmpeg are only offered when that tool is installed, and fall back to pure-Python
paths where one exists (so DOCX, XLSX, PPTX, ODT, RTF, EPUB, Markdown, HTML, CSV, JSON and
archives work with nothing extra installed)."""
from __future__ import annotations

import csv
import html
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable, NamedTuple

from . import convert as cv
from .util import UserError, output_dir, sibling

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# ---------------------------------------------------------------- what each extension is
EXT_CATEGORY = {
    # Word processors
    ".doc": "word", ".odt": "word", ".rtf": "word", ".dot": "word", ".wpd": "word",
    # presentations
    ".pptx": "slides", ".ppt": "slides", ".odp": "slides", ".ppsx": "slides", ".pps": "slides",
    # spreadsheets and tabular data
    ".xlsx": "sheet", ".xlsm": "sheet", ".xls": "sheet", ".ods": "sheet",
    ".csv": "sheet", ".tsv": "sheet", ".json": "sheet",
    # markup
    ".md": "markdown", ".markdown": "markdown",
    ".html": "html", ".htm": "html", ".xhtml": "html",
    # e-books and page-based documents
    ".epub": "ebook", ".mobi": "ebook", ".fb2": "ebook", ".xps": "ebook", ".oxps": "ebook",
    ".cbz": "ebook",
    ".svg": "svg",
    # archives
    ".zip": "archive", ".tar": "archive", ".tgz": "archive", ".tbz2": "archive", ".txz": "archive",
    # subtitles
    ".srt": "subtitle", ".vtt": "subtitle", ".ass": "subtitle", ".ssa": "subtitle",
}
ARCHIVE_SUFFIXES = (".tar.gz", ".tar.bz2", ".tar.xz")
# what the user sees: ".markdown" is MD, ".htm" is HTML…
ALIAS = {"MARKDOWN": "MD", "HTM": "HTML", "XHTML": "HTML", "XLSM": "XLSX", "PPSX": "PPTX",
         "PPS": "PPT", "OXPS": "XPS", "TGZ": "TAR.GZ", "TBZ2": "TAR.BZ2", "TXZ": "TAR.XZ",
         "DOT": "DOC", "SSA": "ASS"}


def full_suffix(path: str | Path) -> str:
    """'.tar.gz' for 'a.tar.gz', otherwise the usual last suffix."""
    name = Path(path).name.lower()
    return next((s for s in ARCHIVE_SUFFIXES if name.endswith(s)), Path(path).suffix)


def category_for(path: str | Path) -> str | None:
    name = Path(path).name.lower()
    if name.endswith(ARCHIVE_SUFFIXES):
        return "archive"
    return EXT_CATEGORY.get(Path(path).suffix.lower())


def source_format(path_or_ext: str | Path) -> str:
    """Upper-case format name of a source: 'a.tar.gz' -> 'TAR.GZ', '.htm' -> 'HTML'."""
    s = str(path_or_ext).lower()
    if "/" not in s and "\\" not in s:
        s = "." + s.lstrip(".")      # 'tar.gz' and '.tar.gz' both mean the same
    for suf in ARCHIVE_SUFFIXES:
        if s.endswith(suf):
            return suf[1:].upper()
    ext = (Path(s).suffix or s).lstrip(".").upper()
    return ALIAS.get(ext, ext)


# ---------------------------------------------------------------- LibreOffice
@lru_cache(maxsize=1)
def find_soffice() -> str | None:
    env = os.environ.get("DRAGSHIFT_SOFFICE")
    if env and os.path.isfile(env):
        return env
    for name in ("soffice", "libreoffice"):
        exe = shutil.which(name)
        if exe:
            return exe
    candidates = []
    if sys.platform == "win32":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")):
            if base:
                candidates.append(Path(base) / "LibreOffice" / "program" / "soffice.exe")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/LibreOffice.app/Contents/MacOS/soffice"))
    return next((str(c) for c in candidates if c.is_file()), None)


def lo_available() -> bool:
    return find_soffice() is not None


_LO_LOCK = threading.Lock()  # one LibreOffice at a time: they share a profile


def _lo_run(src: Path, fmt: str, ext: str, outdir: Path) -> Path:
    """Convert with a headless LibreOffice into `outdir` and return the file it wrote."""
    exe = find_soffice()
    if not exe:
        raise UserError("This conversion needs LibreOffice. Install it from libreoffice.org.")
    profile = Path(tempfile.gettempdir()) / "dragshift-lo-profile"  # never touches the user's own profile
    cmd = [exe, "--headless", "--norestore", "--nolockcheck", "--nologo", "--nodefault",
           f"-env:UserInstallation={profile.as_uri()}", "--convert-to", fmt, "--outdir", str(outdir),
           str(src)]
    with _LO_LOCK:
        try:
            subprocess.run(cmd, capture_output=True, timeout=300, creationflags=_NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise UserError("LibreOffice took too long.")
    made = sorted(outdir.glob("*" + ext))
    if not made:
        raise UserError("LibreOffice couldn't convert this file.")
    return made[0]


def lo_convert(src: Path, fmt: str, ext: str, out: Path) -> Path:
    with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
        made = _lo_run(src, fmt, ext, Path(td))
        _publish(lambda dst: shutil.copyfile(made, dst), out)
    return out


def _publish(write: Callable, out: Path) -> None:
    cv.atomic_save(write, out)


# ---------------------------------------------------------------- HTML -> blocks -> md / txt / docx
Run = tuple  # (text, bold, italic, code, href)


class _Blocks(HTMLParser):
    """Reads HTML into a flat list of blocks: ('h', level, runs) ('p', runs) ('li', depth, number|None,
    runs) ('pre', text) ('table', rows) ('img', src, alt) ('hr',)."""
    SKIP = {"script", "style", "head", "title", "noscript", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.blocks: list[tuple] = []
        self.runs: list[Run] = []
        self.cur: tuple = ("p",)
        self.b = self.i = self.code = 0
        self.href: str | None = None
        self.skip = 0
        self.lists: list[list] = []   # [ordered, counter]
        self.pre: list[str] | None = None
        self.table: list[list[str]] | None = None
        self.row: list[str] | None = None
        self.cell: list[str] | None = None

    def flush(self):
        if self.runs and any(r[0].strip() for r in self.runs):
            runs = self.runs
            while runs and not runs[0][0].strip():
                runs = runs[1:]
            runs = [(runs[0][0].lstrip(),) + runs[0][1:]] + runs[1:]
            kind = self.cur[0]
            if kind == "h":
                self.blocks.append(("h", self.cur[1], runs))
            elif kind == "li":
                self.blocks.append(("li", self.cur[1], self.cur[2], runs))
            else:
                self.blocks.append(("p", runs))
        self.runs = []
        self.cur = ("p",)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
            return
        if self.skip:
            return
        if tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag == "table":
            self.flush()
            self.table = []
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.cur = ("h", int(tag[1]))
        elif tag in ("p", "div", "section", "article", "blockquote", "header", "footer", "figure", "tr"):
            self.flush()
        elif tag in ("ul", "ol"):
            self.flush()
            self.lists.append([tag == "ol", 0])
        elif tag == "li":
            self.flush()
            depth = max(0, len(self.lists) - 1)
            num = None
            if self.lists and self.lists[-1][0]:
                self.lists[-1][1] += 1
                num = self.lists[-1][1]
            self.cur = ("li", depth, num)
        elif tag == "pre":
            self.flush()
            self.pre = []
        elif tag in ("b", "strong"):
            self.b += 1
        elif tag in ("i", "em"):
            self.i += 1
        elif tag in ("code", "tt", "kbd", "samp"):
            self.code += 1
        elif tag == "a":
            self.href = a.get("href")
        elif tag == "br":
            if self.cell is not None:
                self.cell.append(" ")
            else:
                self.runs.append(("\n", 0, 0, 0, None))
        elif tag == "img":
            self.flush()
            self.blocks.append(("img", a.get("src") or "", a.get("alt") or ""))
        elif tag == "hr":
            self.flush()
            self.blocks.append(("hr",))

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(re.sub(r"\s+", " ", "".join(self.cell)).strip())
            self.cell = None
        elif tag == "tr" and self.row is not None and self.table is not None:
            if self.row:
                self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            if self.table:
                w = max(len(r) for r in self.table)
                self.blocks.append(("table", [r + [""] * (w - len(r)) for r in self.table]))
            self.table = None
        elif re.fullmatch(r"h[1-6]", tag) or tag in ("p", "div", "section", "article", "blockquote",
                                                     "header", "footer", "figure", "li"):
            self.flush()
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
        elif tag == "pre" and self.pre is not None:
            self.blocks.append(("pre", "".join(self.pre).strip("\n")))
            self.pre = None
        elif tag in ("b", "strong"):
            self.b = max(0, self.b - 1)
        elif tag in ("i", "em"):
            self.i = max(0, self.i - 1)
        elif tag in ("code", "tt", "kbd", "samp"):
            self.code = max(0, self.code - 1)
        elif tag == "a":
            self.href = None

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre is not None:
            self.pre.append(data)
        elif self.cell is not None:
            self.cell.append(data)
        elif self.table is not None:
            return
        else:
            text = re.sub(r"[ \t\r\n]+", " ", data)
            if text:
                self.runs.append((text, self.b, self.i, self.code, self.href))

    def close(self):
        super().close()
        self.flush()


def html_blocks(markup: str) -> list[tuple]:
    p = _Blocks()
    p.feed(markup)
    p.close()
    return p.blocks


def _plain(runs: list[Run]) -> str:
    return "".join(r[0] for r in runs).strip()


def _md_runs(runs: list[Run]) -> str:
    out = []
    for text, b, i, code, href in runs:
        if not text.strip():
            out.append(text)
            continue
        lead, core, trail = text[:len(text) - len(text.lstrip())], text.strip(), text[len(text.rstrip()):]
        if code:
            core = f"`{core}`"
        if b:
            core = f"**{core}**"
        if i:
            core = f"*{core}*"
        if href:
            core = f"[{core}]({href})"
        out.append(lead + core + trail)
    return "".join(out).strip()


def _md_table(rows: list[list]) -> str:
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    head, *body = rows
    lines = ["| " + " | ".join(esc(c) for c in head) + " |",
             "| " + " | ".join("---" for _ in head) + " |"]
    lines += ["| " + " | ".join(esc(c) for c in r) + " |" for r in body]
    return "\n".join(lines)


def blocks_to_md(blocks: list[tuple]) -> str:
    out: list[str] = []
    prev_li = False
    for blk in blocks:
        kind = blk[0]
        if kind == "li":
            _, depth, num, runs = blk
            bullet = f"{num}. " if num else "- "
            out.append("  " * depth + bullet + _md_runs(runs))
            prev_li = True
            continue
        if prev_li:
            out.append("")
            prev_li = False
        if kind == "h":
            out.append("#" * blk[1] + " " + _md_runs(blk[2]))
        elif kind == "p":
            out.append(_md_runs(blk[1]))
        elif kind == "pre":
            out.append("```\n" + blk[1] + "\n```")
        elif kind == "table":
            out.append(_md_table(blk[1]))
        elif kind == "img":
            out.append(f"![{blk[2]}]({blk[1]})")
        elif kind == "hr":
            out.append("---")
        out.append("")
    return "\n".join(out).strip() + "\n"


def blocks_to_txt(blocks: list[tuple]) -> str:
    out: list[str] = []
    prev_li = False
    for blk in blocks:
        kind = blk[0]
        if kind == "li":
            out.append("  " * blk[1] + (f"{blk[2]}. " if blk[2] else "• ") + _plain(blk[3]))
            prev_li = True
            continue
        if prev_li:
            out.append("")
            prev_li = False
        if kind in ("h", "p"):
            out += [_plain(blk[-1]), ""]
        elif kind == "pre":
            out += [blk[1], ""]
        elif kind == "table":
            out += ["\t".join(r) for r in blk[1]] + [""]
        elif kind == "img" and blk[2]:
            out += [f"[{blk[2]}]", ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def blocks_to_docx(blocks: list[tuple], out: Path, base: Path | None = None) -> None:
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor
    d = Document()

    def add_runs(par, runs):
        for text, b, i, code, href in runs:
            if text == "\n":
                par.add_run().add_break()
                continue
            r = par.add_run(text)
            r.bold = bool(b) or None
            r.italic = bool(i) or None
            if code:
                r.font.name = "Consolas"
                r.font.size = Pt(9.5)
            if href:
                r.underline = True
                r.font.color.rgb = RGBColor(0x05, 0x63, 0xC1)

    for blk in blocks:
        kind = blk[0]
        if kind == "h":
            p = d.add_heading(level=min(blk[1], 4))
            add_runs(p, blk[2])
        elif kind == "p":
            add_runs(d.add_paragraph(), blk[1])
        elif kind == "li":
            style = "List Number" if blk[2] else "List Bullet"
            if blk[1] >= 1:
                style += f" {min(blk[1] + 1, 3)}"
            try:
                p = d.add_paragraph(style=style)
            except KeyError:
                p = d.add_paragraph(style="List Bullet")
            add_runs(p, blk[3])
        elif kind == "pre":
            p = d.add_paragraph()
            r = p.add_run(blk[1])
            r.font.name = "Consolas"
            r.font.size = Pt(9.5)
        elif kind == "table":
            rows = blk[1]
            t = d.add_table(rows=len(rows), cols=len(rows[0]))
            t.style = "Table Grid"
            for y, row in enumerate(rows):
                for x, val in enumerate(row):
                    t.cell(y, x).text = str(val)
        elif kind == "img" and base is not None:
            src = Path(base) / blk[1]
            if blk[1] and not re.match(r"^[a-z]+:", blk[1]) and src.is_file():
                try:
                    d.add_picture(str(src), width=Inches(5.5))
                except Exception:
                    pass
    cv.atomic_save(lambda dst: d.save(dst), out)


def _wrap_html(body: str, title: str = "") -> str:
    return ("<!doctype html>\n<html><head><meta charset=\"utf-8\">"
            f"<title>{html.escape(title)}</title>\n<style>\n"
            "body{font-family:system-ui,sans-serif;max-width:46rem;margin:2rem auto;padding:0 1rem;"
            "line-height:1.5}table{border-collapse:collapse}td,th{border:1px solid #999;padding:4px 8px}"
            "pre{background:#f4f4f4;padding:.6rem;overflow:auto}code{font-family:monospace}\n"
            f"</style></head>\n<body>\n{body}\n</body></html>\n")


def _write_text(out: Path, text: str, encoding: str = "utf-8") -> None:
    _publish(lambda dst: Path(dst).write_text(text, encoding=encoding, newline=""), out)


# ---------------------------------------------------------------- Markdown / HTML / plain text
def _md_to_html_body(src: Path) -> str:
    import markdown
    return markdown.markdown(cv.read_text(src), extensions=["tables", "fenced_code", "sane_lists", "nl2br"])


def _markup_html(src: Path, cat: str) -> str:
    """The document as an HTML string, whatever it started as."""
    if cat == "markdown":
        return _md_to_html_body(src)
    if cat == "html":
        return cv.read_text(src)
    return "<p>" + "</p><p>".join(html.escape(p).replace("\n", "<br>")
                                  for p in re.split(r"\n\s*\n", cv.read_text(src))) + "</p>"


def markup_to_pdf(src: Path, cat: str) -> list[Path]:
    out = sibling(src, ext=".pdf")
    cv.html_to_pdf(_markup_html(src, cat), out, base=src.parent)
    return [out]


def markup_to_html(src: Path, cat: str) -> list[Path]:
    out = sibling(src, ext=".html")
    body = _md_to_html_body(src) if cat == "markdown" else _markup_html(src, cat)
    _write_text(out, _wrap_html(body, src.stem))
    return [out]


def markup_to_docx(src: Path, cat: str) -> list[Path]:
    out = sibling(src, ext=".docx")
    blocks_to_docx(html_blocks(_markup_html(src, cat)), out, base=src.parent)
    return [out]


def markup_to_txt(src: Path, cat: str) -> list[Path]:
    out = sibling(src, ext=".txt")
    _write_text(out, blocks_to_txt(html_blocks(_markup_html(src, cat))))
    return [out]


def html_to_md(src: Path, cat: str = "html") -> list[Path]:
    out = sibling(src, ext=".md")
    _write_text(out, blocks_to_md(html_blocks(cv.read_text(src))))
    return [out]


def text_to_html(src: Path, cat: str = "text") -> list[Path]:
    return markup_to_html(src, "text")


# ---------------------------------------------------------------- DOCX extras (HTML / Markdown)
def docx_to_htmlfile(src: Path, cat: str = "docx") -> list[Path]:
    out = sibling(src, ext=".html")
    _write_text(out, _wrap_html(cv.docx_to_html(src), src.stem))
    return [out]


def docx_to_md(src: Path, cat: str = "docx") -> list[Path]:
    out = sibling(src, ext=".md")
    _write_text(out, blocks_to_md(html_blocks(cv.docx_to_html(src))))
    return [out]


def lo_route(fmt: str, ext: str) -> Callable:
    def go(src: Path, cat: str) -> list[Path]:
        out = sibling(src, ext=ext)
        lo_convert(src, fmt, ext, out)
        return [out]
    return go


# ---------------------------------------------------------------- PDF extras
def pdf_to_html(src: Path, cat: str = "pdf") -> list[Path]:
    with cv.open_pdf(src) as doc:
        body = "\n".join(page.get_text("html") for page in doc)
    out = sibling(src, ext=".html")
    _write_text(out, _wrap_html(body, src.stem))
    return [out]


def pdf_to_svg(src: Path, cat: str = "pdf") -> list[Path]:
    outs = []
    with cv.open_pdf(src) as doc:
        n = doc.page_count
        for i, page in enumerate(doc):
            out = sibling(src, f"page {i + 1}" if n > 1 else "", ".svg")
            _write_text(out, page.get_svg_image(text_as_path=False))
            outs.append(out)
    return outs


# ---------------------------------------------------------------- Word-like files (DOC, ODT, RTF)
def _odt_text(path: Path) -> str:
    import xml.etree.ElementTree as ET
    with zipfile.ZipFile(path) as z:
        root = ET.fromstring(z.read("content.xml"))
    ns = {"t": "urn:oasis:names:tc:opendocument:xmlns:text:1.0"}
    paras = []
    for el in root.iter():
        if el.tag in ("{%s}p" % ns["t"], "{%s}h" % ns["t"]):
            paras.append("".join(el.itertext()))
    return "\n".join(paras)


def plain_text(src: Path) -> str:
    """Text of a word-processor file with no office suite: ODT and RTF only."""
    ext = src.suffix.lower()
    if ext == ".odt":
        return _odt_text(src)
    if ext == ".rtf":
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(src.read_bytes().decode("latin-1"))
    raise UserError("This format needs LibreOffice. Install it from libreoffice.org.")


def word_to_txt(src: Path, cat: str = "word") -> list[Path]:
    out = sibling(src, ext=".txt")
    if lo_available() and src.suffix.lower() not in (".odt", ".rtf"):
        lo_convert(src, "txt:Text (encoded):UTF8", ".txt", out)
    else:
        _write_text(out, plain_text(src))
    return [out]


def word_to_pdf(src: Path, cat: str = "word") -> list[Path]:
    out = sibling(src, ext=".pdf")
    if lo_available():
        lo_convert(src, "pdf", ".pdf", out)
    else:
        cv.html_to_pdf(f"<pre>{html.escape(plain_text(src))}</pre>", out)
    return [out]


def word_to_docx(src: Path, cat: str = "word") -> list[Path]:
    out = sibling(src, ext=".docx")
    if lo_available():
        lo_convert(src, "docx", ".docx", out)
    else:
        from docx import Document
        d = Document()
        for line in plain_text(src).splitlines():
            d.add_paragraph(line)
        cv.atomic_save(lambda dst: d.save(dst), out)
    return [out]


def word_to_html(src: Path, cat: str = "word") -> list[Path]:
    with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
        docx = _lo_run(src, "docx", ".docx", Path(td))
        out = sibling(src, ext=".html")
        _write_text(out, _wrap_html(cv.docx_to_html(docx), src.stem))
    return [out]


# ---------------------------------------------------------------- spreadsheets and data
Sheets = list[tuple[str, list[list]]]
_INT = re.compile(r"-?(0|[1-9]\d{0,14})")
_FLOAT = re.compile(r"-?(0|[1-9]\d*)\.\d+")


def _infer(v):
    if isinstance(v, str):
        s = v.strip()
        if _INT.fullmatch(s):
            return int(s)
        if _FLOAT.fullmatch(s) and len(s) < 18:
            return float(s)
    return v


def _cell_text(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    if hasattr(v, "isoformat"):
        return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
    return str(v)


def _read_delimited(src: Path) -> list[list]:
    text = cv.read_text(src)
    if src.suffix.lower() == ".tsv":
        delim = "\t"
    else:
        try:
            delim = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delim = ","
    return [row for row in csv.reader(io.StringIO(text, newline=""), delimiter=delim)]


def _json_rows(data) -> list[list]:
    if isinstance(data, dict):
        if data and all(isinstance(v, list) for v in data.values()):  # columns
            keys = list(data)
            n = max(len(v) for v in data.values())
            return [keys] + [[data[k][i] if i < len(data[k]) else "" for k in keys] for i in range(n)]
        if data and all(isinstance(v, dict) for v in data.values()):  # keyed records
            data = [{"key": k, **v} for k, v in data.items()]
        else:
            return [["key", "value"]] + [[k, json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v]
                                         for k, v in data.items()]
    if isinstance(data, list):
        if data and all(isinstance(r, dict) for r in data):
            keys: list = []
            for r in data:
                keys += [k for k in r if k not in keys]
            return [keys] + [[json.dumps(r[k], ensure_ascii=False) if isinstance(r.get(k), (dict, list))
                              else r.get(k, "") for k in keys] for r in data]
        if data and all(isinstance(r, list) for r in data):
            return [list(r) for r in data]
        return [["value"]] + [[json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v]
                              for v in data]
    raise UserError("This JSON isn't a table: it needs a list of records.")


def load_sheets(src: Path) -> Sheets:
    ext = src.suffix.lower()
    if ext in (".csv", ".tsv"):
        return [(src.stem, _read_delimited(src))]
    if ext == ".json":
        try:
            return [(src.stem, _json_rows(json.loads(cv.read_text(src))))]
        except json.JSONDecodeError as e:
            raise UserError(f"This isn't valid JSON ({e.msg} at line {e.lineno}).")
    if ext in (".xls", ".ods"):
        with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
            return load_sheets(_lo_run(src, "xlsx", ".xlsx", Path(td)))
    import openpyxl
    try:
        wb = openpyxl.load_workbook(src, read_only=True, data_only=True)
    except Exception:
        raise UserError("Couldn't read this spreadsheet. It may be damaged or password-protected.")
    sheets = []
    try:
        for ws in wb.worksheets:
            rows = [[_cell_text(c) if isinstance(c, (str, type(None))) or hasattr(c, "isoformat") else c
                     for c in row] for row in ws.iter_rows(values_only=True)]
            while rows and not any(str(c) != "" for c in rows[-1]):
                rows.pop()
            if rows:
                sheets.append((ws.title, rows))
    finally:
        wb.close()
    if not sheets:
        raise UserError("This spreadsheet is empty.")
    return sheets


def _rect(rows: list[list]) -> list[list]:
    w = max((len(r) for r in rows), default=0)
    return [list(r) + [""] * (w - len(r)) for r in rows]


def _named(src: Path, sheets: Sheets, ext: str, i: int) -> Path:
    if len(sheets) == 1:
        return sibling(src, ext=ext)
    return sibling(src, re.sub(r'[\\/:*?"<>|]', "_", sheets[i][0]), ext)


def sheet_to_delimited(delim: str, ext: str) -> Callable:
    def go(src: Path, cat: str = "sheet") -> list[Path]:
        sheets = load_sheets(src)
        outs = []
        for i, (_, rows) in enumerate(sheets):
            out = _named(src, sheets, ext, i)

            def write(dst, rows=rows):
                with open(dst, "w", encoding="utf-8-sig", newline="") as f:  # BOM: Excel reads UTF-8 right
                    w = csv.writer(f, delimiter=delim)
                    for r in rows:
                        w.writerow([_cell_text(c) for c in r])
            _publish(write, out)
            outs.append(out)
        return outs
    return go


def sheet_to_json(src: Path, cat: str = "sheet") -> list[Path]:
    sheets = load_sheets(src)
    outs = []
    for i, (_, rows) in enumerate(sheets):
        rows = _rect(rows)
        head = [str(h) if str(h) != "" else f"column{n + 1}" for n, h in enumerate(rows[0])]
        data = [{h: _infer(v) for h, v in zip(head, r)} for r in rows[1:]]
        out = _named(src, sheets, ".json", i)
        _write_text(out, json.dumps(data, ensure_ascii=False, indent=2, default=str) + "\n")
        outs.append(out)
    return outs


def _workbook(src: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter
    sheets = load_sheets(src)
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets:
        ws = wb.create_sheet(re.sub(r"[\\/*?:\[\]]", "_", name)[:31] or "Sheet")
        for r in rows:
            ws.append([_infer(c) for c in r])
        if ws.max_row > 1 and src.suffix.lower() not in (".xls", ".ods", ".xlsx", ".xlsm"):
            for c in ws[1]:
                c.font = Font(bold=True)
            ws.freeze_panes = "A2"
        for col in range(1, ws.max_column + 1):
            width = max((len(str(ws.cell(row=r, column=col).value or "")) for r in range(1, min(ws.max_row, 200) + 1)),
                        default=8)
            ws.column_dimensions[get_column_letter(col)].width = min(60, max(8, width + 2))
    return wb


def sheet_to_xlsx(src: Path, cat: str = "sheet") -> list[Path]:
    wb = _workbook(src)
    out = sibling(src, ext=".xlsx")
    cv.atomic_save(lambda dst: wb.save(dst), out)
    return [out]


def sheet_via_xlsx(fmt: str, ext: str) -> Callable:
    """ODS / XLS: write an XLSX, then let LibreOffice turn it into the target."""
    def go(src: Path, cat: str = "sheet") -> list[Path]:
        out = sibling(src, ext=ext)
        with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
            tmp_src = Path(td) / (src.stem + ".xlsx")
            if src.suffix.lower() in (".xlsx", ".xlsm"):
                shutil.copyfile(src, tmp_src)
            else:
                _workbook(src).save(tmp_src)
            lo_convert(tmp_src, fmt, ext, out)
        return [out]
    return go


def _sheet_html(sheets: Sheets, title: str) -> str:
    parts = []
    for name, rows in sheets:
        rows = _rect(rows)
        head, body = rows[0], rows[1:]
        t = "<table><thead><tr>" + "".join(f"<th>{html.escape(_cell_text(c))}</th>" for c in head) + "</tr></thead><tbody>"
        t += "".join("<tr>" + "".join(f"<td>{html.escape(_cell_text(c))}</td>" for c in r) + "</tr>" for r in body)
        parts.append((f"<h2>{html.escape(name)}</h2>" if len(sheets) > 1 else "") + t + "</tbody></table>")
    return "\n".join(parts)


def sheet_to_html(src: Path, cat: str = "sheet") -> list[Path]:
    out = sibling(src, ext=".html")
    _write_text(out, _wrap_html(_sheet_html(load_sheets(src), src.stem), src.stem))
    return [out]


def sheet_to_md(src: Path, cat: str = "sheet") -> list[Path]:
    sheets = load_sheets(src)
    text = "\n\n".join((f"## {n}\n\n" if len(sheets) > 1 else "") + _md_table([[_cell_text(c) for c in r] for r in _rect(rows)])
                       for n, rows in sheets) + "\n"
    out = sibling(src, ext=".md")
    _write_text(out, text)
    return [out]


def sheet_to_pdf(src: Path, cat: str = "sheet") -> list[Path]:
    sheets = load_sheets(src)
    out = sibling(src, ext=".pdf")
    wide = max(len(_rect(rows)[0]) for _, rows in sheets) > 6
    css = "table{font-size:8pt} td,th{padding:2pt 3pt}" if wide else ""
    cv.html_to_pdf([_sheet_html([s], src.stem) for s in sheets], out, landscape=wide, extra_css=css)
    return [out]


def sheet_to_docx(src: Path, cat: str = "sheet") -> list[Path]:
    sheets = load_sheets(src)
    blocks: list[tuple] = []
    for name, rows in sheets:
        if len(sheets) > 1:
            blocks.append(("h", 2, [(name, 0, 0, 0, None)]))
        blocks.append(("table", [[_cell_text(c) for c in r] for r in _rect(rows)]))
    out = sibling(src, ext=".docx")
    blocks_to_docx(blocks, out)
    return [out]


# ---------------------------------------------------------------- slides (PPTX / PPT / ODP)
def _walk_shapes(shapes):
    for sh in shapes:
        if sh.shape_type == 6:  # group
            yield from _walk_shapes(sh.shapes)
        else:
            yield sh


def _pptx_path(src: Path, td: Path) -> Path:
    if src.suffix.lower() in (".pptx", ".ppsx"):
        return src
    return _lo_run(src, "pptx", ".pptx", td)


def _slide_data(pptx: Path, imgdir: Path | None = None) -> list[dict]:
    from pptx import Presentation
    prs = Presentation(str(pptx))
    out = []
    for n, slide in enumerate(prs.slides, 1):
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None and \
            slide.shapes.title.has_text_frame else ""
        items, tables, images = [], [], []
        shapes = sorted(_walk_shapes(slide.shapes), key=lambda s: (s.top or 0, s.left or 0))
        for sh in shapes:
            if slide.shapes.title is not None and sh.shape_id == slide.shapes.title.shape_id:
                continue
            if sh.has_text_frame:
                for p in sh.text_frame.paragraphs:
                    t = "".join(r.text for r in p.runs).strip()
                    if t:
                        items.append((p.level, t))
            elif getattr(sh, "has_table", False) and sh.has_table:
                tables.append([[c.text.strip() for c in row.cells] for row in sh.table.rows])
            elif sh.shape_type == 13 and imgdir is not None:  # picture
                try:
                    ext = sh.image.ext.lower()
                    if ext in ("png", "jpg", "jpeg", "gif", "bmp", "tiff"):
                        p = imgdir / f"s{n}_{len(images)}.{ext}"
                        p.write_bytes(sh.image.blob)
                        images.append(p.name)
                except Exception:
                    pass
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        out.append({"n": n, "title": title, "items": items, "tables": tables, "images": images, "notes": notes})
    return out


def slides_to_txt(src: Path, cat: str = "slides") -> list[Path]:
    with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
        slides = _slide_data(_pptx_path(src, Path(td)))
    lines = []
    for s in slides:
        lines.append(f"Slide {s['n']}" + (f": {s['title']}" if s["title"] else ""))
        lines += ["  " * lvl + "• " + t for lvl, t in s["items"]]
        for tb in s["tables"]:
            lines += ["\t".join(r) for r in tb]
        if s["notes"]:
            lines.append("Notes: " + s["notes"])
        lines.append("")
    out = sibling(src, ext=".txt")
    _write_text(out, "\n".join(lines))
    return [out]


def _slides_fallback_pdf(src: Path, out: Path) -> None:
    """No LibreOffice: one page per slide with its title, text, tables and pictures (not a pixel copy)."""
    with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
        td = Path(td)
        slides = _slide_data(_pptx_path(src, td), td)
        pages = []
        for s in slides:
            h = f"<h1>{html.escape(s['title'] or 'Slide ' + str(s['n']))}</h1>"
            h += "".join(f"<p style='margin-left:{lvl * 16}pt'>&bull; {html.escape(t)}</p>" for lvl, t in s["items"])
            for tb in s["tables"]:
                h += "<table>" + "".join("<tr>" + "".join(f"<td>{html.escape(c)}</td>" for c in r) + "</tr>"
                                         for r in tb) + "</table>"
            h += "".join(f"<p><img src='{n}' width='320'></p>" for n in s["images"])
            if s["notes"]:
                h += f"<p><i>Notes: {html.escape(s['notes'])}</i></p>"
            pages.append(h)
        if not pages:
            raise UserError("This presentation has no slides.")
        cv.html_to_pdf(pages, out, base=td, landscape=True)


def slides_to_pdf(src: Path, cat: str = "slides") -> list[Path]:
    out = sibling(src, ext=".pdf")
    if lo_available():
        lo_convert(src, "pdf", ".pdf", out)
    else:
        _slides_fallback_pdf(src, out)
    return [out]


def slides_to_images(target: str) -> Callable:
    def go(src: Path, cat: str = "slides") -> list[Path]:
        with tempfile.TemporaryDirectory(prefix="dragshift-") as td:
            pdf = Path(td) / "slides.pdf"
            if lo_available():
                made = _lo_run(src, "pdf", ".pdf", Path(td))
                pdf = made
            else:
                _slides_fallback_pdf(src, pdf)
            outs = []
            with cv.open_pdf(pdf) as doc:
                n = doc.page_count
                for i, page in enumerate(doc):
                    pix = page.get_pixmap(dpi=150, alpha=False)
                    im = cv.Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                    out = sibling(src, f"slide {i + 1}" if n > 1 else "", "." + target.lower())
                    cv.save_image(im, out, target)
                    outs.append(out)
        return outs
    return go


# ---------------------------------------------------------------- e-books and page-based documents
def _open_doc(src: Path):
    try:
        return cv.fitz.open(str(src))
    except Exception:
        raise UserError("Couldn't open this file. It may be damaged, protected or not what its extension says.")


def ebook_to_pdf(src: Path, cat: str = "ebook") -> list[Path]:
    with _open_doc(src) as doc:
        data = doc.convert_to_pdf()
    out = sibling(src, ext=".pdf")
    _publish(lambda dst: Path(dst).write_bytes(data), out)
    return [out]


def ebook_to_txt(src: Path, cat: str = "ebook") -> list[Path]:
    with _open_doc(src) as doc:
        text = "\n\n".join(page.get_text("text") for page in doc)
    out = sibling(src, ext=".txt")
    _write_text(out, text)
    return [out]


def ebook_to_html(src: Path, cat: str = "ebook") -> list[Path]:
    with _open_doc(src) as doc:
        body = "\n".join(page.get_text("html") for page in doc)
    out = sibling(src, ext=".html")
    _write_text(out, _wrap_html(body, src.stem))
    return [out]


def svg_to_image(target: str) -> Callable:
    def go(src: Path, cat: str = "svg") -> list[Path]:
        if target == "PDF":
            return ebook_to_pdf(src)
        with _open_doc(src) as doc:
            page = doc[0]
            z = max(1.0, 1600 / max(page.rect.width, page.rect.height, 1))  # ~1600 px on the long side
            pix = page.get_pixmap(matrix=cv.fitz.Matrix(z, z), alpha=True)
        im = cv.Image.frombytes("RGBA", (pix.width, pix.height), pix.samples)
        out = sibling(src, ext="." + target.lower())
        cv.save_image(im, out, target)
        return [out]
    return go


# ---------------------------------------------------------------- archives
def _safe_name(name: str) -> str:
    n = name.replace("\\", "/")
    n = re.sub(r"^([A-Za-z]:)?/+", "", n)
    parts = [p for p in n.split("/") if p not in ("", ".")]
    if ".." in parts:
        raise UserError("This archive contains unsafe paths (..), so it wasn't converted.")
    return "/".join(parts)


def _iter_archive(src: Path):
    """Yield (name, is_dir, size, mtime, mode, open_fn) for each member, never touching the disk."""
    name = src.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(src) as z:
            for zi in z.infolist():
                if zi.flag_bits & 0x1:
                    raise UserError("This ZIP is password-protected.")
                mtime = time.mktime(zi.date_time + (0, 0, -1)) if zi.date_time[0] >= 1980 else 0
                mode = (zi.external_attr >> 16) & 0o777 or 0o644
                yield _safe_name(zi.filename), zi.is_dir(), zi.file_size, mtime, mode, (lambda zi=zi: z.open(zi))
        return
    with tarfile.open(src, "r:*") as t:
        for ti in t:
            if not (ti.isfile() or ti.isdir()):
                continue  # links and devices don't survive a format change safely
            yield (_safe_name(ti.name), ti.isdir(), ti.size, ti.mtime, ti.mode & 0o777 or 0o644,
                   (lambda ti=ti: t.extractfile(ti)))


def archive_to(target: str) -> Callable:
    ext = "." + target.lower()

    def go(src: Path, cat: str = "archive") -> list[Path]:
        stem = re.sub(r"(\.tar\.(gz|bz2|xz)|\.tgz|\.tbz2|\.txz|\.tar|\.zip)$", "", src.name, flags=re.I)
        out = output_dir(src) / (stem + ext)
        n = 0
        while out.exists():  # never overwrite; 'a.tar.gz' -> 'a (1).tar.gz'
            n += 1
            out = output_dir(src) / f"{stem} ({n}){ext}"

        def write(dst):
            try:
                if target == "ZIP":
                    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
                        for name, is_dir, size, mtime, mode, opener in _iter_archive(src):
                            if not name:
                                continue
                            zi = zipfile.ZipInfo(name + ("/" if is_dir else ""),
                                                 date_time=time.localtime(mtime or 315532800)[:6])
                            zi.external_attr = ((0o40000 if is_dir else 0o100000) | mode) << 16
                            zi.compress_type = zipfile.ZIP_DEFLATED
                            if is_dir:
                                z.writestr(zi, b"")
                            else:
                                with z.open(zi, "w", force_zip64=size > 2**31) as w, opener() as r:
                                    shutil.copyfileobj(r, w, 1 << 20)
                else:
                    mode = {"TAR": "w", "TAR.GZ": "w:gz", "TAR.XZ": "w:xz", "TAR.BZ2": "w:bz2"}[target]
                    with tarfile.open(dst, mode) as t:
                        for name, is_dir, size, mtime, fmode, opener in _iter_archive(src):
                            if not name:
                                continue
                            ti = tarfile.TarInfo(name)
                            ti.mtime, ti.mode = mtime, fmode
                            if is_dir:
                                ti.type = tarfile.DIRTYPE
                                t.addfile(ti)
                            else:
                                ti.size = size
                                with opener() as r:
                                    t.addfile(ti, r)
            except (zipfile.BadZipFile, tarfile.TarError, EOFError):
                raise UserError("This archive is damaged or isn't what its extension says.")
        _publish(write, out)
        return [out]
    return go


# ---------------------------------------------------------------- subtitles
def subtitle_to(target: str) -> Callable:
    spec = {"SRT": ("srt", ".srt"), "VTT": ("webvtt", ".vtt"), "ASS": ("ass", ".ass")}[target]

    def go(src: Path, cat: str = "subtitle") -> list[Path]:
        from . import media
        out = sibling(src, ext=spec[1])
        tmp = out.with_name(f".~{out.name}.part")
        try:
            r = media._run(["-y", "-i", str(src), "-f", spec[0], str(tmp)])
            if r.returncode != 0 or not tmp.exists() or tmp.stat().st_size == 0:
                raise UserError("Couldn't read these subtitles.")
            os.replace(tmp, out)
        finally:
            if tmp.exists():
                tmp.unlink()
        return [out]
    return go


# ---------------------------------------------------------------- route tables
class Route(NamedTuple):
    fn: Callable
    needs: str | None = None   # None, "lo" (LibreOffice) or "ffmpeg"


def _routes_word() -> dict[str, Route]:
    return {"PDF": Route(word_to_pdf), "DOCX": Route(word_to_docx), "ODT": Route(lo_route("odt", ".odt"), "lo"),
            "RTF": Route(lo_route("rtf", ".rtf"), "lo"), "DOC": Route(lo_route("doc", ".doc"), "lo"),
            "TXT": Route(word_to_txt), "HTML": Route(word_to_html, "lo")}


ROUTES: dict[str, dict[str, Route]] = {
    "word": _routes_word(),
    "slides": {"PDF": Route(slides_to_pdf), "PNG": Route(slides_to_images("PNG")),
               "JPG": Route(slides_to_images("JPG")), "TXT": Route(slides_to_txt),
               "PPTX": Route(lo_route("pptx", ".pptx"), "lo"), "ODP": Route(lo_route("odp", ".odp"), "lo"),
               "PPT": Route(lo_route("ppt", ".ppt"), "lo")},
    "sheet": {"XLSX": Route(sheet_to_xlsx), "CSV": Route(sheet_to_delimited(",", ".csv")),
              "TSV": Route(sheet_to_delimited("\t", ".tsv")), "JSON": Route(sheet_to_json),
              "HTML": Route(sheet_to_html), "MD": Route(sheet_to_md), "PDF": Route(sheet_to_pdf),
              "DOCX": Route(sheet_to_docx), "ODS": Route(sheet_via_xlsx("ods", ".ods"), "lo"),
              "XLS": Route(sheet_via_xlsx("xls", ".xls"), "lo")},
    "markdown": {"PDF": Route(markup_to_pdf), "DOCX": Route(markup_to_docx), "HTML": Route(markup_to_html),
                 "TXT": Route(markup_to_txt)},
    "html": {"PDF": Route(markup_to_pdf), "DOCX": Route(markup_to_docx), "MD": Route(html_to_md),
             "TXT": Route(markup_to_txt)},
    "ebook": {"PDF": Route(ebook_to_pdf), "TXT": Route(ebook_to_txt), "HTML": Route(ebook_to_html)},
    "svg": {"PNG": Route(svg_to_image("PNG")), "JPG": Route(svg_to_image("JPG")),
            "WEBP": Route(svg_to_image("WEBP")), "TIFF": Route(svg_to_image("TIFF")),
            "PDF": Route(svg_to_image("PDF"))},
    "archive": {t: Route(archive_to(t)) for t in ("ZIP", "TAR.GZ", "TAR.XZ", "TAR.BZ2", "TAR")},
    "subtitle": {t: Route(subtitle_to(t), "ffmpeg") for t in ("SRT", "VTT", "ASS")},
    # extra targets for categories convert.py already handles
    "pdf": {"HTML": Route(pdf_to_html), "SVG": Route(pdf_to_svg)},
    "docx": {"HTML": Route(docx_to_htmlfile), "MD": Route(docx_to_md), "ODT": Route(lo_route("odt", ".odt"), "lo"),
             "RTF": Route(lo_route("rtf", ".rtf"), "lo"), "DOC": Route(lo_route("doc", ".doc"), "lo")},
    "text": {"HTML": Route(text_to_html)},
}
CATEGORIES = {"word", "slides", "sheet", "markdown", "html", "ebook", "svg", "archive", "subtitle"}
# page-based files FFmpeg-free: CBZ and XPS only make sense as PDF
_PDF_ONLY = {"CBZ", "XPS"}
# source formats that can't be read at all without LibreOffice
_NEEDS_LO_SOURCE = {".doc", ".dot", ".wpd", ".ppt", ".pps", ".xls", ".ods"}


def tool_ok(needs: str | None) -> bool:
    if needs == "lo":
        return lo_available()
    if needs == "ffmpeg":
        from . import media
        return media.available()
    return True


def missing_tool(path_or_ext: str | Path) -> str | None:
    """Name of the program a file needs but doesn't have, for the 'nothing to offer' message."""
    ext = Path(str(path_or_ext).lower()).suffix or str(path_or_ext).lower()
    if ext in _NEEDS_LO_SOURCE and not lo_available():
        return "LibreOffice"
    if ext in (".srt", ".vtt", ".ass", ".ssa"):
        from . import media
        return None if media.available() else "FFmpeg"
    return None


def targets_for(cat: str, src_ext: str) -> list[str]:
    routes = ROUTES.get(cat)
    if not routes:
        return []
    ext = "." + src_ext.lower().lstrip(".")
    if ext in _NEEDS_LO_SOURCE and not lo_available():
        return []
    cur = source_format(src_ext)
    if cat == "ebook" and cur in _PDF_ONLY:
        return ["PDF"]
    out = []
    for t, r in routes.items():
        if t == cur or not tool_ok(r.needs):
            continue
        out.append(t)
    return out


def handles(cat: str, target: str) -> bool:
    return target.upper() in ROUTES.get(cat, {})


def convert_doc(src: str | Path, cat: str, target: str) -> list[Path]:
    r = ROUTES.get(cat, {}).get(target.upper())
    if r is None:
        raise ValueError(f"Can't convert {cat} to {target}")
    if not tool_ok(r.needs):
        raise UserError("This conversion needs " + ("LibreOffice. Install it from libreoffice.org."
                                                    if r.needs == "lo" else "FFmpeg."))
    return r.fn(Path(src), cat)
