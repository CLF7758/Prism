"""Directory-project runtime: SQLite metadata + immutable content objects.

The SQLite transaction is the commit record. Object bytes are durable before
their references commit; aborted appends may leave reclaimable objects, never
references to absent data. The manifest is stable across ordinary saves.
"""
from contextlib import closing, contextmanager
from dataclasses import replace
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile

from .contracts import StorageError, StorageErrorKind
from .migration import METADATA_APPID, BLOBS, CHUNK, quote, _check, _manifest
from .leases import exclusive_destination, project_lease

MANAGED_FORMAT = 'prism-directory'
STATE = '_prism_storage_state'


def root_for(path):
    if not path:
        return None
    path = Path(path).resolve()
    if path.suffix.lower()=='.prism':
        from .entrypoint import is_entry,resolve
        if is_entry(path):return resolve(path)
    if path.name not in ('metadata.sqlite', 'project.json') and not path.is_dir():
        return None
    folder = path if path.is_dir() else path.parent
    marker = folder / 'project.json'
    if not marker.is_file():
        return None
    try:value = json.loads(marker.read_text(encoding='utf-8'))
    except (ValueError,OSError):return None
    return folder if value.get('format') in (MANAGED_FORMAT, 'prism-directory-experimental') else None


def database_path(path):
    folder = root_for(path)
    if folder is None:
        if (Path(path).is_dir() or Path(path).name=='project.json' or
                (Path(path).name=='metadata.sqlite' and (Path(path).parent/'project.json').exists())):
            raise StorageError(StorageErrorKind.CORRUPT,'目录中没有有效的 Prism project.json')
        return str(path)
    from .migration import read_manifest
    read_manifest(folder)
    return str(folder / 'metadata.sqlite')


def is_directory_target(path):
    return str(path).lower().endswith(('.prismproj','.prism.data')) or (root_for(path) is not None and Path(path).name != 'metadata.sqlite')


def initialize(folder):
    """Finalize a converted immutable v1 checkpoint into writable v2."""
    folder = Path(folder)
    value = json.loads((folder/'project.json').read_text(encoding='utf-8'))
    from .schema import legacy_schema
    if value['legacy_user_version'] != legacy_schema().USER_VERSION:
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,
                           'Open and save this old-format project with Prism before converting it')
    with closing(sqlite3.connect(folder/'metadata.sqlite')) as conn:
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute(f'PRAGMA user_version={value["legacy_user_version"]}')
        conn.execute(f'CREATE TABLE IF NOT EXISTS {STATE}(id INTEGER PRIMARY KEY CHECK(id=1),'
                     'instance TEXT NOT NULL,revision INTEGER NOT NULL)')
        conn.execute(f'INSERT OR IGNORE INTO {STATE} VALUES(1,?,0)', (value['storage_instance_id'],))
        conn.commit()
    lock = folder/'.prism-storage.lock'
    if not lock.exists():
        with lock.open('xb') as handle:
            handle.write(b'\0')
    value.update(format=MANAGED_FORMAT, version=2, integrity='sqlite-transaction-revision')
    value.pop('metadata_sha256', None)
    _manifest(folder/'project.json', value)
    return value


def revision(path):
    folder = root_for(path)
    if folder is None:
        return None
    with closing(sqlite3.connect((folder/'metadata.sqlite').as_uri()+'?mode=ro', uri=True)) as conn:
        row = conn.execute(f'SELECT instance,revision FROM {STATE} WHERE id=1').fetchone()
    return tuple(row)


@contextmanager
def repository_reader(folder):
    from .dos_repository import ReadOnlyDosRepository
    folder = Path(folder)
    lease = project_lease(folder) if (folder/'.prism-storage.lock').exists() else _no_lease()
    with lease, tempfile.TemporaryDirectory(prefix='prism-managed-index-') as cache:
        repository = ReadOnlyDosRepository.open(folder/'objects', cache_dir=cache, append_only=True)
        try:
            yield repository
        finally:
            repository.close()


@contextmanager
def _no_lease():
    yield


def binding(path, owner, entity, *, item_id=False):
    folder = root_for(path)
    if folder is None:
        return None
    with closing(sqlite3.connect((folder/'metadata.sqlite').as_uri()+'?mode=ro', uri=True)) as conn:
        if item_id:
            row = conn.execute('SELECT name FROM sqlar WHERE item_id=?', (entity,)).fetchone()
            if row is None:
                return None
            entity = row[0]
        return conn.execute('SELECT hashkey,size FROM object_refs WHERE owner=? AND entity=?',
                            (owner, str(entity))).fetchone()


@contextmanager
def open_original(path, owner, entity, *, item_id=False):
    folder = root_for(path)
    ref = binding(path, owner, entity, item_id=item_id)
    if ref is None:
        raise StorageError(StorageErrorKind.MISSING, 'Original object reference is missing')
    with repository_reader(folder) as repository:
        # Stream directly from the backend, with verification at EOF. Consumers
        # must finish the context before publishing their output.
        stream = _VerifiedStream(repository.open_stream(ref[0]), ref[0], ref[1])
        try:
            yield stream
            while stream.read(CHUNK):
                pass
        finally:
            stream.close()


class _VerifiedStream(io.RawIOBase):
    def __init__(self, chunks, key, length):
        self.chunks, self.key, self.length = iter(chunks), key, length
        self.buffer, self.digest, self.count, self.eof = b'', hashlib.sha256(), 0, False
    def readable(self): return True
    def read(self, size=-1):
        if size == 0: return b''
        while not self.eof and (size < 0 or len(self.buffer) < size):
            chunk = next(self.chunks, None)
            if chunk is None:
                self.eof = True
                if self.count != self.length or self.digest.hexdigest() != self.key:
                    raise StorageError(StorageErrorKind.CORRUPT, 'Original object hash or length mismatch')
            else:
                self.digest.update(chunk); self.count += len(chunk); self.buffer += chunk
        if size < 0:
            data, self.buffer = self.buffer, b''
        else:
            data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data
    def close(self):
        close = getattr(self.chunks, 'close', None)
        if close: close()
        super().close()


def read_bytes(path, owner, entity, *, item_id=False):
    with open_original(path, owner, entity, item_id=item_id) as stream:
        return stream.read()


def _externalize(connection, repository, owner, ids, cancel):
    column, primary = BLOBS[owner]
    for entity in ids:
        _check(cancel)
        row = connection.execute(f'SELECT rowid,length({quote(column)}) FROM {quote(owner)} '
                                 f'WHERE {quote(primary)}=? AND {quote(column)} IS NOT NULL', (entity,)).fetchone()
        if row is None:
            continue
        digest = hashlib.sha256()
        def chunks():
            with connection.blobopen(owner, column, row[0], readonly=True) as stream:
                while chunk := stream.read(CHUNK):
                    _check(cancel); digest.update(chunk); yield chunk
        stat = repository.put_stream(chunks(), length=row[1])
        verified = repository.verify(stat.hashkey)
        if not verified.ok or verified.length != row[1] or stat.hashkey != digest.hexdigest():
            raise StorageError(StorageErrorKind.CORRUPT, 'New original did not verify')
        connection.execute('INSERT OR REPLACE INTO object_refs VALUES(?,?,?,?)',
                           (owner, str(entity), stat.hashkey, row[1]))
        connection.execute(f'UPDATE {quote(owner)} SET {quote(column)}=NULL WHERE rowid=?', (row[0],))


def commit_partial(snapshot, filename=None, *, cancel=None, commit_report=None):
    """Normal incremental save: no old original is copied or re-encoded."""
    folder = root_for(snapshot.source)
    target = Path(filename or snapshot.source).resolve()
    if folder is None or target != Path(snapshot.source).resolve():
        raise ValueError('Managed in-place commit requires its captured metadata source')
    from .migration import read_manifest
    manifest = read_manifest(folder)
    if manifest['version'] != 2:
        raise StorageError(StorageErrorKind.READ_ONLY, 'This experimental v1 project needs conversion before editing')
    from .dos_repository import DosObjectRepository
    from prism.fileio.partial_save import _write_partial_snapshot
    with exclusive_destination(folder/'metadata.sqlite'), project_lease(folder):
        repository = DosObjectRepository(folder/'objects')
        try:
            def hook(connection, inserted, catalog_ids):
                ids = tuple(row[0] for key in catalog_ids for row in connection.execute('SELECT name FROM sqlar WHERE item_id=?', (key,)))
                _externalize(connection, repository, 'sqlar', ids, cancel)
                _externalize(connection, repository, 'attachments', [row[0] for row in snapshot.attachment_writes], cancel)
                for identifier in snapshot.attachment_removals:
                    connection.execute('DELETE FROM object_refs WHERE owner=? AND entity=?', ('attachments', str(identifier)))
                connection.execute(f'UPDATE {STATE} SET revision=revision+1 WHERE id=1')
            return _write_partial_snapshot(snapshot, target, cancel=cancel,
                commit_report=commit_report, _storage_hook=hook)
        finally:
            repository.close()


def rebase_snapshot(snapshot, filename):
    path = Path(filename).resolve()
    stat = path.stat()
    return replace(snapshot, source=str(path), source_identity=(stat.st_dev, stat.st_ino))
