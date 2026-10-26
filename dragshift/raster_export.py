"""Lossless print-file writers for bounded-memory RGB row sources.

A source exposes ``size = (width, height)`` and
``read_rows(start, stop) -> uint8 ndarray[stop-start, width, 3]``. Writers
request consecutive bands; neither the source nor the output is converted to
a full in-memory image. TIFF compression uses stdlib zlib, so exporting does
not require optional native image codecs.
"""
from __future__ import annotations

import math
import numbers
import struct
import zlib
from pathlib import Path

import numpy as np

from . import video
from .util import UserError

BAND_BYTES = 8 * 1024 * 1024
PNG_CHUNK_BYTES = 64 * 1024
# Allow room for TIFF directories, strip tables and incompressible zlib data.
BIGTIFF_THRESHOLD = (1 << 32) - 32 * 1024 * 1024
_UINT32 = (1 << 32) - 1
_INT32 = (1 << 31) - 1
_MESSAGE = 'Saving interlace…'


def validate(size, target, dpi):
    """Check format limits before source preparation or temporary disk writes."""
    from types import SimpleNamespace
    _preflight(SimpleNamespace(size=size), target, dpi)


def _preflight(raster, target, dpi):
    if target not in ('TIFF', 'PNG', 'BMP'):
        raise UserError('Choose a lossless print format: TIFF, PNG or BMP.')
    try:
        width, height = raster.size
        if (isinstance(width, bool) or isinstance(height, bool)
                or not isinstance(width, numbers.Integral)
                or not isinstance(height, numbers.Integral)
                or min(width, height) < 1):
            raise ValueError('dimensions')
        width, height = int(width), int(height)
        resolution = tuple(float(value) for value in dpi) if isinstance(dpi, (tuple, list)) else (float(dpi),) * 2
        if len(resolution) != 2 or not all(math.isfinite(value) and value > 0 for value in resolution):
            raise ValueError('resolution')
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        raise UserError('Choose valid print dimensions and resolution.') from error
    row_bytes = width * 3
    if row_bytes > BAND_BYTES:
        raise UserError('One print row is too wide. Reduce the width or resolution.')
    if target == 'TIFF':
        if max(width, height) > _UINT32:
            raise UserError('This raster exceeds the TIFF dimension limit.')
    elif target == 'PNG':
        if max(width, height) > _INT32 or max(resolution) / .0254 > _UINT32:
            raise UserError('This raster exceeds the PNG dimension or resolution limit.')
    else:
        stride = (row_bytes + 3) & ~3
        if stride * height + 54 > _UINT32:
            raise UserError('BMP files cannot exceed 4 GB. Choose TIFF for this print size.')
        if max(width, height) > _INT32 or max(resolution) / .0254 > _INT32:
            raise UserError('This raster exceeds the BMP dimension or resolution limit.')
    return width, height, resolution, max(1, BAND_BYTES // row_bytes)


def _bands(raster, width, height, rows):
    for start in range(0, height, rows):
        video.check_cancel()
        stop = min(height, start + rows)
        band = raster.read_rows(start, stop)
        video.check_cancel()
        if (not isinstance(band, np.ndarray) or band.dtype != np.uint8
                or band.shape != (stop-start, width, 3)):
            raise UserError('The print renderer returned invalid RGB rows.')
        yield np.ascontiguousarray(band)
        video.progress(stop / height, _MESSAGE)


def _tiff(handle, raster, width, height, resolution, rows):
    import tifffile

    def strips():
        for band in _bands(raster, width, height, rows):
            # Bytes iterators are already encoded strips. Passing ndarray
            # strips would instead be interpreted as single complete pages.
            encoded = zlib.compress(memoryview(band), level=1)
            video.check_cancel()
            yield encoded

    with tifffile.TiffWriter(handle, bigtiff=width*height*3 >= BIGTIFF_THRESHOLD,
                             byteorder='<') as writer:
        handle.flush()
        writer.write(strips(), shape=(height, width, 3), dtype=np.uint8,
                     photometric='rgb', planarconfig='contig', compression='deflate',
                     rowsperstrip=rows, resolution=resolution, resolutionunit='INCH',
                     metadata=None, software='DragShift', maxworkers=1)


def _chunk(handle, kind, payload):
    handle.write(struct.pack('>I', len(payload)))
    handle.write(kind)
    handle.write(payload)
    handle.write(struct.pack('>I', zlib.crc32(payload, zlib.crc32(kind)) & _UINT32))


def _png(handle, raster, width, height, resolution, rows):
    handle.write(b'\x89PNG\r\n\x1a\n')
    _chunk(handle, b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
    _chunk(handle, b'pHYs', struct.pack('>IIB', *(round(value / .0254) for value in resolution), 1))
    encoder = zlib.compressobj(level=1)

    def compressed(data):
        for start in range(0, len(data), PNG_CHUNK_BYTES):
            _chunk(handle, b'IDAT', data[start:start+PNG_CHUNK_BYTES])

    for band in _bands(raster, width, height, rows):
        for row in band:
            video.check_cancel()
            # Filter 0 preserves narrow interlace bands without an extra
            # full-width prediction buffer. One zlib stream spans all IDATs.
            compressed(encoder.compress(b'\x00'))
            compressed(encoder.compress(memoryview(row)))
    compressed(encoder.flush())
    _chunk(handle, b'IEND', b'')


def _bmp(handle, raster, width, height, resolution, rows):
    stride = (width * 3 + 3) & ~3
    length = stride * height
    handle.write(struct.pack('<2sIHHI', b'BM', length + 54, 0, 0, 54))
    # Negative height is a standard top-down DIB: stream forward without a
    # second raster on disk or reverse reads of the rendering pipeline.
    handle.write(struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 24, 0, length,
                             *(round(value / .0254) for value in resolution), 0, 0))
    padding = b'\x00' * (stride - width * 3)
    for band in _bands(raster, width, height, rows):
        bgr = np.ascontiguousarray(band[:, :, ::-1])
        for row in bgr:
            video.check_cancel()
            handle.write(memoryview(row))
            if padding:
                handle.write(padding)


def write(raster, path, target, dpi):
    """Write a new TIFF/PNG/BMP file and return its ``Path``.

    Existing files are never replaced. On cancellation or failure the newly
    created incomplete file is removed. Callers can stage this file and its
    print metadata together before atomically publishing their final names.
    """
    width, height, resolution, rows = _preflight(raster, target, dpi)
    path = Path(path)
    video.progress(0., _MESSAGE)
    created = False
    try:
        with path.open('xb') as handle:
            created = True
            {'TIFF': _tiff, 'PNG': _png, 'BMP': _bmp}[target](
                handle, raster, width, height, resolution, rows)
        video.progress(1., _MESSAGE)
    except BaseException:
        if created:
            path.unlink(missing_ok=True)
        raise
    return path
