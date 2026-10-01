from PyQt6 import QtCore, QtGui, QtTest

from prism.items import PrismTextItem


def test_insert_text_visible_under_filters_and_accepts_typing(view, qtbot):
    panel = view.category_panel
    view.scene.category_names = ['notes']
    panel.update_counts()
    panel._active_filter = ('category', 'notes')
    panel._active_tags = {'night'}
    panel._min_rating = 5
    view.on_action_insert_text()
    item = view.scene.edit_item
    assert isinstance(item, PrismTextItem)
    assert item.isVisible()
    assert item.categories == ['notes']
    assert view.viewport().rect().contains(view.mapFromScene(item.sceneBoundingRect().center()))
    assert view.scene.focusItem() is item
    QtTest.QTest.keyClicks(view.viewport(), 'Hello')
    assert item.toPlainText() == 'Hello'
    QtTest.QTest.keyClick(view.viewport(), QtCore.Qt.Key.Key_Return)
    assert not item.edit_mode
    assert view.scene.edit_item is None


def test_clearing_edited_text_does_not_leave_deleted_qt_reference(view):
    view.on_action_insert_text()
    view.scene.clear()
    assert view.scene.edit_item is None
    view.on_action_insert_text()
    assert isinstance(view.scene.edit_item, PrismTextItem)


def test_undo_insertion_exits_editor_safely(view):
    view.on_action_insert_text()
    item = view.scene.edit_item
    view.undo_stack.undo()
    assert item.scene() is None
    assert view.scene.edit_item is None
    view.undo_stack.redo()
    assert item.scene() is view.scene
    assert not item.edit_mode
