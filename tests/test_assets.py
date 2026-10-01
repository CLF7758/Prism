from PyQt6 import QtGui

from prism.assets import BeeAssets


def test_singleton(view):
    assert BeeAssets() is BeeAssets()
    assert BeeAssets().logo is BeeAssets().logo


def test_has_logo(view):
    assert isinstance(BeeAssets().logo, QtGui.QIcon)
