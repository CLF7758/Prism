"""Whole-project catalog search and counters must survive partial residency."""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from prism.project_catalog import ProjectAssetCatalog, UNASSIGNED_CANVAS
from prism.project_open_scope import ProjectOpenScope, OpenScopeCancelled
from prism.project_search import search_catalog_assets, searchable_items


def scope_for(tmp_path, records):
    source = tmp_path / 'catalog-source.prism'
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);"
                         "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key, kind, extra in records:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
    scope = ProjectOpenScope(source, tmp_path / 'catalog.sqlite')
    assert scope.build().complete
    return scope


def asset(key, cid='A', tags=(), deleted=None):
    return SimpleNamespace(save_id=key, TYPE='pixmap', _canvas_id=cid,
                           _tags=list(tags), _deleted_at=deleted,
                           isVisible=lambda: True)


def test_keyset_search_exceeds_single_canvas_and_kind_batch(tmp_path):
    scope = scope_for(tmp_path, [(key, 'custom-mesh' if key % 2 else 'pixmap',
                                  dict(canvasId='A', title='砖墙 Needle'))
                                 for key in range(-3, 1500)])
    rows = list(scope.iter_query(text='needle', limit=127))
    assert [row['id'] for row in rows] == list(range(-3, 1500))
    scene, service = SimpleNamespace(), SimpleNamespace(nodes=[])
    results = search_catalog_assets(scope, scene, service, '砖墙 needle', loaded_keys={'1499'})
    assert len(results) == 1502
    assert {hit['item_id'] for hit in results} == {str(key) for key in range(-3, 1499)}


def test_unicode_and_wildcard_characters_are_literal(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(title='ÄPFEL Straße 100%_木头')),
                                (2, 'pixmap', dict(title='apfel strasse 100AA木头'))])
    assert [r['id'] for r in scope.iter_query(text='äpfel STRASSE')] == [1]
    assert [r['id'] for r in scope.iter_query(text='%_')] == [1]
    assert scope.count(text='%_') == 1
    assert len(list(scope.iter_query(text='木头'))) == 2


def test_cancellation_between_pages_is_not_empty_success(tmp_path):
    scope = scope_for(tmp_path, [(key, 'pixmap', {}) for key in range(9)])
    cancelled = False
    rows = scope.iter_query(limit=2, cancel=lambda: cancelled)
    assert next(rows)['id'] == 0
    cancelled = True
    with pytest.raises(OpenScopeCancelled):
        next(rows)


def test_counters_overlay_move_tag_trash_new_and_parked_objects(tmp_path):
    records = [
        (1, 'pixmap', dict(canvasId='A', tags=[])),
        (2, 'pixmap', dict(canvasId='A', tags=['old'])),
        (3, 'pixmap', dict(canvasId='B', tags=[])),
        (4, 'pixmap', dict(canvasId='deleted', tags=[])),
        (5, 'pixmap', dict(canvasId='missing', tags=['  '])),
        (6, 'pixmap', dict(canvasId=None, tags=['tag'])),
        (7, 'pixmap', dict(canvasId='A', deletedAt='yesterday'))]
    scope = scope_for(tmp_path, records)
    live = [asset(1, 'B', ['edited']), asset(2, deleted='now'), asset(None, 'default-canvas')]
    parked = asset(3, 'A', ['parked'])
    scene = SimpleNamespace(_partial_project_source=str(scope.project_path),
                            items=lambda: live, iter_assets=lambda: iter(live),
                            workspace_pages=[dict(id='A', kind='canvas'), dict(id='B', kind='canvas')],
                            _partial_project_session=SimpleNamespace(_residency=SimpleNamespace(parked={3: parked})))
    trash_node = dict(id='deleted-node', pageId='deleted', section='canvas', parentId=None)
    service = SimpleNamespace(nodes=[trash_node], trashed_nodes=lambda: [trash_node])
    catalog = ProjectAssetCatalog(scene, service, scope_provider=lambda: scope)
    counts = catalog.count_snapshot()
    assert counts == dict(total=5, untagged=2, unclassified=3, trash=3, visible=2,
                          canvases={'A': 1, 'B': 1, UNASSIGNED_CANVAS: 2, 'default-canvas': 1})
    assert searchable_items(scene) == live + [parked]
    # Repeated queries use the same group projection, but current edits remain live.
    parked._tags = []
    assert catalog.count_snapshot()['untagged'] == 3


def test_summaries_are_bounded_and_do_not_read_source_json(tmp_path, monkeypatch):
    """summaries() 自己分批、不读源 JSON，而且上界只在 MAX_SUMMARY_IDS 处。

    契约变过：目录侧学会了自己按批切 id（``MAX_SUMMARY_IDS``），
    调用方不必再自己分批 —— 所以「1025 个 id 必须抛 ValueError」这条
    断言随之作废。但这条用例真正要守的三件事**一条都没变**：

    1. **不抛**：旧契约的越界点 1025 个 id 现在照常返回，返回的行就是
       目录里真实存在的那些行（目录里没有的 id 不返回）；
    2. **不读源 JSON**（``_source`` 一被调用就 fail）；
    3. **上界还在**：超过 ``MAX_SUMMARY_IDS`` 仍然抛 ValueError。

    1199 条记录是为了让请求的 id 数越过旧契约的 1024 —— 越过后仍不抛，
    才算证明「调用方不必自己分批」这件事。目录侧按几条 SQL 分批是它自己
    的事：批的大小由运行时的 SQLite 变量上限决定，所以这里不写死批大小。
    """
    scope = scope_for(tmp_path, [(key, 'pixmap', dict(canvasId='A'))
                                 for key in range(1, 1200)])
    monkeypatch.setattr(scope.index, '_source', lambda: pytest.fail('source opened'))
    assert scope.summaries([1, 999])[0]['id'] == 1
    assert scope.grouped_counts() == (('A', False, True, 1199),)
    # 旧契约的越界点：1025 个 id 不再抛，返回目录里真实存在的那些行。
    rows = scope.summaries(range(1025))
    assert len(rows) == 1024
    assert [row['id'] for row in rows] == list(range(1, 1025))
    # 一段连续 id 一次交给目录侧，行数与 id 都对。
    rows = scope.summaries(range(1, 1025))
    assert len(rows) == 1024
    assert [row['id'] for row in rows] == list(range(1, 1025))
    # 上界还在，而且不写死数字：契约变了这条要跟着变。
    with pytest.raises(ValueError):
        scope.summaries(range(scope.index.MAX_SUMMARY_IDS + 1))


def test_unavailable_partial_catalog_is_unknown_not_loaded_count(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='A'))])
    scene = SimpleNamespace(_partial_project_source=str(scope.project_path),
                            items=lambda: [], workspace_pages=[])
    catalog = ProjectAssetCatalog(scene, scope_provider=lambda: None)
    assert catalog.count_snapshot() == dict(total=None, unclassified=None,
        untagged=None, trash=None, canvases=None, visible=0, complete=False)
    assert catalog.total_count() is None
    assert catalog.untagged_count() is None
    assert catalog.unclassified_count() is None
    assert catalog.canvas_counts() is None
    assert catalog.trash_top_level_count() is None
    assert catalog.trash_asset_count() is None


def test_partial_scalar_counters_use_whole_project_overlay(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId='A')),
                                 (2, 'pixmap', dict(canvasId='A', deletedAt='yesterday'))])
    item = asset(1, tags=['edited'])
    scene = SimpleNamespace(_partial_project_source=str(scope.project_path),
                            items=lambda: [item], workspace_pages=[dict(id='A', kind='canvas')])
    catalog = ProjectAssetCatalog(scene, scope_provider=lambda: scope)
    assert catalog.total_count() == 1
    assert catalog.untagged_count() == 0
    assert catalog.trash_top_level_count() == 1
    assert catalog.trash_asset_count() == 1
    assert catalog.canvas_counts() == {'A': 1}


def test_tag_matching_decodes_json_escapes(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(tags=['quoted"tag', 'line\nnext'])),
                                 (2, 'pixmap', dict(tags=['other']))])
    assert [row['id'] for row in scope.iter_query(text='quoted"tag')] == [1]
    assert [row['id'] for row in scope.iter_query(text='line next')] == [1]


def test_parked_dirty_search_result_retains_on_demand_navigation():
    from prism.project_search import search_project, item_key
    parked = asset(0)
    parked._title = 'unsaved needle'
    scene = SimpleNamespace(items=lambda: [], workspace_pages=[],
                            _partial_project_session=SimpleNamespace(
                                _residency=SimpleNamespace(parked={0: parked})))
    results = search_project(scene, SimpleNamespace(nodes=[]), 'needle')
    hit = next(row for row in results if row['kind'] == 'asset')
    assert item_key(parked) == '0'
    assert hit['item_id'] == '0'
    assert hit['catalog'] is True
    assert hit['title'] == 'unsaved needle'


def test_search_controller_shows_incomplete_error_instead_of_false_empty(main_window, monkeypatch):
    from prism.project_catalog_index import CatalogNotReady
    controller = main_window.search_controller
    monkeypatch.setattr(controller, '_catalog_scope', lambda: SimpleNamespace(ready=True))
    def fail(*args, **kwargs):
        raise CatalogNotReady('source changed')
    monkeypatch.setattr('prism.search_controller.search_catalog_assets', fail)
    controller.input.setText('needle')
    controller.timer.stop()
    controller.active = True
    controller._run_query()
    assert not controller.info.isHidden()
    assert 'could not finish' in controller.info.text()


def test_null_canvas_filter_matches_null_bucket(tmp_path):
    scope = scope_for(tmp_path, [(1, 'pixmap', dict(canvasId=None)),
                                 (2, 'pixmap', dict(canvasId='A')),
                                 (3, 'pixmap', dict(canvasId='B'))])
    assert [r['id'] for r in scope.iter_query(canvas_ids=(None,))] == [1]
    assert [r['id'] for r in scope.iter_query(canvas_ids=(None, 'A'))] == [1, 2]
    assert scope.count(canvas_ids=(None,)) == 1
