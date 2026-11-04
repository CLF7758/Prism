"""Retained video shutdown must release Qt and tolerate Windows sharing locks."""
import sys
import tempfile
from pathlib import Path

import pytest
from PyQt6 import QtCore, QtWidgets, sip
from prism import source_store
from prism.items import PrismVideoItem


@pytest.mark.skipif(sys.platform != 'win32', reason='Windows file sharing semantics')
def test_retained_directory_cleanup_defers_locked_video_without_raising(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    original = tempfile.TemporaryDirectory
    def directory(**kwargs):
        return original(dir=tmp_path, **kwargs)
    monkeypatch.setattr(source_store.tempfile, 'TemporaryDirectory', directory)
    monkeypatch.setattr(source_store, '_directory', None)
    path = Path(source_store.reserve_file('.mp4'))
    path.write_bytes(b'private retained test video')
    store = source_store._directory
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 0, None, 3, 0x80, None)
    assert handle != wintypes.HANDLE(-1).value
    try:
        store.cleanup()  # used to escape tempfile's exit finalizer as WinError 32
        assert path.exists()
    finally:
        kernel.CloseHandle(handle)
        store.cleanup()
    assert not Path(store.name).exists()


class _Player(QtCore.QObject):
    mediaStatusChanged = QtCore.pyqtSignal()
    positionChanged = QtCore.pyqtSignal()
    errorOccurred = QtCore.pyqtSignal()
    def __init__(self, parent):
        super().__init__(parent)
        self._source, self._video, self._audio = QtCore.QUrl('file:///fixture.mp4'), None, None
    def stop(self): pass
    def setSource(self, value): self._source = value
    def source(self): return self._source
    def setVideoOutput(self, value): self._video = value
    def videoOutput(self): return self._video
    def setAudioOutput(self, value): self._audio = value
    def audioOutput(self): return self._audio


class _Sink(QtCore.QObject):
    videoFrameChanged = QtCore.pyqtSignal()


@pytest.mark.parametrize('remove_from_scene', [False, True])
def test_video_cleanup_detaches_paused_player_and_deletes_qt_owner(qapp, remove_from_scene):
    item = PrismVideoItem(filename='fixture.mp4')
    scene = QtWidgets.QGraphicsScene()
    scene.addItem(item)
    player = _Player(qapp)
    audio, sink = QtCore.QObject(player), _Sink(player)
    player.setAudioOutput(audio)
    player.setVideoOutput(sink)
    item._player, item._audio_output, item._video_sink = player, audio, sink
    item._playing = False
    if remove_from_scene:
        scene.removeItem(item)
    else:
        item.cleanup()
    assert item._player is None and item._video_sink is None
    assert player.source().isEmpty()
    assert player.videoOutput() is None and player.audioOutput() is None
    QtCore.QCoreApplication.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert sip.isdeleted(player) and sip.isdeleted(audio) and sip.isdeleted(sink)
    item.cleanup()  # idempotent after actual QObject destruction
