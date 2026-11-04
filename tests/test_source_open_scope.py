"""Source-only homepage selection, without a ready catalog or source writes."""
import json
import sqlite3
import threading

import pytest
from PyQt6 import QtCore

from prism import paged_open_preparation as preparation
from prism.project_open_scope import ProjectOpenScope
from tests.test_partial_project_gui import SCHEMA


@pytest.fixture
def source(tmp_path):
    path = tmp_path / 'source with space #.prism'
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'source'))
    db.close()
    return path


def populate(source, items=(), pages=(), nodes=()):
    with sqlite3.connect(source) as db:
        db.executemany('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       [(key, kind, key, '{}', order) for key, kind, order in pages])
        db.executemany('INSERT INTO workspace_nodes VALUES(?,?)',
                       [(str(i), json.dumps(node)) for i, node in enumerate(nodes)])
        db.executemany('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       [(key, kind, 0, 0, 0, 1, 0, 1, json.dumps(extra))
                        for key, kind, extra in items])
    db.close()


def forbid_build(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('source scope must not build')
    monkeypatch.setattr(ProjectOpenScope, 'build', forbidden)


def resolve(source, cache=None, cancel=None):
    return preparation.resolve_scope(str(source), str(cache or source.with_suffix('.sqlite')),
                                     'ignored-last', 'ignored-current', cancel=cancel)


@pytest.fixture
def audit(source, monkeypatch):
    """Audit only the source URI; bounded fetches and cleanup are assertable."""
    original = sqlite3.connect
    connections, statements = [], []
    uri = source.resolve().as_uri() + '?mode=ro'

    class Cursor(sqlite3.Cursor):
        def fetchall(self):
            raise AssertionError('source preparation must not fetchall')

        def fetchmany(self, size=None):
            assert size is not None and size <= preparation.MAX_BATCH
            return super().fetchmany(size)

    class Connection(sqlite3.Connection):
        closed = rolled_back = False

        def execute(self, sql, parameters=()):
            statements.append((sql, parameters))
            return self.cursor(factory=Cursor).execute(sql, parameters)

        def rollback(self):
            self.rolled_back = True
            return super().rollback()

        def close(self):
            self.closed = True
            return super().close()

    def connect(database, *args, **kwargs):
        if str(database).startswith(source.resolve().as_uri()):
            assert database == uri and kwargs.get('uri') is True
            kwargs['factory'] = Connection
            connection = original(database, *args, **kwargs)
            connections.append(connection)
            return connection
        return original(database, *args, **kwargs)

    monkeypatch.setattr(preparation.sqlite3, 'connect', connect)
    yield connections, statements
    assert connections
    assert all(c.closed and c.rolled_back for c in connections)


def test_missing_catalog_mixed_scope_keeps_all_1500_nonpixmaps(source, audit, monkeypatch):
    items = [(i, 'pixmap', {'canvasId': 'A'}) for i in range(-3, 301)]
    items += [(1000 + i, ('text', 'path', 'video', 'glb', 'future-kind')[i % 5],
               {'canvasId': 'A'}) for i in range(1500)]
    items += [(3000, 'text', {'canvasId': 'B'}), (3001, 'pixmap', {'canvasId': 'B'})]
    populate(source, items, [('A', 'canvas', 0), ('B', 'canvas', 1)])
    before = source.read_bytes(), preparation.source_stamp(source)
    forbid_build(monkeypatch)
    result = resolve(source)
    assert result['ids'] == tuple(range(-3, 253)) + tuple(range(1000, 2500))
    assert result['target'] == 'A'
    assert result['pending']['reason']
    assert result['scope'].counts() is None and result['scope'].total() is None
    assert not result['scope'].ready
    assert not source.with_suffix('.sqlite').exists()
    assert before == (source.read_bytes(), preparation.source_stamp(source))
    connections, statements = audit
    assert len(connections) == 1
    assert sum(sql == 'BEGIN' for sql, _ in statements) == 1
    scans = [(sql, args) for sql, args in statements if 'WITH metadata' in sql]
    assert len(scans) >= 3
    assert all(args[-1] == preparation.MAX_BATCH for _, args in scans)
    assert all('OFFSET' not in sql.upper() and 'SQLAR' not in sql.upper()
               for sql, _ in statements)
    assert all(not sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE', 'CREATE'))
               for sql, _ in statements)


@pytest.mark.parametrize('state', ['stale', 'interrupted'])
def test_unusable_catalog_does_not_supply_ids_or_get_rebuilt(source, monkeypatch, state):
    populate(source, [(i, 'pixmap', {'canvasId': 'A'}) for i in range(1, 301)],
             [('A', 'canvas', 0), ('B', 'canvas', 1)])
    cache = source.with_suffix('.sqlite')
    scope = ProjectOpenScope(source, cache)
    if state == 'stale':
        assert scope.build().complete
        with sqlite3.connect(source) as db:
            db.execute('UPDATE items SET data=? WHERE id=1', ('{"canvasId":"B"}',))
    else:
        stopped = threading.Event()
        result = scope.build(batch_size=128, cancel=stopped.is_set,
                             on_batch=lambda count: stopped.set())
        assert not result.complete
    before = cache.read_bytes()
    forbid_build(monkeypatch)
    result = resolve(source, cache)
    assert not result['scope'].ready
    assert result['scope'].total() is None
    assert result['ids'] == (tuple(range(2, 258)) if state == 'stale' else tuple(range(1, 257)))
    assert result['pending']['reason']
    assert cache.read_bytes() == before


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('other_content', [False, True])
def test_legal_empty_homepage_is_empty_ids_not_full_reader(source, ready, other_content):
    populate(source, [(8, 'text', {'canvasId': 'B'})] if other_content else [],
             [('A', 'canvas', 0), ('B', 'canvas', 1)])
    if ready:
        assert ProjectOpenScope(source, source.with_suffix('.sqlite')).build().complete
    result = resolve(source)
    assert result['target'] == 'A' and result['ids'] == ()
    assert (result['pending'] is None) == ready


def test_order_uses_sort_order_then_rowid_skips_noncanvas_and_deleted(source):
    populate(source, [(1, 'text', {'canvasId': 'Z'}), (2, 'text', {'canvasId': 'A'})],
             [('notes', 'document', -4), ('deleted', 'canvas', -2),
              ('Z', 'canvas', 0), ('A', 'canvas', 0)],
             [{'pageId': 'deleted', 'deletedAt': 123}])
    result = resolve(source)
    assert result['target'] == 'Z' and result['ids'] == (1,)


@pytest.mark.parametrize('legacy', [False, True])
def test_default_missing_and_explicit_null_match_catalog_projection(source, legacy):
    populate(source, [(1, 'text', {}), (2, 'text', {'canvasId': None}),
                      (3, 'pixmap', {'canvasId': 'default-canvas'}),
                      (4, 'text', {'canvasId': 'orphan'})])
    if legacy:
        with sqlite3.connect(source) as db:
            db.execute('DROP TABLE workspace_pages')
            db.execute('DROP TABLE workspace_nodes')
    result = resolve(source)
    assert result['target'] == 'default-canvas' and result['ids'] == (1, 3)
    # Explicitly built catalog is the authority for the projection semantics.
    scope = ProjectOpenScope(source, source.with_suffix('.sqlite'))
    assert scope.build().complete
    assert scope.counts()[None, 'text'] == 1
    assert tuple(row['id'] for row in scope.page('default-canvas')) == result['ids']


@pytest.mark.parametrize('canvas,stored', [(True, '1'), (False, '0'),
                                           (7, '7'), (1.0, '1.0'), (1e20, '1.0e+20')])
def test_scalar_canvas_text_affinity_matches_actual_catalog(source, canvas, stored):
    populate(source, [(1, 'pixmap', {'canvasId': canvas})], [(stored, 'canvas', 0)])
    result = resolve(source)
    assert result['target'] == stored and result['ids'] == (1,)
    scope = ProjectOpenScope(source, source.with_suffix('.sqlite'))
    assert scope.build().complete
    assert tuple(row['id'] for row in scope.page(stored)) == result['ids']


@pytest.mark.parametrize('payload', [None, '', 0])
def test_falsy_item_payload_matches_current_catalog_empty_object_semantics(source, payload):
    with sqlite3.connect(source) as db:
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                   (1, 'text', 0, 0, 0, 1, 0, 1, payload))
    assert resolve(source)['ids'] == (1,)
    scope = ProjectOpenScope(source, source.with_suffix('.sqlite'))
    assert scope.build().complete
    assert tuple(row['id'] for row in scope.page('default-canvas')) == (1,)


@pytest.mark.parametrize('extra', [{'canvasId': None}, {'canvasId': 'orphan'}, {}])
def test_no_legal_canvas_nonempty_keeps_explicit_full_compatibility(source, extra, monkeypatch):
    populate(source, [(1, 'text', extra)], nodes=[{'pageId': 'default-canvas', 'deletedAt': 1}])
    events = []
    monkeypatch.setattr(preparation, 'trace_record', lambda event, **kw: events.append((event, kw)))
    result = resolve(source)
    assert result['target'] is None and result['ids'] is None
    assert result['pending'] is not None
    selected = next(kw for event, kw in events if event == 'project.open.scope_selected')
    assert selected['selection_reason'] == 'no_legal_canvas_nonempty_source_compatibility'
    assert not selected['total_known']


def test_truly_empty_no_canvas_full_compatibility_has_explicit_reason(source, monkeypatch):
    events = []
    monkeypatch.setattr(preparation, 'trace_record', lambda event, **kw: events.append((event, kw)))
    result = resolve(source)
    assert result['target'] is None and result['ids'] is None
    selected = next(kw for event, kw in events if event == 'project.open.scope_selected')
    assert selected['selection_reason'] == 'no_legal_canvas_empty_source_compatibility'


def test_soft_deleted_items_are_kept_like_catalog_page_and_counts(source):
    populate(source, [(1, 'pixmap', {'canvasId': 'A', 'deletedAt': 1}),
                      (2, 'text', {'canvasId': 'A', 'deletedAt': 1}),
                      (3, 'video', {'canvasId': 'B', 'deletedAt': 1})],
             [('A', 'canvas', 0), ('B', 'canvas', 1)])
    result = resolve(source)
    assert result['ids'] == (1, 2)
    scope = ProjectOpenScope(source, source.with_suffix('.sqlite'))
    assert scope.build().complete
    assert scope.counts()['A', 'pixmap'] == 1
    assert tuple(row['id'] for row in scope.page('A')) == (1, 2)


def test_soft_deleted_homepage_moves_to_empty_next_canvas(source):
    populate(source, [(1, 'text', {'canvasId': 'A'})],
             [('A', 'canvas', 0), ('B', 'canvas', 1)],
             [{'pageId': 'A', 'deletedAt': 1}])
    result = resolve(source)
    assert result['target'] == 'B' and result['ids'] == ()


@pytest.mark.parametrize('table,column,payload', [
    ('items', 'data', '{bad'), ('items', 'data', 'null'),
    ('items', 'data', '[]'), ('items', 'data', '{"canvasId":[]}'),
    ('workspace_nodes', 'payload', '{bad'), ('workspace_nodes', 'payload', 'null'),
])
def test_corrupt_json_is_not_empty_legacy(source, audit, table, column, payload):
    populate(source, [(1, 'text', {'canvasId': 'A'})], [('A', 'canvas', 0)], [{}])
    with sqlite3.connect(source) as db:
        db.execute('UPDATE %s SET %s=?' % (table, column), (payload,))
    with pytest.raises((ValueError, TypeError)):
        resolve(source)
    assert not source.with_suffix('.sqlite').exists()


def test_bad_json_outside_homepage_and_after_pixmap_limit_is_not_hidden(source):
    populate(source, [(i, 'pixmap', {'canvasId': 'A'}) for i in range(1, 1201)] +
             [(2000, 'text', {'canvasId': 'B'})], [('A', 'canvas', 0), ('B', 'canvas', 1)])
    with sqlite3.connect(source) as db:
        db.execute('UPDATE items SET data=? WHERE id=2000', ('{broken',))
    with pytest.raises(ValueError, match='2000'):
        resolve(source)


@pytest.mark.parametrize('table', ['workspace_pages', 'workspace_nodes', 'items'])
def test_existing_malformed_schema_is_not_optional_absence(source, audit, table):
    with sqlite3.connect(source) as db:
        db.execute('DROP TABLE ' + table)
        db.execute('CREATE TABLE %s(wrong_column)' % table)
    with pytest.raises(sqlite3.OperationalError):
        resolve(source)


def test_damaged_sqlite_is_a_visible_error(source):
    source.write_bytes(b'not a sqlite project')
    with pytest.raises(sqlite3.DatabaseError):
        resolve(source)


def test_cancel_before_start_never_returns_full_sentinel(source):
    with pytest.raises(preparation.PreparationCancelled):
        resolve(source, cancel=lambda: True)


def test_cancel_during_python_scan_rolls_back_and_closes(source, audit):
    populate(source, [(i, 'text', {'canvasId': 'A'}) for i in range(1, 1501)],
             [('A', 'canvas', 0)])
    calls = 0
    def cancel():
        nonlocal calls
        calls += 1
        return calls >= 200
    with pytest.raises(preparation.PreparationCancelled):
        resolve(source, cancel=cancel)
    assert audit[0][0].rolled_back and audit[0][0].closed


def test_sqlite_progress_interrupt_is_preparation_cancelled(source, monkeypatch):
    populate(source, [(i, 'text', {'canvasId': 'A'}) for i in range(1, 1501)],
             [('A', 'canvas', 0)])
    original = sqlite3.connect
    cancelled = threading.Event()
    connections = []
    class Connection(sqlite3.Connection):
        closed = rolled_back = interrupted = False
        def set_progress_handler(self, callback, count):
            if callback is None:
                return super().set_progress_handler(None, count)
            def interrupt():
                self.interrupted = True
                cancelled.set()
                return callback()
            return super().set_progress_handler(interrupt, count)
        def rollback(self):
            self.rolled_back = True
            return super().rollback()
        def close(self):
            self.closed = True
            return super().close()
    def connect(database, *args, **kwargs):
        if str(database).startswith(source.resolve().as_uri()):
            kwargs['factory'] = Connection
            conn = original(database, *args, **kwargs)
            connections.append(conn)
            return conn
        return original(database, *args, **kwargs)
    monkeypatch.setattr(preparation.sqlite3, 'connect', connect)
    with pytest.raises(preparation.PreparationCancelled):
        resolve(source, cancel=cancelled.is_set)
    assert connections[0].interrupted
    assert connections[0].rolled_back and connections[0].closed


def test_missing_json1_is_explicit_and_closes_connection(source, monkeypatch):
    original = sqlite3.connect
    connections = []
    class Connection(sqlite3.Connection):
        closed = False
        def execute(self, sql, parameters=()):
            if "SELECT json_valid('{}')" in sql:
                raise sqlite3.OperationalError('no such function: json_valid')
            return super().execute(sql, parameters)
        def close(self):
            self.closed = True
            return super().close()
    def connect(database, *args, **kwargs):
        if str(database).startswith(source.resolve().as_uri()):
            kwargs['factory'] = Connection
            conn = original(database, *args, **kwargs)
            connections.append(conn)
            return conn
        return original(database, *args, **kwargs)
    monkeypatch.setattr(preparation.sqlite3, 'connect', connect)
    with pytest.raises(RuntimeError, match='JSON1'):
        resolve(source)
    assert connections[0].closed


@pytest.mark.parametrize('when', ['before_snapshot', 'after_snapshot', 'wal_write'])
def test_source_change_rejected_without_rebasing(source, monkeypatch, when):
    populate(source, [(1, 'text', {'canvasId': 'A'})], [('A', 'canvas', 0)])
    original = preparation.require_stamp
    writer = None
    if when != 'before_snapshot':
        writer = sqlite3.connect(source)
        writer.execute('PRAGMA journal_mode=WAL')
        writer.commit()
    calls = 0
    trigger = {'before_snapshot': 1, 'wal_write': 2, 'after_snapshot': 3}[when]
    def checked(filename, stamp):
        nonlocal calls
        calls += 1
        if calls == trigger:
            if writer is not None:
                writer.execute('UPDATE items SET x=99 WHERE id=1')
                writer.commit()
            else:
                connection = sqlite3.connect(source)
                try:
                    connection.execute('UPDATE items SET x=99 WHERE id=1')
                    connection.commit()
                finally:
                    connection.close()
        return original(filename, stamp)
    monkeypatch.setattr(preparation, 'require_stamp', checked)
    try:
        with pytest.raises(RuntimeError, match='changed'):
            resolve(source)
    finally:
        if writer is not None:
            writer.close()


@pytest.fixture
def controller(qapp):
    owner = QtCore.QObject()
    worker = preparation.PagedOpenPreparation(owner)
    yield worker
    worker.cancel()
    # Test teardown only; the implementation never joins on the GUI thread.
    assert worker.pool.waitForDone(5000)


def test_bound_operation_signal_precedes_legacy_and_runs_on_gui(controller, qtbot, qapp):
    operation, value = object(), object()
    events = []
    controller.operation_completed.connect(
        lambda *args: events.append(('operation', args, QtCore.QThread.currentThread())))
    controller.completed.connect(lambda *args: events.append(('legacy', args, None)))
    controller.start('session', lambda: value, operation=operation)
    qtbot.waitUntil(lambda: len(events) == 2)
    assert [event[0] for event in events] == ['operation', 'legacy']
    assert events[0][1][:4] == (operation, 'session', value, None)
    assert events[0][2] == qapp.thread()
    assert events[1][1][:3] == ('session', value, None)


def test_unbound_start_preserves_legacy_signal_only(controller, qtbot):
    operations, results = [], []
    controller.operation_completed.connect(lambda *args: operations.append(args))
    controller.completed.connect(lambda *args: results.append(args))
    controller.start('scope', lambda arg, cancel: (arg, cancel()), 3)
    qtbot.waitUntil(lambda: len(results) == 1)
    assert results[0][:3] == ('scope', (3, False), None)
    assert operations == []


@pytest.mark.parametrize('timing', ['running', 'queued_result', 'queued_task'])
def test_bound_cancel_delivers_terminal_not_value(controller, qtbot, timing):
    operation = object()
    entered, release = threading.Event(), threading.Event()
    events, legacy = [], []
    controller.operation_completed.connect(lambda *args: events.append(args))
    controller.completed.connect(lambda *args: legacy.append(args))
    def slow():
        entered.set()
        assert release.wait(5)
        return 'must not install'
    if timing == 'queued_task':
        controller.start('session', slow)
        qtbot.waitUntil(entered.is_set)
        controller.start('session', lambda: 'must not run', operation=operation)
    else:
        controller.start('session', slow, operation=operation)
        qtbot.waitUntil(entered.is_set)
    if timing == 'queued_result':
        release.set()
        assert controller.pool.waitForDone(5000)
    controller.cancel()
    release.set()
    qtbot.waitUntil(lambda: len(events) == 1)
    assert events[0][0] is operation
    assert events[0][2] is None
    assert isinstance(events[0][3], preparation.PreparationCancelled)
    assert legacy == []


def test_replaced_epoch_drops_old_operation_callback(controller, qtbot):
    old_operation, new_operation = object(), object()
    entered, release = threading.Event(), threading.Event()
    events = []
    controller.operation_completed.connect(lambda *args: events.append(args))
    def slow():
        entered.set()
        assert release.wait(5)
        return 'old'
    controller.start('session', slow, operation=old_operation)
    try:
        qtbot.waitUntil(entered.is_set)
        controller.start('session', lambda: 'new', operation=new_operation)
    finally:
        release.set()
    qtbot.waitUntil(lambda: len(events) == 1)
    assert events[0][0] is new_operation and events[0][2] == 'new'


def test_bound_error_preserves_operation_and_exception(controller, qtbot):
    operation, error = object(), ValueError('bad source')
    events = []
    def fail():
        raise error
    controller.operation_completed.connect(lambda *args: events.append(args))
    controller.start('session', fail, operation=operation)
    qtbot.waitUntil(lambda: len(events) == 1)
    assert events[0][0] is operation and events[0][3] is error
