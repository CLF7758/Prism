from types import SimpleNamespace
from unittest.mock import patch
from PyQt6 import QtCore, QtGui, QtTest
from prism.project_search import search_project, plain_text
from prism.widgets.resource_tree_model import TAG_NAME_ROLE, NODE_TYPE_ROLE
from tests.test_material_features import image_item


def results(panel, query):
    text, expression = panel._split_query(query)
    return search_project(panel.scene, panel.resource_service, text, tag_expression=expression)


def test_plain_text_preserves_inline_words_and_ignores_scripts():
    assert plain_text('<p>古<b>风</b>&amp;自然光</p><script>隐藏</script>') == '古风&自然光'


def test_search_covers_deep_folders_docs_nodes_categories_and_aliases(main_window):
    window = main_window
    library = window.view.category_panel
    service = library.resource_service
    folder = service.createFolder('mindmap', '产品目录')
    document = window.workspace_service.add('document', '说明')
    document['content'] = '<p>古<b>风</b>自然光</p>'
    mind = window.workspace_service.add('mindmap', '方案')
    mind['content'] = {'data': {'text': '根'}, 'children': [
        {'data': {'text': '分支'}, 'children': [{'data': {'text': '三级搜索目标'}}]}]}
    library.rebuild_workspace_tree()
    item = image_item(window.view.scene, ['建筑/红墙'])
    item._categories = ['摄影/风景']
    window.view.scene.tag_system.add_sibling('red-wall', '建筑/红墙')
    assert any(r['node_id'] == folder['id'] for r in results(library, '产品目录') if r['kind'] == 'folder')
    assert any(r['kind'] == 'document_text' for r in results(library, '古风'))
    hit = next(r for r in results(library, '三级搜索目标') if r['kind'] == 'mindmap_node')
    assert hit['node_path'] == [0, 0]
    assert any(r.get('item') is item for r in results(library, 'red-wall'))
    assert any(r.get('item') is item for r in results(library, '摄影'))


def test_global_results_ignore_canvas_filters_and_do_not_hide_assets(main_window):
    window = main_window
    library = window.view.category_panel
    first = window.workspace_service.add('canvas', '第一张')
    second = window.workspace_service.add('canvas', '第二张')
    item = image_item(window.view.scene, ['隐藏目标'])
    item._canvas_id = second['id']
    window.open_workspace_page('canvas', first['id'])
    library._min_rating = 5
    library._apply_filter()
    assert not item.isVisible()
    library.search.setText('隐藏目标')
    library._refresh_project_search()
    hits = results(library, '隐藏目标')
    assert any(r.get('item') is item for r in hits)
    assert not item.isVisible()
    group = next(library.search_results.topLevelItem(i) for i in range(library.search_results.topLevelItemCount())
                 if library.search_results.topLevelItem(i).text(0) == '素材')
    library._activate_search_result(group.child(0))
    assert window.current_page_id == second['id']
    assert item.isVisible() and item.isSelected()
    library.clear_button.click()
    assert window.current_page_id == first['id']
    assert library._min_rating == 5
    assert not item.isVisible()


def test_live_unsaved_and_empty_document_override_old_content(main_window):
    library = main_window.view.category_panel
    page = main_window.workspace_service.add('document', '说明')
    page['content'] = '<p>旧文字</p>'
    library.rebuild_workspace_tree()
    live = SimpleNamespace(page_id=page['id'], _html='<p>未保存新文字</p>')
    assert any(r['kind'] == 'document_text' for r in search_project(library.scene, library.resource_service, '新文字', [live]))
    live._html = ''
    assert not search_project(library.scene, library.resource_service, '旧文字', [live])


def test_unused_nested_tags_populate_sidebar_and_update_after_rename(view):
    library = view.category_panel
    view.scene.tag_names = ['风格/古风', '未使用标签']
    library.refresh_tags()
    model = library.resource_tree.model()
    child = model.index_for_id('catalogue-tag:风格/古风')
    assert child.isValid() and child.data(TAG_NAME_ROLE) == '风格/古风'
    assert child.parent().data(TAG_NAME_ROLE) == '风格'
    assert child.data(NODE_TYPE_ROLE) == 'tag'
    assert not (model.flags(child) & QtCore.Qt.ItemFlag.ItemIsDragEnabled)
    view.scene.tag_names = ['风格/现代']
    library.refresh_tags()
    assert not model.index_for_id('catalogue-tag:风格/古风').isValid()
    assert model.index_for_id('catalogue-tag:风格/现代').isValid()
    assert all(n['section'] != 'tag' for n in library.resource_service.nodes)


def test_selecting_new_image_resets_channels_and_restores_original(view):
    a, b = image_item(view.scene), image_item(view.scene)
    panel = view._detail_panel
    a.setSelected(True)
    panel._on_selection_changed()
    panel._channel_combo.setCurrentIndex(1)
    panel._exposure_slider.setValue(10)
    view.scene.clearSelection()
    b.setSelected(True)
    panel._on_selection_changed()
    assert panel._channel_combo.currentData() == 'rgb'
    assert panel._exposure_slider.value() == 0
    panel._channel_combo.setCurrentIndex(2)
    panel._on_selection_changed()
    assert panel._channel_combo.currentData() == 'g'
    view.scene.clearSelection()
    a.setSelected(True)
    panel._on_selection_changed()
    assert panel._channel_combo.currentData() == 'rgb'
    assert not a._preview_adjusted


def test_drag_start_builds_same_stable_mime_used_for_drop(qtbot):
    from tests.test_resource_tree_drag import make_tree, select_only, row_rect
    service, model, view = make_tree(qtbot, show=True)
    folder = service.createFolder('canvas', '目标')
    nested = service.createFolder('canvas', '三级', folder['id'])
    page = service.createPage('canvas', '页面', 'page-id')
    select_only(view, model, page['id'])
    captured = []
    def simulate(drag, action):
        mime = drag.mimeData()
        captured.extend(model.dragged_ids(mime))
        pos = row_rect(view, model.index_for_id(nested['id'])).center()
        enter = QtGui.QDragEnterEvent(pos, action, mime, QtCore.Qt.MouseButton.LeftButton,
                                     QtCore.Qt.KeyboardModifier.NoModifier)
        QtCore.QCoreApplication.sendEvent(view.viewport(), enter)
        assert enter.isAccepted()
        move = QtGui.QDragMoveEvent(pos, action, mime, QtCore.Qt.MouseButton.LeftButton,
                                    QtCore.Qt.KeyboardModifier.NoModifier)
        QtCore.QCoreApplication.sendEvent(view.viewport(), move)
        assert move.isAccepted()
        drop = QtGui.QDropEvent(QtCore.QPointF(pos), action, mime, QtCore.Qt.MouseButton.LeftButton,
                               QtCore.Qt.KeyboardModifier.NoModifier)
        QtCore.QCoreApplication.sendEvent(view.viewport(), drop)
        assert drop.isAccepted()
        return action
    with patch.object(QtGui.QDrag, 'exec', simulate):
        source = row_rect(view, model.index_for_id(page['id'])).center()
        QtTest.QTest.mousePress(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=source)
        for offset in (16, 40, 64):
            point = source + QtCore.QPoint(offset, 0)
            event = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(point),
                QtCore.QPointF(view.viewport().mapToGlobal(point)), QtCore.Qt.MouseButton.NoButton,
                QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.NoModifier)
            QtCore.QCoreApplication.sendEvent(view.viewport(), event)
            if captured:
                break
        QtTest.QTest.mouseRelease(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=source)
    assert captured == [page['id']]
    assert service._by_id(page['id'])['parentId'] == nested['id']


def test_interval_editor_leaves_room_for_full_value(main_window, qtbot):
    from prism.widgets.extract_frames import ExtractFramesDialog
    dialog = ExtractFramesDialog(main_window.view,
        SimpleNamespace(filename='sample.ts', _video_url=None))
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.interval.setValue(60.0)
    QtCore.QCoreApplication.processEvents()
    assert dialog.interval.minimumWidth() >= 120
    edit = dialog.interval.lineEdit()
    assert edit.contentsRect().width() >= edit.fontMetrics().horizontalAdvance('60.0')
