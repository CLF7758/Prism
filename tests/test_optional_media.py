import pytest

from prism.fileio.image import _load_with_openimageio


def test_float_exr_tonemap(tmp_path):
    oiio = pytest.importorskip('OpenImageIO')
    np = pytest.importorskip('numpy')
    path = str(tmp_path / 'linear.exr')
    output = oiio.ImageOutput.create(path)
    assert output.open(path, oiio.ImageSpec(2, 2, 3, oiio.FLOAT))
    assert output.write_image(np.ones((2, 2, 3), dtype=np.float32))
    output.close()
    image = _load_with_openimageio(path)
    assert image.width() == 2
    assert 180 <= image.pixelColor(0, 0).red() <= 190
