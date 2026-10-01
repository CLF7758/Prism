"""把画布上框选的素材拖到左栏另一个画布行 = 移动它们到那个画布。

用户要的链路：A 画布上框选几张图 → 拖到左栏资源树里的 B 画布行 →
它们归 B。这个文件钉三件事：

  · 归属真的改了（`_canvas_id`），而且一步就能撤销
  · 画布之间是隔开的：移过去之后在 A 上消失、切到 B 才出现
  · 落点在非画布行（分区标题、文件夹、标签）上时什么都不做
"""
import pytest
from PyQt6 import QtCore, QtGui

from prism import commands
from prism.items import PrismPixmapItem


def image_item(scene, canvas_id):
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image, filename='example.png')
    item._canvas_id = canvas_id
    scene.addItem(item)
    return item


@pytest.fixture
def two_canvases(qapp, qtbot, main_window):
    """两个画布页、树里有对应的行、当前在 A 画布上。"""
    window = main_window
    scene = window.view.scene
    scene.workspace_pages = [
        {'id': 'canvas-a', 'kind': 'canvas', 'title': '画布甲', 'tags': []},
        {'id': 'canvas-b', 'kind': 'canvas', 'title': '画布乙', 'tags': []},
    ]
    panel = window.view.category_panel
    panel.rebuild_workspace_tree()
    window.resize(1400, 900)
    window.show()
    qapp.processEvents()
    qtbot.wait(30)
    window.open_workspace_page('canvas', 'canvas-a')
    qapp.processEvents()
    return window, panel


def row_index(panel, page_id):
    service = panel.resource_service
    node = next(n for n in service.listChildren('canvas')
                if n.get('pageId') == page_id)
    index = panel.resource_tree.model().index_for_id(node['id'])
    assert index.isValid(), f'{page_id} 在树里应该有一行'
    return index


def test_canvas_at_only_matches_canvas_page_rows(two_canvases):
    """落点判定：只有画布分区的页面行算数，别的一律不接受。"""
    window, panel = two_canvases
    tree = panel.resource_tree
    index = row_index(panel, 'canvas-b')
    rect = tree.visualRect(index)
    assert rect.height() > 0, '树要有布局尺寸，这个测试才有意义'
    spot = tree.viewport().mapToGlobal(rect.center())
    assert panel.canvas_at(spot) == 'canvas-b'

    # 分区标题行（画布/脑图/文档/标签）不是放置目标。
    model = tree.model()
    section = model.index(0, 0)          # 第一个分区 = 画布
    section_rect = tree.visualRect(section)
    if section_rect.height() > 0:
        section_spot = tree.viewport().mapToGlobal(section_rect.center())
        assert panel.canvas_at(section_spot) is None

    # 树外面（比如画布自己身上）什么都不算。
    assert panel.canvas_at(window.view.viewport().mapToGlobal(
        window.view.viewport().rect().center())) is None


def test_move_items_to_canvas_changes_ownership(two_canvases):
    window, panel = two_canvases
    scene = window.view.scene
    items = [image_item(scene, 'canvas-a') for _ in range(3)]
    panel.update_counts()

    assert panel.move_items_to_canvas(items, 'canvas-b') is True

    assert [item.canvas_id for item in items] == ['canvas-b'] * 3
    # 画布是隔开的：它们已经不在当前（A）画布上。
    assert not any(item.isVisible() for item in items)

    # 切到 B 画布就能看到它们了。
    window.open_workspace_page('canvas', 'canvas-b')
    assert all(item.isVisible() for item in items)


def test_move_items_to_canvas_is_undoable(two_canvases):
    window, panel = two_canvases
    scene = window.view.scene
    items = [image_item(scene, 'canvas-a')]
    panel.update_counts()
    panel.move_items_to_canvas(items, 'canvas-b')

    window.view.undo_stack.undo()

    assert items[0].canvas_id == 'canvas-a'
    assert items[0].isVisible(), '撤销之后该回到原来的画布上'


def test_switching_canvas_shows_only_its_own_items(two_canvases):
    window, panel = two_canvases
    scene = window.view.scene
    on_a = [image_item(scene, 'canvas-a') for _ in range(2)]
    on_b = [image_item(scene, 'canvas-b')]
    panel.update_counts()

    window.open_workspace_page('canvas', 'canvas-a')
    assert all(item.isVisible() for item in on_a)
    assert not any(item.isVisible() for item in on_b)

    window.open_workspace_page('canvas', 'canvas-b')
    assert all(item.isVisible() for item in on_b)
    assert not any(item.isVisible() for item in on_a)


def test_move_to_the_same_canvas_does_nothing(two_canvases):
    window, panel = two_canvases
    items = [image_item(window.view.scene, 'canvas-a')]
    before = window.view.undo_stack.count()
    assert panel.move_items_to_canvas(items, 'canvas-a') is False
    assert window.view.undo_stack.count() == before


def test_items_without_a_known_canvas_keep_showing(two_canvases):
    """归属不明的素材不参与画布隔离 —— 不能让旧工程的素材凭空消失。"""
    window, panel = two_canvases
    scene = window.view.scene
    orphan = image_item(scene, '某个已经不在工程里的页面')
    panel.update_counts()
    assert orphan.isVisible(), '页面已经不在了，那就照旧显示'


class _Release:
    """`_canvas_drop_target` 需要的最小事件替身：视口坐标 + 全局坐标。"""

    def __init__(self, widget_pos, global_pos):
        self._widget = QtCore.QPointF(widget_pos)
        self._global = QtCore.QPointF(global_pos)

    def position(self):
        return self._widget

    def globalPosition(self):
        return self._global


def drop_geometry(panel, view, page_id):
    """返回 (视口坐标, 全局坐标)：目标画布行的中心。"""
    tree = panel.resource_tree
    index = row_index(panel, page_id)
    tree.scrollTo(index)
    rect = tree.visualRect(index)
    assert rect.height() > 0, '树要有布局尺寸，这个测试才有意义'
    global_pos = tree.viewport().mapToGlobal(rect.center())
    return view.viewport().mapFromGlobal(global_pos), global_pos


def test_the_drop_target_is_the_canvas_row_under_the_pointer(two_canvases):
    window, panel = two_canvases
    view = window.view
    item = image_item(view.scene, 'canvas-a')
    item.setSelected(True)
    drop, global_pos = drop_geometry(panel, view, 'canvas-b')
    view._drag_origin = drop - QtCore.QPoint(120, 0)   # 拖过一段距离
    assert view._canvas_drop_target(_Release(drop, global_pos)) == 'canvas-b'


def test_a_short_tap_is_never_a_drop(two_canvases):
    """没真的拖动过（比系统的 startDragDistance 还短）就不算拖到画布上。"""
    window, panel = two_canvases
    view = window.view
    item = image_item(view.scene, 'canvas-a')
    item.setSelected(True)
    drop, global_pos = drop_geometry(panel, view, 'canvas-b')
    view._drag_origin = drop                          # 原地按下原地松开
    assert view._canvas_drop_target(_Release(drop, global_pos)) is None


def test_the_current_canvas_is_not_a_drop_target(two_canvases):
    window, panel = two_canvases
    view = window.view
    item = image_item(view.scene, 'canvas-a')
    item.setSelected(True)
    drop, global_pos = drop_geometry(panel, view, 'canvas-a')
    view._drag_origin = drop - QtCore.QPoint(120, 0)
    assert view._canvas_drop_target(_Release(drop, global_pos)) is None


def test_filing_swallows_the_drag_move_so_undo_is_one_step(two_canvases):
    """拖动留下的那次位移要被收回去：撤销栈里只剩「改画布」一条。"""
    window, panel = two_canvases
    view = window.view
    item = image_item(view.scene, 'canvas-a')
    item.setPos(0, 0)
    item.setSelected(True)
    # 真实的释放时序：拖动过程中图元已经被挪走，scene 把位移压进撤销栈。
    item.moveBy(150, 40)
    view._press_stack_count = view.undo_stack.count()
    view.undo_stack.push(commands.MoveItemsBy(
        [item], QtCore.QPointF(150, 40), ignore_first_redo=True))

    view._file_items_into_canvas([item], 'canvas-b')

    assert item.canvas_id == 'canvas-b'
    assert item.pos() == QtCore.QPointF(0, 0), '拖动留下的位移要收回去'
    assert view.undo_stack.count() == 1, '一次拖动只留一条命令'
    assert isinstance(view.undo_stack.command(0), commands.ChangeMetadata)

    view.undo_stack.undo()
    assert item.canvas_id == 'canvas-a'
    assert item.isVisible()
