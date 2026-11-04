"""Aggregate views reuse domain restore services and update without reopening."""
from tests.test_resource_scope_ui import mixed_window, _navigate, _identities
from tests.test_catalog_browser_partial import prepare, wait_complete


def test_material_restore_updates_open_trash(mixed_window, qtbot):
    window, expected = mixed_window
    panel = window.view.category_panel
    material = next(record for record in window.view.scene.material_records)
    panel.material_service.trash([material['id']])
    browser = _navigate(window, qtbot, 'trash')
    key = 'material:' + material['id']
    qtbot.waitUntil(lambda: key in _identities(browser))
    card = next(item for item in browser.model.items if isinstance(item, dict) and item.get('result_id') == key)
    assert browser.restore_resources([card])
    qtbot.waitUntil(lambda: key not in _identities(browser))
    assert next(r for r in window.view.scene.material_records if r['id'] == material['id']).get('deletedAt') is None


def test_page_restore_restores_deleted_parent_and_updates_view(mixed_window, qtbot):
    window, expected = mixed_window
    panel = window.view.category_panel
    panel.resource_service.trash([expected['folder']['id']], '2026-11-02T00:00:00+00:00')
    browser = _navigate(window, qtbot, 'trash')
    key = 'page:' + expected['nested']['id']
    qtbot.waitUntil(lambda: key in _identities(browser))
    card = next(item for item in browser.model.items if isinstance(item, dict) and item.get('result_id') == key)
    assert browser.restore_resources([card])
    qtbot.waitUntil(lambda: key not in _identities(browser))
    assert panel.resource_service._by_id(expected['folder']['id']).get('deletedAt') is None


def test_recent_usage_updates_open_list(mixed_window, qtbot):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, 'recent')
    assert 'model:ui-model' not in _identities(browser)
    window.view.category_panel.record_resource_usage('model', 'ui-model', 'Root model')
    qtbot.waitUntil(lambda: 'model:ui-model' in _identities(browser))
    window.view.category_panel._update_navigation()
    assert int(window.view.category_panel._nav_counts['recent'].text().replace(',', '')) == len(browser.model.items)


def test_persisted_recent_asset_uses_id_lookup_not_full_scan(main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=str(key), tags=[])) for key in range(1, 201)])
    panel = main_window.view.category_panel
    panel.record_resource_usage('asset', '180', '180')
    looked_up = []
    original = scope.card_rows
    def lookup(ids, **kwargs):
        ids = tuple(ids)
        looked_up.extend(ids)
        return original(ids, **kwargs)
    monkeypatch.setattr(scope, 'card_rows', lookup)
    browser.open('recent')
    wait_complete(qtbot, browser, 1)
    assert browser.model.items[0].save_id == 180
    assert looked_up and set(looked_up) == {180}
    panel._update_navigation()
    assert panel._nav_counts['recent'].text() == '1'


def test_tag_click_queries_only_matching_images_and_includes_material(main_window, tmp_path, qtbot, monkeypatch):
    scope, scene, browser = prepare(main_window, tmp_path, [
        (key, 'pixmap', dict(canvasId='A', title=str(key), tags=['wood'] if key < 4 else ['stone']))
        for key in range(1, 401)])
    panel = main_window.view.category_panel
    material = panel.material_service.create(None, name='Wood material')
    next(record for record in scene.material_records if record['id'] == material['id'])['tags'] = ['wood']
    panel.material_service.changed.emit({'id': material['id']})
    queries = []
    original = scope.query
    def query(**kwargs):
        queries.append(kwargs)
        return original(**kwargs)
    monkeypatch.setattr(scope, 'query', query)
    panel._browse_tag('wood')
    wait_complete(qtbot, browser, 4)
    assert {item.save_id for item in browser.model.items if not isinstance(item, dict)} == {1, 2, 3}
    assert 'material:' + material['id'] in _identities(browser)
    assert queries and all(q.get('tag_sets') is not None for q in queries)


def test_image_restore_uses_existing_undo_and_does_not_mark_recent(mixed_window, qtbot):
    from prism.commands import TrashItems
    window, expected = mixed_window
    scene = window.view.scene
    image = next(scene.iter_assets())
    window.view.undo_stack.push(TrashItems(scene, [image]))
    browser = _navigate(window, qtbot, 'trash')
    qtbot.waitUntil(lambda: image in browser.model.items)
    before = image._last_used
    browser.list.setCurrentIndex(browser.model.index(browser.model.items.index(image)))
    assert image._last_used == before
    assert browser.restore_resources([image])
    qtbot.waitUntil(lambda: image not in browser.model.items)
    assert not image._deleted_at
    window.view.undo_stack.undo()
    qtbot.waitUntil(lambda: image in browser.model.items)


def test_model_restore_uses_service_and_rejects_old_project(mixed_window, qtbot, monkeypatch):
    from types import SimpleNamespace
    window, expected = mixed_window
    scene = window.view.scene
    record = scene.model_records[0]
    record['deleted_at'] = '2026-11-02T00:00:00+00:00'
    browser = _navigate(window, qtbot, 'trash')
    key = 'model:' + record['id']
    qtbot.waitUntil(lambda: key in _identities(browser))
    calls = []
    def patch(ids, changes):
        calls.append((ids, changes))
        record.update(changes)
    composition = window.model_composition
    with monkeypatch.context() as patcher:
        patcher.setattr(composition, 'service', SimpleNamespace(patch_metadata_batch=patch))
        card = next(item for item in browser.model.items if isinstance(item, dict) and item.get('result_id') == key)
        assert browser.restore_resources([card])
        qtbot.waitUntil(lambda: key not in _identities(browser))
        assert calls == [((record['id'],), {'deleted_at': None})]
        browser._restore_models((record['id'],), ('old-project', -1))
        assert len(calls) == 1


def test_tag_sets_keep_parent_alias_and_case_semantics():
    from prism import tags
    import json
    system = tags.TagSystem()
    system.set_siblings([('Oak', 'wood/oak')])
    groups = [('A', json.dumps(['Oak']), 3), ('B', json.dumps(['stone']), 4)]
    assert tags.matching_tag_sets(groups, system, active=['WOOD']) == (json.dumps(['Oak']),)


def test_live_material_creation_updates_open_unclassified(mixed_window, qtbot):
    window, expected = mixed_window
    browser = _navigate(window, qtbot, 'unclassified')
    record = window.view.category_panel.material_service.create(None, name='Created while browsing')
    qtbot.waitUntil(lambda: 'material:' + record['id'] in _identities(browser))


def test_native_trash_menu_is_chinese_and_restores_material(mixed_window, qtbot):
    from tests.test_material_cards import javascript
    window, expected = mixed_window
    panel = window.view.category_panel
    record = window.view.scene.material_records[0]
    panel.material_service.trash([record['id']])
    browser = _navigate(window, qtbot, 'trash')
    qtbot.waitUntil(lambda: browser.grid_list.native_cards is not None and browser.grid_list.native_cards.ready,
                    timeout=15000)
    cards = browser.grid_list.native_cards
    key = 'material:' + record['id']
    qtbot.waitUntil(lambda: key in cards._record_ids)
    dom = cards._dom_id(key)
    qtbot.waitUntil(lambda: javascript(cards, f"!!document.querySelector('[data-asset=\"{dom}\"]')", qtbot))
    javascript(cards, f"document.querySelector('[data-asset=\"{dom}\"]').dispatchEvent(new MouseEvent('contextmenu', {{bubbles:true,clientX:20,clientY:20}}))", qtbot)
    assert javascript(cards, "document.querySelector('#material-menu button')?.textContent", qtbot) == '恢复到原位置'
    javascript(cards, "document.querySelector('#material-menu button').click()", qtbot)
    qtbot.waitUntil(lambda: key not in _identities(browser))


def test_tag_index_upgrade_reuses_v3_cache_without_source_reparse(tmp_path):
    import hashlib
    import json
    import sqlite3
    from tests.test_catalog_keyset_overlay import scope_for
    scope = scope_for(tmp_path, [(1, 'pixmap', {'tags': ['wood']})])
    original_hash = hashlib.sha256(scope.project_path.read_bytes()).hexdigest()
    with sqlite3.connect(scope.index.cache) as db:
        state = json.loads(db.execute("SELECT value FROM catalog_state WHERE key='build'").fetchone()[0])
        state['version'] = 3
        db.execute("UPDATE catalog_state SET value=? WHERE key='build'", (json.dumps(state),))
        db.execute('DROP INDEX assets_tag_sets')
    result = scope.build()
    assert result.complete and result.processed_records == 0
    assert scope.ready
    assert [row['id'] for row in scope.iter_query(tag_sets=['["wood"]'])] == [1]
    assert scope.count(tag_sets=[]) == 0
    assert scope.tag_groups() == scope.tag_groups()
    assert scope.tag_groups(include_deleted=True) == ()
    assert hashlib.sha256(scope.project_path.read_bytes()).hexdigest() == original_hash
