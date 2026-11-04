"""Storage safety gates on disposable originals, without a Qt application."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import io
from pathlib import Path
import os
import sqlite3
import subprocess
import sys

import pytest

pytest.importorskip('disk_objectstore')
from prism.storage.contracts import StorageError, StorageErrorKind
from prism.storage.dos_repository import DosObjectRepository
from prism.storage.leases import project_lease


def make_repository(tmp_path):
    repository = DosObjectRepository.create(tmp_path / 'objects')
    payloads = [b'original-first' * 1000, b'original-second' * 1000]
    keys = [repository.put_stream(iter([payload])).hashkey for payload in payloads]
    return repository, dict(zip(keys, payloads))


def test_pack_is_synchronized_before_index_publication(tmp_path, monkeypatch):
    repository, originals = make_repository(tmp_path)
    synced = []
    real_sync = os.fsync
    def sync(fd):
        with sqlite3.connect(tmp_path / 'objects/packs.idx') as reader:
            assert reader.execute('SELECT COUNT(*) FROM db_object').fetchone()[0] == 0
        synced.append(fd)
        real_sync(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    try:
        repository.compact_loose()
        assert synced
        assert not list(repository._container._list_loose())
        for key, payload in originals.items():
            assert b''.join(repository.open_stream(key)) == payload
    finally:
        repository.close()


def test_failed_sync_never_removes_originals_or_commits_index(tmp_path, monkeypatch):
    repository, originals = make_repository(tmp_path)
    def fail(_fd):
        raise OSError('injected pack sync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    try:
        with pytest.raises(OSError, match='injected'):
            repository.compact_loose()
        assert set(repository._container._list_loose()) == set(originals)
        with sqlite3.connect(tmp_path / 'objects/packs.idx') as reader:
            assert reader.execute('SELECT COUNT(*) FROM db_object').fetchone()[0] == 0
        for key, payload in originals.items():
            assert b''.join(repository.open_stream(key)) == payload
    finally:
        repository.close()


def test_cancellation_keeps_loose_originals(tmp_path):
    repository, originals = make_repository(tmp_path)
    def cancel():
        raise StorageError(StorageErrorKind.CANCELLED, 'cancel maintenance')
    try:
        with pytest.raises(StorageError):
            repository.compact_loose(check=cancel)
        assert set(repository._container._list_loose()) == set(originals)
        for key, payload in originals.items():
            assert b''.join(repository.open_stream(key)) == payload
    finally:
        repository.close()


def child_lease(folder, exclusive):
    return subprocess.run([sys.executable, '-c',
        'import sys; from prism.storage.leases import project_lease; '
        'from prism.storage.contracts import StorageError; '
        '\ntry:\n with project_lease(sys.argv[1],exclusive=sys.argv[2]=="1"): print("acquired")'
        '\nexcept StorageError as error: print(error.kind.value); sys.exit(3)',
        str(folder), str(int(exclusive))], capture_output=True, text=True, timeout=10)


def test_shared_readers_across_threads_and_processes_exclude_maintenance(tmp_path):
    (tmp_path / '.prism-storage.lock').write_bytes(b'\0')
    def read():
        with project_lease(tmp_path):
            return True
    with project_lease(tmp_path):
        with ThreadPoolExecutor(max_workers=1) as worker:
            assert worker.submit(read).result(timeout=5)
        result = child_lease(tmp_path, False)
        assert result.returncode == 0, result.stderr
        result = child_lease(tmp_path, True)
        assert result.returncode == 3, result.stdout + result.stderr
    with project_lease(tmp_path, exclusive=True):
        result = child_lease(tmp_path, False)
        assert result.returncode == 3, result.stdout + result.stderr
    assert child_lease(tmp_path, True).returncode == 0


def batch_object(payload, *, expected=None, length=None):
    @contextmanager
    def opener():
        with io.BytesIO(payload) as stream:
            yield stream
    return (expected or hashlib.sha256(payload).hexdigest(),
            len(payload) if length is None else length, opener)


@pytest.mark.parametrize('failure', ['hash', 'length', 'sync'])
def test_batch_failure_never_publishes_unverified_pack_index(tmp_path, monkeypatch, failure):
    repository = DosObjectRepository.create(tmp_path / 'objects')
    row = batch_object(b'unchanged original', expected='0' * 64 if failure == 'hash' else None,
                       length=999 if failure == 'length' else None)
    if failure == 'sync':
        def fail(_fd):
            raise OSError('batch sync failed')
        monkeypatch.setattr(os, 'fsync', fail)
    try:
        with pytest.raises(Exception):
            repository.put_batch([row])
        with sqlite3.connect(tmp_path / 'objects/packs.idx') as reader:
            assert reader.execute('SELECT COUNT(*) FROM db_object').fetchone()[0] == 0
    finally:
        repository.close()


def test_batch_readback_verifies_bytes_and_rejects_wrong_length(tmp_path):
    from prism.storage.object_repository import ObjectCorrupt
    repository = DosObjectRepository.create(tmp_path / 'objects')
    rows = [batch_object(b''), batch_object(b'original' * 1000)]
    try:
        assert repository.put_batch(rows) == [row[0] for row in rows]
        expected = {key: length for key, length, _opener in rows}
        assert repository.verify_many(expected) == set(expected)
        assert repository.last_verify_metrics['read_bytes'] == 8000
        expected[rows[1][0]] += 1
        with pytest.raises(ObjectCorrupt):
            repository.verify_many(expected)
    finally:
        repository.close()


def test_interrupted_readback_closes_pack_while_traceback_is_retained(tmp_path):
    from prism.storage.object_repository import ObjectCorrupt
    repository = DosObjectRepository.create(tmp_path / 'objects')
    rows = [batch_object(b'first'), batch_object(b'second')]
    try:
        repository.put_batch(rows)
        with pytest.raises(ObjectCorrupt) as caught:
            repository.verify_many({key: length + 1 for key, length, _ in rows})
        assert caught.value.__traceback__ is not None
        # A live reader handle blocks pack rename on Windows. Retaining the
        # exception must not retain the backend's generator-owned file handle.
        pack = next((tmp_path / 'objects/packs').iterdir())
        renamed = pack.with_name(pack.name + '.probe')
        pack.rename(renamed)
        renamed.rename(pack)
    finally:
        repository.close()
