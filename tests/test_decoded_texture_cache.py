import io
from concurrent.futures import ThreadPoolExecutor
from PIL import Image
from prism.material_textures import DecodedTextureCache


def png(color):
    stream = io.BytesIO()
    Image.new('RGB', (4,4), color).save(stream, format='PNG')
    return stream.getvalue()


def test_content_cache_reuses_decode_and_isolates_mutation(monkeypatch):
    cache = DecodedTextureCache(256)
    original_open = Image.open
    calls = []
    def counted(*a, **kw):
        calls.append(1)
        return original_open(*a, **kw)
    monkeypatch.setattr(Image, 'open', counted)
    blob = png('red')
    def use(_):
        with cache.open(blob) as image:
            assert image.getpixel((0,0)) == (255,0,0)
            image.putpixel((0,0), (0,0,0))
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(use, range(8)))
    assert len(calls) == 1
    with cache.open(png('blue')) as image:
        assert image.getpixel((0,0)) == (0,0,255)
    assert cache.bytes <= cache.budget
    assert len(cache.entries) == 1
    cache.clear()
    assert cache.bytes == 0 and not cache.entries
