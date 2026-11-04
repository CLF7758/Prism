"""Experimental, resumable legacy conversion. Not a default GUI save format.

Only a new destination is published. Original SQLite columns and DDL survive;
sqlar/attachment BLOBs move verbatim into content-addressed objects. Unsupported
extensions fail before creating a destination. No image transcoding occurs.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import uuid

from .contracts import StorageError, StorageErrorKind
from .migration_plan import preflight_source

FORMAT = 'prism-directory-experimental'
FORMAT_VERSION = 1
METADATA_APPID = 0x50524431
BLOBS = {'sqlar': ('data', 'name'), 'attachments': ('blob', 'id')}
INTERNAL = '_prism_storage_progress'
VERIFIED = '_prism_storage_verified'
BATCH_ITEMS = 64
BATCH_BYTES = 64 * 1024 * 1024
CHUNK = 256 * 1024


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def _check(cancel):
    if cancel is not None and cancel():
        raise StorageError(StorageErrorKind.CANCELLED, 'Conversion cancelled; checkpoint retained')


def _token(source):
    from prism.source_fingerprint import file_fingerprint
    return file_fingerprint(source)


def _file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024*1024):
            digest.update(chunk)
    return digest.hexdigest()


def _manifest(path, value):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_manifest(folder):
    value = json.loads((Path(folder) / 'project.json').read_text(encoding='utf-8'))
    if value.get('format') == 'prism-directory' and value.get('version') == 2:
        if value.get('phase') != 'verified':
            raise StorageError(StorageErrorKind.NOT_LOADED, 'Conversion has not been verified')
        with closing(sqlite3.connect((Path(folder)/'metadata.sqlite').resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            row = conn.execute('SELECT instance,revision FROM _prism_storage_state WHERE id=1').fetchone()
            appid = conn.execute('PRAGMA application_id').fetchone()[0]
        if not row or row[0] != value.get('storage_instance_id') or appid != METADATA_APPID:
            raise StorageError(StorageErrorKind.CORRUPT, 'Directory metadata identity mismatch')
        return value
    if value.get('format') != FORMAT or value.get('version') != FORMAT_VERSION:
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Unknown directory format')
    if value.get('phase') != 'verified':
        raise StorageError(StorageErrorKind.NOT_LOADED, 'Conversion has not been verified')
    if _file_hash(Path(folder) / 'metadata.sqlite') != value.get('metadata_sha256'):
        raise StorageError(StorageErrorKind.CORRUPT, 'Converted metadata identity changed')
    return value


def _tables(connection):
    return connection.execute("SELECT name,sql FROM sqlite_master WHERE type='table' "
                              "AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()


def _columns(connection, table):
    return connection.execute(f'PRAGMA table_info({quote(table)})').fetchall()


def _stream(connection, table, column, rowid, cancel=None, digest=None):
    with connection.blobopen(table, column, rowid, readonly=True) as handle:
        while chunk := handle.read(CHUNK):
            _check(cancel)
            if digest is not None:
                digest.update(chunk)
            yield chunk



class _BlobReader:
    def __init__(self,blob,cancel,metrics):self.blob,self.cancel,self.metrics=blob,cancel,metrics
    def read(self,size=-1):
        _check(self.cancel)
        data=self.blob.read(CHUNK if size<0 else min(size,CHUNK))
        self.metrics['source_read_bytes']+=len(data)
        return data
    def seek(self,*args):return self.blob.seek(*args)
    def tell(self):return self.blob.tell()


def _batches(rows):
    batch=[];size=0
    for row in rows:
        if batch and (len(batch)>=BATCH_ITEMS or size+row[2]>BATCH_BYTES):
            yield batch;batch=[];size=0
        batch.append(row);size+=row[2]
    if batch:yield batch


def _copy_objects(src,dst,repository,names,cancel,progress):
    import time
    from contextlib import contextmanager
    epoch=uuid.uuid4().hex
    copied=processed=0
    totals=dict(source_read_bytes=0,target_payload_bytes=0,verify_read_bytes=0,
                new_objects=0,pack_sync_count=0,pack_sync_ms=0.0,index_commit_ms=0.0,
                metadata_commit_ms=0.0)
    for table,(column,primary) in BLOBS.items():
        if table not in names:continue
        rows=src.execute(f'SELECT rowid,{quote(primary)},length({quote(column)}) FROM {quote(table)} '
                         f'WHERE {quote(column)} IS NOT NULL ORDER BY rowid')
        for batch in _batches(rows):
            _check(cancel);started=time.perf_counter();new={};verify={};references=[]
            for rowid,entity,length in batch:
                if entity is None:
                    raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,'NULL original binding key')
                digest=hashlib.sha256();actual=0
                for chunk in _stream(src,table,column,rowid,cancel):
                    digest.update(chunk);actual+=len(chunk)
                totals['source_read_bytes']+=actual
                if actual!=length:raise StorageError(StorageErrorKind.CORRUPT,'Source original length changed')
                key=digest.hexdigest();entity=str(entity)
                existing=dst.execute('SELECT hashkey,size FROM object_refs WHERE owner=? AND entity=?',(table,entity)).fetchone()
                if existing is not None and existing!=(key,length):
                    raise StorageError(StorageErrorKind.CORRUPT,'Checkpoint original binding changed')
                references.append((entity,key,length,existing is None))
                proved=dst.execute(f'SELECT 1 FROM {VERIFIED} WHERE hashkey=? AND size=? AND epoch=?',(key,length,epoch)).fetchone()
                if proved:continue
                verify[key]=length
                stat=repository.stat(key)
                if stat is not None:
                    if stat.length!=length:raise StorageError(StorageErrorKind.CORRUPT,'Checkpoint original length mismatch')
                    continue
                if existing is not None:
                    raise StorageError(StorageErrorKind.CORRUPT,'Checkpoint references a missing original')
                @contextmanager
                def opener(rowid=rowid,table=table,column=column):
                    with src.blobopen(table,column,rowid,readonly=True) as blob:
                        yield _BlobReader(blob,cancel,totals)
                new.setdefault(key,(key,length,opener))
            if new:
                repository.put_batch(new.values())
                metrics=repository.last_write_metrics
                totals['new_objects']+=len(new);totals['target_payload_bytes']+=metrics['payload_bytes']
                for field in ('pack_sync_count','pack_sync_ms','index_commit_ms'):
                    totals[field]+=metrics[field]
            if verify:
                repository.verify_many(verify,check=lambda:_check(cancel))
                totals['verify_read_bytes']+=repository.last_verify_metrics['read_bytes']
            _check(cancel)
            for key,length in verify.items():
                dst.execute(f'INSERT OR REPLACE INTO {VERIFIED} VALUES(?,?,?)',(key,length,epoch))
            for entity,key,length,is_new in references:
                if is_new:
                    dst.execute('INSERT INTO object_refs VALUES(?,?,?,?)',(table,entity,key,length));copied+=1
            tick=time.perf_counter();dst.commit();totals['metadata_commit_ms']+=(time.perf_counter()-tick)*1000
            processed+=len(batch)
            if progress:progress(dict(phase='objects',copied_this_run=copied,processed=processed,
                                      batch_ms=(time.perf_counter()-started)*1000,**totals))

def convert_legacy(source, destination, *, experimental=False, cancel=None, progress=None,
                   managed=False, prepare=None,publish_directory=True):
    if not experimental:
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Experimental conversion requires opt-in')
    if Path(destination).exists() and publish_directory:
        raise FileExistsError(destination)
    from .leases import exclusive_destination
    # Never allow two processes to operate on the same resumable checkpoint.
    with exclusive_destination(destination):
        return _convert_legacy(source, destination, cancel=cancel, progress=progress,
                               managed=managed, prepare=prepare,publish_directory=publish_directory)


def _convert_legacy(source, destination, *, cancel=None, progress=None, managed=False, prepare=None,publish_directory=True):
    """Explicit opt-in conversion, resumable through a private sibling folder.

    ``progress`` reports work, never an invented percentage. Objects precede
    reference commits. Cancellation may leave unreferenced immutable objects;
    it never publishes incomplete data or removes the source.
    """
    source, destination = Path(source).resolve(strict=True), Path(destination).resolve()
    if destination.exists() and publish_directory:
        raise FileExistsError(destination)
    state = _token(source)
    preflight = preflight_source(source)
    if not preflight.supported:
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, '; '.join(preflight.blockers))
    from .schema import legacy_schema
    APPLICATION_ID, USER_VERSION = legacy_schema().APPLICATION_ID, legacy_schema().USER_VERSION
    if preflight.user_version < 1 or preflight.user_version > USER_VERSION or preflight.application_id not in (0, APPLICATION_ID):
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Unsupported legacy identity/version')
    staging = destination.with_name(destination.name + '.incomplete') if publish_directory else destination
    from .dos_repository import DosObjectRepository
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=.25)) as src:
        src.execute('BEGIN')  # Keep the committed source snapshot throughout the conversion.
        tables = _tables(src)
        if state != _token(source):
            raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Source changed during preflight')
        names = {name for name, _ in tables}
        if {'object_refs', INTERNAL, VERIFIED} & names:
            raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Reserved table name in source')
        for table, ddl in tables:
            if any(row[6] for row in src.execute(f'PRAGMA table_xinfo({quote(table)})')):
                raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'Generated/hidden columns are not supported yet')
            column_names = {r[1].casefold() for r in _columns(src, table)}
            if 'WITHOUT ROWID' not in ddl.upper() and {'rowid', '_rowid_', 'oid'} <= column_names:
                raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, 'All SQLite rowid aliases are shadowed')
            if table in BLOBS:
                column, _ = BLOBS[table]
                info = {row[1]: row for row in _columns(src, table)}
                if column not in info or info[column][3] or 'WITHOUT ROWID' in ddl.upper():
                    raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,
                                       f'Unsupported BLOB constraints in {table}; source untouched')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not staging.exists():
            required = preflight.space.total_bytes
            if shutil.disk_usage(destination.parent).free < required:
                raise StorageError(StorageErrorKind.INSUFFICIENT_SPACE, 'Insufficient conversion workspace')
            staging.mkdir()
            identity_row = (src.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()
                            if 'prism_metadata' in names else None)
            manifest = dict(format=FORMAT, version=FORMAT_VERSION, phase='copying',
                            source=state, project_id=identity_row[0] if identity_row else None,
                            storage_instance_id=uuid.uuid4().hex,
                            legacy_application_id=preflight.application_id,
                            legacy_user_version=preflight.user_version,
                            guarantees=dict(original_bytes='verbatim', power_loss='not_verified'))
            _manifest(staging / 'project.json', manifest)
            repository = DosObjectRepository.create(staging / 'objects')
        else:
            manifest = json.loads((staging / 'project.json').read_text(encoding='utf-8'))
            if (manifest.get('format'), manifest.get('version'), manifest.get('source')) != (FORMAT, FORMAT_VERSION, state):
                raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Source changed since checkpoint; refuse resume')
            repository = DosObjectRepository(staging / 'objects')
        try:
            with closing(sqlite3.connect(staging / 'metadata.sqlite')) as dst:
                dst.execute('PRAGMA synchronous=FULL')
                dst.execute(f'PRAGMA application_id={METADATA_APPID}')
                dst.execute(f'CREATE TABLE IF NOT EXISTS {INTERNAL}(name TEXT PRIMARY KEY, done INTEGER)')
                dst.execute('CREATE TABLE IF NOT EXISTS object_refs(owner TEXT,entity TEXT,hashkey TEXT,size INTEGER,'
                            'PRIMARY KEY(owner,entity))')
                dst.commit()
                for table, ddl in tables:
                    _check(cancel)
                    if dst.execute(f'SELECT done FROM {INTERNAL} WHERE name=?', (table,)).fetchone():
                        continue
                    columns = [r[1] for r in _columns(src, table)]
                    blob = BLOBS.get(table, (None,))[0]
                    projection = ','.join('NULL' if col == blob else quote(col) for col in columns)
                    targets = ','.join(quote(col) for col in columns)
                    if 'WITHOUT ROWID' not in ddl.upper():
                        alias = next(name for name in ('rowid', '_rowid_', 'oid')
                                     if name not in {col.casefold() for col in columns})
                        projection = alias + ',' + projection
                        targets = alias + ',' + targets
                        order = f' ORDER BY {alias}'
                        count = len(columns) + 1
                    else:
                        order, count = '', len(columns)
                    with dst:
                        dst.execute('BEGIN IMMEDIATE')
                        dst.execute(ddl)
                        insert = f'INSERT INTO {quote(table)}({targets}) VALUES({",".join("?" for _ in range(count))})'
                        for row in src.execute(f'SELECT {projection} FROM {quote(table)}{order}'):
                            _check(cancel)
                            dst.execute(insert, row)
                        dst.execute(f'INSERT INTO {INTERNAL} VALUES(?,1)', (table,))
                    if progress:
                        progress(dict(phase='metadata', table=table))
                # Preserve explicit indexes and AUTOINCREMENT's high-water marks.
                with dst:
                    for (ddl,) in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
                        if not dst.execute('SELECT 1 FROM sqlite_master WHERE sql=?', (ddl,)).fetchone():
                            dst.execute(ddl)
                    if src.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                        for name, seq in src.execute('SELECT name,seq FROM sqlite_sequence'):
                            dst.execute('DELETE FROM sqlite_sequence WHERE name=?', (name,))
                            dst.execute('INSERT INTO sqlite_sequence VALUES(?,?)', (name, seq))
                # Original bytes are staged directly into durable packs in
                # bounded batches. Metadata references follow read-back proof.
                dst.execute(f'CREATE TABLE IF NOT EXISTS {VERIFIED}(hashkey TEXT PRIMARY KEY,size INTEGER,epoch TEXT)')
                dst.commit()
                _copy_objects(src,dst,repository,names,cancel,progress)
                dst.execute(f'DROP TABLE {VERIFIED}')
                dst.commit()
                if progress:
                    progress(dict(phase='verification'))
                if dst.execute('PRAGMA integrity_check').fetchone() != ('ok',):
                    raise StorageError(StorageErrorKind.CORRUPT, 'Converted metadata failed integrity check')
                for table, (column, primary) in BLOBS.items():
                    if table in names:
                        expected = src.execute(f'SELECT COUNT(*) FROM {quote(table)} WHERE {quote(column)} IS NOT NULL').fetchone()[0]
                        actual = dst.execute('SELECT COUNT(*) FROM object_refs WHERE owner=?', (table,)).fetchone()[0]
                        if expected != actual:
                            raise StorageError(StorageErrorKind.CORRUPT, 'Missing original bindings')
                # Integrity_check cannot detect a valid but wrongly edited row.
                # Compare every scalar/unknown value to the held source snapshot.
                for table, _ddl in tables:
                    columns = [r[1] for r in _columns(src, table)]
                    blob = BLOBS.get(table, (None,))[0]
                    projection = ','.join('NULL' if col == blob else quote(col) for col in columns)
                    # Stable primary-key ordering; SQLar names and attachment IDs
                    # remain independent of their physical SQLite page layout.
                    pks = sorted((r[5], r[1]) for r in _columns(src, table) if r[5])
                    order = ','.join(quote(name) for _, name in pks) or 'rowid'
                    sql = f'SELECT {projection} FROM {quote(table)} ORDER BY {order}'
                    left, right = src.execute(sql), dst.execute(sql)
                    while True:
                        _check(cancel)
                        expected, actual = left.fetchone(), right.fetchone()
                        if expected != actual:
                            raise StorageError(StorageErrorKind.CORRUPT, f'Metadata changed: {table}')
                        if expected is None:
                            break
            if state != _token(source):
                raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Source changed during conversion')
            manifest['phase'] = 'verified'
            manifest['metadata_sha256'] = _file_hash(staging / 'metadata.sqlite')
            _manifest(staging / 'project.json', manifest)
        finally:
            repository.close()
    _check(cancel)
    if managed:
        from .managed import initialize
        if json.loads((staging/'project.json').read_text(encoding='utf-8')).get('version') != 2:
            initialize(staging)
        if prepare is not None:
            prepare(staging / 'metadata.sqlite')
        manifest = read_manifest(staging)
    # New destination only: a failed conversion remains resumable, never a valid project.
    _check(cancel)
    if not publish_directory:
        return manifest  # The caller publishes the verified small entry last.
    try:
        staging.rename(destination)
    except PermissionError as error:
        raise StorageError(StorageErrorKind.SOURCE_BUSY,
            'Verified checkpoint retained; Windows prevented directory publication. Retry resume after the holder releases it.',
            detail=str(staging)) from error
    return manifest
