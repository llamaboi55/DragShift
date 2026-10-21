"""Shared fixtures: realistic test images (photo-like content, Display P3 profile, camera EXIF),
quality metrics, and an isolated, headless Qt."""
from __future__ import annotations

import io
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pytest
from PIL import Image

from dragshift import convert  # noqa: E402  (registers the HEIF opener)
from dragshift.util import CONFIG

P3_PATH = Path("/System/Library/ColorSync/Profiles/Display P3.icc")
REAL_PAGES_TO_PDF = convert._pages_to_pdf  # before the autouse guard below swaps it out


# ---------------------------------------------------------------- images
def photo(w: int = 640, h: int = 480, seed: int = 1, alpha: bool = False) -> Image.Image:
    """Smooth gradients + fine grain: compresses like a real photo, so quality numbers mean something."""
    yy, xx = np.mgrid[0:h, 0:w]
    a = np.stack([128 + 100 * np.sin(xx / 37), 128 + 100 * np.cos(yy / 29),
                  128 + 80 * np.sin((xx + yy) / 53)], -1)
    a += np.random.default_rng(seed).normal(0, 3, a.shape)
    im = Image.fromarray(np.clip(a, 0, 255).astype("uint8"), "RGB")
    if alpha:
        m = np.zeros((h, w), "uint8")
        m[h // 4: 3 * h // 4, w // 4: 3 * w // 4] = 255
        im.putalpha(Image.fromarray(m))
    return im


def marked(w: int = 300, h: int = 200) -> Image.Image:
    """Grey image with a red block in the top-left corner: shows whether pixels are upright."""
    im = Image.new("RGB", (w, h), (128, 128, 128))
    im.paste((255, 0, 0), (0, 0, w // 5, h // 5))
    return im


def rgb(path_or_im) -> np.ndarray:
    im = path_or_im if isinstance(path_or_im, Image.Image) else Image.open(path_or_im)
    if im.mode == "RGBA":
        im = convert.flatten(im)
    return np.asarray(im.convert("RGB"), dtype=float)


def psnr(a, b) -> float:
    a, b = rgb(a), rgb(b)
    assert a.shape == b.shape, (a.shape, b.shape)
    mse = np.mean((a - b) ** 2)
    return float("inf") if mse == 0 else float(10 * np.log10(255 ** 2 / mse))


def reencode(im: Image.Image, fmt: str, **kw) -> Image.Image:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    buf.seek(0)
    return Image.open(buf)


def camera_exif(orientation: int | None = None, gps: bool = True) -> Image.Exif:
    ex = Image.Exif()
    ex[0x010F] = "Apple"                       # Make
    ex[0x0110] = "iPhone 17 Pro"               # Model
    ex[0x0132] = "2026:10:01 12:00:00"         # DateTime
    if orientation:
        ex[0x0112] = orientation
    ifd = ex.get_ifd(0x8769)                   # Exif sub-IFD
    ifd[0x9003] = "2026:10:01 11:59:58"        # DateTimeOriginal
    ifd[0x829A] = 1 / 250                      # ExposureTime
    if gps:
        g = ex.get_ifd(0x8825)
        g.update({1: "N", 2: (52.0, 22.0, 30.0), 3: "E", 4: (4.0, 53.0, 10.0)})
    return ex


def exif_of(path) -> Image.Exif:
    with Image.open(path) as im:
        ex = im.getexif()
        ex.get_ifd(0x8769)  # read the sub-IFDs while the file is still open
        ex.get_ifd(0x8825)
        return ex


def has_gps(path) -> bool:
    return bool(exif_of(path).get_ifd(0x8825))


def date_taken(path):
    return exif_of(path).get_ifd(0x8769).get(0x9003)


def icc_desc(path) -> str | None:
    from PIL import ImageCms
    with Image.open(path) as im:
        icc = im.info.get("icc_profile")
    if not icc:
        return None
    return ImageCms.ImageCmsProfile(io.BytesIO(icc)).profile.profile_description.strip()


def build_p3_icc() -> bytes:
    """A Display P3 ICC v2 profile (P3 primaries, D65 white, sRGB curve), for systems that don't ship one."""
    import struct

    def tag(sig, body):
        return sig, body + b"\0" * (-len(body) % 4)

    def s15(*v):
        return b"".join(struct.pack(">i", round(x * 65536)) for x in v)

    def xyz(*v):
        return tag(b"", b"XYZ \0\0\0\0" + s15(*v))[1]

    name = b"Display P3"
    desc = (b"desc\0\0\0\0" + struct.pack(">I", len(name) + 1) + name + b"\0"
            + b"\0" * 8 + b"\0\0\0" + b"\0" * 67)
    x = np.linspace(0, 1, 1024)
    srgb = np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)
    curve = b"curv\0\0\0\0" + struct.pack(">I", 1024) + (srgb * 65535).round().astype(">u2").tobytes()
    tags = [tag(b"desc", desc), tag(b"cprt", b"text\0\0\0\0Public domain\0"),
            tag(b"wtpt", xyz(0.9642, 1.0, 0.8249)),
            tag(b"rXYZ", xyz(0.5151, 0.2412, -0.0011)), tag(b"gXYZ", xyz(0.2920, 0.6922, 0.0419)),
            tag(b"bXYZ", xyz(0.1571, 0.0666, 0.7841)),
            tag(b"rTRC", curve), tag(b"gTRC", curve), tag(b"bTRC", curve)]
    offset = 128 + 4 + 12 * len(tags)
    table, data = b"", b""
    for sig, body in tags:
        table += sig + struct.pack(">II", offset + len(data), len(body))
        data += body
    size = offset + len(data)
    header = (struct.pack(">I", size) + b"\0" * 4 + struct.pack(">I", 0x02100000) + b"mntrRGB XYZ "
              + b"\0" * 12 + b"acsp" + b"\0" * 28 + s15(0.9642, 1.0, 0.8249) + b"\0" * 48)
    return header + struct.pack(">I", len(tags)) + table + data


@pytest.fixture(scope="session")
def p3() -> bytes:
    return P3_PATH.read_bytes() if P3_PATH.exists() else build_p3_icc()


def pytest_configure(config):
    config.addinivalue_line("markers", "office: drives a real office app (Pages / LibreOffice)")


@pytest.fixture(autouse=True)
def _no_office_apps(request, monkeypatch):
    """Only tests marked 'office' may launch Pages or LibreOffice (permission prompts, windows)."""
    if request.node.get_closest_marker("office") is None:
        monkeypatch.setattr(convert, "_pages_to_pdf", lambda *a: False)
        monkeypatch.setattr(convert, "_libreoffice_to_pdf", lambda *a: False)


@pytest.fixture(autouse=True)
def _config():
    saved = dict(CONFIG)
    CONFIG.update(output="next", jpg_quality=90, pdf_dpi=200)
    yield
    CONFIG.clear()
    CONFIG.update(saved)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtCore import QCoreApplication, QStandardPaths
    from PySide6.QtWidgets import QApplication
    QStandardPaths.setTestModeEnabled(True)   # QSettings never touch the real preferences
    QCoreApplication.setOrganizationName("DragShiftTests")
    app = QApplication.instance() or QApplication([])
    yield app
