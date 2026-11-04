"""A scoped or paged open must install every kind of a canvas.

Images are not the whole canvas: a canvas also holds text notes, brush
strokes, videos and 3D models. A page that only reads pixmaps leaves those
entities in the project but invisible on the canvas, which is the failure
this file guards against.

The recipes are the ones of ``tests/test_paged_canvas_loader.py``: a real
temporary SQLite project, a real catalog, the real view.
"""
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest

from prism import performance_trace
from prism.asset_records import ImageRecord
from prism.fileio import sql as sql_module
from prism.items import (PrismErrorItem, PrismGlbItem, PrismPathItem,
                         PrismTextItem, PrismVideoItem)
from prism.paged_canvas_loader import CatalogBuildController
from prism.partial_project import PartialProjectSession
from prism.partial_residency import key_of
from prism.project_open_scope import ProjectOpenScope
from tests.test_partial_project_gui import SCHEMA, PNG


#: Canvas A of the loader project: more than one page of images plus one
#: entity of every other kind. Canvas B stays untouched until it is asked
#: for.
A_PIXMAPS = 300
B_PIXMAPS = 5


def _write_items(db, rows, blobs=()):
    for key, kind, extra in rows:
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                   (key, kind, key, 0, 0, 1, 0, 1, json.dumps(extra)))
    for name, key, payload in blobs:
        db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                   (name, key, 0o644, len(payload), payload))


def _write_kinds_project(path, project_id):
    """A project with every kind on canvas A and a little on canvas B."""
    original = PNG.read_bytes()
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('project_id', project_id))
        for canvas in ('A', 'B'):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (canvas, 'canvas', canvas, '{}', 0))
        rows = []
        blobs = []
        key = 1
        for index in range(A_PIXMAPS):
            extra = {'canvasId': 'A', 'imageWidth': 16, 'imageHeight': 16,
                     'filename': 'a-%d.png' % index}
            rows.append((key, 'pixmap', extra))
            blobs.append((extra['filename'], key, original))
            key += 1
        for kind, extra in (
                ('text', {'canvasId': 'A', 'text': 'a note'}),
                ('path', {'canvasId': 'A', 'points': [[0, 0], [10, 10]]}),
                ('video', {'canvasId': 'A', 'filename': 'clip.mp4'}),
                ('glb', {'canvasId': 'A', 'filename': 'model.glb'})):
            rows.append((key, kind, extra))
            if kind == 'video':
                blobs.append(('clip.mp4', key, b'video'))
            elif kind == 'glb':
                blobs.append(('model.glb', key, b'glb\x00'))
            key += 1
        canvas_a = tuple(range(1, key))
        for index in range(B_PIXMAPS):
            extra = {'canvasId': 'B', 'imageWidth': 16, 'imageHeight': 16,
                     'filename': 'b-%d.png' % index}
            rows.append((key, 'pixmap', extra))
            blobs.append((extra['filename'], key, original))
            key += 1
        rows.append((key, 'text', {'canvasId': 'B', 'text': 'other canvas'}))
        canvas_b = tuple(range(canvas_a[-1] + 1, key + 1))
        _write_items(db, rows, blobs)
    return canvas_a, canvas_b


def _write_bulk_project(path, project_id, images=300, notes=1100):
    """A project with more ids than an old SQLite could bind at once."""
    original = PNG.read_bytes()
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('project_id', project_id))
        db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                   ('A', 'canvas', 'A', '{}', 0))
        rows = []
        blobs = []
        key = 1
        for index in range(images):
            extra = {'canvasId': 'A', 'imageWidth': 16, 'imageHeight': 16,
                     'filename': 'p-%d.png' % index}
            rows.append((key, 'pixmap', extra))
            blobs.append((extra['filename'], key, original))
            key += 1
        for index in range(notes):
            rows.append((key, 'text',
                         {'canvasId': 'A', 'text': 'note %d' % index}))
            key += 1
        _write_items(db, rows, blobs)
    return tuple(range(1, key))


def _installed(scene):
    """Installed items by save id.

    An error placeholder has no ``save_id`` of its own; it keeps the id of
    the record it stands in for, so it is found under that id too.
    """
    return {key_of(item): item for item in scene.iter_assets()
            if key_of(item) is not None}


@pytest.fixture
def kinds_project(main_window, tmp_path):
    view = main_window.view
    view.clear_scene()
    scene = view.scene
    source = tmp_path / 'kinds.prism'
    canvas_a, canvas_b = _write_kinds_project(source, scene.project_id)
    scene.workspace_pages = [{'id': key, 'kind': 'canvas', 'title': key}
                             for key in ('A', 'B')]
    view.filename = str(source)
    scope = ProjectOpenScope(source, tmp_path / 'index.sqlite')
    scope.build()
    view._open_scope = scope
    yield view, scope, source, canvas_a, canvas_b
    loader = getattr(view, '_paged_canvas_loader', None)
    if loader:
        loader.cancel(reset=True)
        loader.pool.waitForDone(5000)


@pytest.fixture
def bulk_project(tmp_path):
    """A project with more ids than one bound statement can carry.

    The scoped reader is exercised on a **bare scene**: these cases are
    about the SQL layer, and a bare scene keeps them independent of what
    the rest of the application happens to do in the event loop.
    """
    from PyQt6 import QtGui
    from prism.scene import PrismGraphicsScene
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    source = tmp_path / 'bulk.prism'
    ids = _write_bulk_project(source, scene.project_id)
    return scene, source, ids


class _Trace:
    """A trace that keeps its events, so a test can count the fetches."""

    last_action = None

    def __init__(self):
        self.events = []

    def record(self, kind, **fields):
        self.events.append((kind, fields))

    def fetches(self, query):
        return [fields for kind, fields in self.events
                if kind == 'operation' and fields.get('name') == 'project.sql.fetch'
                and fields.get('query') == query]


def test_scoped_read_installs_every_kind_of_the_scope(kinds_project):
    """The scoped reader installs the canvas' text, strokes, video and model."""
    view, scope, source, canvas_a, canvas_b = kinds_project
    scene = view.scene
    io = sql_module.SQLiteIO(str(source), scene, readonly=True)
    io.read(scope=canvas_a)
    scene.add_queued_items()
    installed = _installed(scene)
    assert set(installed) == set(canvas_a)
    assert isinstance(installed[canvas_a[-4]], PrismTextItem)
    assert isinstance(installed[canvas_a[-3]], PrismPathItem)
    assert isinstance(installed[canvas_a[-2]], PrismVideoItem)
    assert isinstance(installed[canvas_a[-1]], PrismGlbItem)
    # The other canvas is not in the scope, so it stays out of the scene.
    assert not set(installed) & set(canvas_b)


def test_continuation_installs_every_kind_and_keeps_canvases_apart(
        kinds_project, qtbot):
    """The continuation page covers every kind, past the 256 id page."""
    view, scope, source, canvas_a, canvas_b = kinds_project
    scene = view.scene
    session = PartialProjectSession.open(source, item_ids=[],
                                         page_ids=['A', 'B'])
    session.attach(scene)
    assert view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    qtbot.waitUntil(lambda: not loader.active, timeout=20000)
    # The keyset continuation crossed 256 ids and still installed the four
    # non-image entities of the canvas.
    assert set(session.loaded()) == set(canvas_a)
    installed = _installed(scene)
    assert set(installed) == set(canvas_a)
    assert isinstance(installed[canvas_a[-4]], PrismTextItem)
    assert isinstance(installed[canvas_a[-3]], PrismPathItem)
    assert isinstance(installed[canvas_a[-2]], PrismVideoItem)
    assert isinstance(installed[canvas_a[-1]], PrismGlbItem)
    assert not set(installed) & set(canvas_b)
    # The other canvas is still untouched.
    assert view.request_canvas_assets('B')
    qtbot.waitUntil(lambda: not loader.active, timeout=20000)
    assert set(session.loaded()) == set(canvas_a) | set(canvas_b)
    # One canonical instance per material, not a second copy.
    assert view.ensure_asset_loaded(str(canvas_a[0])) is installed[canvas_a[0]]


def test_continuation_refuses_an_unsupported_record(main_window, tmp_path,
                                                    qtbot):
    """A record nothing can install is an error, not a silently skipped row."""
    view = main_window.view
    view.clear_scene()
    scene = view.scene
    source = tmp_path / 'alien.prism'
    original = PNG.read_bytes()
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('project_id', scene.project_id))
        db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                   ('A', 'canvas', 'A', '{}', 0))
        db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                   (1, 'alien', 1, 0, 0, 1, 0, 1,
                    json.dumps({'canvasId': 'A'})))
        db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                   ('a.png', 1, 0o644, len(original), original))
    scene.workspace_pages = [{'id': 'A', 'kind': 'canvas', 'title': 'A'}]
    view.filename = str(source)
    scope = ProjectOpenScope(source, tmp_path / 'index.sqlite')
    scope.build()
    view._open_scope = scope
    session = PartialProjectSession.open(source, item_ids=[], page_ids=['A'])
    session.attach(scene)
    assert view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    with qtbot.waitSignal(loader.failed, timeout=20000) as result:
        pass
    assert 'unsupported record type' in result.args[1]
    assert not loader.active


def test_scoped_read_of_an_empty_scope_reads_nothing(bulk_project,
                                                      monkeypatch):
    """An empty scope is a read of nothing, not a failed read.

    SQLite accepts ``IN ()``, so the scoped statements still run. The trace
    has to show that they ran and that they returned no row: a scope of
    zero must not read the project.
    """
    scene, source, ids = bulk_project
    trace = _Trace()
    monkeypatch.setattr(performance_trace, '_active', trace)

    io = sql_module.SQLiteIO(str(source), scene, readonly=True)
    io.read(scope=())
    scene.add_queued_items()

    assert not list(scene.iter_assets())
    assert scene._loaded_scope_ids == ()
    for query in ('image_metadata', 'native_item_metadata'):
        fetches = trace.fetches(query)
        assert fetches, 'the %s query never ran' % query
        assert all(fields['rows'] == 0 for fields in fetches)


def test_scoped_read_chunks_a_scope_the_runtime_cannot_bind(bulk_project,
                                                             monkeypatch):
    """A scope larger than the variable limit reads the same rows."""
    scene, source, ids = bulk_project
    trace = _Trace()
    monkeypatch.setattr(performance_trace, '_active', trace)

    def read_with(limit):
        scene.clear()
        monkeypatch.setattr(sql_module, 'sql_variable_limit', lambda: limit)
        before = len(trace.events)
        io = sql_module.SQLiteIO(str(source), scene, readonly=True)
        io.read(scope=ids)
        scene.add_queued_items()
        events = trace.events[before:]
        return _installed(scene), events

    chunked, chunked_events = read_with(999)
    whole, whole_events = read_with(10 ** 9)

    def fetches(events, query):
        return [fields for kind, fields in events
                if kind == 'operation'
                and fields.get('name') == 'project.sql.fetch'
                and fields.get('query') == query]

    # The same materials, the same kinds -- chunking changes nothing.
    assert set(chunked) == set(whole) == set(ids)
    assert {key: item.TYPE for key, item in chunked.items()} == {
        key: item.TYPE for key, item in whole.items()}

    # The small limit really did split the work into several statements.
    chunked_native = fetches(chunked_events, 'native_item_metadata')
    whole_native = fetches(whole_events, 'native_item_metadata')
    assert len(chunked_native) > len(whole_native) == 1

    # One page of images per fetch, never the whole project.
    total = len(ids)
    for fields in fetches(chunked_events, 'image_metadata'):
        assert fields['rows'] <= 256
    for fields in chunked_native:
        assert fields['rows'] <= total
    # The non-image rows stay under their own label.
    assert sum(fields['rows'] for fields in chunked_native) == total - 300


def test_chunking_returns_the_rows_the_single_statement_returns(bulk_project,
                                                                 monkeypatch):
    """The merged rows are the rows of the unchunked statement, in order."""
    scene, source, ids = bulk_project
    captured = {}
    original = sql_module.SQLiteIO._read_rows

    def spy(self, label, statement, parameters=()):
        rows = original(self, label, statement, parameters)
        captured.setdefault(label, []).extend(rows)
        return rows

    monkeypatch.setattr(sql_module.SQLiteIO, '_read_rows', spy)
    io = sql_module.SQLiteIO(str(source), scene, readonly=True)
    io.read(scope=ids)
    scene.add_queued_items()

    captured.clear()
    monkeypatch.setattr(sql_module, 'sql_variable_limit', lambda: 999)
    chunked = sql_module.SQLiteIO(str(source), scene, readonly=True)
    chunked.read(scope=ids)
    chunked_rows = list(captured.get('image_metadata', [])) + \
        list(captured.get('native_item_metadata', []))

    captured.clear()
    monkeypatch.setattr(sql_module, 'sql_variable_limit', lambda: 10 ** 9)
    whole = sql_module.SQLiteIO(str(source), scene, readonly=True)
    whole.read(scope=ids)
    whole_rows = list(captured.get('image_metadata', [])) + \
        list(captured.get('native_item_metadata', []))

    assert chunked_rows == whole_rows


def test_continuation_keeps_the_canvas_when_one_original_is_gone(
        main_window, tmp_path, qtbot):
    """A material without its original must not empty the whole canvas.

    One damaged material is a placeholder on the canvas, not a failed page:
    the healthy image and the note of the same canvas still arrive.
    """
    view = main_window.view
    view.clear_scene()
    scene = view.scene
    source = tmp_path / 'damaged.prism'
    original = PNG.read_bytes()
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)',
                   ('project_id', scene.project_id))
        db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                   ('A', 'canvas', 'A', '{}', 0))
        rows = [(1, 'pixmap', {'canvasId': 'A', 'imageWidth': 16,
                               'imageHeight': 16, 'filename': 'a.png'}),
                (2, 'pixmap', {'canvasId': 'A', 'filename': 'lost.png'}),
                (3, 'text', {'canvasId': 'A', 'text': 'a note'})]
        for key, kind, extra in rows:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, key, 0, 0, 1, 0, 1, json.dumps(extra)))
        # Only the first image still has its original bytes.
        db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                   ('a.png', 1, 0o644, len(original), original))
    scene.workspace_pages = [{'id': 'A', 'kind': 'canvas', 'title': 'A'}]
    view.filename = str(source)
    scope = ProjectOpenScope(source, tmp_path / 'index.sqlite')
    scope.build()
    view._open_scope = scope
    session = PartialProjectSession.open(source, item_ids=[], page_ids=['A'])
    session.attach(scene)
    assert view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    qtbot.waitUntil(lambda: not loader.active, timeout=20000)

    installed = _installed(scene)
    # The canvas is not empty: every record of the canvas is installed.
    assert set(installed) == {1, 2, 3}
    assert isinstance(installed[1], ImageRecord)
    assert isinstance(installed[3], PrismTextItem)
    # The damaged one says so instead of pretending to be an image.
    assert isinstance(installed[2], PrismErrorItem)
    assert installed[2].original_save_id == 2
    assert 'lost.png' in installed[2].toPlainText()
    # The page is registered, so a save leaves the damaged row as it is.
    assert set(session.loaded()) == {1, 2, 3}


def test_catalog_build_can_be_cancelled_and_resumes(tmp_path, qtbot):
    """A cancelled build stops at once and leaves a resumable cache."""
    source = tmp_path / 'many.prism'
    total = _write_bulk_project(source, 'project', images=10, notes=1990)
    scope = ProjectOpenScope(source, tmp_path / 'index.sqlite')
    controller = CatalogBuildController()
    progress = []
    finished = []
    cancelled = []
    controller.progress.connect(lambda done, count: progress.append((done, count)))
    controller.finished.connect(finished.append)
    controller.cancelled.connect(lambda: cancelled.append(True))
    try:
        assert controller.start(scope, batch_size=8)
        qtbot.waitUntil(lambda: bool(progress), timeout=20000)
        started = time.perf_counter()
        controller.cancel()
        qtbot.waitUntil(lambda: bool(cancelled) or bool(finished), timeout=20000)
        latency = time.perf_counter() - started
    finally:
        controller.pool.waitForDone(10000)

    # The user stopped it: the build says so instead of finishing.
    assert cancelled, 'the build did not report the cancel'
    assert not finished
    # The stop was immediate: nothing slept and nothing busy-waited.
    assert latency < 1.0
    # An unfinished cache is never mistaken for a complete one.
    assert not scope.ready
    with closing(sqlite3.connect(
            Path(scope.cache_path).as_uri() + '?mode=ro', uri=True)) as db:
        state = json.loads(db.execute(
            "SELECT value FROM catalog_state WHERE key='build'").fetchone()[0])
    assert state['complete'] is False
    # The checkpoint survives, so the next build resumes and finishes.
    result = scope.build(batch_size=128)
    assert result.resumed
    assert result.complete
    assert result.total_records == len(total)
    assert scope.ready


def test_catalog_controller_reports_progress_using_shared_protocol(qapp, monkeypatch):
    from types import SimpleNamespace
    from prism import paged_canvas_loader as loader
    recorded = []
    monkeypatch.setattr(loader, 'record_catalog_build_progress',
                        lambda **fields: recorded.append(fields))
    controller = CatalogBuildController()
    controller.epoch = 3
    controller.build_id = 'build-3'
    controller.active = True
    controller._received((2, 'progress', (999, 0)))
    assert not recorded
    controller._received((3, 'progress', (50000, 0)))
    controller._received((3, 'finished', SimpleNamespace(
        total_records=100000, processed_records=50000)))
    assert [row['build_id'] for row in recorded] == ['build-3', 'build-3']
    assert recorded[0]['processed'] == 50000
    assert recorded[0]['processed_this_run'] is None
    assert recorded[0]['total_records'] is None
    assert recorded[0]['total_known'] is False
    assert recorded[1]['processed_this_run'] == 50000
    assert recorded[1]['total_records'] == 100000
    assert recorded[1]['final'] is True
    assert controller.active is False
