"""Real temporary SQLite pages through the view's asynchronous continuation."""
import json
import sqlite3
import threading

import pytest

from prism.asset_records import ImageRecord
from prism.paged_canvas_loader import PagedCanvasLoader
from prism.partial_project import PartialProjectSession
from prism.project_open_scope import ProjectOpenScope
from tests.test_partial_project_gui import SCHEMA, PNG


@pytest.fixture
def pages(main_window, tmp_path):
    view = main_window.view
    view.clear_scene()
    scene = view.scene
    source = tmp_path / 'images.prism'
    with sqlite3.connect(source) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', scene.project_id))
        for canvas in ('A', 'B'):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (canvas, 'canvas', canvas, '{}', 0))
        for key in range(1, 361):
            extra = {'canvasId': 'A' if key <= 320 else 'B',
                     'imageWidth': 16, 'imageHeight': 16, 'filename': f'{key}.png'}
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 'pixmap', key, 0, 0, 1, 0, 1, json.dumps(extra)))
            original = PNG.read_bytes()
            db.execute('INSERT INTO sqlar(name,item_id,mode,sz,data) VALUES(?,?,?,?,?)',
                       (f'{key}.png', key, 0o644, len(original), original))
    scene.workspace_pages = [{'id': key, 'kind': 'canvas', 'title': key} for key in ('A', 'B')]
    view.filename = str(source)
    scope = ProjectOpenScope(source, tmp_path / 'index.sqlite')
    scope.build()
    view._open_scope = scope
    scene._partial_project_source = str(source)
    session = PartialProjectSession.open(source, item_ids=[], page_ids=['A', 'B'])
    session.attach(scene)
    yield view, scope, session
    preparation = getattr(view, '_paged_open_preparation', None)
    if preparation:
        preparation.cancel()
        preparation.pool.waitForDone(5000)
    loader = getattr(view, '_paged_canvas_loader', None)
    if loader:
        loader.cancel(reset=True)
        loader.pool.waitForDone(5000)


def test_view_continues_beyond_256_and_switches_without_scene_reset(pages, qtbot):
    view, scope, session = pages
    view.current_canvas_id = 'A'
    assert view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    qtbot.waitUntil(lambda: not loader.active, timeout=10000)
    assert set(session.loaded()) == set(range(1, 321))
    original = next(item for item in view.scene.iter_assets() if item.save_id == 1)
    original.notes = 'unsaved canonical edit'
    assert view.request_canvas_assets('B')
    qtbot.waitUntil(lambda: not loader.active, timeout=10000)
    assert len(session.loaded()) == 360
    assert view.ensure_asset_loaded(1) is original
    assert original.notes == 'unsaved canonical edit'
    assert view.request_canvas_assets('A')
    assert not loader.active
    assert len(list(view.scene.iter_assets())) == 360


def test_new_canvas_cancels_previous_request_and_clear_rejects_late_result(pages, qtbot):
    view, scope, session = pages
    view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    view.request_canvas_assets('B')
    qtbot.waitUntil(lambda: not loader.active, timeout=10000)
    assert set(session.loaded()) == set(range(321, 361))
    view.request_canvas_assets('A')
    view.clear_scene()
    assert not loader.active
    assert loader.pool.waitForDone(5000)
    qtbot.wait(30)
    assert not list(view.scene.iter_assets())
    assert view.scene._partial_project_session is None


def test_catalog_failure_is_visible_and_never_full_loads(pages, qtbot, monkeypatch):
    view, scope, session = pages
    def fail(*args, **kwargs):
        raise RuntimeError('injected stale catalog')
    monkeypatch.setattr(scope, 'page', fail)
    assert view.request_canvas_assets('A')
    loader = view._paged_canvas_loader
    with qtbot.waitSignal(loader.failed, timeout=10000) as result:
        pass
    assert 'injected stale catalog' in result.args[1]
    assert not list(view.scene.iter_assets())
    assert session.loaded() == ()


def test_returning_to_completed_page_cancels_other_page(pages, qtbot):
    view, scope, session = pages
    view.request_canvas_assets('B')
    loader = view._paged_canvas_loader
    qtbot.waitUntil(lambda: not loader.active, timeout=10000)
    view.request_canvas_assets('A')
    assert loader.active
    assert view.request_canvas_assets('B')
    assert not loader.active
    assert loader.pool.waitForDone(5000)
    qtbot.wait(20)
    assert set(session.loaded()) == set(range(321, 361))


def test_worker_result_does_not_replace_asset_installed_by_search(pages, qtbot):
    view, scope, session = pages
    view.request_canvas_assets('A')
    # Let disk finish, but do not dispatch its queued GUI delivery yet.
    assert view._paged_canvas_loader.pool.waitForDone(5000)
    # The synchronous search compatibility API must reuse the canonical result.
    first = view.ensure_asset_loaded('1')
    assert first is not None
    first.notes = 'search edit'
    assert view.ensure_asset_loaded('1') is first
    qtbot.waitUntil(lambda: not view._paged_canvas_loader.active, timeout=10000)
    items = [item for item in view.scene.iter_assets() if item.save_id == 1]
    assert items == [first]
    assert items[0].notes == 'search edit'


def test_stale_scope_defers_navigation_and_loader_refuses_unsafe_read(pages, monkeypatch):
    view, scope, session = pages
    loader = view._paged_canvas_loader = PagedCanvasLoader(view)
    errors = []
    loader.failed.connect(lambda canvas, message: errors.append((canvas, message)))
    monkeypatch.setattr(scope, '_cache_state', lambda: None)
    assert not view.request_canvas_assets('A')
    # Navigation retains the latest demand until repair completes. It must
    # not report the queued navigation as a failed disk read.
    assert view._pending_canvas_demand == 'A'
    assert errors == []
    # The reader itself still refuses an explicitly forced stale read and
    # publishes the original failure contract, with no scene installation.
    assert not loader.request('A')
    assert len(errors) == 1
    assert 'stale' in errors[0][1]
    assert not loader.active
    assert not list(view.scene.iter_assets())


def test_deleted_owner_cancels_and_deleted_signal_delivery_is_safe(qapp):
    from PyQt6 import QtCore, sip
    from prism.paged_canvas_loader import _Signals, _deliver
    owner = QtCore.QObject()
    loader = PagedCanvasLoader(owner)
    cancel = loader.cancel_event
    sip.delete(owner)
    assert cancel.is_set()
    assert loader.owner_gone
    assert not loader.request('A')
    signals = _Signals()
    sip.delete(signals)
    # A worker result arriving during destruction must not escape into Qt.
    _deliver(signals, ('unused',), threading.Event())
