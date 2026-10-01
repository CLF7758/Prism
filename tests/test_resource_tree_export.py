"""左侧树右键「导出…」的界面侧：菜单项、接线，以及真的写出文件。

服务层的导出规则在 `tests/test_resource_export.py` 里测；这里测的是
用户那条路径 —— 在分区根 / 文件夹 / 页面上右键能不能看到「导出…」、
点下去发出的参数对不对、主窗口有没有接住、以及走完整条链路之后磁盘上
是不是真的多了那棵目录树。
"""
import os
import zipfile
from unittest.mock import patch

from PyQt6 import QtWidgets

from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView


def export_action(menu):
    """菜单里的「导出…」（测试跑在英文源字符串下）。"""
    for action in menu.actions():
        if action.text() == 'Export...':
            return action
    return None


def a_tree(qtbot):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    requests = []
    view.export_requested.connect(lambda *args: requests.append(args))
    return service, model, view, requests


# ── 三个层级都要有「导出…」 ──────────────────────────────────


def test_section_row_exports_the_whole_section(qtbot):
    _service, model, view, requests = a_tree(qtbot)

    action = export_action(view.menu_for_index(model.index(0, 0)))

    assert action is not None
    action.trigger()
    assert requests == [('canvas', None)]


def test_section_blank_row_exports_the_whole_section(qtbot):
    _service, model, view, requests = a_tree(qtbot)

    blank = model.index(0, 0, model.index(2, 0))        # 文档分区的空白行
    action = export_action(view.menu_for_index(blank))

    assert action is not None
    action.trigger()
    assert requests == [('document', None)]


def test_folder_row_exports_that_folder(qtbot):
    service, model, view, requests = a_tree(qtbot)
    folder = service.createFolder('mindmap', '资料')

    action = export_action(
        view.menu_for_index(model.index_for_id(folder['id'])))

    assert action is not None
    action.trigger()
    assert requests == [('mindmap', folder['id'])]


def test_page_row_exports_that_page(qtbot):
    service, model, view, requests = a_tree(qtbot)
    page = service.createPage('canvas', '甲画布', 'canvas-1')

    action = export_action(
        view.menu_for_index(model.index_for_id(page['id'])))

    assert action is not None
    action.trigger()
    assert requests == [('canvas', page['id'])]


def test_tag_rows_keep_their_own_menu(qtbot):
    """标签分区的右键菜单不该出现「导出…」。"""
    _service, model, view, _requests = a_tree(qtbot)

    menu = view.menu_for_index(model.index(3, 0))

    assert export_action(menu) is None


# ── 主窗口接线 ──────────────────────────────────────────────


def test_the_window_routes_export_requests_to_the_workflow(main_window,
                                                           monkeypatch):
    from prism.actions import export_workflow

    calls = []
    monkeypatch.setattr(
        export_workflow, 'export_resource_subtree',
        lambda view, section, node_id=None: calls.append((view, section,
                                                          node_id)))

    window = main_window
    window.view.category_panel.resource_export_requested.emit('document', None)

    assert calls == [(window.view, 'document', None)]


def add_document_page(window, page_id, title, html='<p>你好</p>'):
    """把一个文档页同时放进资源树和旧的页面表（导出从后者读内容）。"""
    panel = window.view.category_panel
    node = panel.resource_service.createPage('document', title, page_id)
    window.view.scene.workspace_pages.append(
        {'id': page_id, 'kind': 'document', 'title': title,
         'content': html, 'tags': []})
    return node


def wait_for_export(qtbot, window, path):
    """等这次导出真的收尾：目标出现，而且写盘线程已经结束。

    只看文件在不在是不够的 —— `.docx` 是边生成边写的，线程结束之后它
    才是一份完整文件。
    """
    qtbot.waitUntil(lambda: os.path.exists(path), timeout=20000)
    assert window.view.worker.wait(20000), '导出线程没有在超时前结束'


def test_exporting_a_folder_from_the_window_writes_files(
        main_window, tmpdir, qtbot):
    from prism.actions import export_workflow

    window = main_window
    panel = window.view.category_panel
    folder = panel.resource_service.createFolder('document', '章节')
    add_document_page(window, 'doc-1', '第一章')
    panel.resource_service.moveBatch(
        [next(n['id'] for n in panel.resource_service.listChildren('document')
              if n.get('pageId') == 'doc-1')], folder['id'], 'document')

    with patch.object(export_workflow, 'ask_export_shape',
                      return_value='folder'), \
            patch.object(QtWidgets.QFileDialog, 'getExistingDirectory',
                         return_value=str(tmpdir)):
        export_workflow.export_resource_subtree(window.view, 'document',
                                                folder['id'])

    target = os.path.join(str(tmpdir), '章节', '第一章.docx')
    wait_for_export(qtbot, window, target)
    with zipfile.ZipFile(target) as archive:
        assert 'word/document.xml' in archive.namelist()


def test_exporting_a_section_as_zip_from_the_window(
        main_window, tmpdir, qtbot):
    from prism.actions import export_workflow

    window = main_window
    add_document_page(window, 'doc-1', '笔记')
    archive_path = os.path.join(str(tmpdir), '文档.zip')

    with patch.object(export_workflow, 'ask_export_shape',
                      return_value='zip'), \
            patch.object(QtWidgets.QFileDialog, 'getSaveFileName',
                         return_value=(archive_path, 'Zip (*.zip)')):
        export_workflow.export_resource_subtree(window.view, 'document', None)

    wait_for_export(qtbot, window, archive_path)
    with zipfile.ZipFile(archive_path) as archive:
        assert '文档/笔记.docx' in archive.namelist()


def test_exporting_a_canvas_writes_its_assets_untouched(
        main_window, tmpdir, qtbot, imgdata3x3, imgfilename3x3):
    """画布页导出的是素材**原来那份字节**，不是重新编码的一张图。"""
    from PyQt6 import QtGui

    from prism.actions import export_workflow
    from prism.items import PrismPixmapItem

    window = main_window
    panel = window.view.category_panel
    page = panel.resource_service.createPage('canvas', '素材画布', 'canvas-1')
    window.view.scene.workspace_pages.append(
        {'id': 'canvas-1', 'kind': 'canvas', 'title': '素材画布',
         'content': None, 'tags': []})
    item = PrismPixmapItem(QtGui.QImage(imgfilename3x3), imgfilename3x3)
    item.set_source_blob(imgdata3x3)
    item._canvas_id = 'canvas-1'
    window.view.scene.addItem(item)

    with patch.object(export_workflow, 'ask_export_shape',
                      return_value='folder'), \
            patch.object(QtWidgets.QFileDialog, 'getExistingDirectory',
                         return_value=str(tmpdir)):
        export_workflow.export_resource_subtree(window.view, 'canvas',
                                                page['id'])

    target = os.path.join(str(tmpdir), '素材画布', 'test3x3.png')
    wait_for_export(qtbot, window, target)
    with open(target, 'rb') as handle:
        assert handle.read() == imgdata3x3


def test_assets_of_another_canvas_stay_out_of_the_export(
        main_window, tmpdir, qtbot, imgfilename3x3):
    """别的画布上的素材不能混进这一次导出。"""
    from PyQt6 import QtGui

    from prism.actions import export_workflow
    from prism.items import PrismPixmapItem

    window = main_window
    panel = window.view.category_panel
    page = panel.resource_service.createPage('canvas', '素材画布', 'canvas-1')
    window.view.scene.workspace_pages.append(
        {'id': 'canvas-1', 'kind': 'canvas', 'title': '素材画布',
         'content': None, 'tags': []})
    item = PrismPixmapItem(QtGui.QImage(imgfilename3x3), imgfilename3x3)
    item._canvas_id = 'another-canvas'
    window.view.scene.addItem(item)

    with patch.object(export_workflow, 'ask_export_shape',
                      return_value='folder'), \
            patch.object(QtWidgets.QFileDialog, 'getExistingDirectory',
                         return_value=str(tmpdir)):
        export_workflow.export_resource_subtree(window.view, 'canvas',
                                                page['id'])

    exported = os.path.join(str(tmpdir), '素材画布')
    wait_for_export(qtbot, window, exported)
    assert os.listdir(exported) == []


def test_cancelling_the_shape_dialog_exports_nothing(main_window, tmpdir):
    from prism.actions import export_workflow

    window = main_window
    add_document_page(window, 'doc-1', '笔记')

    with patch.object(export_workflow, 'ask_export_shape', return_value=None):
        export_workflow.export_resource_subtree(window.view, 'document', None)

    assert os.listdir(str(tmpdir)) == []


def test_an_empty_selection_is_reported_without_a_dialog(
        main_window, tmpdir):
    from prism.actions import export_workflow

    window = main_window
    with patch.object(export_workflow, 'ask_export_shape') as asked:
        # 树里没有这个节点：应该在问形态之前就停下。
        export_workflow.export_resource_subtree(window.view, 'document',
                                                'does-not-exist')

    asked.assert_not_called()
    assert os.listdir(str(tmpdir)) == []
