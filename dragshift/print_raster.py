"""Deterministic integer sampling for calibrated print rasters.

Coordinates use a 15-bit fractional grid. Keep this path separate from ordinary
photo resizing: changing its rounding or filtering changes narrow printed bands.
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from . import video
from .util import UserError

MAX_PIXELS = 100_000_000
GRID = 32768


def checked_size(size):
    width, height = map(int, size)
    if width < 1 or height < 1 or width * height > MAX_PIXELS or max(width, height) > 100_000:
        raise UserError('The print raster is too large. Reduce its size or resolution.')
    return width, height


def resize(image: Image.Image, size, *, cancel=True) -> Image.Image:
    """RGB sampling, with bounded working memory and cancellation between rows."""
    width, height = checked_size(size)
    checked_size(image.size)
    if image.size == (width, height):
        return image.convert('RGB').copy()
    source = np.asarray(image.convert('RGB'))
    source_h, source_w = source.shape[:2]
    step_x, step_y = source_w * GRID // width, source_h * GRID // height
    x = np.arange(width, dtype=np.int64) * step_x
    ix = np.minimum(x // GRID, source_w - 1)
    result = np.empty((height, width, 3), dtype=np.uint8)
    if step_x > GRID and step_y > GRID:
        # The sample box has an integer extent; it is clipped at image edges.
        box_x = (step_x + (GRID - 1 if step_x <= 3 * GRID // 2 else GRID // 2)) // GRID
        box_y = (step_y + (GRID - 1 if step_y <= 3 * GRID // 2 else GRID // 2)) // GRID
        right = np.minimum(ix + box_x, source_w)
        for row in range(height):
            if cancel:
                video.check_cancel()
            top = min(source_h - 1, row * step_y // GRID)
            bottom = min(source_h, top + box_y)
            # Sum source rows first, then one horizontal prefix sum. Avoid a
            # full-image 64-bit integral buffer for large print photographs.
            sums = source[top:bottom].sum(axis=0, dtype=np.int64)
            prefix = np.concatenate((np.zeros((1, 3), np.int64), sums.cumsum(axis=0)))
            result[row] = (prefix[right] - prefix[ix]) // ((right - ix)[:, None] * (bottom - top))
    else:
        fraction_x = (x % GRID)[:, None]
        next_x = np.minimum(ix + 1, source_w - 1)
        for row in range(height):
            if cancel:
                video.check_cancel()
            coordinate_y = row * step_y
            iy, fraction_y = min(coordinate_y // GRID, source_h - 1), coordinate_y % GRID
            a = source[iy, ix].astype(np.int64)
            b = source[iy, next_x].astype(np.int64)
            c = source[min(iy + 1, source_h - 1), ix].astype(np.int64)
            d = source[min(iy + 1, source_h - 1), next_x].astype(np.int64)
            top = a * GRID + (b - a) * fraction_x
            bottom = c * GRID + (d - c) * fraction_x
            difference = bottom - top
            difference = np.where(difference < 0, -((-difference) // GRID), difference // GRID)
            result[row] = ((top + difference * fraction_y + GRID // 2) // GRID).clip(0, 255)
    return Image.fromarray(result)
