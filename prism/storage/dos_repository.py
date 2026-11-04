"""The disk-objectstore backend, and the strict read-only session.

The backend is `disk-objectstore` 1.5.0 (pinned, see the package
registration).  Business code never touches it: it goes through
:class:`prism.storage.object_repository.ObjectRepository`.

Why this module exists at all
-----------------------------

The default read path of the library **writes into the container folder**:
the pack index is a SQLite database that is opened with
``PRAGMA journal_mode=wal``, and that creates ``packs.idx-wal`` and
``packs.idx-shm`` next to it.  A reader that must not touch the source
directory therefore cannot simply open the container.

The measured options (feasibility run, 2026-10-25):

* reading the source directory directly **adds files to it** - not
  acceptable for a read-only source;
* copying the index to a private directory and pointing the container at
  the copy keeps the source still - but only when the index is
  **quiesced**: no live writer, no write ahead log.

So the read-only session here does three things, in this order:

1. **Refuse** when the container has a live writer (``packs.idx-wal``
   exists).  Ignoring the write ahead log would show a state that never
   existed; refusing is the honest answer.
2. **Snapshot** the index into a private directory and check the copy's
   integrity (``PRAGMA integrity_check``) before it is used.
3. **Watch the packs** while reading: if a pack file changed (size or
   mtime) between the stat before and after the read, the reader's view
   is stale and the read fails loudly instead of returning mixed bytes.

The private API
----------------

Step 2 needs one private method of the library,
``Container._get_pack_index_path``.  It is overridden in
:class:`_RedirectedIndexContainer` - **in this module only** - and the
override is checked against the pinned version at import time, so a
different library version fails loudly instead of silently reading the
wrong file.  The library is not forked and no pack format is touched.

This is the "narrow private adaptation" the feasibility report allows for
an experimental backend.  It is **not** a supported public API, and the
backend stays experimental until AI-01 approves the version pin.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import tempfile
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from prism.storage.object_repository import (
    HASH_ALGORITHM, ActiveWriter, DurableResult, ObjectCorrupt, ObjectError,
    ObjectNotFound, ObjectRepository, ObjectStat, ReadOnlyViolation,
    RepositoryClosed, RepositoryStats)

#: The library version this module is written against.
REQUIRED_DISK_OBJECTSTORE_VERSION = '1.5.0'

#: The file names the library uses inside a container folder.
INDEX_SUFFIX = '.idx'
PACK_INDEX_SUFFIX = 'packs.idx'
WAL_SUFFIX = '-wal'
SHM_SUFFIX = '-shm'

#: How much is read from the source in one go when the index is copied.
_COPY_CHUNK = 1 << 20

#: How much of an object is read at once.  The backend asks for at most
#: 512 KiB itself; this is the repository's own bound on the chunks it
#: hands out.
READ_CHUNK = 1 << 18


def _import_container():
    """Import the backend, or explain what is missing."""
    try:
        from disk_objectstore import Container, ObjectType
    except ImportError as exc:                       # pragma: no cover
        raise ObjectError(
            'disk-objectstore is not available in this interpreter; the DOS '
            'backend needs the pinned package (see the package '
            'registration for the private directory)') from exc
    version = getattr(__import__('disk_objectstore'), '__version__', '?')
    if version != REQUIRED_DISK_OBJECTSTORE_VERSION:
        raise ObjectError(
            f'disk-objectstore {version} is not the pinned version '
            f'{REQUIRED_DISK_OBJECTSTORE_VERSION}; the read-only adaptation '
            'depends on the exact index layout')
    return Container, ObjectType


Container, ObjectType = _import_container()


class _DurableMaintenanceContainer(Container):
    """Synchronize each pack before the pinned backend commits its index.

    DOS 1.5.0's Windows fullsync helper skips fsync. Keep this adaptation
    local to maintenance, without patching the library's global helpers.
    """

    @contextmanager
    def lock_pack(self, pack_id):
        with super().lock_pack(pack_id) as handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())


class _ChunkReader:
    """A file-like reader over an iterator of byte chunks.

    The backend writes with ``stream.read(n)``, so the repository's
    iterator shaped API is adapted here.  The adapter never buffers more
    than one chunk plus the bytes the caller asked for, so a 32 MiB object
    does not become a 32 MiB buffer.
    """

    def __init__(self, chunks: Iterator[bytes]):
        self._chunks = iter(chunks)
        self._buffer = b''
        self._done = False

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            parts = [self._buffer]
            self._buffer = b''
            for chunk in self._chunks:
                parts.append(chunk)
            return b''.join(parts)
        while len(self._buffer) < size and not self._done:
            try:
                self._buffer += next(self._chunks)
            except StopIteration:
                self._done = True
        out, self._buffer = self._buffer[:size], self._buffer[size:]
        return out


@dataclass(frozen=True)
class PackFingerprint:
    """What the reader saw of a pack file."""

    name: str
    size: int
    mtime_ns: int


def _pack_fingerprint(folder: Path) -> dict:
    """A cheap fingerprint of the pack files (no content hashing)."""
    result = {}
    pack_dir = folder / 'packs'
    if not pack_dir.is_dir():
        return result
    for path in sorted(pack_dir.iterdir()):
        if path.is_file():
            stat = path.stat()
            result[path.name] = PackFingerprint(path.name, stat.st_size,
                                                 stat.st_mtime_ns)
    return result


class _RedirectedIndexContainer(Container):
    """A container whose pack index lives somewhere else.

    This is the only place that touches a private method of the library.
    The override is a **path redirect**, nothing else: the pack files are
    still read from the original folder.
    """

    def __init__(self, folder, index_path: Path):
        super().__init__(folder)
        self._redirected_index_path = Path(index_path)

    def _get_pack_index_path(self) -> Path:          # noqa: D401
        """Return the index path (redirected to the private copy)."""
        return self._redirected_index_path


class DosObjectRepository(ObjectRepository):
    """A read/write container of content addressed objects."""

    backend = 'dos'

    @classmethod
    def create(cls, folder):
        """Create a new container; never clear or adopt an existing folder."""
        folder = Path(folder).resolve()
        folder.mkdir(parents=True, exist_ok=False)
        container = Container(folder)
        container.init_container(hash_type=HASH_ALGORITHM)
        return cls(folder, container=container)

    def __init__(self, folder, *, container=None):
        self._folder = Path(folder).resolve()
        self._container = container if container is not None \
            else Container(self._folder)
        self._lock = threading.Lock()
        self._stopped = False
        self._closed = False
        self._in_flight = 0
        self._sessions = 0

    # -- writing --------------------------------------------------------

    def put_stream(self, stream: Iterator[bytes], *,
                   length: Optional[int] = None) -> ObjectStat:
        """Store the bytes of ``stream``.

        The library hashes the content while it writes, so the same bytes
        are one object and different bytes never merge.
        """
        self._check_open()
        with self._lock:
            self._in_flight += 1
        try:
            hashkey = self._container.add_streamed_object(
                _ChunkReader(stream))
        finally:
            with self._lock:
                self._in_flight -= 1
        meta = self._container.get_object_meta(hashkey)
        return ObjectStat(
            hashkey=hashkey,
            length=int(getattr(meta, 'size', 0) or 0),
            stored_length=int(getattr(meta, 'pack_length', 0) or 0),
            compressed=bool(getattr(meta, 'pack_compressed', False)),
            verified=False,
            container=str(self._folder))

    # -- reading --------------------------------------------------------

    def compact_loose(self, *, check=None):
        """Caller must hold the exclusive project lease throughout."""
        from disk_objectstore import CompressMode
        from sqlalchemy import text
        self._check_open()
        container = _DurableMaintenanceContainer(self._folder)
        try:
            session = container._get_operation_session()
            if session.execute(text('PRAGMA synchronous')).scalar_one() < 2:
                raise ObjectError('Pack index requires FULL synchronous commits')
            def progress(_action, _value):
                if check:
                    check()
            # lock_pack synchronizes before pack_all_loose commits each index
            # batch. Loose originals survive cancellation or failed sync.
            container.pack_all_loose(compress=CompressMode.AUTO, do_fsync=False,
                                     clean_loose_per_pack=False, callback=progress)
            if check:
                check()
            container.clean_storage()
        finally:
            container.close()

    def put_batch(self, objects):
        """Private single-writer import: (expected hash, length, opener).

        Bounded, seekable openers are supplied by migration. On Windows 1.5.0
        the upstream fullsync helper does not execute fsync; synchronize pack
        files explicitly, then commit the FULL-synchronous index. References
        are committed separately only after verify_many succeeds.
        """
        import time
        from sqlalchemy import text
        self._check_open()
        objects=list(objects)
        if not objects:return []
        expected=[key for key,_length,_opener in objects]
        if len(expected)!=len(set(expected)):raise ValueError('Import batch must contain unique hashes')
        session=self._container._get_operation_session()
        if session.execute(text('PRAGMA synchronous')).scalar_one()<2:
            raise ObjectError('Pack index requires FULL synchronous commits')
        started=time.perf_counter();sync_seconds=0.0
        with self._lock:self._in_flight+=1
        try:
            keys=self._container.add_streamed_objects_to_pack(
                [opener() for _key,_length,opener in objects],open_streams=True,
                compress=False,no_holes=False,do_fsync=False,do_commit=False)
            if keys!=expected:raise ObjectCorrupt('Source stream changed during batch import')
            metadata=[self._container.get_object_meta(key) for key in keys]
            if any(meta.size!=length for meta,(_key,length,_opener) in zip(metadata,objects)):
                raise ObjectCorrupt('Source stream length changed during batch import')
            packs={meta.pack_id for meta in metadata}
            for pack in packs:
                begin=time.perf_counter()
                with self._container._get_pack_path_from_pack_id(pack).open('r+b') as stream:
                    os.fsync(stream.fileno())
                sync_seconds+=time.perf_counter()-begin
            begin=time.perf_counter();session.commit();commit_seconds=time.perf_counter()-begin
            self.last_write_metrics=dict(objects=len(keys),payload_bytes=sum(row[1] for row in objects),
                pack_sync_count=len(packs),pack_sync_ms=sync_seconds*1000,
                index_commit_ms=commit_seconds*1000,write_ms=(time.perf_counter()-started)*1000)
            return keys
        except BaseException:
            session.rollback()
            raise
        finally:
            with self._lock:self._in_flight-=1

    def verify_many(self, expected,*,check=None):
        """Read each requested unique object once, in backend storage order."""
        import time
        self._check_open();expected=dict(expected);seen=set();total=0
        started=time.perf_counter()
        with self._container.get_objects_stream_and_meta(list(expected)) as generated, closing(generated) as streams:
            # DOS's outer context manager yields a generator without closing
            # it. Cancellation/corruption traceback retention must not pin an
            # open pack and prevent Windows publication or later maintenance.
            for key,stream,_meta in streams:
                if check:check()
                if stream is None:raise ObjectNotFound(f'Missing imported object {key}')
                digest=hashlib.sha256();length=0
                while chunk:=stream.read(READ_CHUNK):
                    if check:check()
                    digest.update(chunk);length+=len(chunk)
                if key not in expected or digest.hexdigest()!=key or length!=expected[key]:
                    raise ObjectCorrupt(f'Imported object failed verification: {key}')
                seen.add(key);total+=length
        if seen!=set(expected):raise ObjectNotFound('Import verification omitted an object')
        self.last_verify_metrics=dict(objects=len(seen),read_bytes=total,
                                      verify_ms=(time.perf_counter()-started)*1000)
        return seen

    def open_stream(self, hashkey: str) -> Iterator[bytes]:
        """The bytes of one object, in bounded chunks.  Not verified."""
        self._check_open()
        if not self._container.has_object(hashkey):
            raise ObjectNotFound(f'no object {hashkey} in {self._folder}')
        return self._iter_object(hashkey)

    def _iter_object(self, hashkey: str, *, chunk_size: int = READ_CHUNK):
        """Yield the object's bytes, reading at most `chunk_size` at once.

        The backend hands out a file-like handle; it is read in bounded
        pieces and closed when the generator ends.
        """
        with self._container.get_object_stream(hashkey) as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def stat(self, hashkey: str) -> Optional[ObjectStat]:
        """What is known about one object, or ``None``."""
        if self._closed:
            return None
        if not self._container.has_object(hashkey):
            return None
        meta = self._container.get_object_meta(hashkey)
        return ObjectStat(
            hashkey=hashkey,
            length=int(getattr(meta, 'size', 0) or 0),
            stored_length=int(getattr(meta, 'pack_length', 0) or 0),
            compressed=bool(getattr(meta, 'pack_compressed', False)),
            verified=False,
            container=str(self._folder))

    def verify(self, hashkey: str) -> DurableResult:
        """Read the object back and recompute its content hash."""
        digest = hashlib.new(HASH_ALGORITHM)
        length = 0
        try:
            for chunk in self.open_stream(hashkey):
                digest.update(chunk)
                length += len(chunk)
        except ObjectNotFound:
            return DurableResult(hashkey=hashkey, length=0, durable=False,
                                 verified=False, container=str(self._folder),
                                 reason='missing')
        matched = digest.hexdigest() == hashkey
        if not matched:
            raise ObjectCorrupt(
                f'object {hashkey} read back as {digest.hexdigest()}')
        return DurableResult(hashkey=hashkey, length=length, durable=True,
                             verified=True, container=str(self._folder))

    def materialize(self, hashkey: str, destination) -> DurableResult:
        """Write the object to ``destination`` byte for byte."""
        digest = hashlib.new(HASH_ALGORITHM)
        length = 0
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as handle:
            for chunk in self.open_stream(hashkey):
                digest.update(chunk)
                length += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        matched = digest.hexdigest() == hashkey
        if not matched:
            raise ObjectCorrupt(
                f'object {hashkey} materialized as {digest.hexdigest()}')
        return DurableResult(hashkey=hashkey, length=length, durable=True,
                             verified=True, fsynced=True,
                             container=str(self._folder))

    # -- closing --------------------------------------------------------

    def stop(self) -> None:
        """Stop accepting new work."""
        with self._lock:
            self._stopped = True

    def drain(self, timeout: Optional[float] = None) -> bool:
        """Wait until nothing is in flight."""
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                busy = self._in_flight
            if not busy:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        """Release the handles.  Idempotent."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopped = True
        try:
            self._container.close()
        except Exception:                              # noqa: BLE001
            pass

    def stats(self) -> RepositoryStats:
        return RepositoryStats(backend=self.backend,
                               container=str(self._folder),
                               stopped=self._stopped, closed=self._closed,
                               in_flight=self._in_flight)

    # -- internals ------------------------------------------------------

    def _check_open(self) -> None:
        if self._closed:
            raise RepositoryClosed('the repository is closed')
        if self._stopped:
            raise RepositoryClosed('the repository stopped accepting work')


class ReadOnlyDosRepository(ObjectRepository):
    """A strictly read-only view of a container folder.

    The source folder is never written to.  That is not a promise, it is
    checked: the test measures the file set and the content hashes of the
    source before and after reading.

    The session refuses to open a container that has a live writer; see
    the module docstring for why.
    """

    backend = 'dos-readonly'

    def __init__(self, folder, *, cache_dir, append_only=False):
        self._folder = Path(folder).resolve()
        self._cache = Path(cache_dir).resolve()
        if self._cache.is_relative_to(self._folder):
            raise ReadOnlyViolation('The private index cache must be outside the source container')
        self._cache.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._stopped = False
        self._closed = False
        self._in_flight = 0
        self._container = None
        self._index_copy = None
        self._pack_fingerprint = {}
        self._append_only = append_only

    # -- opening --------------------------------------------------------

    @classmethod
    def open(cls, folder, *, cache_dir, append_only=False) -> 'ReadOnlyDosRepository':
        """Open a read-only session.

        Raises :class:`ActiveWriter` when the container has a live writer
        and :class:`ObjectCorrupt` when the index snapshot does not check
        out.
        """
        repository = cls(folder, cache_dir=cache_dir, append_only=append_only)
        repository._open_session()
        return repository

    def _open_session(self) -> None:
        index = self._folder / PACK_INDEX_SUFFIX
        wal = self._folder / (PACK_INDEX_SUFFIX + WAL_SUFFIX)
        shm = self._folder / (PACK_INDEX_SUFFIX + SHM_SUFFIX)
        busy_wal = lambda: wal.exists() and (not self._append_only or wal.stat().st_size > 0)
        if busy_wal():
            raise ActiveWriter(
                f'{wal.name} exists: the container has a live writer, a read '
                'only snapshot would show a state that never existed')
        if not index.exists():
            raise ObjectNotFound(f'{index} does not exist')
        # Many projects have a folder named "objects". A basename-derived
        # cache name lets another session overwrite this session's index.
        descriptor, name = tempfile.mkstemp(prefix='prism-packs-', suffix='.idx', dir=self._cache)
        os.close(descriptor)
        copy = Path(name)
        before = index.stat()
        packs = _pack_fingerprint(self._folder)
        try:
            shutil.copyfile(index, copy)
            self._check_index_copy(copy)
            after = index.stat()
            if (busy_wal() or (before.st_size, before.st_mtime_ns, before.st_ino) !=
                    (after.st_size, after.st_mtime_ns, after.st_ino) or
                    packs != _pack_fingerprint(self._folder)):
                raise ActiveWriter('The source index/packs changed during snapshot')
        except BaseException:
            copy.unlink(missing_ok=True)
            raise
        self._index_copy = copy
        self._container = _RedirectedIndexContainer(self._folder, copy)
        self._pack_fingerprint = packs

    @staticmethod
    def _check_index_copy(copy: Path) -> None:
        """The snapshot has to be a usable index, not a truncated file."""
        try:
            connection = sqlite3.connect(f'file:{copy}?mode=ro', uri=True)
        except sqlite3.Error as exc:
            raise ObjectCorrupt(f'cannot open the index snapshot: {exc}') from exc
        try:
            row = connection.execute('PRAGMA integrity_check').fetchone()
            if not row or str(row[0]).lower() != 'ok':
                raise ObjectCorrupt(
                    f'the index snapshot does not check out: {row}')
        finally:
            connection.close()

    # -- reading --------------------------------------------------------

    def open_stream(self, hashkey: str) -> Iterator[bytes]:
        """The bytes of one object, in bounded chunks.  Not verified."""
        self._check_open()
        if self._container is None:
            raise RepositoryClosed('the session is not open')
        if not self._container.has_object(hashkey):
            raise ObjectNotFound(f'no object {hashkey} in {self._folder}')
        before = _pack_fingerprint(self._folder)
        return self._guarded_stream(hashkey, before)

    def _iter_object(self, hashkey: str, *, chunk_size: int = READ_CHUNK):
        with self._container.get_object_stream(hashkey) as handle:
            while True:
                chunk = handle.read(chunk_size)
                if not chunk:
                    break
                yield chunk

    def _guarded_stream(self, hashkey, before):
        """Yield the bytes, and fail if a pack changed under the reader."""
        with self._lock:
            self._in_flight += 1
        try:
            for chunk in self._iter_object(hashkey):
                yield chunk
            after = _pack_fingerprint(self._folder)
            if after != before:
                raise ActiveWriter(
                    'a pack file changed while the object was read; the '
                    'reader view is stale and the bytes are not trustworthy')
        finally:
            with self._lock:
                self._in_flight -= 1

    def stat(self, hashkey: str) -> Optional[ObjectStat]:
        if self._closed or self._container is None:
            return None
        if not self._container.has_object(hashkey):
            return None
        meta = self._container.get_object_meta(hashkey)
        return ObjectStat(
            hashkey=hashkey,
            length=int(getattr(meta, 'size', 0) or 0),
            stored_length=int(getattr(meta, 'pack_length', 0) or 0),
            compressed=bool(getattr(meta, 'pack_compressed', False)),
            verified=False,
            container=str(self._folder))

    def verify(self, hashkey: str) -> DurableResult:
        digest = hashlib.new(HASH_ALGORITHM)
        length = 0
        try:
            for chunk in self.open_stream(hashkey):
                digest.update(chunk)
                length += len(chunk)
        except ObjectNotFound:
            return DurableResult(hashkey=hashkey, length=0, durable=False,
                                 verified=False, container=str(self._folder),
                                 reason='missing')
        matched = digest.hexdigest() == hashkey
        if not matched:
            raise ObjectCorrupt(
                f'object {hashkey} read back as {digest.hexdigest()}')
        return DurableResult(hashkey=hashkey, length=length, durable=True,
                             verified=True, container=str(self._folder))

    def put_stream(self, stream: Iterator[bytes], *,
                   length: Optional[int] = None) -> ObjectStat:
        raise ReadOnlyViolation('this repository is read only')

    def materialize(self, hashkey: str, destination) -> DurableResult:
        digest = hashlib.new(HASH_ALGORITHM)
        length = 0
        target = Path(destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('wb') as handle:
            for chunk in self.open_stream(hashkey):
                digest.update(chunk)
                length += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        if digest.hexdigest() != hashkey:
            raise ObjectCorrupt(
                f'object {hashkey} materialized as {digest.hexdigest()}')
        return DurableResult(hashkey=hashkey, length=length, durable=True,
                             verified=True, fsynced=True,
                             container=str(self._folder))

    # -- closing --------------------------------------------------------

    def stop(self) -> None:
        with self._lock:
            self._stopped = True

    def drain(self, timeout: Optional[float] = None) -> bool:
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                busy = self._in_flight
            if not busy:
                return True
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._stopped = True
            container = self._container
            self._container = None
        if container is not None:
            try:
                container.close()
            except Exception:                          # noqa: BLE001
                pass

    def stats(self) -> RepositoryStats:
        return RepositoryStats(backend=self.backend,
                               container=str(self._folder),
                               stopped=self._stopped, closed=self._closed,
                               in_flight=self._in_flight,
                               notes=(f'index snapshot: {self._index_copy}',))

    def _check_open(self) -> None:
        if self._closed:
            raise RepositoryClosed('the session is closed')
        if self._stopped:
            raise RepositoryClosed('the session stopped accepting work')


def open_readonly(folder, *, cache_dir) -> ReadOnlyDosRepository:
    """Open a strictly read-only view of a container folder."""
    return ReadOnlyDosRepository.open(folder, cache_dir=cache_dir)
