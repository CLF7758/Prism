"""Behaviour of the compact library navigation and inspector controls."""
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.items import PrismPixmapItem, PrismTextItem
from prism.widgets.resource_tree_model import ASSET_COUNT_ROLE
from prism.widgets.components.flow_layout import FlowLayout


def photo(view, tags=(), canvas=None):
    image = QtGui.QImage(24, 24, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#336699'))
    item = PrismPixmapItem(image)
    item._tags = list(tags)
    item._canvas_id = canvas or view.current_canvas_id
    view.scene.addItem(item)
    return item


def test_untagged_navigation_uses_project_scope_and_result_mode(view):
    """方案 §6、§12 A01/A03/A04：全部/未标签是全项目口径，点击进入结果模式。

    旧测试断言"顶部导航只过滤当前画布"，与本次需求冲突，已重写。
    """
    plain = photo(view)
    tagged = photo(view, ['reference'])
    elsewhere = photo(view, canvas='other-canvas')
    # Explicit known canvas prevents the old-file compatibility fallback.
    view.scene.workspace_pages.append({'id': 'other-canvas', 'kind': 'canvas', 'title': 'Other'})
    annotation = PrismTextItem('A note')
    view.scene.addItem(annotation)
    panel = view.category_panel
    panel.update_counts()
    # 全项目口径：3 张图（注释不计），其中 2 张无标签（plain + elsewhere）
    assert panel._nav_counts['all'].text() == '3'
    assert panel._nav_counts['untagged'].text() == '2'
    # 点击未标签：进入全项目结果模式
    panel.untagged_button.click()
    browser = view.window().asset_browser
    assert browser.mode == 'untagged'
    assert set(browser.model.items) == {plain, elsewhere}
    assert panel.resource_tree.isVisible()
    assert not panel._untagged_only
    panel.all_button.click()
    assert browser.mode == 'all'
    assert set(browser.model.items) == {plain, tagged, elsewhere}
    view.window().open_workspace_page('canvas', view.current_canvas_id)
    assert not browser.isVisible()
    assert panel.resource_tree.isVisible()


def test_tree_counts_roll_up_nested_canvas_folders(view):
    panel = view.category_panel
    folder = panel.resource_service.createFolder('canvas', 'References')
    page = panel.resource_service.createPage('canvas', 'Board', 'test-board', folder['id'])
    model = panel.resource_tree.model()
    model.set_asset_counts({'test-board': 3})
    assert model.index_for_id(page['id']).data(ASSET_COUNT_ROLE) == 3
    assert model.index_for_id(folder['id']).data(ASSET_COUNT_ROLE) == 3
    model.set_asset_counts({'test-board': 1})
    assert model.index_for_id(folder['id']).data(ASSET_COUNT_ROLE) == 1


def test_color_distribution_restores_bar_and_labels(view):
    """需求 3：右侧色彩分析恢复原「色彩分布条 + 名称/百分比」实现。

    Eagle 风格的圆点色带（`_palette` / `_palette_layout`）按方案 §8 移除，
    改回原 `_cd_container`（色条 + 标签行）。用 isHidden() 而不是
    isVisible() 判断——离屏测试下面板父级没有 show()，isVisible() 恒为
    False，只有 isHidden() 能真实反映 setVisible 的调用结果。
    """
    item = photo(view)
    item.setSelected(True)
    panel = view._detail_panel
    panel._on_selection_changed()
    # 旧圆点色带彻底移除，不再有任何引用。
    assert not hasattr(panel, '_palette')
    assert not hasattr(panel, '_palette_layout')
    # 原色彩分布：色条 + 标签行两个部件，容器解除隐藏。
    assert panel._cd_layout.count() == 2
    assert not panel._cd_container.isHidden()
    view.scene.clearSelection()
    panel._on_selection_changed()
    # 取消选择后色彩分布清空并隐藏，不留上一张图的残影。
    assert panel._cd_layout.count() == 0
    assert panel._cd_container.isHidden()


def test_tag_chips_wrap_and_long_names_fit(qapp):
    container = QtWidgets.QWidget()
    layout = FlowLayout(container)
    for title in ('a very long namespace / tag name', 'Reference', 'Colour'):
        layout.addWidget(QtWidgets.QCheckBox(title))
    container.show()
    layout.setGeometry(QtCore.QRect(0, 0, 110, 150))
    assert layout.heightForWidth(110) > layout.heightForWidth(600)
    for index in range(layout.count()):
        assert layout.itemAt(index).geometry().right() < 110
    container.close()
