"""Cold codec loading belongs to the worker, including missing-backend fallback."""
import builtins
import threading

from PyQt6 import QtCore, QtWidgets

from prism import video_thumbnails


def test_cold_or_missing_codec_never_loads_on_gui_thread(qapp, qtbot, tmp_path, monkeypatch):
    path = tmp_path / 'clip.mp4'
    path.write_bytes(b'fixture')
    item = QtWidgets.QGraphicsPixmapItem()
    item._video_url = QtCore.QUrl.fromLocalFile(str(path))
    imports, fallbacks = [], []
    gui_thread = threading.get_ident()
    item._capture_thumbnail_qt = lambda: fallbacks.append(threading.get_ident())
    original_import = builtins.__import__

    def import_codec(name, *args, **kwargs):
        if name == 'prism.fileio.video_pyav':
            imports.append(threading.get_ident())
            assert threading.get_ident() != gui_thread, 'codec import blocked GUI'
            raise ImportError('optional codec unavailable')
        return original_import(name, *args, **kwargs)

    service = video_thumbnails.Thumbnails(qapp)
    monkeypatch.setattr(video_thumbnails, '_service', service)
    monkeypatch.setattr(builtins, '__import__', import_codec)
    assert video_thumbnails.request(item) is True
    qtbot.waitUntil(lambda: bool(fallbacks), timeout=5000)
    assert imports and all(value != gui_thread for value in imports)
    assert fallbacks == [gui_thread]
    assert not service.running
    service.pool.waitForDone(5000)
    service.deleteLater()
