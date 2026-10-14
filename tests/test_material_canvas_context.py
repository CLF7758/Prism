from unittest.mock import patch
from PyQt6 import QtCore, QtTest
from tests.test_asset_browser import photo


def test_ctrl_a_and_arrange_use_material_canvas_only(main_window, qapp):
    items = [photo(main_window, 'default-canvas', str(i)) for i in range(4)]
    for i, item in enumerate(items):
        item.setPos(i*400, 500)
    original = [QtCore.QPointF(item.pos()) for item in items]
    main_window.show()
    main_window.asset_browser.open('all')
    canvas = main_window.asset_browser.list
    canvas.setFocus()
    qapp.processEvents()
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert len(canvas.selectedIndexes()) == 4
    assert all(c.isSelected() for c in canvas.cards)
    before = [QtCore.QPointF(c.pos()) for c in canvas.cards]
    main_window.view.on_action_arrange_vertical()
    assert len({c.x() for c in canvas.cards}) == 1
    assert len({c.y() for c in canvas.cards}) == 4
    assert [item.pos() for item in items] == original
    main_window.view.on_action_undo()
    assert [c.pos() for c in canvas.cards] == before
    main_window.view.on_action_deselect_all()
    assert not canvas.selectedIndexes()


def test_context_menu_is_original_canvas_menu_and_keeps_selection(main_window):
    for i in range(3):
        photo(main_window, 'default-canvas', str(i))
    browser = main_window.asset_browser
    browser.open('all')
    main_window.view.on_action_select_all()
    with patch.object(main_window.view.context_menu, 'exec') as opened:
        browser.context_menu(QtCore.QPoint(1, 1))
    opened.assert_called_once()
    assert len(browser.list.selectedIndexes()) == 3
    # Original canvas menu retains its Arrange submenu and four actions.
    from prism.actions.actions import actions
    for name in ('arrange_optimal', 'arrange_horizontal', 'arrange_vertical', 'arrange_square'):
        assert actions[name].qaction.isEnabled()


def test_arrangement_only_moves_selected_cards(main_window):
    for i in range(3):
        photo(main_window, 'default-canvas', str(i))
    main_window.asset_browser.open('all')
    canvas = main_window.asset_browser.list
    canvas.cards[0].setSelected(True)
    canvas.cards[1].setSelected(True)
    before = QtCore.QPointF(canvas.cards[2].pos())
    main_window.view.on_action_arrange_horizontal()
    assert canvas.cards[0].y() == canvas.cards[1].y()
    assert canvas.cards[2].pos() == before
