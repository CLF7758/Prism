"""Native/web parity checks for shared state and pinned controls."""
from PyQt6 import QtCore, QtTest
from prism.items import ARROW_END, ARROW_NONE


def test_toolbar_mode_and_settings_follow_external_changes(main_window):
    window = main_window
    bar, view = window.canvas_toolbar, window.view
    bar.tools['pen'].click()
    assert view.active_mode == view.DRAW_MODE
    assert bar.tools['pen'].isChecked()
    assert view._drawing_toolbar.isHidden()
    bar.tools['arrow'].click()
    assert view._drawing_tool == 'line' and view._arrow == ARROW_END
    assert bar.tools['arrow'].isChecked()
    bar.tools['line'].click()
    assert view._arrow == ARROW_NONE
    view.set_pen_width(7.5)
    assert bar.width_combo.currentData() == 7.5
    view.cancel_draw_mode()
    assert bar.tools['select'].isChecked()


def test_colour_filter_has_one_shared_state_and_clear_keeps_page(main_window):
    window = main_window
    assert window.filter_popover.color_host is window.color_filter
    window.canvas_toolbar.colours['blue'].click()
    assert window.color_filter._active_group == 'blue'
    assert not window.filter_popover.colour_buttons
    assert any(kind == 'colour' for kind, label in window.filter_popover.active_summary())
    before = window.current_page_id
    window._clear_filters()
    assert window.current_page_id == before
    assert window.canvas_toolbar.colours['all'].isChecked()


def test_filter_and_sidebar_remain_accessible_when_narrow(main_window, qapp):
    window = main_window
    window.resize(900, 600)
    qapp.processEvents()
    window._apply_responsive_panels()
    bar = window.canvas_toolbar
    assert bar.rect().contains(bar.filter_button.geometry())
    assert window._right_column.isVisible()
    assert window._window_controls.parentWidget() is window._right_header
    assert not hasattr(window, '_edge_handles')
    bar.filter_button.click()
    assert window.filter_popover.isVisible()
    QtTest.QTest.keyClick(window, QtCore.Qt.Key.Key_Escape)
    assert window.filter_popover.isHidden()
    assert not bar.filter_button.isChecked()


def test_text_tool_places_text_on_clicked_canvas_point(main_window, qapp):
    window = main_window
    view = window.view
    before = len(list(view.scene.items_for_save()))
    window.canvas_toolbar.tools['text'].click()
    assert window.canvas_toolbar.tools['text'].isChecked()
    assert len(list(view.scene.items_for_save())) == before
    QtTest.QTest.mouseClick(view.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=QtCore.QPoint(80, 90))
    qapp.processEvents()
    assert len(list(view.scene.items_for_save())) == before + 1
    assert not view._text_tool_armed


def test_non_canvas_has_own_chrome_and_no_material_inspector(main_window, qapp):
    window = main_window
    window.resize(1440, 900)
    window.tabs.setCurrentWidget(window.document_panel)
    qapp.processEvents()
    window._apply_responsive_panels()
    assert window.view._detail_panel.isHidden()
    assert window.filter_bar.isHidden()
    assert not window.canvas_toolbar.isVisible()
    window.tabs.setCurrentWidget(window.canvas_page)
    qapp.processEvents()
    assert window.canvas_toolbar.isVisible()
    assert window.view._detail_panel.isVisible()


def test_canvas_toolbar_is_top_row_and_popups_show_full_labels(main_window, qapp):
    from PyQt6 import QtWidgets
    w = main_window
    w.resize(1440, 900)
    w.show()
    qapp.processEvents()
    bar = w.canvas_toolbar
    assert w.filter_bar.isHidden()
    assert w._eagle_header_stack.currentWidget() is bar
    assert bar.mapTo(w._centre_column, QtCore.QPoint()).y() == 0
    for combo in (bar.width_combo, bar.style_combo):
        combo.showPopup()
        qapp.processEvents()
        assert combo.view().textElideMode() == QtCore.Qt.TextElideMode.ElideNone
        text_width = max(combo.fontMetrics().horizontalAdvance(combo.itemText(i)) for i in range(combo.count()))
        assert combo.view().viewport().width() >= text_width + 28
        assert combo.style().subControlRect(QtWidgets.QStyle.ComplexControl.CC_ComboBox, _combo_option(combo), QtWidgets.QStyle.SubControl.SC_ComboBoxEditField, combo).width() >= combo.fontMetrics().horizontalAdvance(combo.currentText())
        combo.hidePopup()


def _combo_option(combo):
    from PyQt6 import QtWidgets
    option = QtWidgets.QStyleOptionComboBox()
    combo.initStyleOption(option)
    return option


def test_left_sidebar_has_no_removed_controls_and_empty_rows(main_window):
    from prism.widgets.resource_tree_model import NODE_TYPE_ROLE
    panel = main_window.view.category_panel
    assert 'random' not in panel._nav_buttons
    assert not hasattr(panel, 'smart_folders')
    assert panel.filter_button.isHidden()
    model = panel.resource_tree.model()
    for row in range(model.rowCount()):
        section = model.index(row, 0)
        for child in range(model.rowCount(section)):
            index = model.index(child, 0, section)
            if index.data(NODE_TYPE_ROLE) == 'blank':
                assert panel.resource_tree.isRowHidden(child, section)
        real_children = [model.index(child, 0, section) for child in range(model.rowCount(section)) if model.index(child, 0, section).data(NODE_TYPE_ROLE) != 'blank']
        assert model.hasChildren(section) == bool(real_children)


def test_recent_files_are_in_hamburger_menu(main_window, tmp_path):
    path = tmp_path / 'example.prism'
    path.write_bytes(b'project')
    main_window.view.settings.update_recent_files(str(path))
    menu = main_window._eagle_menu()
    recent = menu.actions()[0].menu()
    assert recent is main_window.view._recent_files_submenu
    assert [a.text() for a in recent.actions()] == ['example.prism']
    overlay = main_window.view.welcome_overlay
    overlay.show()
    assert overlay.files_widget.isHidden()
