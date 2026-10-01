"""Load Prism's locally authored, stroke-consistent SVG interface icons."""

from importlib.resources import files
from functools import lru_cache

from PyQt6 import QtCore, QtGui, QtWidgets

from prism import ui_tokens


@lru_cache(maxsize=64)
def tinted_icon(name, colour):
    """Tint the exact shared SVG, retaining its original path and stroke."""
    from PyQt6 import QtSvg
    data = ICON_DIR.joinpath(name + '.svg').read_bytes().replace(b'#AAB0BB', colour.encode('ascii'))
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(data))
    pixmap = QtGui.QPixmap(48, 48)
    pixmap.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QtGui.QIcon(pixmap)


ICON_DIR = files('prism.assets').joinpath('icons')


def icon(name):
    """Return an icon from the bundled SVG set, rejecting path traversal."""
    if not name or not all(ch.isascii() and (ch.isalnum() or ch == '-')
                           for ch in name):
        raise ValueError('Invalid icon name')
    path = ICON_DIR.joinpath(name + '.svg')
    if not path.is_file():
        raise ValueError('Unknown icon: ' + name)
    return QtGui.QIcon(str(path))


def set_icon_button(button, name, accessible_name, *, size=None):
    """Turn a QAbstractButton into a labelled, tooltip-backed icon button."""
    button.setObjectName('prismIconButton')
    button.setText('')
    button.setIcon(icon(name))
    side = size or ui_tokens.COMPONENTS['icon-size']
    button.setIconSize(QtCore.QSize(side, side))
    button.setAccessibleName(accessible_name)
    if not button.toolTip():
        button.setToolTip(accessible_name)
    return button


def icon_button(name, accessible_name, *, size=None, parent=None):
    button = QtWidgets.QToolButton(parent)
    side = size or ui_tokens.COMPONENTS['icon-button']
    button.setFixedSize(side, side)
    return set_icon_button(button, name, accessible_name)
