import io
import pytest
from PIL import Image, UnidentifiedImageError
from prism.material_textures import open_decoded_texture, prepare_texture


def test_original_pixels_and_bytes_are_preserved(tmp_path):
    source = tmp_path / 'original.png'
    Image.new('RGBA', (3, 2), (20, 40, 60, 80)).save(source)
    blob = source.read_bytes()
    with open_decoded_texture(blob) as image:
        assert image.size == (3, 2)
        assert image.getpixel((0, 0)) == (20, 40, 60, 80)
    metadata, original = prepare_texture(source)
    assert original == blob == source.read_bytes()
    assert (metadata['width'], metadata['height']) == (3, 2)


def test_bad_image_is_rejected_by_both_entry_points(tmp_path):
    source = tmp_path / 'bad.png'
    source.write_bytes(b'not an image')
    with pytest.raises(UnidentifiedImageError):
        open_decoded_texture(source.read_bytes())
    with pytest.raises(UnidentifiedImageError):
        prepare_texture(source)
