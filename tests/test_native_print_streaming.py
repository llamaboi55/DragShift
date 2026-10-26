from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift import native_print, video
from dragshift.util import UserError


class Rows:
    def __init__(self, pixels, fail_at=None):
        self.pixels = pixels
        self.size = pixels.shape[1], pixels.shape[0]
        self.calls = []
        self.fail_at = fail_at

    def read_rows(self, start, stop):
        self.calls.append((start, stop))
        if len(self.calls) == self.fail_at:
            raise video.Cancelled()
        return self.pixels[start:stop]


class FakeGDI:
    def __init__(self, fail=None):
        self.fail = fail
        self.calls = []
        self.bitmaps = {}
        self.selected = 1
        self.next_bitmap = 100
        self.page = np.full((30, 30, 3), 255, np.uint8)
        self.job_output = None
        self.blit_count = 0
        self.load_count = 0

    def CreateCompatibleDC(self, dc):
        self.calls.append(('CreateCompatibleDC', dc))
        return 0 if self.fail == 'memory' else 10

    def LoadImageW(self, instance, path, kind, width, height, flags):
        self.load_count += 1
        if self.fail == 'load' and self.load_count == 2:
            return 0
        with Image.open(path) as image:
            self.bitmaps[self.next_bitmap] = np.asarray(image.convert('RGB')).copy()
        handle = self.next_bitmap
        self.next_bitmap += 1
        self.calls.append(('LoadImageW', handle, flags))
        return handle

    def SelectObject(self, memory, bitmap):
        self.calls.append(('SelectObject', memory, bitmap))
        if self.fail == 'select' and bitmap == 101:
            return 0
        previous, self.selected = self.selected, bitmap
        return previous

    def DeleteObject(self, bitmap):
        self.calls.append(('DeleteObject', bitmap))
        self.bitmaps.pop(bitmap)
        return True

    def DeleteDC(self, dc):
        self.calls.append(('DeleteDC', dc))
        return True

    def StartDocW(self, dc, document):
        self.calls.append(('StartDocW', dc))
        self.job_output = document._obj.output
        return 0 if self.fail == 'startdoc' else 1

    def StartPage(self, dc):
        self.calls.append(('StartPage', dc))
        return 0 if self.fail == 'startpage' else 1

    def BitBlt(self, dc, x, y, width, height, memory, sx, sy, operation):
        self.blit_count += 1
        self.calls.append(('BitBlt', x, y, width, height, sx, sy, operation))
        if self.fail == 'blit' and self.blit_count == 2:
            return False
        self.page[y:y+height, x:x+width] = self.bitmaps[self.selected][sy:sy+height, sx:sx+width]
        return True

    def EndPage(self, dc):
        self.calls.append(('EndPage', dc))
        return 0 if self.fail == 'endpage' else 1

    def EndDoc(self, dc):
        self.calls.append(('EndDoc', dc))
        if self.job_output:
            Path(self.job_output).write_bytes(b'verified printer job')
        return 0 if self.fail == 'enddoc' else 1

    def AbortDoc(self, dc):
        self.calls.append(('AbortDoc', dc))
        return True


@pytest.fixture
def printer(monkeypatch, tmp_path):
    # Keep tests independent of an installed printer and a Windows desktop.
    monkeypatch.setattr(native_print.tempfile, 'tempdir', str(tmp_path))
    monkeypatch.setattr(native_print, '_PRINT_BAND_BYTES', 64, raising=False)
    monkeypatch.setattr(native_print, '_PRINT_BAND_ROWS', 256, raising=False)
    value = native_print.Printer.__new__(native_print.Printer)
    value.gdi = value.user = FakeGDI()
    value.dc, value.name, value.global_handles = 99, 'Test printer', ()
    value.dpi, value.printable, value.offset = (600, 600), (30, 30), (80, 70)
    return value


def pixels():
    y, x = np.mgrid[:7, :7]
    return np.stack((x*17+y, y*19+x, x*11+y*13), axis=-1).astype(np.uint8)


def test_streamed_print_is_bounded_exact_and_uses_one_page(printer):
    source = Rows(pixels())
    printer.emit(source, position=(3, 4))
    assert source.calls == [(0, 2), (2, 4), (4, 6), (6, 7)]
    assert np.array_equal(printer.gdi.page[4:11, 3:10], source.pixels)
    assert [call for call in printer.gdi.calls if call[0] == 'BitBlt'] == [
        ('BitBlt', 3, y, 7, height, 0, 0, 0x00CC0020)
        for y, height in [(4, 2), (6, 2), (8, 2), (10, 1)]]
    for name in ('StartDocW', 'StartPage', 'EndPage', 'EndDoc'):
        assert sum(call[0] == name for call in printer.gdi.calls) == 1
    assert not any(call[0] == 'AbortDoc' for call in printer.gdi.calls)
    assert sum(call[0] == 'DeleteObject' for call in printer.gdi.calls) == 4
    assert all(call[2] == 0x2010 for call in printer.gdi.calls if call[0] == 'LoadImageW')
    assert printer.gdi.selected == 1 and printer.gdi.bitmaps == {}
    assert ('DeleteDC', 10) in printer.gdi.calls


def test_small_pillow_print_keeps_single_blit_path(printer):
    printer.emit(Image.fromarray(pixels()))
    assert np.array_equal(printer.gdi.page[:7, :7], pixels())
    assert [call for call in printer.gdi.calls if call[0] == 'BitBlt'] == [
        ('BitBlt', 0, 0, 7, 7, 0, 0, 0x00CC0020)]
    assert all(call[2] == 0x10 for call in printer.gdi.calls if call[0] == 'LoadImageW')


@pytest.mark.parametrize('position,printable', [((-1, 0), (30, 30)), ((0, 0), (6, 30)), ((25, 0), (30, 30))])
def test_streamed_print_rejects_before_reading_any_pixels(printer, position, printable):
    source = Rows(pixels())
    printer.printable = printable
    with pytest.raises(UserError, match='does not fit'):
        printer.emit(source, position=position)
    assert source.calls == [] and printer.gdi.calls == []


@pytest.mark.parametrize('failure', ['memory', 'startdoc', 'startpage', 'load', 'select', 'blit', 'endpage', 'enddoc', 'cancel'])
def test_streamed_print_failure_aborts_and_releases_handles(printer, tmp_path, failure):
    printer.gdi.fail = failure
    source = Rows(pixels(), fail_at=2 if failure == 'cancel' else None)
    output = tmp_path / 'cancelled.prn'
    error = video.Cancelled if failure == 'cancel' else UserError
    with pytest.raises(error):
        printer.emit(source, output=output)
    assert not output.exists()
    assert printer.gdi.bitmaps == {} and printer.gdi.selected == 1
    assert any(call[0] == 'AbortDoc' for call in printer.gdi.calls) == (failure not in ('memory', 'startdoc'))
    if failure != 'memory':
        assert ('DeleteDC', 10) in printer.gdi.calls


def test_streamed_print_prn_publishes_only_after_success(printer, tmp_path):
    output = tmp_path / 'result.prn'
    printer.emit(Rows(pixels()), output=output)
    assert output.read_bytes() == b'verified printer job'
    source = Rows(pixels())
    with pytest.raises(UserError, match='already exists'):
        printer.emit(source, output=output)
    assert source.calls == []
    assert output.read_bytes() == b'verified printer job'


def test_streamed_print_prn_atomic_collision_preserves_existing(printer, tmp_path, monkeypatch):
    output = tmp_path / 'result.prn'
    end_doc = printer.gdi.EndDoc
    def race(dc):
        result = end_doc(dc)
        output.write_bytes(b'another job won')
        return result
    monkeypatch.setattr(printer.gdi, 'EndDoc', race)
    with pytest.raises(UserError, match='already exists'):
        printer.emit(Rows(pixels()), output=output)
    assert output.read_bytes() == b'another job won'
    assert not any(call[0] == 'AbortDoc' for call in printer.gdi.calls)
