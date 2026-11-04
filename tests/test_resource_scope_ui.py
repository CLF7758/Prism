"""The five left-bar entries publish the same mixed-resource card list.

These are real Qt window/navigation tests with private INI and usage-ledger
fixtures. Expected membership comes from the assets created below, never from
project_resources.collect/counts (which are the implementation under test).
"""
import pytest

from tests.test_material_features import image_item


def _identities(browser):
    return {
        item['result_id'] if isinstance(item, dict) else ('asset', id(item))
        for item in browser.model.items
    }


def _navigate(window, qtbot, scope):
    panel = window.view.category_panel
    panel._nav_buttons[scope].click()
    qtbot.waitUntil(lambda: window.tabs.currentWidget() is window.asset_browser)
    qtbot.waitUntil(lambda: window.asset_browser.mode == scope)
    return window.asset_browser


@pytest.fixture
def mixed_window(main_window, qtbot, monkeypatch, isolate_recent_usage):
    # The test covers routing into the real panel, not Chromium editing. No
    # user's desktop, clipboard, project, INI or recent-usage ledger is used.
    from prism.widgets.mindmap_panel import MindMapPanel
    monkeypatch.setattr(MindMapPanel, '_ensure_loaded', lambda self: None)
    window = main_window
    window.resize(1200, 800)
    window.show()
    panel = window.view.category_panel
    scene = window.view.scene
    root = window.workspace_service.add('mindmap', 'Root mind map')
    nested = window.workspace_service.add('mindmap', 'Root folder mind map')
    document = window.workspace_service.add('document', 'Tagged document')
    nested['tags'] = ['nested']
    document['tags'] = ['document']
    panel.rebuild_workspace_tree()
    service = panel.resource_service
    folder = service.createFolder('mindmap', 'Mind map folder')
    nested_node = next(n for n in service.nodes if n.get('pageId') == nested['id'])
    service.moveBatch([nested_node['id']], folder['id'], 'mindmap')
    image = image_item(scene, ['image'])
    material = panel.material_service.create(None, name='Root material')
    model = {'id': 'ui-model', 'name': 'Root model', 'tags': [], 'notes': '',
             'format': 'fbx', 'folder_id': None, 'deleted_at': None, 'files': []}
    scene.model_records.append(model)
    root_id = 'page:' + root['id']
    nested_id = 'page:' + nested['id']
    document_id = 'page:' + document['id']
    image_id = ('asset', id(image))
    material_id = 'material:' + material['id']
    model_id = 'model:' + model['id']
    # An image on the legacy default canvas materializes that page as an
    # independent resource card; it is not another copy of the image card.
    all_ids = {root_id, nested_id, document_id, 'page:default-canvas',
               image_id, material_id, model_id}
    isolate_recent_usage.record(scene.project_id, 'mindmap', root['id'], root['title'])
    isolate_recent_usage.record(scene.project_id, 'material', material['id'], material['name'])
    image._last_used = '2026-11-02T00:00:00+00:00'
    panel.update_counts()
    yield window, dict(
        root=root, nested=nested, folder=folder,
        named_root={root_id, nested_id, material_id, model_id},
        all=all_ids, unclassified=all_ids - {nested_id},
        untagged={root_id, 'page:default-canvas', material_id, model_id},
        recent={root_id, material_id, image_id}, trash=set())


def test_new_root_mindmap_is_visible_in_unclassified(mixed_window, qtbot):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, 'unclassified')
    assert 'page:' + expected['root']['id'] in _identities(browser)
    assert 'page:' + expected['nested']['id'] not in _identities(browser)
    assert browser.list is browser.grid_list


def test_sidebar_counts_follow_library_changes_without_manual_recount(mixed_window, qtbot):
    window, _expected = mixed_window
    panel = window.view.category_panel
    before = int(panel._nav_counts['all'].text().replace(',',''))
    record = panel.material_service.create(name='live addition')
    qtbot.waitUntil(lambda:panel._nav_counts['all'].text()==f'{before+1:,}')
    panel.material_service.trash((record['id'],))
    qtbot.waitUntil(lambda:panel._nav_counts['all'].text()==f'{before:,}')
    qtbot.waitUntil(lambda:panel._nav_counts['trash'].text()=='1')
    panel.material_service.restore((record['id'],))
    qtbot.waitUntil(lambda:panel._nav_counts['all'].text()==f'{before+1:,}')
    qtbot.waitUntil(lambda:panel._nav_counts['trash'].text()=='0')


def test_all_scope_uses_native_model_card_and_receives_cover_update(mixed_window, qtbot, monkeypatch):
    window, _expected = mixed_window
    browser = _navigate(window,qtbot,'all')
    grid = browser.grid_list
    qtbot.waitUntil(lambda:grid.native_cards is not None and 'model:ui-model' in grid._native_rows)
    cards=grid.native_cards
    asset=next(asset for asset in cards.assets if asset['id']==cards._dom_id('model:ui-model'))
    assert asset['assetKind']=='model' and asset['thumbnail']
    row=grid._native_rows['model:ui-model']
    epoch=browser._catalog_metadata_epoch
    calls=[];monkeypatch.setattr(grid,'_native_thumbnail',calls.append)
    model=next(record for record in window.view.scene.model_records if record['id']=='ui-model')
    model['thumbnail']={'cache_key':'new-cover','state':'ready'}
    window.view.category_panel.resource_records_changed.emit(dict(kind='model',id='ui-model',operation='preview'))
    assert browser.model.items[row]['cover']['cache_key']=='new-cover'
    assert calls==['model:ui-model']
    assert browser._catalog_metadata_epoch==epoch


def test_model_record_change_updates_counts_and_all_cards(mixed_window, qtbot):
    window, _expected = mixed_window
    panel = window.view.category_panel
    before = int(panel._nav_counts['all'].text().replace(',',''))
    window.view.scene.model_records.append(dict(id='imported-model',name='Imported model',
        format='fbx',folder_id=None,deleted_at=None,tags=[],files=[]))
    panel.resource_records_changed.emit(dict(kind='model',id='imported-model'))
    qtbot.waitUntil(lambda:panel._nav_counts['all'].text()==f'{before+1:,}')
    browser = _navigate(window,qtbot,'all')
    qtbot.waitUntil(lambda:'model:imported-model' in _identities(browser))


def test_library_tab_type_does_not_replace_global_totals(mixed_window,qtbot):
    window,expected=mixed_window
    panel=window.view.category_panel
    # A library/page tab may change the search type, but isn't a filtered
    # resource browser. Sidebar totals must still describe the whole project.
    window.tabs.setCurrentWidget(window.canvas_page)
    panel.set_search_kind('model')
    panel._resource_counts_cache=None
    panel._update_navigation()
    assert panel._nav_counts['all'].text()==f"{len(expected['all']):,}"


def test_legacy_mindmap_without_a_folder_or_tree_node_is_unclassified(main_window, qtbot):
    window = main_window
    window.view.scene.mindmap_tree = {'data': {'text': 'Legacy root mind map'}, 'children': []}
    window.show()
    browser = _navigate(window, qtbot, 'unclassified')
    assert 'page:default-mindmap' in _identities(browser)
    assert not any(node.get('pageId') == 'default-mindmap'
                   for node in window.view.category_panel.resource_service.nodes)


def test_moving_a_root_mindmap_into_a_folder_updates_membership(mixed_window, qtbot):
    window, expected = mixed_window
    panel = window.view.category_panel
    root_id = expected['root']['id']
    node = next(n for n in panel.resource_service.nodes if n.get('pageId') == root_id)
    browser = _navigate(window, qtbot, 'unclassified')
    assert 'page:' + root_id in _identities(browser)
    panel.resource_service.moveBatch([node['id']], expected['folder']['id'], 'mindmap')
    browser = _navigate(window, qtbot, 'unclassified')
    assert _identities(browser) == expected['unclassified'] - {'page:' + root_id}
    panel._update_navigation()
    assert panel._nav_counts['unclassified'].text() == str(len(expected['unclassified']) - 1)
    browser = _navigate(window, qtbot, 'all')
    assert _identities(browser) == expected['all']


@pytest.mark.parametrize('scope', ['all', 'unclassified', 'untagged', 'recent', 'trash'])
def test_five_navigation_counts_and_card_membership(mixed_window, qtbot, scope):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, scope)
    assert _identities(browser) == expected[scope]
    assert browser.model.rowCount() == len(expected[scope])
    assert browser.list.model() is browser.model
    panel = window.view.category_panel
    panel._update_navigation()
    assert panel._nav_counts[scope].text() == f'{len(expected[scope]):,}'


def test_root_mindmap_card_opens_its_real_workspace_panel(mixed_window, qtbot):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, 'unclassified')
    root_id = expected['root']['id']
    row = next(index for index, item in enumerate(browser.model.items)
               if isinstance(item, dict) and item.get('result_id') == 'page:' + root_id)
    browser.list.doubleClicked.emit(browser.model.index(row))
    qtbot.waitUntil(lambda: window.current_page_kind == 'mindmap'
                   and window.current_page_id == root_id)
    assert window.tabs.currentWidget() is window._page_panels[root_id]
    tree = window.view.category_panel.resource_tree
    from prism.widgets.resource_tree_model import NODE_ID_ROLE
    node = next(n for n in window.view.category_panel.resource_service.nodes
                if n.get('pageId') == root_id)
    assert tree.currentIndex().data(NODE_ID_ROLE) == node['id']


@pytest.mark.parametrize('delete_ancestor', [False, True])
def test_trashed_mindmap_cannot_be_opened_as_live_card(mixed_window, qtbot, delete_ancestor):
    window, expected = mixed_window
    panel = window.view.category_panel
    page = expected['nested'] if delete_ancestor else expected['root']
    node = next(n for n in panel.resource_service.nodes if n.get('pageId') == page['id'])
    panel.resource_service.trash(
        [expected['folder']['id'] if delete_ancestor else node['id']],
        '2026-11-02T00:00:00+00:00')
    for scope in ('all', 'unclassified', 'untagged', 'recent'):
        browser = _navigate(window, qtbot, scope)
        assert 'page:' + page['id'] not in _identities(browser), scope
    browser = _navigate(window, qtbot, 'trash')
    row = next(index for index, item in enumerate(browser.model.items)
               if isinstance(item, dict) and item.get('result_id') == 'page:' + page['id'])
    before = window.current_page_kind, window.current_page_id
    browser.list.doubleClicked.emit(browser.model.index(row))
    qtbot.wait(10)
    assert window.tabs.currentWidget() is browser
    assert (window.current_page_kind, window.current_page_id) == before


def test_recent_navigation_uses_private_project_ledger(mixed_window, qtbot, isolate_recent_usage):
    window, expected = mixed_window
    assert isolate_recent_usage.path.name == 'recent-usage.json'
    assert isolate_recent_usage.path.exists()
    isolate_recent_usage.record('different-project', 'mindmap',
                                expected['nested']['id'], 'Other project only')
    browser = _navigate(window, qtbot, 'recent')
    assert _identities(browser) == expected['recent']
    assert 'page:' + expected['nested']['id'] not in _identities(browser)


def test_usage_recount_reuses_metadata_but_library_edits_invalidate_it(mixed_window, monkeypatch):
    from prism import project_resources
    window, expected = mixed_window
    panel = window.view.category_panel
    panel._resource_scope_counts()
    before = panel._resource_domain_cache[0]
    original = project_resources.resource_domain_snapshot
    calls = []
    def observed(*args, **kwargs):
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(project_resources, 'resource_domain_snapshot', observed)
    panel.record_resource_usage('model', 'ui-model', 'Root model')
    panel._resource_scope_counts()
    assert calls == [], [(i, a, b) for i, (a, b) in enumerate(zip(before, panel._resource_domain_cache[0])) if a != b]
    panel.material_service.create(None, name='new material')
    panel._resource_scope_counts()
    assert calls


def test_large_first_metadata_count_is_cooperative_and_rejects_old_revision(main_window, qtbot):
    panel = main_window.view.category_panel
    scene = panel.scene
    scene.workspace_pages.extend(dict(id=f'bulk-{i}', kind='mindmap', title=f'Page {i}', tags=[])
                                 for i in range(200))
    panel._resource_counts_cache = None
    panel._resource_domain_cache = None
    first = panel._resource_scope_counts()
    assert all(value is None for value in first.values())
    old = panel._resource_domain_pending
    scene.workspace_pages[-1]['title'] = 'Current revision'
    panel._resource_scope_counts()
    assert panel._resource_domain_pending is not old
    qtbot.waitUntil(lambda: panel._resource_domain_pending is None, timeout=5000)
    snapshot = panel._resource_domain_cache[1]
    assert any(row.get('title') == 'Current revision' for row in snapshot[0])
    assert panel._resource_scope_counts()['all'] >= 200


@pytest.mark.parametrize('scope', ['all', 'unclassified', 'untagged', 'recent', 'trash'])
def test_scope_search_filters_cards_and_its_sidebar_count(mixed_window, qtbot, scope):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, scope)
    browser.search.setText('Root')
    wanted = expected[scope] & expected['named_root']
    qtbot.waitUntil(lambda: _identities(browser) == wanted, timeout=3000)
    panel = window.view.category_panel
    panel._update_navigation()
    assert panel._nav_counts[scope].text() == f'{len(wanted):,}'
