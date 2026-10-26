import threading

import numpy as np
import pytest
from PIL import Image

from dragshift import lenticular as lens, lenticular_stream, print_raster, print_stream, video


@pytest.mark.parametrize('size', [(5, 5), (2, 2), (18, 3), (3, 18), (1, 1), (11, 10), (4, 4)])
def test_streamed_sampler_matches_all_calibrated_paths(size):
    array = np.random.default_rng(734).integers(0, 256, (11, 13, 3), dtype=np.uint8)
    expected = np.asarray(print_raster.resize(Image.fromarray(array), size))
    raster = print_stream.SampledRaster(print_stream.ArrayRaster(array), size)
    actual = np.concatenate([raster.read_rows(y, min(y+2, size[1])) for y in range(0, size[1], 2)])
    assert np.array_equal(actual, expected)


@pytest.mark.parametrize('orientation', ['vertical', 'horizontal'])
@pytest.mark.parametrize('fit', ['cover', 'contain', 'stretch'])
@pytest.mark.parametrize('printer_dpi', [None, (117, 83), (35, 43)])
def test_complete_streamed_sheet_is_pixel_identical(tmp_path, orientation, fit, printer_dpi):
    frames = []
    for i, shape in enumerate([(19, 31, 3), (31, 17, 3), (24, 29, 3)]):
        path = tmp_path/f'{i}.png'
        Image.fromarray(np.random.default_rng(i).integers(0, 256, shape, dtype=np.uint8)).save(path)
        frames.append(lens.Frame(path, fit=fit, crop=(.07, .1, .97, .93), align_x=.3, align_y=.7))
    project = lens.Project(frames, lens.Settings(width_mm=19.1, height_mm=13.7, auto_height=False,
        lpi=28.7, quality=2, columns=2, rows=2, gap_mm=1, margin_mm=1.1,
        phase=37, black_edges=True, orientation=orientation))
    expected, geometry = lens.render(project)
    if printer_dpi:
        expected = lens.printer_raster(expected, geometry, printer_dpi)
    with lenticular_stream.prepared(project, folder=tmp_path, dpi=printer_dpi) as (raster, actual_geometry):
        assert actual_geometry == geometry
        actual = np.concatenate([raster.read_rows(y, min(y+7, raster.size[1])) for y in range(0, raster.size[1], 7)])
        assert np.array_equal(actual, np.asarray(expected))
    assert not list(tmp_path.glob('.~print-frames-*'))


def test_two_metre_geometry_and_small_photo_geometry(tmp_path):
    path = tmp_path/'source.png'
    Image.new('RGB', (744, 1053), 'red').save(path)
    project = lens.Project([lens.Frame(path)]*10, lens.Settings(width_mm=2000, height_mm=2000,
        auto_height=False, lpi=75, quality=1))
    geometry = project.geometry()
    assert geometry.sheet == (59050, 59060)
    assert geometry.dpi == 750
    project.frames = project.frames[:3]
    project.settings = lens.Settings(width_mm=210, height_mm=297.18, auto_height=False, lpi=75.5, quality=4)
    assert project.geometry().sheet == (7488, 10596)


def test_export_stream_path_and_cancellation_clean_staging(tmp_path, monkeypatch):
    path = tmp_path/'source.png'
    Image.new('RGB', (40, 20), 'purple').save(path)
    project = lens.Project([lens.Frame(path), lens.Frame(path)], lens.Settings(width_mm=25.4, quality=1))
    expected, _ = lens.render(project)
    monkeypatch.setattr(lens, 'needs_streaming', lambda size: True)
    output = lens.export(project, 'TIFF')
    with Image.open(output[0]) as image:
        assert np.array_equal(np.asarray(image), np.asarray(expected))
    cancelled = threading.Event()
    cancelled.set()
    with video.job_context(video.Control(cancel=cancelled)):
        with pytest.raises(video.Cancelled):
            lens.export(project)
    assert not list(tmp_path.glob('.~interlace-*'))
    assert not list(tmp_path.glob('.~print-frames-*'))


def test_format_limit_is_checked_before_preparing_frames(tmp_path, monkeypatch):
    path = tmp_path/'source.png'
    Image.new('RGB', (40, 20), 'purple').save(path)
    project = lens.Project([lens.Frame(path)]*10, lens.Settings(width_mm=2000,
        height_mm=2000, auto_height=False, lpi=75, quality=1))
    monkeypatch.setattr(lenticular_stream, 'prepared', lambda *a, **kw: pytest.fail('decoded before format check'))
    with pytest.raises(lens.UserError, match='4 GB'):
        lens.export(project, 'BMP')
    assert not list(tmp_path.glob('.~interlace-*'))
