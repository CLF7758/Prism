"""Every import entry point must place batches without overlapping images."""
from types import SimpleNamespace

from PyQt6 import QtCore, QtGui

from prism.items import PrismPixmapItem
from tests.test_resource_tree_import import run_import


def images(tmp_path, count=100):
    paths = []
    for index in range(count):
        image = QtGui.QImage(40 + index % 7 * 9, 30 + index % 5 * 11,
                             QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('red'))
        path = tmp_path / f'{index:03}.png'
        assert image.save(str(path))
        paths.append(path)
    return paths


def assert_separate(scene, items):
    rects = [scene.itemsBoundingRect(items=[item]) for item in items]
    for index, rect in enumerate(rects):
        assert not rect.isEmpty()
        for other in rects[index + 1:]:
            assert not rect.intersects(other), (rect, other)


def test_folder_import_packs_one_hundred_images(main_window, tmp_path, qtbot):
    images(tmp_path)
    run_import(main_window, 'canvas', None, str(tmp_path))
    view = main_window.view
    qtbot.waitUntil(lambda: not view.worker.isRunning(), timeout=15000)
    qtbot.waitUntil(lambda: len(list(view.scene.items_for_save())) == 100, timeout=5000)
    qtbot.wait(100)
    assert_separate(view.scene, list(view.scene.items_for_save()))


def test_external_import_preserves_existing_and_avoids_overlap(view, tmp_path, qtbot, settings):
    settings.setValue('Items/arrange_default', 'horizontal')
    image = QtGui.QImage(400, 300, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    existing = PrismPixmapItem(image)
    existing._canvas_id = view.current_canvas_id
    view.scene.addItem(existing)
    existing.set_pos_center(view.mapToScene(view.get_view_center()))
    position = QtCore.QPointF(existing.pos())
    view.do_insert_images([str(path) for path in images(tmp_path)])
    qtbot.waitUntil(lambda: not view._import_in_progress, timeout=15000)
    items = list(view.scene.items_for_save())
    assert len(items) == 101
    assert existing.pos() == position
    assert_separate(view.scene, items)
    new = [item for item in items if item is not existing]
    bounds = view.scene.itemsBoundingRect(items=new)
    assert bounds.width() < 2000  # A compact block, rather than a 100-image row.
    arranged = {item: QtCore.QPointF(item.pos()) for item in new}
    view.undo_stack.undo()
    assert list(view.scene.items_for_save()) == [existing]
    view.undo_stack.redo()
    assert all(item.pos() == arranged[item] for item in new)


def test_clipboard_batch_is_arranged_and_undoable(view, tmp_path):
    entries = [SimpleNamespace(data=path.read_bytes(), suffix='png', tags=[])
               for path in images(tmp_path, 12)]
    view._on_inbox_import(entries)
    items = list(view.scene.items_for_save())
    assert len(items) == 12
    assert_separate(view.scene, items)
    view.undo_stack.undo()
    assert not list(view.scene.items_for_save())
    view.undo_stack.redo()
    assert_separate(view.scene, list(view.scene.items_for_save()))
