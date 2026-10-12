"""Wrapping chip layout for a resizable inspector."""
from PyQt6 import QtCore, QtWidgets


class FlowLayout(QtWidgets.QLayout):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._items = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(5)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return QtCore.Qt.Orientation(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._arrange(QtCore.QRect(0, 0, width, 0), False)

    def minimumSize(self):
        # Long tag names must not force the splitter to widen the inspector.
        return QtCore.QSize(0, 26 if self._items else 0)

    def sizeHint(self):
        return self.minimumSize()

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._arrange(rect, True)

    def _arrange(self, rect, apply):
        x, y, height = rect.x(), rect.y(), 0
        gap = self.spacing()
        for item in self._items:
            if item.isEmpty():
                continue
            size = item.sizeHint()
            width = min(size.width(), rect.width())
            if x > rect.x() and x + width > rect.x() + rect.width():
                x, y, height = rect.x(), y + height + gap, 0
            if apply:
                item.setGeometry(QtCore.QRect(x, y, width, size.height()))
            x += width + gap
            height = max(height, size.height())
        return y + height - rect.y()
