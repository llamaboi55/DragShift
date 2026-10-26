"""Windows device-pixel printing, without a second image scaling stage."""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from pathlib import Path

from . import video
from .util import UserError

_PRINT_BAND_BYTES = 8 * 1024 * 1024
_PRINT_BAND_ROWS = 256


def supported():
    return sys.platform == 'win32'


def _api():
    if not supported():
        raise UserError('Calibrated direct printing is currently available on Windows. Export a printer raster on this platform.')
    from ctypes import wintypes as w
    gdi, user = ctypes.WinDLL('gdi32', use_last_error=True), ctypes.WinDLL('user32', use_last_error=True)
    signatures = {
        'CreateDCW': ([w.LPCWSTR, w.LPCWSTR, w.LPCWSTR, ctypes.c_void_p], w.HDC),
        'DeleteDC': ([w.HDC], w.BOOL),
        'GetDeviceCaps': ([w.HDC, ctypes.c_int], ctypes.c_int),
        'CreateCompatibleDC': ([w.HDC], w.HDC),
        'SelectObject': ([w.HDC, w.HANDLE], w.HANDLE),
        'DeleteObject': ([w.HANDLE], w.BOOL),
        'StartDocW': ([w.HDC, ctypes.c_void_p], ctypes.c_int),
        'StartPage': ([w.HDC], ctypes.c_int),
        'EndPage': ([w.HDC], ctypes.c_int),
        'EndDoc': ([w.HDC], ctypes.c_int),
        'AbortDoc': ([w.HDC], ctypes.c_int),
        'BitBlt': ([w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                    w.HDC, ctypes.c_int, ctypes.c_int, w.DWORD], w.BOOL),
    }
    for name, (args, result) in signatures.items():
        fn = getattr(gdi, name)
        fn.argtypes, fn.restype = args, result
    user.LoadImageW.argtypes = [w.HINSTANCE, w.LPCWSTR, w.UINT, ctypes.c_int, ctypes.c_int, w.UINT]
    user.LoadImageW.restype = w.HANDLE
    return gdi, user


class Printer:
    """Own one printer DC. Creation and submission are distinct operations."""
    def __init__(self, dc, name='', global_handles=()):
        self.gdi, self.user = _api()
        if not dc:
            raise UserError('The printer could not be opened.')
        self.dc, self.name, self.global_handles = dc, name, global_handles
        self.dpi = tuple(self.gdi.GetDeviceCaps(dc, index) for index in (88, 90))
        self.printable = tuple(self.gdi.GetDeviceCaps(dc, index) for index in (8, 10))
        self.offset = tuple(self.gdi.GetDeviceCaps(dc, index) for index in (112, 113))
        if min(*self.dpi, *self.printable) <= 0:
            self.close()
            raise UserError('The printer did not report a usable resolution or page size.')

    @classmethod
    def open(cls, name):
        gdi, _ = _api()
        return cls(gdi.CreateDCW('WINSPOOL', name, None, None), name)

    def close(self):
        if getattr(self, 'dc', None):
            self.gdi.DeleteDC(self.dc)
            self.dc = None
        if getattr(self, 'global_handles', ()):
            kernel = ctypes.WinDLL('kernel32')
            kernel.GlobalFree.argtypes = [ctypes.c_void_p]
            kernel.GlobalFree.restype = ctypes.c_void_p
            for handle in self.global_handles:
                if handle:
                    kernel.GlobalFree(handle)
            self.global_handles = ()

    def emit(self, image, *, output=None, title='DragShift', position=(0, 0)):
        """Submit an already prepared raster, one image pixel per device pixel.

        ``output`` explicitly redirects the driver job to a file for inspection.
        The normal UI calls this only after the printer dialog is accepted.
        """
        if callable(getattr(image, 'read_rows', None)):
            return self._emit_stream(image, output=output, title=title, position=position)
        from ctypes import wintypes as w
        if not self.dc:
            raise UserError('The printer is closed.')
        width, height = image.size
        x, y = map(int, position)
        if x < 0 or y < 0 or x + width > self.printable[0] or y + height > self.printable[1]:
            raise UserError('The image does not fit the printable area. Reduce its size or choose a larger paper size; automatic fitting is disabled.')
        if output is not None:
            output = Path(output).resolve()
            if output.exists():
                raise UserError('The print output file already exists. Choose another name.')
        class Document(ctypes.Structure):
            _fields_ = [('size', ctypes.c_int), ('name', w.LPCWSTR), ('output', w.LPCWSTR),
                        ('datatype', w.LPCWSTR), ('flags', w.DWORD)]
        document = Document(ctypes.sizeof(Document), title, str(output) if output else None, None, 0)
        memory, bitmap, previous, started = None, None, None, False
        try:
            with tempfile.TemporaryDirectory(prefix='dragshift-print-') as folder:
                path = Path(folder) / 'raster.bmp'
                image.convert('RGB').save(path, 'BMP')
                if output:
                    # Keep incomplete jobs private and publish without replacing
                    # another file, just like ordinary media exports.
                    stage = Path(folder) / 'job.prn'
                    document.output = str(stage)
                bitmap = self.user.LoadImageW(None, str(path), 0, 0, 0, 0x10)
                memory = self.gdi.CreateCompatibleDC(self.dc)
                if not bitmap or not memory:
                    raise UserError('The print raster could not be prepared.')
                previous = self.gdi.SelectObject(memory, bitmap)
                if not previous or previous == ctypes.c_void_p(-1).value:
                    raise UserError('The print raster could not be selected.')
                video.check_cancel()
                if self.gdi.StartDocW(self.dc, ctypes.byref(document)) <= 0:
                    raise UserError('The printer refused the print job.')
                started = True
                if self.gdi.StartPage(self.dc) <= 0:
                    raise UserError('The printer could not start the page.')
                video.check_cancel()
                if not self.gdi.BitBlt(self.dc, x, y, width, height, memory, 0, 0, 0x00CC0020):
                    raise UserError('The printer could not receive the raster.')
                video.check_cancel()
                if self.gdi.EndPage(self.dc) <= 0 or self.gdi.EndDoc(self.dc) <= 0:
                    raise UserError('The printer could not finish the print job.')
                started = False
                if output:
                    video.check_cancel()
                    with video._publish_lock:
                        # The temporary directory may be on another volume.
                        # Stage beside the destination before the atomic link.
                        with tempfile.TemporaryDirectory(prefix='.~print-', dir=output.parent) as destination:
                            import shutil
                            copied = Path(destination) / 'job.prn'
                            shutil.copyfile(stage, copied)
                            video.check_cancel()
                            try:
                                os.link(copied, output)
                            except FileExistsError:
                                raise UserError('The print output file already exists. Choose another name.')
                            except OSError:
                                os.rename(copied, output)  # Windows refuses replacement.
        finally:
            if started:
                self.gdi.AbortDoc(self.dc)
            if previous and previous != ctypes.c_void_p(-1).value and memory:
                self.gdi.SelectObject(memory, previous)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)

    def _emit_stream(self, raster, *, output=None, title='DragShift', position=(0, 0)):
        """Send bounded RGB bands through the same unscaled BitBlt path.

        Keep one document and page, and place each band at its absolute device
        row. No whole-page BMP or GDI bitmap is needed. Printer-driver spool
        storage remains controlled by Windows and the selected driver.
        """
        from ctypes import wintypes as w
        import numpy as np
        from PIL import Image

        if not self.dc:
            raise UserError('The printer is closed.')
        width, height = map(int, raster.size)
        x, y = map(int, position)
        if (min(width, height) < 1 or x < 0 or y < 0
                or x + width > self.printable[0] or y + height > self.printable[1]):
            raise UserError('The image does not fit the printable area. Reduce its size or choose a larger paper size; automatic fitting is disabled.')
        if output is not None:
            output = Path(output).resolve()
            if output.exists():
                raise UserError('The print output file already exists. Choose another name.')

        class Document(ctypes.Structure):
            _fields_ = [('size', ctypes.c_int), ('name', w.LPCWSTR), ('output', w.LPCWSTR),
                        ('datatype', w.LPCWSTR), ('flags', w.DWORD)]

        # BMP scanlines are padded to four bytes. Limit both the byte budget and
        # row count, including an exceptionally wide single row when necessary.
        stride = (width * 3 + 3) & ~3
        band_rows = max(1, min(_PRINT_BAND_ROWS, _PRINT_BAND_BYTES // stride))
        document = Document(ctypes.sizeof(Document), title, None, None, 0)
        memory, started = None, False
        try:
            with tempfile.TemporaryDirectory(prefix='dragshift-print-') as folder:
                folder = Path(folder)
                path, stage = folder / 'band.bmp', folder / 'job.prn'
                if output:
                    document.output = str(stage)
                memory = self.gdi.CreateCompatibleDC(self.dc)
                if not memory:
                    raise UserError('The print raster could not be prepared.')
                video.check_cancel()
                if self.gdi.StartDocW(self.dc, ctypes.byref(document)) <= 0:
                    raise UserError('The printer refused the print job.')
                started = True
                if self.gdi.StartPage(self.dc) <= 0:
                    raise UserError('The printer could not start the page.')
                for start in range(0, height, band_rows):
                    video.check_cancel()
                    stop = min(height, start + band_rows)
                    pixels = np.asarray(raster.read_rows(start, stop))
                    if pixels.shape != (stop-start, width, 3) or pixels.dtype != np.uint8:
                        raise UserError('The print raster could not be prepared.')
                    with Image.fromarray(pixels) as band:
                        band.save(path, 'BMP')
                    bitmap, previous = None, None
                    try:
                        # Preserve RGB samples rather than mapping every band to
                        # the display's compatible-bitmap format. In particular,
                        # some printer drivers render segmented DDBs differently.
                        bitmap = self.user.LoadImageW(None, str(path), 0, 0, 0, 0x2010)
                        if not bitmap:
                            raise UserError('The print raster could not be prepared.')
                        previous = self.gdi.SelectObject(memory, bitmap)
                        if not previous or previous == ctypes.c_void_p(-1).value:
                            raise UserError('The print raster could not be selected.')
                        video.check_cancel()
                        if not self.gdi.BitBlt(self.dc, x, y+start, width, stop-start, memory, 0, 0, 0x00CC0020):
                            raise UserError('The printer could not receive the raster.')
                    finally:
                        if previous and previous != ctypes.c_void_p(-1).value:
                            self.gdi.SelectObject(memory, previous)
                        if bitmap:
                            self.gdi.DeleteObject(bitmap)
                    video.progress(stop / height, 'Sending print raster…')
                video.check_cancel()
                if self.gdi.EndPage(self.dc) <= 0 or self.gdi.EndDoc(self.dc) <= 0:
                    raise UserError('The printer could not finish the print job.')
                started = False
                if output:
                    video.check_cancel()
                    with video._publish_lock:
                        with tempfile.TemporaryDirectory(prefix='.~print-', dir=output.parent) as destination:
                            import shutil
                            copied = Path(destination) / 'job.prn'
                            shutil.copyfile(stage, copied)
                            video.check_cancel()
                            try:
                                os.link(copied, output)
                            except FileExistsError:
                                raise UserError('The print output file already exists. Choose another name.')
                            except OSError:
                                if os.name != 'nt':
                                    raise
                                try:
                                    os.rename(copied, output)  # Windows refuses replacement.
                                except FileExistsError:
                                    raise UserError('The print output file already exists. Choose another name.')
        finally:
            if started:
                self.gdi.AbortDoc(self.dc)
            if memory:
                self.gdi.DeleteDC(memory)


def choose(owner_id):
    """Use the native driver settings and return its selected DC and file flag."""
    _api()
    from ctypes import wintypes as w
    class PrintDialog(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('owner', w.HWND), ('devmode', w.HGLOBAL),
                    ('devnames', w.HGLOBAL), ('dc', w.HDC), ('flags', w.DWORD),
                    ('from_page', w.WORD), ('to_page', w.WORD), ('min_page', w.WORD),
                    ('max_page', w.WORD), ('copies', w.WORD), ('instance', w.HINSTANCE),
                    ('custom_data', w.LPARAM), ('print_hook', ctypes.c_void_p),
                    ('setup_hook', ctypes.c_void_p), ('print_template', w.LPCWSTR),
                    ('setup_template', w.LPCWSTR), ('print_template_handle', w.HGLOBAL),
                    ('setup_template_handle', w.HGLOBAL)]
    library = ctypes.WinDLL('comdlg32', use_last_error=True)
    library.PrintDlgW.argtypes, library.PrintDlgW.restype = [ctypes.POINTER(PrintDialog)], w.BOOL
    library.CommDlgExtendedError.restype = w.DWORD
    data = PrintDialog()
    data.size, data.owner = ctypes.sizeof(data), owner_id
    data.flags = 0x100 | 0x40000 | 0x4 | 0x8  # RETURNDC, driver copies, no selection/page ranges
    data.min_page = data.max_page = data.copies = 1
    accepted = library.PrintDlgW(ctypes.byref(data))
    if not accepted:
        # Cancelled dialogs may still allocate driver state.
        handles = data.devmode, data.devnames
        if data.dc:
            Printer(data.dc, global_handles=handles).close()
        else:
            kernel = ctypes.WinDLL('kernel32')
            kernel.GlobalFree.argtypes = [ctypes.c_void_p]
            for handle in handles:
                if handle:
                    kernel.GlobalFree(handle)
        if library.CommDlgExtendedError():
            raise UserError('The print dialog could not be opened.')
        return None, False
    return Printer(data.dc, global_handles=(data.devmode, data.devnames)), bool(data.flags & 0x20)
