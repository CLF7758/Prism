"""Bounded history must not stop capturing or delete an active original."""
import os
from pathlib import Path
from unittest.mock import Mock

from prism import capture_service as capture


def inbox(tmp_path, monkeypatch):
    directory = tmp_path/'inbox'
    directory.mkdir()
    monkeypatch.setattr(capture, 'inbox_directory', lambda: directory)
    monkeypatch.setattr(capture, '_active_capture_paths', set())
    return directory


def test_capture_keeps_working_after_100_historical_files(tmp_path, monkeypatch):
    directory = inbox(tmp_path, monkeypatch)
    for number in range(100):
        (directory/f'{number}.png').write_bytes(b'history')
    source = tmp_path/'new.png'
    source.write_bytes(b'new original')
    result = Path(capture.retain_capture(source))
    assert result.read_bytes() == source.read_bytes()
    assert len(list(directory.glob('*.png'))) == 100


def test_cleanup_expires_history_but_keeps_protected_sources(tmp_path, monkeypatch):
    directory = inbox(tmp_path, monkeypatch)
    active, old = directory/'active.png', directory/'old.png'
    for path in (active, old):
        path.write_bytes(b'original')
        os.utime(path, (1, 1))
    capture.protect_capture_paths([active])
    result = capture.cleanup_capture_inbox()
    assert active.read_bytes() == b'original'
    assert not old.exists()
    assert result['removed_files'] == 1


def test_protected_quota_does_not_block_new_capture(tmp_path, monkeypatch):
    inbox(tmp_path, monkeypatch)
    monkeypatch.setattr(capture, 'INBOX_MAX_ITEMS', 1)
    source = tmp_path/'new.png'
    source.write_bytes(b'original')
    first = Path(capture.retain_capture(source))
    second = Path(capture.retain_capture(source))
    assert first.read_bytes() == second.read_bytes() == b'original'
    assert capture.cleanup_capture_inbox()['over_budget']


def test_periodic_cleanup_uses_existing_pool_and_stops_with_service(qapp, tmp_path, monkeypatch):
    from PyQt6.QtCore import QObject
    inbox(tmp_path, monkeypatch)
    owner = QObject()
    service = capture.CaptureService(owner)
    original_pool = service.pool
    service.pool = Mock()
    try:
        assert service._inbox_cleanup_timer.interval() == 15*60*1000
        service._inbox_cleanup_timer.timeout.emit()
        service.pool.submit.assert_called_once_with(capture.cleanup_capture_inbox)
        service.close()
        assert not service._inbox_cleanup_timer.isActive()
    finally:
        original_pool.shutdown(wait=True)


def test_import_protection_lasts_until_worker_finishes(qapp, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from PyQt6.QtCore import QObject, pyqtSignal
    from prism.actions.capture_workflow import _insert_capture_images
    directory = inbox(tmp_path, monkeypatch)
    source = directory/'pending.png'
    source.write_bytes(b'original')
    os.utime(source, (1, 1))
    class Worker(QObject):
        finished = pyqtSignal()
        def isRunning(self):
            return True
    worker = Worker()
    view = SimpleNamespace(worker=worker, do_insert_images=Mock())
    _insert_capture_images(view, [str(source)])
    assert capture.cleanup_capture_inbox()['protected_files'] == 1
    assert source.exists()
    worker.finished.emit()
    assert capture.cleanup_capture_inbox()['removed_files'] == 1
