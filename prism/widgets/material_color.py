"""Compatibility adapter; the picker UI lives only in compact_color."""
from prism.widgets.compact_color import CompactColorDialog
from prism.i18n import _
from PyQt6 import QtCore, QtWidgets, sip


class MaterialColorDialog(CompactColorDialog):
    def __init__(self, rgb, parent=None, *, title=None, anchor=None):
        super().__init__(rgb, parent, title=title or _('Choose material color'))
        self._anchor = anchor
        # The model preview is an owned topmost window in another process.
        # Parenting to the inspector alone cannot put this dialog above it.
        self.setWindowFlag(QtCore.Qt.WindowType.WindowStaysOnTopHint, True)

    def showEvent(self, event):
        super().showEvent(event)
        anchor = self._anchor
        if anchor is not None and not sip.isdeleted(anchor):
            origin = anchor.mapToGlobal(QtCore.QPoint(0, 0))
            rect = QtCore.QRect(origin, anchor.size())
            screen = QtWidgets.QApplication.screenAt(rect.center()) or anchor.screen()
            if screen is not None:
                available = screen.availableGeometry()
                frame = self.frameGeometry()
                x = rect.left()
                if x + frame.width() > available.right() + 1:
                    x = rect.right() + 1 - frame.width()
                y = rect.bottom() + 7
                if y + frame.height() > available.bottom() + 1:
                    y = rect.top() - frame.height() - 6
                x = max(available.left(), min(x, available.right() + 1 - frame.width()))
                y = max(available.top(), min(y, available.bottom() + 1 - frame.height()))
                self.move(x, y)
        self.raise_()
        self.activateWindow()
