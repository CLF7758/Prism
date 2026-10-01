"""资源树测试共用的几个小工具。

单独放一个模块（**不是** `test_` 开头，pytest 不会收集它）：这几个 helper
三个测试文件都要用，互相 import 会绕成环。
"""
from PyQt6 import QtWidgets

from prism.widgets.resource_tree_view import _NameEditor


def orphan_report(scene):
    """哪一边多了东西：页面节点没有对应页面，或页面没有对应节点。

    软删除的节点也算数：它还在 `workspace_nodes` 里（正文也还在
    `workspace_pages` 里），这正是「删除不会丢内容」的实现方式。
    """
    node_pages = {n['pageId'] for n in scene.workspace_nodes
                  if n.get('pageId')}
    page_ids = {p['id'] for p in scene.workspace_pages}
    return {'只在树里': sorted(node_pages - page_ids),
            '只在页面表里': sorted(page_ids - node_pages)}


def current_editor(view):
    """最后打开的那个就地命名编辑器。

    关掉的编辑器是 `deleteLater`，测试里没有事件循环去回收它们，
    所以 `findChild` 可能拿到上一个 —— 连续两次新建时就会取错。
    """
    editors = view.findChildren(_NameEditor)
    assert editors, '就地编辑器应该已经打开'
    return editors[-1]


def confirm_the_editor(view, text=None):
    """把就地命名的名字确认掉（等价于回车或有效失焦）。"""
    editor = current_editor(view)
    if text is not None:
        editor.setText(text)
    view.commitData(editor)
    view.itemDelegate().closeEditor.emit(
        editor, QtWidgets.QAbstractItemDelegate.EndEditHint.SubmitModelCache)
    return editor


def cancel_the_editor(view):
    """按 Esc 取消就地命名（等价于 RevertModelCache）。"""
    editor = current_editor(view)
    view.itemDelegate().closeEditor.emit(
        editor, QtWidgets.QAbstractItemDelegate.EndEditHint.RevertModelCache)
    return editor


def create_page(window, section, title, parent_id=None):
    """走 main window 的创建入口建一个页面，和右键菜单用的是同一条路。

    C5 之后这条入口是**就地新建**：先把名字确认掉，节点才真的落地。
    """
    window._create_resource_item(section, 'page', parent_id)
    confirm_the_editor(window.view.category_panel.resource_tree, title)
    service = window.view.category_panel.resource_service
    return next(n for n in service.nodes
                if n['parentId'] == parent_id and n['title'] == title
                and n['nodeType'] == 'page')
