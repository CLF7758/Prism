from PyQt6 import QtCore, QtGui
from prism import image_pyramid, image_decode, thumbnail_cache


def test_pyramid_reuses_lower_levels_without_decoding(tmp_path, monkeypatch):
    store = thumbnail_cache.ThumbnailCache(str(tmp_path/'pyramid'), max_side=1024)
    monkeypatch.setattr(image_pyramid, 'cache', lambda: store)
    calls = []
    def decode(*args, **kwargs):
        calls.append(kwargs['max_side'])
        image = QtGui.QImage(512, 256, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        return image, QtCore.QSize(4096, 2048)
    monkeypatch.setattr(image_decode, 'decode', decode)
    image_pyramid.decode_display(blob=b'original', max_side=512)
    result = image_pyramid.decode_display(blob=b'original', max_side=64)
    assert calls == [512]
    assert result[0].size() == QtCore.QSize(64, 32)
    assert result[1] == QtCore.QSize(4096, 2048)
    image_pyramid.decode_display(blob=b'replaced', max_side=64)
    assert calls == [512, 64]
    store.close()


def test_file_version_changes_cache_identity(tmp_path):
    path = tmp_path/'source.png'
    path.write_bytes(b'one')
    old = image_pyramid.source_key(None, str(path), None)
    path.write_bytes(b'longer replacement')
    assert image_pyramid.source_key(None, str(path), None) != old
