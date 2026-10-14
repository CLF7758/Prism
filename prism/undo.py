"""Protect Qt's undo callbacks from recursively creating commands."""
from PyQt6 import QtGui


class UndoStack(QtGui.QUndoStack):
    executing = False

    def push(self, command):
        if self.executing:
            return
        self.executing = True
        try:
            super().push(command)
        finally:
            self.executing = False

    def undo(self):
        if self.executing:
            return
        self.executing = True
        try:
            super().undo()
        finally:
            self.executing = False

    def redo(self):
        if self.executing:
            return
        self.executing = True
        try:
            super().redo()
        finally:
            self.executing = False

    def setIndex(self, index):
        if self.executing:
            return
        self.executing = True
        try:
            super().setIndex(index)
        finally:
            self.executing = False
