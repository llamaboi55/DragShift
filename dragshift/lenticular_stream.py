"""Disk-backed source fitting and virtual interlace sheets for large jobs."""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import shutil
import tempfile

import numpy as np

from . import print_stream as stream, video
from .convert import flatten, open_image
from .util import UserError


class FittedRaster:
    def __init__(self, image, frame, size, aspect):
        from .lenticular import fit_layout
        image, inner, self.offset = fit_layout(image, frame, size, aspect=aspect)
        self.size = size
        self.sampled = stream.SampledRaster(stream.ArrayRaster(np.asarray(image)), inner)

    def read_rows(self, start, stop):
        width, _ = self.size
        x, y = self.offset
        iw, ih = self.sampled.size
        if self.offset == (0, 0) and self.size == self.sampled.size:
            return self.sampled.read_rows(start, stop)
        output = np.full((stop-start, width, 3), 255, np.uint8)
        top, bottom = max(start, y), min(stop, y+ih)
        if bottom > top:
            output[top-start:bottom-start, x:x+iw] = self.sampled.read_rows(top-y, bottom-y)
        return output


class InterlaceRaster:
    def __init__(self, project, geometry, sources):
        self.project, self.geometry, self.size = project, geometry, geometry.sheet
        self.count, self.quality = len(sources), int(project.settings.quality)
        self.vertical = project.settings.orientation == 'vertical'
        width, height = geometry.tile
        expanded = (geometry.source[0], height) if self.vertical else (width, geometry.source[1])
        self.expanded = [stream.SampledRaster(source, expanded) for source in sources]
        length = width if self.vertical else height
        coordinates = np.arange(length, dtype=np.int64)
        phase = round(project.settings.phase/100 * self.count*self.quality)
        self.frame_index = self.count-1-((coordinates+phase)//self.quality) % self.count
        self.source_index = coordinates//(self.count*self.quality)*self.quality + coordinates%self.quality
        self.selected = [np.flatnonzero(self.frame_index == i) for i in range(self.count)]

    def _tile_rows(self, start, stop):
        width, height = self.geometry.tile
        output = np.empty((stop-start, width, 3), np.uint8)
        if self.vertical:
            for index, source in enumerate(self.expanded):
                selected = self.selected[index]
                values = source.read_rows(start, stop)
                output[:, selected] = values[:, np.minimum(self.source_index[selected], values.shape[1]-1)]
        else:
            for row in range(start, stop):
                source = self.expanded[self.frame_index[row]]
                selected = min(self.source_index[row], source.size[1]-1)
                output[row-start] = source.read_rows(selected, selected+1)[0]
        if self.project.settings.black_edges:
            edge = self.count*self.quality
            if self.vertical:
                output[:, :edge] = output[:, -edge:] = 0
            else:
                y = np.arange(start, stop)
                output[(y < edge) | (y >= height-edge)] = 0
        return output

    def read_rows(self, start, stop):
        width, height = self.size
        if not 0 <= start <= stop <= height:
            raise ValueError('Invalid raster row interval')
        if self.size == self.geometry.tile:
            return self._tile_rows(start, stop)
        tw, th = self.geometry.tile
        margin, gap = self.geometry.margin, self.geometry.gap
        output = np.full((stop-start, width, 3), 255, np.uint8)
        for row in range(int(self.project.settings.rows)):
            origin = margin+row*(th+gap)
            top, bottom = max(start, origin), min(stop, origin+th)
            if bottom <= top:
                continue
            tile = self._tile_rows(top-origin, bottom-origin)
            for column in range(int(self.project.settings.columns)):
                x = margin+column*(tw+gap)
                output[top-start:bottom-start, x:x+tw] = tile
        return output


@contextmanager
def prepared(project, *, folder=None, dpi=None):
    from .lenticular import source_size
    project.validate()
    geometry = project.geometry()
    size = geometry.sheet if dpi is None else tuple(round(p/device*target) for p, device, target in
        zip(geometry.sheet, (geometry.dpi, geometry.dpi), dpi))
    stream.checked_size(size)
    # Preflight before any full source decode or temporary writes.
    for frame in project.frames:
        source_size(frame.path)
    required = geometry.source[0]*geometry.source[1]*3*len(project.frames)
    root = Path(folder) if folder else Path(tempfile.gettempdir())
    if shutil.disk_usage(root).free < required + 64*1024*1024:
        raise UserError('There is not enough free disk space for the prepared print frames.')
    sources = []
    try:
        with tempfile.TemporaryDirectory(prefix='.~print-frames-', dir=root) as temporary:
            try:
                for index, frame in enumerate(project.frames):
                    video.check_cancel()
                    video.progress(.2*index/len(project.frames), 'Preparing interlace frames…')
                    with open_image(frame.path) as original:
                        raster = FittedRaster(flatten(original), frame, geometry.source, geometry.aspect)
                        path = Path(temporary)/f'{index}.rgb'
                        stream.write_raw(raster, path)
                        del raster
                    sources.append(stream.FileRaster(path, geometry.source))
                raster = InterlaceRaster(project, geometry, sources)
                if dpi is not None:
                    raster = stream.SampledRaster(raster, size)
                yield raster, geometry
            finally:
                for source in sources:
                    source.close()
    except OSError as error:
        raise UserError(f'The print raster could not be written: {error}') from error
