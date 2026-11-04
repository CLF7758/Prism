"""Explicit recovery of private conversion/Save As checkpoints.

Never publish a folder just because its manifest exists. Verify SQLite and
every referenced immutable original before the single directory publication.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .contracts import StorageError, StorageErrorKind
from .leases import exclusive_destination
from .migration import read_manifest, convert_legacy, _check


def recover(checkpoint, *, cancel=None):
    checkpoint = Path(checkpoint).resolve(strict=True)
    entry_payload=checkpoint.name.lower().endswith('.prism.data')
    if not checkpoint.is_dir() or not (entry_payload or checkpoint.name.endswith('.incomplete')):
        raise ValueError('Select a Prism .incomplete or unpublished .prism.data directory')
    destination = checkpoint.with_name(checkpoint.name[:-5] if entry_payload else checkpoint.name[:-len('.incomplete')])
    if destination.exists():
        raise FileExistsError(destination)
    value = json.loads((checkpoint/'project.json').read_text(encoding='utf-8'))
    if value.get('version') == 1 and value.get('phase') != 'verified':
        # The migration engine verifies the original source fingerprint and
        # its committed checkpoint offset before resuming.
        convert_legacy(value['source']['path'], checkpoint if entry_payload else destination,
                       experimental=True,managed=True,cancel=cancel,publish_directory=not entry_payload)
        if not entry_payload:return destination
    with exclusive_destination(destination):
        _check(cancel)
        value = read_manifest(checkpoint)
        with closing(sqlite3.connect(checkpoint/'metadata.sqlite')) as connection:
            if connection.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise StorageError(StorageErrorKind.CORRUPT, 'Checkpoint metadata failed integrity verification')
        from .managed import repository_reader, initialize
        with closing(sqlite3.connect((checkpoint/'metadata.sqlite').as_uri()+'?mode=ro',uri=True)) as connection:
            with repository_reader(checkpoint) as repository:
                for key, length in connection.execute('SELECT DISTINCT hashkey,size FROM object_refs'):
                    _check(cancel)
                    result = repository.verify(key)
                    if not result.ok or result.length != length:
                        raise StorageError(StorageErrorKind.CORRUPT, 'Checkpoint original failed verification')
        if value['version'] == 1:
            initialize(checkpoint)
        _check(cancel)
        if entry_payload:
            from .entrypoint import publish
            return publish(checkpoint,destination)
        try:
            checkpoint.rename(destination)
        except OSError as error:
            raise StorageError(StorageErrorKind.SOURCE_BUSY,
                               'Checkpoint retained; close applications holding this folder, then retry recovery') from error
    return destination
