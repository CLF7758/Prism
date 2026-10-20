from unittest.mock import patch
from PyQt6 import QtCore
from prism import tags
from prism.project_search import search_project
from prism.widgets import tag_panel
from tests.test_search_controller import submit


def make_material(window):
    library = window.view.category_panel
    folder = library.resource_service.createFolder('material', '木材目录')
    material = library.material_service.create(folder['id'], name='Old Oak 木材')
    library.material_service.update(material['id'], name=material['name'], notes='用于古堡家具')
    library.material_service.configure(material['id'], tags=['Wood/Oak', '木材'])
    return library, folder, material


def test_global_search_material_name_notes_tags_and_open(main_window, qtbot):
    main_window.show()
    library, folder, material = make_material(main_window)
    for text in ('Old Oak', '古堡', 'Wood/Oak', '木材'):
        controller = submit(main_window, text)
        hit = next(r for r in controller.results if r.get('material_id') == material['id'])
        assert hit['page_kind'] == 'material' and '木材目录' in hit['path']
    qtbot.waitUntil(lambda: main_window.material_library.materials.ready, timeout=15000)
    controller.open_result(hit)
    page = main_window.material_library
    assert main_window.tabs.currentWidget() is page
    assert page.folder_id == folder['id'] and page.selected_id == material['id']
    assert page.details.tag_edit.text() == 'Wood/Oak, 木材'
    assert '古堡' in page.details.notes.toPlainText()


def test_material_tags_count_filter_rename_delete_undo(main_window):
    library, folder, material = make_material(main_window)
    scene = main_window.view.scene
    assert tags.tag_names_of_items(scene)['Wood/Oak'] == 1
    library._browse_tag('Wood/Oak')
    controller = main_window.search_controller
    assert any(r.get('material_id') == material['id'] for r in controller.results)
    tag_panel.rename_tag(main_window.view, 'Wood/Oak', 'Timber')
    assert library.material_service.get(material['id'])['tags'] == ['Wood/Timber', '木材']
    scene.undo_stack.undo()
    assert library.material_service.get(material['id'])['tags'] == ['Wood/Oak', '木材']
    tag_panel.delete_tag(main_window.view, 'Wood/Oak')
    assert library.material_service.get(material['id'])['tags'] == ['木材']
    scene.undo_stack.undo()
    assert 'Wood/Oak' in library.material_service.get(material['id'])['tags']


def test_deleted_folder_material_and_stale_hit_not_navigable(main_window):
    library, folder, material = make_material(main_window)
    controller = submit(main_window, 'Old Oak')
    hit = next(r for r in controller.results if r.get('material_id') == material['id'])
    node = library.resource_service._by_id(folder['id'])
    node['deletedAt'] = '2026-10-19T00:00:00+00:00'
    assert not controller.valid_result(hit)
    results = search_project(main_window.view.scene, library.resource_service, 'Old Oak')
    assert not any(r.get('material_id') == material['id'] for r in results)


def test_render_only_changes_do_not_requery_global_search(main_window):
    library, folder, material = make_material(main_window)
    controller = submit(main_window, 'Old Oak')
    with patch.object(controller, 'schedule') as schedule:
        library.material_service.configure(material['id'], slot_settings=('roughness', {'value':.2}))
        assert not schedule.called
        library.material_service.configure(material['id'], tags=['New tag'])
        assert schedule.called


def test_existing_search_tree_uses_same_material_navigation(main_window, qtbot):
    library, folder, material = make_material(main_window)
    library.search.setText('Old Oak')
    library._refresh_project_search()
    result = next(r for r in library.project_search_results('Old Oak') if r.get('material_id')==material['id'])
    library.activate_project_result(result)
    assert main_window.material_library.selected_id == material['id']
    assert main_window.material_library.folder_id == folder['id']
