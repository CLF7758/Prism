"""LegacyBackend — the current ``.prism`` format behind the new seam.

D03's first requirement is deliberately conservative: *the new routing calls
the existing SQLite backend and nothing else changes*.  So this backend is a
thin wrapper:

* it **reuses** AI-02's read-only accessors (``read_page_body`` /
  ``read_attachment``) instead of writing a second SQL reader;
* it **reuses** the existing schema constants (``APPLICATION_ID``,
  ``USER_VERSION``) for the capability gate;
* it never creates a database from a path that merely *looks* like a project.

The capability gate is the interesting part.  A project whose
``user_version`` is **higher** than the one this build understands is not
readable-and-writable: we may read what we can, but writing would silently
downgrade data we do not understand.  Such a project is refused for writing,
and the source bytes are not touched.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Optional, Sequence

from .contracts import (
    ProjectIdentity,
    SaveReceipt,
    SourceKind,
    SourceRef,
    AckState, CommitState, DurableState, Terminal,
    StorageError,
    StorageErrorKind,
)
from .project_backend import (
    BackendKind,
    BackendProbe,
    Capability,
    ProjectSnapshot,
    unknown_probe,
)
from .source_ref import (
    resolve_legacy_attachment,
    resolve_legacy_page,
)

#: The sqlite header magic.  A project file is recognised by its *content*,
#: never by its extension.
SQLITE_MAGIC = b"SQLite format 3\x00"


class LegacyBackend:
    """Reads and writes the current ``.prism`` format."""

    kind = BackendKind.LEGACY_SQLITE

    def __init__(self, path: str, *, identity: Optional[ProjectIdentity] = None,
                 writable: bool = False):
        self.path = str(path)
        self._writable = bool(writable)
        self._identity = identity
        self._closed = False
        self._open_epoch = uuid.uuid4().hex

    def open(self, path, *, writable=False, identity=None):
        """Bind a new handle to the path selected by the router."""
        return type(self)(path, writable=writable, identity=identity)

    # ---------------------------------------------------------------- probe
    def probe(self, path: str) -> BackendProbe:
        """Recognise a project by content, and decide what may be done to it."""
        from prism.fileio.schema import APPLICATION_ID, USER_VERSION

        if not os.path.isfile(path):
            return unknown_probe(path, "path does not exist")
        try:
            with open(path, "rb") as fh:
                head = fh.read(len(SQLITE_MAGIC))
        except OSError as error:
            return unknown_probe(path, f"path is not readable: {error}")
        if head != SQLITE_MAGIC:
            return unknown_probe(path, "file does not carry the sqlite header")
        try:
            app_id, user_version = self._read_header(path)
        except (sqlite3.Error, OSError) as error:
            return unknown_probe(path, f"sqlite header unreadable: {error}")

        capabilities = [Capability.READ, Capability.PAGE_BODY,
                         Capability.ATTACHMENT, Capability.QUERY,
                         Capability.COMPATIBLE_EXPORT]
        writable = True
        supported = True
        reason = None
        if user_version > USER_VERSION:
            # A newer format: readable, but we must not write, because writing
            # with an older schema would drop what we do not understand.
            writable = False
            supported = False
            reason = (f"project user_version {user_version} is newer than the "
                      f"supported {USER_VERSION}; refusing to write")
        elif app_id == 0 and 0 < user_version <= USER_VERSION:
            with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)) as conn:
                tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'items', 'sqlar'} <= tables:
                writable = supported = False
                reason = 'unidentified SQLite file is not a legacy Prism project'
        elif app_id != APPLICATION_ID:
            writable = False
            supported = False
            reason = (f"project application_id {app_id} is not "
                      f"{APPLICATION_ID}; refusing to touch it")
        if writable:
            capabilities.append(Capability.WRITE)
            capabilities.append(Capability.INCREMENTAL_COMMIT)
        return BackendProbe(path=path, kind=self.kind, readable=True,
                            writable=writable, application_id=app_id,
                            user_version=user_version, supported=supported,
                            unsupported_reason=reason, capabilities=tuple(capabilities))

    @staticmethod
    def _read_header(path: str):
        # PRAGMAs observe committed WAL state; the main-file header alone can
        # still carry an older version while a newer schema lives in WAL.
        with closing(sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro',
                                     uri=True)) as connection:
            connection.execute('BEGIN')
            return (connection.execute('PRAGMA application_id').fetchone()[0],
                    connection.execute('PRAGMA user_version').fetchone()[0])

    # ------------------------------------------------------------- snapshot
    def snapshot(self) -> ProjectSnapshot:
        """A read-only view of the project.

        Only page and attachment identifiers are read here.  Bodies are
        **not** materialised: a body that was never opened stays unloaded.
        """
        if self._closed:
            raise StorageError(StorageErrorKind.READ_ONLY,
                               "backend is closed", source_id=self.path)
        before_revision = self.source_revision()
        pages: tuple = ()
        attachments: tuple = ()
        entity_count = 0
        try:
            with closing(sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro',
                                         uri=True)) as conn:
                conn.execute('BEGIN')
                names = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if 'items' not in names:
                    raise sqlite3.DatabaseError('project has no items table')
                if 'workspace_pages' in names:
                    pages = tuple(r[0] for r in conn.execute(
                        "SELECT id FROM workspace_pages ORDER BY id"))
                if 'attachments' in names:
                    attachments = tuple(r[0] for r in conn.execute(
                        "SELECT id FROM attachments ORDER BY id"))
                entity_count = int(conn.execute(
                    "SELECT COUNT(*) FROM items").fetchone()[0])
        except sqlite3.Error as error:
            raise StorageError(StorageErrorKind.CORRUPT,
                               f"project {self.path!r} could not be read",
                               detail=error, source_id=self.path) from error
        if self.source_revision() != before_revision:
            raise StorageError(StorageErrorKind.SOURCE_BUSY, 'Source changed during header snapshot')
        return ProjectSnapshot(
            identity=self.identity(),
            source_revision=before_revision,
            page_ids=pages, attachment_ids=attachments,
            entity_count=entity_count, read_only=not self._writable,
            notes={"backend": self.kind.value, 'revision_kind': 'legacy-file-wal-header-token',
                   'full_content_digest': False})

    def identity(self) -> ProjectIdentity:
        if self._identity is not None:
            return self._identity
        with closing(sqlite3.connect(Path(self.path).resolve().as_uri()+'?mode=ro', uri=True)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            row = (conn.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()
                   if 'prism_metadata' in tables else None)
        stat = Path(self.path).stat()
        instance = f'legacy:{stat.st_dev}:{stat.st_ino}'
        # Very old files have no persistent project ID. The fallback is
        # explicitly local to this storage instance, never a content hash.
        self._identity = ProjectIdentity(project_id=str(row[0]) if row else instance,
                                         storage_instance_id=instance,
                                         open_epoch=self._open_epoch)
        return self._identity

    def source_revision(self) -> str:
        """A bounded legacy file/WAL validation token, not a content digest.

        Full original-byte hashing belongs to import/export verification,
        never to opening a 55GB project or refreshing its header snapshot.
        Legacy files lack a reliable persisted logical revision, so retain
        conservative file/WAL invalidation until their writer supplies one.
        """
        from prism.source_fingerprint import file_fingerprint
        state = file_fingerprint(self.path)
        with open(self.path, "rb") as fh:
            header = fh.read(100)
        payload = json.dumps(state, sort_keys=True).encode() + header
        return hashlib.sha256(payload).hexdigest()

    # ----------------------------------------------------------------- read
    def read_source(self, ref: SourceRef) -> bytes:
        if self._closed:
            raise StorageError(StorageErrorKind.READ_ONLY,
                               "backend is closed", source_id=self.path)
        if ref.kind is SourceKind.LEGACY_ARCHIVE:
            if ref.media_role == "attachment":
                resolved = resolve_legacy_attachment(self.path, ref.source_id)
            elif ref.media_role in ('image', 'video', 'material', 'model', 'archive-entry'):
                from prism.source_store import read_archive
                data = read_archive((self.path, int(ref.source_id)))
                if data is None:
                    raise StorageError(StorageErrorKind.MISSING, 'archive entry has no stored bytes',
                                       source_id=ref.source_id)
                return self._validate_source(ref, bytes(data))
            elif ref.media_role not in (None, 'body'):
                raise StorageError(StorageErrorKind.UNKNOWN, 'explicit media role is required',
                                   source_id=ref.source_id)
            else:
                resolved = resolve_legacy_page(self.path, ref.source_id)
            if resolved.error is not None:
                raise resolved.error
            assert resolved.data is not None
            return self._validate_source(ref, resolved.data)
        raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION,
                           f"legacy backend cannot serve {ref.kind.value}",
                           source_id=ref.source_id)

    @staticmethod
    def _validate_source(ref, data):
        if ref.byte_length is not None and len(data) != ref.byte_length:
            raise StorageError(StorageErrorKind.CORRUPT, 'source length changed', source_id=ref.source_id)
        if ref.content_revision is not None and hashlib.sha256(data).hexdigest() != ref.content_revision:
            raise StorageError(StorageErrorKind.CORRUPT, 'source content revision changed', source_id=ref.source_id)
        return data

    def read_raw_page_data(self, page_id):
        from prism.fileio.sql import read_page_body
        body = read_page_body(self.path, page_id)
        if body.error is not None:
            raise StorageError(StorageErrorKind.CORRUPT, detail=body.error)
        if not body.found:
            raise StorageError(StorageErrorKind.MISSING, source_id=str(page_id))
        return body.raw_data

    def query(self, **criteria) -> Sequence[str]:
        """Candidate ids.  The legacy backend answers from the items table."""
        if self._closed:
            raise StorageError(StorageErrorKind.READ_ONLY,
                               "backend is closed", source_id=self.path)
        unsupported = set(criteria) - {'type', 'limit', 'after_id'}
        if unsupported:
            raise ValueError(f'Unsupported legacy query criteria: {sorted(unsupported)}')
        limit = criteria.get("limit")
        sql = "SELECT id FROM items"
        params: list = []
        conditions = []
        if criteria.get("type"):
            conditions.append('type=?')
            params.append(criteria["type"])
        if criteria.get('after_id') is not None:
            conditions.append('id>?')
            params.append(criteria['after_id'])
        if conditions:
            sql += ' WHERE ' + ' AND '.join(conditions)
        sql += " ORDER BY id"
        if limit is not None:
            if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                raise ValueError('limit must be a nonnegative integer')
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            with closing(sqlite3.connect(Path(self.path).resolve().as_uri() + '?mode=ro',
                                         uri=True)) as conn:
                return tuple(r[0] for r in conn.execute(sql, params))
        except sqlite3.Error as error:
            raise StorageError(StorageErrorKind.CORRUPT,
                               "query failed", detail=error,
                               source_id=self.path) from error

    # --------------------------------------------------------------- commit
    def commit(self, *, snapshot, operation_id, target=None, cancel=None) -> SaveReceipt:
        """Use the existing guarded row transaction; never acknowledge live UI.

        The returned receipt says committed/unconfirmed. The GUI must still
        acknowledge the captured snapshot, retaining any edits made meanwhile.
        """
        from prism.fileio.partial_save import (PartialSaveSnapshot, write_partial_snapshot,
                                               PartialSaveCancelled, PartialSaveConflict)
        if self._closed or not self._writable:
            raise StorageError(StorageErrorKind.READ_ONLY, source_id=self.path)
        if not operation_id or not isinstance(snapshot, PartialSaveSnapshot):
            raise ValueError('An operation ID and captured partial snapshot are required')
        probe = self.probe(self.path)
        if not probe.supported or not probe.writable:
            raise StorageError(StorageErrorKind.UNSUPPORTED_VERSION, probe.unsupported_reason)
        if Path(snapshot.source).resolve() != Path(self.path).resolve():
            raise ValueError('Captured snapshot belongs to another source')
        identity = self.identity()
        if snapshot.project_id is not None and str(snapshot.project_id) != identity.project_id:
            raise ValueError('Captured snapshot belongs to another project')
        try:
            written = write_partial_snapshot(snapshot, target, cancel=cancel)
        except PartialSaveCancelled as error:
            raise StorageError(StorageErrorKind.CANCELLED, str(error)) from error
        except PartialSaveConflict as error:
            raise StorageError(StorageErrorKind.SOURCE_BUSY, str(error)) from error
        destination = Path(written.filename).resolve()
        revision, error = None, None
        try:
            revision = LegacyBackend(str(destination)).source_revision()
        except (OSError, sqlite3.Error) as failure:
            # The write already committed. A failed optional revision read
            # must not turn it into a fictitious failed write.
            error = StorageError(StorageErrorKind.COMMITTED_NOT_CONFIRMED,
                                 'Saved, but the committed revision could not be observed',
                                 detail=failure)
        entities = tuple((change.table, change.key if change.key is not None
                          else dict(written.inserted).get(change.tag)) for change in snapshot.changes)
        return SaveReceipt(operation_id=str(operation_id), identity=identity,
            target=str(destination), captured_revision=snapshot.revision,
            committed_revision=revision,
            changed_entities=entities, changed_entities_complete=False,
            complete=True, durable_state=DurableState.COMMITTED_UNCONFIRMED,
            commit_state=CommitState.COMPLETE, ack_state=AckState.NOT_APPLICABLE,
            terminal=Terminal.COMMITTED_NOT_CONFIRMED,
            source_before=self.path, source_after=str(destination), error=error)

    # ---------------------------------------------------------------- close
    def close(self) -> None:
        self._closed = True
