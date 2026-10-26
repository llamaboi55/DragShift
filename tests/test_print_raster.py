import numpy as np
import pytest
from PIL import Image

from dragshift import print_raster
from dragshift.util import UserError


def test_fixed_grid_preserves_calibrated_interpolation_and_edge_pixels():
    image = Image.fromarray(np.array([[[0, 100, 250], [100, 0, 50]],
                                     [[200, 200, 0], [50, 40, 255]]], dtype=np.uint8))
    result = np.asarray(print_raster.resize(image, (5, 5)))
    assert result[0, 0].tolist() == [0, 100, 250]
    assert result[1, 1].tolist() == [80, 90, 143]
    assert result[-1, -1].tolist() == [50, 40, 255]
    assert np.array_equal(np.asarray(print_raster.resize(image, (2, 2))), np.asarray(image))


def test_area_sampling_uses_integer_boxes_and_floor():
    image = Image.fromarray(np.arange(4 * 4 * 3, dtype=np.uint8).reshape(4, 4, 3))
    result = np.asarray(print_raster.resize(image, (2, 2)))
    assert result.tolist() == [[[7, 8, 9], [13, 14, 15]], [[31, 32, 33], [37, 38, 39]]]


def test_print_sampling_checks_size_and_cancellation(monkeypatch):
    with pytest.raises(UserError):
        print_raster.resize(Image.new('RGB', (1, 1)), (100_000, 100_000))
    calls = []
    def stop():
        calls.append(1)
        raise UserError('cancelled')
    monkeypatch.setattr(print_raster.video, 'check_cancel', stop)
    with pytest.raises(UserError, match='cancelled'):
        print_raster.resize(Image.new('RGB', (4, 4)), (8, 8))
    assert len(calls) == 1
