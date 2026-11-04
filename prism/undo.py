"""Protect Qt's undo callbacks from recursively creating commands."""
from PyQt6 import QtGui


class UndoStack(QtGui.QUndoStack):
    executing = False

    def preview_locked(self):
        return bool(getattr(self.parent(), '_upgrade_preview_readonly', False))

    def push(self, command):
        if self.executing or self.preview_locked():
            return
        self.executing = True
        try:
            super().push(command)
        finally:
            self.executing = False

    def undo(self):
        if self.executing or self.preview_locked():
            return
        self.executing = True
        try:
            super().undo()
        finally:
            self.executing = False

    def redo(self):
        if self.executing or self.preview_locked():
            return
        self.executing = True
        try:
            super().redo()
        finally:
            self.executing = False

    def setIndex(self, index):
        if self.executing or self.preview_locked():
            return
        self.executing = True
        try:
            super().setIndex(index)
        finally:
            self.executing = False
