"""Lossless strip export uses a bounded row source, including BigTIFF jobs."""
import struct
import threading
import zlib

import numpy as np
import pytest
import tifffile
from PIL import Image

from dragshift import raster_export as output, video
from dragshift.util import UserError


class Rows:
    def __init__(self, size=(257, 129)):
        self.size = size
        self.requests = []

    def read_rows(self, start, stop):
        self.requests.append((start, stop))
        x = np.arange(self.size[0], dtype=np.uint32)[None, :]
        y = np.arange(start, stop, dtype=np.uint32)[:, None]
        result = np.empty((stop-start, self.size[0], 3), np.uint8)
        result[:, :, 0] = (x * 13 + y * 7) % 256
        result[:, :, 1] = (x * 3 + y * 11) % 256
        result[:, :, 2] = (x * 19 + y * 23) % 256
        return result

    def __array__(self, *args, **kwargs):
        raise AssertionError('Export must never materialize the complete source')


@pytest.mark.parametrize('target', ['TIFF', 'PNG', 'BMP'])
def test_lossless_roundtrip_and_dpi_from_bounded_bands(tmp_path, monkeypatch, target):
    monkeypatch.setattr(output, 'BAND_BYTES', 4096)
    source = Rows()
    expected = Rows(source.size).read_rows(0, source.size[1])
    progress = []
    path = tmp_path / ('large.' + target.lower())
    with video.job_context(video.Control(progress=lambda value, _: progress.append(value))):
        assert output.write(source, path, target, (749.125, 900)) == path
    with Image.open(path) as decoded:
        assert decoded.size == source.size
        assert np.array_equal(np.asarray(decoded), expected)
        assert decoded.info['dpi'] == pytest.approx((749.125, 900), abs=.02)
    assert len(source.requests) > 1
    assert source.requests[0][0] == 0 and source.requests[-1][1] == source.size[1]
    assert all(b == c for (_, b), (c, _) in zip(source.requests, source.requests[1:]))
    assert max((b-a) * source.size[0] * 3 for a, b in source.requests) <= output.BAND_BYTES
    assert progress[0] == 0 and progress[-1] == 1
    assert progress == sorted(progress)
    if target == 'TIFF':
        with tifffile.TiffFile(path) as tiff:
            assert not tiff.is_bigtiff
            assert tiff.pages[0].compression == 32946 or tiff.pages[0].compression == 8
            assert tiff.pages[0].rowsperstrip * source.size[0] * 3 <= output.BAND_BYTES
            assert np.array_equal(tiff.asarray(), expected)
    elif target == 'BMP':
        header = path.read_bytes()[:54]
        assert struct.unpack_from('<i', header, 22)[0] == -source.size[1]
        assert struct.unpack_from('<I', header, 2)[0] == path.stat().st_size


def test_png_chunks_crc_and_scanlines_are_standard_rgb(tmp_path, monkeypatch):
    monkeypatch.setattr(output, 'BAND_BYTES', 2000)
    source = Rows((101, 75))
    path = output.write(source, tmp_path / 'print.png', 'PNG', 600)
    data, offset, idat, kinds = path.read_bytes(), 8, [], []
    assert data[:8] == b'\x89PNG\r\n\x1a\n'
    while offset < len(data):
        size = struct.unpack_from('>I', data, offset)[0]
        kind = data[offset+4:offset+8]
        payload = data[offset+8:offset+8+size]
        crc = struct.unpack_from('>I', data, offset+8+size)[0]
        assert crc == zlib.crc32(payload, zlib.crc32(kind)) & 0xffffffff
        kinds.append(kind)
        if kind == b'IDAT':
            assert len(payload) <= output.PNG_CHUNK_BYTES
            idat.append(payload)
        elif kind == b'pHYs':
            assert struct.unpack('>IIB', payload) == (23622, 23622, 1)
        offset += size + 12
    assert offset == len(data) and kinds[0] == b'IHDR' and kinds[-1] == b'IEND'
    raw = np.frombuffer(zlib.decompress(b''.join(idat)), np.uint8).reshape(75, 304)
    assert not raw[:, 0].any()  # PNG filter method 0 for every scanline.
    assert np.array_equal(raw[:, 1:].reshape(75, 101, 3), Rows(source.size).read_rows(0, 75))


def test_bigtiff_can_be_selected_for_small_job_without_changing_pixels(tmp_path, monkeypatch):
    monkeypatch.setattr(output, 'BIGTIFF_THRESHOLD', 1)
    source = Rows((13, 17))
    path = output.write(source, tmp_path / 'large.tif', 'TIFF', 750)
    assert path.read_bytes()[:8] == b'II+\x00\x08\x00\x00\x00'
    with tifffile.TiffFile(path) as tiff:
        assert tiff.is_bigtiff
        assert np.array_equal(tiff.asarray(), Rows(source.size).read_rows(0, 17))
        assert tiff.pages[0].tags['ResolutionUnit'].value == 2


def test_automatic_bigtiff_selection_precedes_real_large_raster_read(tmp_path):
    path = tmp_path / 'over-four-gb.tif'
    header = []

    class Huge:
        size = (10000, 150000)  # 4.5GB RGB without allocating it.

        def read_rows(self, start, stop):
            header.append(path.read_bytes()[:8])
            raise video.Cancelled('Stop before allocating the raster')

    with pytest.raises(video.Cancelled):
        output.write(Huge(), path, 'TIFF', 750)
    assert header == [b'II+\x00\x08\x00\x00\x00']
    assert not path.exists()


def test_bmp_rejects_format_limit_before_reading_or_creating_file(tmp_path):
    source = Rows((100000, 20000))
    path = tmp_path / 'too-large.bmp'
    with pytest.raises(UserError, match='4 GB'):
        output.write(source, path, 'BMP', 750)
    assert not source.requests and not path.exists()


@pytest.mark.parametrize('target', ['TIFF', 'PNG', 'BMP'])
def test_cancellation_between_bands_removes_incomplete_file(tmp_path, monkeypatch, target):
    monkeypatch.setattr(output, 'BAND_BYTES', 2048)
    cancelled = threading.Event()

    class Stops(Rows):
        def read_rows(self, start, stop):
            band = super().read_rows(start, stop)
            if len(self.requests) == 2:
                cancelled.set()
            return band

    source = Stops()
    path = tmp_path / 'cancelled'
    with video.job_context(video.Control(cancel=cancelled)):
        with pytest.raises(video.Cancelled):
            output.write(source, path, target, 600)
    assert len(source.requests) == 2 and not path.exists()


@pytest.mark.parametrize('target', ['TIFF', 'PNG', 'BMP'])
def test_existing_output_is_never_overwritten_or_removed(tmp_path, target):
    path = tmp_path / 'original'
    path.write_bytes(b'original data')
    source = Rows()
    with pytest.raises(FileExistsError):
        output.write(source, path, target, 750)
    assert path.read_bytes() == b'original data' and not source.requests


@pytest.mark.parametrize('target', ['TIFF', 'PNG', 'BMP'])
def test_invalid_band_is_reported_and_partial_file_removed(tmp_path, target):
    class Invalid(Rows):
        def read_rows(self, start, stop):
            return np.zeros((stop-start, self.size[0], 3), np.float64)

    path = tmp_path / 'invalid'
    with pytest.raises(UserError, match='RGB'):
        output.write(Invalid(), path, target, 600)
    assert not path.exists()


@pytest.mark.parametrize('size, dpi, target', [((0, 10), 600, 'TIFF'),
    ((10, 10), float('nan'), 'TIFF'), ((10, 10), 0, 'PNG'),
    ((10, 10), 600, 'JPEG'), ((10.5, 10), 600, 'PNG')])
def test_invalid_format_and_geometry_are_preflighted(tmp_path, size, dpi, target):
    source = Rows(size)
    path = tmp_path / 'invalid'
    with pytest.raises(UserError):
        output.write(source, path, target, dpi)
    assert not source.requests and not path.exists()
