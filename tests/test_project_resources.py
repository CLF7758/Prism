"""左栏五个口径：同一张混排资源列表。

这组测试盯两件事：

* **口径语义** —— 「未分类」是不在任何文件夹里的资源，「未标签」是一个
  有效标签都没有的资源，「回收站」是被软删的那批。
* **数字和内容同源** —— 左栏那五个数字必须和点进去看到的那张列表用同一套
  判定。数字和内容对不上，用户就不信这个数字了。
"""
import pytest
from types import SimpleNamespace

from prism import project_resources
from tests.test_material_features import image_item


def add_model(scene, name, *, folder_id=None, tags=(), deleted_at=None):
    """往场景里塞一条模型库记录。"""
    record = {'id': 'model-' + name, 'name': name, 'tags': list(tags),
              'notes': '', 'format': 'fbx', 'folder_id': folder_id,
              'deleted_at': deleted_at, 'files': []}
    scene.model_records.append(record)
    return record


def build(window):
    """搭一个什么都有一点的小工程：画布、脑图、文档各一页。"""
    library = window.view.category_panel
    scene = window.view.scene
    pages = {'canvas': window.workspace_service.add('canvas', '主画布'),
             'mindmap': window.workspace_service.add('mindmap', '脑图'),
             'document': window.workspace_service.add('document', '虚幻五引擎')}
    library.rebuild_workspace_tree()
    return library, scene, pages


def collect(library, scope, **kwargs):
    """某个口径下的列表条目。"""
    scene = library.scene
    return project_resources.collect(
        scene, library.resource_service, scope,
        page_records=library._pages_with_implicit_defaults(), **kwargs)


def counts(library, **kwargs):
    """五个口径各自的数字。"""
    scene = library.scene
    return project_resources.scope_counts(
        scene, library.resource_service,
        recent=project_resources.recent_entries(scene.project_id),
        page_records=library._pages_with_implicit_defaults(), **kwargs)


def result_ids(items):
    return {item['result_id'] for item in items if isinstance(item, dict)}


def test_all_scope_mixes_every_resource_kind(main_window):
    """图片素材、画布、脑图、文档、材质、模型平级躺在同一张列表上。"""
    library, scene, pages = build(main_window)
    item = image_item(scene)
    material = library.material_service.create(None, name='木纹')
    model = add_model(scene, '机器人')

    items = collect(library, 'all')
    assert item in items                    # 图片素材是素材对象本身
    assert {'page:' + pages['canvas']['id'],
            'page:' + pages['mindmap']['id'],
            'page:' + pages['document']['id'],
            'material:' + material['id'],
            'model:' + model['id']} <= result_ids(items)


def test_unclassified_means_outside_any_folder(main_window):
    """没进任何文件夹的才是未分类；进了文件夹的就不算。"""
    library, scene, pages = build(main_window)
    service = library.resource_service
    folder = service.createFolder('canvas', '归档')
    node = next(n for n in service.nodes
                if n.get('pageId') == pages['canvas']['id'])
    service.moveBatch([node['id']], folder['id'], 'canvas')
    item = image_item(scene)
    material = library.material_service.create(None, name='木纹')
    model = add_model(scene, '机器人')

    items = collect(library, 'unclassified')
    assert 'page:' + pages['canvas']['id'] not in result_ids(items)
    assert {'page:' + pages['mindmap']['id'],
            'page:' + pages['document']['id'],
            'material:' + material['id'],
            'model:' + model['id']} <= result_ids(items)
    assert item in items                    # 默认画布上的贴图也没归类


def test_unclassified_also_covers_pages_without_a_tree_node(main_window):
    """默认画布这类还没长出树节点的页面，也是项目里的资源。"""
    library, scene, pages = build(main_window)
    image_item(scene)
    items = collect(library, 'all')
    assert any(isinstance(item, dict) and item.get('page_id') == 'default-canvas'
               for item in items)


def test_untagged_means_no_usable_tag(main_window):
    """一个有效标签都没有的才算未标签。"""
    library, scene, pages = build(main_window)
    pages['canvas']['tags'] = ['古风']
    item = image_item(scene, ['夜景'])
    material = library.material_service.create(None, name='木纹')
    library.material_service.configure(material['id'], tags=['木材'])
    model = add_model(scene, '机器人', tags=['模型'])

    items = collect(library, 'untagged')
    assert {'page:' + pages['mindmap']['id'],
            'page:' + pages['document']['id']} <= result_ids(items)
    assert 'page:' + pages['canvas']['id'] not in result_ids(items)
    assert 'material:' + material['id'] not in result_ids(items)
    assert 'model:' + model['id'] not in result_ids(items)
    assert item not in items


def test_recent_scope_follows_the_usage_ledger(main_window):
    """页面/材质/模型的最近使用看用户级账本。"""
    library, scene, pages = build(main_window)
    project_resources.record_use(scene.project_id, 'document',
                                  pages['document']['id'], '虚幻五引擎')
    entries = project_resources.recent_entries(scene.project_id)
    assert entries and entries[0]['resource_id'] == pages['document']['id']

    items = collect(library, 'recent')
    assert 'page:' + pages['document']['id'] in result_ids(items)
    assert 'page:' + pages['mindmap']['id'] not in result_ids(items)


def test_recent_scope_also_sees_assets_used_before(main_window):
    """图片素材沿用它自己的 ``_last_used``。"""
    library, scene, pages = build(main_window)
    item = image_item(scene)
    assert item not in collect(library, 'recent')
    item._last_used = '2026-11-01T10:00:00+00:00'
    assert item in collect(library, 'recent')


def test_trash_scope_collects_soft_deleted_resources(main_window):
    """回收站口径收的是软删的那批：页面节点、材质、模型。"""
    library, scene, pages = build(main_window)
    service = library.resource_service
    material = library.material_service.create(None, name='木纹')
    model = add_model(scene, '机器人')
    node = next(n for n in service.nodes
                if n.get('pageId') == pages['mindmap']['id'])
    service.trash([node['id']], '2026-11-01T00:00:00+00:00')
    library.material_service.trash([material['id']])
    model['deleted_at'] = '2026-11-01T00:00:00+00:00'

    items = collect(library, 'trash')
    assert {'page:' + pages['mindmap']['id'],
            'material:' + material['id'],
            'model:' + model['id']} <= result_ids(items)
    # 活着的资源不进回收站。
    assert 'page:' + pages['document']['id'] not in result_ids(items)
    assert 'material:' + library.material_service.create(
        None, name='还在')['id'] not in result_ids(items)


def test_trash_scope_keeps_the_page_kind(main_window):
    """被软删的画布不该显示成「文档」——类型从页面记录里取。"""
    library, scene, pages = build(main_window)
    service = library.resource_service
    node = next(n for n in service.nodes
                if n.get('pageId') == pages['canvas']['id'])
    service.trash([node['id']], '2026-11-01T00:00:00+00:00')

    trashed = next(item for item in collect(library, 'trash')
                   if isinstance(item, dict)
                   and item.get('result_id') == 'page:' + pages['canvas']['id'])
    assert trashed['kind'] == 'canvas'


def test_left_counts_and_the_lists_agree(main_window):
    """左栏那五个数字和点进去看到的那张列表必须是同一套口径。"""
    library, scene, pages = build(main_window)
    image_item(scene)
    material = library.material_service.create(None, name='木纹')
    model = add_model(scene, '机器人')
    project_resources.record_use(scene.project_id, 'material',
                                  material['id'], '木纹')

    numbers = counts(library)
    for scope in ('all', 'unclassified', 'untagged', 'recent', 'trash'):
        assert numbers[scope] == len(collect(library, scope)), scope


def test_unknown_scope_is_refused():
    with pytest.raises(ValueError):
        project_resources.collect(None, None, 'nope')


def pure_resources(pages=(), nodes=(), items=()):
    service = SimpleNamespace(nodes=list(nodes))
    scene = SimpleNamespace(project_id='pure-resource-project', workspace_pages=list(pages),
                            workspace_nodes=service.nodes, material_records=[], model_records=[],
                            items=lambda: list(items), iter_assets=lambda: iter(items))
    return scene, service


def page_node(page_id, *, parent=None, deleted=None):
    return dict(id='node-' + page_id, nodeType='page', pageId=page_id,
                section='mindmap', title=page_id, parentId=parent, deletedAt=deleted)


def test_recent_cap_is_shared_by_images_domains_and_counts():
    from prism.recent_usage import PROJECT_LIMIT
    base = 1000
    items = [SimpleNamespace(save_id=i, TYPE='pixmap', _title=str(i), _tags=[],
        _deleted_at=None, _canvas_id=None, _last_used=base+i) for i in range(1, 601)]
    oldest = SimpleNamespace(save_id=601, TYPE='pixmap', _title='oldest', _tags=[],
        _deleted_at=None, _canvas_id=None, _last_used=1)
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='mind')],
                                    items=items+[oldest])
    recent = [dict(section='mindmap', resource_id='mind', ts=base+1000)]
    rows = project_resources.collect(scene, service, 'recent', recent=recent)
    assert len(rows) == PROJECT_LIMIT
    assert rows[0]['page_id'] == 'mind'
    assert oldest not in rows
    assert project_resources.scope_counts(scene, service, recent=recent)['recent'] == len(rows)


def test_recent_directory_seek_caps_candidates(tmp_path):
    from tests.test_project_catalog_max_rows import _scope
    from prism.recent_usage import PROJECT_LIMIT
    base = 1000
    scope = _scope(tmp_path, records=[
        (i, 'pixmap', dict(lastUsed=base+i)) for i in range(1, 601)] +
        [(601, 'pixmap', dict(lastUsed=1))])
    filters = dict(recent_only=True, recent_limit=PROJECT_LIMIT)
    assert scope.count(**filters) == PROJECT_LIMIT
    rows = scope.index.query(**filters)
    assert len(rows) == PROJECT_LIMIT
    assert all(row['last_used'] > 1 for row in rows)


def test_deleted_page_and_deleted_ancestor_cannot_reappear_as_missing_page():
    pages = [dict(id='direct', kind='mindmap', title='direct'),
             dict(id='child', kind='mindmap', title='child'),
             dict(id='implicit', kind='mindmap', title='implicit')]
    folder = dict(id='folder', nodeType='folder', section='mindmap',
                  title='folder', parentId=None, deletedAt='2026-11-02')
    scene, service = pure_resources(pages, [page_node('direct', deleted='2026-11-02'),
                                           folder, page_node('child', parent='folder')])
    for scope in ('all', 'unclassified', 'untagged', 'recent'):
        ids = result_ids(project_resources.collect(scene, service, scope, recent=[]))
        assert not ids & {'page:direct', 'page:child'}
    assert result_ids(project_resources.collect(scene, service, 'trash', recent=[])) == {'page:direct', 'page:child'}
    assert result_ids(project_resources.collect(scene, service, 'unclassified', recent=[])) == {'page:implicit'}


def test_orphan_parent_is_unclassified_but_real_folder_is_classified():
    pages = [dict(id=name, kind='mindmap', title=name) for name in ('orphan', 'owned', 'root')]
    folder = dict(id='folder', nodeType='folder', section='mindmap', title='folder',
                  parentId=None, deletedAt=None)
    scene, service = pure_resources(pages, [folder, page_node('orphan', parent='gone'),
                                           page_node('owned', parent='folder'), page_node('root')])
    assert result_ids(project_resources.collect(scene, service, 'unclassified', recent=[])) == {'page:orphan', 'page:root'}


def test_recent_sorts_actual_timestamps_and_limits_after_valid_resource_join():
    base = 1000
    pages = [dict(id=str(i), kind='mindmap', title=str(i)) for i in range(60)]
    scene, service = pure_resources(pages)
    recent = [dict(section='mindmap', resource_id='removed-' + str(i), ts=base + 1000 + i)
              for i in range(70)]
    recent += [dict(section='mindmap', resource_id=str(i), ts=base + i + 1) for i in range(60)]
    # Ledger input order is deliberately unrelated to usage order.
    recent.reverse()
    picked = project_resources.collect(scene, service, 'recent', recent=recent, recent_limit=50)
    assert [result['page_id'] for result in picked] == [str(i) for i in range(59, 9, -1)]
    assert project_resources.scope_counts(scene, service, recent=recent, recent_limit=50)['recent'] == 50
    assert project_resources.scope_counts(scene, service, recent=recent)['recent'] == 60


def test_recent_combines_asset_iso_time_with_domain_ledger_seconds():
    asset = SimpleNamespace(save_id=1, TYPE='pixmap', _title='image', _tags=[],
                            _deleted_at=None, _canvas_id=None, _last_used='2026-11-02T08:00:00Z')
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='mind')], items=[asset])
    recent = [dict(section='mindmap', resource_id='mind', ts=1793608200)]  # 08:30 UTC
    picked = project_resources.collect(scene, service, 'recent', recent=recent)
    assert isinstance(picked[0], dict) and picked[0]['page_id'] == 'mind'
    assert picked[1] is asset
    asset._last_used = 'not-a-time'
    assert project_resources.collect(scene, service, 'recent', recent=recent) == [picked[0]]


def test_filtered_counts_and_cards_share_text_tags_kind_and_rating():
    asset = SimpleNamespace(save_id=1, TYPE='pixmap', _title='wood image', _tags=['wood'],
                            _rating=2, _deleted_at=None, _canvas_id=None)
    pages = [dict(id='mind', kind='mindmap', title='planning', tags=['wood'],
                  content={'data': {'text': 'wood production'}}),
             dict(id='other', kind='mindmap', title='metal', tags=['metal'])]
    scene, service = pure_resources(pages, items=[asset])
    options = dict(text='wood', active_tags={'wood'}, min_rating=3, recent=[])
    assert result_ids(project_resources.collect(scene, service, 'all', **options)) == {'page:mind'}
    for scope in project_resources.SCOPES:
        numbers = project_resources.scope_counts(scene, service, **options)
        assert numbers[scope] == len(project_resources.collect(scene, service, scope, **options))
    assert result_ids(project_resources.collect(scene, service, 'all', kind='mindmap', recent=[])) == {'page:mind', 'page:other'}


def test_partial_scene_without_directory_is_unknown_not_a_loaded_page_total():
    from prism.project_catalog_index import CatalogNotReady
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='mind')])
    scene._partial_project_source = 'readonly-project.prism'
    with pytest.raises(CatalogNotReady):
        project_resources.collect(scene, service, 'all', recent=[])
    with pytest.raises(CatalogNotReady):
        project_resources.scope_counts(scene, service, recent=[])
    # An explicit candidate set is allowed for the parent's existing directory
    # pipeline, including its other-domains-only [] projection.
    assert result_ids(project_resources.collect(scene, service, 'all', recent=[], asset_items=[])) == {'page:mind'}


def test_filtered_directory_totals_do_not_materialize_all_matches(tmp_path, monkeypatch):
    from tests.test_project_catalog_max_rows import _scope, _scene
    from prism.project_catalog import ProjectAssetCatalog
    scope = _scope(tmp_path, records=[
        (1,'pixmap',dict(canvasId='canvas-a',title='wood old',tags=['wood'],rating=4)),
        (2,'pixmap',dict(title='wood root',tags=['wood'],rating=4,lastUsed=10)),
        (3,'pixmap',dict(title='wood deleted',tags=['wood'],rating=4,deletedAt='2026-11-03')),
        (4,'pixmap',dict(title='other',tags=[],rating=0))])
    # A current edit replaces the old tagged/rated catalog row.
    changed = SimpleNamespace(save_id=1,TYPE='pixmap',_canvas_id='canvas-a',
        _title='changed',filename='',_tags=[],_rating=0,_last_used=0,_deleted_at=None)
    scene = _scene([changed])
    scene._partial_project_source = str(scope.project_path)
    service = SimpleNamespace(nodes=[])
    catalog = ProjectAssetCatalog(scene,scope_provider=lambda:scope)
    variants = [dict(active_tags=('wood',)),dict(text='old'),dict(text='wood'),dict(min_rating=3),
                dict(text='wood',active_tags=('wood',),min_rating=3)]
    expected = [{name:len(project_resources.collect(scene,service,name,catalog=catalog,recent=[],**options))
                 for name in project_resources.SCOPES} for options in variants]
    iterate = scope.iter_query
    def bounded(**filters):
        assert filters.get('recent_only'), 'Counts must not iterate all matching directory rows'
        return iterate(**filters)
    monkeypatch.setattr(scope,'iter_query',bounded)
    catalog = ProjectAssetCatalog(scene,scope_provider=lambda:scope)
    for options, numbers in zip(variants,expected):
        assert project_resources.scope_counts(scene,service,catalog=catalog,recent=[],**options) == numbers


def test_real_directory_contributes_unloaded_assets_and_uses_catalog_ownership(tmp_path):
    from tests.test_project_catalog_max_rows import _scope, _scene
    from prism.project_catalog import ProjectAssetCatalog
    records = [(1, 'pixmap', dict(canvasId='gone', title='orphan', tags=[])),
               (2, 'pixmap', dict(canvasId='canvas-a', title='owned', tags=['wood'])),
               (3, 'pixmap', dict(canvasId='canvas-a', title='trashed', deletedAt='2026-11-02'))]
    scope = _scope(tmp_path, records=records)
    scene = _scene()
    scene._partial_project_source = str(scope.project_path)
    service = SimpleNamespace(nodes=[])
    catalog = ProjectAssetCatalog(scene, scope_provider=lambda: scope)
    live = project_resources.collect(scene, service, 'all', catalog=catalog, recent=[])
    image_ids = {str(item.save_id) for item in live if not isinstance(item, dict)}
    assert image_ids == {'1', '2'}
    unclassified = project_resources.collect(scene, service, 'unclassified', catalog=catalog, recent=[])
    assert {str(item.save_id) for item in unclassified if not isinstance(item, dict)} == {'1'}
    trash = project_resources.collect(scene, service, 'trash', catalog=catalog, recent=[])
    assert {str(item.save_id) for item in trash if not isinstance(item, dict)} == {'3'}
    totals = project_resources.scope_counts(scene, service, catalog=catalog, recent=[])
    assert totals['all'] == len(live) and totals['trash'] == len(trash)


def test_supplied_directory_items_are_reused_without_another_query():
    asset = SimpleNamespace(save_id=1, TYPE='pixmap', _title='directory image',
                            _tags=[], _deleted_at=None, _canvas_id=None)
    scene, service = pure_resources()
    class SuppliedCatalog:
        def query(self, **options):
            raise AssertionError('the existing image pipeline already queried the directory')
        def _known_canvas_ids(self):
            return set()
        def is_unclassified(self, item, known):
            return item is asset
    catalog = SuppliedCatalog()
    assert project_resources.collect(scene, service, 'all', catalog=catalog, asset_items=[asset], recent=[]) == [asset]
    assert project_resources.collect(scene, service, 'unclassified', catalog=catalog, asset_items=[asset], recent=[]) == [asset]


@pytest.mark.parametrize('section', ['material', 'model'])
def test_deleted_library_ancestor_hides_descendants_until_restored(section):
    folder = dict(id='parent', nodeType='folder', section=section, title='parent',
                  parentId=None, deletedAt='2026-11-02')
    child = dict(id='child', nodeType='folder', section=section, title='child',
                 parentId='parent', deletedAt=None)
    scene, service = pure_resources(nodes=[folder, child])
    if section == 'material':
        scene.material_records = [dict(id='m', name='wood', tags=['wood'], notes='',
                                      files=[], nativePackages=[], folderId='child', deletedAt=None)]
    else:
        add_model(scene, 'wood', folder_id='child', tags=['wood'])
    assert not project_resources.collect(scene, service, 'all', recent=[])
    trash = project_resources.collect(scene, service, 'trash', recent=[])
    assert len(trash) == 1 and trash[0]['kind'] == section
    folder['deletedAt'] = None
    assert not project_resources.collect(scene, service, 'trash', recent=[])
    assert len(project_resources.collect(scene, service, 'all', recent=[])) == 1


def test_recent_ledger_is_loaded_before_its_display_limit(isolate_recent_usage):
    ledger = isolate_recent_usage
    # A removed document with a newer timestamp must not consume the sole
    # visible slot of a still-existing mind map in another domain.
    ledger.record('p', 'mindmap', 'live', 'live')
    ledger.record('p', 'document', 'removed', 'removed')
    entries = project_resources.recent_entries('p')
    assert len(entries) == 2
    scene, service = pure_resources([dict(id='live', kind='mindmap', title='live')])
    assert result_ids(project_resources.collect(scene, service, 'recent', recent=entries, recent_limit=1)) == {'page:live'}


def test_recent_totals_use_sql_and_merge_usage_without_full_iteration(tmp_path, monkeypatch):
    from prism.recent_usage import PROJECT_LIMIT
    now = 1000
    from tests.test_project_catalog_max_rows import _scope, _scene
    from prism.project_catalog import ProjectAssetCatalog
    records = [(i, 'pixmap', dict(title='wood', tags=['wood'], lastUsed=now))
               for i in range(1, 61)]
    records.append((61, 'pixmap', dict(title='wood', tags=['wood'])))
    scope = _scope(tmp_path, records=records)
    changed = SimpleNamespace(save_id=1, TYPE='pixmap', _canvas_id=None,
        _title='wood', filename='', _tags=['wood'], _rating=0, _last_used=0, _deleted_at=None,
        isVisible=lambda: True)
    scene = _scene([changed])
    scene._partial_project_source = str(scope.project_path)
    service = SimpleNamespace(nodes=[])
    catalog = ProjectAssetCatalog(scene, scope_provider=lambda: scope)
    recent = [dict(section='asset', resource_id='61', ts=now+10)]
    expected = len(project_resources.collect(scene, service, 'recent', catalog=catalog, recent=recent))
    assert expected == 60
    iterate = scope.iter_query
    def bounded_recent(**filters):
        assert filters.get('recent_only')
        assert filters.get('recent_limit', PROJECT_LIMIT+1) <= PROJECT_LIMIT+1
        return iterate(**filters)
    monkeypatch.setattr(scope, 'iter_query', bounded_recent)
    for options in ({}, dict(text='wood'), dict(active_tags=('wood',))):
        assert project_resources.scope_counts(scene, service, catalog=catalog,
            recent=recent, **options)['recent'] == expected


def test_page_usage_reuses_persisted_recent_image_rows_but_updates_counts(tmp_path, monkeypatch):
    from tests.test_project_catalog_max_rows import _scope, _scene
    from prism.project_catalog import ProjectAssetCatalog
    scope = _scope(tmp_path, records=[(1,'pixmap',dict(lastUsed=1000))])
    scene = _scene([])
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(id='page',kind='mindmap',title='Page',tags=[])]
    service = SimpleNamespace(nodes=[])
    catalog = ProjectAssetCatalog(scene, scope_provider=lambda: scope)
    calls = []
    query = scope.iter_query
    def counted(**filters):
        calls.append(filters)
        return query(**filters)
    monkeypatch.setattr(scope, 'iter_query', counted)
    first = project_resources.scope_counts(scene, service, catalog=catalog, recent=[])
    second = project_resources.scope_counts(scene, service, catalog=catalog,
        recent=[dict(section='mindmap',resource_id='page',ts=2000)])
    assert second['recent'] == first['recent']+1
    assert len(calls) == 1
    # Query predicates and source/cache revisions still invalidate the memo.
    project_resources.scope_counts(scene, service, catalog=catalog, recent=[], text='something')
    assert len(calls) == 2


def test_image_overlay_counts_do_not_build_library_search_cards(monkeypatch):
    scene, service = pure_resources()
    asset = SimpleNamespace(save_id=1, TYPE='pixmap', _title='image', _tags=[],
        _deleted_at=None, _canvas_id=None, _last_used=10)
    def unexpected(*args, **kwargs):
        raise AssertionError('image corrections must not enumerate other resource domains')
    monkeypatch.setattr(project_resources, 'search_project_steps', unexpected)
    counts = project_resources.scope_counts(scene, service, asset_items=[asset], kind='asset', recent=[])
    assert counts['all'] == counts['recent'] == 1


@pytest.mark.parametrize('scope', project_resources.SCOPES)
@pytest.mark.parametrize('options', [{}, {'text': 'wood'}, {'active_tags': ('wood',)},
                                  {'min_rating': 3}, {'kind': 'model'}])
def test_cooperative_collection_preserves_scope_membership_and_order(scope, options):
    items = [SimpleNamespace(save_id=i, TYPE='pixmap', _title='wood' if i % 2 else 'metal',
        filename='', _notes='', _tags=['wood'] if i % 2 else [], _rating=i % 5,
        _deleted_at='2026-11-03' if i % 7 == 0 else None,
        _canvas_id=None, _last_used=i) for i in range(1, 30)]
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='wood', tags=['wood'])])
    add_model(scene, 'wood', tags=['wood'])
    recent = [dict(section='mindmap', resource_id='mind', ts=50)]
    expected = project_resources.collect(scene, service, scope, asset_items=items, recent=recent, **options)
    steps = project_resources.collect_steps(scene, service, scope, asset_items=items, recent=recent, **options)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            assert complete.value == expected
            break


def test_cooperative_collection_yields_before_processing_all_images(monkeypatch):
    scene, service = pure_resources()
    images = [SimpleNamespace(save_id=i, TYPE='pixmap', _title='image', _tags=[],
        _canvas_id=None, _deleted_at=None) for i in range(1000)]
    visited = []
    original = project_resources._asset_result
    def observed(item, **kwargs):
        visited.append(item.save_id)
        return original(item, **kwargs)
    monkeypatch.setattr(project_resources, '_asset_result', observed)
    steps = project_resources.collect_steps(scene, service, 'all', asset_items=images, recent=[], text='image')
    next(steps)
    assert visited == []
    next(steps)
    assert 0 < len(visited) <= 256
    steps.close()


def test_domain_snapshot_can_answer_counts_without_rebuilding_search_results(monkeypatch):
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='wood', tags=[])])
    snapshot = project_resources.resource_domain_snapshot(scene, service, scene.workspace_pages)
    def unexpected(*args, **kwargs):
        raise AssertionError('domain metadata has not changed')
    monkeypatch.setattr(project_resources, 'search_project_steps', unexpected)
    result = project_resources.scope_counts(scene, service, asset_items=[], domain_snapshot=snapshot,
        recent=[dict(section='mindmap', resource_id='mind', ts=5)])
    assert result['all'] == result['recent'] == 1


def test_plain_image_collection_reuses_objects_without_search_wrappers(monkeypatch):
    scene, service = pure_resources()
    images = [SimpleNamespace(save_id=i, TYPE='pixmap', _tags=[],
        _canvas_id=None, _deleted_at=None) for i in range(1000)]
    def unexpected(*args, **kwargs):
        raise AssertionError('plain images need no temporary search dictionaries')
    monkeypatch.setattr(project_resources, '_asset_result', unexpected)
    steps = project_resources.collect_steps(scene, service, 'untagged', asset_items=images)
    turns = 0
    while True:
        try:
            next(steps)
            turns += 1
        except StopIteration as complete:
            assert complete.value == images
            break
    assert turns >= 4


def test_collection_reuses_validated_domain_snapshot(monkeypatch):
    scene, service = pure_resources([dict(id='mind', kind='mindmap', title='wood', tags=[])])
    snapshot = project_resources.resource_domain_snapshot(scene, service, scene.workspace_pages)
    expected = project_resources.collect(scene, service, 'all', asset_items=[])
    def unexpected(*args, **kwargs):
        raise AssertionError('the sidebar already built this metadata revision')
    monkeypatch.setattr(project_resources, 'search_project_steps', unexpected)
    steps = project_resources.collect_steps(scene, service, 'all', asset_items=[], domain_snapshot=snapshot)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            assert complete.value == expected
            break


def test_metadata_search_yields_before_building_every_page(monkeypatch):
    pages = [dict(id=str(i), kind='canvas', title=str(i)) for i in range(1000)]
    nodes = [dict(id='node-'+page['id'], nodeType='page', section='canvas',
                  pageId=page['id'], title=page['title'], parentId=None) for page in pages]
    scene, service = pure_resources(pages, nodes)
    from prism import project_search
    original = project_search._build_result
    visited = []
    def observed(*args, **kwargs):
        visited.append(args[1])
        return original(*args, **kwargs)
    monkeypatch.setattr(project_search, '_build_result', observed)
    steps = project_resources.collect_steps(scene, service, 'all', asset_items=[])
    next(steps)
    assert len(visited) < len(pages)
    while True:
        try:
            next(steps)
        except StopIteration as complete:
            assert len(visited) == len(pages)
            assert len(complete.value) == len(pages)
            break
