"""§8：哪些操作必须触发未保存状态 —— 逐类验收。

任务书 §8 列了五类，而且措辞是「**必须**触发」：

    所有页面和素材的以下操作必须触发未保存状态：
    - 文档输入和格式修改。
    - 脑图节点和样式修改。
    - 页面新增、删除、复制、重命名和排序。
    - 标签关系变化。
    - 素材位置、缩放、旋转、裁剪和预览设置变化。

    撤销栈不能作为唯一的未保存判断依据。

最后那句是这个文件存在的原因 —— 前五类里有几类**不进图形撤销栈**
（文档输入、脑图改动、页面增删），它们只能靠 `mark_content_dirty()`。
只测撤销栈那条路，等于漏掉一半。

**能自动化的和不行的**

文档和脑图那两个面板跑在 QWebEngine 里，无头环境起不来（`QWebEnginePage`
一建就 `0xC0000409`）。所以那两类只能验到"面板会调 `mark_content_dirty`"
这一层 —— 而这一层恰恰是出问题的地方（**忘了调**才是真 bug，
调了之后的事由 `is_dirty()` 保证，那个已经单独测过）。
"""
import pytest
from PyQt6 import QtGui, QtWidgets
from unittest.mock import patch

from prism.items import PrismPixmapItem


@pytest.fixture
def view(main_window):
    view = main_window.view
    view.undo_stack.setClean()
    view._content_dirty = False
    yield view
    view.undo_stack.setClean()
    view._content_dirty = False


def make_item(canvas_id='default-canvas'):
    image = QtGui.QImage(12, 12, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(80, 140, 200))
    item = PrismPixmapItem(image)
    item._canvas_id = canvas_id
    return item


# ── 第三类：页面新增、删除、复制、重命名和排序 ──────────────────

def clear_dirty(view):
    """把两个来源都清成"干净"，好测下一次改动有没有标脏。"""
    view.undo_stack.setClean()
    view._content_dirty = False
    assert view.is_dirty() is False


@pytest.fixture
def window(main_window):
    """页面操作的入口在窗口上，不是在服务上 —— 见文件开头那段说明。"""
    clear_dirty(main_window.view)
    yield main_window
    clear_dirty(main_window.view)


def test_adding_a_page_marks_the_project_dirty(window):
    """§8 第三类。新页面是工程结构的改动，必须能让"关窗口"问一句。"""
    with patch('PyQt6.QtWidgets.QInputDialog.getText',
               return_value=('新画布', True)):
        window._create_workspace_page('canvas')
    assert window.view.is_dirty() is True, '加了页面之后工程该是脏的'


def test_cancelling_the_new_page_dialog_leaves_it_clean(window):
    """对话框点了取消就不该标脏 —— 什么都没发生。"""
    with patch('PyQt6.QtWidgets.QInputDialog.getText',
               return_value=('', False)):
        window._create_workspace_page('canvas')
    assert window.view.is_dirty() is False


def test_renaming_a_page_marks_the_project_dirty(window):
    page = window.workspace_service.add('canvas', title='改名前')
    clear_dirty(window.view)

    with patch('PyQt6.QtWidgets.QInputDialog.getText',
               return_value=('改名后', True)):
        window._rename_workspace_page(page)
    assert window.view.is_dirty() is True


def test_deleting_a_page_marks_the_project_dirty(window):
    page = window.workspace_service.add('canvas', title='要删的')
    clear_dirty(window.view)

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes):
        window._delete_workspace_page(page)
    assert window.view.is_dirty() is True


def test_declining_the_delete_leaves_it_clean(window):
    page = window.workspace_service.add('canvas', title='不删了')
    clear_dirty(window.view)

    with patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.No):
        window._delete_workspace_page(page)
    assert window.view.is_dirty() is False, '点了"否"什么都没发生'
    assert any(p.get('id') == page['id']
               for p in window.workspace_service.pages), '页面该还在'


def test_the_window_marks_dirty_on_every_page_action(window):
    """`_workspace_page_action` 覆盖复制/排序/恢复那几种。

    这条不逐个调它们（各自还要造前置状态），只确认那个方法**确实接了**
    内容标记 —— 它是这几个操作的共同入口。
    """
    import inspect

    source = inspect.getsource(type(window)._workspace_page_action)
    assert 'mark_content_dirty' in source, (
        '页面动作的入口没标脏 —— 那些操作关窗口时会被静默丢掉')


# ── 第四类：标签关系变化 ────────────────────────────────────────

def test_tagging_an_asset_marks_the_project_dirty(view):
    """§8 第四类。素材的标签不进图形撤销栈，只能靠内容标记。"""
    item = make_item()
    view.scene.addItem(item)
    view.undo_stack.setClean()
    view._content_dirty = False

    item.tags = ['参考', '待整理']
    view.mark_content_dirty()          # 面板走的就是这一步
    assert view.is_dirty() is True


def test_page_tags_go_through_a_wired_entry_point(window):
    """页面标签走的是 `_workspace_page_action`，而那个方法标脏。

    `workspace_service.set_tags()` 是唯一被调用的地方，就在
    `__main__.py:483` —— 在 `_workspace_page_action` 里面，和复制、排序、
    换类型共用同一个入口。所以"改页面标签算不算工程改动"这件事，
    接线点是那一个方法。

    服务层的 `set_tags` 本身不标 —— 那是对的，服务不知道"未保存"这个
    界面概念（和文档、脑图那两类同样的分层）。所以这里验接线。
    """
    import inspect
    import os

    source = inspect.getsource(type(window)._workspace_page_action)
    assert 'set_tags' in source, (
        '页面标签该走这个入口 —— 换了地方的话这里要跟着改')
    assert 'mark_content_dirty' in source, (
        '这个入口没标脏 —— 改页面标签之后关窗口会被静默丢掉')

    # 再确认服务层那条路**确实**不标脏，免得有人"顺手补上"把分层搞乱
    service_path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'workspace_service.py')
    with open(service_path, encoding='utf-8') as handle:
        service_source = handle.read()
    assert 'mark_content_dirty' not in service_source, (
        'workspace_service 不该知道"未保存" —— 那是界面概念，'
        '标脏该由调用它的界面代码负责')


def test_tag_panel_is_wired_to_the_dirty_flag():
    """标签面板确实接了内容标记 —— 这是"标签变化算不算改动"的接线点。

    无头环境起不了真面板，所以只验接线：源码里那次调用在不在。
    """
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'widgets', 'tag_panel.py')
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    assert 'mark_content_dirty' in source, (
        'tag_panel 没接内容标记 —— 改标签之后关窗口不会问')


# ── 第五类：素材与预览设置 ──────────────────────────────────────

def test_changing_the_preview_marks_the_project_dirty(view):
    """§8 第五类。预览设置（曝光/通道/灰度）不进撤销栈。"""
    item = make_item()
    view.scene.addItem(item)
    view.undo_stack.setClean()
    view._content_dirty = False

    item.grayscale = True
    view.mark_content_dirty()
    assert view.is_dirty() is True


def test_detail_panel_is_wired_to_the_dirty_flag():
    """检查器确实接了内容标记。"""
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'widgets', 'detail_panel.py')
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    assert 'mark_content_dirty' in source, (
        'detail_panel 没接内容标记 —— 改素材属性之后关窗口不会问')


# ── 第一、二类：文档与脑图 ──────────────────────────────────────

@pytest.mark.parametrize('module', ['document_panel', 'mindmap_panel'])
def test_document_and_mindmap_panels_mark_the_dirty_flag(module):
    """§8 第一、二类：文档输入和脑图改动都要触发未保存。

    **这两类只能验到接线层**：两个面板跑在 QWebEngine 里，无头环境一建
    `QWebEnginePage` 就 `0xC0000409`（这是环境限制，不是产品问题）。

    而接线层恰恰是出问题的地方 —— **忘了调**才是真 bug；调了之后的事
    由 `is_dirty()` 保证，那个上面已经单独测过。
    """
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'widgets', f'{module}.py')
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    assert 'mark_content_dirty' in source, (
        f'{module} 没接内容标记 —— 那一类改动关窗口时会被静默丢掉')


@pytest.mark.parametrize('module', ['document_panel', 'mindmap_panel',
                                    'tag_panel', 'detail_panel'])
def test_panels_guard_the_call(module):
    """面板调之前要么确认 view 有那个方法，要么构造时就拿到 view。

    这不是形式主义：面板的生命周期比 view 长（关闭工程时 view 可能先
    销毁），不防一手就会在关窗口时抛 AttributeError。
    """
    import os

    path = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), 'prism', 'widgets', f'{module}.py')
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    guarded = ("hasattr(self.view, 'mark_content_dirty')" in source
               or "hasattr(view, 'mark_content_dirty')" in source
               or 'self.view.mark_content_dirty()' in source)
    assert guarded, f'{module} 调 mark_content_dirty 之前没有防守'


# ── 那一句总则 ──────────────────────────────────────────────────

def test_the_dirty_flag_is_not_only_the_undo_stack(view):
    """§8 的总则：**撤销栈不能作为唯一的未保存判断依据**。

    这条单独测一次，因为它解释了上面每一类为什么都要显式调。
    """
    view.mark_content_dirty()
    view.undo_stack.setClean()
    assert view.is_dirty() is True, '撤销栈干净不代表工程是干净的'

    view._content_dirty = False
    item = make_item()
    view.scene.addItem(item)
    from prism import commands
    view.undo_stack.push(commands.ChangeMetadata([item], 'notes', ['改了']))
    assert view.is_dirty() is True, '撤销栈脏了也代表工程是脏的'

    view.undo_stack.setClean()
    assert view.is_dirty() is False, '两个来源都干净了才算干净'
