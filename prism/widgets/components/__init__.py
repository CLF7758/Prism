"""Small Qt controls that share Prism's design-token geometry and roles.

Colours and interaction states stay in theme.qss. These classes only provide
the dimensions and semantic role that Qt cannot reliably infer from QSS.
"""

from PyQt6 import QtGui, QtWidgets

from prism import ui_tokens


class RoleLabel(QtWidgets.QLabel):
    def __init__(self, text='', role='secondary', parent=None):
        super().__init__(text, parent)
        self.setProperty('uiRole', role)


class LineEdit(QtWidgets.QLineEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(ui_tokens.COMPONENTS['input-height'])
        font = QtGui.QFont(self.font())
        font.setPixelSize(ui_tokens.TEXT_ROLES['inspector-field-value'].size)
        self.setFont(font)


class TextEdit(QtWidgets.QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(ui_tokens.COMPONENTS['textarea-min-height'])
        self.setMaximumHeight(ui_tokens.COMPONENTS['textarea-max-height'])


class Button(QtWidgets.QPushButton):
    def __init__(self, text='', parent=None):
        super().__init__(text, parent)
        self.setMinimumHeight(ui_tokens.COMPONENTS['button-height'])
        self.setMinimumWidth(ui_tokens.COMPONENTS['button-min-width'])
