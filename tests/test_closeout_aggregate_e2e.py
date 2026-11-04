"""Small real-project open/save/aggregate integration; no injected result model."""
import hashlib
import json
from pathlib import Path
import sqlite3
import threading

from tests.test_zz_deleg_b1_open_scope import build_project, _page


EVIDENCE = Path('artifacts/closeout-20261023/aggregate')


def ids(browser):
    return {int(item.save_id) for item in browser.model.items}


def wait_result(qtbot, browser, expected):
    qtbot.waitUntil(lambda: ids(browser) == expected
                    and browser._catalog_pending is None, timeout=15000)


def test_actual_async_open_aggregate_save_and_rebuild(main_window, qtbot,
                                                     monkeypatch, tmp_path):
    from prism import thumbnail_cache
    from prism.project_open_scope import ProjectOpenScope

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    project = tmp_path / 'aggregate-e2e.prism'
    pages = [_page('A', 'First', 0), _page('B', 'Second', 1)]
    rows = [dict(page_id=('A' if i < 100 else 'B' if i < 115 else 'default-canvas'),
                 filename=f'original-{i}.png', tags=['tag'] if i % 2 else [],
                 notes=f'note-{i}') for i in range(125)]
    build_project(project, pages, rows)
    with sqlite3.connect(project) as db:
        all_ids = {r[0] for r in db.execute('SELECT id FROM items')}
        original_hashes = {r[0]: hashlib.sha256(r[1]).hexdigest()
                           for r in db.execute('SELECT item_id,data FROM sqlar')}
    unclassified = set(range(116, 126))
    untagged = {i + 1 for i in range(125) if i % 2 == 0}
    cache_root = tmp_path / 'cache'
    monkeypatch.setattr(thumbnail_cache, 'cache_directory',
                        lambda: str(cache_root / 'thumbs'))
    cache = cache_root / 'catalog' / (project.name + '.sqlite')
    cache.parent.mkdir(parents=True)
    assert ProjectOpenScope(project, cache).build().complete
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')

    window, view = main_window, main_window.view
    browser = window.asset_browser
    assert view.open_from_file(str(project)) is False
    qtbot.waitUntil(lambda: getattr(view.scene, '_partial_project_session', None)
                    is not None and not view._paged_preparing, timeout=15000)
    qtbot.waitUntil(lambda: not getattr(view, '_paged_canvas_loader', None)
                    or not view._paged_canvas_loader.active, timeout=10000)
    assert window.current_page_id == 'A'
    assert window.tabs.currentWidget() is window.canvas_page
    assert len(list(view.scene.iter_assets())) == 100

    observed = {'initial_page': window.current_page_id,
                'initial_loaded': len(list(view.scene.iter_assets())),
                'source': 'generated isolated SQLite fixture; no user files',
                'entry': 'view.open_from_file -> actual async reader/session'}
    for mode, expected in [('all', all_ids), ('unclassified', unclassified),
                           ('untagged', untagged)]:
        browser.open(mode)
        wait_result(qtbot, browser, expected)
        assert browser.scope_pages is None
        observed[mode] = sorted(ids(browser))
    browser.open('all')
    wait_result(qtbot, browser, all_ids)
    from prism.project_catalog_index import ProjectCatalogIndex
    original_build = ProjectCatalogIndex.build
    entered, release = threading.Event(), threading.Event()

    def held_build(index, *args, **kwargs):
        entered.set()
        if not release.wait(10):
            raise RuntimeError('test did not release catalog rebuild')
        return original_build(index, *args, **kwargs)

    monkeypatch.setattr(ProjectCatalogIndex, 'build', held_build)
    loaded = next(item for item in view.scene.iter_assets() if item.save_id == 1)
    loaded.notes = 'saved through actual do_save'
    view.mark_content_dirty()
    view.do_save(str(project), create_new=False, silent=True)
    try:
        qtbot.waitUntil(lambda: not view.worker.isRunning()
                        and getattr(view, '_save_partial_snapshot', None) is None,
                        timeout=15000)
        qtbot.waitUntil(entered.is_set, timeout=5000)
        # Allow the normal 180ms scene debounce to expire BEFORE rebuilding.
        # A stray scheduled refresh must not make a broken completion notice pass.
        qtbot.wait(400)
        assert not view._open_scope.ready
        assert ids(browser) == set(), 'stale loaded-page items must not impersonate all assets'
        observed['during_rebuild_ids'] = sorted(ids(browser))
        observed['during_rebuild_empty'] = browser.empty.text()
    finally:
        release.set()
    with sqlite3.connect(project) as db:
        assert json.loads(db.execute('SELECT data FROM items WHERE id=1').fetchone()[0])['notes'] == loaded.notes
        assert {r[0]: hashlib.sha256(r[1]).hexdigest()
                for r in db.execute('SELECT item_id,data FROM sqlar')} == original_hashes
        assert {r[0] for r in db.execute('SELECT id FROM items')} == all_ids
    qtbot.waitUntil(lambda: view._open_scope.ready, timeout=15000)
    # Deliberately DO NOT refresh/reopen/query the browser here. The catalog
    # completion notification itself must republish the whole-project result.
    try:
        wait_result(qtbot, browser, all_ids)
    finally:
        observed['after_save_ids'] = sorted(ids(browser))
        observed['after_save_catalog_ready'] = view._open_scope.ready
        observed['after_save_summary'] = browser.summary.text()
        observed['after_save_empty'] = browser.empty.text()
        (EVIDENCE / 'actual-open-save-rebuild.json').write_text(
            json.dumps(observed, indent=2), encoding='utf-8')
    assert len(list(view.scene.iter_assets())) == 100
