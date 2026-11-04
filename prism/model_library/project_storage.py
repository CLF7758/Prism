"""Project-bound storage for the model library.

This module owns exactly three things:

* the identity of the project an operation belongs to
  (:class:`ModelProjectContext`),
* the on-disk shape of the model domain (one ``model_records`` table inside
  the project's **own** SQLite metadata database),
* the byte channel that carries the original model files
  (:class:`ModelResourceStore`).

It is deliberately Qt-free and F3D-free.  The UI and the native preview host
never share a connection, a widget or a file handle with this module, and no
database connection is shared across threads: every operation opens its own
short-lived connection.

Existing types this module binds to (no new identity vocabulary is invented):

``prism.storage.legacy_backend.LegacyBackend``
    supplies :class:`prism.storage.contracts.ProjectIdentity` with the three
    identities the storage contract keeps apart -- ``project_id`` (business
    identity), ``storage_instance_id`` (which physical copy this is) and
    ``open_epoch`` (which generation of the open this is).
``prism.storage.managed``
    supplies ``root_for`` / ``database_path`` / ``open_original`` and the
    ``DosObjectRepository`` for directory projects, so a directory project and
    a single-file project use the same code path.
``prism.fileio.partial_save``
    supplies the ``prism_metadata.project_id`` reader.

The original bytes of a model live in the project's existing ``attachments``
table -- the channel the material library already uses.  That table is lazy
(metadata resident, blobs on demand), it is externalised into the
content-addressed object store for directory projects, and it is copied by
*Save As* without any new plumbing.  The model domain therefore owns no
second byte store.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

from .contracts import (
    ModelAppearance,
    ModelFileRef,
    ModelRecord,
    ModelStatistics,
)

#: Chunk size for every streamed copy.  1 MiB keeps the resident set small
#: without turning the copy into a syscall per kilobyte.
CHUNK_SIZE = 1024 * 1024

#: The table that carries the model domain inside the project database.
MODEL_TABLE = 'model_records'

#: The table that carries the original bytes (shared with the note images).
ATTACHMENT_TABLE = 'attachments'

#: ``object_refs.owner`` used when the project is a directory project.
MANAGED_OWNER = 'attachments'

#: MIME type stored for an original model file.
MODEL_MIME = 'application/octet-stream'

#: Payload fields that belong to the model contract.  Everything else that a
#: future version writes is preserved verbatim and reported as unknown.
CONTRACT_FIELDS = frozenset({
    'model_id', 'project_id', 'name', 'main_file_id', 'format',
    'source_revision', 'record_revision', 'folder_id', 'tags', 'notes',
    'files', 'appearance', 'statistics', 'unknown_fields',
})

#: Payload fields this implementation owns although the public contract has no
#: slot for them.  They are never reported as "unknown" and never dropped.
OWNED_FIELDS = frozenset({
    'deleted_at', 'sort_order', 'created_at', 'updated_at', 'main_file_name',
    'package_root', 'id', 'thumbnail', 'total_size',
})

#: The table that carries the model domain.  It has exactly two columns: the
#: stable id and the whole record.  Everything a query filters or sorts on is an
#: *expression index* over the payload rather than a second copy of the value,
#: so the payload stays the single source of truth and a project can still be
#: converted to a directory project (the converter refuses generated columns).
MODEL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS model_records (
    id TEXT PRIMARY KEY,
    payload JSON NOT NULL
)
"""

#: The payload fields a model-library query filters or sorts on, and the
#: expression behind each index.
MODEL_INDEX_DDL = (
    # Cover search/count and the common name order without reparsing every
    # payload or building a temporary sort for each page. Original bytes stay
    # entirely outside this index; notes and tags keep substring semantics.
    'CREATE INDEX IF NOT EXISTS model_records_browse '
    "ON model_records (COALESCE(json_extract(payload, '$.deleted_at'), 0), "
    "COALESCE(json_extract(payload, '$.name'), '') COLLATE NOCASE, "
    "COALESCE(json_extract(payload, '$.format'), ''), "
    "COALESCE(json_extract(payload, '$.notes'), ''), "
    "COALESCE(json_extract(payload, '$.tags'), '[]'))",
    'CREATE INDEX IF NOT EXISTS model_records_name '
    "ON model_records (COALESCE(json_extract(payload, '$.name'), ''))",
    'CREATE INDEX IF NOT EXISTS model_records_format '
    "ON model_records (COALESCE(json_extract(payload, '$.format'), ''))",
    'CREATE INDEX IF NOT EXISTS model_records_folder '
    "ON model_records (COALESCE(json_extract(payload, '$.folder_id'), ''))",
    'CREATE INDEX IF NOT EXISTS model_records_deleted '
    "ON model_records (COALESCE(json_extract(payload, '$.deleted_at'), 0))",
    'CREATE INDEX IF NOT EXISTS model_records_sort '
    "ON model_records (COALESCE(json_extract(payload, '$.sort_order'), 0), "
    "COALESCE(json_extract(payload, '$.name'), ''))",
    'CREATE INDEX IF NOT EXISTS model_records_revision '
    "ON model_records (COALESCE(json_extract(payload, '$.record_revision'), 0))",
)

#: The expressions the repository uses in its own SQL, so a query hits the
#: indexes above instead of a second spelling of the same field.
EXPR_NAME = "COALESCE(json_extract(payload, '$.name'), '')"
EXPR_FORMAT = "COALESCE(json_extract(payload, '$.format'), '')"
EXPR_FOLDER = "COALESCE(json_extract(payload, '$.folder_id'), '')"
EXPR_DELETED = "COALESCE(json_extract(payload, '$.deleted_at'), 0)"
EXPR_SORT_ORDER = "COALESCE(json_extract(payload, '$.sort_order'), 0)"
EXPR_CREATED = "COALESCE(json_extract(payload, '$.created_at'), '')"
EXPR_UPDATED = "COALESCE(json_extract(payload, '$.updated_at'), '')"
EXPR_TOTAL_SIZE = "COALESCE(json_extract(payload, '$.total_size'), 0)"


class ModelStorageError(RuntimeError):
    """A storage operation failed for a reason the caller must see."""


class ModelRevisionConflict(ModelStorageError):
    """The stored record moved on; the caller's revision is stale."""

    def __init__(self, model_id, expected, actual):
        super().__init__(
            f'Model {model_id} changed underneath this edit '
            f'(expected revision {expected}, stored revision {actual})')
        self.model_id = model_id
        self.expected_revision = expected
        self.actual_revision = actual


class ModelResourceMissing(ModelStorageError):
    """The original bytes of a file are not in this project."""


class ModelOperationCancelled(ModelStorageError):
    """Cancellation before the durable metadata commit."""


# ---------------------------------------------------------------------------
# project identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelProjectContext:
    """Everything an operation needs to know about *which* project it is in.

    This is not a global file name.  It carries the three identities the
    existing storage contract keeps apart, the resolved metadata database, the
    backend kind and the revision of the source at the time it was opened.
    """

    project_path: str
    metadata_path: str
    project_id: str
    storage_instance_id: str
    open_epoch: int
    session_token: str
    backend: str
    source_revision: str
    managed_root: Optional[str] = None

    @property
    def is_managed(self) -> bool:
        """True when the project is a directory project with an object store."""
        return self.managed_root is not None

    @property
    def project_session(self) -> str:
        """The session token the ports use to close a project's jobs."""
        return self.session_token


def open_model_context(project_path, *, open_epoch: int = 0,
                       project_id: Optional[str] = None,
                       writable: bool = True) -> ModelProjectContext:
    """Bind a model-library context to a real, open project.

    ``project_path`` is the path the application has open: a ``.prism`` file or
    a ``.prism.data`` directory (or its launcher).  The identity comes from
    the existing storage layer, never from a name the caller made up.
    """
    from prism.storage.managed import database_path, root_for

    path = str(Path(project_path).resolve())
    folder = root_for(path)
    metadata_path = database_path(path)
    if folder is not None:
        from prism.storage.migration import read_manifest
        manifest = read_manifest(folder)
        instance = str(manifest.get('storage_instance_id') or folder)
        revision = _managed_revision(folder)
        backend = 'managed_object_store'
        managed_root = str(folder)
    else:
        from prism.storage.legacy_backend import LegacyBackend
        backend_handle = LegacyBackend(path, writable=writable)
        identity = backend_handle.identity()
        instance = identity.storage_instance_id
        revision = backend_handle.source_revision()
        backend = 'legacy_sqlite'
        managed_root = None
    resolved_project_id = project_id or _read_project_id(metadata_path)
    return ModelProjectContext(
        project_path=path,
        metadata_path=metadata_path,
        project_id=str(resolved_project_id),
        storage_instance_id=instance,
        open_epoch=int(open_epoch),
        session_token=uuid.uuid4().hex,
        backend=backend,
        source_revision=revision,
        managed_root=managed_root,
    )


def _read_project_id(metadata_path: str) -> str:
    from prism.fileio.partial_save import _project_id
    with closing(sqlite3.connect(Path(metadata_path).resolve().as_uri() + '?mode=ro',
                                 uri=True)) as connection:
        value = _project_id(connection)
    return str(value) if value else ''


def _managed_revision(folder: str) -> str:
    from prism.storage.managed import revision as managed_revision
    value = managed_revision(folder)
    return ':'.join(str(part) for part in value) if value else ''


def require_same_project(context: ModelProjectContext, other: ModelProjectContext) -> None:
    """Refuse to mix two opens of the same project."""
    if context.project_id != other.project_id:
        raise ModelStorageError('Model data belongs to another project')
    if context.storage_instance_id != other.storage_instance_id:
        raise ModelStorageError('Model data belongs to another storage instance')


# ---------------------------------------------------------------------------
# payload codec
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelFileDetail:
    """One original file of a model, with everything the manifest knows.

    The public contract's :class:`~prism.model_library.contracts.ModelFileRef`
    carries only the six fields every consumer needs.  The import manifest
    needs more (where the bytes came from, whether they are missing, which
    project attachment holds them), so the extra facts live here and in the
    stored payload instead of being smuggled into a string.
    """

    file_id: str
    relative_path: str
    role: str
    size_bytes: int
    sha256: str
    source_ref: str
    origin: str = ''
    missing: bool = False
    attachment_id: Optional[int] = None
    original_reference: str = ''
    absolute_reference: bool = False

    def to_contract(self) -> ModelFileRef:
        return ModelFileRef(file_id=self.file_id, relative_path=self.relative_path,
                             role=self.role, size_bytes=self.size_bytes,
                             sha256=self.sha256, source_ref=self.source_ref,
                             missing=self.missing, original_reference=self.original_reference,
                             attachment_id=self.attachment_id, origin=self.origin,
                             absolute_reference=self.absolute_reference)

    def to_payload(self) -> dict:
        return {
            'file_id': self.file_id,
            'relative_path': self.relative_path,
            'role': self.role,
            'size_bytes': self.size_bytes,
            'sha256': self.sha256,
            'source_ref': self.source_ref,
            'origin': self.origin,
            'missing': self.missing,
            'attachment_id': self.attachment_id,
            'original_reference': self.original_reference,
            'absolute_reference': self.absolute_reference,
        }

    @classmethod
    def from_payload(cls, payload: dict) -> 'ModelFileDetail':
        return cls(
            file_id=str(payload.get('file_id', '')),
            relative_path=str(payload.get('relative_path', '')),
            role=str(payload.get('role', 'unknown')),
            size_bytes=int(payload.get('size_bytes', 0) or 0),
            sha256=str(payload.get('sha256', '')),
            source_ref=str(payload.get('source_ref', '')),
            origin=str(payload.get('origin', '')),
            missing=bool(payload.get('missing', False)),
            attachment_id=payload.get('attachment_id'),
            original_reference=str(payload.get('original_reference', '')),
            absolute_reference=bool(payload.get('absolute_reference', False)),
        )


def decode_appearance(payload) -> ModelAppearance:
    """Build a **detached** appearance; never hand out a live mapping."""
    data = payload if isinstance(payload, dict) else {}
    bindings = data.get('bindings') or {}
    properties = data.get('properties') or {}
    if not isinstance(bindings, dict) or not isinstance(properties, dict):
        raise ValueError('Model appearance bindings and properties must be objects')
    return ModelAppearance(
        revision=int(data.get('revision', 0) or 0),
        bindings=json.loads(json.dumps(bindings, ensure_ascii=False)),
        properties=json.loads(json.dumps(properties, ensure_ascii=False)),
    )


def decode_statistics(payload) -> ModelStatistics:
    data = payload if isinstance(payload, dict) else {}
    def _optional(name):
        value = data.get(name)
        return None if value is None else int(value)
    return ModelStatistics(
        vertices=_optional('vertices'),
        triangles=_optional('triangles'),
        polygons=_optional('polygons'),
        instances=_optional('instances'),
        basis=str(data.get('basis', 'unknown')),
        backend_version=str(data.get('backend_version', '')),
    )


def encode_record(record: ModelRecord) -> dict:
    """Serialize a contract record into the stored payload."""
    payload = {
        'model_id': record.model_id,
        'project_id': record.project_id,
        'name': record.name,
        'main_file_id': record.main_file_id,
        'format': record.format,
        'source_revision': int(record.source_revision),
        'record_revision': int(record.record_revision),
        'folder_id': record.folder_id,
        'tags': list(record.tags),
        'notes': record.notes,
        'files': [dict(item) if not isinstance(item, dict) else dict(item)
                  for item in _file_payloads(record.files)],
        'appearance': {
            'revision': int(record.appearance.revision),
            'bindings': json.loads(json.dumps(dict(record.appearance.bindings),
                                              ensure_ascii=False)),
            'properties': json.loads(json.dumps(dict(record.appearance.properties),
                                                ensure_ascii=False)),
        },
        'statistics': {
            'vertices': record.statistics.vertices,
            'triangles': record.statistics.triangles,
            'polygons': record.statistics.polygons,
            'instances': record.statistics.instances,
            'basis': record.statistics.basis,
            'backend_version': record.statistics.backend_version,
        },
        'unknown_fields': json.loads(json.dumps(dict(record.unknown_fields),
                                                ensure_ascii=False)),
    }
    for key, value in payload['unknown_fields'].items():
        if key not in CONTRACT_FIELDS and key not in OWNED_FIELDS:
            payload.setdefault(key, value)
    return payload


def _file_payloads(files) -> list:
    result = []
    for item in files or ():
        if isinstance(item, dict):
            result.append(dict(item))
        else:
            result.append({
                'file_id': item.file_id,
                'relative_path': item.relative_path,
                'role': item.role,
                'size_bytes': item.size_bytes,
                'sha256': item.sha256,
                'source_ref': item.source_ref,
                'missing': item.missing,
                'original_reference': item.original_reference,
                'attachment_id': item.attachment_id,
                'origin': item.origin,
                'absolute_reference': item.absolute_reference,
            })
    return result


def decode_record(payload: dict) -> ModelRecord:
    """Build a contract record from a stored payload.

    Fields this version does not know are kept in ``unknown_fields`` and are
    written back untouched, so a project written by a newer Prism keeps its
    data when an older one saves it.
    """
    files = tuple(ModelFileDetail.from_payload(item).to_contract()
                  for item in payload.get('files', []) if isinstance(item, dict))
    known = CONTRACT_FIELDS | OWNED_FIELDS
    unknown = dict(payload.get('unknown_fields') or {})
    unknown.update({key: value for key, value in payload.items() if key not in known})
    return ModelRecord(
        model_id=str(payload.get('model_id', '')),
        project_id=str(payload.get('project_id', '')),
        name=str(payload.get('name', '')),
        main_file_id=str(payload.get('main_file_id', '')),
        format=str(payload.get('format', '')),
        source_revision=int(payload.get('source_revision', 0) or 0),
        record_revision=int(payload.get('record_revision', 0) or 0),
        folder_id=payload.get('folder_id'),
        tags=tuple(str(tag) for tag in payload.get('tags', []) or ()),
        notes=str(payload.get('notes', '') or ''),
        files=files,
        appearance=decode_appearance(payload.get('appearance')),
        statistics=decode_statistics(payload.get('statistics')),
        unknown_fields=json.loads(json.dumps(unknown, ensure_ascii=False)),
    )


def file_details(payload: dict) -> tuple:
    """The full per-file details of a stored payload."""
    return tuple(ModelFileDetail.from_payload(item)
                 for item in payload.get('files', []) if isinstance(item, dict))


def merge_unknown_fields(payload: dict, unknown: dict) -> dict:
    """Put unknown fields back into a payload without losing owned ones."""
    result = dict(payload)
    for key, value in (unknown or {}).items():
        result.setdefault(key, value)
    return result


def validate_model_records(records, project_id: str) -> None:
    """Reject model records that could not be written safely.

    The rules mirror the material library: a record belongs to one project,
    has one stable id, and its file manifest points at real project
    attachments.
    """
    if not isinstance(records, list):
        raise ValueError('模型库记录必须是列表')
    seen = set()
    for record in records:
        if not isinstance(record, dict):
            raise ValueError('模型库记录必须是对象')
        model_id = record.get('id') or record.get('model_id')
        if not isinstance(model_id, str) or not model_id:
            raise ValueError('模型记录缺少稳定 id')
        if model_id in seen:
            raise ValueError('模型记录 id 重复：' + model_id)
        seen.add(model_id)
        owner = record.get('project_id')
        if owner and project_id and str(owner) != str(project_id):
            raise ValueError('模型记录属于另一个工程：' + model_id)
        files = record.get('files', [])
        if not isinstance(files, list):
            raise ValueError('模型记录的文件清单必须是列表')
        for item in files:
            if not isinstance(item, dict):
                raise ValueError('模型记录的文件清单项必须是对象')
            relative = item.get('relative_path', '')
            if relative and ('..' in Path(relative).parts
                             or relative.startswith('/')
                             or (len(relative) > 1 and relative[1] == ':')):
                raise ValueError('模型记录的文件路径越界：' + str(relative))
            if item.get('missing'):
                continue
            if item.get('attachment_id') is None and item.get('sha256'):
                raise ValueError('模型记录缺少受管理原件：' + str(relative))
        tags = record.get('tags', [])
        if not isinstance(tags, (list, tuple)):
            raise ValueError('模型记录的标签必须是列表')
        for tag in tags:
            if not isinstance(tag, str):
                raise ValueError('模型记录的标签必须是文本')
        name = record.get('name')
        if name is not None and not isinstance(name, str):
            raise ValueError('模型记录的名称必须是文本')
    json.dumps(records, ensure_ascii=False, allow_nan=False)


def register_model_attachments(scene, payload: dict) -> tuple:
    """Teach the project's attachment index about the originals of a model.

    The model originals live in the project's ``attachments`` table.  The
    index of that table decides which rows a save keeps, so a freshly
    imported model must be registered before the next save -- otherwise the
    note's own cleanup would delete the originals.

    This must be called on the thread that owns the scene (the GUI thread).
    It only touches a dict under the store's own lock.
    """
    ids = []
    attachments = getattr(scene, 'attachments', None)
    if attachments is None:
        return ()
    meta = getattr(attachments, '_meta', None)
    if meta is None:
        return ()
    lock = getattr(attachments, '_lock', None)
    entries = list(payload.get('files', []) or ())
    entries.extend(dict(attachment_id=key, relative_path='material-resource', missing=False)
                   for key in _material_attachment_ids(payload))
    for binding in (payload.get('appearance', {}).get('bindings', {}) or {}).values():
        if isinstance(binding, dict) and binding.get('attachmentId') is not None:
            entries.append(dict(attachment_id=binding['attachmentId'],
                                relative_path=binding.get('name', ''), missing=False))
    for item in entries:
        if not isinstance(item, dict) or item.get('missing'):
            continue
        attachment_id = item.get('attachment_id')
        if attachment_id is None:
            continue
        attachment_id = int(attachment_id)
        name = str(item.get('relative_path', '') or '').rsplit('/', 1)[-1]
        if lock is not None:
            with lock:
                meta[attachment_id] = (name, MODEL_MIME)
        else:
            meta[attachment_id] = (name, MODEL_MIME)
        ids.append(attachment_id)
    return tuple(ids)


def model_attachment_ids(payload: dict) -> tuple:
    """The project attachments one model payload owns."""
    ids = list(_material_attachment_ids(payload))
    for item in payload.get('files', []) or ():
        if not isinstance(item, dict) or item.get('missing'):
            continue
        attachment_id = item.get('attachment_id')
        if attachment_id is not None:
            ids.append(int(attachment_id))
    for binding in (payload.get('appearance', {}).get('bindings', {}) or {}).values():
        if isinstance(binding, dict) and binding.get('attachmentId') is not None:
            ids.append(int(binding['attachmentId']))
    return tuple(set(ids))


def _material_attachment_ids(payload):
    """Pin committed material documents/resources, including retained history.

    Only typed resource records are traversed; arbitrary metadata keys are not
    interpreted as ownership. Resource GC is separate from save preservation.
    """
    block = payload.get('multimaterial') or {}
    if not isinstance(block, dict):
        return ()
    result = set()
    for entry in (block.get('resources') or {}).values():
        if isinstance(entry, dict) and entry.get('attachment_id') is not None:
            result.add(int(entry['attachment_id']))
    document = block.get('catalog_ref') or {}
    if isinstance(document, dict) and document.get('attachment_id') is not None:
        result.add(int(document['attachment_id']))
    return tuple(sorted(result))


# ---------------------------------------------------------------------------
# the byte channel
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredResource:
    """A file that now lives inside the project."""

    attachment_id: int
    name: str
    size_bytes: int
    sha256: str
    source_ref: str


class ModelResourceStore:
    """Read and write the original bytes of model files.

    Two backends, one behaviour:

    * a single-file ``.prism`` project keeps the bytes in the ``attachments``
      table and streams them through SQLite's incremental blob interface;
    * a directory project keeps them in the content-addressed object store
      and streams them through ``prism.storage.managed.open_original``.

    Neither path needs the whole file in memory.
    """

    def __init__(self, context: ModelProjectContext, *, begin_catalog=None, finish_catalog=None):
        self.context = context
        self._begin_catalog = begin_catalog
        self._finish_catalog = finish_catalog

    # -- writing ---------------------------------------------------------

    def put_batch(self, files, *, commit=None, cancel=None) -> list:
        """Copy several files into the project in **one** transaction.

        ``files`` is a sequence of ``(path, name)`` pairs.  ``commit`` is an
        optional callable that receives the open connection **and** the
        resources written so far; it is called inside the same transaction, so
        a record and its originals are either both there or neither is.
        Nothing is left half-imported, and the caller never has to read a
        value that only exists after the transaction.
        """
        prepared = []
        for item in files:
            path, name = item if isinstance(item, (tuple, list)) else (item, None)
            source = Path(path)
            display = name or source.name
            stat = source.stat()
            prepared.append((source, display, int(stat.st_size), int(stat.st_mtime_ns)))
        if self.context.is_managed:
            return self._put_managed_batch(prepared, commit, cancel)
        return self._put_inline_batch(prepared, commit, cancel)

    def put_file(self, path, *, name: Optional[str] = None) -> StoredResource:
        """Copy one file into the project and verify what was written."""
        return self.put_batch([(path, name)])[0]

    def _chunks(self, path: Path, digest, cancel=None) -> Iterator[bytes]:
        with path.open('rb') as handle:
            while True:
                if cancel is not None and cancel():
                    raise ModelOperationCancelled('Model source copy cancelled')
                chunk = handle.read(CHUNK_SIZE)
                if not chunk:
                    return
                digest.update(chunk)
                yield chunk

    def _put_inline_batch(self, prepared, commit, cancel=None) -> list:
        results = []
        connection = sqlite3.connect(self.context.metadata_path, timeout=30)
        try:
            connection.isolation_level = None
            connection.execute('BEGIN IMMEDIATE')
            proof = self._begin_catalog(connection) if self._begin_catalog else None
            for source, display, size, _mtime in prepared:
                digest = hashlib.sha256()
                cursor = connection.execute(
                    f'INSERT INTO {ATTACHMENT_TABLE} (board_id, name, mime, blob, created_at) '
                    'VALUES (0, ?, ?, zeroblob(?), ?)',
                    (display, MODEL_MIME, size, int(time.time())))
                attachment_id = int(cursor.lastrowid)
                blob = connection.blobopen(ATTACHMENT_TABLE, 'blob', attachment_id)
                written = 0
                try:
                    for chunk in self._chunks(source, digest, cancel):
                        blob.write(chunk)
                        written += len(chunk)
                finally:
                    blob.close()
                if written != size:
                    raise ModelStorageError(
                        f'Original changed while it was copied: {source}')
                results.append(StoredResource(
                    attachment_id=attachment_id, name=display, size_bytes=size,
                    sha256=digest.hexdigest(),
                    source_ref=f'attachment:{attachment_id}'))
            if cancel is not None and cancel():
                raise ModelOperationCancelled('Model import cancelled before commit')
            if commit is not None:
                commit(connection, results)
            connection.execute('COMMIT')
            if self._finish_catalog:self._finish_catalog(connection, proof)
        except BaseException:
            try:
                connection.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        finally:
            connection.close()
        return results

    def _put_managed_batch(self, prepared, commit, cancel=None) -> list:
        from prism.storage.dos_repository import DosObjectRepository
        from prism.storage.leases import exclusive_destination, project_lease

        folder = Path(self.context.managed_root)
        repository = DosObjectRepository(folder / 'objects')
        try:
            with exclusive_destination(folder / 'metadata.sqlite'), project_lease(folder):
                # Object bytes are durable before their references commit.
                staged = []
                for source, display, size, _mtime in prepared:
                    digest = hashlib.sha256()
                    stat = repository.put_stream(self._chunks(source, digest, cancel), length=size)
                    verified = repository.verify(stat.hashkey)
                    if not verified.ok or verified.length != size:
                        raise ModelStorageError('The stored original did not verify')
                    staged.append((source, display, size, stat.hashkey))
                results = []
                connection = sqlite3.connect(self.context.metadata_path, timeout=30)
                try:
                    connection.isolation_level = None
                    connection.execute('BEGIN IMMEDIATE')
                    proof = self._begin_catalog(connection) if self._begin_catalog else None
                    for source, display, size, hashkey in staged:
                        cursor = connection.execute(
                            f'INSERT INTO {ATTACHMENT_TABLE} (board_id, name, mime, blob, created_at) '
                            'VALUES (0, ?, ?, NULL, ?)',
                            (display, MODEL_MIME, int(time.time())))
                        attachment_id = int(cursor.lastrowid)
                        connection.execute(
                            'INSERT OR REPLACE INTO object_refs VALUES(?,?,?,?)',
                            (MANAGED_OWNER, str(attachment_id), hashkey, size))
                        results.append(StoredResource(
                            attachment_id=attachment_id, name=display, size_bytes=size,
                            sha256=hashkey,
                            source_ref=f'attachment:{attachment_id}'))
                    if commit is not None:
                        commit(connection, results)
                    connection.execute('COMMIT')
                    if self._finish_catalog:self._finish_catalog(connection, proof)
                except BaseException:
                    try:
                        connection.execute('ROLLBACK')
                    except sqlite3.Error:
                        pass
                    raise
                finally:
                    connection.close()
        finally:
            repository.close()
        return results

    # -- reading ---------------------------------------------------------

    @contextmanager
    def open(self, attachment_id: int) -> Iterator['ResourceReader']:
        """Stream one stored original; the hash is checked while it streams."""
        if self.context.is_managed:
            reader = self._open_managed(attachment_id)
        else:
            reader = self._open_inline(attachment_id)
        try:
            yield reader
        finally:
            reader.close()

    def read_all(self, attachment_id: int) -> bytes:
        with self.open(attachment_id) as reader:
            return reader.read()

    def _open_inline(self, attachment_id: int) -> 'ResourceReader':
        connection = sqlite3.connect(
            Path(self.context.metadata_path).resolve().as_uri() + '?mode=ro', uri=True)
        try:
            row = connection.execute(
                f'SELECT rowid, length(blob) FROM {ATTACHMENT_TABLE} WHERE id=?',
                (attachment_id,)).fetchone()
            if row is None:
                raise ModelResourceMissing(
                    f'Original file {attachment_id} is not in this project')
            blob = connection.blobopen(ATTACHMENT_TABLE, 'blob', row[0], readonly=True)
        except BaseException:
            connection.close()
            raise
        return ResourceReader(_BlobSource(blob, connection),
                              expected_size=row[1])

    def _open_managed(self, attachment_id: int) -> 'ResourceReader':
        from prism.storage.managed import open_original
        manager = open_original(self.context.project_path, MANAGED_OWNER,
                                str(attachment_id))
        stream = manager.__enter__()
        size = self._managed_size(attachment_id)
        return ResourceReader(_ManagedSource(stream, manager), expected_size=size)

    def _managed_size(self, attachment_id: int) -> Optional[int]:
        with closing(sqlite3.connect(
                Path(self.context.metadata_path).resolve().as_uri() + '?mode=ro',
                uri=True)) as connection:
            row = connection.execute(
                'SELECT size FROM object_refs WHERE owner=? AND entity=?',
                (MANAGED_OWNER, str(attachment_id))).fetchone()
        return int(row[0]) if row and row[0] is not None else None


class ResourceReader:
    """A verified, chunked reader over one stored original."""

    def __init__(self, source, *, expected_size: Optional[int] = None):
        self._source = source
        self._expected_size = expected_size
        self._digest = hashlib.sha256()
        self._count = 0

    def read(self, size: int = -1) -> bytes:
        data = self._source.read(size)
        self._digest.update(data)
        self._count += len(data)
        return data

    def chunks(self, size: int = CHUNK_SIZE) -> Iterator[bytes]:
        while True:
            chunk = self.read(size)
            if not chunk:
                return
            yield chunk

    def verify(self) -> str:
        """Read the rest of the file and return the hash of what was read."""
        for _chunk in self.chunks():
            pass
        if (self._expected_size is not None and self._count != self._expected_size):
            raise ModelStorageError(
                f'Stored original has {self._count} bytes, expected '
                f'{self._expected_size}')
        return self._digest.hexdigest()

    @property
    def bytes_read(self) -> int:
        return self._count

    def close(self) -> None:
        self._source.close()


class _BlobSource:
    """Adapts a SQLite incremental blob to a plain readable stream."""

    def __init__(self, blob, connection):
        self._blob = blob
        self._connection = connection

    def read(self, size: int = -1) -> bytes:
        return self._blob.read(size)

    def close(self) -> None:
        try:
            self._blob.close()
        finally:
            self._connection.close()


class _ManagedSource:
    """Adapts the managed object stream to a plain readable stream."""

    def __init__(self, stream, manager):
        self._stream = stream
        self._manager = manager

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def close(self) -> None:
        try:
            self._manager.__exit__(None, None, None)
        except Exception:  # pragma: no cover - the manager already closed
            pass


# ---------------------------------------------------------------------------
# database helpers
# ---------------------------------------------------------------------------


def connect(metadata_path: str, *, readonly: bool = False,
            timeout: float = 30.0) -> sqlite3.Connection:
    """Open a short-lived connection; no connection is shared across threads."""
    path = Path(metadata_path).resolve()
    if readonly:
        connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                     timeout=timeout)
    else:
        connection = sqlite3.connect(path, timeout=timeout)
    connection.isolation_level = None
    return connection


def ensure_model_tables(connection: sqlite3.Connection) -> None:
    """Create the model domain table and its expression indexes if missing.

    An experimental build stored generated columns beside the payload.  The
    storage converter refuses hidden columns, and those columns were only ever
    a projection of the payload, so such a table is rebuilt from its own rows:
    no stored value changes and the project stays convertible.
    """
    if table_exists(connection, MODEL_TABLE):
        columns = list(connection.execute(f'PRAGMA table_xinfo({MODEL_TABLE})'))
        if any(len(row) > 6 and row[6] for row in columns):
            _rebuild_without_hidden_columns(connection)
    else:
        connection.execute(MODEL_TABLE_DDL)
    for statement in MODEL_INDEX_DDL:
        connection.execute(statement)


def _rebuild_without_hidden_columns(connection: sqlite3.Connection) -> None:
    """Replace a table that carries generated columns with the plain shape."""
    temporary = f'{MODEL_TABLE}_rebuild'
    connection.execute(f'DROP TABLE IF EXISTS {temporary}')
    connection.execute(
        f'CREATE TABLE {temporary} (id TEXT PRIMARY KEY, payload JSON NOT NULL)')
    connection.execute(
        f'INSERT INTO {temporary} (id, payload) SELECT id, payload FROM {MODEL_TABLE}')
    connection.execute(f'DROP TABLE {MODEL_TABLE}')
    connection.execute(f'ALTER TABLE {temporary} RENAME TO {MODEL_TABLE}')


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def sha256_of(path: Path) -> tuple:
    """The content hash and the size of a file, read in chunks."""
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        while True:
            chunk = handle.read(CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# job events (private, immutable result DTOs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelJobPhaseEvent:
    """A job moved to a new phase.  ``kind`` is the discriminator."""

    kind: str = 'phase'
    job_id: str = ''
    job_kind: str = ''
    state: str = ''
    completed: int = 0
    total: int = 0
    message: str = ''


@dataclass(frozen=True)
class ModelImportResult:
    """One model was imported and committed to the project."""

    model_id: str
    name: str
    format: str
    payload: dict
    files: tuple = ()
    missing: tuple = ()
    warnings: tuple = ()
    attachment_ids: tuple = ()
    package_root: str = ''


@dataclass(frozen=True)
class ModelImportCompletedEvent:
    kind: str = 'import_completed'
    job_id: str = ''
    result: Optional[ModelImportResult] = None


@dataclass(frozen=True)
class ModelImportFailure:
    source: str
    error_code: str
    message: str


@dataclass(frozen=True)
class ModelImportFailedEvent:
    kind: str = 'import_failed'
    job_id: str = ''
    failure: Optional[ModelImportFailure] = None


@dataclass(frozen=True)
class ModelExportItemResult:
    """The honest outcome of one model inside an export job."""

    model_id: str
    name: str
    state: str            # completed | failed | cancelled | missing
    directory: Optional[str] = None
    files: int = 0
    bytes_written: int = 0
    error: Optional[str] = None
    missing: tuple = ()


@dataclass(frozen=True)
class ModelExportResult:
    job_id: str
    destination: str
    state: str            # completed | failed | cancelled
    items: tuple = ()


@dataclass(frozen=True)
class ModelExportCompletedEvent:
    kind: str = 'export_completed'
    job_id: str = ''
    result: Optional[ModelExportResult] = None


@dataclass(frozen=True)
class ModelExportFailedEvent:
    kind: str = 'export_failed'
    job_id: str = ''
    error_code: str = ''
    message: str = ''


@dataclass(frozen=True)
class ModelJobHandle:
    """The identity of an accepted job.  It is NOT a completion promise."""

    job_id: str
    kind: str
    accepted_paths: tuple = ()
    model_ids: tuple = ()
    destination: Optional[str] = None
    metadata: dict = field(default_factory=dict)
