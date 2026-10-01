"""左侧树右键「导入…」：菜单、接线，以及真的把文件导进工程。

扫描规则在 `tests/test_resource_import.py` 里测；这里测用户那条路径 ——
右键能不能看到「导入…」、点下去发出的参数对不对、主窗口接没接住，以及
走完整条链路之后树里、页面表里、画布上是不是真的多了东西。
"""
import os
import zipfile
from unittest.mock import patch

import pytest
from PyQt6 import QtWidgets

from prism.workspace_service import ResourceTreeService
from prism.widgets.import_preview import ImportPreviewDialog
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView


def import_action(menu):
    """菜单里的「导入…」（测试跑在英文源字符串下）。"""
    for action in menu.actions():
        if action.text() == 'Import...':
            return action
    return None


def a_tree(qtbot):
    service = ResourceTreeService('project')
    model = ResourceTreeModel(service)
    view = ResourceTreeView()
    qtbot.addWidget(view)
    view.setModel(model)
    requests = []
    view.import_requested.connect(lambda *args: requests.append(args))
    return service, model, view, requests


# ── 哪些行有「导入…」 ──────────────────────────────────────


def test_section_row_imports_into_the_whole_section(qtbot):
    _service, model, view, requests = a_tree(qtbot)

    action = import_action(view.menu_for_index(model.index(0, 0)))

    assert action is not None
    action.trigger()
    assert requests == [('canvas', None)]


def test_folder_row_imports_into_that_folder(qtbot):
    service, model, view, requests = a_tree(qtbot)
    folder = service.createFolder('document', '资料')

    action = import_action(
        view.menu_for_index(model.index_for_id(folder['id'])))

    assert action is not None
    action.trigger()
    assert requests == [('document', folder['id'])]


def test_page_rows_have_no_import(qtbot):
    """页面是叶子，装不下子节点 —— 不给导入入口。"""
    service, model, view, _requests = a_tree(qtbot)
    page = service.createPage('mindmap', '大纲', 'mind-1')

    menu = view.menu_for_index(model.index_for_id(page['id']))

    assert import_action(menu) is None


def test_tag_rows_have_no_import(qtbot):
    _service, model, view, _requests = a_tree(qtbot)

    menu = view.menu_for_index(model.index(3, 0))

    assert import_action(menu) is None


# ── 主窗口接线 ──────────────────────────────────────────────


def test_the_window_routes_import_requests(main_window, monkeypatch):
    from prism.actions import import_workflow

    calls = []
    monkeypatch.setattr(
        import_workflow, 'import_resource_subtree',
        lambda view, section, node_id=None: calls.append((view, section,
                                                          node_id)))

    window = main_window
    window.view.category_panel.resource_import_requested.emit('document', None)

    assert calls == [(window.view, 'document', None)]


# ── 端到端 ──────────────────────────────────────────────────


def write_file(root, relative, payload=b'x'):
    path = os.path.join(str(root), *relative.split('/'))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as handle:
        handle.write(payload)
    return path


def make_docx(path, text):
    document = pytest.importorskip('docx').Document()
    document.add_paragraph(text)
    document.save(path)
    return path


def run_import(window, section, node_id, source, shape='folder',
               archive=None):
    """走一遍真实的导入工作流，只把对话框换成固定答案。"""
    from prism.actions import import_workflow

    if shape == 'archive':
        patcher = patch.object(
            QtWidgets.QFileDialog, 'getOpenFileName',
            return_value=(archive, 'Zip (*.zip)'))
    else:
        patcher = patch.object(
            QtWidgets.QFileDialog, 'getExistingDirectory',
            return_value=source)
    with patch.object(import_workflow, 'ask_import_source',
                      return_value=shape), \
            patcher, \
            patch.object(ImportPreviewDialog, 'exec',
                         return_value=QtWidgets.QDialog.DialogCode.Accepted):
        import_workflow.import_resource_subtree(window.view, section, node_id)


def wait_until(qtbot, check, attempts=20, interval=100):
    """等条件成立：先让事件循环转 ``interval`` 毫秒，再看结果。

    素材是另一条线程加载的（加载完 → 进度信号 → 图元进场景 → 完成
    信号 → 归属到画布页），所以先把线程等到底，信号才在队列里等着被
    处理。
    """
    for _ in range(attempts):
        qtbot.wait(interval)
        if check():
            return True
    return False


def test_importing_a_folder_builds_folders_and_pages(main_window, tmpdir,
                                                     qtbot):
    window = main_window
    source = os.path.join(str(tmpdir), '报告')
    make_docx(write_file(source, '周报.docx', b''), '你好，导入的内容')
    write_file(source, '月报/三月.docx', b'')
    make_docx(os.path.join(source, '月报', '三月.docx'), '三月的正文')

    run_import(window, 'document', None, source)

    service = window.view.category_panel.resource_service
    folders = {node['title']: node
               for node in service.listChildren('document')
               if node['nodeType'] == 'folder'}
    assert '报告' in folders
    children = service.listChildren('document', folders['报告']['id'])
    titles = [node['title'] for node in children]
    assert '周报' in titles and '月报' in titles

    pages = {page['title']: page
             for page in window.view.scene.workspace_pages}
    assert '你好，导入的内容' in (pages['周报']['content'] or '')


def test_importing_a_canvas_folder_puts_assets_on_the_new_canvas(
        main_window, tmpdir, qtbot, imgdata3x3):
    window = main_window
    source = os.path.join(str(tmpdir), '参考')
    write_file(source, 'top.png', imgdata3x3)

    run_import(window, 'canvas', None, source)

    service = window.view.category_panel.resource_service
    node = next(node for node in service.listChildren('canvas')
                if node['nodeType'] == 'page' and node['title'] == '参考')

    def filed():
        return any(getattr(item, '_canvas_id', None) == node['pageId']
                   for item in window.view.scene.items_for_save())

    worker = window.view.worker
    assert worker.wait(20000), '加载素材的线程没有在超时前结束'
    filed_ok = wait_until(qtbot, filed)
    assert filed_ok, '素材应该归到新建的画布页上'
    item = next(item for item in window.view.scene.items_for_save()
                if getattr(item, '_canvas_id', None) == node['pageId'])
    assert item.original_bytes() == imgdata3x3, '导入要保住原始字节'


def test_importing_a_zip_uses_the_same_rules(main_window, tmpdir, qtbot):
    window = main_window

    archive = os.path.join(str(tmpdir), '文档.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('笔记.docx', b'x')

    run_import(window, 'document', None, None, shape='archive',
               archive=archive)

    service = window.view.category_panel.resource_service
    # 包里没有顶层目录，zip 的名字就成了容器名
    folders = service.listChildren('document')
    assert [node['title'] for node in folders] == ['文档']
    assert [node['title']
            for node in service.listChildren('document', folders[0]['id'])] \
        == ['笔记']


def test_importing_a_zip_keeps_the_document_content(main_window, tmpdir,
                                                    qtbot):
    """zip 里的真 .docx：内容要真的读进来，不能报"找不到文件"。

    回归：解压出来的临时目录必须活到内容读完 —— 太早清理会让每个
    文档页都变成"Package not found"。
    """
    window = main_window
    source = os.path.join(str(tmpdir), '笔记.docx')
    make_docx(source, '压缩包里的正文')
    archive = os.path.join(str(tmpdir), '文档.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.write(source, '笔记.docx')

    with patch.object(QtWidgets.QMessageBox, 'warning') as warned:
        run_import(window, 'document', None, None, shape='archive',
                   archive=archive)

        pages = {page['title']: page
                 for page in window.view.scene.workspace_pages}
        assert '压缩包里的正文' in (pages['笔记']['content'] or '')
        assert not warned.called, '不该有"读不了这个文件"的报错'


def test_importing_a_zip_keeps_its_assets_readable(main_window, tmpdir,
                                                   qtbot, imgdata3x3):
    """zip 里的素材：解压目录要活到素材那条线程读完。"""
    window = main_window
    archive = os.path.join(str(tmpdir), '图片.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('参考/top.png', imgdata3x3)

    run_import(window, 'canvas', None, None, shape='archive', archive=archive)

    service = window.view.category_panel.resource_service
    node = next(node for node in service.listChildren('canvas')
                if node['nodeType'] == 'page')
    assert window.view.worker.wait(20000)

    def filed():
        return any(getattr(item, '_canvas_id', None) == node['pageId']
                   for item in window.view.scene.items_for_save())

    assert wait_until(qtbot, filed), '压缩包里的素材应该归到画布页上'


def test_a_broken_document_is_reported_in_plain_words(main_window, tmpdir):
    """坏文件要说人话：不是有效的 Word 文件 —— 而不是把临时路径甩出来。"""
    window = main_window
    archive = os.path.join(str(tmpdir), '坏的.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('笔记.docx', b'this is definitely not a docx')

    with patch.object(QtWidgets.QMessageBox, 'warning') as warned:
        run_import(window, 'document', None, None, shape='archive',
                   archive=archive)

    assert warned.called, '读不了的文件要如实报告'
    message = warned.call_args[0][2]
    assert 'is not a valid Word file' in message
    assert 'prism-import-' not in message, '别把解压出来的临时路径给用户'


def test_the_unpacked_copy_is_cleaned_up(main_window, tmpdir):
    """导入收尾之后，解压出来的临时目录不留下来。"""
    import glob
    import tempfile

    window = main_window
    archive = os.path.join(str(tmpdir), '文档.zip')
    with zipfile.ZipFile(archive, 'w') as zf:
        zf.writestr('参考/笔记.docx', b'x')
    pattern = os.path.join(tempfile.gettempdir(), 'prism-import-*')
    before = set(glob.glob(pattern))

    run_import(window, 'document', None, None, shape='archive', archive=archive)

    assert set(glob.glob(pattern)) <= before


def test_a_cancelled_preview_imports_nothing(main_window, tmpdir):
    from prism.actions import import_workflow

    window = main_window
    source = os.path.join(str(tmpdir), '报告')
    make_docx(write_file(source, '周报.docx', b''), '内容')

    with patch.object(import_workflow, 'ask_import_source',
                      return_value='folder'), \
            patch.object(QtWidgets.QFileDialog, 'getExistingDirectory',
                         return_value=source), \
            patch.object(ImportPreviewDialog, 'exec',
                         return_value=QtWidgets.QDialog.DialogCode.Rejected):
        import_workflow.import_resource_subtree(window.view, 'document', None)

    service = window.view.category_panel.resource_service
    assert service.listChildren('document') == []


def test_a_canvas_folder_with_subfolders_nests_them(main_window, tmpdir,
                                                    qtbot, imgdata3x3):
    """既有素材又有子目录的目录：画布页装素材、同名文件夹撑层级。

    回归：子节点一度被挂到画布页上（画布页是叶子，不能当父节点），
    整个导入因此报「父节点必须是同工程、同分区的有效文件夹」。
    """
    window = main_window
    source = os.path.join(str(tmpdir), '图片')
    write_file(source, 'top.png', imgdata3x3)
    write_file(source, '新建文件夹/inner.png', imgdata3x3)

    with patch.object(QtWidgets.QMessageBox, 'warning') as warned:
        run_import(window, 'canvas', None, source)

        service = window.view.category_panel.resource_service
        roots = service.listChildren('canvas')
        folder = next(node for node in roots if node['title'] == '图片'
                      and node['nodeType'] == 'folder')
        page = next(node for node in roots if node['title'] == '图片'
                    and node['nodeType'] == 'page')

        inner = service.listChildren('canvas', folder['id'])
        assert [node['title'] for node in inner] == ['新建文件夹']
        assert inner[0]['parentId'] == folder['id']

        assert window.view.worker.wait(20000)

        def both_filed():
            filed = {getattr(item, '_canvas_id', None)
                     for item in window.view.scene.items_for_save()}
            return {page['pageId'], inner[0]['pageId']} <= filed

        assert wait_until(qtbot, both_filed), '两份素材各自归到自己的画布'
        assert not warned.called, '这次导入不该报任何错误'


def test_importing_into_a_folder_lands_inside_it(main_window, tmpdir):
    window = main_window
    service = window.view.category_panel.resource_service
    folder = service.createFolder('document', '资料')
    source = os.path.join(str(tmpdir), '报告')
    make_docx(write_file(source, '周报.docx', b''), '内容')

    run_import(window, 'document', folder['id'], source)

    titles = [node['title']
              for node in service.listChildren('document', folder['id'])]
    assert '报告' in titles
    inner = next(node for node in service.listChildren('document', folder['id'])
                 if node['title'] == '报告')
    assert [node['title']
            for node in service.listChildren('document', inner['id'])] == ['周报']
