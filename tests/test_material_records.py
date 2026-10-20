"""Material metadata and UI must survive actual project saves and switches."""
import copy
import sqlite3
from unittest.mock import patch

import pytest
from PyQt6.QtCore import Qt

from prism.fileio.sql import SQLiteIO
from prism.fileio.errors import PrismFileIOError
from prism.material_service import validate_material_records


def test_drafts_have_independent_identity_and_allow_same_display_name(main_window):
    panel = main_window.view.category_panel
    service = panel.material_service
    first = service.create(name='木头')
    second = service.create(name='木头')
    assert first['id'] != second['id']
    assert first['status'] == 'draft'
    assert first['textures'] == {} and first['nativePackages'] == []
    first['name'] = '改副本'
    assert service.get(first['id'])['name'] == '木头'
    assert not main_window.view.scene.workspace_pages
    assert service.create()['name'] == '未命名材质'
    assert service.create()['name'] == '未命名材质 2'


def test_record_edits_validate_before_mutating(main_window):
    service = main_window.view.category_panel.material_service
    record = service.create()
    before = copy.deepcopy(service.records)
    with pytest.raises(ValueError):
        service.update(record['id'], name=' ', notes='不能半保存')
    with pytest.raises(ValueError):
        service.update(record['id'], name='有效', notes=123)
    with pytest.raises(ValueError):
        service.create('missing-folder')
    assert service.records == before
    service.update(record['id'], name=' 金属 ', notes='用途说明')
    updated = service.get(record['id'])
    assert updated['name'] == '金属'
    assert updated['createdAt'] == record['createdAt']
    assert updated['updatedAt'] >= record['updatedAt']


def test_folder_scopes_hide_trashed_folder_records_and_restore(main_window):
    panel = main_window.view.category_panel
    a = panel.resource_service.createFolder('material', '木头')
    b = panel.resource_service.createFolder('material', '金属')
    child = panel.resource_service.createFolder('material', '原木', a['id'])
    service = panel.material_service
    service.create(a['id'], '松木')
    service.create(b['id'], '铁')
    service.create(child['id'], '榉木')
    assert len(service.list_records()) == 3
    assert len(service.list_records(a['id'])) == 2
    assert len(service.list_records(query='铁')) == 1
    panel.resource_service.trash([a['id']], '2026-10-19')
    assert [r['name'] for r in service.list_records()] == ['铁']
    assert len(service.records) == 3
    panel.resource_service.restore_deleted([a['id']])
    assert len(service.list_records()) == 3


def test_new_material_button_and_rapid_switch_flush_last_edit(main_window, qtbot):
    window = main_window
    window.open_material_library()
    page = window.material_library
    qtbot.mouseClick(page.new_material_button, Qt.MouseButton.LeftButton)
    first = page.selected_id
    qtbot.keyClicks(page.details.name, 'Wood')
    page.details.notes.setPlainText('最后输入，切换前必须收到')
    # Create a second record before the 500 ms edit timer fires.
    qtbot.mouseClick(page.new_material_button, Qt.MouseButton.LeftButton)
    second = page.selected_id
    assert second != first
    assert page.records.get(first)['name'] == 'Wood'
    assert page.records.get(first)['notes'] == '最后输入，切换前必须收到'
    page.details.notes.setPlainText('第二条说明')
    page.materials.setCurrentRow(0)
    assert page.selected_id == first
    assert page.materials.currentRow() == 0
    assert page.records.get(second)['notes'] == '第二条说明'
    assert page.details.name.text() == 'Wood'


def test_debounce_does_not_reset_editor_cursor_or_undo(main_window, qtbot):
    page = main_window.material_library
    main_window.open_material_library()
    page.create_material()
    page.details.notes.setPlainText('开头与结尾')
    cursor = page.details.notes.textCursor()
    cursor.setPosition(2)
    page.details.notes.setTextCursor(cursor)
    qtbot.keyClicks(page.details.notes, 'ABC')
    position = page.details.notes.textCursor().position()
    qtbot.waitUntil(lambda: page.records.get(page.selected_id)['notes'] == '开头ABC与结尾')
    assert page.details.notes.textCursor().position() == position
    assert page.details.notes.document().isUndoAvailable()


def test_switch_page_flushes_and_restores_folder_search_selection(main_window):
    window = main_window
    page = window.material_library
    folder = window.view.category_panel.resource_service.createFolder('material', '木头')
    window.open_material_library(folder['id'])
    page.create_material()
    record_id = page.selected_id
    page.details.name.setText('木头样例')
    page.details.notes.setPlainText('立即切页')
    window.tabs.setCurrentWidget(window.canvas_page)
    assert page.records.get(record_id)['notes'] == '立即切页'
    window.tabs.setCurrentWidget(page)
    page.search.setText('样例')
    window.open_material_library()
    window.open_material_library(folder['id'])
    assert page.search.text() == '样例'
    assert page.selected_id == record_id
    assert page.details.name.text() == '木头样例'


def test_metadata_and_view_state_roundtrip_and_same_project_reload(main_window, tmp_path):
    window = main_window
    page = window.material_library
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('material', '木头')
    window.open_material_library(folder['id'])
    page.create_material()
    record_id = page.selected_id
    page.details.name.setText('原木 <样例>')
    page.details.notes.setPlainText('第一行\n第二行')
    page.search.setText('原木')
    page.store_to_scene()
    filename = str(tmp_path / 'material-records.prism')
    SQLiteIO(filename, window.view.scene, create_new=True).write()
    # Leave stale text in the former inspector, then load the saved revision.
    page.details.notes.setPlainText('不应覆盖重开后的记录')
    SQLiteIO(filename, window.view.scene, readonly=True).read()
    panel._install_resource_service()
    page.load_from_scene()
    assert page.folder_id == folder['id']
    assert page.selected_id == record_id
    assert page.search.text() == '原木'
    assert page.details.notes.toPlainText() == '第一行\n第二行'
    assert page.records.get(record_id)['name'] == '原木 <样例>'
    assert len(window.view.scene.material_records) == 1


def test_rebuilding_tree_flushes_pending_material_description(main_window):
    window = main_window
    window.open_material_library()
    page = window.material_library
    page.create_material()
    record_id = page.selected_id
    page.details.notes.setPlainText('目录刷新不能吞掉这句')
    window.view.category_panel.rebuild_workspace_tree()
    page.refresh()
    assert page.records.get(record_id)['notes'] == '目录刷新不能吞掉这句'


def test_project_switch_discards_stale_timer_and_does_not_cross_write(main_window, qtbot):
    window = main_window
    window.open_material_library()
    page = window.material_library
    page.create_material()
    page.details.notes.setPlainText('旧工程待回传内容')
    scene = window.view.scene
    scene.material_records = []
    scene.material_library_state = {}
    scene.workspace_nodes = []
    scene.project_id = 'other-project'
    window.view.category_panel._install_resource_service()
    page.load_from_scene()
    qtbot.wait(550)
    assert scene.material_records == []
    assert page.selected_id is None
    assert page.details.notes.toPlainText() == ''
    assert page.materials.count() == 0


def test_old_project_without_material_table_loads_an_empty_library(main_window, tmp_path):
    scene = main_window.view.scene
    filename = str(tmp_path / 'old-project.prism')
    SQLiteIO(filename, scene, create_new=True).write()
    with sqlite3.connect(filename) as connection:
        connection.execute('DROP TABLE material_records')
        connection.execute("DELETE FROM prism_metadata WHERE key='material_library_state'")
    main_window.view.category_panel.material_service.create(name='旧内存内容')
    SQLiteIO(filename, scene, readonly=True).read()
    assert scene.material_records == []
    assert scene.material_library_state == {}


def test_corrupted_material_data_fails_instead_of_becoming_empty(main_window, tmp_path):
    scene = main_window.view.scene
    filename = str(tmp_path / 'corrupt-material.prism')
    main_window.view.category_panel.material_service.create()
    SQLiteIO(filename, scene, create_new=True).write()
    with sqlite3.connect(filename) as connection:
        connection.execute("UPDATE material_records SET payload = '{broken json'")
    with pytest.raises(PrismFileIOError, match='材质库数据损坏'):
        SQLiteIO(filename, scene, readonly=True).read()


def test_ctrl_save_flushes_current_material_form(main_window, tmp_path, qtbot):
    window = main_window
    window.open_material_library()
    page = window.material_library
    page.create_material()
    page.details.notes.setPlainText('保存按钮前的最后输入')
    from prism.actions.actions import actions
    filename = str(tmp_path / 'material-save.prism')
    record_id = page.selected_id
    # Keep the real save handler and worker; only answer its filename dialog.
    with patch('PyQt6.QtWidgets.QFileDialog.getSaveFileName', return_value=(filename, '')):
        actions['save'].qaction.trigger()
    qtbot.waitUntil(lambda: window.view.filename == filename, timeout=15000)
    with sqlite3.connect(filename) as connection:
        payload = connection.execute('SELECT payload FROM material_records WHERE id=?',
                                     (record_id,)).fetchone()[0]
    import json
    assert json.loads(payload)['notes'] == '保存按钮前的最后输入'


def test_material_scroll_and_selection_restore_per_folder(main_window, qtbot):
    window = main_window
    window.resize(1280, 760)
    window.show()
    window.open_material_library()
    page = window.material_library
    for i in range(30):
        page.records.create(name=f'Material {i}')
    qtbot.wait(50)
    page.materials.setCurrentRow(20)
    record_id = page.selected_id
    page.materials.verticalScrollBar().setValue(18)
    scroll = page.materials.verticalScrollBar().value()
    assert scroll > 0
    folder = page.service.createFolder('material', '收藏')
    window.open_material_library(folder['id'])
    window.open_material_library()
    assert page.selected_id == record_id
    assert page.materials.verticalScrollBar().value() == scroll


def test_validation_rejects_wrong_project_and_duplicate_ids(main_window):
    service = main_window.view.category_panel.material_service
    record = service.create()
    with pytest.raises(ValueError):
        validate_material_records([record], 'another-project')
    with pytest.raises(ValueError):
        validate_material_records([record, copy.deepcopy(record)], service.project_id)
    record['preview']['invalid'] = float('nan')
    with pytest.raises(ValueError):
        validate_material_records([record], service.project_id)


def test_actual_new_project_clears_materials_and_folders(main_window):
    window = main_window
    page = window.material_library
    old_project = window.view.scene.project_id
    folder = page.service.createFolder('material', '旧工程文件夹')
    window.open_material_library(folder['id'])
    page.create_material()
    page.details.notes.setPlainText('旧工程内容')
    window.view.on_action_new_scene()
    window.open_material_library()
    assert window.view.scene.project_id != old_project
    assert page.records.records == []
    assert page.service.listChildren('material') == []
    assert page.selected_id is None


def test_edit_marks_dirty_before_debounce_to_allow_close_confirmation(main_window):
    window = main_window
    window.open_material_library()
    page = window.material_library
    page.create_material()
    window.view.undo_stack.setClean()
    window.view._content_dirty = False
    page.details.notes.setPlainText('还没有等到半秒')
    assert window.view.is_dirty()


def test_sidebar_entry_returns_to_last_folder_from_other_page(main_window):
    window = main_window
    page = window.material_library
    folder = page.service.createFolder('material', '常用')
    window.open_material_library(folder['id'])
    page.create_material()
    selected = page.selected_id
    window.tabs.setCurrentWidget(window.canvas_page)
    tree = window.view.category_panel.resource_tree
    tree._on_clicked(tree.model().index(3, 0))
    assert window.tabs.currentWidget() is page
    assert page.folder_id == folder['id']
    assert page.selected_id == selected
    # Clicking the library root while already browsing still opens the root.
    tree._on_clicked(tree.model().index(3, 0))
    assert page.folder_id is None
