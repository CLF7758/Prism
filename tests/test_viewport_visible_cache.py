"""Arriving pixels must not re-query/sort an unchanged dense camera."""
from unittest.mock import Mock

from PyQt6 import QtCore

from tests.test_virtual_canvas import record


def test_pixel_updates_keep_camera_membership_but_still_publish_repaints(view, tmp_path, monkeypatch):
    item = record(view, tmp_path)
    manager = view.scene._viewport_items
    rect = QtCore.QRectF(-10, -10, 500, 500)
    query = Mock(wraps=manager.index.query)
    monkeypatch.setattr(manager.index, 'query', query)
    pixels = Mock(wraps=view.scene.update)
    monkeypatch.setattr(view.scene, 'update', pixels)
    expected = manager.visible(rect)
    generation, layout = manager.generation, manager.layout_revision
    for _ in range(100):
        item.update()
        # Native graphics update() also supplies geometry=True. An unchanged
        # rectangle must still reuse the list rather than trigger a scan.
        manager.changed(item, True)
        actual = manager.visible(rect)
        assert actual[0] is expected[0]
        assert actual[1] is expected[1]
    assert query.call_count == 1
    assert manager.generation > generation
    assert manager.layout_revision == layout
    assert pixels.call_count == 200


def test_geometry_camera_add_remove_invalidate_visible_membership(view, tmp_path, monkeypatch):
    first = record(view, tmp_path)
    second = record(view, tmp_path, 'blue', x=10000)
    manager = view.scene._viewport_items
    rect = QtCore.QRectF(-10, -10, 500, 500)
    query = Mock(wraps=manager.index.query)
    monkeypatch.setattr(manager.index, 'query', query)
    assert manager.visible(rect)[0] == [first]
    second.setPos(120, 0)
    assert set(manager.visible(rect)[0]) == {first, second}
    manager.remove(first)
    assert manager.visible(rect)[0] == [second]
    third = record(view, tmp_path, 'green', x=240)
    assert set(manager.visible(rect)[0]) == {second, third}
    assert manager.visible(rect.translated(10000, 0))[0] == []
    assert query.call_count == 5


def test_stacking_changes_survive_selection_materialization_and_recycle(view, tmp_path):
    first = record(view, tmp_path, z=1)
    second = record(view, tmp_path, 'blue', z=2)
    manager = view.scene._viewport_items
    rect = QtCore.QRectF(-10, -10, 500, 500)
    cached = manager.visible(rect)[0]
    first.setSelected(True)
    assert manager.visible(rect)[0] is cached
    # The helper bypasses the file loader which restores scene z bounds.
    view.scene.max_z = second.zValue()
    first.bring_to_front()
    assert manager.visible(rect)[0] == [second, first]
    manager.materialize(second)
    second._graphics.setZValue(first.zValue()+1)
    assert manager.visible(rect)[0] == [first, second]
    manager.recycle(second)
    assert manager.visible(rect)[0] == [first, second]
    # Same-z stacking is ordered by the stable insertion sequence too.
    first.setZValue(second.zValue())
    first._sequence = second._sequence+1
    manager.changed(first)
    assert manager.visible(rect)[0] == [second, first]


def test_visibility_changes_invalidate_layout_without_materializing(view, tmp_path):
    item = record(view, tmp_path)
    manager = view.scene._viewport_items
    rect = QtCore.QRectF(-10, -10, 500, 500)
    cached = manager.visible(rect)[0]
    layout = manager.layout_revision
    item.setVisible(False)
    assert manager.layout_revision > layout
    assert manager.visible(rect)[0] is not cached
    assert not item.isVisible()
    item.setVisible(True)
    assert item.isVisible()
    assert manager.visible(rect)[0] == [item]
    assert item._graphics is None
    manager.clear()
    assert manager.visible(rect)[0] == []
    assert not manager._stack_keys
