"""Whole-library tag rows must not depend on which canvases are resident."""
from types import SimpleNamespace
from unittest.mock import Mock

from prism import tags
from prism.project_catalog import ProjectAssetCatalog
from prism.widgets.tag_panel import project_tag_nodes
from tests.test_catalog_keyset_overlay import scope_for, asset


def setup_catalog(scope, live, parked=()):
    scene = SimpleNamespace(items=lambda: live, iter_assets=lambda: iter(live),
        workspace_pages=[dict(id='A', kind='canvas'), dict(id='B', kind='canvas')],
        tag_names=[], material_records=[], workspace_nodes=[],
        _partial_project_source=str(scope.project_path),
        _partial_project_session=SimpleNamespace(_residency=SimpleNamespace(
            parked={i: item for i, item in enumerate(parked)})))
    scene.get_all_tags = lambda: {name: sum(name in i._tags for i in live)
                                 for i in live for name in i._tags}
    return scene, ProjectAssetCatalog(scene, scope_provider=lambda: scope)


def rows(scene, catalog):
    return {node.name: node for node in project_tag_nodes(scene, catalog)}


def test_tree_has_37_matches_with_one_resident_and_live_edits(tmp_path):
    scope = scope_for(tmp_path, [(i, 'pixmap', dict(canvasId='B', tags=['shared']))
                                for i in range(1, 38)])
    live = [asset(1, 'A', ['shared'])]
    scene, catalog = setup_catalog(scope, live)
    assert scene.get_all_tags() == {'shared': 1}
    assert rows(scene, catalog)['shared'].count == 37
    live[0]._tags = ['new']
    found = rows(scene, catalog)
    assert found['shared'].count == 36
    assert found['new'].count == 1
    live[0]._deleted_at = 'now'
    assert 'new' not in rows(scene, catalog)
    live[0]._deleted_at = None
    assert rows(scene, catalog)['new'].count == 1


def test_tree_deduplicates_parent_and_alias_matches(tmp_path):
    scope = scope_for(tmp_path, [
        (1, 'pixmap', dict(canvasId='B', tags=['Wood', 'Wood/Oak'])),
        (2, 'pixmap', dict(canvasId='B', tags=['Timber', 'Wood'])),
        (3, 'pixmap', dict(canvasId='B', tags=['Wood'], deletedAt='now')),
        (4, 'text', dict(canvasId='B', tags=['Wood']))])
    scene, catalog = setup_catalog(scope, [])
    tags.tag_system(scene).set_siblings([('Timber', 'Wood')])
    found = rows(scene, catalog)
    assert found['Wood'].count == 2
    assert found['Wood'].asset == 2
    assert found['Wood/Oak'].count == 1


def test_tree_overlay_parked_new_and_deleted_page(tmp_path):
    scope = scope_for(tmp_path, [
        (1, 'pixmap', dict(canvasId='B', tags=['old'])),
        (2, 'pixmap', dict(canvasId='B', tags=['deleted-page']))])
    parked = asset(1, 'A', ['parked'])
    scene, catalog = setup_catalog(scope, [asset(None, 'A', ['new'])], [parked])
    catalog._deleted_page_ids = lambda: {'B'}
    found = rows(scene, catalog)
    assert set(found) == {'parked', 'new'}
    assert found['parked'].count == 1
    assert found['new'].count == 1


def test_tree_preserves_material_and_page_membership(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='B', tags=['all']))])
    scene, catalog = setup_catalog(scope, [])
    scene.material_records = [dict(id='m', tags=['all']), dict(id='d', tags=['all'], deletedAt='now')]
    scene.workspace_pages[0]['tags'] = ['all']
    assert rows(scene, catalog)['all'].count == 3


def test_tag_groups_reuse_version_cached_projection(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='B', tags=['one']))])
    first = scope.tag_groups()
    assert scope.tag_groups() is first
    scene, catalog = setup_catalog(scope, [asset(1, 'A', ['one'])])
    read = Mock(wraps=scope.summaries)
    monkeypatch.setattr(scope, 'summaries', read)
    assert rows(scene, catalog)['one'].count == 1
    assert rows(scene, catalog)['one'].count == 1
    assert read.call_count == 1
    next(scene.iter_assets())._tags = ['edited']
    assert rows(scene, catalog)['edited'].count == 1
    assert read.call_count == 1


def test_unavailable_partial_catalog_is_unknown_not_resident_total(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='A', tags=['one']))])
    scene, catalog = setup_catalog(scope, [asset(1, 'A', ['one'])])
    scene.tag_names = ['one']
    catalog._scope_provider = lambda: None
    assert rows(scene, catalog)['one'].count is None


def test_sidebar_formal_update_publishes_whole_library_tag_count(main_window, tmp_path):
    from tests.test_asset_browser import photo
    scope = scope_for(tmp_path, [(i, 'pixmap', dict(canvasId='B', tags=['shared']))
                                for i in range(1, 38)])
    scene = main_window.view.scene
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(id='A', kind='canvas', title='A'),
                             dict(id='B', kind='canvas', title='B')]
    item = photo(main_window, 'A', 'resident', ['shared'])
    item.save_id = 1
    panel = main_window.view.category_panel
    panel.catalog = ProjectAssetCatalog(scene, panel.resource_service, scope_provider=lambda: scope)
    panel.update_counts()
    assert scene.get_all_tags()['shared'] == 1
    record = panel.resource_tree.model()._tag_records['catalogue-tag:shared']
    assert record['assetCount'] == 37


def test_tag_group_cache_expires_when_source_and_directory_change(tmp_path):
    import json
    import sqlite3
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='B', tags=['before']))])
    scene, catalog = setup_catalog(scope, [])
    assert rows(scene, catalog)['before'].count == 1
    with sqlite3.connect(scope.project_path) as db:
        db.execute('UPDATE items SET data=? WHERE id=1',
                   (json.dumps(dict(canvasId='B', tags=['after'])),))
    assert not scope.ready
    assert catalog.tag_distribution() is None
    assert scope.build().complete
    assert rows(scene, catalog)['after'].count == 1
    assert 'before' not in rows(scene, catalog)


def test_assembled_tag_tree_reuses_only_identical_counts_and_rules(tmp_path, monkeypatch):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='B', tags=['one']))])
    scene, catalog = setup_catalog(scope, [])
    build = Mock(wraps=tags.build_tree)
    monkeypatch.setattr(tags, 'build_tree', build)
    original = project_tag_nodes(scene, catalog)
    assert project_tag_nodes(scene, catalog) is original
    build.assert_called_once()
    scene.workspace_pages[0]['tags'] = ['page']
    updated = project_tag_nodes(scene, catalog)
    assert updated is not original
    assert {n.name: n.count for n in updated} == {'one': 1, 'page': 1}
    scene.tag_names = ['empty']
    assert 'empty' in {n.name for n in project_tag_nodes(scene, catalog)}
