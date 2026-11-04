"""The real aggregate browser includes assets outside the loaded canvas."""
from unittest.mock import patch
import pytest

from PyQt6 import QtCore

from prism.project_catalog import ProjectAssetCatalog
from tests.test_catalog_keyset_overlay import scope_for
from tests.test_asset_browser import photo


def prepare(window, tmp_path, records):
    scope = scope_for(tmp_path, records)
    scene = window.view.scene
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(id='A', kind='canvas', title='A'),
                             dict(id='B', kind='canvas', title='B')]
    library = window.view.category_panel
    library.catalog = ProjectAssetCatalog(scene, library.resource_service, scope_provider=lambda: scope)
    return scope, scene, window.asset_browser


def wait_complete(qtbot, browser, expected, *, images_only=False):
    qtbot.waitUntil(lambda: browser._catalog_pending is None and
                   (sum(not isinstance(item, dict) for item in browser.model.items) if images_only else browser.model.rowCount()) == expected, timeout=15000)


def test_formal_browser_queries_all_unloaded_summaries_without_scene_install(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(key, 'pixmap', dict(canvasId='A' if key < 500 else 'B', title=f'Asset {key}',
                              imageWidth=100, imageHeight=50, tags=[])) for key in range(1200)])
    before = list(scene.iter_assets())
    with patch.object(scope.index, 'details', side_effect=AssertionError('full details read')):
        browser.open('all')
        wait_complete(qtbot, browser, 1200, images_only=True)
    assert list(scene.iter_assets()) == before
    assert all(getattr(item, '_catalog_proxy', False) for item in browser.model.items if not isinstance(item, dict))
    first = next(item for item in browser.model.items if not isinstance(item, dict))
    assert first._prism_source == (str(scope.project_path), 0)
    # Same projection and proxies are retained across repeat opens.
    browser.open('all')
    wait_complete(qtbot, browser, 1200, images_only=True)
    assert next(item for item in browser.model.items if not isinstance(item, dict)) is first


def test_untagged_unclassified_and_current_dirty_overlay(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='old', tags=[])),
        (2, 'pixmap', dict(canvasId='B', title='two', tags=['tag'])),
        (3, 'pixmap', dict(canvasId='missing', title='orphan', tags=[])),
        (4, 'pixmap', dict(canvasId='default-canvas', title='default', tags=[]))])
    live = photo(main_window, 'B', 'edited')
    live.save_id = 1
    live._tags = ['edited tag']
    browser.open('all')
    wait_complete(qtbot, browser, 4, images_only=True)
    assert live in browser.model.items
    assert sum(getattr(item, 'save_id', None) == 1 for item in browser.model.items) == 1
    browser.open('untagged')
    wait_complete(qtbot, browser, 2, images_only=True)
    assert {item.save_id for item in browser.model.items if not isinstance(item, dict)} == {3, 4}
    browser.open('unclassified')
    wait_complete(qtbot, browser, 2, images_only=True)
    assert {item.save_id for item in browser.model.items if not isinstance(item, dict)} == {3, 4}
    assert list(scene.iter_assets()) == [live]


def test_proxy_selection_inspects_without_loading_and_multi_selection_never_partially_edits(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='one')),
        (2, 'pixmap', dict(canvasId='B', title='two'))])
    browser.open('all')
    wait_complete(qtbot, browser, 2, images_only=True)
    proxies = [item for item in browser.model.items if not isinstance(item, dict)]
    loaded = photo(main_window, 'A', 'actual')
    loaded.save_id = 1
    browser.timer.stop()
    with patch.object(main_window.view, 'ensure_asset_loaded', return_value=loaded) as loader:
        browser.list.selectionModel().select(browser.model.index(browser.model.items.index(proxies[0])),
            QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        loader.assert_not_called()
        assert not loaded.isSelected()
        browser.list.selectionModel().select(browser.model.index(browser.model.items.index(proxies[1])),
            QtCore.QItemSelectionModel.SelectionFlag.Select)
        assert not scene.selectedItems(user_only=True)
        loader.assert_not_called()
    assert all(item not in list(scene.iter_assets()) for item in proxies)


def test_visible_metadata_reads_only_requested_small_projection(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(imageWidth=100, imageHeight=50,
                          displayOverview=[1, 1, 'AAAAAA=='], crop=[1, 2, 30, 20])),
                                 (2, 'pixmap', dict(title='unrequested'))])
    result = scope.display_summaries([1])
    assert len(result) == 1 and result[0]['id'] == 1
    assert result[0]['crop'] == [1, 2, 30, 20]
    assert 'data' not in result[0] and 'title' not in result[0]


def test_visible_proxy_hydrates_real_thumbnail_without_reading_original(main_window, tmp_path, qtbot, monkeypatch):
    import base64
    from PyQt6 import QtGui
    image = QtGui.QImage(64, 32, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#e00000'))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    thumbnail = [64, 32, base64.b64encode(bytes(buffer.data())).decode('ascii')]
    scope, scene, browser = prepare(main_window, tmp_path, [(1, 'pixmap',
        dict(canvasId='A', title='red', imageWidth=64, imageHeight=32,
             displayThumbnail=thumbnail, crop=[0, 0, 32, 32], opacity=0.35,
             grayscale=False))])
    monkeypatch.setattr('prism.source_store.read_archive',
                        lambda *args: (_ for _ in ()).throw(AssertionError('original read')))
    browser.open('all')
    wait_complete(qtbot, browser, 1, images_only=True)
    proxy = next(item for item in browser.model.items if not isinstance(item, dict))
    qtbot.waitUntil(lambda: proxy._catalog_display_ready, timeout=10000)
    qtbot.waitUntil(lambda: proxy._preview_pixmap is not None, timeout=10000)
    assert proxy._preview_pixmap.toImage().pixelColor(20, 10).red() > 200
    assert proxy._crop == QtCore.QRectF(0, 0, 32, 32)
    assert proxy.opacity() == 0.35
    assert not proxy.grayscale
    assert browser.list._catalog_geometry_rows[id(proxy)] == browser.model.items.index(proxy)
    browser.list.selectionModel().select(browser.model.index(browser.model.items.index(proxy)),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    inspector = main_window.view._detail_panel
    assert inspector._scroll.isVisible() and inspector._collection.isHidden()
    assert inspector._preview_label.pixmap() is not None
    assert not inspector._preview_label.pixmap().isNull()
    assert inspector._hist_widget._stats['total'] > 0
    assert inspector._title_edit.text() == 'red'
    assert list(scene.iter_assets()) == []


def test_stale_directory_callback_cannot_install_old_project(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [(1, 'pixmap', dict(canvasId='A'))])
    browser.open('all')
    wait_complete(qtbot, browser, 3)  # Image plus the two root canvas resources.
    previous = list(browser.model.items)
    browser._catalog_rows_ready(((browser._catalog_generation - 1, scene.project_id), [], None))
    assert browser.model.items == previous
    with patch.object(main_window.view, 'ensure_asset_loaded') as loader:
        proxy = next(item for item in previous if not isinstance(item, dict))
        proxy._catalog_project_id = 'old-project'
        browser._locate_item(proxy, scene.project_id)
        loader.assert_not_called()


def test_unchanged_scope_skips_filter_and_install_but_metadata_invalidates(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=f'item{key}')) for key in range(400)])
    browser.open('all')
    wait_complete(qtbot, browser, 400, images_only=True)
    with patch.object(browser, '_catalog_matches', wraps=browser._catalog_matches) as matches:
        browser.open('all')
        assert matches.call_count == 0
        scene.metadata_changed.emit()
        browser.timer.stop()
        browser.open('all')
        qtbot.waitUntil(lambda: browser._catalog_pending is None, timeout=15000)
        assert matches.call_count == 400


def test_partial_project_resource_scopes_include_unloaded_images_and_root_mindmap(
        main_window, tmp_path, qtbot, isolate_recent_usage):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='Unloaded image', tags=[])),
        (2, 'pixmap', dict(canvasId='B', title='Deleted image',
                           deletedAt='2026-11-02T01:00:00Z'))])
    panel = main_window.view.category_panel
    brain = dict(id='root-brain', kind='mindmap', title='Root mind map', tags=[])
    scene.workspace_pages.append(brain)
    panel.resource_service.ensurePages(scene.workspace_pages)
    material = panel.material_service.create(None, name='Material')
    scene.model_records.append(dict(id='root-model', name='Model', folder_id=None,
                                   tags=[], notes='', files=[], format='fbx', deleted_at=None))
    isolate_recent_usage.record(scene.project_id, 'mindmap', 'root-brain', brain['title'])
    pages = {'page:A', 'page:B', 'page:root-brain'}
    others = pages | {'material:' + material['id'], 'model:root-model'}
    expected = {'all': others | {'asset:1'}, 'unclassified': others,
                'untagged': others | {'asset:1'}, 'recent': {'page:root-brain'},
                'trash': {'asset:2'}}
    def identities():
        return {value['result_id'] if isinstance(value, dict) else 'asset:' + str(value.save_id)
                for value in browser.model.items}
    with patch.object(scope.index, 'details', side_effect=AssertionError('original details read')):
        for mode, wanted in expected.items():
            browser.open(mode)
            qtbot.waitUntil(lambda: browser._catalog_pending is None and identities() == wanted,
                            timeout=15000)
            panel._update_navigation()
            assert panel._nav_counts[mode].text() == str(len(wanted))
    assert list(scene.iter_assets()) == [], 'directory display must not load editable originals'


def test_mixed_scope_revisit_reuses_complete_result_without_row_install(
        main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='One', tags=[])),
        (2, 'pixmap', dict(canvasId='B', title='Two', tags=['tag']))])
    scene.workspace_pages.append(dict(id='brain', kind='mindmap', title='Brain', tags=[]))
    panel = main_window.view.category_panel
    panel.resource_service.ensurePages(scene.workspace_pages)
    browser.open('all')
    wait_complete(qtbot, browser, 5)
    original = list(browser.model.items)
    browser.open('untagged')
    wait_complete(qtbot, browser, 4)
    monkeypatch.setattr(browser, '_install_catalog_rows',
                        lambda *_: (_ for _ in ()).throw(AssertionError('repeated full row scan')))
    browser.open('all')
    wait_complete(qtbot, browser, 5)
    assert browser.model.items == original
    assert any(isinstance(item, dict) and item['page_id'] == 'brain'
               for item in browser.model.items)


def test_resource_counts_are_reused_between_five_scope_buttons(
        main_window, tmp_path, qtbot, monkeypatch):
    from prism import project_resources
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='One', tags=[]))])
    panel = main_window.view.category_panel
    panel._resource_counts_cache = None
    with patch.object(project_resources, 'scope_counts', wraps=project_resources.scope_counts) as count:
        first = panel._resource_scope_counts()
        initial_calls = count.call_count
        for mode in project_resources.SCOPES:
            browser.mode = mode
            assert panel._resource_scope_counts() == first
        assert count.call_count == initial_calls


def test_all_five_mixed_scopes_revisit_without_collecting_or_installing_rows(
        main_window, tmp_path, qtbot, isolate_recent_usage):
    from prism import project_resources
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='missing', title='One', tags=[])),
        (2, 'pixmap', dict(canvasId='B', title='Two', tags=['tag'])),
        (3, 'pixmap', dict(canvasId='B', title='Deleted', deletedAt='2026-11-02'))])
    brain = dict(id='brain', kind='mindmap', title='Brain', tags=[])
    scene.workspace_pages.append(brain)
    main_window.view.category_panel.resource_service.ensurePages(scene.workspace_pages)
    isolate_recent_usage.record(scene.project_id, 'mindmap', 'brain', 'Brain')
    expected_counts = {'all': 5, 'unclassified': 4, 'untagged': 4, 'recent': 1, 'trash': 1}
    results = {}
    for mode, count in expected_counts.items():
        browser.open(mode)
        wait_complete(qtbot, browser, count)
        results[mode] = list(browser.model.items)
    with patch.object(project_resources, 'collect', side_effect=AssertionError('repeated collection')), \
            patch.object(browser, '_install_catalog_rows', side_effect=AssertionError('repeated row scan')):
        for mode, count in expected_counts.items():
            browser.open(mode)
            wait_complete(qtbot, browser, count)
            assert browser.model.items == results[mode]


def test_first_trash_query_evaluates_only_deleted_directory_rows(main_window, tmp_path, qtbot):
    records = [(key, 'pixmap', dict(canvasId='A', title=str(key))) for key in range(2000)]
    records += [(3000 + key, 'pixmap', dict(canvasId='A', title='Deleted',
                 deletedAt='2026-11-02T00:00:00Z')) for key in range(5)]
    scope, scene, browser = prepare(main_window, tmp_path, records)
    with patch.object(browser, '_catalog_matches', wraps=browser._catalog_matches) as matches:
        browser.open('trash')
        wait_complete(qtbot, browser, 5)
        assert matches.call_count == 5
        assert len(browser._catalog_scalar_cache[1]) == 5
        assert browser._catalog_pushdown_filters() == {'include_deleted': True}


@pytest.mark.parametrize('mode,image_count,result_count', [
    ('untagged', 7, 9), ('unclassified', 5, 7), ('recent', 80, 80)])
def test_first_scoped_query_reads_candidates_not_all_assets(
        main_window, tmp_path, qtbot, mode, image_count, result_count):
    records = [(key, 'pixmap', dict(canvasId='A', title=str(key), tags=['tag']))
               for key in range(2000)]
    records += [(3000 + key, 'pixmap', dict(canvasId='A', title='Untagged', tags=[]))
                for key in range(7)]
    records += [(4000 + key, 'pixmap', dict(canvasId='missing', title='Unclassified', tags=['tag']))
                for key in range(5)]
    records += [(5000 + key, 'pixmap', dict(canvasId='B', title='Recent', tags=['tag'],
                 lastUsed=1793600000 + key)) for key in range(80)]
    scope, scene, browser = prepare(main_window, tmp_path, records)
    # Sidebar counts must use the existing aggregates, never catalog.query(all).
    with patch.object(browser.catalog, 'query', side_effect=AssertionError('unfiltered asset traversal')), \
            patch.object(browser, '_catalog_matches', wraps=browser._catalog_matches) as matches:
        browser.open(mode)
        wait_complete(qtbot, browser, result_count)
        assert matches.call_count == image_count
        assert len(browser._catalog_scalar_cache[1]) == image_count
        if mode == 'recent':
            assert {item.save_id for item in browser.model.items} == set(range(5000, 5080))
        main_window.view.category_panel._update_navigation()
        assert main_window.view.category_panel._nav_counts[mode].text() == str(result_count)


def test_empty_unclassified_directory_still_shows_unclassified_pages(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', tags=['tag']))])
    browser.open('unclassified')
    wait_complete(qtbot, browser, 2)
    assert all(isinstance(item, dict) for item in browser.model.items)


def test_recent_query_refills_after_unsaved_overlay_removes_newest_item(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', lastUsed=1793600000 + key)) for key in range(80)])
    live = photo(main_window, 'A', 'No longer recent')
    live.save_id = 79
    live._last_used = ''
    browser.open('recent')
    wait_complete(qtbot, browser, 79)
    assert {item.save_id for item in browser.model.items} == set(range(79))



def test_folder_query_reads_only_its_canvas_candidates(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(key, 'pixmap', dict(canvasId='B' if key < 7 else 'A', title=f'Asset {key}',
                              imageWidth=100, imageHeight=50, tags=[])) for key in range(100)])
    with patch.object(scope, 'iter_query', wraps=scope.iter_query) as queries:
        browser.open('all', {'B'}, 'Folder B')
        wait_complete(qtbot, browser, 7)
        assert queries.call_args_list
        assert all(call.kwargs.get('canvas_ids') == ('B',) for call in queries.call_args_list
                   if not call.kwargs.get('recent_only')), queries.call_args_list
        assert all(item._canvas_id == 'B' for item in browser.model.items)
    browser.open('all', set(), 'Empty folder')
    wait_complete(qtbot, browser, 0)


def test_selecting_catalog_asset_inspects_location_without_scene_load_or_scope_reset(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(key, 'pixmap', dict(canvasId='B', title=f'Asset {key}',
                              imageWidth=100, imageHeight=50, tags=[])) for key in range(3)])
    library = main_window.view.category_panel
    library.resource_service.ensurePages(scene.workspace_pages)
    folder = library.resource_service.createFolder('canvas', 'Parent folder')
    node = next(n for n in library.resource_service.nodes if n.get('pageId') == 'B')
    library.resource_service.moveBatch([node['id']], folder['id'], 'canvas')
    browser.open('all', {'B'}, 'Folder B')
    wait_complete(qtbot, browser, 3)
    with patch.object(main_window.view, 'ensure_asset_loaded', side_effect=AssertionError('selection loaded original')):
        browser.list.selectionModel().select(browser.model.index(1),
            QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        assert browser.scope_pages == {'B'}
        panel = main_window.view._detail_panel
        assert panel._folder_button.text() == 'Parent folder / B'
        assert panel._scroll.isVisible() and panel._collection.isHidden()
        assert panel._title_edit.text() == 'Asset 1'
        assert panel._notes_edit.isEnabled()
        assert panel._rating_buttons[0].isEnabled()
        browser.show_scope_info(browser.model.items)
        browser.refresh()
        qtbot.wait(50)
        assert panel._scroll.isVisible() and panel._collection.isHidden()
        assert panel._item is browser.model.items[1]
    with patch.object(browser, '_locate_item') as locate:
        panel._folder_button.click()
        locate.assert_called_once_with(browser.model.items[1], scene.project_id)



def test_full_catalog_inspector_edits_resolve_the_canonical_asset(main_window, tmp_path, qtbot):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(1, 'pixmap', dict(canvasId='A', title='Original', notes='Original note'))])
    browser.open('all', {'A'}, 'Folder A')
    wait_complete(qtbot, browser, 1)
    proxy = browser.model.items[0]
    browser.list.selectionModel().select(browser.model.index(0),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    canonical = photo(main_window, 'A', 'Canonical')
    canonical.save_id = 1
    browser.timer.stop()
    with patch.object(main_window.view, 'ensure_asset_loaded', return_value=canonical) as loader:
        inspector = main_window.view._detail_panel
        inspector._notes_edit.setPlainText('Changed note')
        assert canonical._notes == 'Changed note'
        assert proxy._notes == 'Original note'
        loader.assert_called_with('1')
        scene.undo_stack.undo()
        assert canonical._notes == ''



@pytest.mark.parametrize('mode', ['all', 'unclassified', 'untagged', 'recent'])
def test_aggregate_refresh_does_not_replace_selected_asset_with_scope_statistics(
        main_window, tmp_path, qtbot, isolate_recent_usage, mode):
    scope, scene, browser = prepare(main_window, tmp_path,
        [(1, 'pixmap', dict(canvasId='default-canvas', title='Selected asset',
                           tags=[], lastUsed='2026-11-04T12:00:00Z'))])
    isolate_recent_usage.record(scene.project_id, 'asset', '1', 'Selected asset')
    browser.open(mode)
    wait_complete(qtbot, browser, 1, images_only=True)
    image = next(item for item in browser.model.items if not isinstance(item, dict))
    browser.list.selectionModel().select(browser.model.index(browser.model.items.index(image)),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    inspector = main_window.view._detail_panel
    with patch.object(main_window.view.category_panel, 'record_resource_usage') as usage:
        browser.show_scope_info(browser.model.items)
        browser.refresh()
        qtbot.wait(50)
        assert inspector._scroll.isVisible() and inspector._collection.isHidden()
        assert inspector._title_edit.text() == 'Selected asset'
        usage.assert_not_called()  # background refresh cannot restart recent-use sorting
