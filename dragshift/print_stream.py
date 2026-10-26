"""Bounded-row RGB rasters with the calibrated integer print sampler."""
from __future__ import annotations

from collections import OrderedDict
import numpy as np

from . import print_raster, video
from .util import UserError

MAX_AXIS = 1_000_000
MAX_PIXELS = 16_000_000_000
BAND_BYTES = 2 * 1024 * 1024


def checked_size(size):
    width, height = map(int, size)
    if min(width, height) < 1 or max(width, height) > MAX_AXIS or width * height > MAX_PIXELS:
        raise UserError('The print dimensions exceed the streaming limit (one million pixels per side, 16 billion pixels total).')
    return width, height


def band_rows(width):
    return max(1, min(128, BAND_BYTES // (width * 3)))


class ArrayRaster:
    def __init__(self, array):
        self.array = array
        self.size = array.shape[1], array.shape[0]

    def read_rows(self, start, stop):
        return self.array[start:stop]


class FileRaster:
    """Seek/read rather than mmap: consumed source pages do not accumulate in RSS."""
    def __init__(self, path, size):
        self.size = checked_size(size)
        self.file = open(path, 'rb')

    def read_rows(self, start, stop):
        width, height = self.size
        if not 0 <= start <= stop <= height:
            raise ValueError('Invalid raster row interval')
        self.file.seek(start * width * 3)
        raw = self.file.read((stop - start) * width * 3)
        return np.frombuffer(raw, dtype=np.uint8).reshape(stop-start, width, 3)

    def close(self):
        self.file.close()


class SampledRaster:
    """Same Q15 bilinear/box arithmetic as print_raster.resize, evaluated on demand."""
    def __init__(self, source, size):
        self.source, self.size = source, checked_size(size)
        self.sw, self.sh = source.size
        width, height = self.size
        grid = print_raster.GRID
        self.sx, self.sy = self.sw * grid // width, self.sh * grid // height
        x = np.arange(width, dtype=np.int64) * self.sx
        self.ix = np.minimum(x // grid, self.sw - 1)
        self.fx = (x % grid)[:, None]
        self.nx = np.minimum(self.ix + 1, self.sw - 1)
        self.cache = OrderedDict()

    def _rows(self, start, stop):
        # Reuse just a few source lines during enlargement. Each returned array
        # owns its bytes, so no open mmap or whole-image reference is retained.
        key = start, stop
        if key not in self.cache:
            self.cache[key] = self.source.read_rows(start, stop)
            while len(self.cache) > 3:
                self.cache.popitem(last=False)
        return self.cache[key]

    def read_rows(self, start, stop):
        width, height = self.size
        if not 0 <= start <= stop <= height:
            raise ValueError('Invalid raster row interval')
        if self.size == self.source.size:
            return self.source.read_rows(start, stop)
        result = np.empty((stop-start, width, 3), np.uint8)
        grid = print_raster.GRID
        area = self.sx > grid and self.sy > grid
        if area:
            bx = (self.sx + (grid-1 if self.sx <= 3*grid//2 else grid//2)) // grid
            by = (self.sy + (grid-1 if self.sy <= 3*grid//2 else grid//2)) // grid
            right = np.minimum(self.ix + bx, self.sw)
        for index, row in enumerate(range(start, stop)):
            video.check_cancel()
            iy = min(row * self.sy // grid, self.sh - 1)
            if area:
                end = min(self.sh, iy + by)
                # Accumulate in bounded groups, even for drastic reductions.
                sums = np.zeros((self.sw, 3), np.int64)
                for first in range(iy, end, band_rows(self.sw)):
                    video.check_cancel()
                    sums += self.source.read_rows(first, min(end, first+band_rows(self.sw))).sum(axis=0, dtype=np.int64)
                prefix = np.concatenate((np.zeros((1, 3), np.int64), sums.cumsum(axis=0)))
                result[index] = (prefix[right] - prefix[self.ix]) // ((right-self.ix)[:, None] * (end-iy))
            else:
                rows = self._rows(iy, min(iy+2, self.sh))
                if width == self.sw:
                    top = rows[0].astype(np.int64)
                    delta = rows[-1].astype(np.int64)-top
                    result[index] = ((top*grid + delta*(row*self.sy % grid) + grid//2)//grid).clip(0,255)
                    continue
                a = rows[0, self.ix].astype(np.int64)
                b = rows[0, self.nx].astype(np.int64)
                c = rows[-1, self.ix].astype(np.int64)
                d = rows[-1, self.nx].astype(np.int64)
                top = a*grid + (b-a)*self.fx
                bottom = c*grid + (d-c)*self.fx
                delta = bottom-top
                delta = np.where(delta < 0, -((-delta)//grid), delta//grid)
                result[index] = ((top + delta*(row*self.sy % grid) + grid//2)//grid).clip(0,255)
        return result


def write_raw(raster, path):
    width, height = raster.size
    with open(path, 'wb') as destination:
        for start in range(0, height, band_rows(width)):
            video.check_cancel()
            destination.write(raster.read_rows(start, min(height, start+band_rows(width))).tobytes())
