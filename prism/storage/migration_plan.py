"""Preflight and space estimate for a legacy -> new format migration.

Pure Python: no Qt, no object-store backend, no database writes.  The plan
has to be computable *before* a backend is chosen, and the preflight has to
be able to BLOCK a conversion that would silently drop data.

Three questions are answered here:

  * can this source be converted at all?  (DDL, WAL, sqlite identity)
  * how much space does the target need?
  * what are the phases, and what does each phase promise?

The blocker list is deliberately conservative.  A trigger, a virtual table
or an active write-ahead log is refused rather than "converted best
effort": the ruling for D08 is that an unsupported extension must be
blocked in the preflight, not silently dropped.

Unknown tables and unknown columns are NOT blockers.  They are ordinary
data as far as this module can tell, and the prototype round trip kept
them; they are reported so the caller can decide, and the verifier
(AI-07) compares them independently.
"""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

#: Tables the current build knows about.  Anything else is carried across
#: as ordinary data and must survive the round trip.
KNOWN_TABLES = frozenset({
    'items', 'sqlar', 'board_notes', 'attachments', 'board_mindmap',
    'workspace_pages', 'material_records', 'prism_metadata', 'workspace_nodes',
})

#: Tables whose BLOB payload is an ORIGINAL byte stream.  These are the
#: columns that must never be reconstructed from a preview or a thumbnail.
ORIGINAL_BLOB_TABLES = {'attachments': ('blob',), 'sqlar': ('data',)}


class MigrationPhase(str, Enum):
    """The migration state machine, in order.  Each phase is recoverable."""

    DISCOVER = 'discover'
    PREFLIGHT = 'preflight'
    COPY_METADATA = 'copy_metadata'
    COPY_OBJECTS = 'copy_objects'
    VERIFY = 'verify'
    FINALIZE = 'finalize'
    PUBLISH = 'publish'


@dataclass(frozen=True)
class TablePlan:
    """One table and the work it needs."""

    name: str
    columns: tuple
    row_count: int
    original_blob_columns: tuple = ()
    known: bool = True

    @property
    def carries_original_bytes(self) -> bool:
        return bool(self.original_blob_columns)


@dataclass
class SpaceEstimate:
    """Space the target needs, split by what the space is for."""

    source_bytes: int = 0
    metadata_bytes: int = 0
    original_bytes: int = 0
    page_size: int = 0
    page_count: int = 0
    row_count: int = 0

    @property
    def total_bytes(self) -> int:
        """Upper bound: metadata plus one copy of every original byte."""
        return self.metadata_bytes + self.original_bytes

    def as_dict(self) -> dict:
        return {
            'source_bytes': self.source_bytes,
            'metadata_bytes': self.metadata_bytes,
            'original_bytes': self.original_bytes,
            'page_size': self.page_size,
            'page_count': self.page_count,
            'row_count': self.row_count,
            'total_bytes': self.total_bytes,
        }


@dataclass
class PreflightResult:
    """What the preflight decided, and why."""

    source: str
    supported: bool = False
    blockers: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    tables: list = field(default_factory=list)
    space: SpaceEstimate = field(default_factory=SpaceEstimate)
    user_version: int = 0
    application_id: int = 0
    journal_mode: str = ''

    def block(self, reason: str):
        self.blockers.append(reason)
        self.supported = False

    def warn(self, reason: str):
        self.warnings.append(reason)

    def as_dict(self) -> dict:
        return {
            'source': self.source,
            'supported': self.supported,
            'blockers': list(self.blockers),
            'warnings': list(self.warnings),
            'user_version': self.user_version,
            'application_id': self.application_id,
            'journal_mode': self.journal_mode,
            'tables': [
                {
                    'name': table.name,
                    'columns': list(table.columns),
                    'row_count': table.row_count,
                    'original_blob_columns': list(table.original_blob_columns),
                    'known': table.known,
                }
                for table in self.tables
            ],
            'space': self.space.as_dict(),
        }


def _open_readonly(path: Path) -> sqlite3.Connection:
    """Open an existing database without ever creating one."""
    uri = 'file:{}?mode=ro'.format(path.as_posix().replace('?', '%3f').replace('#', '%23'))
    return sqlite3.connect(uri, uri=True)


def preflight_source(source, *, project_id=None) -> PreflightResult:
    """Decide whether ``source`` can be converted, and how much room it needs.

    The source is opened read-only.  A path that is not a readable sqlite
    database is refused; this never creates a database and never writes.
    """
    path = Path(source)
    result = PreflightResult(source=str(path))

    if not path.exists():
        result.block('source path does not exist')
        return result
    if path.is_dir():
        result.block('source path is a directory')
        return result
    with path.open('rb') as stream:
        header = stream.read(16)
    if header[:15] != b'SQLite format 3':
        result.block('source is not a sqlite database')
        return result

    # An active write-ahead log means the file on disk is not a consistent
    # snapshot.  Converting it anyway would mix two revisions.
    wal = path.with_name(path.name + '-wal')
    if wal.exists() and wal.stat().st_size > 0:
        result.block('active write-ahead log present: the source is not a '
                     'consistent snapshot')

    try:
        connection = _open_readonly(path)
    except sqlite3.Error as error:
        result.block('source cannot be opened read-only: {}'.format(error))
        return result

    try:
        result.user_version = connection.execute('PRAGMA user_version').fetchone()[0]
        result.application_id = connection.execute('PRAGMA application_id').fetchone()[0]
        result.journal_mode = connection.execute('PRAGMA journal_mode').fetchone()[0]
        result.space.page_size = connection.execute('PRAGMA page_size').fetchone()[0]
        result.space.page_count = connection.execute('PRAGMA page_count').fetchone()[0]

        objects = connection.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table','trigger','view','index')").fetchall()

        tables = {}
        triggers = []
        views = []
        for object_type, name, tbl_name, sql in objects:
            if object_type == 'trigger':
                triggers.append((name, tbl_name))
            elif object_type == 'view':
                views.append((name, tbl_name))
            elif object_type == 'table':
                tables[name] = (sql or '')

        for name, owner in triggers:
            result.block('unsupported trigger {!r} on table {!r}: a conversion '
                         'would drop it'.format(name, owner))
        for name, owner in views:
            result.block('unsupported view {!r} on table {!r}: a conversion would '
                         'drop it'.format(name, owner))

        for name in sorted(tables):
            if name.startswith('sqlite_'):
                continue
            sql = tables[name]
            if 'VIRTUAL TABLE' in sql.upper():
                result.block('unsupported virtual table {!r}'.format(name))
                continue
            columns = _columns_of(connection, name)
            row_count = connection.execute(
                'SELECT COUNT(*) FROM "{}"'.format(name.replace('"', '""'))).fetchone()[0]
            blob_columns = tuple(
                column for column in ORIGINAL_BLOB_TABLES.get(name, ())
                if column in columns)
            known = name in KNOWN_TABLES
            if not known:
                result.warn('unknown table {!r} is carried across as ordinary '
                            'data and must survive the round trip'.format(name))
            result.tables.append(TablePlan(
                name=name, columns=columns, row_count=row_count,
                original_blob_columns=blob_columns, known=known))
            result.space.row_count += row_count

        result.space.original_bytes = _original_bytes(connection, result.tables)
        result.space.metadata_bytes = result.space.page_size * result.space.page_count
        result.space.source_bytes = path.stat().st_size
        result.supported = not result.blockers
    except sqlite3.Error as error:
        result.block('source cannot be read: {}'.format(error))
    finally:
        connection.close()
    return result


def _columns_of(connection: sqlite3.Connection, table: str):
    try:
        rows = connection.execute('PRAGMA table_info("{}")'.format(
            table.replace('"', '""'))).fetchall()
    except sqlite3.Error:
        return ()
    return tuple(row[1] for row in rows)


def _original_bytes(connection: sqlite3.Connection, tables) -> int:
    """Total length of the ORIGINAL byte payloads, read as blobs."""
    total = 0
    for table in tables:
        for column in table.original_blob_columns:
            try:
                row = connection.execute(
                    'SELECT COALESCE(SUM(LENGTH("{}")), 0) FROM "{}"'.format(
                        column.replace('"', '""'),
                        table.name.replace('"', '""'))).fetchone()
            except sqlite3.Error:
                raise  # A failed size read is not evidence of zero bytes.
            total += row[0]
    return total


def plan_phases() -> list:
    """The ordered phase list, each with its own promise."""
    return [
        (MigrationPhase.DISCOVER, 'enumerate entities, references and order'),
        (MigrationPhase.PREFLIGHT, 'decide whether the source may be converted'),
        (MigrationPhase.COPY_METADATA, 'page the metadata into the target'),
        (MigrationPhase.COPY_OBJECTS, 'stream the original bytes into the object store'),
        (MigrationPhase.VERIFY, 'compare entity by entity, byte for byte'),
        (MigrationPhase.FINALIZE, 'close resources and persist the checkpoint'),
        (MigrationPhase.PUBLISH, 'publish the entry point; never delete the source'),
    ]


def space_upper_bound(result: PreflightResult, *, reserve_ratio: float = 0.2) -> dict:
    """Space to reserve on the target volume, with a working margin.

    The margin is a ratio of the estimate, not a fixed number, so it
    follows the data size instead of pretending to know the answer.
    """
    estimate = result.space
    total = estimate.total_bytes
    margin = int(total * reserve_ratio)
    return {
        'metadata_bytes': estimate.metadata_bytes,
        'original_bytes': estimate.original_bytes,
        'total_bytes': total,
        'margin_bytes': margin,
        'required_bytes': total + margin,
        'reserve_ratio': reserve_ratio,
        'basis': 'one copy of every original byte plus the metadata pages plus a '
                 'ratio margin; deduplicated content may need less, never more',
    }


__all__ = [
    'KNOWN_TABLES', 'ORIGINAL_BLOB_TABLES', 'MigrationPhase', 'TablePlan',
    'SpaceEstimate', 'PreflightResult', 'preflight_source', 'plan_phases',
    'space_upper_bound',
]
