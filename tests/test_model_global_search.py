"""Model metadata uses the existing project search and bounded model browser."""
from unittest.mock import patch

from prism import tags
from prism.project_search import search_project
from prism.model_library.contracts import ModelQuery
from prism.model_library.project_storage import open_model_context
from prism.model_library.repository import ModelRepositoryAdapter
from prism.model_library.service import ModelLibraryService
from tests.model_library.test_storage import (
    sample_payload, make_scene, create_project, open_project,
)
from tests.test_search_controller import submit


def add_model(window, **extra):
    library = window.view.category_panel
    folder = library.resource_service.createFolder('model', '建筑模型')
    record = sample_payload(name='Wood 雕刻', folder_id=folder['id'],
                            tags=['Props/Wood'], notes='大厅屋顶装饰', **extra)
    window.view.scene.model_records = [record]
    return library, folder, record


def test_model_search_all_fields_tags_alias_and_shared_surfaces(main_window):
    library, folder, record = add_model(main_window)
    scene = main_window.view.scene
    scene.tag_system.add_sibling('timber', 'Props/Wood')
    for text in ('雕刻', '屋顶', 'chair.fbx', 'fbx', 'Props/Wood', 'timber', 'Wood 屋顶'):
        controller = submit(main_window, text)
        hit = next(r for r in controller.results if r.get('model_id') == record['id'])
        assert hit['page_kind'] == 'model'
        assert hit['path'] == '模型库 / 建筑模型'
        assert hit['result_id'] == 'model:' + record['id']
    assert not any(r.get('model_id') for r in search_project(
        scene, library.resource_service, 'Wood 不存在'))
    assert tags.tag_names_of_items(scene)['Props/Wood'] == 1
    assert 'Props/Wood' in tags.scene_tag_names(scene)
    assert any(r.get('model_id') for r in library.project_search_results('雕刻'))
    library.search.setText('雕刻')
    library._refresh_project_search()  # Legacy result tree must recognize model type.


def test_model_type_counts_tag_predicate_and_scope(main_window):
    library, folder, record = add_model(main_window)
    controller = submit(main_window, 'Wood')
    assert controller.type_combo.findData('model') >= 0
    library.set_search_kind('model')
    controller.submit()
    assert [r['model_id'] for r in controller.results] == [record['id']]
    assert controller.page.type_buttons['model'].text().endswith('(1)')
    controller.set_scope('folder', folder['id'], '建筑模型')
    controller.submit()
    assert len(controller.results) == 1
    other = library.resource_service.createFolder('model', '其他')
    controller.set_scope('folder', other['id'], '其他')
    controller.submit()
    assert controller.results == []
    controller.set_scope('page', ('model', None), '当前模型库')
    controller.submit()
    assert len(controller.results) == 1
    assert len([r for r in search_project(main_window.view.scene, library.resource_service, '',
                              tag_expression='Props/Wood') if r['kind'] == 'model']) == 1
    assert not search_project(main_window.view.scene, library.resource_service, '',
                              tag_expression='NoSuchTag')


def test_model_deleted_ancestor_and_stale_project_are_not_navigable(main_window):
    library, folder, record = add_model(main_window)
    child = library.resource_service.createFolder('model', '子目录', folder['id'])
    record['folder_id'] = child['id']
    controller = submit(main_window, '雕刻')
    hit = next(r for r in controller.results if r.get('model_id'))
    assert controller.valid_result(hit)
    record['deleted_at'] = '2026-10-29'
    assert not controller.valid_result(hit)
    record['deleted_at'] = None
    library.resource_service._by_id(folder['id'])['deletedAt'] = '2026-10-29'
    assert not controller.valid_result(hit)
    assert not any(r.get('model_id') for r in search_project(
        main_window.view.scene, library.resource_service, '雕刻'))
    library.resource_service._by_id(folder['id'])['deletedAt'] = None
    assert not controller.valid_result(hit | {'project_id': 'other-project'})


def test_model_metadata_survives_real_save_reopen_and_remains_searchable(tmp_path, qapp):
    scene = make_scene()
    scene.model_records = [sample_payload(name='Carved Wood', notes='雕刻', tags=['Wood'])]
    path = create_project(tmp_path / 'models.prism', scene)
    reopened = make_scene()
    open_project(path, reopened)
    from prism.workspace_service import ResourceTreeService
    service = ResourceTreeService(reopened)
    hits = search_project(reopened, service, '雕刻 Wood')
    assert [r['model_id'] for r in hits] == ['model-1']


def test_search_focus_fetches_only_target_page_in_existing_order(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    repo = ModelRepositoryAdapter(open_model_context(path))
    for i in range(160):
        repo.insert(sample_payload(model_id=f'm{i:03}', name=f'Chair {i:03}'))
    page = repo.query(ModelQuery(limit=72, focus_id='m153'))
    assert page.offset == 144 and page.total == 160
    assert 'm153' in {m.model_id for m in page.items}
    assert len(page.items) == 16
    reverse = repo.query(ModelQuery(limit=72, sort='-name', focus_id='m153'))
    assert reverse.offset == 0
    assert 'm153' in {m.model_id for m in reverse.items}


def test_real_model_result_opens_folder_and_selects_beyond_first_page(main_window, qtbot, tmp_path):
    library, folder, record = add_model(main_window)
    scene = main_window.view.scene
    path = create_project(tmp_path / 'navigate.prism', scene)
    repo = ModelRepositoryAdapter(open_model_context(path))
    for i in range(80):
        repo.insert(sample_payload(model_id=f'm{i:03}', name=f'Chair {i:03}', folder_id=folder['id']))
    scene.model_records = list(repo.list_payloads())
    page = main_window.model_library
    page.bind_scope(scene.project_id, getattr(main_window.view, '_trace_open_epoch', 0), refresh=False)
    service = ModelLibraryService(repo, readonly=True)
    page.set_service(service)
    try:
        controller = submit(main_window, 'Chair 079')
        hit = next(r for r in controller.results if r.get('model_id') == 'm079')
        with patch.object(main_window.model_composition, 'ensure'):
            controller.open_result(hit)
        qtbot.waitUntil(lambda: page.selected_id == 'm079', timeout=15000)
        assert page._folder_id() == folder['id']
        assert page.details._record.model_id == 'm079'
        assert len(page._summaries) <= 72
        assert controller.active  # Keep the existing back-to-results navigation.
        assert main_window.tabs.currentWidget() is page
    finally:
        page._pool.waitForDone(10000)
        service.close()


def test_durable_metadata_edits_refresh_search_but_render_only_changes_do_not(main_window, tmp_path):
    library, folder, record = add_model(main_window)
    scene = main_window.view.scene
    path = create_project(tmp_path / 'edit.prism', scene)
    repo = ModelRepositoryAdapter(open_model_context(path))
    scene.model_records = list(repo.list_payloads())
    composition = main_window.model_composition
    key = composition._key()
    service = ModelLibraryService(repo, on_commit=lambda before, after: composition._commit(key, before, after))
    try:
        controller = submit(main_window, '雕刻')
        with patch.object(controller, 'schedule') as schedule:
            service.rename(record['id'], '替换后的模型')
            service.update_metadata(record['id'], notes='新备注', tags=['NewTag'],
                                    expected_revision=repo.get(record['id']).record_revision)
            assert schedule.called
        controller.submit()
        assert not any(r.get('model_id') for r in controller.results)
        assert any(r.get('model_id') for r in submit(main_window, '新备注 NewTag').results)
        assert repo.get(record['id']).name == '替换后的模型'
        before = scene.model_records[0]
        after = dict(before, thumbnail={'state': 'ready', 'cache_key': 'a' * 64})
        with patch.object(controller, 'schedule') as schedule:
            composition._commit(key, before, after)
            assert not schedule.called
    finally:
        service.close()
