"""Explicit row changes for partially loaded projects, using SQLite transactions.

This does not serialize a scene or infer deletions from absent widgets. A GUI
must capture an immutable snapshot and acknowledge only that snapshot's revision.

Covered here: updates/deletes of rows the caller names, inserts of keyed or
auto-increment rows, new assets whose original bytes still live in the item,
and attachment blobs that are already resident in memory. Everything else
(session-scoped dirty tracking, page-scoped item diffs) lives in
``prism.partial_project``; hard deletion of an asset still needs the source
lifecycle and is refused rather than guessed.
"""
from contextlib import closing, suppress
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import math
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid


logger = logging.getLogger(__name__)


# Only internal schema identifiers enter SQL. Binary source tables are excluded.
TABLES = {
    'items': ('id', ('type', 'x', 'y', 'z', 'scale', 'rotation', 'flip', 'data'), ('data',)),
    'workspace_pages': ('id', ('kind', 'title', 'data', 'sort_order'), ('data',)),
    'workspace_nodes': ('id', ('payload',), ('payload',)),
    'material_records': ('id', ('payload',), ('payload',)),
    'model_records': ('id', ('payload',), ('payload',)),
    'board_notes': ('id', ('board_id', 'html', 'updated_at'), ()),
    'board_mindmap': ('board_id', ('tree', 'updated_at'), ('tree',)),
    'prism_metadata': ('key', ('value',), ()),
}

#: Tables whose INTEGER PRIMARY KEY SQLite fills in when the insert omits it.
AUTO_KEY = frozenset({'items', 'board_notes'})

# Bounds apply to explicitly requested edits, never to a source-table scan.
# A bulk change may cover the 100k stress project plus its domain metadata.
MAX_SNAPSHOT_CHANGES = 131072


class PartialSaveConflict(RuntimeError):
    """The source or a captured record changed; never overwrite it silently."""


class PartialSaveCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class RowPatch:
    table: str
    key: int | str
    fields: tuple = ()
    json_updates: tuple = ()  # (column, mapping) pairs, encoded during capture
    json_removals: tuple = ()  # (column, keys) pairs; missing != removed
    delete: bool = False
    # Explicit whole-column replacement for caller-owned structures. Absence
    # means no replacement; a supplied None means SQL NULL. Ordinary updates
    # retain recursive merge semantics and preserve unknown JSON fields.
    json_replacements: tuple = ()


@dataclass(frozen=True)
class RowChange:
    table: str
    key: int | str
    before: tuple
    after: tuple | None
    #: Caller-supplied marker so a generated key can be mapped back to its row.
    tag: object = None


@dataclass(frozen=True)
class RowInsert:
    """A row that does not exist yet. ``values`` holds (column, value) pairs."""

    table: str
    values: tuple
    tag: object = None


@dataclass(frozen=True)
class NewItem:
    """An asset that only exists in memory; the GUI captured its columns.

    The item itself stays referenced because its original bytes are read at
    write time (same rule as the full writer). ``expected`` is the captured
    ``items`` row, so a source that changed underneath the save is refused
    instead of storing a half-updated snapshot.
    """

    item: object
    expected: tuple
    tag: object = None


@dataclass(frozen=True)
class PartialReadSnapshot:
    source: str
    source_identity: tuple
    project_id: str | None
    schema_version: int
    records: tuple


@dataclass(frozen=True)
class PartialSaveSnapshot:
    source: str
    source_identity: tuple
    project_id: str | None
    schema_version: int
    revision: str
    changes: tuple[RowChange, ...]
    baseline_checked: bool = False
    new_items: tuple[NewItem, ...] = ()
    #: (id, name, mime, blob) for attachments already resident in memory.
    attachment_writes: tuple = ()
    attachment_removals: tuple = ()
    #: (id, entry) pairs whose identity the GUI uses to clear its dirty flags.
    attachment_versions: tuple = ()
    attachment_origin: object = None
    #: Read dependencies (table, key, expected row), checked in the write transaction.
    read_guards: tuple = ()
    read_sets: tuple = ()
    required_attachments: tuple = ()


@dataclass(frozen=True)
class PartialSaveReceipt:
    filename: str
    revision: str
    changed_rows: int
    #: (tag, generated key) pairs, so a new row's id can be rebound on the GUI.
    inserted: tuple = ()
    source_before: dict | None = None
    source_after: dict | None = None
    catalog_item_ids: tuple = ()


def _identity(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _project_id(connection):
    table = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                               "AND name='prism_metadata'").fetchone()
    row = connection.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone() if table else None
    return row[0] if row else None


def _record(connection, table, key):
    primary, columns, _json_columns = TABLES[table]
    return connection.execute(f'SELECT {",".join(columns)} FROM {table} WHERE {primary}=?',
                              (key,)).fetchone()


def _domain_keys(connection, table):
    # Older projects have no optional model domain. Absence means an empty
    # membership set; a concurrently created nonempty table still conflicts.
    if table == 'model_records' and not connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
        return ()
    return tuple(row[0] for row in connection.execute(f'SELECT id FROM {table} ORDER BY id'))


def read_partial_records(source, references):
    """Capture the immutable baseline when a page is loaded, not when saved."""
    path = Path(source).resolve(strict=True)
    identity, result = _identity(path), []
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as connection:
        connection.execute('BEGIN')
        project_id = _project_id(connection)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        for table, key in references:
            if len(result) >= 1024 or table not in TABLES:
                raise ValueError('Read a bounded set of supported records')
            row = _record(connection, table, key)
            if row is None:
                raise KeyError((table, key))
            result.append((table, key, tuple(row)))
    if _identity(path) != identity:
        raise PartialSaveConflict('Source project was replaced during read')
    return PartialReadSnapshot(str(path), identity, project_id, version, tuple(result))


def _merge_json(base, changes):
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge_json(base[key], value)
        else:
            base[key] = deepcopy(value)


def item_save_columns(item):
    """Return the ``items`` columns for one item, exactly as a save writes them."""
    return (getattr(item, 'TYPE', None), item.pos().x(), item.pos().y(),
            item.zValue(), item.scale(), item.rotation(), item.flip(),
            json.dumps(item.get_extra_save_data(), ensure_ascii=False))


def _attachment_changes(connection, attachments):
    """Split resident attachment edits into writes and explicit removals.

    Only blobs already in memory are written, and the loaded attachment index
    is complete, so "no longer in the index" is a real removal rather than an
    unloaded record. A missing original is an error, never a silent drop.
    """
    if attachments is None:
        return (), (), (), None
    meta = getattr(attachments, '_meta', None)
    if meta is None:
        return (), (), (), None
    writes, versions = [], []
    for key in list(getattr(attachments, '_dirty', ())):
        entry = getattr(attachments, '_cache', {}).get(key)
        if entry is None:
            raise ValueError(f'Attachment {key} was edited but its data is not resident; save stopped')
        if entry[2] is None:
            raise ValueError(f'Original attachment {key} is unreadable; save stopped')
        writes.append((key, entry[0], entry[1], entry[2]))
        versions.append((key, entry))
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone()
    removals = ()
    if exists:
        stored = {row[0] for row in connection.execute('SELECT id FROM attachments WHERE board_id=0')}
        removals = tuple(sorted(stored - set(meta)))
    return tuple(writes), removals, tuple(versions), attachments


def _start_background_materialization(new_items):
    """Start extracting the embedded videos a partial save is going to write.

    The capture runs on the GUI thread, and pulling a large original out of
    the archive there would freeze the window.  The extraction is handed to
    ``source_store``'s background service instead, so the write thread
    finds the bytes waiting for it.
    """
    for new in new_items or ():
        item = getattr(new, 'item', new)
        request = getattr(item, 'request_materialization', None)
        if not callable(request):
            continue
        try:
            request()
        except Exception:                       # noqa: BLE001
            logger.debug('Could not start the background materialization',
                         exc_info=True)


def _apply_row_patch(patch, before):
    """Return frozen row values; never read the database or mutate the input.

    The caller owns revision validation and the SQLite read transaction. JSON
    replacement, merge and explicit removal policy lives only here.
    """
    _primary, columns, json_columns = TABLES[patch.table]
    values = dict(zip(columns, before))
    replacements = dict(patch.json_replacements)
    if len(replacements) != len(patch.json_replacements):
        raise ValueError('A structured replacement cannot repeat a column')
    if any(name not in json_columns for name in replacements):
        raise ValueError('Invalid structured-column replacement')
    if set(replacements).intersection(
            name for name, _value in (*patch.json_updates, *patch.json_removals)):
        raise ValueError('A structured replacement cannot also update or remove the same column')
    for name, value in patch.fields:
        if name not in columns or name in json_columns:
            raise ValueError('Use explicit JSON updates for structured columns')
        if not isinstance(value, (str, int, float, type(None))):
            raise TypeError('Save snapshot must contain scalar database values')
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError('Non-finite database value')
        values[name] = value
    structured = {}
    for name, mapping in patch.json_updates:
        if name not in json_columns or not isinstance(mapping, dict):
            raise ValueError('Invalid structured-column update')
        data = structured.setdefault(name, json.loads(values[name]) if values[name] else {})
        if not isinstance(data, dict):
            raise ValueError('Structured update requires an object')
        _merge_json(data, mapping)
    for name, keys in patch.json_removals:
        if name not in json_columns:
            raise ValueError('Invalid structured-column removal')
        data = structured.setdefault(name, json.loads(values[name]) if values[name] else {})
        if not isinstance(data, dict):
            raise ValueError('Structured removal requires an object')
        for key in keys:
            parts = (key,) if isinstance(key, str) else tuple(key)
            if not parts or not all(isinstance(part, str) for part in parts):
                raise ValueError('JSON removal requires a nonempty key path')
            parent = data
            for part in parts[:-1]:
                parent = parent.get(part) if isinstance(parent, dict) else None
            if isinstance(parent, dict):
                parent.pop(parts[-1], None)
    for name, data in structured.items():
        values[name] = json.dumps(data, ensure_ascii=False, allow_nan=False)
    for name, data in replacements.items():
        # Serialization freezes mutable inputs at capture time and
        # rejects non-JSON values and NaN rather than deferring errors
        # until the worker has begun writing.
        values[name] = (None if data is None else
                        json.dumps(deepcopy(data), ensure_ascii=False, allow_nan=False))
    after = None if patch.delete else tuple(values[name] for name in columns)
    return after


def capture_partial_save(source, patches, *, inserts=(), new_items=(), attachments=None,
                         baseline=None, read_guards=(), read_sets=(),
                         attachment_references=(), required_attachments=(), cancel=None):
    """Capture explicit edits. GUI callers must supply the page's read baseline.

    With no baseline this is a fresh database command, not a stale-editor check.
    Nested objects merge, lists/scalars replace; removals are always explicit.
    Unchanged rows produce no change, so unloaded records are never rewritten.
    """
    def check_cancel():
        if cancel is not None and cancel():
            raise PartialSaveCancelled('Partial save preparation cancelled')

    check_cancel()
    _start_background_materialization(new_items)
    path = Path(source).resolve(strict=True)
    identity = _identity(path)
    changes, seen = [], set()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as connection:
        connection.execute('BEGIN')
        project_id = _project_id(connection)
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        expected = {}
        if baseline is not None:
            if (baseline.source != str(path) or baseline.source_identity != identity or
                    baseline.project_id != project_id or baseline.schema_version != version):
                raise PartialSaveConflict('Loaded page belongs to a different project revision')
            expected = {(table, key): row for table, key, row in baseline.records}
        guards = []
        for table, key, row in read_guards:
            if table not in TABLES or len(guards) >= MAX_SNAPSHOT_CHANGES:
                raise ValueError('Read a bounded set of supported dependencies')
            row = tuple(row) if row is not None else None
            if _record(connection, table, key) != row:
                raise PartialSaveConflict(f'Save dependency changed: {table}/{key}')
            guards.append((table, key, row))
        sets = []
        for table, keys in read_sets:
            if table not in ('workspace_nodes', 'material_records', 'model_records'):
                raise ValueError('Unsupported domain membership dependency')
            keys = tuple(sorted(keys))
            if _domain_keys(connection, table) != keys:
                raise PartialSaveConflict(f'Save domain membership changed: {table}')
            sets.append((table, keys))
        for patch in patches:
            check_cancel()
            if len(seen) >= MAX_SNAPSHOT_CHANGES:
                raise ValueError('Too many explicit changes in one snapshot')
            if patch.table not in TABLES:
                raise ValueError('Unsupported partial-save table')
            if (patch.table, patch.key) in seen:
                raise ValueError('Coalesce changes to a row before capturing')
            seen.add((patch.table, patch.key))
            if patch.table == 'prism_metadata' and patch.key == 'project_id':
                raise ValueError('Partial saves cannot change project identity')
            if patch.table == 'items' and patch.delete:
                raise ValueError('Asset purge needs source lifecycle handling; use deletedAt for trash')
            before = _record(connection, patch.table, patch.key)
            if before is None:
                raise KeyError((patch.table, patch.key))
            if baseline is not None and expected.get((patch.table, patch.key)) != before:
                raise PartialSaveConflict('Record changed after page was loaded')
            after = _apply_row_patch(patch, before)
            if after != before:
                changes.append(RowChange(patch.table, patch.key, tuple(before), after))
        for insert in inserts:
            check_cancel()
            if insert.table not in TABLES:
                raise ValueError('Unsupported partial-save table')
            if len(seen) >= MAX_SNAPSHOT_CHANGES:
                raise ValueError('Too many explicit changes in one snapshot')
            primary, columns, json_columns = TABLES[insert.table]
            provided = dict(insert.values)
            if len(provided) != len(insert.values):
                raise ValueError('A new row cannot repeat a column')
            if set(provided) - set(columns) - {primary}:
                raise ValueError('New row has an unknown column')
            if primary not in provided and insert.table not in AUTO_KEY:
                raise ValueError('New rows need their stable key')
            if insert.table == 'items':
                raise ValueError('New assets must carry their source bytes; use new_items')
            key = provided.get(primary)
            if key is not None:
                if (insert.table, key) in seen:
                    raise ValueError('Coalesce changes to a row before capturing')
                seen.add((insert.table, key))
                if _record(connection, insert.table, key) is not None:
                    raise PartialSaveConflict(f'Row already exists: {insert.table}/{key}')
            after = []
            for name in columns:
                value = provided.get(name)
                if name in json_columns:
                    if isinstance(value, str):
                        value = json.loads(value) if value else None
                    value = json.dumps(value, ensure_ascii=False, allow_nan=False)
                elif not isinstance(value, (str, int, float, type(None))):
                    raise TypeError('Save snapshot must contain scalar database values')
                after.append(value)
            changes.append(RowChange(insert.table, key, None, tuple(after), insert.tag))
        for new in new_items:
            if len(new_items) + len(seen) > MAX_SNAPSHOT_CHANGES:
                raise ValueError('Too many explicit changes in one snapshot')
            if not isinstance(new.expected, tuple) or len(new.expected) != len(TABLES['items'][1]):
                raise TypeError('New asset must carry its captured items columns')
            if not isinstance(new.expected[-1], str):
                raise TypeError('New asset metadata must already be JSON encoded')
        attachment_writes, attachment_removals, attachment_versions, origin = \
            _attachment_changes(connection, attachments)
        if set(attachment_references) & set(attachment_removals):
            raise PartialSaveConflict('An attachment is still referenced by a material')
        pending = {key: blob for key, _name, _mime, blob in attachment_writes}
        needed = tuple(sorted(set(required_attachments)))
        for key in needed:
            if not isinstance(key, int) or isinstance(key, bool) or key < 1:
                raise ValueError('Invalid material attachment reference')
            if key in pending:
                present = bool(pending[key])
            else:
                row = connection.execute(
                    'SELECT length(blob) FROM attachments WHERE id=?', (key,)).fetchone()
                present = bool(row and row[0])
                if not present:
                    from prism.storage.managed import root_for
                    if root_for(path) is not None:
                        row=connection.execute("SELECT size FROM object_refs WHERE owner='attachments' AND entity=?",(str(key),)).fetchone()
                        present=bool(row and row[0])
            if not present:
                raise PartialSaveConflict(f'Material attachment is missing or empty: {key}')
    check_cancel()
    if _identity(path) != identity:
        raise PartialSaveConflict('Source project was replaced during capture')
    return PartialSaveSnapshot(
        str(path), identity, project_id, version, uuid.uuid4().hex, tuple(changes),
        baseline is not None, tuple(new_items), attachment_writes, attachment_removals,
        attachment_versions, origin, tuple(guards), tuple(sets), needed)


def _new_item_source(item, item_id):
    """Locate the original bytes for a brand new asset; never re-encode them.

    Mirrors ``SQLiteIO.insert_item`` so an asset saved through a partially
    loaded project keeps the bytes a full save would store. Returns
    ``(blob, path, format)``; referenced (not embedded) media stays referenced.
    """
    kind = getattr(item, 'TYPE', None)
    referenced = getattr(item, '_is_reference', False)
    if kind == 'video':
        blob = getattr(item, '_video_blob', None)
        if referenced:
            return None, None, 'video'
        if blob is None and hasattr(item, '_ensure_materialized'):
            # 内嵌视频可能尚未物化（打开只登记源引用）；保存前按需物化，
            # 否则 path 为 None 会把视频当"无源"。提取本身在后台线程上
            # 发生，这里只是等它就位。
            item._ensure_materialized()
        path = getattr(item, '_video_source_path', None)
        return (blob, None, 'video') if blob else (None, path, 'video')
    if kind == 'glb':
        blob = getattr(item, '_glb_blob', None)
        return (blob, None, 'glb') if blob and not referenced else (None, None, 'glb')
    # CapturedItem exposes image-source methods for every item type. Text and
    # paths have no image original; dispatch by the persisted type, as the full
    # writer does, rather than trying to render a worker-private snapshot.
    if kind != 'pixmap' or not hasattr(item, 'pixmap_to_bytes'):
        return None, None, None
    data = item.original_bytes() if hasattr(item, 'original_bytes') else None
    imgformat = item.original_format() if data and hasattr(item, 'original_format') else None
    if not data:
        if getattr(item, '_source_path', None) or getattr(item, '_prism_source', None):
            from prism.fileio.errors import PrismFileIOError
            raise PrismFileIOError(
                msg='Original image is missing; saving was stopped to preserve the project.',
                filename=getattr(item, 'filename', None))
        from prism.image_source import generated_png_bytes
        data, imgformat = generated_png_bytes(item)
    return data, None, (imgformat or 'png')


def _write_new_item(connection, new):
    """Insert one captured new asset plus its original bytes; return its id."""
    connection.execute(
        'INSERT INTO items (type, x, y, z, scale, rotation, flip, data) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', new.expected)
    item_id = connection.execute('SELECT last_insert_rowid()').fetchone()[0]
    item = new.item
    blob, path, imgformat = _new_item_source(item, item_id)
    if blob is None and path is None:
        return item_id
    captured_name = getattr(item, 'captured_filename_for_export', None)
    name = (captured_name(imgformat, item_id) if callable(captured_name)
            else item.get_filename_for_export(imgformat)) or f'{item_id}-original'
    if blob is not None:
        connection.execute('INSERT INTO sqlar (name, item_id, mode, sz, data) '
                           'VALUES (?, ?, ?, ?, ?)',
                           (name, item_id, 0o644, len(blob), blob))
        if getattr(item, 'TYPE', None) not in ('video', 'glb'):
            with suppress(Exception):
                from prism import thumbnail_cache
                thumbnail_cache.defer_preview(thumbnail_cache.digest(blob), blob)
        return item_id
    from prism.source_store import materialization_hold
    with materialization_hold(path):
        size = os.path.getsize(path)
        connection.execute('INSERT INTO sqlar (name, item_id, mode, sz, data) '
                           'VALUES (?, ?, ?, ?, zeroblob(?))',
                           (name, item_id, 0o644, size, size))
        rowid = connection.execute('SELECT rowid FROM sqlar WHERE item_id=?',
                                   (item_id,)).fetchone()[0]
        with connection.blobopen('sqlar', 'data', rowid, readonly=False) as output:
            with open(path, 'rb') as stream:
                shutil.copyfileobj(stream, output, length=1024*1024)
    return item_id


def write_partial_snapshot(snapshot, filename=None, *, cancel=None, commit_report=None):
    from prism.storage.managed import root_for, commit_partial
    if root_for(snapshot.source) is not None:
        if filename is not None and Path(filename).resolve()!=Path(snapshot.source).resolve():
            from prism.storage.managed_io import save_legacy_copy
            return save_legacy_copy(snapshot,filename,cancel=cancel,commit_report=commit_report)
        return commit_partial(snapshot, filename, cancel=cancel, commit_report=commit_report)
    return _write_partial_snapshot(snapshot, filename, cancel=cancel, commit_report=commit_report)


def _write_partial_snapshot(snapshot, filename=None, *, cancel=None, commit_report=None, _storage_hook=None):
    """Apply explicit changes atomically; save-as clones every unloaded table.

    A separate target is published only after a successful backup + transaction.
    Cancellation/failure leaves an existing target untouched. No source BLOB is
    decoded or re-encoded. The caller must handle acknowledgement on the GUI.

    ``commit_report`` is called with the moment the target really changed:
    ``committed_complete`` once the data is on disk, ``not_started`` when the
    write was discarded before the target changed. The writer knows this
    better than the caller: for a save-as the transaction only reaches the
    private copy, and the target changes when the copy is published.
    """
    source = Path(snapshot.source).resolve(strict=True)
    from .schema import APPLICATION_ID, USER_VERSION

    def supported_format(connection):
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        appid = connection.execute('PRAGMA application_id').fetchone()[0]
        from prism.storage.migration import METADATA_APPID
        allowed = (0, APPLICATION_ID, METADATA_APPID) if _storage_hook is not None else (0, APPLICATION_ID)
        if version < 1 or version > USER_VERSION or appid not in allowed:
            raise PartialSaveConflict('Unsupported project format; partial write refused')

    # Reject before a potentially huge Save As clone, then recheck inside
    # the write transaction to cover concurrent schema changes.
    with closing(sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=.25)) as guard:
        supported_format(guard)
    target = Path(filename).resolve() if filename is not None else source
    if target != source and target.exists() and source.samefile(target):
        raise ValueError('Save-as target aliases the source project')
    if _identity(source) != snapshot.source_identity:
        raise PartialSaveConflict('Source project has been replaced')

    def target_identity():
        if not target.exists():
            return None
        stat = target.stat()
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    target_before = target_identity() if target != source else None

    def check_cancel():
        if cancel is not None and cancel():
            raise PartialSaveCancelled('Partial save canceled before commit')

    def report(state, reason='', **extra):
        """Record the commit moment without ever breaking the save."""
        if commit_report is None:
            return
        try:
            commit_report(state, reason, **extra)
        except Exception:
            logger.exception('The commit report could not be recorded')

    temporary, monitor, source_version = None, None, None
    catalog_before = catalog_after = None
    published = False
    from prism.source_fingerprint import file_fingerprint
    try:
        check_cancel()
        if target != source:
            if any(Path(str(target)+suffix).exists() for suffix in ('-wal', '-journal')):
                raise PartialSaveConflict('Save-as target has an active journal')
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, name = tempfile.mkstemp(prefix=target.name+'.', suffix='.partial', dir=target.parent)
            os.close(descriptor)
            temporary = Path(name)
            monitor = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True, timeout=.25)
            source_version = monitor.execute('PRAGMA data_version').fetchone()[0]
            with closing(sqlite3.connect(temporary, timeout=.25)) as destination:
                monitor.backup(destination, pages=128,
                               progress=lambda _status, _remaining, _total: check_cancel())
            output = temporary
        else:
            output = source
        with closing(sqlite3.connect(output.as_uri()+'?mode=rw', uri=True, timeout=.25)) as connection:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('PRAGMA foreign_keys=ON')
            connection.execute('BEGIN IMMEDIATE')
            supported_format(connection)
            catalog_version = connection.execute('PRAGMA data_version').fetchone()[0]
            if target == source:
                catalog_before = file_fingerprint(source)
            try:
                if _identity(source) != snapshot.source_identity:
                    raise PartialSaveConflict('Source project has been replaced')
                if (_project_id(connection) != snapshot.project_id or
                        connection.execute('PRAGMA user_version').fetchone()[0] != snapshot.schema_version):
                    raise PartialSaveConflict('Project identity or schema has changed')
                # Validate the entire snapshot before changing its first row.
                changed_keys = {(change.table, change.key) for change in snapshot.changes}
                for table, keys in snapshot.read_sets:
                    if table not in ('workspace_nodes', 'material_records', 'model_records'):
                        raise ValueError('Unsupported domain membership dependency')
                    if _domain_keys(connection, table) != keys:
                        raise PartialSaveConflict(f'Save domain membership changed: {table}')
                for table, key, expected_row in snapshot.read_guards:
                    check_cancel()
                    if table not in TABLES:
                        raise ValueError('Unsupported save dependency')
                    if (table, key) in changed_keys:
                        continue  # The row's before image is checked below.
                    if _record(connection, table, key) != expected_row:
                        raise PartialSaveConflict(f'Save dependency changed: {table}/{key}')
                for change in snapshot.changes:
                    check_cancel()
                    if change.before is None:
                        if change.key is not None and _record(
                                connection, change.table, change.key) is not None:
                            raise PartialSaveConflict(f'Row already exists: {change.table}/{change.key}')
                        continue
                    if _record(connection, change.table, change.key) != change.before:
                        raise PartialSaveConflict(f'Record changed: {change.table}/{change.key}')
                for new in snapshot.new_items:
                    check_cancel()
                    if item_save_columns(new.item) != new.expected:
                        raise PartialSaveConflict('New asset changed while the project was saving; retry')
                inserted, now, catalog_ids = [], int(time.time()), set()
                from prism.library_observability import record_rows
                for table,domain in (('material_records','material'),('model_records','model')):
                    record_rows(domain,((change.key,change.after[0]) for change in snapshot.changes
                        if change.table==table and change.after is not None),
                        'patch_write_prepared',snapshot.project_id,scope='changed_rows_only; excludes deletions')
                for change in snapshot.changes:
                    check_cancel()
                    primary, columns, _json_columns = TABLES[change.table]
                    written_key = change.key
                    if change.before is None:
                        placeholders = ','.join('?' for _column in columns)
                        if change.key is None:
                            cursor = connection.execute(
                                f'INSERT INTO {change.table} ({",".join(columns)}) '
                                f'VALUES ({placeholders})', change.after)
                            inserted.append((change.tag, cursor.lastrowid))
                            written_key = cursor.lastrowid
                        else:
                            connection.execute(
                                f'INSERT INTO {change.table} ({primary},{",".join(columns)}) '
                                f'VALUES (?,{placeholders})', (change.key, *change.after))
                            inserted.append((change.tag, change.key))
                    elif change.after is None:
                        connection.execute(f'DELETE FROM {change.table} WHERE {primary}=?', (change.key,))
                    else:
                        connection.execute(f'UPDATE {change.table} SET '+
                                           ','.join(f'{column}=?' for column in columns)+
                                           f' WHERE {primary}=?', (*change.after, change.key))
                    if change.table == 'items':
                        catalog_ids.add(written_key)
                for new in snapshot.new_items:
                    check_cancel()
                    written_key = _write_new_item(connection, new)
                    inserted.append((new.tag, written_key))
                    catalog_ids.add(written_key)
                if snapshot.attachment_writes or snapshot.attachment_removals:
                    connection.execute(
                        'CREATE TABLE IF NOT EXISTS attachments ('
                        'id INTEGER PRIMARY KEY AUTOINCREMENT,'
                        ' board_id INTEGER NOT NULL DEFAULT 0,'
                        ' name TEXT NOT NULL, mime TEXT, blob BLOB,'
                        ' created_at INTEGER NOT NULL DEFAULT 0)')
                for attachment_id, name, mime, blob in snapshot.attachment_writes:
                    check_cancel()
                    if blob is None:
                        raise ValueError(f'Original attachment {attachment_id} is unreadable; save stopped')
                    connection.execute(
                        'INSERT OR REPLACE INTO attachments '
                        '(id, board_id, name, mime, blob, created_at) '
                        'VALUES (?, 0, ?, ?, ?, ?)',
                        (attachment_id, name, mime, blob, now))
                if snapshot.attachment_removals:
                    connection.executemany('DELETE FROM attachments WHERE id=?',
                                           [(key,) for key in snapshot.attachment_removals])
                for key in snapshot.required_attachments:
                    row = connection.execute(
                        'SELECT length(blob) FROM attachments WHERE id=?', (key,)).fetchone()
                    if (not row or not row[0]) and _storage_hook is not None:
                        row = connection.execute('SELECT size FROM object_refs WHERE owner=? AND entity=?',
                                                 ('attachments', str(key))).fetchone()
                    if not row or not row[0]:
                        raise PartialSaveConflict(f'Material attachment is missing or empty: {key}')
                check_cancel()
                if _storage_hook is not None:
                    _storage_hook(connection, inserted, catalog_ids)
                    check_cancel()
                connection.commit()
                if target == source:
                    # In place: the commit IS the disk fact.
                    published = True
                    report('committed_complete', 'The project file was committed')
                    # Pin a read snapshot while observing the committed file.
                    # An intervening external commit makes this receipt unsafe
                    # for incremental indexing, not unsafe for the saved bytes.
                    try:
                        connection.execute('BEGIN')
                        connection.execute('SELECT id FROM items LIMIT 1').fetchone()
                        if connection.execute('PRAGMA data_version').fetchone()[0] == catalog_version:
                            catalog_after = file_fingerprint(source)
                    except (sqlite3.Error, OSError):
                        catalog_after = None  # Optional indexing proof must not fail a committed save.
                    finally:
                        connection.rollback()
            except BaseException:
                connection.rollback()
                raise
        if temporary is not None:
            check_cancel()
            with temporary.open('r+b') as handle:
                os.fsync(handle.fileno())
            # Flush can take time on a large project. Recheck at publication,
            # not only before flushing the completed private database.
            check_cancel()
            if _identity(source) != snapshot.source_identity:
                raise PartialSaveConflict('Source project has been replaced')
            if monitor.execute('PRAGMA data_version').fetchone()[0] != source_version:
                raise PartialSaveConflict('Source changed while save-as was being prepared')
            if target_identity() != target_before or any(
                    Path(str(target)+suffix).exists() for suffix in ('-wal', '-journal')):
                raise PartialSaveConflict('Save-as target changed while preparing')
            os.replace(temporary, target)
            temporary = None
            # The target changed only now. Until this moment the commit had
            # merely reached the private copy.
            published = True
            report('committed_complete', 'The target was published')
        if catalog_after is not None:
            try:
                if file_fingerprint(source) != catalog_after:
                    catalog_after = None  # Closing/checkpointing or another writer changed it.
            except Exception:
                # The save is already committed. An indexing proof that cannot
                # be taken must not turn a committed save into a failure.
                logger.exception('The catalog fingerprint could not be re-checked')
                catalog_after = None
        return PartialSaveReceipt(str(target), snapshot.revision,
                                  len(snapshot.changes) + len(snapshot.new_items),
                                  tuple(inserted), catalog_before, catalog_after,
                                  tuple(sorted(catalog_ids)))
    except BaseException as error:
        if not published:
            # The target was never changed: the private copy was discarded.
            # "Nothing was written" is the disk fact.  Whether it was a
            # cancel is a different fact, and the save adapter needs both:
            # a write that died on an error is a failure, not a cancel.
            if isinstance(error, PartialSaveCancelled):
                report('not_started', 'The write was discarded before the target changed',
                       cancelled=True)
            else:
                report('not_started', 'The write was discarded before the target changed')
        raise
    finally:
        if monitor is not None:
            monitor.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
