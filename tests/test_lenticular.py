import json
import threading

import numpy as np
import pytest
from PIL import Image

from dragshift import lenticular as lens, video
from dragshift.util import UserError


@pytest.fixture
def frames(tmp_path):
    result = []
    for index in range(1, 4):
        y, x = np.mgrid[:80, :120]
        rgb = np.stack(((x*2+index*31)%256, (y*3+index*53)%256, (x+y*2+index*67)%256), axis=-1).astype(np.uint8)
        path = tmp_path / f'frame-{index}.png'
        Image.fromarray(rgb).save(path)
        result.append(lens.Frame(path))
    return result


def test_calibrated_interlace_and_integer_quality(frames):
    project = lens.Project(frames, lens.Settings(width_mm=25.4, quality=2))
    result, geometry = lens.render(project)
    assert result.size == (360, 240)
    assert geometry.source == (120, 80)
    assert geometry.dpi == 360
    assert [result.getpixel((x, 0)) for x in range(6)] == [
        (93, 159, 201), (95, 159, 202), (62, 106, 134),
        (64, 106, 135), (31, 53, 67), (33, 53, 68)]
    project.settings.orientation = 'horizontal'
    horizontal, _ = lens.render(project)
    assert [horizontal.getpixel((0, y)) for y in range(6)] == [
        (93, 159, 201), (93, 162, 203), (62, 106, 134),
        (62, 109, 136), (31, 53, 67), (31, 56, 69)]


def test_fractional_pitch_rounding_and_frame_aspect(frames):
    project = lens.Project(frames, lens.Settings(lpi=74.84, width_mm=37.9, quality=2))
    assert project.geometry().tile == (666, 444)
    assert project.geometry((120, 83)).tile == (666, 462)
    assert project.geometry().dpi == pytest.approx(449.04)
    assert project.geometry().size_mm == pytest.approx((37.672367718, 25.114911812), abs=1e-5)


@pytest.mark.parametrize('height_mm, base_height', [(25.0, 74), (25.3, 75), (38.6, 114)])
def test_manual_height_rounds_independently_of_quantized_width(frames, height_mm, base_height):
    project = lens.Project(frames, lens.Settings(lpi=74.84, width_mm=37.9,
        height_mm=height_mm, auto_height=False, quality=1))
    result, geometry = lens.render(project)
    assert geometry.source == (111, base_height)
    assert geometry.tile == result.size == (333, base_height * 3)
    assert geometry.aspect == pytest.approx(37.9 / height_mm)


def test_fit_modes_preserve_alpha_background_crop_and_focus(tmp_path):
    image = Image.new('RGBA', (20, 10), 'red')
    image.paste('blue', (10, 0, 20, 10))
    image.putpixel((0, 0), (0, 0, 0, 0))
    path = tmp_path / 'wide.webp'
    image.save(path, lossless=True)
    frame = lens.Frame(path, fit='cover', align_x=1)
    assert lens.prepare(frame, (10, 10)).getpixel((0, 5)) == (0, 0, 255)
    frame.align_x = 0
    assert lens.prepare(frame, (10, 10)).getpixel((0, 0)) == (255, 255, 255)
    frame.fit = 'contain'
    contained = lens.prepare(frame, (10, 10))
    assert contained.getpixel((5, 0)) == (255, 255, 255)
    frame.fit, frame.crop = 'stretch', (.5, 0, 1, 1)
    assert lens.prepare(frame, (10, 10)).getpixel((0, 5)) == (0, 0, 255)


def test_grid_alignment_and_lossless_export_collision(frames):
    project = lens.Project(frames, lens.Settings(width_mm=25.4, quality=1, columns=2, rows=2, gap_mm=1, margin_mm=1))
    result, geometry = lens.render(project)
    period = len(frames) * project.settings.quality
    assert geometry.gap % period == geometry.margin % period == 0
    assert result.getpixel((0, 0)) == (255, 255, 255)
    first = np.asarray(result.crop((geometry.margin, geometry.margin, geometry.margin+geometry.tile[0], geometry.margin+geometry.tile[1])))
    second = np.asarray(result.crop((geometry.margin+geometry.tile[0]+geometry.gap, geometry.margin,
                                    geometry.margin+2*geometry.tile[0]+geometry.gap, geometry.margin+geometry.tile[1])))
    assert np.array_equal(first, second)
    outputs = lens.export(project)
    again = lens.export(project)
    assert set(outputs).isdisjoint(again)
    with Image.open(outputs[0]) as written:
        assert np.array_equal(np.asarray(written), np.asarray(result))
        assert written.info['dpi'] == pytest.approx((geometry.dpi, geometry.dpi))
    assert json.loads(outputs[1].read_text())['pixels'] == list(result.size)


def test_project_roundtrip_missing_source_validation_and_cancel(frames, tmp_path):
    project = lens.Project(frames, lens.Settings(width_mm=25.4, quality=1))
    project.frames[1].fit, project.frames[1].crop = 'contain', (.1, .2, .9, 1)
    path = tmp_path / 'проект.dshift-print'
    lens.save_project(project, path)
    assert not json.loads(path.read_text(encoding='utf-8'))['frames'][0]['path'].startswith('C:')
    assert lens.load_project(path) == project
    frozen = project.snapshot()
    project.frames.reverse()
    assert frozen.frames != project.frames
    with pytest.raises(UserError):
        lens.load_project(frames[0].path)
    frozen.frames[0].path.unlink()
    with pytest.raises(UserError, match='could not be read'):
        lens.render(frozen)
    project.frames = project.frames[:-1]
    cancelled = threading.Event()
    cancelled.set()
    with video.job_context(video.Control(cancel=cancelled)):
        with pytest.raises(video.Cancelled):
            lens.render(project)
    project.settings.lpi = float('nan')
    with pytest.raises(UserError):
        project.validate()


def test_printer_raster_export_uses_same_sampling(frames):
    project = lens.Project(frames[:2], lens.Settings(width_mm=25.4, quality=1, export_dpi=600))
    original, geometry = lens.render(project)
    expected = lens.printer_raster(original, geometry, (600, 600))
    outputs = lens.export(project, 'PNG')
    with Image.open(outputs[0]) as image:
        assert image.size == (600, 400)
        assert np.array_equal(np.asarray(image), np.asarray(expected))
        assert image.info['dpi'] == pytest.approx((600, 600), abs=.01)
    metadata = json.loads(outputs[1].read_text())
    assert metadata['dpi'] == 600 and metadata['interlace_ppi'] == 120


def test_tiny_edge_crop_stays_inside_source_pixels():
    source = Image.new('RGB', (20,10), 'red')
    frame = lens.Frame('unused.png', fit='stretch', crop=(.998,.998,1,1))
    output = lens.fit_image(source,frame,(12,12))
    assert output.getextrema() == ((255,255),(0,0),(0,0))
