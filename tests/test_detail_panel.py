"""属性面板：右栏那个「文字」样式分组已经按用户要求删除。

删掉的是**界面**——右栏最下面的「文字」分组（颜色 / 字号 / 粗体 / 斜体）。
文字素材自己的样式数据一个字没动：旧工程里的颜色和字号照样读得出来、
画得出来，`PrismTextItem.set_style()` 和 `commands.ChangeTextStyle` 也都还在
（`tests/test_text_style.py` 继续守着它们），只是**目前没有任何界面入口**
再去改它了。

这条测试是那次删除的验收，同时防回归：控件不许被顺手加回来。
"""
import pytest

from prism.items import PrismTextItem

#: 删掉的那些控件和只服务于它们的方法。
REMOVED_WIDGETS = ('_text_style_container', '_text_colour_button',
                   '_text_size', '_text_bold', '_text_italic')
REMOVED_METHODS = ('_update_text_controls', '_pick_text_colour',
                   '_show_text_colour', '_on_text_style_changed')


def test_the_panel_no_longer_carries_the_text_style_controls(view, qapp):
    panel = view._detail_panel
    for name in REMOVED_WIDGETS + REMOVED_METHODS:
        assert not hasattr(panel, name), name


def test_selecting_a_text_item_still_refreshes_the_panel(view, qapp):
    """删除只该拿走那些控件，不该让选中文字素材变得会崩。"""
    panel = view._detail_panel
    item = PrismTextItem('面板刷新测试')
    view.scene.addItem(item)
    view.scene.clearSelection()
    item.setSelected(True)

    panel._on_selection_changed()

    assert panel._item is item


def test_a_text_item_keeps_its_style_without_the_controls(view, qapp):
    """界面没了，数据还在：颜色字号仍然存得住、读得回。"""
    item = PrismTextItem('带样式的文字')
    item.set_style(color='#123456', size=21.0, bold=True)

    saved = item.get_extra_save_data()
    restored = PrismTextItem.create_from_data(data=saved)

    style = restored.style()
    assert style['color'] == '#123456'
    assert style['size'] == pytest.approx(21.0)
    assert style['bold'] is True


def test_channel_switch_for_hdr_keeps_display_option_out_of_decoder(view, tmp_path, monkeypatch):
    from PyQt6 import QtGui
    from prism.fileio import image as image_io
    from tests.test_material_features import image_item

    path = tmp_path / 'scene.hdr'
    path.touch()
    received = []
    def decode(path, exposure=0.0, gamma=2.2, operator='reinhard', channel='rgb'):
        received.append(channel)
        result = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
        result.fill(QtGui.QColor('red'))
        return result
    monkeypatch.setattr(image_io, '_load_with_openimageio', decode)
    item = image_item(view.scene)
    item.filename = str(path)
    item.setSelected(True)
    panel = view._detail_panel
    panel._on_selection_changed()
    for index in range(panel._channel_combo.count()):
        panel._channel_combo.setCurrentIndex(index)
    panel._false_color_box.setChecked(True)
    assert set(received) >= {'r', 'g', 'b', 'alpha', 'luminance'}
    assert not item.pixmap().isNull()


def test_decode_error_preserves_previous_preview(view, tmp_path, monkeypatch):
    from prism.widgets import detail_panel
    from tests.test_material_features import image_item

    path = tmp_path / 'broken.exr'
    path.touch()
    item = image_item(view.scene)
    item.filename = str(path)
    item.setSelected(True)
    panel = view._detail_panel
    panel._on_selection_changed()
    before = item.pixmap().toImage()
    def fail(*args, **kwargs):
        raise RuntimeError('bad image data')
    monkeypatch.setattr(detail_panel, 'load_image', fail)
    panel._channel_combo.setCurrentIndex(1)
    assert item.pixmap().toImage() == before
    assert '保留原图' in panel._hdr_hint.text()
