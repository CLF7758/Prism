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

        # AI 服务
        ai_tab = QtWidgets.QWidget()
        ai_layout = QtWidgets.QVBoxLayout(ai_tab)
        
        # API Key
        ai_layout.addWidget(QtWidgets.QLabel("DeepSeek API Key:"))
        self.api_key_input = QtWidgets.QLineEdit()
        self.api_key_input.setPlaceholderText("sk-...")
        ai_layout.addWidget(self.api_key_input)
        
        # Base URL
        ai_layout.addWidget(QtWidgets.QLabel("API Base URL:"))
        self.base_url_input = QtWidgets.QLineEdit()
        self.base_url_input.setPlaceholderText("https://api.deepseek.com")
        ai_layout.addWidget(self.base_url_input)
        
        # 模型名
        ai_layout.addWidget(QtWidgets.QLabel("模型名:"))
        self.model_input = QtWidgets.QLineEdit()
        self.model_input.setPlaceholderText("deepseek-chat")
        ai_layout.addWidget(self.model_input)
        
        # 开关
        self.translate_checkbox = QtWidgets.QCheckBox("启用标签翻译（英文→中文）")
        ai_layout.addWidget(self.translate_checkbox)
        
        self.title_checkbox = QtWidgets.QCheckBox("启用自动标题生成")
        ai_layout.addWidget(self.title_checkbox)
        
        self.category_checkbox = QtWidgets.QCheckBox("启用自动分类归纳")
        ai_layout.addWidget(self.category_checkbox)
        
        self.local_only_checkbox = QtWidgets.QCheckBox("纯本地模式（不调用 API）")
        ai_layout.addWidget(self.local_only_checkbox)
        
        # 加载当前配置
        from prism.ai_client import get_client
        client = get_client()
        self.api_key_input.setText(client.get_api_key())
        self.base_url_input.setText(client.get_base_url())
        self.model_input.setText(client.get_model())
        self.translate_checkbox.setChecked(client.is_translate_enabled())
        self.title_checkbox.setChecked(client.is_title_enabled())
        self.category_checkbox.setChecked(client.is_category_enabled())
        self.local_only_checkbox.setChecked(client.is_local_only_mode())
        
        # 保存按钮
        save_btn = QtWidgets.QPushButton("保存 AI 配置")
        save_btn.clicked.connect(self.save_ai_settings)
        ai_layout.addWidget(save_btn)
        
        ai_layout.addStretch()
        tabs.addTab(ai_tab, "AI 服务")

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
    
    def save_ai_settings(self):
        """保存 AI 服务配置"""
        from prism.ai_client import (
            SETTINGS_KEY_API_KEY, SETTINGS_KEY_BASE_URL, SETTINGS_KEY_MODEL,
            SETTINGS_KEY_TRANSLATE_ENABLED, SETTINGS_KEY_TITLE_ENABLED,
            SETTINGS_KEY_CATEGORY_ENABLED, SETTINGS_KEY_LOCAL_ONLY
        )
        
        settings = self.settings
        settings.setValue(SETTINGS_KEY_API_KEY, self.api_key_input.text().strip())
        settings.setValue(SETTINGS_KEY_BASE_URL, self.base_url_input.text().strip())
        settings.setValue(SETTINGS_KEY_MODEL, self.model_input.text().strip())
        settings.setValue(SETTINGS_KEY_TRANSLATE_ENABLED, self.translate_checkbox.isChecked())
        settings.setValue(SETTINGS_KEY_TITLE_ENABLED, self.title_checkbox.isChecked())
        settings.setValue(SETTINGS_KEY_CATEGORY_ENABLED, self.category_checkbox.isChecked())
        settings.setValue(SETTINGS_KEY_LOCAL_ONLY, self.local_only_checkbox.isChecked())
        
        QtWidgets.QMessageBox.information(
            self, "保存成功", "AI 服务配置已保存。")
