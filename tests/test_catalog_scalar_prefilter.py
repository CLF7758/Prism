from unittest.mock import patch

from prism.catalog_browser import CatalogAssetProxy
from prism.catalog_match_record import CatalogMatchRecord
from tests.test_catalog_browser_partial import prepare, wait_complete
from tests.test_asset_browser import photo


def test_rejected_scalar_rows_never_allocate_display_proxies(main_window, tmp_path, qtbot):
    _scope, _scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=f'Asset {key}',
                             tags=['keep'] if key == 777 else ['other']))
        for key in range(1200)])
    main_window.view.category_panel._active_tags = {'keep'}
    with patch('prism.catalog_browser.CatalogAssetProxy', wraps=CatalogAssetProxy) as construct:
        browser.open('all')
        wait_complete(qtbot, browser, 1)
    assert browser.model.items[0].save_id == 777
    assert construct.call_count == 1
    assert set(browser._catalog_proxy_cache) == {777}


def test_dirty_overlay_can_match_when_disk_summary_does_not(main_window, tmp_path, qtbot):
    _scope, _scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='disk', tags=['other'])),
        (2, 'pixmap', dict(canvasId='A', title='disk two', tags=['keep']))])
    changed = photo(main_window, 'B', 'dirty')
    changed.save_id = 1
    changed._tags = ['keep']
    changed._rating = 5
    removed = photo(main_window, 'A', 'no longer matching')
    removed.save_id = 2
    removed._tags = ['other']
    panel = main_window.view.category_panel
    panel._active_tags = {'keep'}
    panel._min_rating = 4
    browser.open('all')
    wait_complete(qtbot, browser, 1)
    assert browser.model.items == [changed]
    assert browser._catalog_proxy_cache == {}


def test_scalar_predicate_parity_and_asset_only_search_skips_other_domains(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='木头', tags=['材质'], rating=4)),
        (2, 'pixmap', dict(canvasId='missing', title='stone', tags=['  '], rating=2)),
        (3, 'pixmap', dict(canvasId='default-canvas', title='metal', tags=[]))])
    browser.open('all')
    wait_complete(qtbot, browser, 3)
    rows = list(scope.iter_query())
    for mode in ('all', 'unclassified', 'untagged', 'recent'):
        browser.mode = mode
        for row in rows:
            scalar = CatalogMatchRecord(row)
            proxy = CatalogAssetProxy(row, scope.project_path, scene, browser._catalog_display_reader)
            assert browser._catalog_matches(scalar) == browser._catalog_matches(proxy)
    browser.mode = 'all'
    main_window.view.category_panel.search_kind = 'asset'
    browser.search.setText('木头')
    with patch.object(main_window.view.category_panel, 'project_search_results',
                      side_effect=AssertionError('asset-only query searched other domains')):
        browser.refresh()
        wait_complete(qtbot, browser, 1)
    assert browser.model.items[0]._title == '木头'
