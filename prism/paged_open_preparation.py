"""Off-GUI scope selection and immutable baseline preparation for scoped opens."""
from contextvars import ContextVar
from functools import lru_cache
import json
import logging
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid

from PyQt6 import QtCore

from prism import page_selection
from prism.paged_canvas_loader import _Signals, _deliver
from prism.performance_trace import record as trace_record
from prism.performance_trace import capture_context, restore_context, span
from prism.performance_trace import record_catalog_build_progress
from prism.performance_trace import record_scope_prepared


logger = logging.getLogger(__name__)


# Propagate the worker token through transparent wrappers without changing the
# generic task callable contract. ContextVar isolates simultaneous workers.
_task_cancel = ContextVar('paged_open_cancel', default=None)
# The progress sink rides the same channel instead of becoming a keyword
# argument, so the callable contract of a stage stays exactly what it was.
_task_progress = ContextVar('paged_open_progress', default=None)

#: How many pixmaps the scope opens at once. One bounded page.
PAGE_LIMIT = 256

#: The catalog's own ceiling for one read batch. Paging a kind uses this
#: size, so a kind with more rows than this is read in several rounds.
MAX_BATCH = 1024

#: ``prism/fileio/sql.py`` binds the scope id list three times in its scoped
#: branch (once per UNION arm of ``native_item_metadata``), so a reader that
#: does not chunk needs three bound parameters per scope id.
SCOPED_READER_BINDINGS_PER_ID = 3


def _catalog_build_progress(**fields):
    """Compatibility wrapper around the shared C-group progress protocol."""
    record_catalog_build_progress(_record=trace_record, **fields)


def _scope_prepared(**fields):
    """Compatibility wrapper around the shared C-group scope protocol."""
    record_scope_prepared(_record=trace_record, **fields)


def source_stamp(filename):
    """Cheap identity/content-change guard, including SQLite WAL writes."""
    path = Path(filename).resolve(strict=True)
    def stat(name):
        try:
            value = os.stat(name)
        except FileNotFoundError:
            return None
        return (value.st_dev, value.st_ino, value.st_size,
                value.st_mtime_ns, value.st_ctime_ns)
    return str(path), stat(path), stat(str(path) + '-wal')


def require_stamp(filename, expected):
    if source_stamp(filename) != expected:
        raise RuntimeError('Source project changed while its opening baseline was prepared; reopen it')


class PreparationCancelled(RuntimeError):
    """The user stopped preparation.

    This is not a project defect, so the view must not report it as a failed
    open and must not leave a half-opened project behind.
    """


def resolve_scope(filename, cache, last_canvas, current_canvas,
                  cancel=None, progress=None):
    """Return small scope state; never touch the view/settings from this worker.

    ``progress`` remains accepted for compatibility, but scope selection does
    not build a catalog or publish catalog-build progress. A not-ready scope
    returns ``pending`` for the owner's independent background build.

    The scope covers the whole target canvas: one page of pixmaps plus
    **every** non-pixmap entity on that canvas (text, path, video, glb and
    whatever else the catalog holds for it). A pixmap-only scope never reads
    those rows back, and the canvas then looks like it lost data — the files
    are still in the project, but the user cannot see them.
    """
    from prism import fileio
    from prism.project_open_scope import ProjectOpenScope
    open_id = uuid.uuid4().hex[:16]
    if progress is None:
        progress = _task_progress.get()
    if not fileio.is_prism_file(filename):
        trace_record('project.open.scope_selected', scope_kind='full',
                     selection_reason='non_prism_compatibility', target_canvas=None,
                     requested_ids_count=None, total_known=False)
        return dict(scope=None, ids=None, target=None, pending=None, stamp=None)
    stamp = source_stamp(filename)
    scope = ProjectOpenScope(filename, cache)
    _check_preparation_cancel(cancel)
    if not scope.ready:
        state = scope.telemetry_state()
        pending = dict(project=str(scope.project_path), cache=str(scope.cache_path),
                       reason=state.get('catalog_state_reason', 'catalog_not_ready'))
        target, ids, pixmaps, non_pixmaps, kinds, nonempty = _source_first_scope(
            filename, stamp, cancel)
        reason = ('first_legal_canvas_source' if target is not None else
                  'no_legal_canvas_nonempty_source_compatibility' if nonempty else
                  'no_legal_canvas_empty_source_compatibility')
        if target is None and nonempty:
            logger.warning('No legal canvas in nonempty source; retaining full-reader compatibility: %s',
                           filename)
        trace_record('project.open.scope_selected', scope_kind='full' if ids is None else 'ids',
                     selection_reason=reason, target_canvas=target,
                     requested_ids_count=None if ids is None else len(ids),
                     total_records=None, total_known=False, catalog_ready=False,
                     catalog_state=state.get('catalog_state', 'unknown'))
        if ids is not None:
            _scope_prepared(open_id=open_id, canvas=target, scope_ids=len(ids),
                            pixmap_count=pixmaps, non_pixmap_count=non_pixmaps,
                            kinds=kinds, catalog_ready=False)
            _report_scope_binding_budget(len(ids))
        require_stamp(filename, stamp)
        return dict(scope=scope, ids=ids, target=target, pending=pending, stamp=stamp)
    # This scope is returned unchanged to the GUI. Populate its existing
    # tag-set memo on this preparation worker, before sidebar recounts.
    from prism.project_catalog_index import CatalogBuildCancelled
    try:
        scope.index.tag_groups(cancel=cancel)
    except CatalogBuildCancelled:
        raise PreparationCancelled('Scope preparation cancelled') from None
    _check_preparation_cancel(cancel)
    counts = scope.counts()
    if counts is None:
        raise RuntimeError('Project catalog became unavailable during scope preparation')
    # The landing canvas is chosen by ONE rule, shared with the interface
    # (``prism/page_selection.py``).  It used to be "last opened, else the
    # current canvas, else the canvas holding the most assets", so the page
    # the background loaded and the page the interface showed were two
    # different pages.  Both sides now take the first LEGAL canvas in project
    # order -- an empty stored canvas counts, a soft-deleted one does not, and
    # the legacy implicit default canvas is only used when the project still
    # carries content for it.
    #
    # ``last_canvas`` / ``current_canvas`` are deliberately NOT consulted for
    # the target any more.  They stay in the signature so the callers do not
    # change, but "resume where I was" is a navigation preference, not the
    # rule for which page a project opens on.
    canvas_counts = {}
    for (canvas, kind), count in counts.items():
        canvas_counts[canvas] = canvas_counts.get(canvas, 0) + count
    stored, deleted = _read_source_workspace(filename, stamp, cancel)
    implicit_legal = page_selection.implicit_default_has_content(
        catalog_known=True, catalog_canvases=canvas_counts)
    target = page_selection.first_legal_canvas(stored, deleted, implicit_legal)
    if target is None:
        # No legal canvas at all: open everything and let the interface show
        # the normal empty state.  Do not invent a page for it.
        trace_record('project.open.scope_selected', scope_kind='full',
                     selection_reason='no_legal_canvas', target_canvas=None,
                     requested_ids_count=None, total_known=True,
                     total_records=sum(counts.values()))
        _check_preparation_cancel(cancel)
        require_stamp(filename, stamp)
        return dict(scope=scope, ids=None, target=None, pending=None, stamp=stamp)
    # The kinds this canvas really holds. A kind the catalog never saw cannot
    # be on the canvas either, so it is not worth a query; the kind list is
    # read from the catalog instead of being hardcoded here.
    kinds = sorted({kind for (canvas, kind) in counts
                    if canvas == target and kind != 'pixmap'})
    ids = _scope_page_ids(scope, target, 'pixmap', PAGE_LIMIT, cancel)
    pixmap_count = len(ids)
    non_pixmap = 0
    for kind in kinds:
        collected = _scope_kind_ids(scope, target, kind, cancel)
        non_pixmap += len(collected)
        ids.extend(collected)
    # Downstream keyset paging feeds the last id of a page back as
    # ``after_id``, so the scope has to be one ascending run.
    ids = tuple(sorted(set(ids)))
    trace_record('project.open.scope_selected', scope_kind='ids',
                 selection_reason='first_legal_canvas', target_canvas=str(target),
                 requested_ids_count=len(ids), total_known=True,
                 total_records=sum(counts.values()), pixmap_count=pixmap_count,
                 non_pixmap_count=non_pixmap, catalog_ready=True)
    # ``processed`` counts what this open read as a page: one bounded page of
    # pixmaps (<= PAGE_LIMIT). The non-pixmap entities were enumerated from
    # the catalog index, not read out of the project, so they are reported on
    # their own fields (``non_pixmap_count`` / ``scope_ids``) instead of
    # being folded into the page counter. ``total_records`` is the number of
    # entities this scope covers; it is known.
    _scope_prepared(
        open_id=open_id, canvas=str(target), scope_ids=len(ids),
        pixmap_count=pixmap_count, non_pixmap_count=non_pixmap,
        kinds=kinds)
    _report_scope_binding_budget(len(ids))
    require_stamp(filename, stamp)
    return dict(scope=scope, ids=ids, target=target, pending=None, stamp=stamp)


def _check_preparation_cancel(cancel):
    if cancel is not None and cancel():
        raise PreparationCancelled('Opening was cancelled')


def _source_workspace(connection, cancel):
    """Only absent optional tables mean legacy; damaged schemas/JSON fail."""
    def exists(name):
        return connection.execute(
            'SELECT 1 FROM sqlite_master WHERE name=? AND type IN (?,?)',
            (name, 'table', 'view')).fetchone() is not None

    stored, deleted = [], set()
    if exists('workspace_pages'):
        cursor = connection.execute(
            'SELECT id,kind FROM workspace_pages ORDER BY sort_order,rowid')
        try:
            while True:
                _check_preparation_cancel(cancel)
                rows = cursor.fetchmany(MAX_BATCH)
                if not rows:
                    break
                stored.extend(page_selection.stored_canvas_ids(
                    [{'id': key, 'kind': kind} for key, kind in rows]))
        finally:
            cursor.close()
    if exists('workspace_nodes'):
        cursor = connection.execute('SELECT payload FROM workspace_nodes')
        try:
            while True:
                _check_preparation_cancel(cancel)
                rows = cursor.fetchmany(MAX_BATCH)
                if not rows:
                    break
                for (payload,) in rows:
                    _check_preparation_cancel(cancel)
                    node = json.loads(payload)
                    if not isinstance(node, dict):
                        raise ValueError('Workspace node metadata is not an object')
                    deleted.update(page_selection.deleted_page_ids([node]))
        finally:
            cursor.close()
    return stored, deleted


def _read_source_workspace(filename, stamp, cancel):
    """The ready path reuses catalog pages, but still reads legal page state."""
    _check_preparation_cancel(cancel)
    require_stamp(filename, stamp)
    connection = sqlite3.connect(Path(filename).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=.25)
    try:
        connection.set_progress_handler(lambda: int(cancel is not None and cancel()), 1000)
        connection.execute('BEGIN')
        result = _source_workspace(connection, cancel)
        _check_preparation_cancel(cancel)
        require_stamp(filename, stamp)
        return result
    except sqlite3.OperationalError as error:
        if (getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                and cancel is not None and cancel()):
            raise PreparationCancelled('Opening was cancelled') from None
        raise
    finally:
        try:
            connection.set_progress_handler(None, 0)
            connection.rollback()
        finally:
            connection.close()


def _source_first_scope(filename, stamp, cancel):
    """Read scalar metadata in one read-only snapshot, never build a catalog.

    Batches bound Python memory, NOT source I/O: discovering every non-pixmap
    and the implicit default may scan all item metadata. No archive bytes,
    source writes or new indexes are involved. The projection matches the
    catalog builder: absent canvasId defaults; explicit JSON null stays None;
    deletedAt is intentionally not a filter for page/count semantics.
    """
    _check_preparation_cancel(cancel)
    require_stamp(filename, stamp)
    connection = sqlite3.connect(Path(filename).resolve().as_uri() + '?mode=ro',
                                 uri=True, timeout=.25)
    try:
        connection.set_progress_handler(lambda: int(cancel is not None and cancel()), 1000)
        connection.execute('BEGIN')
        try:
            connection.execute("SELECT json_valid('{}'),json_type('{}'),json_extract('{}','$.canvasId')")
        except sqlite3.OperationalError as error:
            if 'no such function' in str(error).lower():
                raise RuntimeError('Source scope preparation requires SQLite JSON1') from error
            raise
        stored, deleted = _source_workspace(connection, cancel)
        target = page_selection.first_legal_canvas(stored, deleted, False)
        candidate = target if target is not None else page_selection.DEFAULT_CANVAS_ID
        ids, kinds = [], set()
        pixmaps = non_pixmaps = scanned = 0
        implicit = nonempty = False
        after = None
        while True:
            _check_preparation_cancel(cancel)
            where = '' if after is None else 'WHERE id>?'
            parameters = () if after is None else (after,)
            # A falsy payload is {} in the current catalog builder. Invalid
            # nonempty JSON is reported, never coerced to missing metadata.
            cursor = connection.execute('''
                WITH metadata AS (
                    SELECT id,type,CASE WHEN data IS NULL OR data='' OR data=0
                                       THEN '{}' ELSE data END AS payload
                    FROM items %s ORDER BY id LIMIT ?
                )
                SELECT id,type,
                    CASE WHEN json_valid(payload) THEN json_type(payload) ELSE 'invalid' END,
                    CASE WHEN json_valid(payload) THEN json_type(payload,'$.canvasId') END,
                    CASE WHEN json_valid(payload)
                         THEN CAST(json_extract(payload,'$.canvasId') AS TEXT) END
                FROM metadata ORDER BY id
                ''' % where, (*parameters, MAX_BATCH))
            try:
                rows = cursor.fetchmany(MAX_BATCH)
            finally:
                cursor.close()
            if not rows:
                break
            for key, kind, root_type, canvas_type, canvas in rows:
                _check_preparation_cancel(cancel)
                if root_type != 'object':
                    raise ValueError('Item %s metadata is invalid JSON or not an object' % key)
                if canvas_type in ('array', 'object'):
                    raise ValueError('Item %s canvasId is not a scalar' % key)
                if canvas_type is None:
                    canvas = page_selection.DEFAULT_CANVAS_ID
                # The SQL CAST uses assets.canvas_id's TEXT affinity; explicit
                # JSON null stays None, and booleans become SQLite's 0/1 text.
                nonempty = True
                implicit = implicit or canvas == page_selection.DEFAULT_CANVAS_ID
                if canvas != candidate:
                    continue
                if kind == 'pixmap':
                    if pixmaps < PAGE_LIMIT:
                        ids.append(int(key))
                        pixmaps += 1
                else:
                    ids.append(int(key))
                    non_pixmaps += 1
                    kinds.add(kind)
            scanned += len(rows)
            after = rows[-1][0]
        target = page_selection.first_legal_canvas(stored, deleted, implicit)
        _check_preparation_cancel(cancel)
        require_stamp(filename, stamp)
        trace_record('paged_source_scope_scan', metadata_rows=scanned,
                     total_records=None, total_known=False, catalog_ready=False,
                     bounded_io=False)
        if target is None:
            return None, None, 0, 0, [], nonempty
        return target, tuple(ids), pixmaps, non_pixmaps, sorted(kinds), nonempty
    except sqlite3.OperationalError as error:
        if (getattr(error, 'sqlite_errorcode', None) == sqlite3.SQLITE_INTERRUPT
                and cancel is not None and cancel()):
            raise PreparationCancelled('Opening was cancelled') from None
        raise
    finally:
        # An interrupt handler must not interrupt rollback itself.
        try:
            connection.set_progress_handler(None, 0)
            connection.rollback()
        finally:
            connection.close()


def _scope_page(scope, canvas_id, kind, after_id, limit, cancel):
    """One catalog page, with the user's cancel turned into our own signal."""
    from prism.project_open_scope import OpenScopeCancelled
    if cancel is not None and cancel():
        raise PreparationCancelled('Opening was cancelled')
    try:
        return scope.page(canvas_id, kind=kind, after_id=after_id,
                          limit=limit, cancel=cancel)
    except OpenScopeCancelled:
        # A cancelled read is not a failed project: the view must not report
        # it as a broken open.
        raise PreparationCancelled('Opening was cancelled') from None


def _scope_page_ids(scope, canvas_id, kind, limit, cancel):
    """Ids of one bounded page of one kind."""
    return [int(row['id']) for row in
            _scope_page(scope, canvas_id, kind, None, limit, cancel)]


def _scope_kind_ids(scope, canvas_id, kind, cancel):
    """Every id of one kind, read in bounded keyset pages until it is done."""
    ids = []
    after_id = None
    while True:
        rows = _scope_page(scope, canvas_id, kind, after_id, MAX_BATCH, cancel)
        if not rows:
            return ids
        ids.extend(int(row['id']) for row in rows)
        after_id = rows[-1]['id']
        if len(rows) < MAX_BATCH:
            return ids


@lru_cache(maxsize=1)
def _sql_variable_limit():
    """``SQLITE_MAX_VARIABLE_NUMBER`` of the runtime SQLite (999 ... 32766)."""
    try:
        connection = sqlite3.connect(':memory:')
    except sqlite3.Error:
        return 999
    try:
        return int(connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    except (AttributeError, sqlite3.Error):
        return 999
    finally:
        connection.close()


def _report_scope_binding_budget(scope_size):
    """Say out loud when the scope outgrows an unchunked SQL reader.

    The scope is never trimmed here: dropping non-pixmap rows is exactly the
    "the canvas lost data" failure this scope exists to prevent. Instead the
    numbers are logged and traced so the reader side chunks the id list
    instead of binding it in one statement.
    """
    limit = _sql_variable_limit()
    bindings = scope_size * SCOPED_READER_BINDINGS_PER_ID
    if bindings <= limit:
        return
    logger.warning(
        'Paged open scope holds %d ids, which needs %d bound SQL parameters '
        'at %d per id; this runtime allows %d. The scoped reader has to chunk '
        'the id list instead of binding it in one statement.',
        scope_size, bindings, SCOPED_READER_BINDINGS_PER_ID, limit)
    trace_record('paged_prepare_scope_over_sql_limit', scope_ids=scope_size,
                 bindings=bindings, bindings_per_id=SCOPED_READER_BINDINGS_PER_ID,
                 variable_limit=limit)


def prepare_session(filename, loaded, page_ids, stamp, cancel=None):
    """Read all currently required writable bases without blocking GUI.

    The source stamp was captured before SQL loaded the first page. In-memory
    edits during this read remain edits against that same source baseline;
    an external write/replacement invalidates preparation rather than rebasing.
    """
    from prism.partial_project import PartialProjectSession, _check_cancel
    cancel = cancel if cancel is not None else _task_cancel.get()
    _check_cancel(cancel)
    require_stamp(filename, stamp)
    session = PartialProjectSession.open(filename, item_ids=loaded, page_ids=page_ids,
        pages_complete=True, include_boards=True, include_domains=True, cancel=cancel)
    _check_cancel(cancel)
    require_stamp(filename, stamp)
    return session


class _Task(QtCore.QRunnable):
    def __init__(self, epoch, stage, function, args, cancel, signals, progress=None,
                 operation=None):
        super().__init__()
        self.args = epoch, stage, function, args, cancel, signals, progress, operation
        self.trace_context = capture_context()

    def run(self):
        with restore_context(self.trace_context):
            with span('project.prepare', stage=self.args[1]) as observed:
                state = self._run()
                observed.set(result_state=state, failed=state == 'error')

    def _run(self):
        epoch, stage, function, args, cancel, signals, progress, operation = self.args
        if cancel.is_set() and operation is None:
            return 'cancelled_before_start'
        started = time.perf_counter()
        token = _task_cancel.set(cancel.is_set)
        progress_token = _task_progress.set(progress)
        try:
            _check_preparation_cancel(cancel.is_set)
            # The call signature of a stage is unchanged: 'scope' receives the
            # cancel probe, every other stage is called as it always was.
            result = function(*args, cancel.is_set) if stage == 'scope' else function(*args)
            error = None
        except Exception as caught:
            result, error = None, caught
        finally:
            _task_progress.reset(progress_token)
            _task_cancel.reset(token)
        if cancel.is_set():
            result, error = None, PreparationCancelled('Opening was cancelled')
        # Keep destroyed-QObject protection, but do not let the page loader's
        # cancellation filter swallow the lifecycle's terminal notification.
        delivery_cancel = cancel if operation is None else threading.Event()
        _deliver(signals, (epoch, stage, result, error,
                          (time.perf_counter() - started) * 1000, operation),
                 delivery_cancel)
        return ('cancelled' if isinstance(error, PreparationCancelled) else
                'error' if error is not None else 'completed')


class PagedOpenPreparation(QtCore.QObject):
    completed = QtCore.pyqtSignal(str, object, object, float)
    operation_completed = QtCore.pyqtSignal(object, str, object, object, float)
    #: Legacy progress signal; source scope selection never builds a catalog.
    progress = QtCore.pyqtSignal(int)

    def __init__(self, view):
        super().__init__(view)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.signals = _Signals(self)
        self.signals.result.connect(self._received)
        self.epoch = 0
        self.cancel_event = threading.Event()
        self.owner_gone = False
        self.trace_context = None
        view.destroyed.connect(self._destroyed)

    def cancel(self):
        # Keep this epoch deliverable so a running operation receives its
        # cancellation terminal. No GUI join and no scene writes are involved.
        # Queued tasks also run their cheap cancelled-before-start terminal.
        self.cancel_event.set()

    def _destroyed(self, *_):
        self.owner_gone = True
        self.cancel()

    def start(self, stage, function, *args, progress=None, operation=None):
        if self.owner_gone:
            return
        self.cancel()
        self.epoch += 1
        self.cancel_event = threading.Event()
        self.trace_context = capture_context()
        self.pool.start(_Task(self.epoch, stage, function, args,
                             self.cancel_event, self.signals, progress, operation))

    @QtCore.pyqtSlot(object)
    def _received(self, result):
        epoch, stage, value, error, duration, operation = result
        if self.owner_gone or epoch != self.epoch:
            return
        if self.cancel_event.is_set():
            if operation is None:
                return
            value, error = None, PreparationCancelled('Opening was cancelled')
        with restore_context(self.trace_context):
            with span('project.prepare.install', stage=stage,
                      failed=error is not None):
                if operation is not None:
                    self.operation_completed.emit(operation, stage, value, error, duration)
                # A lifecycle handler may synchronously start another epoch.
                if (not self.owner_gone and epoch == self.epoch
                        and not self.cancel_event.is_set()):
                    self.completed.emit(stage, value, error, duration)
