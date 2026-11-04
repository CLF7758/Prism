"""Lossless experimental directory -> legacy export, published only on success."""
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile

from .contracts import StorageError, StorageErrorKind
from .migration import BLOBS, INTERNAL, _check, _file_hash, read_manifest, quote


def export_legacy(folder, destination, *, experimental=False, cancel=None):
    if not experimental:
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Experimental export requires opt-in')
    folder, destination = Path(folder).resolve(strict=True), Path(destination).resolve()
    manifest = read_manifest(folder)
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    from .managed import repository_reader, revision, STATE
    before_revision = revision(folder/'metadata.sqlite') if manifest['version']==2 else None
    with tempfile.TemporaryDirectory(prefix='prism-object-index-') as cache:
        reader = repository_reader(folder)
        repository = reader.__enter__()
        descriptor, name = tempfile.mkstemp(prefix=destination.name+'.', suffix='.partial', dir=destination.parent)
        os.close(descriptor)
        temporary = Path(name)
        try:
            with closing(sqlite3.connect((folder/'metadata.sqlite').as_uri()+'?mode=ro', uri=True)) as src:
                src.execute('BEGIN')
                with closing(sqlite3.connect(temporary)) as dst:
                    src.backup(dst, pages=128, progress=lambda *_: _check(cancel))
                    dst.execute('PRAGMA synchronous=FULL')
                    dst.execute('BEGIN IMMEDIATE')
                    for owner, entity, key, length in src.execute('SELECT owner,entity,hashkey,size FROM object_refs'):
                        _check(cancel)
                        if owner not in BLOBS:
                            raise StorageError(StorageErrorKind.CORRUPT, 'Unknown original binding domain')
                        column, primary = BLOBS[owner]
                        existing = dst.execute(f'SELECT rowid,{quote(column)} FROM {quote(owner)} '
                                               f'WHERE {quote(primary)}=?', (entity,)).fetchone()
                        if existing is None or existing[1] is not None:
                            raise StorageError(StorageErrorKind.CORRUPT, 'Invalid original binding')
                        dst.execute(f'UPDATE {quote(owner)} SET {quote(column)}=zeroblob(?) '
                                    f'WHERE rowid=?', (length, existing[0]))
                        digest, written = hashlib.sha256(), 0
                        with dst.blobopen(owner, column, existing[0], readonly=False) as output:
                            for chunk in repository.open_stream(key):
                                _check(cancel)
                                digest.update(chunk)
                                written += len(chunk)
                                output.write(chunk)
                        if written != length or digest.hexdigest() != key:
                            raise StorageError(StorageErrorKind.CORRUPT, 'Original content hash/length mismatch')
                    dst.execute('DROP TABLE object_refs')
                    dst.execute(f'DROP TABLE {INTERNAL}')
                    if manifest['version']==2:
                        dst.execute(f'DROP TABLE {STATE}')
                    dst.execute(f'PRAGMA application_id={int(manifest["legacy_application_id"])}')
                    dst.execute(f'PRAGMA user_version={int(manifest["legacy_user_version"])}')
                    if dst.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                        raise StorageError(StorageErrorKind.CORRUPT, 'Export integrity check failed')
                    dst.commit()
            _check(cancel)
            if ((manifest['version']==1 and _file_hash(folder/'metadata.sqlite')!=manifest['metadata_sha256']) or
                (manifest['version']==2 and revision(folder/'metadata.sqlite')!=before_revision)):
                raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Metadata changed during export')
            with temporary.open('r+b') as handle:
                os.fsync(handle.fileno())
            _check(cancel)
            # Hard-link publication is atomic and refuses an existing name.
            # The private file lives on the same volume as the destination.
            os.link(temporary, destination)
            return destination
        finally:
            reader.__exit__(None,None,None)
            temporary.unlink(missing_ok=True)
