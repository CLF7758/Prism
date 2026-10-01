# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

import logging

from PyQt6 import QtWidgets

from prism.config import KeyboardSettings
from prism.i18n import _
from prism.widgets.controls.keyboard import KeyboardShortcutsView
from prism.widgets.controls.mouse import MouseView
from prism.widgets.controls.mousewheel import MouseWheelView


logger = logging.getLogger(__name__)


class ControlsDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(_('Keyboard & Mouse Controls'))
        tabs = QtWidgets.QTabWidget()

        # Keyboard shortcuts
        keyboard = QtWidgets.QWidget(parent)
        kb_layout = QtWidgets.QVBoxLayout()
        keyboard.setLayout(kb_layout)
        table = KeyboardShortcutsView(keyboard)
        search_input = QtWidgets.QLineEdit()
        search_input.setPlaceholderText(_('Search...'))
        search_input.textChanged.connect(table.model().setFilterFixedString)
        kb_layout.addWidget(search_input)
        kb_layout.addWidget(table)
        tabs.addTab(keyboard, _('&Keyboard Shortcuts'))

        # Mouse controls
        mouse = QtWidgets.QWidget(parent)
        mouse_layout = QtWidgets.QVBoxLayout()
        mouse.setLayout(mouse_layout)
        table = MouseView(mouse)
        search_input = QtWidgets.QLineEdit()
        search_input.setPlaceholderText(_('Search...'))
        search_input.textChanged.connect(table.model().setFilterFixedString)
        mouse_layout.addWidget(search_input)
        mouse_layout.addWidget(table)
        tabs.addTab(mouse, _('&Mouse'))

        # Mouse wheel controls
        mousewheel = QtWidgets.QWidget(parent)
        wheel_layout = QtWidgets.QVBoxLayout()
        mousewheel.setLayout(wheel_layout)
        table = MouseWheelView(mousewheel)
        search_input = QtWidgets.QLineEdit()
        search_input.setPlaceholderText(_('Search...'))
        search_input.textChanged.connect(table.model().setFilterFixedString)
        wheel_layout.addWidget(search_input)
        wheel_layout.addWidget(table)
        tabs.addTab(mousewheel, _('Mouse &Wheel'))

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(tabs)

        # Bottom row of buttons
        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        reset_btn = QtWidgets.QPushButton(_('&Restore Defaults'))
        reset_btn.setAutoDefault(False)
        reset_btn.clicked.connect(self.on_restore_defaults)
        buttons.addButton(reset_btn,
                          QtWidgets.QDialogButtonBox.ButtonRole.ActionRole)

        layout.addWidget(buttons)
        self.show()

    def on_restore_defaults(self, *args, **kwargs):
        reply = QtWidgets.QMessageBox.question(
            self,
            _('Restore defaults?'),
            _('Do you want to restore all keyboard and mouse settings '
              'to their default values?'))

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            KeyboardSettings().restore_defaults()
