"""The shared SVG assets must render and icon-only controls stay named."""

from xml.etree import ElementTree

from PyQt6 import QtWidgets

from prism.widgets.components.icons import ICON_DIR, icon, set_icon_button


NAMES = (
    'folder', 'canvas', 'mindmap', 'document', 'tag', 'search', 'filter',
    'plus', 'more', 'chevron-down', 'close', 'undo', 'redo', 'trash', 'move',
    'export', 'settings', 'save-state',
)


def test_all_required_icons_are_consistent_and_render(qapp):
    for name in NAMES:
        root = ElementTree.parse(ICON_DIR.joinpath(name + '.svg')).getroot()
        assert root.attrib['viewBox'] == '0 0 24 24'
        assert root.attrib['stroke-width'] == '1.75'
        assert not icon(name).pixmap(16, 16).isNull()


def test_icon_only_button_has_accessible_name_and_tooltip(qapp):
    button = QtWidgets.QToolButton()
    set_icon_button(button, 'settings', '管理标签')
    assert button.text() == ''
    assert button.accessibleName() == '管理标签'
    assert button.toolTip() == '管理标签'
    assert not button.icon().isNull()
