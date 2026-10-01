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

from functools import partial
import logging

from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from prism import constants
from prism.config import PrismSettings, settings_events
from prism.i18n import _
from prism.i18n.translator import LANGUAGES


logger = logging.getLogger(__name__)


class GroupBase(QtWidgets.QGroupBox):
    TITLE = None
    HELPTEXT = None
    KEY = None

    def __init__(self):
        super().__init__()
        self.settings = PrismSettings()
        self.update_title()
        self.layout = QtWidgets.QVBoxLayout()
        self.setLayout(self.layout)
        settings_events.restore_defaults.connect(self.on_restore_defaults)

        if self.HELPTEXT:
            helptxt = QtWidgets.QLabel(self.HELPTEXT)
            helptxt.setWordWrap(True)
            self.layout.addWidget(helptxt)

    def update_title(self):
        title = [self.TITLE]
        if self.settings.value_changed(self.KEY):
            title.append(constants.CHANGED_SYMBOL)
        self.setTitle(' '.join(title))

    def on_value_changed(self, value):
        if self.ignore_value_changed:
            return

        value = self.convert_value_from_qt(value)
        if value != self.settings.valueOrDefault(self.KEY):
            logger.debug(f'Setting {self.KEY} changed to: {value}')
            self.settings.setValue(self.KEY, value)
            self.update_title()

    def convert_value_from_qt(self, value):
        return value

    def on_restore_defaults(self):
        new_value = self.settings.valueOrDefault(self.KEY)
        self.ignore_value_changed = True
        self.set_value(new_value)
        self.ignore_value_changed = False
        self.update_title()


class RadioGroup(GroupBase):
    OPTIONS = None

    def __init__(self):
        super().__init__()

        self.ignore_value_changed = True
        self.buttons = {}
        for (value, label, helptext) in self.OPTIONS:
            btn = QtWidgets.QRadioButton(label)
            self.buttons[value] = btn
            btn.setToolTip(helptext)
            btn.toggled.connect(partial(self.on_value_changed, value=value))
            if value == self.settings.valueOrDefault(self.KEY):
                btn.setChecked(True)
            self.layout.addWidget(btn)

        self.ignore_value_changed = False
        self.layout.addStretch(100)

    def set_value(self, value):
        for old_value, btn in self.buttons.items():
            btn.setChecked(old_value == value)


class IntegerGroup(GroupBase):
    MIN = None
    MAX = None

    def __init__(self):
        super().__init__()
        self.input = QtWidgets.QSpinBox()
        self.input.setRange(self.MIN, self.MAX)
        self.set_value(self.settings.valueOrDefault(self.KEY))
        self.input.valueChanged.connect(self.on_value_changed)
        self.layout.addWidget(self.input)
        self.layout.addStretch(100)
        self.ignore_value_changed = False

    def set_value(self, value):
        self.input.setValue(value)


class SingleCheckboxGroup(GroupBase):
    LABEL = None

    def __init__(self):
        super().__init__()
        self.input = QtWidgets.QCheckBox(self.LABEL)
        self.set_value(self.settings.valueOrDefault(self.KEY))
        self.input.checkStateChanged.connect(self.on_value_changed)
        self.layout.addWidget(self.input)
        self.layout.addStretch(100)
        self.ignore_value_changed = False

    def set_value(self, value):
        self.input.setChecked(value)

    def convert_value_from_qt(self, value):
        return value == Qt.CheckState.Checked


class ArrangeDefaultWidget(RadioGroup):
    TITLE = _('Default Arrange Method:')
    HELPTEXT = _('How images are arranged when inserted in batch')
    KEY = 'Items/arrange_default'
    OPTIONS = (
        ('optimal', _('Optimal'), _('Arrange Optimal')),
        ('horizontal', _('Horizontal (by filename)'),
         _('Arrange Horizontal (by filename)')),
        ('vertical', _('Vertical (by filename)'),
         _('Arrange Vertical (by filename)')),
        ('square', _('Square (by filename)'), _('Arrannge Square (by filename)')))


class ImageStorageFormatWidget(RadioGroup):
    TITLE = _('Image Storage Format:')
    HELPTEXT = _('How images are stored inside bee files. Changes will only take effect on newly saved images.')
    KEY = 'Items/image_storage_format'
    OPTIONS = (
        ('best', _('Best Guess'),
         _('Small images and images with alpha channel are stored as png, everything else as jpg')),
        ('png', _('Always PNG'), _('Lossless, but large bee file')),
        ('jpg', _('Always JPG'),
         _('Small bee file, but lossy and no transparency support')))


class ArrangeGapWidget(IntegerGroup):
    TITLE = _('Arrange Gap:')
    HELPTEXT = _('The gap between images when using arrange actions.')
    KEY = 'Items/arrange_gap'
    MIN = 0
    MAX = 200


class AllocationLimitWidget(IntegerGroup):
    TITLE = _('Maximum Image Size:')
    HELPTEXT = _('The maximum image size that can be loaded (in megabytes). Set to 0 for no limitation.')
    KEY = 'Items/image_allocation_limit'
    MIN = 0
    MAX = 10000


class ConfirmCloseUnsavedWidget(SingleCheckboxGroup):
    TITLE = _('Confirm when closing an unsaved file:')
    HELPTEXT = _('When about to close an unsaved file, should Prism ask for confirmation?')
    LABEL = _('Confirm when closing')
    KEY = 'Save/confirm_close_unsaved'


class ClipboardCaptureWidget(SingleCheckboxGroup):
    TITLE = _('Clipboard Inbox:')
    HELPTEXT = _('Collect copied images for review before adding them.')
    LABEL = _('Automatically watch the clipboard')
    KEY = 'Clipboard/auto_capture'


class ClipboardUrlDownloadWidget(SingleCheckboxGroup):
    TITLE = _('Copied image URLs:')
    HELPTEXT = _('Download explicit HTTP image URLs in the background and '
                 'show the result in the clipboard inbox.')
    LABEL = _('Download copied image URLs')
    KEY = 'Clipboard/download_image_urls'


class ClipboardMaxItemsWidget(IntegerGroup):
    TITLE = _('Clipboard inbox capacity:')
    HELPTEXT = _('Maximum number of reviewed captures kept in memory.')
    KEY = 'Clipboard/max_items'
    MIN = 1
    MAX = 500


class LanguageWidget(RadioGroup):
    TITLE = _('Language:')
    HELPTEXT = _('Changes will take effect after restarting Prism.')
    KEY = 'Interface/language'
    OPTIONS = tuple(
        (code, label, '') for code, label in LANGUAGES
    )


class SettingsDialog(QtWidgets.QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(f'{constants.APPNAME} {_("Settings")}')
        tabs = QtWidgets.QTabWidget()

        # Miscellaneous
        misc = QtWidgets.QWidget()
        misc_layout = QtWidgets.QGridLayout()
        misc.setLayout(misc_layout)
        misc_layout.addWidget(ConfirmCloseUnsavedWidget(), 0, 0)
        misc_layout.addWidget(LanguageWidget(), 0, 1)
        tabs.addTab(misc, _('&Miscellaneous'))

        # Images & Items
        items = QtWidgets.QWidget()
        items_layout = QtWidgets.QGridLayout()
        items.setLayout(items_layout)
        items_layout.addWidget(ImageStorageFormatWidget(), 0, 0)
        items_layout.addWidget(AllocationLimitWidget(), 0, 1)
        items_layout.addWidget(ArrangeGapWidget(), 1, 0)
        items_layout.addWidget(ArrangeDefaultWidget(), 1, 1)
        tabs.addTab(items, _('&Images && Items'))

        clipboard = QtWidgets.QWidget()
        clipboard_layout = QtWidgets.QGridLayout(clipboard)
        clipboard_layout.addWidget(ClipboardCaptureWidget(), 0, 0)
        clipboard_layout.addWidget(ClipboardUrlDownloadWidget(), 0, 1)
        clipboard_layout.addWidget(ClipboardMaxItemsWidget(), 1, 0)
        tabs.addTab(clipboard, _('&Clipboard'))

        layout = QtWidgets.QVBoxLayout()
        self.setLayout(layout)
        layout.addWidget(tabs)

        # Bottom row of buttons
        buttons = QtWidgets.QDialogButtonBox()
        close_btn = QtWidgets.QPushButton(_('&Close'))
        close_btn.setAutoDefault(False)
        buttons.addButton(close_btn, QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
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
            _('Do you want to restore all settings to their default values?'))

        if reply == QtWidgets.QMessageBox.StandardButton.Yes:
            PrismSettings().restore_defaults()
