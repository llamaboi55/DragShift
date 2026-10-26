"""Lenticular source preparation, fixed-pitch interlace and print projects."""
from __future__ import annotations

import copy
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

from . import print_raster, print_stream, video
from .convert import atomic_save, flatten, open_image
from .util import UserError, sibling, unique_path

PROJECT_VERSION = 1


@dataclass
class Frame:
    path: Path
    fit: str = 'cover'
    crop: tuple = (0., 0., 1., 1.)
    align_x: float = .5
    align_y: float = .5


@dataclass
class Settings:
    lpi: float = 60.
    quality: int = 5
    width_mm: float = 100.
    height_mm: float = 100.
    auto_height: bool = True
    orientation: str = 'vertical'
    columns: int = 1
    rows: int = 1
    gap_mm: float = 0.
    margin_mm: float = 0.
    phase: int = 0
    black_edges: bool = False
    export_dpi: int = 0
    auto_quality: bool = False


@dataclass
class Geometry:
    source: tuple
    tile: tuple
    sheet: tuple
    gap: int
    margin: int
    dpi: float
    aspect: float = 1.

    @property
    def size_mm(self):
        return tuple(value * 25.4 / self.dpi for value in self.sheet)


@dataclass
class Project:
    frames: list[Frame] = field(default_factory=list)
    settings: Settings = field(default_factory=Settings)

    def snapshot(self):
        return copy.deepcopy(self)

    def validate(self, *, require_frames=True):
        settings = self.settings
        if require_frames and not 2 <= len(self.frames) <= 30:
            raise UserError('Choose between 2 and 30 source images.')
        numbers = (settings.lpi, settings.quality, settings.width_mm, settings.height_mm,
                   settings.gap_mm, settings.margin_mm, settings.columns, settings.rows, settings.phase, settings.export_dpi)
        if (not all(isinstance(v, (int, float)) and math.isfinite(v) for v in numbers)
                or not 1 <= settings.lpi <= 1000 or not 1 <= settings.quality <= 100
                or settings.quality != int(settings.quality)
                or not 1 <= settings.width_mm <= 2500 or not 1 <= settings.height_mm <= 2500
                or not 0 <= settings.gap_mm <= 100 or not 0 <= settings.margin_mm <= 100
                or settings.orientation not in ('vertical', 'horizontal')
                or not 1 <= settings.columns <= 20 or not 1 <= settings.rows <= 20
                or any(v != int(v) for v in (settings.columns, settings.rows, settings.phase))
                or not 0 <= settings.phase <= 100
                or settings.export_dpi != int(settings.export_dpi) or not 0 <= settings.export_dpi <= 9600):
            raise UserError('Choose valid print dimensions, pitch, quality and sheet settings.')
        for frame in self.frames:
            try:
                left, top, right, bottom = frame.crop
                values = (left, top, right, bottom, frame.align_x, frame.align_y)
                valid = (all(math.isfinite(v) for v in values) and 0 <= left < right <= 1
                         and 0 <= top < bottom <= 1 and 0 <= frame.align_x <= 1 and 0 <= frame.align_y <= 1)
            except (ValueError, TypeError):
                valid = False
            if not valid or frame.fit not in ('cover', 'contain', 'stretch'):
                raise UserError('Choose valid image fitting and crop settings.')

    def geometry(self, first_size=None):
        self.validate()
        if first_size is None:
            first_size = source_size(self.frames[0].path)
        settings, count = self.settings, len(self.frames)
        # One base sample per lens, then an integer quality multiplier. Rounding
        # the whole high-resolution width gives a different lens pitch.
        base_w = math.floor(settings.width_mm / 25.4 * settings.lpi)
        aspect = first_size[0] / first_size[1] if settings.auto_height else settings.width_mm / settings.height_mm
        base_h = (round(base_w / aspect) if settings.auto_height
                  else round(settings.height_mm / 25.4 * settings.lpi))
        if min(base_w, base_h) < 1:
            raise UserError('The selected print size is smaller than one lens.')
        quality = int(settings.quality)
        source = base_w * quality, base_h * quality
        tile = source[0] * count, source[1] * count
        quantum = count * quality
        gap = round(settings.gap_mm / 25.4 * settings.lpi) * quantum
        margin = round(settings.margin_mm / 25.4 * settings.lpi) * quantum
        columns, rows = int(settings.columns), int(settings.rows)
        sheet = (tile[0] * columns + gap * (columns - 1) + 2 * margin,
                 tile[1] * rows + gap * (rows - 1) + 2 * margin)
        print_stream.checked_size(source)
        print_stream.checked_size(sheet)
        return Geometry(source, tile, sheet, gap, margin, settings.lpi * count * quality, aspect)


def source_size(path):
    try:
        with Image.open(path) as image:
            print_raster.checked_size(image.size)
            size = image.size
            if image.getexif().get(274, 1) in (5, 6, 7, 8):
                size = size[::-1]
            return size
    except (OSError, ValueError) as error:
        raise UserError(f'This source image could not be read: {Path(path).name}') from error


def prepare(frame: Frame, size, *, aspect=None):
    path = Path(frame.path)
    if not path.is_file():
        raise UserError(f'Source image is missing: {path.name}')
    try:
        with Image.open(path) as header:
            print_raster.checked_size(header.size)
        with open_image(path) as loaded:
            image = flatten(loaded).copy()
    except (OSError, ValueError) as error:
        raise UserError(f'This source image could not be read: {path.name}') from error
    return fit_image(image, frame, size, aspect=aspect)


def fit_layout(image, frame: Frame, size, *, aspect=None):
    """Crop and placement shared by in-memory and streamed source fitting."""
    image = flatten(image)
    left, top, right, bottom = frame.crop
    x = max(0, min(image.width-1, round(left*image.width)))
    y = max(0, min(image.height-1, round(top*image.height)))
    box = (x, y, min(image.width, max(x+1, round(right*image.width))),
           min(image.height, max(y+1, round(bottom*image.height))))
    image = image.crop(box)
    width, height = size
    if frame.fit == 'cover':
        ratio, desired = image.width / image.height, aspect or width / height
        if ratio > desired:
            extent = max(1, round(image.height * desired))
            offset = round((image.width - extent) * frame.align_x)
            image = image.crop((offset, 0, offset + extent, image.height))
        elif ratio < desired:
            extent = max(1, round(image.width / desired))
            offset = round((image.height - extent) * frame.align_y)
            image = image.crop((0, offset, image.width, offset + extent))
    elif frame.fit == 'contain':
        scale = min(width / image.width, height / image.height)
        inner = max(1, round(image.width * scale)), max(1, round(image.height * scale))
        return image, inner, (round((width-inner[0])*frame.align_x), round((height-inner[1])*frame.align_y))
    return image, size, (0, 0)


def fit_image(image, frame: Frame, size, *, aspect=None):
    """Shared source fitting for the full export and a cached screen preview."""
    image, inner, offset = fit_layout(image, frame, size, aspect=aspect)
    fitted = print_raster.resize(image, inner)
    if inner == size:
        return fitted
    result = Image.new('RGB', size, 'white')
    result.paste(fitted, offset)
    return result


def render(project: Project):
    """Prepare one source at a time, interleave strips, then impose whole lenses."""
    project.validate()
    geometry = project.geometry()
    print_raster.checked_size(geometry.sheet)
    settings, count = project.settings, len(project.frames)
    width, height = geometry.tile
    quality = int(settings.quality)
    output = np.empty((height, width, 3), dtype=np.uint8)
    axis_length = width if settings.orientation == 'vertical' else height
    coordinates = np.arange(axis_length, dtype=np.int64)
    phase = round(settings.phase / 100 * count * quality)
    frame_index = count - 1 - ((coordinates + phase) // quality) % count
    source_index = coordinates // (count * quality) * quality + coordinates % quality
    for index, frame in enumerate(project.frames):
        video.check_cancel()
        video.progress(index / count, 'Preparing interlace frames…')
        prepared = prepare(frame, geometry.source, aspect=geometry.aspect)
        if settings.orientation == 'vertical':
            expanded = np.asarray(print_raster.resize(prepared, (geometry.source[0], height)))
            selected = coordinates[frame_index == index]
            output[:, selected] = expanded[:, np.minimum(source_index[selected], expanded.shape[1]-1)]
        else:
            expanded = np.asarray(print_raster.resize(prepared, (width, geometry.source[1])))
            selected = coordinates[frame_index == index]
            output[selected] = expanded[np.minimum(source_index[selected], expanded.shape[0]-1)]
    if settings.black_edges:
        extent = count * quality
        if settings.orientation == 'vertical':
            output[:, :extent] = output[:, -extent:] = 0
        else:
            output[:extent] = output[-extent:] = 0
    tile = Image.fromarray(output)
    if geometry.sheet == geometry.tile:
        video.progress(1., 'Interlace ready')
        return tile, geometry
    sheet = Image.new('RGB', geometry.sheet, 'white')
    for row in range(int(settings.rows)):
        for column in range(int(settings.columns)):
            video.check_cancel()
            sheet.paste(tile, (geometry.margin + column * (width + geometry.gap),
                              geometry.margin + row * (height + geometry.gap)))
    video.progress(1., 'Interlace ready')
    return sheet, geometry


def printer_raster(image, geometry: Geometry, dpi):
    size = tuple(round(pixels / geometry.dpi * device_dpi) for pixels, device_dpi in zip(image.size, dpi))
    return print_raster.resize(image, size)


def export(project: Project, target='TIFF'):
    if target not in ('TIFF', 'PNG', 'BMP'):
        raise UserError('Choose a lossless print format: TIFF, PNG or BMP.')
    geometry = project.geometry()
    output_dpi = project.settings.export_dpi or geometry.dpi
    final_size = tuple(round(p/geometry.dpi*output_dpi) for p in geometry.sheet)
    print_stream.checked_size(final_size)
    from . import raster_export
    raster_export.validate(final_size, target, output_dpi)
    first = Path(project.frames[0].path)
    folder = sibling(first, 'Interlace', '.tif' if target == 'TIFF' else '.' + target.lower()).parent
    with tempfile.TemporaryDirectory(prefix='.~interlace-', dir=folder) as temporary:
        stage = Path(temporary)
        raster = stage / 'image'
        if needs_streaming(geometry.sheet) or needs_streaming(final_size):
            from . import lenticular_stream, raster_export
            dpi = (output_dpi, output_dpi) if project.settings.export_dpi else None
            with lenticular_stream.prepared(project, folder=stage, dpi=dpi) as (rows, _):
                raster_export.write(rows, raster, target, output_dpi)
        else:
            image, _ = render(project)
            if project.settings.export_dpi:
                image = printer_raster(image, geometry, (output_dpi, output_dpi))
            image.save(raster, target, dpi=(output_dpi, output_dpi))
        metadata = {'format': 'dragshift-print-job', 'version': 1, 'dpi': output_dpi,
                    'interlace_ppi': geometry.dpi,
                    'pixels': list(final_size), 'size_mm': [p*25.4/output_dpi for p in final_size],
                    'settings': asdict(project.settings), 'frames': len(project.frames)}
        sidecar = stage / 'settings'
        sidecar.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
        published = []
        with video._publish_lock:
            video.check_cancel()
            out = unique_path(folder / (first.stem + ' Interlace' + ('.tif' if target == 'TIFF' else '.' + target.lower())))
            information = out.with_suffix(out.suffix + '.print.json')
            while information.exists():
                out = unique_path(out.with_name(out.stem + ' (1)' + out.suffix))
                information = out.with_suffix(out.suffix + '.print.json')
            try:
                # link refuses replacement on all supported operating systems.
                for source, destination in ((raster, out), (sidecar, information)):
                    try:
                        os.link(source, destination)
                    except FileExistsError:
                        raise
                    except OSError:
                        if os.name != 'nt':
                            raise
                        os.rename(source, destination)  # Windows refuses replacement.
                    published.append(destination)
            except Exception:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
    return published


def needs_streaming(size):
    return size[0]*size[1] > 20_000_000 or max(size) > 100_000


def save_project(project: Project, path):
    project.validate(require_frames=False)
    path = Path(path).resolve()
    frames = []
    for frame in project.frames:
        value = asdict(frame)
        try:
            value['path'] = os.path.relpath(Path(frame.path).resolve(), path.parent).replace(os.sep, '/')
        except ValueError:
            value['path'] = str(Path(frame.path).resolve())
        frames.append(value)
    data = {'format': 'dragshift-lenticular', 'version': PROJECT_VERSION,
            'settings': asdict(project.settings), 'frames': frames}
    atomic_save(lambda temporary: Path(temporary).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8'), path)


def load_project(path):
    path = Path(path).resolve()
    try:
        if path.stat().st_size > 1_000_000:
            raise ValueError('size')
        data = json.loads(path.read_text(encoding='utf-8'))
        if data['format'] != 'dragshift-lenticular' or data['version'] != PROJECT_VERSION or len(data['frames']) > 30:
            raise ValueError('version or frames')
        frames = []
        for item in data['frames']:
            item = dict(item)
            item['path'] = (path.parent / item['path']).resolve()
            item['crop'] = tuple(item.get('crop', (0., 0., 1., 1.)))
            frames.append(Frame(**item))
        project = Project(frames, Settings(**data['settings']))
        project.validate(require_frames=False)
        return project
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise UserError('This print project could not be opened. Choose a valid project file.') from error
