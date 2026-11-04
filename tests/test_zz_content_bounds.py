"""The scene rectangle must cover the canvas the user is looking at.

事故背景（2026-10-22 实测：**15,308 ms / 8 次**，约 1.9 秒/次切画布）：

``_content_bounds()`` 调的是 ``scene.itemsBoundingRect()`` —— 不带参数时
它遍历场景里的**每一项**，10 万项的工程里每次切画布都要把 10 万条重算
一遍（每条还要做四次角点变换）。

素材是按画布隔离的：``media_library._apply_filter`` 按
``canvas == current_canvas`` 设置可见性，画布上其实只显示几十张。所以
"这个画布上看得见的东西"就是包围盒该覆盖的东西 —— 这也正是
``on_action_fit_scene`` 早就在用的口径（``view.py``：``items_for_save()``
里 ``isVisible()`` 的那一批）。
"""
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui


def _image_item():
    from prism.items import PrismPixmapItem
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    return PrismPixmapItem(image)


def test_the_bounds_cover_the_visible_items_only(view, qapp):
    """隐藏的项不进包围盒 —— 画布上显示什么，场景矩形就覆盖什么。"""
    first = _image_item()
    first.setPos(0, 0)
    view.scene.addItem(first)
    second = _image_item()
    second.setPos(5000, 5000)
    view.scene.addItem(second)
    second.setVisible(False)
    qapp.processEvents()

    bounds = QtCore.QRectF(view._content_bounds())

    assert bounds.width() < 1000, (
        '隐藏的项也进了包围盒：%s' % (bounds,))
    assert bounds.contains(QtCore.QPointF(0, 0))


def test_the_bounds_are_computed_from_a_restricted_item_set(view, qapp, monkeypatch):
    """结构保证：算包围盒时喂进去的是**可见项**，不是整个场景。

    这一条是防退化的：只要还是"遍历整个场景"，10 万项的工程里每次切
    画布就会重算一遍（实测 1.9 秒/次）。
    """
    for index in range(5):
        item = _image_item()
        item.setPos(index * 100, 0)
        view.scene.addItem(item)
        if index >= 2:
            item.setVisible(False)      # 只有前两个在这个画布上
    qapp.processEvents()

    seen = []
    original = view.scene.itemsBoundingRect

    def spy(selection_only=False, items=None):
        seen.append(items)
        return original(selection_only=selection_only, items=items)

    monkeypatch.setattr(view.scene, 'itemsBoundingRect', spy)
    view._cached_scene_bounds = None
    view._content_bounds()

    assert seen, 'itemsBoundingRect 根本没有被调用'
    assert seen[0] is not None, (
        '仍在算整个场景的包围盒（没有传 items）')
    assert len(seen[0]) == 2, (
        '喂进去的不是可见项，是 %d 项' % len(seen[0]))
    assert len(seen[0]) < 5, (
        '喂进去的还是整个场景：%d 项' % len(seen[0]))


def test_the_bounds_follow_the_current_canvas(view, qapp):
    """切画布时包围盒跟着换 —— 缓存键里带着画布 id，值也得跟着换。"""
    view.current_canvas_id = 'A'
    on_a = _image_item()
    on_a.setPos(0, 0)
    view.scene.addItem(on_a)
    on_b = _image_item()
    on_b.setPos(9000, 9000)
    view.scene.addItem(on_b)
    on_a.setVisible(True)
    on_b.setVisible(False)
    qapp.processEvents()

    first = QtCore.QRectF(view._content_bounds())

    view.current_canvas_id = 'B'
    on_b.setVisible(True)
    on_a.setVisible(False)
    qapp.processEvents()

    second = QtCore.QRectF(view._content_bounds())

    assert first != second, '切了画布，包围盒还是旧的'
    assert second.left() > 5000, (
        '包围盒没有跟着画布走：%s' % (second,))


def test_a_canvas_without_visible_items_has_an_empty_bound(view, qapp):
    """空画布没有可见内容 ⇒ 包围盒为空（和 `itemsBoundingRect(items=[])` 一致）。"""
    item = _image_item()
    view.scene.addItem(item)
    item.setVisible(False)
    qapp.processEvents()

    assert QtCore.QRectF(view._content_bounds()) == QtCore.QRectF(0, 0, 0, 0)


def test_the_bounds_are_still_cached(view, qapp, monkeypatch):
    """改完之后缓存不能丢：重绘不该重算包围盒。"""
    item = _image_item()
    view.scene.addItem(item)
    qapp.processEvents()

    bounds = QtCore.QRectF(view._content_bounds())
    scan = Mock(wraps=view.scene.itemsBoundingRect)
    monkeypatch.setattr(view.scene, 'itemsBoundingRect', scan)
    for _ in range(20):
        item.update()
        qapp.processEvents()
        assert view._content_bounds() == bounds
    assert scan.call_count == 0, '缓存失效了'
