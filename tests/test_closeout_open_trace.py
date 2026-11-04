from contextlib import contextmanager
import sqlite3
import threading
from unittest.mock import Mock

from prism import performance_trace as pt
from prism.fileio.sql import SQLiteIO


def test_query_trace_keeps_empty_scope_distinct_and_never_logs_values(monkeypatch):
    events = []
    trace = Mock(last_action=None)
    def record(kind, **fields):
        context = pt.current_context()
        fields.setdefault('trace_id', context.trace_id if context else None)
        events.append((kind, fields))
    trace.record.side_effect = record
    monkeypatch.setattr(pt, '_active', trace)
    connection = sqlite3.connect(':memory:')
    reader = object.__new__(SQLiteIO)
    reader._connection = connection
    reader._cursor = connection.cursor()
    reader.scope_ids = ()
    secret = 'never put user notes in trace'
    try:
        with pt.restore_context(pt.TraceContext(trace_id='open-test', project_token='project-test')):
            rows = reader._read_rows('image_metadata', 'SELECT ? AS payload', (secret,))
        assert rows == [(secret,)]
        ends = [fields for kind, fields in events if kind == 'operation'
                and fields.get('name') in ('project.sql.execute', 'project.sql.fetch')]
        assert len(ends) == 2
        assert len({row['query_id'] for row in ends}) == 1
        assert all(row['scope_kind'] == 'ids' and row['requested_ids_count'] == 0 for row in ends)
        assert ends[0]['rows_returned'] is None
        assert ends[1]['rows_returned'] == 1
        assert ends[1]['returned_text_chars_sample'] == len(secret)
        assert secret not in repr(events)
        assert all(row['physical_disk_bytes'] is None and row['lock_wait_ms'] is None for row in ends)
        assert all(row['trace_id'] == 'open-test' for row in ends)
    finally:
        connection.close()


def test_reader_wrapper_restores_captured_context(monkeypatch):
    from prism import view
    expected = pt.TraceContext(trace_id='open-worker', project_token='p', open_epoch=4)
    observed = []
    monkeypatch.setattr(view.fileio, 'load_prism', lambda *a, **kw: observed.append(pt.current_context()))
    def work():
        assert pt.current_context() is None
        view._read_project_with_trace(expected, 'unused', object())
        assert pt.current_context() is None
    worker = threading.Thread(target=work)
    worker.start()
    worker.join(5)
    assert not worker.is_alive()
    assert observed[0].trace_id == expected.trace_id
    assert observed[0].project_token == expected.project_token


def test_preparation_worker_and_queued_install_keep_context(main_window, qtbot):
    from prism.paged_open_preparation import PagedOpenPreparation
    expected = pt.TraceContext(trace_id='scope-worker', project_token='p', open_epoch=6)
    observed = []
    preparation = PagedOpenPreparation(main_window.view)
    def function():
        observed.append(('worker', pt.current_context(), threading.get_ident()))
        return 3
    preparation.completed.connect(lambda *args: observed.append(
        ('install', pt.current_context(), threading.get_ident())))
    with pt.restore_context(expected):
        preparation.start('session', function)
    qtbot.waitUntil(lambda: len(observed) == 2, timeout=5000)
    assert observed[0][1] == expected
    assert observed[1][1] == expected
    assert observed[0][2] != threading.get_ident()
    assert observed[1][2] == threading.get_ident()


def test_catalog_delivery_runs_on_gui_and_rejects_cancelled_or_old_project(main_window, qtbot, monkeypatch):
    from types import SimpleNamespace
    from prism.view import _CatalogBuildDelivery
    view = main_window.view
    observed = []
    monkeypatch.setattr(view, '_catalog_status', lambda text: observed.append((text, threading.get_ident())))
    invalidations = []
    monkeypatch.setattr(main_window.asset_browser, 'invalidate_aggregate_result', lambda: invalidations.append(True))
    delivery = _CatalogBuildDelivery(view)
    delivery.delivered.connect(view._catalog_build_received)
    context = pt.TraceContext(trace_id='catalog')

    def request(project_id=None):
        return {'build_id': 'catalog-build', 'cancel': threading.Event(),
                'project_id': project_id or view.scene.project_id,
                'epoch': view._catalog_build_epoch, 'callbacks': [],
                'cache': 'catalog.sqlite'}

    current = request()
    view._catalog_build_active = current
    result = SimpleNamespace(total_records=1)
    payload = (current, 'complete', result, None, context)
    worker = threading.Thread(target=lambda: delivery.delivered.emit(payload))
    worker.start()
    worker.join(5)
    qtbot.waitUntil(lambda: len(observed) == 1, timeout=5000)
    assert observed[0][0].startswith('Project index ready')
    assert observed[0][1] == threading.get_ident()
    assert invalidations == [True]

    cancelled = request()
    cancelled['cancel'].set()
    view._catalog_build_active = cancelled
    view._catalog_build_received((cancelled, 'complete', result, None, context))
    old = request('wrong-project')
    view._catalog_build_active = old
    view._catalog_build_received((old, 'complete', result, None, context))
    assert len(observed) == len(invalidations) == 1
