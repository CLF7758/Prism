from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism import commands
from prism.items import PrismPathItem, PrismPixmapItem, PrismTextItem
from prism.widgets.duplicate_finder import DuplicateFinderDialog


def photo(view, x=0):
    image = QtGui.QImage(120, 80, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    view.scene.addItem(item)
    item.setPos(x, 0)
    return item


@pytest.mark.parametrize('method,kwargs', [
    ('arrange', {}), ('arrange', {'vertical': True}),
    ('arrange_optimal', {}), ('arrange_square', {})])
@pytest.mark.parametrize('explicit', [False, True])
def test_arrangement_leaves_annotations_in_place(view, method, kwargs, explicit):
    images = [photo(view), photo(view, 400)]
    stroke = PrismPathItem([(0, 0), (200, 100)], width=12)
    text = PrismTextItem('Keep this note here')
    for item in (stroke, text):
        view.scene.addItem(item)
        item.setPos(2300, 1800)
    originals = [QtCore.QPointF(i.pos()) for i in (stroke, text)]
    items = images + [stroke, text]
    for item in items:
        item.setSelected(True)
    if explicit:
        kwargs['items'] = items
    getattr(view.scene, method)(**kwargs)
    assert [i.pos() for i in (stroke, text)] == originals
    assert images[0].pos() != QtCore.QPointF(0, 0)
    view.undo_stack.undo()
    assert images[0].pos() == QtCore.QPointF(0, 0)
    assert images[1].pos() == QtCore.QPointF(400, 0)


def test_video_frames_are_packed_clear_of_existing_content_and_undo_together(view):
    existing = photo(view)
    previous = QtCore.QPointF(existing.pos())
    image = QtGui.QImage(120, 80, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    before = view.undo_stack.index()
    view.add_frames_to_canvas([(image, i*1000) for i in range(6)])
    inserted = [i for i in view.scene.items_for_save() if i is not existing]
    assert len(inserted) == 6
    assert existing.pos() == previous
    assert view.undo_stack.index() == before+1
    for index, item in enumerate(inserted):
        rect = item.bounding_rect_unselected().translated(item.pos())
        assert not rect.intersects(existing.bounding_rect_unselected().translated(existing.pos()))
        for other in inserted[index+1:]:
            assert not rect.intersects(other.bounding_rect_unselected().translated(other.pos()))
    view.undo_stack.undo()
    assert list(view.scene.items_for_save()) == [existing]
    view.undo_stack.redo()
    assert len(list(view.scene.items_for_save())) == 7


def test_large_video_frame_batch_uses_optimal_instead_of_square(view, monkeypatch):
    items = [photo(view) for _ in range(201)]
    optimal, square = Mock(), Mock()
    monkeypatch.setattr(view.scene, 'arrange_optimal', optimal)
    monkeypatch.setattr(view.scene, 'arrange_square', square)
    view._arrange_imported_items(items, optimal=True)
    optimal.assert_called_once_with(items=items)
    square.assert_not_called()


@pytest.mark.parametrize('physical', [False, True])
def test_canvas_delete_updates_duplicate_results_and_undo_restores_them(view, physical):
    images = [photo(view, i*150) for i in range(3)]
    dialog = DuplicateFinderDialog(view, [images])
    command = (commands.DeleteItems(view.scene, [images[0]]) if physical
               else commands.TrashItems(view.scene, [images[0]]))
    view.undo_stack.push(command)
    assert dialog.groups == [images[1:]]
    assert dialog.image_list.count() == 2
    view.undo_stack.push(commands.TrashItems(view.scene, [images[1]]))
    assert not dialog.groups
    assert dialog.image_list.count() == 0
    view.undo_stack.undo()
    view.undo_stack.undo()
    assert dialog.groups == [images]
    assert dialog.image_list.count() == 3
    dialog.close()


def test_dialog_theme_installs_once_and_applies_to_secondary_windows(qapp, monkeypatch):
    from prism.widgets import dialog_theme
    apply = Mock()
    monkeypatch.setattr(dialog_theme, 'apply_native_theme', apply)
    first = dialog_theme.install(qapp)
    assert dialog_theme.install(qapp) is first
    dialog = QtWidgets.QDialog()
    dialog.show()
    assert any(call.args == (dialog,) for call in apply.call_args_list)
    dialog.close()


def test_virtual_asset_deletion_syncs_snapshot_results_after_editor_recycling(view, tmp_path):
    from tests.test_virtual_canvas import record
    from prism.duplicate_scan import ImageSource
    items = [record(view, tmp_path, color, x=index*150)
             for index, color in enumerate(('red', 'blue', 'green'))]
    sources = [ImageSource.from_item(item) for item in items]
    dialog = DuplicateFinderDialog(view, [sources])
    manager = view.scene._viewport_items
    manager.materialize(items[0])
    manager.recycle(items[0])
    view.undo_stack.push(commands.TrashItems(view.scene, [items[0]]))
    assert dialog.groups == [sources[1:]]
    view.undo_stack.undo()
    assert dialog.groups == [sources]
    dialog.close()


def test_native_dialog_theme_requests_dark_chrome(qapp, monkeypatch):
    from prism.widgets import dialog_theme
    import ctypes
    dialog = QtWidgets.QDialog()
    palette = dialog.palette()
    palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor('#242628'))
    palette.setColor(QtGui.QPalette.ColorRole.WindowText, QtGui.QColor('#eeeeee'))
    dialog.setPalette(palette)
    calls = []
    def setter(hwnd, attribute, pointer, size):
        calls.append((attribute, ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint)).contents.value))
        return 0
    native = Mock(side_effect=setter)
    monkeypatch.setattr(dialog_theme.sys, 'platform', 'win32')
    monkeypatch.setattr(QtGui.QGuiApplication, 'platformName', lambda: 'windows')
    monkeypatch.setattr(ctypes.windll.dwmapi, 'DwmSetWindowAttribute', native)
    dialog_theme.apply_native_theme(dialog)
    assert (20, 1) in calls
    assert (35, 0x282624) in calls
    assert (36, 0xeeeeee) in calls
