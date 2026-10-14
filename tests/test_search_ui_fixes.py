from PyQt6 import QtCore, QtWidgets
from unittest.mock import patch
from tests.test_asset_browser import photo
from tests.test_unified_project_search import page
from tests.test_search_controller import submit
from prism.widgets.resource_tree_model import ASSET_COUNT_ROLE
from prism.project_search import item_key


def test_requested_search_controls_are_removed_and_clear_returns(main_window):
    photo(main_window, 'default-canvas', '雨夜')
    controller = submit(main_window, '雨夜')
    assert controller.exit_button.isHidden()
    assert controller.scope_combo.isHidden()
    assert controller.filter_button.isHidden()
    controller.input.clear()
    controller.submit()
    assert not controller.active
    assert main_window.tabs.currentWidget() is not controller.page


def test_unclassified_and_untagged_override_previous_mindmap_type(main_window):
    page(main_window, 'mindmap', '脑图', {'topic': '内容'})
    item = photo(main_window, 'default-canvas', '素材')
    library = main_window.view.category_panel
    controller = main_window.search_controller
    for target in ('untagged', 'unclassified'):
        library.set_search_kind('mindmap')
        controller.scope = ('page', ('mindmap', 'old-page'))
        library._navigate(target)
        assert library.search_kind == 'asset'
        assert main_window.asset_browser.model.items == [item]
        assert controller.scope == ('project', None)


def test_result_cards_fill_width_and_reflow(main_window, qtbot):
    for index in range(30):
        photo(main_window, 'default-canvas', '雨夜'+str(index))
    controller = submit(main_window, '雨夜')
    controller.page.resize(1800, 600)
    controller.page.view.resize(1800, 450)
    controller.page.arrange()
    cards = list(controller.page.cards.values())
    columns = controller.page.layout_dimensions()[0]
    assert columns >= 6
    assert cards[columns-1].pos().x() > 1400
    assert cards[columns].pos().y() > cards[0].pos().y()
    assert cards[0].pos().x() == 12


def test_implicit_default_canvas_count_is_visible_without_project_mutation(main_window):
    library = main_window.view.category_panel
    model = library.resource_tree.model()
    # The stored project can omit its legacy default page/node entirely.
    before = list(library.resource_service.nodes)
    model.set_asset_counts({'default-canvas': 783})
    index = model.index_for_id('implicit:default-canvas')
    if not index.isValid():
        node = next(n for n in model.service.nodes if n.get('pageId') == 'default-canvas')
        index = model.index_for_id(node['id'])
    assert index.isValid()
    assert index.data(ASSET_COUNT_ROLE) == 783
    assert library.resource_service.nodes == before
    menu = library.resource_tree.menu_for_index(index)
    assert not any(action.text() == '删除' for action in menu.actions())
    with patch.object(main_window, 'open_workspace_page') as opened:
        library.resource_tree._open_current(index)
    opened.assert_called_once_with('canvas', 'default-canvas')


def test_search_card_displays_right_inspector(main_window):
    item = photo(main_window, 'default-canvas', '雨夜')
    item._notes = '素材备注'
    controller = submit(main_window, '雨夜')
    card = controller.page.cards['asset:'+item_key(item)]
    card.setSelected(True)
    main_window._apply_responsive_panels()
    inspector = main_window.view._detail_panel
    assert not inspector.isHidden()
    assert inspector._collection.isHidden()
    assert not inspector._scroll.isHidden()
    assert inspector._item is item
    assert inspector._title_edit.text() == '雨夜'
    assert inspector._notes_edit.toPlainText() == '素材备注'


def test_mindmap_has_no_outer_empty_right_column(main_window):
    record = page(main_window, 'mindmap', '脑图', {'topic': '根'})
    main_window.tabs.setCurrentWidget(main_window._page_panels[record['id']])
    main_window._apply_responsive_panels()
    assert main_window._right_column.isHidden()
    assert main_window._window_controls.parentWidget() is main_window._collapsed_window_host
    main_window.tabs.setCurrentWidget(main_window.canvas_page)
    main_window._apply_responsive_panels()
    assert not main_window._right_column.isHidden()


def test_status_bar_leaves_vertical_room_for_text(main_window):
    main_window.tabs.setCurrentWidget(main_window.canvas_page)
    assert main_window._centre_status.height() >= 36
    assert main_window._status_text.alignment() & QtCore.Qt.AlignmentFlag.AlignTop

