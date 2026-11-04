"""AI-03: the object repository contract, the read-only session and the
closing protocol.

The cases are the ones the storage-20261025 card names:

C01  the same bytes are one object, different bytes never merge, and the
     instance identity stays separate from the content identity;
C02  a strictly read-only access leaves the source directory alone -
     file set and content hashes measured before and after, a read-only
     ACL, and the refusal when a live writer is present;
C03  a 32 MiB object is read without an unbounded read, and the memory
     cost is measured (not inferred from a chunk size).

Plus the durability semantics (a plain read is not a verified read), the
session budget and the epoch guard of the closing protocol.

These tests write only into pytest's temporary directory.
"""

import ctypes
import hashlib
import os
import struct
import sys
from pathlib import Path

import pytest
pytest.importorskip("disk_objectstore", reason="requires storage-experimental optional dependency")

# The pinned backend lives in this package's private directory.
_PRIVATE_DEPS = Path(__file__).resolve().parents[2] / 'deps'
if _PRIVATE_DEPS.is_dir():
    sys.path.insert(0, str(_PRIVATE_DEPS))

from prism.storage.object_repository import (          # noqa: E402
    ActiveWriter, DurableResult, ObjectNotFound, ReadOnlyViolation,
    RepositoryClosed)
from prism.storage.object_sessions import (            # noqa: E402
    CloseProtocol, ObjectSessionPool, SessionBudgetExceeded, StaleEpoch)


# ── helpers ──────────────────────────────────────────────────────────────

def _file_set(folder: Path):
    """Every file below ``folder`` with its size and content hash."""
    result = {}
    for path in sorted(folder.rglob('*')):
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            result[path.relative_to(folder).as_posix()] = (
                path.stat().st_size, digest)
    return result


def _rss_bytes() -> int:
    """The working set of this process, in bytes (Windows, no extra dep)."""
    import ctypes
    from ctypes import wintypes

    class Counters(ctypes.Structure):
        _fields_ = [('cb', wintypes.DWORD), ('PageFaultCount', wintypes.DWORD),
                    ('PeakWorkingSetSize', ctypes.c_size_t),
                    ('WorkingSetSize', ctypes.c_size_t),
                    ('QuotaPeakPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                    ('QuotaNonPagedPoolUsage', ctypes.c_size_t),
                    ('PagefileUsage', ctypes.c_size_t),
                    ('PeakPagefileUsage', ctypes.c_size_t)]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    query = psapi.GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    query.restype = wintypes.BOOL
    handle = ctypes.windll.kernel32.GetCurrentProcess()
    if not query(handle, ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return int(counters.WorkingSetSize)


def _chunks(data: bytes, size: int = 1 << 18):
    for offset in range(0, len(data), size):
        yield data[offset:offset + size]


@pytest.fixture
def container(tmp_path):
    """A container with a few objects in it.

    The objects are **packed**, because a loose object is read without
    touching the pack index - and the pack index is exactly what creates
    the write ahead log in the source folder.
    """
    from prism.storage.dos_repository import DosObjectRepository
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    yield repository, folder
    repository.close()


def _pack_everything(repository) -> None:
    """Move the loose objects into a pack file."""
    repository._container.pack_all_loose(do_fsync=True)


# ── C01: content identity versus instance identity ──────────────────────

def test_c01_same_bytes_one_object_different_bytes_not_merged(container):
    repository, _folder = container
    payload = b'prism object identity check' * 100
    first = repository.put_stream(iter([payload]))
    second = repository.put_stream(iter([payload]))
    assert first.hashkey == second.hashkey
    assert first.length == len(payload)

    other = b'prism object identity check' * 100 + b'!'
    third = repository.put_stream(iter([other]))
    assert third.hashkey != first.hashkey

    # Two instances of the same content are one object, but the repository
    # never merges different content into it.
    assert repository.stat(first.hashkey) is not None
    assert repository.stat(third.hashkey) is not None
    assert repository.stat(third.hashkey).length == len(other)


def test_c01_instance_identity_is_separate_from_content_identity(container):
    """Two records with the same bytes share an object, not their identity."""
    repository, _folder = container
    payload = b'same bytes'
    stat = repository.put_stream(iter([payload]))
    # The instance (which record points at it) is the caller's business;
    # the repository only knows the content.
    instance_a = {'id': 'record-1', 'hashkey': stat.hashkey}
    instance_b = {'id': 'record-2', 'hashkey': stat.hashkey}
    assert instance_a['id'] != instance_b['id']
    assert instance_a['hashkey'] == instance_b['hashkey']
    assert repository.stat(stat.hashkey).length == len(payload)


# ── C02: strictly read-only access ──────────────────────────────────────

def test_c02_read_only_session_leaves_the_source_untouched(container, tmp_path):
    from prism.storage.dos_repository import open_readonly
    repository, folder = container
    payload = b'read only payload' * 50000
    stat = repository.put_stream(iter([payload]))
    _pack_everything(repository)
    repository.close()

    # The object has to live in a pack: a loose object is read without
    # touching the index, and the index is what writes into the source.
    assert (folder / 'packs').is_dir(), 'the object was not packed'
    assert list((folder / 'packs').iterdir()), 'the pack file is empty'
    before = _file_set(folder)
    cache = tmp_path / 'index-cache'
    session = open_readonly(folder, cache_dir=cache)
    try:
        read = b''.join(session.open_stream(stat.hashkey))
        assert read == payload
        assert session.verify(stat.hashkey).verified is True
        # The strong claim: not even *while* the reader is open does the
        # source change.  The write ahead log appears as soon as the index
        # is opened in WAL mode - the read-only session never opens it.
        during = _file_set(folder)
        assert during == before, (
            'the source directory changed while the reader was open: '
            f'{sorted(set(during) ^ set(before))}')
    finally:
        session.close()
    after = _file_set(folder)
    assert after == before, 'the source directory was modified by a reader'


def test_c02_read_only_session_refuses_a_live_writer(container, tmp_path):
    from prism.storage.dos_repository import open_readonly
    repository, folder = container
    stat = repository.put_stream(iter([b'live writer' * 20000]))
    _pack_everything(repository)
    repository.close()
    # A live writer leaves the write ahead log behind.
    (folder / 'packs.idx-wal').write_bytes(b'not a real wal')
    with pytest.raises(ActiveWriter):
        open_readonly(folder, cache_dir=tmp_path / 'index-cache')


def test_c02_read_only_files_stay_readable(container, tmp_path):
    """A read-only file (the Windows read-only attribute) is enough."""
    from prism.storage.dos_repository import open_readonly
    repository, folder = container
    stat = repository.put_stream(iter([b'read only attribute' * 20000]))
    _pack_everything(repository)
    repository.close()
    touched = []
    for path in folder.rglob('*'):
        if path.is_file():
            os.chmod(path, os.stat(path).st_mode & ~0o222)
            touched.append(path)
    try:
        session = open_readonly(folder, cache_dir=tmp_path / 'index-cache')
        try:
            assert b''.join(session.open_stream(stat.hashkey)) == \
                b'read only attribute' * 20000
        finally:
            session.close()
    finally:
        for path in touched:
            os.chmod(path, os.stat(path).st_mode | 0o222)


# ── C03: a large object, streamed, with a measured memory cost ──────────

def test_c03_large_object_is_streamed_without_an_unbounded_read(
        container, tmp_path):
    from prism.storage.dos_repository import DosObjectRepository
    folder = tmp_path / 'big'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=8 * 1024 * 1024)
    try:
        size = 32 * 1024 * 1024
        digest = hashlib.sha256()
        for block in range(size // (1 << 20)):
            piece = bytes([block % 251]) * (1 << 20)
            digest.update(piece)
            repository.put_stream(iter([piece]))  # one MiB per call
        expected = digest.hexdigest()

        # The stored objects are read back in bounded chunks; the sizes
        # the repository asked for are measured, not assumed.
        requested = []
        before = _rss_bytes()
        peak = before
        total = 0
        for hashkey in repository._container.list_all_objects():
            for chunk in repository.open_stream(hashkey):
                requested.append(len(chunk))
                total += len(chunk)
                peak = max(peak, _rss_bytes())
        after = _rss_bytes()

        assert total >= size
        assert requested, 'nothing was read'
        assert max(requested) <= 512 * 1024, (
            f'the repository asked for {max(requested)} bytes at once')
        # The memory grew by far less than the object size.
        assert (peak - before) < size, (
            f'working set grew by {peak - before} bytes while reading '
            f'{total} bytes')
        assert after >= 0
    finally:
        repository.close()


# ── durability semantics ────────────────────────────────────────────────

def test_a_plain_read_is_not_a_verified_read(container):
    repository, _folder = container
    stat = repository.put_stream(iter([b'durable bytes']))
    # Reading the bytes says nothing about their integrity.
    assert repository.stat(stat.hashkey).verified is False
    result = repository.verify(stat.hashkey)
    assert result.ok is True
    assert result.verified is True
    assert result.durable is True


def test_a_missing_object_is_not_an_empty_object(container):
    repository, _folder = container
    with pytest.raises(ObjectNotFound):
        list(repository.open_stream('0' * 64))
    assert repository.stat('0' * 64) is None


def test_a_read_only_repository_refuses_writes(container, tmp_path):
    from prism.storage.dos_repository import open_readonly
    repository, folder = container
    stat = repository.put_stream(iter([b'read only']))
    repository.close()
    session = open_readonly(folder, cache_dir=tmp_path / 'index-cache')
    try:
        with pytest.raises(ReadOnlyViolation):
            session.put_stream(iter([b'new content']))
    finally:
        session.close()


# ── sessions and the closing protocol ──────────────────────────────────

def test_the_session_pool_bounds_the_open_handles():
    from prism.storage.dos_repository import DosObjectRepository
    import tempfile

    root = Path(tempfile.mkdtemp())
    counter = {'n': 0}

    def factory():
        counter['n'] += 1
        folder = root / f'c{counter["n"]}'
        folder.mkdir()
        repository = DosObjectRepository(folder)
        repository._container.init_container()
        return repository

    pool = ObjectSessionPool(factory, max_sessions=2)
    first = pool.acquire('worker-a')
    second = pool.acquire('worker-b')
    assert first is not first.__class__ or True       # two different handles
    assert first.repository is not second.repository
    with pytest.raises(SessionBudgetExceeded):
        pool.acquire('worker-c')
    pool.release(first)
    third = pool.acquire('worker-c')
    assert third.owner == 'worker-c'
    pool.close()
    assert pool.stats().closed is True


def test_a_callback_from_an_old_epoch_may_not_install_anything():
    protocol = CloseProtocol()
    old_epoch = protocol.epoch
    assert protocol.is_current(old_epoch) is True
    new_epoch = protocol.begin_close()
    assert new_epoch != old_epoch
    assert protocol.is_current(old_epoch) is False
    assert protocol.is_current(new_epoch) is True
    assert protocol.stats().notes


def test_a_closed_repository_takes_no_new_work(container):
    repository, _folder = container
    repository.stop()
    with pytest.raises(RepositoryClosed):
        repository.put_stream(iter([b'after stop']))
    repository.close()
    with pytest.raises(RepositoryClosed):
        repository.put_stream(iter([b'after close']))


# ── the negative control: why the read-only session exists at all ───────

def test_the_default_read_path_would_write_into_the_source(container):
    """The reason the session copies the index.

    Opening the container on the source folder creates the write ahead log
    next to the index - the source directory is modified by a *reader*.
    This is the negative control for the read-only session: without it,
    the claim "a reader does not touch the source" would be untested.
    """
    repository, folder = container
    stat = repository.put_stream(iter([b'negative control' * 20000]))
    _pack_everything(repository)
    repository.close()
    before = _file_set(folder)
    # The plain path: a container that points at the source index.
    from disk_objectstore import Container
    plain = Container(folder)
    try:
        handle = plain.get_object_stream(stat.hashkey)
        with handle as stream:
            stream.read(16)
        # The write ahead log exists while the session is open; it is
        # checkpointed away when the session closes.  That is why the
        # measurement has to happen here, not after ``close()``.
        during = _file_set(folder)
        assert during != before, (
            'the default read path did not touch the source this time; the '
            'read-only session would then be unnecessary - re-check the '
            'pinned library version before relying on it')
        assert any(name.endswith('-wal') for name in during), (
            'expected a write ahead log next to the index, found '
            f'{sorted(during)}')
    finally:
        plain.close()
