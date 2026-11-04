"""Rebuildable, read-only-project catalog prototype.

Not the editable scene or a replacement save format. Only explicit audit tools
use this until partial-load save safety is implemented. Source JSON and original
bytes remain in the project; the cache stores small scalar projections only.

The cache also owns two derived indexes, both rebuildable **from the cache
itself** and never from the source project:

* ``counts_by_scope`` pre-aggregates the per ``(canvas_id, kind, untagged,
  deleted)`` counters, so the sidebar numbers are a table read instead of a
  full scan of the catalog;
* ``assets_fts`` is the FTS5 trigram index of the catalog rows. Its consumer
  side lives in :mod:`prism.search_candidates`; this module owns the table,
  the rows and the freshness marker.

Both are derived data: losing them costs a rebuild of the cache, never a
rebuild of the project.
"""
from collections import Counter
from contextlib import closing, contextmanager
from dataclasses import dataclass, replace
import json
import math
from pathlib import Path
import sqlite3
import threading
import time
from prism.source_fingerprint import file_fingerprint as _fingerprint
from prism.recent_usage import timestamp

from prism.search_candidates import (CATALOG_FTS_TABLE, TRIGRAM_MARKER_KEY,
                                     catalog_haystack, catalog_tag_names,
                                     catalog_trigram_plan, create_trigram_index,
                                     iter_catalog_candidate_rows,
                                     normalize_haystack, populate_trigram_index,
                                     trigram_available, trigram_index_status,
                                     trigram_projection, write_trigram_marker)


VERSION = 4
MAGIC = 'PrismReadOnlyCatalog'
ITEM_COLUMNS = 'id,type,x,y,z,scale,rotation,flip,data'

#: Pre-aggregated counters: one row per (canvas_id, kind, untagged, deleted).
COUNTS_TABLE = 'counts_by_scope'
#: ``catalog_state`` key of the pre-aggregate freshness marker.
COUNTS_MARKER_KEY = 'aggregate'

#: The canvas bucket of an asset whose canvas is gone from the project. It is
#: not a canvas id but a bucket, so it cannot be pushed down as a plain
#: ``IN`` list - it needs the set of canvases that still exist. A real page id
#: is a uuid4 hex or ``default-canvas``/``default-mindmap``/``default-document``,
#: so this literal cannot collide with a real canvas.
UNASSIGNED_CANVAS = '__unassigned__'
#: The "未分类" bucket: the default canvas plus every asset whose canvas is
#: gone. Same contract as :data:`UNASSIGNED_CANVAS`.
UNCLASSIFIED_CANVAS = '__unclassified__'


_writer_lock_state = threading.local()


class CatalogNotReady(RuntimeError):
    """An incomplete or stale cache must not look like an empty project."""


class CatalogBuildCancelled(RuntimeError):
    """A catalog build was cancelled before it could enter the writer."""


@dataclass(frozen=True)
class BuildResult:
    complete: bool
    total_records: int
    processed_records: int
    last_id: int | None
    seconds: float
    resumed: bool
    json_characters: int
    lock_wait_seconds: float = 0.0


def _tag_names(value):
    names = json.loads(value) if isinstance(value, str) and value else value
    if names is None:
        return []
    if not isinstance(names, list):
        raise ValueError('Catalog tags are not a list')
    return names


def _untagged(value):
    return int(not any(str(name).strip() for name in _tag_names(value)))


def _untagged_flag(value):
    """The untagged flag of a tags column, tolerant of malformed payloads.

    The exact scan asks SQLite for ``prism_untagged(tags)``, which raises when
    the column is not a list. The pre-aggregate is written while the catalog is
    built, so it may not fail a whole build over one malformed row: such a row
    is classified as tagged instead. Every well formed row gets exactly the
    flag the exact scan would compute.
    """
    try:
        return _untagged(value)
    except (TypeError, ValueError):
        return 0


def _tag_text(value):
    return ' '.join(str(name) for name in _tag_names(value)).casefold()


def _variable_limit(connection):
    """How many variables one statement may carry on this runtime.

    SQLite allows 32,766 variables on the runtime Prism ships with and 999 on
    the old ones. The connection knows which one it is, so a query never
    asks for more than the runtime accepts.
    """
    try:
        return int(connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, sqlite3.Error):
        return 999


class ProjectCatalogIndex:
    """Explicit single-builder cache; each operation owns its connections.

    Cancellation is cooperative. Native disk reads can outlast a time budget;
    the audit runner must also have a process timeout. Never open this cache
    from another writer or treat a partially built catalog as authoritative.
    """

    #: How the last :meth:`count` answered: ``'aggregate'``, ``'trigram'`` or
    #: ``'exact'``, with the candidate count and the selectivity of a
    #: trigram shortlist. It is a trace, not a promise.
    last_count_stats = None

    def __init__(self, source, cache):
        self.source = Path(source).resolve(strict=True)
        self.cache = Path(cache).resolve()
        if (self.source == self.cache or
                (self.cache.exists() and self.source.samefile(self.cache))):
            raise ValueError('Catalog cache must not be the source project')

    def _source(self):
        return sqlite3.connect(self.source.as_uri()+'?mode=ro', uri=True, timeout=.25)

    def _cache(self, readonly=False, *, timeout=None):
        if readonly:
            if not self.cache.exists():
                raise CatalogNotReady('Catalog has not been built')
            connection = sqlite3.connect(self.cache.as_uri()+'?mode=ro', uri=True,
                                         timeout=5.0 if timeout is None else timeout)
        else:
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.cache, timeout=.25)
        try:
            # SQLite lower()/LIKE are ASCII-only and treat %/_ as wildcards.
            # Search here has the same Unicode literal-substring contract as GUI.
            connection.create_function('prism_casefold', 1,
                                       lambda value: str(value or '').casefold(), deterministic=True)
            connection.create_function('prism_untagged', 1, _untagged, deterministic=True)
            connection.create_function('prism_untagged_safe', 1, _untagged_flag, deterministic=True)
            connection.create_function('prism_tag_text', 1, _tag_text, deterministic=True)
            # Refuse an unrelated database rather than replacing its tables.
            tables = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if tables:
                try:
                    marker = connection.execute(
                        "SELECT value FROM catalog_state WHERE key='magic'").fetchone()
                except sqlite3.DatabaseError:
                    raise ValueError('Destination is not a Prism catalog cache') from None
                if marker != (MAGIC,):
                    raise ValueError('Destination is not a Prism catalog cache')
            elif not readonly:
                connection.executescript('''
                    CREATE TABLE catalog_state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                    CREATE TABLE assets(
                        id INTEGER PRIMARY KEY,kind TEXT NOT NULL,canvas_id TEXT,
                        title TEXT,group_id TEXT,width INTEGER,height INTEGER,
                        has_preview INTEGER NOT NULL,has_original INTEGER NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]',rating INTEGER NOT NULL DEFAULT 0,
                        notes TEXT NOT NULL DEFAULT '',filename TEXT NOT NULL DEFAULT '',
                        deleted_at TEXT,last_used REAL NOT NULL DEFAULT 0);
                    CREATE INDEX assets_page ON assets(canvas_id,id);
                    CREATE INDEX assets_kind_page ON assets(canvas_id,kind,id);
                    CREATE TABLE counts(scope TEXT PRIMARY KEY,total INTEGER NOT NULL);
                ''')
                connection.execute('INSERT INTO catalog_state VALUES(?,?)', ('magic', MAGIC))
                connection.commit()
            if not readonly:
                # The derived indexes are additive: a cache that predates them
                # keeps working, it only answers the counters with the exact scan
                # until they are migrated. The `counts` table stays untouched.
                self._ensure_schema(connection)
            return connection
        except BaseException:
            connection.close()
            raise

    def _ensure_schema(self, connection):
        """Create the derived tables of the cache when they are missing.

        Idempotent, so a cache that already has them is left alone and an
        existing cache gains the derived indexes without a rebuild of the
        source project.
        """
        columns = {row[1] for row in connection.execute('PRAGMA table_info(assets)')}
        if 'last_used' not in columns:
            connection.execute('ALTER TABLE assets ADD COLUMN last_used REAL NOT NULL DEFAULT 0')
        connection.execute('''CREATE INDEX IF NOT EXISTS assets_recent
            ON assets(last_used DESC,id) WHERE deleted_at IS NULL AND last_used>0''')
        # The default recent count excludes annotation kinds and may restrict
        # canvases. Cover those fields so counting never fetches wide asset
        # rows (titles, notes, filenames) merely to check kind/canvas identity.
        recent_columns = tuple(row[2] for row in connection.execute(
            'PRAGMA index_info(assets_recent_metadata)'))
        if recent_columns and recent_columns != ('last_used', 'id', 'kind', 'canvas_id', 'deleted_at'):
            connection.execute('DROP INDEX assets_recent_metadata')
        connection.execute('''CREATE INDEX IF NOT EXISTS assets_recent_metadata
            ON assets(last_used DESC,id,kind,canvas_id,deleted_at)
            WHERE deleted_at IS NULL AND last_used>0''')
        connection.execute('''CREATE INDEX IF NOT EXISTS assets_untagged
            ON assets(prism_untagged_safe(tags),id) WHERE deleted_at IS NULL''')
        connection.execute('CREATE INDEX IF NOT EXISTS assets_tag_sets ON assets(tags,id)')
        connection.execute('''CREATE TABLE IF NOT EXISTS counts_by_scope(
            scope TEXT NOT NULL,canvas_id TEXT,kind TEXT NOT NULL,
            untagged INTEGER NOT NULL,deleted INTEGER NOT NULL,total INTEGER NOT NULL,
            PRIMARY KEY(scope,untagged,deleted))''')
        connection.execute('''CREATE INDEX IF NOT EXISTS counts_by_scope_kind
            ON counts_by_scope(kind,untagged,deleted)''')
        if trigram_available():
            create_trigram_index(connection, CATALOG_FTS_TABLE)
        connection.commit()

    @staticmethod
    def _state(connection):
        row = connection.execute(
            "SELECT value FROM catalog_state WHERE key='build'").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def _store(connection, state):
        connection.execute('INSERT OR REPLACE INTO catalog_state VALUES(?,?)',
                           ('build', json.dumps(state, separators=(',', ':'))))

    def _ready(self, connection):
        state = self._state(connection)
        if (not state or state.get('version') != VERSION or not state['complete']
                or state.get('invalid') or state['source'] != _fingerprint(self.source)):
            raise CatalogNotReady('Catalog is incomplete or source has changed')
        return state

    # ── derived indexes ─────────────────────────────────────────
    #
    # The pre-aggregated counters and the trigram index are derived from the
    # rows the cache already holds. Each one carries a freshness marker in
    # ``catalog_state``: a marker that matches the build state is the proof
    # that the derived data describes *these* rows. A marker that is missing or
    # stale is never trusted - the read paths fall back to the exact scan.

    @staticmethod
    def _marker_matches(connection, key, state):
        """Whether the freshness marker of ``key`` describes ``state``."""
        row = connection.execute('SELECT value FROM catalog_state WHERE key=?',
                                 (key,)).fetchone()
        if not row:
            return False
        try:
            marker = json.loads(row[0])
        except (TypeError, ValueError):
            return False
        return (isinstance(marker, dict)
                and marker.get('total') == state.get('total')
                and marker.get('last_id') == state.get('last_id')
                and marker.get('source') == state.get('source'))

    @staticmethod
    def _write_marker(connection, key, state):
        """Record that a derived index describes ``state``."""
        marker = {'total': int(state.get('total') or 0),
                  'last_id': state.get('last_id'),
                  'source': state.get('source')}
        connection.execute('INSERT OR REPLACE INTO catalog_state(key,value) VALUES(?,?)',
                           (key, json.dumps(marker, ensure_ascii=False, separators=(',', ':'))))
        return marker

    @staticmethod
    def _has_table(connection, name):
        return connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (name,)).fetchone() is not None

    def _clear_derived(self, connection):
        """Drop the derived indexes and their markers.

        The rows they describe are gone, so a marker that survived would
        describe an index that no longer holds the catalog - the one wrong
        answer: a short list that looks complete.
        """
        connection.execute('DELETE FROM ' + COUNTS_TABLE)
        connection.execute('DELETE FROM catalog_state WHERE key=?', (COUNTS_MARKER_KEY,))
        if self._has_table(connection, CATALOG_FTS_TABLE):
            connection.execute('DELETE FROM "%s"' % CATALOG_FTS_TABLE)
        connection.execute('DELETE FROM catalog_state WHERE key=?', (TRIGRAM_MARKER_KEY,))

    def _rebuild_counts_aggregate(self, connection, state):
        """Pre-aggregate the counters from the rows the cache already holds.

        Reads the cache only - the source project is not touched - so an
        existing cache gains the counters without a rebuild of the project.
        """
        rows = connection.execute(
            'SELECT canvas_id,kind,prism_untagged_safe(tags),deleted_at IS NOT NULL,'
            'COUNT(*) FROM assets GROUP BY canvas_id,kind,prism_untagged_safe(tags),'
            'deleted_at IS NOT NULL').fetchall()
        with connection:
            connection.execute('DELETE FROM ' + COUNTS_TABLE)
            connection.executemany(
                'INSERT INTO counts_by_scope(scope,canvas_id,kind,untagged,deleted,total) '
                'VALUES(?,?,?,?,?,?)',
                [(json.dumps([canvas, kind]), canvas, kind, int(untagged), int(deleted),
                  int(total))
                 for canvas, kind, untagged, deleted, total in rows])
            self._write_marker(connection, COUNTS_MARKER_KEY, state)

    def _rebuild_trigram_index(self, connection, state):
        """Fill the trigram virtual table from the rows the cache already holds.

        The projection is the one the consumer side expects: the rowid is the
        catalog id and the text is the projection of the canonical catalog
        haystack, so the candidate scan and the exact scan agree on what a
        word may match.
        """
        if not trigram_available():
            return
        rows = connection.execute(
            'SELECT id,title,filename,notes,tags FROM assets').fetchall()
        with connection:
            create_trigram_index(connection, CATALOG_FTS_TABLE)
            populate_trigram_index(
                connection, CATALOG_FTS_TABLE,
                ((row[0], catalog_haystack(row[1], row[2], row[3],
                                           catalog_tag_names(row[4])))
                 for row in rows))
            write_trigram_marker(connection, state)

    def _sync_derived(self, connection, state, *, trigram=True):
        """Make the derived indexes describe exactly the rows in the cache.

        A cache that was built before the derived indexes existed - or one
        whose marker does not match the build state - is migrated here, from
        the cache alone: the source project is never read again.
        """
        if not self._marker_matches(connection, COUNTS_MARKER_KEY, state):
            self._rebuild_counts_aggregate(connection, state)
        if trigram and trigram_available():
            if (not self._marker_matches(connection, TRIGRAM_MARKER_KEY, state)
                    or not self._trigram_rows_match(connection)):
                self._rebuild_trigram_index(connection, state)

    @staticmethod
    def _trigram_rows_match(connection):
        """Whether the trigram table holds one row per catalog row."""
        try:
            indexed = connection.execute('SELECT count(*) FROM "%s"' % CATALOG_FTS_TABLE).fetchone()[0]
            total = connection.execute('SELECT count(*) FROM assets').fetchone()[0]
        except sqlite3.DatabaseError:
            return False
        return indexed == total

    def source_revision(self):
        """Return the exact source/WAL identity used to coordinate builders."""
        fingerprint = _fingerprint(self.source)
        return (fingerprint['path'], tuple(fingerprint['file'] or ()),
                tuple(fingerprint['wal'] or ()))

    def build(self, *, batch_size=1024, max_seconds=None, cancel=None, on_batch=None,
              trigram=True, lock_wait_seconds=0.0, lock_poll_seconds=0.05,
              recover_corrupt=False):
        """Build (or resume) the catalog cache of this source project.

        ``trigram`` decides whether the build also fills the FTS5 trigram
        index of the catalog rows. It is on by default, so a fresh build
        produces a searchable index without a second step; turning it off only
        skips the index, it never leaves a stale one behind.

        The writer remains fail-fast by default. Background coordinators may
        opt into a finite, cancellable wait with ``lock_wait_seconds``; only
        SQLite BUSY/LOCKED responses are retried.

        ``batch_size`` is the number of rows one transaction covers.  The
        per-batch cost is fixed (one commit of the cache, two source checks),
        so the default is the largest batch the builder accepts: a 100k-row
        project measured 21.98 s at 128 rows per batch and 6.57 s at 1024
        rows per batch.  A bigger batch also means a longer cancellation
        delay -- one batch of rows has to finish before the cancel flag is
        read again -- which is why this is a default and not a hard limit.
        """
        started = time.perf_counter()
        wait = self._validated_lock_seconds('lock_wait_seconds', lock_wait_seconds,
                                            allow_zero=True)
        poll = self._validated_lock_seconds('lock_poll_seconds', lock_poll_seconds,
                                            allow_zero=False)
        if max_seconds is not None and max_seconds < 0:
            raise ValueError('max_seconds must be non-negative')
        deadline = started + wait
        if max_seconds is not None:
            deadline = min(deadline, started + max_seconds)
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with self._writer_lock(cancel=cancel, deadline=deadline,
                               poll_seconds=poll) as waited:
            if recover_corrupt:
                self._recover_corrupt_cache(cancel)
            remaining = (None if max_seconds is None else
                         max(0.0, max_seconds - (time.perf_counter() - started)))
            result = self._build(batch_size=batch_size, max_seconds=remaining,
                                 cancel=cancel, on_batch=on_batch, trigram=trigram)
        return replace(result, seconds=time.perf_counter() - started,
                       lock_wait_seconds=waited)

    def _recover_corrupt_cache(self, cancel):
        """Opt-in for a known disposable cache, under the single-writer lock.

        Never repair arbitrary SQLite databases or a broken source. Preserve
        the damaged cache for diagnosis instead of deleting it in place.
        """
        if cancel is not None and cancel():
            raise CatalogBuildCancelled('Cancelled before cache recovery')
        try:
            with closing(self._cache()) as cache:
                cache.execute('PRAGMA schema_version').fetchone()
            return
        except sqlite3.DatabaseError as error:
            code = getattr(error, 'sqlite_errorcode', 0) & 0xff
            if code not in (sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB):
                raise
            # A live WAL requires a coordinated checkpoint/recovery protocol;
            # do not rename half of an active SQLite file set.
            wal = Path(str(self.cache) + '-wal')
            if wal.exists() and wal.stat().st_size:
                raise
        if self.source == self.cache or self.source.samefile(self.cache):
            raise ValueError('Catalog recovery cannot replace the source project')
        if cancel is not None and cancel():
            raise CatalogBuildCancelled('Cancelled before cache quarantine')
        quarantine = self.cache.with_name(self.cache.name + f'.corrupt-{time.time_ns()}')
        self.cache.replace(quarantine)
        for suffix in ('-wal', '-shm', '-journal'):
            sidecar = Path(str(self.cache) + suffix)
            if sidecar.exists():
                sidecar.replace(Path(str(quarantine) + suffix))

    @staticmethod
    def _validated_lock_seconds(name, value, *, allow_zero):
        if isinstance(value, bool):
            raise ValueError(f'{name} must be a finite number')
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError(f'{name} must be a finite number') from None
        if not math.isfinite(value) or value < 0 or (not allow_zero and value == 0):
            qualifier = 'non-negative' if allow_zero else 'positive'
            raise ValueError(f'{name} must be a finite {qualifier} number')
        return value

    @staticmethod
    def _retryable_lock_error(error):
        code = getattr(error, 'sqlite_errorcode', None)
        if code is None:
            return False
        return (int(code) & 0xff) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)

    @contextmanager
    def _writer_lock(self, *, cancel=None, deadline=None, poll_seconds=0.05):
        """One writer per cache, with optional finite background waiting.

        A separate SQLite lock survives checkpoints without blocking cache
        readers. The default public deadline is immediate, preserving the
        historical fail-fast contract. The connection is always closed before
        the caller can hand work to a replacement builder.
        """
        lock_path = Path(str(self.cache)+'.build-lock')
        if lock_path == self.source or (lock_path.exists() and self.source.samefile(lock_path)):
            raise ValueError('Catalog lock must not be the source project')
        lock_key = str(lock_path.resolve())
        held = getattr(_writer_lock_state, 'paths', None)
        if held is None:
            held = _writer_lock_state.paths = set()
        started = time.perf_counter()
        if deadline is None:
            deadline = started
        lock = sqlite3.connect(lock_path, timeout=0)
        acquired = registered = False
        try:
            while True:
                if cancel is not None and cancel():
                    raise CatalogBuildCancelled('Catalog build cancelled while waiting for writer lock')
                try:
                    lock.execute('BEGIN EXCLUSIVE')
                    acquired = True
                    break
                except sqlite3.OperationalError as error:
                    if (lock_key in held or not self._retryable_lock_error(error)
                            or time.perf_counter() >= deadline):
                        raise
                    remaining = deadline - time.perf_counter()
                    if remaining <= 0:
                        raise
                    time.sleep(min(poll_seconds, remaining))
            held.add(lock_key)
            registered = True
            if cancel is not None and cancel():
                raise CatalogBuildCancelled('Catalog build cancelled after acquiring writer lock')
            yield time.perf_counter() - started
        finally:
            try:
                if acquired:
                    lock.rollback()
            finally:
                lock.close()
                if registered:
                    held.discard(lock_key)

    def needs_index_upgrade(self):
        """Read-only readiness hint; the existing background writer upgrades."""
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            columns = tuple(row[2] for row in cache.execute('PRAGMA index_info(assets_recent_metadata)'))
            return columns != ('last_used', 'id', 'kind', 'canvas_id', 'deleted_at')

    def ensure_indexes(self):
        """Bring the derived indexes in line with the rows the cache holds.

        Reads the cache only - the source project is never read - so an
        existing cache gains the pre-aggregated counters and the trigram index
        without the 91-second rebuild. Returns ``True`` when both are in
        place; an unusable cache raises :class:`CatalogNotReady`.
        """
        self.cache.parent.mkdir(parents=True, exist_ok=True)
        with self._writer_lock():
            with closing(self._cache()) as cache:
                state = self._ready(cache)
                self._sync_derived(cache, state)
                self._ready(cache)
                return True

    @staticmethod
    def _project_asset(row):
        key, kind, payload, has_original = row
        extra = json.loads(payload) if payload else {}
        if not isinstance(extra, dict):
            raise ValueError(f'Item {key} metadata is not an object')
        return (key, kind, extra.get('canvasId', 'default-canvas'), extra.get('title', ''),
                extra.get('groupId'), extra.get('imageWidth'), extra.get('imageHeight'),
                bool(extra.get('displayThumbnail') or extra.get('displayOverview')),
                has_original, json.dumps(extra.get('tags') or [], ensure_ascii=False, separators=(',', ':')),
                max(0, min(5, int(extra.get('rating', 0) or 0))),
                extra.get('notes', '') or '', extra.get('filename', '') or '', extra.get('deletedAt'),
                timestamp(extra.get('lastUsed')))

    def apply_save_receipts(self, receipts, *, cancel=None):
        """Transactionally advance a proven save chain, or request full rebuild.

        A stat mismatch alone is never permission to relabel old catalog rows.
        Every link must come from a confirmed partial save's before/after stamp.
        """
        started = time.perf_counter()
        with self._writer_lock(deadline=started+2.0, poll_seconds=.05, cancel=cancel) as waited:
            with closing(self._cache()) as cache, closing(self._source()) as source:
                state = self._state(cache)
                if not state or not state.get('complete') or state.get('invalid') or state.get('version') != VERSION:
                    return None
                chain = list(receipts)
                first = next((i for i, r in enumerate(chain)
                              if getattr(r, 'source_before', None) == state['source']), None)
                if first is None:
                    return None
                chain = chain[first:]
                expected = state['source']
                ids = set()
                for receipt in chain:
                    if (Path(receipt.filename).resolve() != self.source
                            or receipt.source_before != expected or receipt.source_after is None):
                        return None
                    expected = receipt.source_after
                    ids.update(receipt.catalog_item_ids)
                if expected != _fingerprint(self.source):
                    return None
                if not self._marker_matches(cache, COUNTS_MARKER_KEY, state):
                    return None
                source.execute('SELECT id FROM items LIMIT 1').fetchone()
                version = source.execute('PRAGMA data_version').fetchone()[0]
                has_fts = (self._has_table(cache, CATALOG_FTS_TABLE)
                           and self._marker_matches(cache, TRIGRAM_MARKER_KEY, state))
                changed = sorted(ids)
                characters = 0
                try:
                    cache.execute('BEGIN IMMEDIATE')

                    def bucket(row, amount):
                        cid, kind, raw, deleted = row
                        scope = json.dumps([cid, kind])
                        cache.execute('INSERT INTO counts VALUES(?,?) ON CONFLICT(scope) '
                                      'DO UPDATE SET total=total+excluded.total', (scope, amount))
                        cache.execute('INSERT INTO counts_by_scope '
                            '(scope,canvas_id,kind,untagged,deleted,total) VALUES(?,?,?,?,?,?) '
                            'ON CONFLICT(scope,untagged,deleted) DO UPDATE SET total=total+excluded.total',
                            (scope, cid, kind, _untagged_flag(raw), int(deleted is not None), amount))

                    for start in range(0, len(changed), 256):
                        if cancel and cancel():
                            raise CatalogBuildCancelled('Incremental catalog update cancelled')
                        batch = changed[start:start+256]
                        marks = ','.join('?' for _ in batch)
                        old = cache.execute(f'SELECT canvas_id,kind,tags,deleted_at FROM assets WHERE id IN ({marks})', batch).fetchall()
                        for row in old:
                            bucket(row, -1)
                        cache.execute(f'DELETE FROM assets WHERE id IN ({marks})', batch)
                        if has_fts:
                            cache.execute(f'DELETE FROM "{CATALOG_FTS_TABLE}" WHERE rowid IN ({marks})', batch)
                        item_rows = source.execute(
                            f'SELECT id,type,data FROM items WHERE id IN ({marks}) ORDER BY id', batch).fetchall()
                        # One bounded membership query avoids a correlated
                        # sqlar scan per item in older projects lacking an index.
                        originals = {int(row[0]) for row in source.execute(
                            f'SELECT DISTINCT item_id FROM sqlar WHERE item_id IN ({marks})', batch)}
                        rows = [(*row, int(row[0] in originals)) for row in item_rows]
                        projected = [self._project_asset(row) for row in rows]
                        characters += sum(len(row[2] or '') for row in rows)
                        cache.executemany('INSERT INTO assets(id,kind,canvas_id,title,group_id,width,height,'
                            'has_preview,has_original,tags,rating,notes,filename,deleted_at,last_used) '
                            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', projected)
                        for row in projected:
                            bucket((row[2], row[1], row[9], row[13]), 1)
                        if has_fts:
                            cache.executemany(f'INSERT INTO "{CATALOG_FTS_TABLE}"(rowid,text) VALUES(?,?)',
                                [(r[0], trigram_projection(normalize_haystack(catalog_haystack(
                                    r[3], r[12], r[11], catalog_tag_names(r[9]))))) for r in projected])
                        state['total'] += len(projected) - len(old)
                    if cancel and cancel():
                        raise CatalogBuildCancelled('Incremental catalog update cancelled')
                    if (_fingerprint(self.source) != expected or
                            source.execute('PRAGMA data_version').fetchone()[0] != version):
                        raise CatalogNotReady('Source changed during incremental catalog update')
                    cache.execute('DELETE FROM counts WHERE total=0')
                    cache.execute('DELETE FROM counts_by_scope WHERE total=0')
                    state.update(source=expected, last_id=cache.execute('SELECT MAX(id) FROM assets').fetchone()[0])
                    self._store(cache, state)
                    self._write_marker(cache, COUNTS_MARKER_KEY, state)
                    if has_fts:
                        write_trigram_marker(cache, state)
                    cache.commit()
                except BaseException:
                    cache.rollback()
                    raise
                return BuildResult(True, state['total'], len(changed), state['last_id'],
                                   time.perf_counter()-started, True, characters, waited)

    def _build(self, *, batch_size, max_seconds, cancel, on_batch, trigram=True):
        if not 1 <= batch_size <= 1024:
            raise ValueError('batch_size must be between 1 and 1024')
        if max_seconds is not None and max_seconds < 0:
            raise ValueError('max_seconds must be non-negative')
        started = time.perf_counter()
        stopped = lambda: ((cancel is not None and cancel()) or
                          (max_seconds is not None and time.perf_counter()-started >= max_seconds))
        fingerprint = _fingerprint(self.source)
        processed = characters = 0
        with closing(self._source()) as source, closing(self._cache()) as cache:
            source_version = source.execute('PRAGMA data_version').fetchone()[0]
            state = self._state(cache)
            resumed = bool(state and state.get('version') in (3, VERSION)
                           and state['source'] == fingerprint and not state.get('invalid'))
            if not resumed:
                state = dict(version=VERSION, source=fingerprint, complete=False,
                             last_id=None, total=0)
                with cache:
                    cache.execute('DELETE FROM assets')
                    cache.execute('DELETE FROM counts')
                    # The derived indexes describe the rows that just went
                    # away: the indexes and their markers have to go with
                    # them, or a marker would describe an index that no longer
                    # holds the catalog.
                    self._clear_derived(cache)
                    self._store(cache, state)
            else:
                upgrading = state['version'] != VERSION
                state['version'] = VERSION
                # A resumed build keeps the rows it already has, so the derived
                # indexes have to describe exactly those rows before the next
                # batch adds more. This is the cache-internal migration: it
                # never reads the source project again.
                self._sync_derived(cache, state, trigram=trigram)
                if upgrading:
                    with cache:
                        self._store(cache, state)

            def check_source():
                current_fingerprint = _fingerprint(self.source)
                current_version = source.execute('PRAGMA data_version').fetchone()[0]
                if (current_fingerprint != fingerprint or current_version != source_version):
                    state.update(complete=False, invalid=True)
                    with cache:
                        self._store(cache, state)
                    error = CatalogNotReady('Source changed during catalog build')
                    error.source_before = {key:fingerprint[key] for key in ('file','wal')}
                    error.source_after = {key:current_fingerprint[key] for key in ('file','wal')}
                    error.data_versions = (source_version, current_version)
                    raise error

            source.set_progress_handler(lambda: int(stopped()), 1000)
            while not state['complete'] and not stopped():
                where = '' if state['last_id'] is None else 'WHERE i.id>?'
                parameters = () if state['last_id'] is None else (state['last_id'],)
                try:
                    cursor = source.execute(
                        f'SELECT i.id,i.type,i.data,EXISTS('
                        f'SELECT 1 FROM sqlar s WHERE s.item_id=i.id) '
                        f'FROM items i {where} ORDER BY i.id LIMIT ?',
                        (*parameters, batch_size))
                    try:
                        rows = cursor.fetchmany(batch_size)
                    finally:
                        cursor.close()
                except sqlite3.OperationalError as error:
                    if getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT and stopped():
                        break
                    raise
                check_source()
                projected, counts, aggregates, haystacks = [], Counter(), Counter(), []
                for key, kind, payload, has_original in rows:
                    if stopped():
                        break
                    row = self._project_asset((key, kind, payload, has_original))
                    projected.append(row)
                    canvas, title, tags_json, notes, filename, deleted_at = (
                        row[2], row[3], row[9], row[11], row[12], row[13])
                    counts[canvas, kind] += 1
                    # The pre-aggregate is exact: one bucket per
                    # (canvas, kind, untagged, deleted) combination, never a
                    # sample and never a truncated list.
                    aggregates[canvas, kind, _untagged_flag(tags_json),
                               int(deleted_at is not None)] += 1
                    if trigram:
                        # The projection is the one the consumer side expects:
                        # the rowid is the catalog id and the text is the
                        # projection of the canonical catalog haystack.
                        haystacks.append((key, trigram_projection(normalize_haystack(
                            catalog_haystack(title, filename, notes,
                                             catalog_tag_names(tags_json))))))
                    characters += len(payload or '')
                if len(projected) != len(rows):
                    break  # Keep the previous committed checkpoint.
                check_source()
                with cache:
                    cache.executemany(
                        'INSERT INTO assets(id,kind,canvas_id,title,group_id,width,height,'
                        'has_preview,has_original,tags,rating,notes,filename,deleted_at,last_used) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', projected)
                    cache.executemany('''INSERT INTO counts VALUES(?,?)
                        ON CONFLICT(scope) DO UPDATE SET total=total+excluded.total''',
                        [(json.dumps([canvas, kind]), count)
                         for (canvas, kind), count in counts.items()])
                    cache.executemany('''INSERT INTO counts_by_scope
                        (scope,canvas_id,kind,untagged,deleted,total) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(scope,untagged,deleted)
                        DO UPDATE SET total=total+excluded.total''',
                        [(json.dumps([canvas, kind]), canvas, kind, untagged, deleted, count)
                         for (canvas, kind, untagged, deleted), count in aggregates.items()])
                    if trigram and haystacks:
                        cache.executemany('INSERT INTO "%s"(rowid,text) VALUES(?,?)'
                                          % CATALOG_FTS_TABLE, haystacks)
                    state.update(last_id=rows[-1][0] if rows else state['last_id'],
                                 total=state['total']+len(rows), complete=len(rows) < batch_size)
                    self._store(cache, state)
                    # The markers are written in the same transaction as the
                    # rows they describe, so a reader never sees an index that
                    # claims to be fresh while it is not.
                    self._write_marker(cache, COUNTS_MARKER_KEY, state)
                    if trigram:
                        write_trigram_marker(cache, state)
                processed += len(rows)
                if on_batch:
                    on_batch(state['total'])
            # Disabling the interrupt hook also lets us verify after cancellation.
            source.set_progress_handler(None, 0)
            check_source()
            return BuildResult(state['complete'], state['total'], processed, state['last_id'],
                               time.perf_counter()-started, resumed, characters)

    def page(self, canvas_id, *, after_id=None, limit=128, kind=None):
        """Bounded keyset paging; no OFFSET walk and no source JSON read."""
        if not 1 <= limit <= 1024:
            raise ValueError('limit must be between 1 and 1024')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            conditions, parameters = ['canvas_id IS ?'], [canvas_id]
            if after_id is not None:
                conditions.append('id>?')
                parameters.append(after_id)
            if kind is not None:
                conditions.append('kind=?')
                parameters.append(kind)
            cache.row_factory = sqlite3.Row
            # 只取标量摘要列：page 契约是不返回 tags/rating/notes/filename/
            # deleted_at 等元数据列，保持每页廉价（不随素材大小膨胀）。
            rows = cache.execute(
                'SELECT id,kind,canvas_id,title,group_id,width,height,'
                'has_preview,has_original FROM assets WHERE '
                + ' AND '.join(conditions) + ' ORDER BY id LIMIT ?',
                (*parameters, limit)).fetchall()
            result = tuple(dict(row) for row in rows)
            self._ready(cache)
            return result

    def details(self, item_ids):
        """Fetch canonical rows only for this batch, retaining unknown fields."""
        ids = tuple(dict.fromkeys(item_ids))
        if len(ids) > 1024:
            raise ValueError('Details must be fetched in bounded batches')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            if not ids:
                return ()
            with closing(self._source()) as source:
                source.row_factory = sqlite3.Row
                rows = source.execute(f'SELECT {ITEM_COLUMNS} FROM items '
                                      f'WHERE id IN ({",".join("?" for _ in ids)})', ids)
                found = {row['id']: dict(row) for row in rows}
            self._ready(cache)
            return tuple(found[key] for key in ids if key in found)

    # ── 聚合范围查询（不依赖 scene 内存对象） ─────────────────

    @staticmethod
    def _canvas_id_tuple(canvas_ids):
        """The canvas ids of a filter, with a plain string as ONE id.

        ``tuple('stress-canvas-0000')`` splits the id into its characters and
        turns the filter into ``canvas_id IN ('s','t','r',...)``: zero rows and
        no error - the one answer that is worse than an exception. A string is
        one canvas id here, exactly like it is one canvas id in the scope
        layer.
        """
        if isinstance(canvas_ids, (str, bytes)):
            return (canvas_ids,)
        return tuple(canvas_ids)

    @staticmethod
    def _predicate(*, kind=None, canvas_ids=None, untagged_only=False,
                   min_rating=0, text='', include_deleted=False,
                   known_canvas_ids=None, recent_only=False, recent_limit=None, tag_sets=None):
        """Build the WHERE clause + bound parameters shared by query/count.

        ``canvas_ids`` may carry the two bucket sentinels
        :data:`UNASSIGNED_CANVAS` and :data:`UNCLASSIFIED_CANVAS`. They are
        not canvas ids, they are the two buckets whose membership depends on
        which canvases still exist in the project, so they cannot be expressed
        as a plain ``IN`` list:

        * ``UNASSIGNED_CANVAS`` - the asset has no canvas, or its canvas is
          gone from the project (``default-canvas`` is an implicit default
          and stays itself);
        * ``UNCLASSIFIED_CANVAS`` - the asset is on ``default-canvas`` or has
          no canvas at all.

        ``known_canvas_ids`` is the set of canvases that still exist. With it,
        the two buckets are exactly the buckets the scene side computes
        (``project_catalog._effective_canvas_id``). Without it, the catalog
        can only say which rows belong to no canvas at all: the rows of a
        canvas that is gone are the scene's business, so a caller that wants
        the scene's numbers has to pass the set.
        """
        conditions, params = [], []
        if kind is not None:
            conditions.append('kind=?')
            params.append(kind)
        else:
            # Aggregate ranges are asset ranges; brush/文字 annotations are not
            # assets (matches `ProjectAssetCatalog.is_annotation`).
            conditions.append("kind NOT IN ('text','path')")
        if canvas_ids is not None:
            ids = ProjectCatalogIndex._canvas_id_tuple(canvas_ids)
            if not ids:
                return None, None
            present = tuple(value for value in ids if value is not None)
            buckets = tuple(value for value in ids
                            if value in (UNASSIGNED_CANVAS, UNCLASSIFIED_CANVAS))
            choices = []
            if present:
                choices.append('canvas_id IN (' + ','.join('?' for _ in present) + ')')
                params.extend(present)
            if None in ids:
                choices.append('canvas_id IS NULL')
            if buckets:
                known = tuple(value for value in (known_canvas_ids or ())
                              if value is not None)
                marks = ','.join('?' for _ in known)
                for bucket in buckets:
                    if bucket == UNASSIGNED_CANVAS:
                        # The rows of a canvas that is gone from the project.
                        if known:
                            choices.append("(canvas_id IS NULL OR "
                                           "(canvas_id <> 'default-canvas' AND "
                                           "canvas_id NOT IN (" + marks + ')))')
                        else:
                            # Without the known canvases the catalog can only
                            # say which rows belong to no canvas at all.
                            choices.append('canvas_id IS NULL')
                    else:
                        # The default canvas plus the rows of a canvas that is
                        # gone from the project.
                        if known:
                            choices.append("(canvas_id IS NULL OR "
                                           "canvas_id = 'default-canvas' OR "
                                           "canvas_id NOT IN (" + marks + '))')
                        else:
                            choices.append("(canvas_id IS NULL OR "
                                           "canvas_id = 'default-canvas')")
                    params.extend(known)
            conditions.append('(' + ' OR '.join(choices) + ')')
        if untagged_only:
            conditions.append('prism_untagged_safe(tags)=1')
        if tag_sets is not None:
            conditions.append('tags IN (SELECT value FROM json_each(?))')
            params.append(json.dumps(list(tag_sets), ensure_ascii=False))
        if min_rating:
            conditions.append('rating>=?')
            params.append(int(min_rating))
        conditions.append('deleted_at IS NOT NULL' if include_deleted else 'deleted_at IS NULL')
        for word in (text or '').split():
            word = word.casefold()
            if not word:
                continue
            conditions.append('(instr(prism_casefold(title),?)>0 OR '
                              'instr(prism_casefold(notes),?)>0 OR '
                              'instr(prism_casefold(filename),?)>0 OR '
                              'instr(prism_tag_text(tags),?)>0)')
            params.extend([word] * 4)
        if recent_only:
            conditions.append('last_used>0')
        where = ' AND '.join(conditions)
        if recent_only and recent_limit is not None:
            return ('id IN (SELECT id FROM assets WHERE ' + where +
                    ' ORDER BY last_used DESC,id LIMIT ?)', [*params, int(recent_limit)])
        return where, params

    def query(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False, limit=1024, after_id=None,
              known_canvas_ids=None, recent_only=False, recent_limit=None, tag_sets=None):
        """Bounded aggregate query over the catalog projection.

        This is the unified-range query backing "全部/未标签/未分类/默认/搜索":
        it reads the read-only cache, never the editable scene, so a partially
        loaded page still yields the whole-project truth. Text matching is a
        Unicode case-insensitive literal substring over title/notes/filename/tags.
        Rows have stable ascending IDs; pass the last ID as ``after_id``. Full
        `tags.parse_query` semantics stay in `project_catalog.query`.

        ``canvas_ids`` may carry the bucket sentinels
        :data:`UNASSIGNED_CANVAS` / :data:`UNCLASSIFIED_CANVAS`; they need
        ``known_canvas_ids`` (see :meth:`_predicate`).
        """
        if not 1 <= limit <= 1024:
            raise ValueError('limit must be between 1 and 1024')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            where, params = self._predicate(
                kind=kind, canvas_ids=canvas_ids, untagged_only=untagged_only,
                min_rating=min_rating, text=text, include_deleted=include_deleted,
                known_canvas_ids=known_canvas_ids, recent_only=recent_only, recent_limit=recent_limit, tag_sets=tag_sets)
            if where is None:
                return ()
            if after_id is not None:
                where += ' AND id>?'
                params.append(after_id)
            cache.row_factory = sqlite3.Row
            rows = cache.execute(
                'SELECT id,kind,canvas_id,title,group_id,width,height,'
                'has_preview,has_original,tags,rating,notes,filename,deleted_at,last_used '
                'FROM assets WHERE ' + where + ' ORDER BY id LIMIT ?',
                (*params, limit)).fetchall()
            self._ready(cache)
            return tuple(dict(row) for row in rows)

    def count(self, *, kind=None, canvas_ids=None, untagged_only=False,
              min_rating=0, text='', include_deleted=False, known_canvas_ids=None,
              recent_only=False, recent_limit=None, tag_sets=None):
        """Matching-row count for the same predicate as :meth:`query`.

        A text filter is answered by the trigram index when the cache has a
        verified one: the index shortlists the rows that can match, the exact
        filter still decides. The route and its selectivity are recorded in
        :attr:`last_count_stats`.
        """
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            where, params = self._predicate(
                kind=kind, canvas_ids=canvas_ids, untagged_only=untagged_only,
                min_rating=min_rating, text=text, include_deleted=include_deleted,
                known_canvas_ids=known_canvas_ids, recent_only=recent_only, recent_limit=recent_limit, tag_sets=tag_sets)
            if where is None:
                return 0
            # Recent/tag counts lack the general aggregate table's fast path.
            # Reuse exact scalar answers while source, cache and WAL are stable.
            # Filters remain part of the key; dirty scene overlays stay with
            # the caller, which applies them to these persisted-row totals.
            count_signature = None
            if recent_only or tag_sets is not None:
                count_signature = (json.dumps(_fingerprint(self.source), sort_keys=True),
                                   json.dumps(_fingerprint(self.cache), sort_keys=True))
                count_key = (where, tuple(params))
                previous_counts = getattr(self, '_exact_count_cache', None)
                answers = (previous_counts[1] if previous_counts is not None
                           and previous_counts[0] == count_signature else {})
                if count_key in answers:
                    self.last_count_stats = dict(route='exact_cached', text=text)
                    return answers[count_key]
            if text and not (kind is not None or canvas_ids is not None
                             or untagged_only or min_rating or include_deleted or recent_only or tag_sets is not None):
                total = self._count_text_trigram(cache, text)
                if total is not None:
                    self._ready(cache)
                    return total
            total = None if recent_only or tag_sets is not None else self._count_aggregate(
                cache, kind=kind, canvas_ids=canvas_ids, untagged_only=untagged_only,
                min_rating=min_rating, text=text, include_deleted=include_deleted)
            if total is None:
                total = cache.execute('SELECT COUNT(*) FROM assets WHERE ' + where, params).fetchone()[0]
                self.last_count_stats = dict(route='exact', text=text)
            self._ready(cache)
            if count_signature is not None:
                current_signature = (json.dumps(_fingerprint(self.source), sort_keys=True),
                                     json.dumps(_fingerprint(self.cache), sort_keys=True))
                if current_signature != count_signature:
                    raise CatalogNotReady('Source or cache changed during exact count')
                # Copy-on-publication; keep at most 64 scalar answers.
                answers = dict(answers)
                if len(answers) >= 64:
                    answers.pop(next(iter(answers)))
                answers[count_key] = total
                self._exact_count_cache = (count_signature, answers)
            return total

    def _count_text_trigram(self, connection, text):
        """The number of rows matching ``text``, shortlisted by the trigram index.

        ``None`` means the index cannot answer it: the exact scan has to. The
        trigram path is only used when the index is verified, because a stale
        index would answer with a *short* list - the one wrong answer.

        The index only rejects rows: the exact word filter decides, so the
        count is the same the exact scan gives.
        """
        plan = catalog_trigram_plan(text)
        if plan is None:
            # Every word is shorter than three characters: the tokenizer
            # cannot index them, so the exact scan answers.
            self.last_count_stats = dict(route='exact', text=text, reason='no anchor')
            return None
        status = trigram_index_status(connection, table='assets', id_column='id',
                                      fts_table=CATALOG_FTS_TABLE)
        if not status.usable:
            self.last_count_stats = dict(route='exact', text=text,
                                         reason=status.reason)
            return None
        candidates = total = 0
        for row in iter_catalog_candidate_rows(
                connection, text, table='assets', fts_table=CATALOG_FTS_TABLE,
                columns=('id', 'title', 'filename', 'notes', 'tags')):
            candidates += 1
            haystack = catalog_haystack(row['title'], row['filename'], row['notes'],
                                        catalog_tag_names(row['tags']))
            if all(word in haystack for word in plan.words):
                total += 1
        rows = status.rows or 0
        self.last_count_stats = dict(
            route='trigram', text=text, candidates=candidates, hits=total,
            rows=rows, selectivity=(candidates / rows) if rows else None)
        return total

    def _count_aggregate(self, connection, *, kind, canvas_ids, untagged_only,
                          min_rating, text, include_deleted):
        """The pre-aggregated row count, or ``None`` when it cannot answer.

        Only the filters the pre-aggregate knows about can be answered by it.
        A text or a rating filter needs the rows themselves, so those counts
        keep the exact scan.
        """
        if text or min_rating:
            return None
        if canvas_ids is not None and any(
                value in (UNASSIGNED_CANVAS, UNCLASSIFIED_CANVAS)
                for value in self._canvas_id_tuple(canvas_ids)):
            # The bucket sentinels are not canvas ids: the pre-aggregate stores
            # the raw canvas id of every row, so it cannot answer them.
            return None
        state = self._state(connection)
        if state is None or not self._marker_matches(connection, COUNTS_MARKER_KEY, state):
            return None
        conditions, params = [], []
        if kind is not None:
            conditions.append('kind=?')
            params.append(kind)
        else:
            conditions.append("kind NOT IN ('text','path')")
        conditions.append('deleted=1' if include_deleted else 'deleted=0')
        if untagged_only:
            conditions.append('untagged=1')
        if canvas_ids is not None:
            ids = self._canvas_id_tuple(canvas_ids)
            if not ids:
                return 0
            present = tuple(value for value in ids if value is not None)
            choices = []
            if present:
                choices.append('canvas_id IN (' + ','.join('?' for _ in present) + ')')
                params.extend(present)
            if None in ids:
                choices.append('canvas_id IS NULL')
            conditions.append('(' + ' OR '.join(choices) + ')')
        total = connection.execute('SELECT COALESCE(SUM(total),0) FROM ' + COUNTS_TABLE
                                   + ' WHERE ' + ' AND '.join(conditions), params).fetchone()[0]
        self.last_count_stats = dict(route='aggregate')
        return int(total)

    def canvas_counts(self, *, kind=None, untagged_only=False):
        """Active asset counts grouped by ``canvas_id`` from the cache.

        Used by the aggregate sidebar/resource-tree counters so a partially
        loaded page still reports whole-project per-canvas numbers without
        touching the editable scene. Soft-deleted rows are excluded (matching
        `ProjectAssetCatalog.active_items`).

        The numbers come from the pre-aggregated counters when the cache has
        them, and from the grouped scan when it does not. Both paths answer
        the same numbers: the pre-aggregate is exact, never a sample.
        """
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            result = self._canvas_counts_aggregate(cache, kind, untagged_only)
            if result is None:
                result = self._canvas_counts_exact(cache, kind, untagged_only)
            self._ready(cache)
            return result

    def _canvas_counts_aggregate(self, connection, kind, untagged_only):
        """The pre-aggregated per-canvas counts, or ``None`` when unusable."""
        state = self._state(connection)
        if state is None or not self._marker_matches(connection, COUNTS_MARKER_KEY, state):
            return None
        conditions, params = ['deleted=0'], []
        if kind is not None:
            conditions.append('kind=?')
            params.append(kind)
        else:
            conditions.append("kind NOT IN ('text','path')")
        if untagged_only:
            conditions.append('untagged=1')
        rows = connection.execute(
            'SELECT canvas_id,SUM(total) FROM ' + COUNTS_TABLE + ' WHERE '
            + ' AND '.join(conditions) + ' GROUP BY canvas_id', params).fetchall()
        return {canvas_id: int(total) for canvas_id, total in rows}

    def _canvas_counts_exact(self, connection, kind, untagged_only):
        """The grouped scan over the catalog rows (the fallback)."""
        conditions, params = ['deleted_at IS NULL'], []
        if kind is not None:
            conditions.append('kind=?')
            params.append(kind)
        else:
            conditions.append("kind NOT IN ('text','path')")
        if untagged_only:
            conditions.append('prism_untagged(tags)=1')
        rows = connection.execute(
            'SELECT canvas_id,COUNT(*) FROM assets WHERE '
            + ' AND '.join(conditions) + ' GROUP BY canvas_id', params).fetchall()
        return {canvas_id: int(total) for canvas_id, total in rows}


    #: How many ids one :meth:`summaries` call may carry. The call chunks them
    #: internally, so one call with a whole overlay costs one connection
    #: instead of one connection per 1024 ids.
    MAX_SUMMARY_IDS = 200000

    #: The scalar summary columns one :meth:`summaries_grouped` row carries.
    SUMMARY_FIELDS = ('id', 'kind', 'canvas_id', 'tags', 'rating', 'notes',
                      'filename', 'deleted_at', 'title')

    #: How many ids one SQL statement may carry. SQLite's own limit is
    #: 32,766 variables per statement; the value is read from the connection
    #: so a build with a smaller limit still works.
    SUMMARY_CHUNK = 32000

    def _chunk_size(self, connection):
        """The id count one statement may carry: the cap or the runtime limit.

        The cap leaves room under SQLite's own limit; the runtime limit wins
        when it is smaller, so an old SQLite never sees a statement it would
        refuse.
        """
        return max(1, min(self.SUMMARY_CHUNK, _variable_limit(connection)))

    def summaries(self, item_ids):
        """Small cache rows for overlay reconciliation; no original JSON reads.

        One call may carry a whole overlay: the ids are chunked here, so the
        caller does not have to open a connection per 1024 ids. The rows are
        the same ones as before, in ascending id order.
        """
        ids = tuple(dict.fromkeys(item_ids))
        if len(ids) > self.MAX_SUMMARY_IDS:
            raise ValueError('Summaries must be fetched in bounded batches')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            cache.row_factory = sqlite3.Row
            rows = []
            chunk_size = self._chunk_size(cache)
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                rows.extend(cache.execute(
                    'SELECT id,kind,canvas_id,tags,deleted_at FROM assets WHERE id IN ('
                    + ','.join('?' for _ in chunk) + ')', chunk).fetchall())
            rows.sort(key=lambda row: row['id'])
            self._ready(cache)
            return tuple(dict(row) for row in rows)

    def summaries_grouped(self, item_ids):
        """The scalar summary rows of the given catalog ids, in input order.

        One call, one connection and a handful of queries: the ids are chunked
        here, so a caller that has a whole overlay to reconcile does not pay
        one connection per batch. The rows carry the same columns
        :meth:`summaries` returns plus ``rating``, ``notes`` and
        ``filename``; an id the catalog does not know is not returned.

        The rows are the ones the overlay reconciliation needs, so a caller
        that only wants the *counters* should use :meth:`summaries_buckets` or
        :meth:`summaries_uncovered` - they answer with aggregates instead of
        with one row per asset.
        """
        ids = tuple(item_ids)
        if not ids:
            return ()
        if len(ids) > self.MAX_SUMMARY_IDS:
            raise ValueError('Summaries must be fetched in bounded batches')
        wanted = set(ids)
        ascending = all(first < second for first, second in zip(ids, ids[1:]))
        # The ids that arrive in ascending order need no second map: the
        # query already answers in id order.
        chunk = ids if ascending else sorted(wanted)
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            rows = {}
            chunk_size = self._chunk_size(cache)
            for start in range(0, len(chunk), chunk_size):
                part = chunk[start:start + chunk_size]
                marks = ','.join('?' for _ in part)
                for row in cache.execute(
                        'SELECT ' + ','.join(self.SUMMARY_FIELDS) +
                        ' FROM assets WHERE id IN (' + marks + ')', part):
                    rows[row[0]] = dict(zip(self.SUMMARY_FIELDS, row))
            self._ready(cache)
            if ascending:
                return tuple(rows.values())
            return tuple(rows[key] for key in ids if key in rows)

    def summaries_buckets(self, item_ids):
        """The counter buckets of the given catalog rows.

        The same buckets as :meth:`grouped_counts`, restricted to the given
        ids: one entry per ``(canvas_id, deleted, untagged)`` with the number of
        the given asset rows in it. It answers a counter question with a
        counter, so a caller that has to reconcile the rows it already loaded
        does not have to read them back one by one - and it needs one
        connection for the whole list instead of one per batch.

        Only asset rows are counted (``kind NOT IN ('text','path')``), exactly
        like :meth:`grouped_counts`.

        A caller that covers *every* catalog row - a fully loaded project -
        gets the pre-aggregated buckets: the buckets of the covered rows are
        then the buckets of the whole catalog, and the pre-aggregate already
        holds them. The numbers are the same either way, so the two paths
        cannot drift.
        """
        ids = tuple(dict.fromkeys(item_ids))
        if len(ids) > 1000000:
            raise ValueError('Summaries must be fetched in bounded batches')
        if not ids:
            return ()
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            every_row = self._buckets_of_every_row(cache, ids)
            if every_row is not None:
                self._ready(cache)
                return every_row
            totals = Counter()
            chunk_size = self._chunk_size(cache)
            for start in range(0, len(ids), chunk_size):
                chunk = ids[start:start + chunk_size]
                rows = cache.execute(
                    'SELECT canvas_id,deleted_at IS NOT NULL,prism_untagged_safe(tags),'
                    'COUNT(*) FROM assets WHERE id IN ('
                    + ','.join('?' for _ in chunk) + ') '
                    "AND kind NOT IN ('text','path') "
                    'GROUP BY canvas_id,deleted_at IS NOT NULL,prism_untagged_safe(tags)',
                    chunk).fetchall()
                for canvas, deleted, untagged, total in rows:
                    totals[(canvas, bool(deleted), bool(untagged))] += int(total)
            self._ready(cache)
            return tuple((canvas, deleted, untagged, total)
                         for (canvas, deleted, untagged), total in totals.items())

    def _buckets_of_every_row(self, connection, ids):
        """The buckets of the given ids when they cover every catalog row.

        ``None`` when the given ids do not cover every row, or when the
        pre-aggregate cannot answer - then the caller reads the rows.

        The check is exact: the given ids have to hit as many catalog rows as
        the catalog holds. A cheaper test - "the ids are as many as the rows" -
        would accept ids that are not rows and rows that are not covered, and
        that would answer with a bucket the row scan would not give.
        """
        state = self._state(connection)
        if state is None or not self._marker_matches(connection, COUNTS_MARKER_KEY,
                                                      state):
            return None
        rows = connection.execute(
            'SELECT COALESCE(SUM(total),0) FROM ' + COUNTS_TABLE).fetchone()[0]
        if not rows:
            return None
        if self._count_rows_in(connection, ids) != rows:
            return None
        return tuple((canvas, bool(deleted), bool(untagged), int(total))
                     for canvas, deleted, untagged, total
                     in self._grouped_counts_rows(connection))

    def _count_rows_in(self, connection, ids):
        """How many catalog rows the given ids hit."""
        total = 0
        chunk_size = self._chunk_size(connection)
        for start in range(0, len(ids), chunk_size):
            part = ids[start:start + chunk_size]
            total += connection.execute(
                'SELECT COUNT(*) FROM assets WHERE id IN ('
                + ','.join('?' for _ in part) + ')', part).fetchone()[0]
        return total

    def summaries_uncovered(self, item_ids):
        """The counter buckets of the catalog rows the caller does **not** have.

        The overlay reconciliation asks the disk for the rows it already
        loaded, which costs one row per loaded asset - the reason a fully
        loaded project paid for 98 round trips to learn what it already knew.
        This answers the complementary question: the rows the caller is
        missing. A fully loaded project has none, so the disk adds nothing and
        the counter costs one range scan.

        The buckets are the ones of :meth:`grouped_counts` (asset rows only,
        ``kind NOT IN ('text','path')``). The caller adds its own rows to
        them, so every asset is counted exactly once.
        """
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            bounds = cache.execute('SELECT min(id),max(id) FROM assets').fetchone()
            if not bounds or bounds[0] is None:
                self._ready(cache)
                return ()
            lowest, highest = int(bounds[0]), int(bounds[1])
            covered = sorted({int(value) for value in item_ids})
            ranges, start = [], lowest
            for value in covered:
                if value < start:
                    continue
                if value > start:
                    ranges.append((start, value - 1))
                start = value + 1
                if start > highest:
                    break
            if start <= highest:
                ranges.append((start, highest))
            totals = Counter()
            for first, last in ranges:
                rows = cache.execute(
                    'SELECT canvas_id,deleted_at IS NOT NULL,prism_untagged_safe(tags),'
                    "COUNT(*) FROM assets WHERE id BETWEEN ? AND ? "
                    "AND kind NOT IN ('text','path') "
                    'GROUP BY canvas_id,deleted_at IS NOT NULL,prism_untagged_safe(tags)',
                    (first, last)).fetchall()
                for canvas, deleted, untagged, total in rows:
                    totals[(canvas, bool(deleted), bool(untagged))] += int(total)
            self._ready(cache)
            return tuple((canvas, deleted, untagged, total)
                         for (canvas, deleted, untagged), total in totals.items())

    def card_rows(self, item_ids):
        """Read a small explicit set of existing card projections by primary key."""
        ids = tuple(dict.fromkeys(int(value) for value in item_ids))
        if not ids:
            return ()
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            cache.row_factory = sqlite3.Row
            rows = []
            size = self._chunk_size(cache)
            for start in range(0, len(ids), size):
                chunk = ids[start:start + size]
                rows.extend(dict(row) for row in cache.execute(
                    'SELECT id,kind,canvas_id,title,group_id,width,height,has_preview,has_original,'
                    'tags,rating,notes,filename,deleted_at,last_used FROM assets WHERE id IN ('
                    + ','.join('?' for _ in chunk) + ')', chunk))
            self._ready(cache)
            return tuple(rows)

    def tag_groups(self, *, cancel=None, include_deleted=False):
        """Version-cached tag-set counts; metadata only, never source images.

        Keep tag sets together so aliases/parents can count one asset once.
        Per-name totals alone cannot represent overlapping tags correctly.
        """
        if cancel and cancel():
            raise CatalogBuildCancelled('Tag aggregation cancelled')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            signature = (json.dumps(_fingerprint(self.source), sort_keys=True),
                         json.dumps(_fingerprint(self.cache), sort_keys=True), include_deleted)
            previous = getattr(self, '_tag_groups_cache', {})
            if isinstance(previous, dict) and signature in previous:
                return previous[signature]
            if cancel is not None:
                cache.set_progress_handler(lambda: int(bool(cancel())), 1000)
            try:
                rows = tuple(cache.execute(
                    "SELECT canvas_id,tags,COUNT(*) FROM assets "
                    "WHERE kind NOT IN ('text','path') AND " + ('deleted_at IS NOT NULL ' if include_deleted else 'deleted_at IS NULL ') +
                    "GROUP BY canvas_id,tags"))
            except sqlite3.OperationalError:
                if cancel and cancel():
                    raise CatalogBuildCancelled('Tag aggregation cancelled') from None
                raise
            finally:
                if cancel is not None:
                    cache.set_progress_handler(None, 0)
            if cancel and cancel():
                raise CatalogBuildCancelled('Tag aggregation cancelled')
            self._ready(cache)
            if signature != (json.dumps(_fingerprint(self.source), sort_keys=True),
                             json.dumps(_fingerprint(self.cache), sort_keys=True), include_deleted):
                raise CatalogNotReady('Source changed during tag aggregation')
            self._tag_groups_cache = {key: value for key, value in previous.items()
                                      if key[:2] == signature[:2]} if isinstance(previous, dict) else {}
            self._tag_groups_cache[signature] = rows
            return rows

    def grouped_counts(self):
        """One scalar aggregate for active/trash/tag and per-canvas counters."""
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            signature = (_fingerprint(self.source), _fingerprint(self.cache))
            previous = getattr(self, '_grouped_counts_cache', None)
            if previous is not None and previous[0] == signature:
                return previous[1]
            rows = self._grouped_counts_rows(cache)
            self._ready(cache)
            result = tuple((cid, bool(deleted), bool(untagged), int(total))
                           for cid, deleted, untagged, total in rows)
            self._grouped_counts_cache = (signature, result)
            return result

    def _grouped_counts_rows(self, connection):
        """The ``(canvas, deleted, untagged, total)`` buckets of the counters.

        The pre-aggregate answers the same buckets the grouped scan produces;
        only the scan has to touch every catalog row.
        """
        state = self._state(connection)
        if state is not None and self._marker_matches(connection, COUNTS_MARKER_KEY, state):
            return connection.execute(
                'SELECT canvas_id,deleted,untagged,total FROM ' + COUNTS_TABLE +
                " WHERE kind NOT IN ('text','path') "
                'ORDER BY canvas_id,deleted,untagged').fetchall()
        return connection.execute(
            "SELECT canvas_id,deleted_at IS NOT NULL,prism_untagged(tags),COUNT(*) "
            "FROM assets WHERE kind NOT IN ('text','path') "
            "GROUP BY canvas_id,deleted_at IS NOT NULL,prism_untagged(tags)").fetchall()


    def display_summaries(self, item_ids):
        """Only display metadata for visible proxies; never read sqlar originals."""
        ids = tuple(dict.fromkeys(item_ids))
        if len(ids) > 128:
            raise ValueError('Display metadata must be fetched in bounded batches')
        with closing(self._cache(readonly=True)) as cache:
            self._ready(cache)
            if not ids:
                return ()
            with closing(self._source()) as source:
                rows = source.execute(
                    "SELECT id,json_extract(data,'$.displayOverview'),"
                    "json_extract(data,'$.displayThumbnail'),json_extract(data,'$.crop'),"
                    "json_extract(data,'$.grayscale'),flip,"
                    "json_extract(data,'$.opacity') FROM items WHERE id IN ("
                    + ','.join('?' for _ in ids) + ')', ids).fetchall()
            self._ready(cache)
            return tuple(dict(id=key, overview=json.loads(overview) if overview else None,
                              thumbnail=json.loads(thumbnail) if thumbnail else None,
                              crop=json.loads(crop) if crop else None, grayscale=bool(gray),
                              flip=flip if flip is not None else 1,
                              opacity=opacity if opacity is not None else 1)
                         for key, overview, thumbnail, crop, gray, flip, opacity in rows)


# ── pkg3: 范围快照 / 呈现 marker / stall 上下文 的共享埋点 ─────────────
#
# 四个 owner 模块（`project_open_scope`、`project_catalog`、
# `widgets/asset_browser`、`widgets/asset_canvas`）共用这一段埋点：
#
# * **只观察，不查询。** 每个字段都来自调用方**已经算好的**计数或状态；拿不到
#   就写 `PKG3_UNKNOWN`（`'unknown'`），**绝不为记日志再跑一次 COUNT、再遍历
#   一遍十万行**。所以这里没有连接、没有 cursor：拿不到数据时只能说实话。
# * **不序列化整个集合。** 页面 id 只给有限样本（`SCOPE_SAMPLE_LIMIT`）加一个
#   稳定指纹；样本带 `sampled=True`，指纹只用来比较"是不是同一个范围"。
# * **空集合与 None 严格区分。** `scope_pages=None`（没有范围这个概念）与
#   `scope_pages=set()`（明确就是空的）是两件事：分别是
#   `pages_known=False` 与 `pages_known=True, pages_count=0`，绝不合流。
# * **降级链。** `performance_trace` 是并行实现中的包 1：符号 import 不到或
#   调用抛异常时，事件只交给永远活着的进程内观察者，绝不打断业务路径。

import hashlib
import threading
import time as _pkg3_time
from collections import deque as _pkg3_deque

try:  # pragma: no cover - 包 1 并行落地中，符号缺失时整段降级
    from prism.performance_trace import record as _pkg3_trace_record
except Exception:  # pragma: no cover
    _pkg3_trace_record = None
try:  # pragma: no cover
    from prism.performance_trace import span as _pkg3_trace_span
except Exception:  # pragma: no cover
    _pkg3_trace_span = None
try:  # pragma: no cover
    from prism.performance_trace import current_context as _pkg3_current_context
except Exception:  # pragma: no cover
    _pkg3_current_context = None
try:  # pragma: no cover
    from prism.performance_trace import capture_context as _pkg3_capture_context
except Exception:  # pragma: no cover
    _pkg3_capture_context = None
try:  # pragma: no cover
    from prism.performance_trace import restore_context as _pkg3_restore_context
except Exception:  # pragma: no cover
    _pkg3_restore_context = None
try:  # pragma: no cover
    from prism.performance_trace import PRIORITY_STATE as _PKG3_PRIORITY_STATE
except Exception:  # pragma: no cover
    _PKG3_PRIORITY_STATE = None
try:  # pragma: no cover
    from prism.performance_trace import PRIORITY_TERMINAL as _PKG3_PRIORITY_TERMINAL
except Exception:  # pragma: no cover
    _PKG3_PRIORITY_TERMINAL = None


#: 拿不到就是拿不到：快照里写它，不猜，也不用 0 冒充。
PKG3_UNKNOWN = 'unknown'

#: 范围 id 样本上限。十万个页面 id 的列表不是日志。
SCOPE_SAMPLE_LIMIT = 5

#: 默认画布的固定 id（隐式页面），与 `workspace_service` / `item_metadata`
#: 用的是同一个字面量。
DEFAULT_CANVAS_IDS = ('default-canvas',)

#: 事件名（规格 §6 / §7 / §8）。
SCOPE_SNAPSHOT_EVENT = 'catalog.scope_snapshot'
RANGE_SNAPSHOT_EVENT = 'catalog.range_snapshot'
FRAME_EVENT = 'assets.paint_frame'
STALL_EVENT = 'ui.stall_context'

_PKG3_LOCK = threading.Lock()
_PKG3_EVENTS: '_pkg3_deque' = _pkg3_deque(maxlen=512)
_PKG3_OBSERVERS: list = []


def add_pkg3_observer(callback) -> None:
    """注册 pkg3 观察者：``callback(event, **fields)``，同步调用。"""
    if callback not in _PKG3_OBSERVERS:
        _PKG3_OBSERVERS.append(callback)


def remove_pkg3_observer(callback) -> None:
    """注销 pkg3 观察者。"""
    try:
        _PKG3_OBSERVERS.remove(callback)
    except ValueError:
        pass


def pkg3_events():
    """最近的 pkg3 事件（最多 :data:`_PKG3_EVENTS` 的容量条）。"""
    with _PKG3_LOCK:
        return tuple(_PKG3_EVENTS)


def clear_pkg3_events() -> None:
    """清空最近事件（测试用；生产路径不调用）。"""
    with _PKG3_LOCK:
        _PKG3_EVENTS.clear()


def _deliver_pkg3(event, payload):
    """Deliver one in-process observation without creating a second trace event."""
    with _PKG3_LOCK:
        _PKG3_EVENTS.append((event, dict(payload)))
        observers = tuple(_PKG3_OBSERVERS)
    for callback in observers:
        try:
            callback(event, **payload)
        except Exception:
            pass


def record_pkg3(event: str, *, _priority=None, **fields):
    """记一条 pkg3 事件：进程内观察者永远到达，trace 尽力而为。

    任何一层抛异常都在这里被吃掉：埋点失败绝不允许改变业务结果，也绝不允许
    把异常带给调用方。``None`` 字段被丢掉（调用方约定用 ``PKG3_UNKNOWN``
    表示"没有"，所以 ``None`` 只是"没传"）。
    """
    payload = {key: value for key, value in fields.items() if value is not None}
    _deliver_pkg3(event, payload)
    if _pkg3_trace_record is None:
        return
    priority = _PKG3_PRIORITY_STATE if _priority is None else _priority
    try:
        if priority is None:
            _pkg3_trace_record(event, **payload)
        else:
            _pkg3_trace_record(event, priority=priority, **payload)
    except TypeError:
        # 包 1 还没接受 `priority` 形参时不放弃这条埋点。
        try:
            _pkg3_trace_record(event, **payload)
        except Exception:
            pass
    except Exception:
        pass


class _Pkg3Span:
    """Adapter that enters the real context manager and mirrors it in-process."""

    def __init__(self, event, fields, manager=None):
        self._event = event
        self._fields = dict(fields)
        self._manager = manager
        self._handle = None
        self._real_entered = False

    def set(self, **fields):
        self._fields.update(fields)
        setter = getattr(self._handle, 'set', None)
        if callable(setter):
            try:
                setter(**fields)
            except Exception:
                pass

    def __enter__(self):
        if self._manager is not None:
            try:
                self._handle = self._manager.__enter__()
                self._real_entered = True
            except Exception:
                self._manager = None
                self._handle = None
        return self

    def __exit__(self, *exc_info):
        suppressed = False
        if self._real_entered:
            try:
                suppressed = bool(self._manager.__exit__(*exc_info))
            except Exception:
                # Instrumentation must not replace or hide a business exception.
                suppressed = False
            # The real span is the sole trace lifecycle. Keep the historical
            # in-process observer contract without writing a same-name duplicate.
            _deliver_pkg3(self._event, self._fields)
        else:
            # No span implementation: retain one best-effort standalone record.
            record_pkg3(self._event, **self._fields)
        return suppressed


def pkg3_span(event: str, **fields):
    """一个"永远能进去"的 span：包 1 就绪时是真 span，否则退化成一条记录。

    ``.set()`` 在两种情况下都是同一个意思；真实 span 是 trace 中唯一的生命
    周期，进程内观察者仍收到一次最终字段快照，不再追加同名伪终止事件。
    """
    manager = None
    if _pkg3_trace_span is not None:
        try:
            manager = _pkg3_trace_span(event, **fields)
        except Exception:
            manager = None
    return _Pkg3Span(event, fields, manager)


def pkg3_capture():
    """异步任务**创建处**的 trace 上下文快照（没有就返回 ``None``）。"""
    if _pkg3_capture_context is None:
        return None
    try:
        return _pkg3_capture_context()
    except Exception:
        return None


@contextmanager
def pkg3_restore(context):
    """在 worker 工作区间恢复创建处上下文，并在退出时可靠还原。"""
    if context is None or _pkg3_restore_context is None:
        yield False
        return
    try:
        manager = _pkg3_restore_context(context)
        manager.__enter__()
    except Exception:
        yield False
        return
    try:
        yield True
    except BaseException as error:
        try:
            suppressed = bool(manager.__exit__(type(error), error,
                                               error.__traceback__))
        except Exception:
            suppressed = False
        if not suppressed:
            raise
    else:
        try:
            manager.__exit__(None, None, None)
        except Exception:
            pass


def pkg3_trace_ids():
    """当前活跃的 trace/span 标识；关联不上就没有这个字段，不编造。"""
    if _pkg3_current_context is None:
        return {}
    try:
        context = _pkg3_current_context()
    except Exception:
        return {}
    if context is None:
        return {}
    if isinstance(context, dict):
        source = context
    else:
        source = {name: getattr(context, name, None)
                  for name in ('trace_id', 'span_id', 'parent_span_id',
                               'thread_id')}
    return {name: source[name] for name in
            ('trace_id', 'span_id', 'parent_span_id', 'thread_id')
            if source.get(name) is not None}


def stable_fingerprint(values, *, prefix: str = '') -> str:
    """一组标量的稳定短指纹（16 个 hex 字符）。

    排序后再散列，所以 ``{'a','b'}`` 与 ``{'b','a'}`` 是同一个指纹；**只用于
    比较两个范围是否相同**，不用于反查内容。
    """
    try:
        items = sorted(str(value) for value in values)
    except TypeError:
        return 'unhashable'
    joined = '|'.join([prefix] + items)
    return hashlib.blake2b(joined.encode('utf-8', 'replace'),
                           digest_size=8).hexdigest()


def page_scope_fields(scope_pages, *, sample_limit: int = SCOPE_SAMPLE_LIMIT,
                      prefix: str = 'scope_pages'):
    """页面范围的字段字典：已知性、数量、有限样本、稳定指纹。

    ``None`` 与空集合严格区分（见模块头注释）。
    """
    if scope_pages is None:
        return {prefix + '_known': False, prefix + '_count': PKG3_UNKNOWN,
                prefix + '_sampled': False, prefix + '_sample': [],
                prefix + '_fingerprint': 'none'}
    try:
        ids = sorted(str(value) for value in scope_pages)
    except TypeError:
        return {prefix + '_known': True, prefix + '_count': PKG3_UNKNOWN,
                prefix + '_sampled': False, prefix + '_sample': [],
                prefix + '_fingerprint': 'unhashable'}
    return {prefix + '_known': True, prefix + '_count': len(ids),
            prefix + '_sampled': len(ids) > sample_limit,
            prefix + '_sample': ids[:sample_limit],
            prefix + '_fingerprint': stable_fingerprint(ids)}


def classify_scope_mode(mode='all', scope_pages=None, *, search_text='',
                        search_kind='all', saved_rule=None,
                        default_canvas_ids=DEFAULT_CANVAS_IDS):
    """把"用户点了什么"翻译成**一个**有效范围口径，并说明为什么。

    六个口径互不合并：全部素材 / 未分类 / 未标签 / 默认画布 / 普通页 / 搜索。
    判定顺序即优先级，``reason`` 把赢的那一条写进快照，所以"这个范围为什么
    不是另一个范围"永远可以被复查。
    """
    if mode == 'unclassified':
        return 'unclassified', 'mode_unclassified'
    if mode == 'untagged':
        return 'untagged', 'mode_untagged'
    text = str(search_text or '').strip()
    if text or (search_kind not in ('all', 'asset', None)):
        return 'search', ('search_text' if text else 'search_kind')
    if saved_rule:
        return 'search', 'smart_folder_rule'
    if scope_pages is None:
        return 'all_assets', 'no_page_scope'
    try:
        pages = {str(value) for value in scope_pages}
    except TypeError:
        return 'page', 'unreadable_page_scope'
    if not pages:
        # 明确的空范围不是"全部"。合流会把两个 bug 藏起来。
        return 'empty_scope', 'explicit_empty_page_scope'
    if pages <= set(default_canvas_ids):
        return 'default_canvas', 'default_canvas_only'
    return 'page', 'page_scope'


def build_scope_snapshot(scope_pages=None, **overrides) -> dict:
    """范围快照（规格 §6）。字段齐全，缺的就是 ``PKG3_UNKNOWN``。

    这个函数**不发事件也不查数据**，只是把调用方给的字段补齐成一份字典：
    缺一个字段就显式写成 ``'unknown'``，而不是悄悄消失 —— 缺的字段和值为
    0 的字段在日志里必须看得出区别。
    """
    snapshot = dict(
        stage=PKG3_UNKNOWN,
        project=PKG3_UNKNOWN,
        requested_mode=PKG3_UNKNOWN,
        effective_mode=PKG3_UNKNOWN,
        effective_mode_reason=PKG3_UNKNOWN,
        search_active=False,
        search_kind=PKG3_UNKNOWN,
        default_canvas_selected=False,
        expected_total=PKG3_UNKNOWN,
        expected_total_known=False,
        catalog_state=PKG3_UNKNOWN,
        catalog_state_reason=PKG3_UNKNOWN,
        source_fingerprint=PKG3_UNKNOWN,
        cache_fingerprint=PKG3_UNKNOWN,
        cache_revision=PKG3_UNKNOWN,
        fallback_reason=PKG3_UNKNOWN,
        disk_candidates=PKG3_UNKNOWN,
        overlay_loaded=PKG3_UNKNOWN,
        overlay_parked=PKG3_UNKNOWN,
        overlay_new=PKG3_UNKNOWN,
        overlay_deleted=PKG3_UNKNOWN,
        overlay_dirty=PKG3_UNKNOWN,
        deduped_count=PKG3_UNKNOWN,
        filter_hits=PKG3_UNKNOWN,
        logical_result_count=PKG3_UNKNOWN,
        model_row_count=PKG3_UNKNOWN,
        installed_card_count=PKG3_UNKNOWN,
        visible_card_count=PKG3_UNKNOWN,
        current_page_kind=PKG3_UNKNOWN,
        current_page_id=PKG3_UNKNOWN,
        resource_tree_selected_id=PKG3_UNKNOWN,
        stale_result_discarded=False,
        stale_result_reason=PKG3_UNKNOWN)
    snapshot.update(page_scope_fields(scope_pages))
    snapshot.update(overrides)
    return snapshot


def build_range_snapshot(canvas_scope=None, **overrides) -> dict:
    """范围读取快照（规格 §6 的读取侧）。

    同一个约定：**只描述已经发生的读取**。``expected_total_known`` 为 False
    时 ``expected_total`` 一定是 ``PKG3_UNKNOWN`` —— "不知道总数"不能写成 0。
    """
    snapshot = dict(
        stage=PKG3_UNKNOWN,
        project=PKG3_UNKNOWN,
        effective_mode=PKG3_UNKNOWN,
        effective_mode_reason=PKG3_UNKNOWN,
        filter_label=PKG3_UNKNOWN,
        filters=PKG3_UNKNOWN,
        page_size=PKG3_UNKNOWN,
        pages_read=PKG3_UNKNOWN,
        rows_read=PKG3_UNKNOWN,
        rows_returned=PKG3_UNKNOWN,
        delivered=PKG3_UNKNOWN,
        complete=PKG3_UNKNOWN,
        cancelled=PKG3_UNKNOWN,
        truncated=PKG3_UNKNOWN,
        limit_respected=PKG3_UNKNOWN,
        known_canvas_forwarded=PKG3_UNKNOWN,
        known_canvases=PKG3_UNKNOWN,
        expected_total=PKG3_UNKNOWN,
        expected_total_known=False,
        catalog_state=PKG3_UNKNOWN,
        source_fingerprint=PKG3_UNKNOWN,
        cache_fingerprint=PKG3_UNKNOWN,
        cache_revision=PKG3_UNKNOWN,
        read_reason=PKG3_UNKNOWN)
    snapshot.update(page_scope_fields(canvas_scope, prefix='canvas_scope'))
    snapshot.update(overrides)
    return snapshot


class PaintRollup:
    """呈现 marker 的每帧累积 / 少量阈值事件（规格 §8）。

    **不逐图写日志。** 一帧里发生的每一件事只是把一个整数加进计数器；真正
    发出的事件只有三种：:meth:`flush` 到期（默认每秒一次）、阈值事件（内容
    首绘、清晰度翻转、相机代数变化）、调用方显式 flush。于是"一帧画了多少
    张图"是一个计数，而不是两千条 JSONL。
    """

    def __init__(self, *, interval=1.0):
        self.interval = float(interval)
        self._frames = 0
        self._content_paints = 0
        self._text_paints = 0
        self._cards = 0
        self._quality_samples = 0
        self._quality_ready = 0
        self._quality_unknown = 0
        self._first_paint_ms = None
        self._flushes = 0
        self._last_flush = _pkg3_time.perf_counter()

    def count_frame(self, *, content, cards, quality=None, seconds=None):
        """记一帧。``quality`` 只做计数，不在这里下清晰度结论。"""
        self._frames += 1
        if content:
            self._content_paints += 1
        else:
            self._text_paints += 1
        self._cards += int(cards or 0)
        if quality == 'ready':
            self._quality_samples += 1
            self._quality_ready += 1
        elif quality == 'unknown':
            self._quality_samples += 1
            self._quality_unknown += 1
        if seconds is not None and self._first_paint_ms is None:
            self._first_paint_ms = round(float(seconds) * 1000.0, 3)

    def due(self) -> bool:
        """距上次汇总是否已到期（是否 flush 由调用方决定）。"""
        return _pkg3_time.perf_counter() - self._last_flush >= self.interval

    def take(self) -> dict:
        """取走并清零这一段累积的计数。"""
        now = _pkg3_time.perf_counter()
        snapshot = dict(frames=self._frames, cards=self._cards,
                        content_paints=self._content_paints,
                        text_paints=self._text_paints,
                        quality_samples=self._quality_samples,
                        quality_ready_samples=self._quality_ready,
                        quality_unknown_samples=self._quality_unknown,
                        first_paint_ms=self._first_paint_ms,
                        flushes=self._flushes,
                        window_ms=round((now - self._last_flush) * 1000.0, 1))
        self._last_flush = now
        self._frames = self._content_paints = self._text_paints = 0
        self._cards = self._quality_samples = 0
        self._quality_ready = self._quality_unknown = 0
        self._first_paint_ms = None
        self._flushes += 1
        return snapshot

    def flush(self, **fields):
        """把这一段的计数发成一条事件（到期时或阈值事件时）。"""
        payload = self.take()
        if not payload['frames'] and not fields:
            return payload
        record_pkg3(FRAME_EVENT, **dict(payload, **fields))
        return payload


def sample_stack(limit: int = 12, *, thread_id=None):
    """当前栈的有限样本 + 截断标志。

    ⚠️ **两条不可省略的限定**（同样写在 pkg3-REPORT 里）：

    * 采样落在 ``app.exec`` / ``processEvents`` **不是**"某个具体业务函数慢"
      的证据 —— 事件循环返回时才被采到，只能说明"此刻在这里"。
    * **有栈只证明被采到了**，不证明这段时间被栈上那个函数占着。归类要靠
      时长、计数等旁证；旁证不足就标 ``unclassified``。
    """
    import sys as _pkg3_sys
    frame = (_pkg3_sys._getframe(1) if thread_id is None else
             _pkg3_sys._current_frames().get(thread_id))
    sample = []
    depth = 0
    truncated = False
    while frame is not None:
        code = frame.f_code
        name = code.co_filename.rsplit('\\', 1)[-1].rsplit('/', 1)[-1]
        sample.append('%s:%s:%s' % (name, code.co_name, frame.f_lineno))
        depth += 1
        if depth > limit:
            truncated = True
            break
        frame = frame.f_back
    return dict(stack_sample=sample, stack_depth=depth,
                stack_truncated=truncated)


def _dispatch_window_evidence(intervals, window_start, window_end):
    """Return immutable overlap facts for dispatches in one frozen stall window."""
    overlaps = []
    for raw in intervals or ():
        try:
            started = float(raw['start_monotonic'])
            ended = float(raw['end_monotonic'])
        except (KeyError, TypeError, ValueError):
            continue
        if ended < window_start or started > window_end or ended < started:
            continue
        overlap_start = max(started, window_start)
        overlap_end = min(ended, window_end)
        item = dict(raw)
        item['overlap_ms'] = round(max(0.0, overlap_end-overlap_start)*1000, 3)
        item['covers_window'] = started <= window_start and ended >= window_end
        window_ms = max(0.0, (window_end-window_start)*1000)
        item['overlap_ratio'] = (item['overlap_ms']/window_ms if window_ms else 0.0)
        item['leading_gap_ms'] = round(max(0.0, started-window_start)*1000, 3)
        item['trailing_gap_ms'] = round(max(0.0, window_end-ended)*1000, 3)
        # A heartbeat samples on a 50ms grid. Preserve exact full coverage,
        # but separately expose a bounded near-cover; never relabel it full.
        item['near_covers_window'] = (not item['covers_window']
            and item['overlap_ratio'] >= .9
            and item['leading_gap_ms'] <= 50.001
            and item['trailing_gap_ms'] <= 50.001)
        overlaps.append(item)
    overlaps.sort(key=lambda item: (item['start_monotonic'], item.get('depth', 0),
                                    str(item.get('dispatch_id', ''))))
    return overlaps


def classify_stall(*, duration_ms, dispatch_span_ms=None,
                   dispatches_completed=None, modal=False,
                   nested_loop=False, background_busy=False,
                   heartbeat_delay_ms=None, covering_dispatches=None,
                   dominant_dispatches=None):
    """Classify only from evidence frozen inside the original stall window."""
    covering = list(covering_dispatches or ())
    if modal:
        return 'modal_nested_wait', 'modal_dialog_open'
    if nested_loop or any(item.get('nested_loop') for item in covering):
        return 'modal_nested_wait', 'nested_event_loop_covered_window'
    if covering:
        if heartbeat_delay_ms is not None:
            reason = ('heartbeat_delayed_by_one_dispatch' if len(covering) == 1
                      else 'heartbeat_delayed_by_nested_dispatch_chain')
            return 'heartbeat_starved', reason
        if len(covering) == 1:
            return 'single_dispatch_long', 'one_dispatch_covered_frozen_window'
        return 'unclassified', 'multiple_dispatch_layers_covered_window'
    if dominant_dispatches and heartbeat_delay_ms is not None:
        return 'heartbeat_starved', 'dispatch_near_covered_window_with_bounded_heartbeat_grid_gap'
    # Compatibility for callers that only have the old scalar evidence. New
    # callers always pass intervals and therefore never use recovery-time age.
    if (dispatch_span_ms is not None
            and float(dispatch_span_ms) >= float(duration_ms)):
        return 'single_dispatch_long', 'legacy_dispatch_span_filled_window'
    if background_busy:
        return 'unclassified', 'background_activity_observed_not_causal'
    return 'unclassified', 'insufficient_frozen_window_evidence'


def build_stall_context(*, duration_ms, stage, stall_id=None,
                        window_start_monotonic=None, window_end_monotonic=None,
                        dispatch_intervals=None, dispatch_span_ms=None,
                        dispatches_completed=None, heartbeat_delay_ms=None,
                        timer_starved=None, sample_count=None, modal=None,
                        nested_loop=None, background_busy=None, project=None,
                        mode=None, effective_mode=None, scope_fingerprint=None,
                        stack_limit=12, stack_thread_id=None):
    """Build one stall context from frozen window evidence when available."""
    stack = sample_stack(stack_limit, thread_id=stack_thread_id)
    evidence = []
    covering = []
    dominant = []
    if window_start_monotonic is not None and window_end_monotonic is not None:
        evidence = _dispatch_window_evidence(
            dispatch_intervals, float(window_start_monotonic),
            float(window_end_monotonic))
        covering = [item for item in evidence if item['covers_window']]
        dominant = [item for item in evidence if item['near_covers_window']]
        if evidence:
            dispatch_span_ms = max(item['overlap_ms'] for item in evidence)
        dispatches_completed = sum(
            float(item['end_monotonic']) <= float(window_end_monotonic)
            for item in evidence)
    classification, reason = classify_stall(
        duration_ms=duration_ms, dispatch_span_ms=dispatch_span_ms,
        dispatches_completed=dispatches_completed, modal=bool(modal),
        nested_loop=bool(nested_loop), background_busy=bool(background_busy),
        heartbeat_delay_ms=heartbeat_delay_ms,
        covering_dispatches=covering, dominant_dispatches=dominant)
    fields = dict(
        stall_id=PKG3_UNKNOWN if stall_id is None else stall_id,
        stage=stage,
        duration_ms=round(float(duration_ms), 3),
        window_start_monotonic=(PKG3_UNKNOWN if window_start_monotonic is None
                                else float(window_start_monotonic)),
        window_end_monotonic=(PKG3_UNKNOWN if window_end_monotonic is None
                              else float(window_end_monotonic)),
        dispatch_evidence_version=2,
        dispatch_span_ms=(PKG3_UNKNOWN if dispatch_span_ms is None
                          else round(float(dispatch_span_ms), 3)),
        dispatches_completed=(PKG3_UNKNOWN if dispatches_completed is None
                              else dispatches_completed),
        dispatch_intervals=evidence,
        covering_dispatches=covering,
        dominant_dispatches=dominant,
        coverage_rule='exact_or_ratio_ge_0.9_with_each_gap_le_50ms',
        classification_is_root_cause=False,
        heartbeat_delay_ms=(PKG3_UNKNOWN if heartbeat_delay_ms is None
                            else round(float(heartbeat_delay_ms), 3)),
        heartbeat_source='performance_trace_sampler',
        timer_starved=(PKG3_UNKNOWN if timer_starved is None
                       else bool(timer_starved)),
        sample_count=PKG3_UNKNOWN if sample_count is None else sample_count,
        stack_depth=stack['stack_depth'],
        stack_truncated=stack['stack_truncated'],
        stack_is_sampled_not_blame=True,
        stack_caveat=('a sample inside app.exec/processEvents is not evidence '
                      'of a specific business function; a stack only proves the '
                      'sample landed here'),
        nested_loop=PKG3_UNKNOWN if nested_loop is None else bool(nested_loop),
        modal=PKG3_UNKNOWN if modal is None else bool(modal),
        background_busy=(PKG3_UNKNOWN if background_busy is None
                         else bool(background_busy)),
        background_is_causal_evidence=False,
        project=PKG3_UNKNOWN if project is None else project,
        mode=PKG3_UNKNOWN if mode is None else mode,
        effective_mode=PKG3_UNKNOWN if effective_mode is None else effective_mode,
        scope_fingerprint=(PKG3_UNKNOWN if scope_fingerprint is None
                           else scope_fingerprint),
        classification=classification,
        classification_reason=reason)
    fields.update(pkg3_trace_ids())
    fields['stack_sample'] = stack['stack_sample']
    fields['stack_thread_id'] = stack_thread_id
    fields['stack_capture_scope'] = ('calling_thread' if stack_thread_id is None
                                     else 'requested_thread_current_python_stack')
    fields['native_stack_collected'] = False
    return fields


def record_stall(**fields):
    """发一条与 ``gui_stall`` 结束记录同保留级别的上下文事件。"""
    record_pkg3(STALL_EVENT, _priority=_PKG3_PRIORITY_TERMINAL, **fields)
