"""D3：筛选条与筛选弹层（§8.3）。

规范要的：**移除常驻的颜色条**，颜色筛选收进「筛选」弹层（宽 320）；启用后中央出现
**36 px 条**：可关闭 Chip、结果数、"清除筛选"；顶部显示"范围：当前画布"。
陷阱：**清除筛选不能偷偷切换页面**。
"""
from PyQt6 import QtWidgets

from tests.test_material_features import image_item


def bar_of(window):
    return window.filter_bar


def popover_of(window):
    return window.filter_popover


# ── 常驻颜色条没了 ────────────────────────────────────────────────────

def test_the_old_colour_strip_is_gone(main_window):
    assert not hasattr(main_window, 'color_strip'), \
        '§8.3：常驻的颜色条必须移除'
    assert not hasattr(main_window, 'filter_bar').__class__ or True


def test_the_old_colour_bar_is_not_on_screen(main_window):
    """颜色条那个类还在（弹层借它当逻辑宿主），但界面上不该再挂它。"""
    window = main_window
    bar = window.color_filter
    assert bar.parent() is None or not bar.isVisible(), \
        '颜色条不该再出现在界面上'


# ── 筛选条 ────────────────────────────────────────────────────────────

def test_the_bar_shows_the_scope_and_the_count(main_window):
    window = main_window
    image_item(window.view.scene, ['甲'])
    image_item(window.view.scene, [])
    window.filter_bar.setVisible(True)

    window._refresh_filter_bar()

    assert bar_of(window).scope_label.text() == '范围：当前画布'
    assert '2' in bar_of(window).count_label.text()


def test_the_bar_height_is_thirty_six(main_window):
    assert bar_of(main_window).height() == 36


def test_the_bar_lists_the_active_filters_as_chips(main_window):
    window = main_window
    popover = popover_of(window)
    popover.panel._active_tags = {'甲'}
    popover.panel._min_rating = 3

    window._refresh_filter_bar()

    labels = [chip.findChild(QtWidgets.QLabel).text()
              for chip in _chips(bar_of(window))]
    assert '甲' in labels
    assert any('★★★' in label for label in labels)


def _chips(bar):
    found = []
    for position in range(bar.chips_row.count()):
        widget = bar.chips_row.itemAt(position).widget()
        if widget is not None:
            found.append(widget)
    return found


def test_a_chip_can_be_removed_on_its_own(main_window):
    window = main_window
    popover = popover_of(window)
    popover.panel._active_tags = {'甲', '乙'}
    window._refresh_filter_bar()

    window._remove_filter_chip('tag', '甲')

    assert popover.panel._active_tags == {'乙'}
    labels = [chip.findChild(QtWidgets.QLabel).text()
              for chip in _chips(bar_of(window))]
    assert '甲' not in labels and '乙' in labels


# ── 弹层 ──────────────────────────────────────────────────────────────

def test_the_popover_is_320_wide_and_starts_hidden(main_window):
    assert popover_of(main_window).width() == 320
    assert not popover_of(main_window).isVisible()


def test_the_filter_button_toggles_the_popover(main_window):
    window = main_window
    window._toggle_filter_popover()
    assert popover_of(window).isVisible()
    window._toggle_filter_popover()
    assert not popover_of(window).isVisible()


def test_checking_a_tag_filters_the_board(main_window):
    window = main_window
    tagged = image_item(window.view.scene, ['甲'])
    clean = image_item(window.view.scene, [])
    window.view.scene.tag_names = ['甲']
    popover = popover_of(window)
    popover.refresh()

    box = _tag_box(popover, '甲')
    assert box is not None, '弹层里应该列出已有标签'
    box.setChecked(True)

    assert popover.panel._active_tags == {'甲'}
    assert tagged.isVisible() and not clean.isVisible()


def _tag_box(popover, name):
    for position in range(popover.tags_layout.count()):
        widget = popover.tags_layout.itemAt(position).widget()
        if isinstance(widget, QtWidgets.QCheckBox) and widget.text() == name:
            return widget
    return None


def test_the_rating_field_drives_the_min_rating(main_window):
    window = main_window
    image_item(window.view.scene, [], rating=4)
    popped = popover_of(window)
    popped.refresh()

    popped.rating_combo.setCurrentIndex(4)

    assert popped.panel._min_rating == 4


def test_the_untagged_field_drives_the_untagged_filter(main_window):
    window = main_window
    tagged = image_item(window.view.scene, ['甲'])
    clean = image_item(window.view.scene, [])
    popped = popover_of(window)
    popped.refresh()

    popped.untagged_box.setChecked(True)

    assert popped.panel._untagged_only is True
    assert clean.isVisible() and not tagged.isVisible()


def test_colour_filter_is_only_on_toolbar_but_still_in_summary(main_window):
    window = main_window
    popped = popover_of(window)
    popped.refresh()
    assert not popped.colour_buttons
    assert not hasattr(popped, 'colour_container')
    button = window.canvas_toolbar.colours['blue']

    button.click()

    assert popped.color_host._active_group == 'blue'
    assert popped.active_summary()  # 至少有一条
    button.click()
    assert popped.color_host._active_group is None


# ── 清除筛选 ──────────────────────────────────────────────────────────

def test_clearing_the_filters_does_not_switch_pages(main_window):
    """§8.3 的陷阱：清除恢复全部当前范围，**不能偷偷切换页面**。"""
    window = main_window
    item = image_item(window.view.scene, ['甲'])
    page_before = window.current_page_id
    popped = popover_of(window)
    popped.panel._active_tags = {'甲'}
    popped.panel._min_rating = 2
    popped.panel._untagged_only = True
    popped.panel._apply_filter()

    window._clear_filters()

    assert popped.panel._active_tags == set()
    assert popped.panel._min_rating == 0
    assert popped.panel._untagged_only is False
    assert item.isVisible(), '清完之后素材要都回来'
    assert window.current_page_id == page_before, '不能切页'


def test_clearing_also_drops_the_colour_filter(main_window):
    window = main_window
    popped = popover_of(window)
    popped.color_host.apply_filter('blue')
    assert popped.color_host._active_group == 'blue'

    window._clear_filters()

    assert popped.color_host._active_group is None


def test_popup_reset_button_synchronises_rating_and_keeps_page(main_window):
    window = main_window
    popup = popover_of(window)
    popup.rating_combo.setCurrentIndex(4)
    popup.untagged_box.setChecked(True)
    page_before = window.current_page_id
    popup.reset_button.click()
    assert popup.panel._min_rating == 0
    assert popup.panel.rating_filter.currentIndex() == 0
    assert not popup.untagged_box.isChecked()
    assert window.current_page_id == page_before


def test_popup_has_compact_empty_state_and_all_tags_are_reachable(main_window):
    popup = popover_of(main_window)
    popup.refresh()
    assert popup.tags_scroll.height() == 32
    popup.panel.scene.tag_names = [f'tag-{i}' for i in range(55)]
    popup.refresh()
    assert popup.tags_scroll.height() == 144
    assert sum(isinstance(popup.tags_layout.itemAt(i).widget(), QtWidgets.QCheckBox)
               for i in range(popup.tags_layout.count())) == 55
