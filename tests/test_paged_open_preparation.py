"""Scoped open preparation is asynchronous and cannot rebase edited assets."""
import sqlite3
import json
import threading

from PyQt6 import QtCore, QtWidgets

from prism import paged_open_preparation as preparation
from prism.partial_project import PartialProjectSession
from prism.project_open_scope import ProjectOpenScope
from tests.test_paged_canvas_loader import pages
from tests.test_partial_project_gui import SCHEMA


def configure_open(view, scope, monkeypatch):
    # The shared pages fixture already names this project as open. These
    # cases exercise a fresh open, not the same-project no-reload shortcut.
    view.filename = None
    from prism import thumbnail_cache
    root = scope.cache_path.parent
    monkeypatch.setattr(thumbnail_cache, 'cache_directory', lambda: str(root / 'thumbs'))
    from pathlib import Path
    from prism.view import _catalog_cache_path
    cache = Path(_catalog_cache_path(scope.project_path))
    cache.parent.mkdir(exist_ok=True)
    ProjectOpenScope(scope.project_path, cache).build()
    view.settings.setValue(view._last_canvas_setting_key(str(scope.project_path)), 'A')
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')


def test_scope_and_baseline_read_off_gui_then_attach_on_gui(pages, qtbot, monkeypatch):
    view, scope, _old = pages
    configure_open(view, scope, monkeypatch)
    gui = QtWidgets.QApplication.instance().thread()
    observed = []
    original_resolve = preparation.resolve_scope
    original_open = PartialProjectSession.open
    original_attach = PartialProjectSession.attach
    def resolve(*args):
        observed.append(('scope', QtCore.QThread.currentThread() == gui))
        return original_resolve(*args)
    def open_session(cls, *args, **kwargs):
        observed.append(('baseline', QtCore.QThread.currentThread() == gui))
        return original_open(*args, **kwargs)
    def attach(self, scene):
        observed.append(('attach', QtCore.QThread.currentThread() == gui))
        return original_attach(self, scene)
    monkeypatch.setattr(preparation, 'resolve_scope', resolve)
    monkeypatch.setattr(PartialProjectSession, 'open', classmethod(open_session))
    monkeypatch.setattr(PartialProjectSession, 'attach', attach)
    # A scoped open is not finished when it starts: open_from_file reports
    # "not open yet" so no caller adopts the filename before preparation is
    # known to have succeeded.
    assert view.open_from_file(str(scope.project_path)) is False
    assert view.filename is None
    qtbot.waitUntil(lambda: getattr(view.scene, '_partial_project_session', None) is not None,
                    timeout=15000)
    qtbot.waitUntil(lambda: not view._paged_canvas_loader.active, timeout=10000)
    assert ('scope', False) in observed
    assert ('baseline', False) in observed
    assert ('attach', True) in observed
    assert view.filename == str(scope.project_path)
    assert len(view.scene._partial_project_session.loaded()) == 320
    assert len(list(view.scene.iter_assets())) == 320
    assert view.current_canvas_id == 'A'
    assert not view._paged_preparing


def test_edit_during_background_preparation_remains_dirty_and_save_waits(
        pages, qtbot, monkeypatch, tmp_path):
    view, scope, _old = pages
    configure_open(view, scope, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = preparation.prepare_session
    def slow(*args):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test did not release preparation')
        return original(*args)
    monkeypatch.setattr(preparation, 'prepare_session', slow)
    try:
        view.open_from_file(str(scope.project_path))
        qtbot.waitUntil(entered.is_set, timeout=15000)
        assert any(item.save_id == 1 for item in view.scene.iter_assets()), (
            view._paged_open_target, view._paged_scope,
            [item.save_id for item in view.scene.iter_assets()],
            len(view.scene._viewport_items.records), view.scene.items_to_add.qsize(),
            view._load_scheduler.scene is view.scene, view.scene._item_count)
        item = next(item for item in view.scene.iter_assets() if item.save_id == 1)
        item.notes = 'edited while safe-saving prepared'
        target = tmp_path / 'not-ready.prism'
        worker = view.worker
        view.do_save(str(target), create_new=True)
        assert view.worker is worker
        assert not target.exists()
        assert 'not ready' in view.parent.statusBar().currentMessage()
    finally:
        release.set()
    qtbot.waitUntil(lambda: view.scene._partial_project_session is not None, timeout=15000)
    session = view.scene._partial_project_session
    assert view.ensure_asset_loaded(1) is item
    assert item.notes == 'edited while safe-saving prepared'
    snapshot = session.capture(view.scene)
    change = next(change for change in snapshot.changes if change.table == 'items' and change.key == 1)
    assert json.loads(change.after[-1])['notes'] == 'edited while safe-saving prepared'


def test_source_change_after_preparation_rejects_attach(pages, qtbot, monkeypatch):
    view, scope, _old = pages
    configure_open(view, scope, monkeypatch)
    entered, release = threading.Event(), threading.Event()
    original = preparation.prepare_session
    def hold_result(*args):
        result = original(*args)
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test did not release result')
        return result
    monkeypatch.setattr(preparation, 'prepare_session', hold_result)
    try:
        view.open_from_file(str(scope.project_path))
        qtbot.waitUntil(entered.is_set, timeout=15000)
        with sqlite3.connect(scope.project_path) as db:
            db.execute('UPDATE items SET x=987 WHERE id=1')
    finally:
        release.set()
    qtbot.waitUntil(lambda: not view._paged_preparing, timeout=15000)
    assert view.scene._partial_project_session is None
    assert view.scene._partial_project_source
    assert 'changed' in view.parent.statusBar().currentMessage()


def test_clear_cancels_preparation_and_cannot_attach_to_new_project(pages, qtbot):
    view, scope, old = pages
    prepare = view._paged_preparer()
    before = view.scene.project_id
    prepare.start('session', lambda: old)
    view.clear_scene()
    assert prepare.pool.waitForDone(5000)
    qtbot.wait(30)
    assert view.scene.project_id != before
    assert view.scene._partial_project_session is None


def test_first_open_reads_source_scope_without_building_missing_catalog(pages, monkeypatch):
    view, scope, _ = pages
    scope.cache_path.unlink()
    def forbidden(*args, **kwargs):
        raise AssertionError('resolve_scope must not build a catalog')
    monkeypatch.setattr(ProjectOpenScope, 'build', forbidden)
    result = preparation.resolve_scope(str(scope.project_path), str(scope.cache_path), 'B', 'B')
    assert result['pending']['project'] == str(scope.project_path)
    assert result['pending']['cache'] == str(scope.cache_path)
    assert result['pending']['reason']
    assert not result['scope'].ready
    assert result['scope'].total() is None
    assert not scope.cache_path.exists()
    assert result['target'] == 'A'
    with sqlite3.connect(scope.project_path) as db:
        expected = tuple(row[0] for row in db.execute(
            "SELECT id FROM items WHERE json_extract(data,'$.canvasId')='A' ORDER BY id LIMIT 256"))
    assert result['ids'] == expected


def test_cancelled_first_catalog_never_returns_full_read_sentinel(pages):
    import pytest
    view, scope, _ = pages
    scope.cache_path.unlink()
    with pytest.raises(RuntimeError, match='cancelled'):
        preparation.resolve_scope(str(scope.project_path), str(scope.cache_path), 'A', 'A', lambda: True)


def build_mixed_project(path, rows):
    """Write a project whose items cover several kinds on several canvases.

    ``rows`` is a sequence of ``(item_id, kind, canvas_id)``. The catalog is
    built from the real ``items`` table, so the kinds here are the kinds the
    scope has to cope with.
    """
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.execute('INSERT INTO prism_metadata VALUES(?,?)', ('project_id', 'mixed'))
        for canvas in sorted({row[2] for row in rows}):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (canvas, 'canvas', canvas, '{}', 0))
        for key, kind, canvas in rows:
            extra = {'canvasId': canvas, 'imageWidth': 16, 'imageHeight': 16,
                     'filename': '%d.png' % key}
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 1, json.dumps(extra)))
    return path


def kinds_of(scope, ids):
    """``{id: kind}`` for a scope, read back in the catalog's own batches.

    ``ProjectOpenScope.summaries`` refuses more than 1024 ids per call, so
    the scope has to be read back in batches — the same requirement the SQL
    reader has.
    """
    result = {}
    ids = tuple(ids)
    for start in range(0, len(ids), 512):
        for row in scope.summaries(ids[start:start + 512]):
            result[row['id']] = row['kind']
    return result


def test_scope_carries_every_non_pixmap_entity_of_the_target_canvas(tmp_path):
    rows = [(key, 'pixmap', 'A') for key in range(1, 301)]
    rows += [(301, 'text', 'A'), (302, 'path', 'A'),
             (303, 'video', 'A'), (304, 'glb', 'A')]
    # The other canvas carries pixmaps of its own: a scope that leaked them
    # would still look right if that canvas only held annotations.
    rows += [(305, 'text', 'B'), (306, 'glb', 'B'),
             (307, 'pixmap', 'B'), (308, 'pixmap', 'B')]
    source = build_mixed_project(tmp_path / 'mixed.prism', rows)
    assert ProjectOpenScope(source, tmp_path / 'mixed.sqlite').build().complete
    result = preparation.resolve_scope(str(source), str(tmp_path / 'mixed.sqlite'),
                                        'A', 'A')
    ids = result['ids']
    assert result['target'] == 'A'
    # Ascending, without duplicates: downstream keyset paging feeds the last
    # id of a page back as ``after_id``.
    assert ids == tuple(sorted(ids))
    assert len(set(ids)) == len(ids)
    kinds = kinds_of(result['scope'], ids)
    # Every non-pixmap entity of the target canvas is in the scope, and the
    # scope holds nothing else: one page of pixmaps plus those entities.
    assert set(ids) == set(range(1, 257)) | {301, 302, 303, 304}
    assert kinds[301] == 'text'
    assert kinds[302] == 'path'
    assert kinds[303] == 'video'
    assert kinds[304] == 'glb'
    # The other canvas stays out of this canvas' scope.
    assert 305 not in ids
    assert 306 not in ids
    assert 307 not in ids
    assert 308 not in ids
    # The pixmap part is still one page.
    assert sum(1 for key in ids if kinds[key] == 'pixmap') == 256


def test_scope_pages_through_more_than_one_batch_of_a_kind(tmp_path):
    rows = [(key, 'pixmap', 'A') for key in range(1, 4)]
    rows += [(key, 'text', 'A') for key in range(1000, 2500)]
    source = build_mixed_project(tmp_path / 'many.prism', rows)
    result = preparation.resolve_scope(str(source), str(tmp_path / 'many.sqlite'),
                                        'A', 'A')
    ids = result['ids']
    # 1500 text rows need more than one 1024-row batch; none of them may be
    # dropped on the way.
    assert len(ids) == 1503
    assert ids == tuple(sorted(ids))
    assert {key for key in range(1000, 2500)} <= set(ids)


def test_ready_catalog_open_reports_only_the_scope_page(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(preparation, 'trace_record',
                        lambda kind, **fields: events.append((kind, fields)))
    rows = [(key, 'pixmap', 'A') for key in range(1, 301)]
    rows += [(301, 'text', 'A'), (302, 'glb', 'A')]
    source = build_mixed_project(tmp_path / 'traced.prism', rows)
    cache = str(tmp_path / 'traced.sqlite')
    assert ProjectOpenScope(source, cache).build().complete
    events.clear()
    result = preparation.resolve_scope(str(source), cache, 'A', 'A')
    reported = [fields for kind, fields in events
                if kind == 'paged_prepare_processed']
    current = [fields for kind, fields in events
               if kind == 'paged_scope_prepared']
    # One new event plus one explicitly labelled legacy mirror describe the
    # same scope identity and semantics.
    assert len(reported) == len(current) == 1
    assert reported[0]['phase'] == 'scope'
    assert reported[0]['legacy'] is True
    assert reported[0]['open_id'] == current[0]['open_id']
    # The page counter never grows past one page of pixmaps ...
    assert reported[0]['processed'] == 256
    assert reported[0]['processed'] <= preparation.PAGE_LIMIT
    # ... while the size of the scope itself is reported honestly.
    assert reported[0]['scope_ids'] == len(result['ids']) == 258
    assert reported[0]['total_records'] == 258
    assert reported[0]['total_known'] is True
    assert reported[0]['pixmap_count'] == 256
    assert reported[0]['non_pixmap_count'] == 2
    assert reported[0]['kinds'] == ['glb', 'text']


def test_missing_catalog_scope_trace_is_not_catalog_build_progress(tmp_path, monkeypatch):
    events, progress = [], []
    monkeypatch.setattr(preparation, 'trace_record',
                        lambda kind, **fields: events.append((kind, fields)))
    def forbidden(*args, **kwargs):
        raise AssertionError('scope selection must not build')
    monkeypatch.setattr(ProjectOpenScope, 'build', forbidden)
    rows = [(key, 'pixmap', 'A') for key in range(1, 301)]
    source = build_mixed_project(tmp_path / 'built.prism', rows)
    result = preparation.resolve_scope(str(source), str(tmp_path / 'built.sqlite'),
                                       'A', 'A', progress=progress.append)
    assert progress == []
    assert result['scope'].counts() is None
    assert not any(kind == 'paged_catalog_build_progress' or
                   fields.get('phase') == 'catalog_build' for kind, fields in events)
    selected = next(fields for kind, fields in events
                    if kind == 'project.open.scope_selected')
    assert selected['total_records'] is None
    assert selected['total_known'] is False
    scope = next(fields for kind, fields in events if kind == 'paged_scope_prepared')
    assert scope['scope_ids'] == 256
    assert scope['pixmap_count'] == 256
    assert scope['total_records'] is None
    assert scope['total_known'] is False
    scan = next(fields for kind, fields in events if kind == 'paged_source_scope_scan')
    assert scan['metadata_rows'] == 300
    assert scan['bounded_io'] is False


def test_scope_over_the_sql_binding_budget_is_reported_not_trimmed(monkeypatch):
    events = []
    monkeypatch.setattr(preparation, 'trace_record',
                        lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(preparation, '_sql_variable_limit', lambda: 999)
    preparation._report_scope_binding_budget(400)
    reported = dict(events)
    assert 'paged_prepare_scope_over_sql_limit' in reported
    fields = reported['paged_prepare_scope_over_sql_limit']
    assert fields['scope_ids'] == 400
    assert fields['bindings'] == 400 * 3
    assert fields['variable_limit'] == 999


def test_scope_over_the_sql_binding_budget_is_reported_and_not_trimmed(tmp_path, monkeypatch):
    """A scope that outgrows the SQL binding budget is reported, never cut.

    Dropping the tail of the scope would be exactly the "the canvas lost
    data" failure this scope exists to prevent, so the reader side has to
    chunk instead.
    """
    events = []
    monkeypatch.setattr(preparation, 'trace_record',
                        lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(preparation, '_sql_variable_limit', lambda: 999)
    rows = [(key, 'pixmap', 'A') for key in range(1, 301)]
    rows += [(400 + key, 'text', 'A') for key in range(100)]
    source = build_mixed_project(tmp_path / 'budget.prism', rows)
    result = preparation.resolve_scope(str(source), str(tmp_path / 'budget.sqlite'),
                                        'A', 'A')
    ids = result['ids']
    # 256 pixmaps + 100 annotations = 356 ids, 1068 bindings at 3 per id.
    assert len(ids) == 356
    assert {400 + key for key in range(100)} <= set(ids)
    over = dict(events).get('paged_prepare_scope_over_sql_limit')
    assert over is not None
    assert over['scope_ids'] == 356
    assert over['bindings'] == 356 * 3
    assert over['variable_limit'] == 999


def test_every_scope_id_is_resolvable_by_the_reader_side(tmp_path):
    """The reader side must be able to look up the kind of every scope id.

    The scope is not pixmap-only, so the installer has to be able to ask
    the catalog what each id is -- in batches of at most 1024 ids, which is
    what ``summaries`` accepts.
    """
    rows = [(key, 'pixmap', 'A') for key in range(1, 301)]
    rows += [(301, 'text', 'A'), (302, 'path', 'A'),
             (303, 'video', 'A'), (304, 'glb', 'A')]
    source = build_mixed_project(tmp_path / 'resolvable.prism', rows)
    assert ProjectOpenScope(source, tmp_path / 'resolvable.sqlite').build().complete
    result = preparation.resolve_scope(str(source), str(tmp_path / 'resolvable.sqlite'),
                                        'A', 'A')
    ids = result['ids']
    scope = result['scope']
    seen = {}
    for start in range(0, len(ids), 1024):
        batch = ids[start:start + 1024]
        assert len(batch) <= 1024
        for row in scope.summaries(batch):
            seen[row['id']] = row['kind']
    assert set(seen) == set(ids)
    assert seen[301] == 'text'
    assert seen[302] == 'path'
    assert seen[303] == 'video'
    assert seen[304] == 'glb'


def test_project_without_pixmaps_scopes_its_first_legal_canvas(tmp_path):
    """A canvas without a single pixmap is still a legal landing canvas.

    It is not an empty scope either: the landing canvas is the first legal
    canvas in project order and the scope carries every entity that canvas
    holds, so the canvas comes up with its content.
    """
    rows = [(301, 'text', 'A'), (302, 'glb', 'A')]
    source = build_mixed_project(tmp_path / 'nopixmap.prism', rows)
    result = preparation.resolve_scope(str(source), str(tmp_path / 'nopixmap.sqlite'),
                                        'A', 'A')
    assert result['target'] == 'A'
    assert result['ids'] == (301, 302)
    assert result['pending'] is not None
    assert not result['scope'].ready


def test_project_without_any_entity_falls_back_to_the_full_reader(tmp_path):
    """There is nothing to scope at all: the old full read is used.

    An empty scope would leave the project looking empty, so the reader
    falls back to reading everything.
    """
    source = build_mixed_project(tmp_path / 'empty.prism', [])
    result = preparation.resolve_scope(str(source), str(tmp_path / 'empty.sqlite'),
                                        'A', 'A')
    assert result['ids'] is None
    assert result['target'] is None
    assert result['pending'] is not None
    assert not result['scope'].ready


def test_cancelling_the_scope_read_is_not_a_project_failure(tmp_path):
    import pytest
    source = build_mixed_project(tmp_path / 'cancel.prism',
                                 [(key, 'pixmap', 'A') for key in range(1, 4)])
    cache = str(tmp_path / 'cancel.sqlite')
    assert ProjectOpenScope(source, cache).build().complete
    with pytest.raises(preparation.PreparationCancelled):
        preparation.resolve_scope(str(source), cache, 'A', 'A', cancel=lambda: True)


def test_catalog_build_progress_has_stable_identity_and_legacy_mirror(monkeypatch):
    events = []
    monkeypatch.setattr(preparation, 'trace_record',
                        lambda kind, **fields: events.append((kind, fields)))
    preparation._catalog_build_progress(
        build_id='build-1', processed=50000, processed_this_run=None,
        total_records=None, total_known=False, final=False)
    preparation._catalog_build_progress(
        build_id='build-1', processed=100000, processed_this_run=50000,
        total_records=100000, total_known=True, final=True)
    current = [fields for kind, fields in events
               if kind == 'paged_catalog_build_progress']
    legacy = [fields for kind, fields in events
              if kind == 'paged_prepare_processed']
    assert [item['processed'] for item in current] == [50000, 100000]
    assert all(item['build_id'] == 'build-1' for item in current + legacy)
    assert all(item['phase'] == 'catalog_build' and item['legacy'] is True
               for item in legacy)
    assert current[-1]['total_records'] == 100000
    assert current[-1]['total_known'] is True and current[-1]['final'] is True
