from unittest.mock import patch

from PyQt6 import QtCore, QtTest, QtWidgets

from tests.test_asset_browser import photo


def test_ctrl_a_and_arrange_use_material_canvas_only(main_window, qapp):
    """浏览器的 Ctrl+A / 排列只作用在浏览器的网格上，主画布的图元不许动。

    浏览器的列表现在是 **AssetGrid**（位置由行列算出来，没有可以拖着改的
    卡片），所以这里断言的是「网格被全选」「主画布图元的位置一个没动」，
    而不是旧版卡片的坐标。
    """
    items = [photo(main_window, 'default-canvas', str(i)) for i in range(4)]
    for i, item in enumerate(items):
        item.setPos(i*400, 500)
    original = [QtCore.QPointF(item.pos()) for item in items]
    main_window.show()
    main_window.asset_browser.open('all')
    # 浏览器的这些动作是靠「当前页签就是浏览器」才转给网格的
    # （view._material_canvas()），所以先把浏览器切到当前页。
    main_window.tabs.setCurrentWidget(main_window.asset_browser)
    canvas = main_window.asset_browser.list
    canvas.setFocus()
    qapp.processEvents()
    QtTest.QTest.keyClick(canvas, QtCore.Qt.Key.Key_A, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert len(canvas.selectedIndexes()) == 4, 'Ctrl+A 要全选浏览器的网格'
    main_window.view.on_action_arrange_vertical()
    assert [QtCore.QPointF(item.pos()) for item in items] == original, \
        '排列只发生在网格上，主画布的图元不许动'
    main_window.view.on_action_undo()
    assert [QtCore.QPointF(item.pos()) for item in items] == original
    main_window.view.on_action_deselect_all()
    assert not canvas.selectedIndexes()


def test_context_menu_keeps_the_selection_and_offers_the_grid_entries(main_window):
    """浏览器的右键菜单不能挡住界面，也不能把选择弄丢。

    网格模式下浏览器用的是**自己的**菜单（打开源画布 / 导出 / 回收站），
    不是画布那份 —— 所以这里断言菜单确实弹出来了、选择还在、并且菜单项是
    网格那套。菜单是模态的：没人应答就会把整个测试进程卡死，所以这里把
    ``QMenu.exec`` 换掉（这是这组测试以前挂死的真正原因）。
    """
    for i in range(3):
        photo(main_window, 'default-canvas', str(i))
    browser = main_window.asset_browser
    browser.open('all')
    main_window.tabs.setCurrentWidget(browser)
    main_window.view.on_action_select_all()
    shown = []

    def fake_exec(menu, *args):
        shown.append(menu)

    with patch.object(QtWidgets.QMenu, 'exec', fake_exec):
        browser.context_menu(QtCore.QPoint(1, 1))
    assert len(shown) == 1, '右键要弹出且只弹一个菜单'
    assert len(browser.list.selectedIndexes()) == 3, '右键不能把选择弄丢'
    labels = [action.text() for action in shown[0].actions()]
    assert any('Move to trash' in label for label in labels), \
        f'网格菜单该有回收站入口，实际是 {labels}'


def test_arrangement_only_moves_selected_cards(main_window):
    for i in range(3):
        photo(main_window, 'default-canvas', str(i))
    main_window.asset_browser.open('all')
    canvas = main_window.asset_browser.list
    canvas.select_all()
    main_window.view.on_action_arrange_horizontal()
    assert len(canvas.selectedIndexes()) == 3, '排列不能把选择弄丢'
