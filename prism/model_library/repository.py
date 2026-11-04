"""The project-bound model repository.

This is the only component that reads or writes model rows.  It binds to a
real project through :class:`~prism.model_library.project_storage.ModelProjectContext`
and never to a bare file name, so a record can never be written into the wrong
project.

Guarantees this implementation makes:

* a stable model id, independent of the file name and of the path the model
  was imported from;
* metadata, the file manifest, the appearance configuration, the statistics
  and the cover-cache identity are stored **separately** from the original
  bytes, so editing a note never rewrites a large file;
* ``query`` returns one page of summaries and reads no blob, no original
  image and starts no F3D process;
* ``get`` returns the complete record;
* ``save`` is optimistic: the caller states the revision it believes is
  stored, and a mismatch is an explicit failure, never a silent overwrite;
* unknown fields of a payload written by a newer version are preserved.
"""
from __future__ import annotations

import json
from collections import deque
import base64
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Optional

from .contracts import ModelCardSummary, ModelPage, ModelQuery, ModelRecord
from .events import record_model_event
from .project_storage import (
    CONTRACT_FIELDS,
    EXPR_CREATED,
    EXPR_DELETED,
    EXPR_FOLDER,
    EXPR_FORMAT,
    EXPR_NAME,
    EXPR_SORT_ORDER,
    EXPR_TOTAL_SIZE,
    EXPR_UPDATED,
    MODEL_TABLE,
    ModelProjectContext,
    ModelRevisionConflict,
    ModelResourceStore,
    OWNED_FIELDS,
    decode_record,
    encode_record,
    ensure_model_tables,
    file_details,
    merge_unknown_fields,
    table_exists,
)

#: Sort keys a query may ask for.  Anything else is refused rather than
#: silently turned into "name".  The expressions are the ones the model table
#: is indexed on.
SORT_KEYS = {
    'name': f'{EXPR_NAME} COLLATE NOCASE ASC',
    '-name': f'{EXPR_NAME} COLLATE NOCASE DESC',
    'format': f'{EXPR_FORMAT} ASC, {EXPR_NAME} COLLATE NOCASE ASC',
    '-format': f'{EXPR_FORMAT} DESC, {EXPR_NAME} COLLATE NOCASE ASC',
    'created_at': f'{EXPR_CREATED} ASC, {EXPR_NAME} COLLATE NOCASE ASC',
    '-created_at': f'{EXPR_CREATED} DESC, {EXPR_NAME} COLLATE NOCASE ASC',
    'updated_at': f'{EXPR_UPDATED} ASC, {EXPR_NAME} COLLATE NOCASE ASC',
    '-updated_at': f'{EXPR_UPDATED} DESC, {EXPR_NAME} COLLATE NOCASE ASC',
    'sort_order': f'{EXPR_SORT_ORDER} ASC, {EXPR_NAME} COLLATE NOCASE ASC',
    '-sort_order': f'{EXPR_SORT_ORDER} DESC, {EXPR_NAME} COLLATE NOCASE ASC',
    'size': f'{EXPR_TOTAL_SIZE} DESC, {EXPR_NAME} COLLATE NOCASE ASC',
}


class ModelRepositoryAdapter:
    """Read and write the model rows of one open project."""

    def __init__(self, project_context: ModelProjectContext, *, cover_cache_root=None,
                 material_cache_root=None):
        self.context = project_context
        self.resources = ModelResourceStore(project_context,
            begin_catalog=self._begin_catalog_receipt, finish_catalog=self._finish_catalog_receipt)
        self._catalog_receipts = deque(maxlen=64)
        self.cover_cache_root = Path(cover_cache_root) if cover_cache_root else None
        #: Where unpacked material originals live.  They are addressed by
        #: their content digest, so this cache belongs to no single
        #: session: a session directory is thrown away every time the model
        #: library is opened.
        self.material_cache_root = Path(material_cache_root) if material_cache_root else None

    # -- helpers ---------------------------------------------------------

    def _begin_catalog_receipt(self, connection):
        """Prove this write leaves the image directory's source rows alone."""
        from prism.source_fingerprint import file_fingerprint
        source = Path(self.context.metadata_path).resolve()
        tracking = [file_fingerprint(source), connection.execute('PRAGMA data_version').fetchone()[0], True]
        def authorize(action, table, column, database, trigger):
            if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE):
                if table in ('items', 'sqlar', 'sqlite_master'):
                    tracking[2] = False
            return sqlite3.SQLITE_OK
        connection.set_authorizer(authorize)
        return source, tracking

    def _finish_catalog_receipt(self, connection, proof):
        from prism.source_fingerprint import file_fingerprint
        from prism.fileio.partial_save import PartialSaveReceipt
        try:
            source, tracking = proof
            after = file_fingerprint(source)
            connection.set_authorizer(None)
            # Never include another connection's changes after our COMMIT.
            # A broken chain simply rebuilds; indexing cannot fail a durable edit.
            if tracking[2] and connection.execute('PRAGMA data_version').fetchone()[0] == tracking[1]:
                self._catalog_receipts.append(PartialSaveReceipt(str(source), uuid.uuid4().hex,
                    0, source_before=tracking[0], source_after=after))
        except Exception as error:
            record_model_event('catalog_receipt_skipped', reason=type(error).__name__)

    def take_catalog_receipts(self):
        receipts = []
        while True:
            try:
                receipts.append(self._catalog_receipts.popleft())
            except IndexError:
                return tuple(receipts)

    def _connect(self, *, readonly: bool = False) -> sqlite3.Connection:
        from .project_storage import connect
        return connect(self.context.metadata_path, readonly=readonly)

    def _guard_project(self, payload: dict) -> None:
        project_id = str(payload.get('project_id', '') or '')
        if project_id and project_id != self.context.project_id:
            raise ValueError('Model record belongs to another project')

    # -- reading ---------------------------------------------------------

    def query(self, query: ModelQuery) -> ModelPage:
        """One page of card summaries.  No blob, no original, no F3D."""
        sort = SORT_KEYS.get(query.sort or 'name')
        if sort is None:
            raise ValueError(f'Unsupported model sort key: {query.sort!r}')
        offset = max(0, int(query.offset))
        limit = max(1, min(int(query.limit or 1), 1024))
        text = (query.text or '').strip()
        clauses, parameters = [f'{EXPR_DELETED} = 0'], []
        if query.folder_id:
            clauses.append(f'{EXPR_FOLDER} = ?')
            parameters.append(query.folder_id)
        if text:
            clauses.append(f'({EXPR_NAME} COLLATE NOCASE LIKE ? ESCAPE \'\\\' '
                           f'OR {EXPR_FORMAT} LIKE ? ESCAPE \'\\\' '
                           "OR COALESCE(json_extract(payload,'$.notes'), '') "
                           'LIKE ? ESCAPE \'\\\' '
                           "OR COALESCE(json_extract(payload,'$.tags'), '[]') "
                           'LIKE ? ESCAPE \'\\\')')
            pattern = '%' + _escape_like(text) + '%'
            parameters.extend((pattern, pattern, pattern, pattern))
        where = ' AND '.join(clauses)
        connection = self._connect(readonly=True)
        try:
            if not table_exists(connection, MODEL_TABLE):
                return ModelPage(items=(), total=0, offset=offset)
            indexed = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index' AND name='model_records_browse'"
            ).fetchone()
            source = MODEL_TABLE
            if indexed and query.sort in ('name', '-name', '') and not query.folder_id:
                source += ' INDEXED BY model_records_browse'
            total = connection.execute(
                f'SELECT COUNT(*) FROM {source} WHERE {where}',
                tuple(parameters)).fetchone()[0]
            if query.focus_id:
                position = connection.execute(
                    f'SELECT position FROM (SELECT id, ROW_NUMBER() OVER '
                    f'(ORDER BY {sort}, id) - 1 AS position FROM {source} WHERE {where}) '
                    'WHERE id=?', tuple(parameters) + (query.focus_id,)).fetchone()
                if position is not None:
                    offset = int(position[0]) // limit * limit
            rows = connection.execute(
                f'SELECT payload FROM {source} WHERE {where} '
                f'ORDER BY {sort}, id LIMIT ? OFFSET ?',
                tuple(parameters) + (limit, offset)).fetchall()
        finally:
            connection.close()
        items = tuple(self._summary(json.loads(payload)) for (payload,) in rows)
        return ModelPage(items=items, total=int(total), offset=offset)

    def _summary(self, payload: dict) -> ModelCardSummary:
        from .project_storage import decode_statistics
        files = payload.get('files', []) or []
        size = sum(int(item.get('size_bytes', 0) or 0) for item in files
                   if isinstance(item, dict))
        thumbnail = payload.get('thumbnail') or {}
        if not isinstance(thumbnail, dict):
            thumbnail = {}
        return ModelCardSummary(
            model_id=str(payload.get('model_id', '')),
            name=str(payload.get('name', '')),
            format=str(payload.get('format', '')),
            tags=tuple(str(tag) for tag in payload.get('tags', []) or ()),
            size_bytes=size,
            statistics=decode_statistics(payload.get('statistics')),
            thumbnail_data_url=self._cover_url(thumbnail),
            thumbnail_state=str(thumbnail.get('state', 'missing')),
            thumbnail_renderer=str(thumbnail.get('renderer', '')),
        )

    def get(self, model_id: str) -> Optional[ModelRecord]:
        """The complete record of one model, or ``None``."""
        payload = self.get_payload(model_id)
        if payload is None:
            return None
        return decode_record(payload)

    def get_payload(self, model_id: str) -> Optional[dict]:
        """The stored payload of one model, including the file manifest."""
        connection = self._connect(readonly=True)
        try:
            if not table_exists(connection, MODEL_TABLE):
                return None
            row = connection.execute(
                f'SELECT payload FROM {MODEL_TABLE} WHERE id=?', (model_id,)).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        return json.loads(row[0])

    def list_payloads(self) -> tuple:
        """Every stored payload of this project (metadata only)."""
        connection = self._connect(readonly=True)
        try:
            if not table_exists(connection, MODEL_TABLE):
                return ()
            rows = connection.execute(
                f'SELECT payload FROM {MODEL_TABLE} '
                f'ORDER BY {EXPR_NAME} COLLATE NOCASE').fetchall()
        finally:
            connection.close()
        return tuple(json.loads(payload) for (payload,) in rows)

    def file_details(self, model_id: str) -> tuple:
        """The full per-file manifest of one model."""
        payload = self.get_payload(model_id)
        return () if payload is None else file_details(payload)

    def thumbnail_data_url(self, model_id: str) -> Optional[str]:
        """The cached cover of one model, read on demand only."""
        payload = self.get_payload(model_id)
        if payload is None:
            return None
        thumbnail = payload.get('thumbnail')
        if not isinstance(thumbnail, dict):
            return None
        return self._cover_url(thumbnail)

    def _cover_url(self, thumbnail):
        return cached_model_cover(thumbnail, self.cover_cache_root)

    # -- writing ---------------------------------------------------------

    def save(self, record: ModelRecord, *, expected_revision: int) -> None:
        """Write one record, refusing to overwrite a later edit.

        ``expected_revision`` is the revision the caller believes is stored.
        A different stored revision means somebody else edited the model in
        the meantime; the write is refused instead of winning by force.
        """
        payload = encode_record(record)
        self._guard_project(payload)
        self._write_payload(payload, expected_revision=int(expected_revision),
                            must_exist=True)

    def insert(self, payload: dict) -> dict:
        """Write a brand new record.  It must not exist yet."""
        payload = dict(payload)
        self._guard_project(payload)
        if not payload.get('project_id'):
            payload['project_id'] = self.context.project_id
        payload['record_revision'] = int(payload.get('record_revision', 0) or 0) + 1
        self._write_payload(payload, expected_revision=0, must_exist=False)
        return payload

    def insert_in(self, connection: sqlite3.Connection, payload: dict) -> dict:
        """Insert a record inside a transaction the caller owns.

        The importer uses this so that a record and the originals it points at
        are committed together: either the model exists with its bytes, or it
        does not exist at all.
        """
        payload = dict(payload)
        self._guard_project(payload)
        if not payload.get('project_id'):
            payload['project_id'] = self.context.project_id
        model_id = str(payload.get('model_id', ''))
        if not model_id:
            raise ValueError('A model record needs an id')
        ensure_model_tables(connection)
        if connection.execute(f'SELECT 1 FROM {MODEL_TABLE} WHERE id=?',
                              (model_id,)).fetchone() is not None:
            raise ValueError(f'Model {model_id} already exists in this project')
        payload['id'] = model_id
        payload['record_revision'] = int(payload.get('record_revision', 0) or 0) + 1
        connection.execute(
            f'INSERT INTO {MODEL_TABLE} (id, payload) VALUES (?, ?)',
            (model_id, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
        return payload

    def update(self, payload: dict, *, expected_revision: int) -> dict:
        """Replace the metadata of an existing record."""
        payload = dict(payload)
        self._guard_project(payload)
        current = self.get_payload(str(payload.get('model_id', '')))
        stored_revision = int((current or {}).get('record_revision', 0) or 0)
        if stored_revision != int(expected_revision):
            record_model_event('model_tx.conflict', model_id=str(payload.get('model_id', '')),
                               expected_revision=int(expected_revision),
                               stored_revision=stored_revision,
                               reason='revision_changed', path='update',
                               state='conflict')
            raise ModelRevisionConflict(str(payload.get('model_id', '')),
                                        int(expected_revision), stored_revision)
        payload['record_revision'] = stored_revision + 1
        self._write_payload(payload, expected_revision=stored_revision, must_exist=True)
        return payload

    def patch_batch(self, changes):
        """Validate every revision before atomically changing metadata only."""
        connection = self._connect()
        result = []
        try:
            connection.execute('BEGIN IMMEDIATE')
            proof = self._begin_catalog_receipt(connection)
            for model_id, expected, patch in changes:
                if set(patch) - {'folder_id', 'name', 'deleted_at'}:
                    raise ValueError('Unsupported metadata patch')
                row = connection.execute('SELECT payload FROM model_records WHERE id=?',
                                         (model_id,)).fetchone()
                if row is None:
                    raise KeyError(model_id)
                before = json.loads(row[0])
                self._guard_project(before)
                actual = int(before.get('record_revision', 0))
                if actual != expected:
                    record_model_event('model_tx.conflict', model_id=model_id,
                                       expected_revision=expected,
                                       stored_revision=actual,
                                       reason='revision_changed', path='patch_batch',
                                       state='conflict')
                    raise ModelRevisionConflict(model_id, expected, actual)
                after = dict(before)
                after.update(patch, record_revision=actual + 1, updated_at=_now())
                connection.execute('UPDATE model_records SET payload=? WHERE id=?',
                    (json.dumps(after, ensure_ascii=False, allow_nan=False), model_id))
                result.append((before, after))
            connection.commit()
            self._finish_catalog_receipt(connection, proof)
            return result
        except BaseException as error:
            connection.rollback()
            record_model_event('model_tx.rollback', path='patch_batch',
                               model_count=len(changes),
                               error_code=type(error).__name__, state='rolled_back')
            raise
        finally:
            connection.close()

    def delete(self, model_id: str, *, expected_revision: int) -> None:
        """Remove one record for good.  The original bytes are freed separately."""
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            ensure_model_tables(connection)
            proof = self._begin_catalog_receipt(connection)
            row = connection.execute(
                f'SELECT payload FROM {MODEL_TABLE} WHERE id=?', (model_id,)).fetchone()
            if row is None:
                raise KeyError(model_id)
            stored = int(json.loads(row[0]).get('record_revision', 0) or 0)
            if stored != int(expected_revision):
                record_model_event('model_tx.conflict', model_id=model_id,
                                   expected_revision=int(expected_revision),
                                   stored_revision=stored,
                                   reason='revision_changed', path='delete',
                                   state='conflict')
                raise ModelRevisionConflict(model_id, int(expected_revision), stored)
            connection.execute(f'DELETE FROM {MODEL_TABLE} WHERE id=?', (model_id,))
            connection.execute('COMMIT')
            self._finish_catalog_receipt(connection, proof)
            record_model_event('model_tx.committed', path='delete',
                               model_id=model_id, state='committed')
        except BaseException:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def retire(self, model_id: str, *, expected_revision: int) -> dict:
        """Move one model out of the library without touching its originals.

        A retired model keeps its record, its files and its revision: it just
        stops appearing in a query.  Restoring it is therefore a metadata
        operation, not a re-import.
        """
        return self._set_deleted_at(model_id, _now(), expected_revision)

    def restore(self, model_id: str, *, expected_revision: int) -> dict:
        """Bring a retired model back into the library."""
        return self._set_deleted_at(model_id, None, expected_revision)

    def _set_deleted_at(self, model_id: str, stamp, expected_revision: int) -> dict:
        payload = self.get_payload(model_id)
        if payload is None:
            raise KeyError(model_id)
        stored_revision = int(payload.get('record_revision', 0) or 0)
        if stored_revision != int(expected_revision):
            raise ModelRevisionConflict(model_id, int(expected_revision),
                                        stored_revision)
        payload['deleted_at'] = stamp
        payload['updated_at'] = stamp or _now()
        self._write_payload(payload, expected_revision=stored_revision,
                            must_exist=True)
        return payload

    def retired(self) -> tuple:
        """Every retired payload of this project, newest first."""
        connection = self._connect(readonly=True)
        try:
            if not table_exists(connection, MODEL_TABLE):
                return ()
            rows = connection.execute(
                f'SELECT payload FROM {MODEL_TABLE} WHERE {EXPR_DELETED} != 0 '
                f'ORDER BY {EXPR_UPDATED} DESC').fetchall()
        finally:
            connection.close()
        return tuple(json.loads(payload) for (payload,) in rows)

    def _write_payload(self, payload: dict, *, expected_revision: int,
                       must_exist: bool) -> None:
        model_id = str(payload.get('model_id', ''))
        if not model_id:
            raise ValueError('A model record needs an id')
        connection = self._connect()
        # A write that fails here leaves no durable row; saying "transaction
        # started" before the first statement is what makes a half write
        # distinguishable from "nothing was attempted".
        record_model_event('model_tx.begin', model_id=model_id,
                           expected_revision=expected_revision,
                           must_exist=bool(must_exist), state='begun')
        try:
            connection.execute('BEGIN IMMEDIATE')
            ensure_model_tables(connection)
            row = connection.execute(
                f'SELECT payload FROM {MODEL_TABLE} WHERE id=?', (model_id,)).fetchone()
            proof = self._begin_catalog_receipt(connection)
            if row is None:
                if must_exist:
                    record_model_event('model_tx.conflict', model_id=model_id,
                                       expected_revision=expected_revision,
                                       stored_revision=0, reason='row_missing',
                                       state='conflict')
                    raise ModelRevisionConflict(model_id, expected_revision, 0)
                stored_revision = 0
            else:
                stored = json.loads(row[0])
                stored_revision = int(stored.get('record_revision', 0) or 0)
                if stored_revision != expected_revision:
                    record_model_event('model_tx.conflict', model_id=model_id,
                                       expected_revision=expected_revision,
                                       stored_revision=stored_revision,
                                       reason='revision_changed', state='conflict')
                    raise ModelRevisionConflict(model_id, expected_revision,
                                                stored_revision)
                # Unknown fields written by another version stay untouched.
                unknown = {key: value for key, value in stored.items()
                           if key not in CONTRACT_FIELDS and key not in OWNED_FIELDS}
                payload = merge_unknown_fields(payload, unknown)
                # Contract edits don't own cache bookkeeping or future
                # per-file manifest fields. Retain both on roundtrip.
                for key in OWNED_FIELDS:
                    if key in stored:
                        payload.setdefault(key, stored[key])
                old_files = {entry.get('file_id'): entry for entry in stored.get('files', [])}
                for entry in payload.get('files', []):
                    for key, value in old_files.get(entry.get('file_id'), {}).items():
                        entry.setdefault(key, value)
            payload['record_revision'] = stored_revision + 1
            connection.execute(
                f'INSERT INTO {MODEL_TABLE} (id, payload) VALUES (?, ?) '
                'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload '
                f'WHERE {MODEL_TABLE}.payload IS NOT excluded.payload',
                (model_id, json.dumps(payload, ensure_ascii=False, allow_nan=False)))
            connection.execute('COMMIT')
            self._finish_catalog_receipt(connection, proof)
        except BaseException:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()

    def update_cache(self, model_id, *, expected_revision, thumbnail, statistics=None):
        """Derived data must not invalidate an in-progress metadata edit."""
        connection = self._connect()
        try:
            connection.execute('BEGIN IMMEDIATE')
            proof = self._begin_catalog_receipt(connection)
            row = connection.execute('SELECT payload FROM model_records WHERE id=?', (model_id,)).fetchone()
            if row is None:
                raise KeyError(model_id)
            payload = json.loads(row[0])
            actual = int(payload.get('record_revision', 0))
            if actual != expected_revision:
                record_model_event('model_tx.conflict', model_id=model_id,
                                   expected_revision=expected_revision,
                                   stored_revision=actual,
                                   reason='revision_changed', path='update_cache',
                                   state='conflict')
                raise ModelRevisionConflict(model_id, expected_revision, actual)
            payload['thumbnail'] = thumbnail
            if statistics is not None:
                payload['statistics'] = statistics
            connection.execute('UPDATE model_records SET payload=? WHERE id=?',
                (json.dumps(payload, ensure_ascii=False, allow_nan=False), model_id))
            connection.commit()
            self._finish_catalog_receipt(connection, proof)
            return payload
        finally:
            connection.close()

    # -- cover cache -----------------------------------------------------

    def set_thumbnail(self, model_id: str, data_url: Optional[str], *,
                      expected_revision: int) -> dict:
        """Store the cached cover of one model.

        The cover is a *derived* resource: it never replaces the originals
        and it is never read while a query runs.
        """
        payload = self.get_payload(model_id)
        if payload is None:
            raise KeyError(model_id)
        stored_revision = int(payload.get('record_revision', 0) or 0)
        if stored_revision != int(expected_revision):
            raise ModelRevisionConflict(model_id, int(expected_revision),
                                        stored_revision)
        thumbnail = dict(payload.get('thumbnail') or {})
        if data_url:
            thumbnail['data_url'] = data_url
            thumbnail['state'] = 'ready'
        else:
            thumbnail.pop('data_url', None)
            thumbnail['state'] = 'missing'
        payload['thumbnail'] = thumbnail
        self._write_payload(payload, expected_revision=stored_revision, must_exist=True)
        return payload

    # -- statistics ------------------------------------------------------

    def count(self) -> int:
        connection = self._connect(readonly=True)
        try:
            if not table_exists(connection, MODEL_TABLE):
                return 0
            return int(connection.execute(
                f'SELECT COUNT(*) FROM {MODEL_TABLE} '
                f'WHERE {EXPR_DELETED} = 0').fetchone()[0])
        finally:
            connection.close()


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def _escape_like(value: str) -> str:
    return (value.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_'))


def cached_model_cover(thumbnail, cover_cache_root):
    legacy = thumbnail.get('data_url')
    if legacy:
        return legacy
    key = str(thumbnail.get('cache_key') or '')
    if cover_cache_root is None or len(key) != 64 or any(c not in '0123456789abcdef' for c in key):
        return None
    try:
        path = cover_cache_root / (key + '.png')
        if path.stat().st_size > 4 * 1024 * 1024:
            return None
        return 'data:image/png;base64,' + base64.b64encode(path.read_bytes()).decode('ascii')
    except OSError:
        return None


