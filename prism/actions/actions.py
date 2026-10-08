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

from functools import cached_property
import logging

from PyQt6 import QtGui

from prism.actions.menu_structure import menu_structure
from prism.config import KeyboardSettings, settings_events
from prism.i18n import _
from prism.utils import ActionList


logger = logging.getLogger(__name__)


class Action:
    SETTINGS_GROUP = 'Actions'

    def __init__(self, id, text, callback=None, shortcuts=None,
                 checkable=False, checked=False, group=None, settings=None,
                 enabled=True, menu_item=None, menu_id=None):
        self.id = id
        self.text = text
        self.callback = callback
        self.shortcuts = shortcuts or []
        self.checkable = checkable
        self.checked = checked
        self.group = group
        self.settings = settings
        self.enabled = enabled
        self.menu_item = menu_item
        self.menu_id = menu_id
        self.qaction = None
        self.kb_settings = KeyboardSettings()
        settings_events.restore_keyboard_defaults.connect(
            self.on_restore_defaults)

    def __eq__(self, other):
        return self.id == other.id

    def __str__(self):
        return self.id

    def on_restore_defaults(self):
        if self.qaction:
            self.qaction.setShortcuts(self.get_shortcuts())

    @cached_property
    def menu_path(self):
        path = []

        def _get_path(menu_item):
            if isinstance(menu_item['items'], list):
                # This is a normal menu
                for item in menu_item['items']:
                    if item == self.id:
                        path.append(menu_item['menu'])
                        return True
                    if isinstance(item, dict):
                        # This is a submenu
                        if _get_path(item):
                            path.append(menu_item['menu'])
                            return True
            elif menu_item['items'] == self.menu_id:
                # This is a dynamic submenu (e.g. Recent Files)
                path.append(menu_item['menu'])
                return True

        for menu_item in menu_structure:
            _get_path(menu_item)

        return path[::-1]

    def get_shortcuts(self):
        return self.kb_settings.get_list(
            self.SETTINGS_GROUP, self.id, self.shortcuts)

    def set_shortcuts(self, value):
        logger.debug(f'Setting shortcut "{self.id}" to: {value}')
        self.kb_settings.set_list(
            self.SETTINGS_GROUP, self.id, value, self.shortcuts)
        if self.qaction:
            self.qaction.setShortcuts(value)

    def get_qkeysequence(self, index):
        """Current shortcuts as QKeySequence"""
        try:
            return QtGui.QKeySequence(self.get_shortcuts()[index])
        except IndexError:
            return QtGui.QKeySequence()

    def shortcuts_changed(self):
        """Whether shortcuts have changed from their defaults."""
        return self.get_shortcuts() != self.shortcuts

    def get_default_shortcut(self, index):
        try:
            return self.shortcuts[index]
        except IndexError:
            return None


actions = ActionList([
    Action(
        id='open',
        text=_('&Open'),
        shortcuts=['Ctrl+O'],
        callback='on_action_open',
    ),
    Action(
        id='save',
        text=_('&Save'),
        shortcuts=['Ctrl+S'],
        callback='on_action_save',
    ),
    Action(
        id='save_as',
        text=_('Save &As...'),
        shortcuts=['Ctrl+Shift+S'],
        callback='on_action_save_as',
    ),
    Action(
        id='export_scene',
        text=_('E&xport Scene...'),
        shortcuts=['Ctrl+Shift+E'],
        callback='on_action_export_scene',
        group='active_when_items_in_scene',
    ),
    Action(
        id='export_images',
        text=_('Export &Images...'),
        callback='on_action_export_images',
        group='active_when_items_in_scene',
    ),
    Action(
        id='export_selected_images',
        text=_('Export Selected &Images...'),
        callback='on_action_export_selected_images',
        group='active_when_selection',
    ),
    Action(
        id='import_document',
        text=_('Import &Word Document...'),
        shortcuts=['Ctrl+Shift+I'],
        callback='on_action_import_document',
    ),
    Action(
        id='import_mindmap',
        text=_('Import &XMind Mind Map...'),
        shortcuts=['Ctrl+Shift+X'],
        callback='on_action_import_mindmap',
    ),
    Action(
        id='export_note_word',
        text=_('Export Current &Document...'),
        shortcuts=['Ctrl+Shift+W'],
        callback='on_action_export_note_word',
    ),
    Action(
        id='export_mindmap',
        text=_('Export Current &Mind Map...'),
        # Ctrl+Shift+M 已经被 toggle_side_panel 占着（那是原有的动作）。
        # 两个动作抢一个键的话只有一个能触发，另一个等于失灵 ——
        # tools/_probe_menu3.py 那种遍历就能查出来。
        shortcuts=['Ctrl+Shift+D'],
        callback='on_action_export_mindmap',
    ),
    Action(
        id='quit',
        text=_('&Quit'),
        shortcuts=['Ctrl+Q'],
        callback='on_action_quit',
    ),
    Action(
        id='insert_images',
        text=_('&Images...'),
        shortcuts=['Ctrl+I'],
        callback='on_action_insert_images',
    ),
    Action(
        id='insert_text',
        text=_('&Text'),
        shortcuts=['Ctrl+T'],
        callback='on_action_insert_text',
    ),
    Action(
        id='clipboard_inbox',
        text=_('Clipboard &Inbox'),
        shortcuts=['Ctrl+Shift+V'],
        callback='on_action_clipboard_inbox',
    ),
    Action(
        id='undo',
        text=_('&Undo'),
        shortcuts=['Ctrl+Z'],
        callback='on_action_undo',
        group='active_when_can_undo',
    ),
    Action(
        id='redo',
        text=_('&Redo'),
        shortcuts=['Ctrl+Shift+Z'],
        callback='on_action_redo',
        group='active_when_can_redo',
    ),
    Action(
        id='copy',
        text=_('&Copy'),
        shortcuts=['Ctrl+C'],
        callback='on_action_copy',
        group='active_when_selection',
    ),
    Action(
        id='cut',
        text=_('Cu&t'),
        shortcuts=['Ctrl+X'],
        callback='on_action_cut',
        group='active_when_selection',
    ),
    Action(
        id='paste',
        text=_('&Paste'),
        shortcuts=['Ctrl+V'],
        callback='on_action_paste',
    ),
    Action(
        id='delete',
        text=_('&Delete'),
        shortcuts=['Del'],
        callback='on_action_delete_items',
        group='active_when_selection',
    ),
    Action(
        id='raise_to_top',
        text=_('&Raise to Top'),
        shortcuts=['PgUp'],
        callback='on_action_raise_to_top',
        group='active_when_selection',
    ),
    Action(
        id='lower_to_bottom',
        text=_('Lower to Bottom'),
        shortcuts=['PgDown'],
        callback='on_action_lower_to_bottom',
        group='active_when_selection',
    ),



    Action(
        id='arrange_optimal',
        text=_('&Optimal'),
        shortcuts=['Shift+O'],
        callback='on_action_arrange_optimal',
        group='active_when_selection',
    ),
    Action(
        id='arrange_horizontal',
        text=_('&Horizontal (by filename)'),
        callback='on_action_arrange_horizontal',
        group='active_when_selection',
    ),
    Action(
        id='arrange_vertical',
        text=_('&Vertical (by filename)'),
        callback='on_action_arrange_vertical',
        group='active_when_selection',
    ),
    Action(
        id='arrange_square',
        text=_('&Square (by filename)'),
        callback='on_action_arrange_square',
        group='active_when_selection',
    ),
    Action(
        id='change_opacity',
        text=_('Change &Opacity...'),
        callback='on_action_change_opacity',
        group='active_when_selection',
    ),
    Action(
        id='grayscale',
        text=_('&Grayscale'),
        shortcuts=['G'],
        checkable=True,
        callback='on_action_grayscale',
        group='active_when_selection',
    ),

    Action(
        id='sample_color',
        text=_('Sample Color'),
        shortcuts=['S'],
        callback='on_action_sample_color',
        group='active_when_items_in_scene',
    ),
    Action(
        id='find_duplicates',
        text=_('Find Duplicate Photos...'),
        callback='on_action_find_duplicates',
        group='active_when_items_in_scene',
    ),
    Action(
        id='group_selection', text=_('Group'), shortcuts=['C'],
        callback='on_action_group_selection', group='active_when_selection',
    ),
    Action(
        id='ungroup_selection', text=_('Ungroup'), shortcuts=['Ctrl+Shift+G'],
        callback='on_action_ungroup_selection', group='active_when_selection',
    ),
    Action(
        id='edit_group_note', text=_('Edit Group Note…'),
        callback='on_action_edit_group_note', group='active_when_selection',
    ),
    Action(
        id='crop',
        text=_('&Crop'),
        shortcuts=['Shift+C'],
        callback='on_action_crop',
        group='active_when_single_image',
    ),
    Action(
        id='flip_horizontally',
        text=_('Flip &Horizontally'),
        shortcuts=['H'],
        callback='on_action_flip_horizontally',
        group='active_when_selection',
    ),
    Action(
        id='flip_vertically',
        text=_('Flip &Vertically'),
        shortcuts=['V'],
        callback='on_action_flip_vertically',
        group='active_when_selection',
    ),
    Action(
        id='new_scene',
        text=_('&New Scene'),
        shortcuts=['Ctrl+N'],
        callback='on_action_new_scene',
    ),
    Action(
        id='fit_scene',
        text=_('&Fit Scene'),
        shortcuts=['1'],
        callback='on_action_fit_scene',
    ),
    Action(
        id='fit_selection',
        text=_('Fit &Selection'),
        shortcuts=['2'],
        callback='on_action_fit_selection',
        group='active_when_selection',
    ),
    Action(
        id='actual_size',
        text=_('Actual Si&ze (1:1)'),
        shortcuts=['3'],
        callback='on_action_actual_size',
    ),
    Action(
        id='reset_scale',
        text=_('Reset &Scale'),
        callback='on_action_reset_scale',
        group='active_when_selection',
    ),
    Action(
        id='reset_rotation',
        text=_('Reset &Rotation'),
        callback='on_action_reset_rotation',
        group='active_when_selection',
    ),
    Action(
        id='reset_flip',
        text=_('Reset &Flip'),
        callback='on_action_reset_flip',
        group='active_when_selection',
    ),
    Action(
        id='reset_crop',
        text=_('Reset Cro&p'),
        callback='on_action_reset_crop',
        group='active_when_selection',
    ),
    Action(
        id='reset_transforms',
        text=_('Reset &All'),
        shortcuts=['R'],
        callback='on_action_reset_transforms',
        group='active_when_selection',
    ),
    Action(
        id='select_all',
        text=_('&Select All'),
        shortcuts=['Ctrl+A'],
        callback='on_action_select_all',
    ),
    Action(
        id='deselect_all',
        text=_('Deselect &All'),
        shortcuts=['Ctrl+Shift+A'],
        callback='on_action_deselect_all',
    ),
    Action(
        id='help',
        text=_('&Help'),
        shortcuts=['F1', 'Ctrl+H'],
        callback='on_action_help',
    ),
    Action(
        id='about',
        text=_('&About'),
        callback='on_action_about',
    ),
    Action(
        id='debuglog',
        text=_('Show &Debug Log'),
        callback='on_action_debuglog',
    ),
    Action(
        id='show_scrollbars',
        text=_('Show &Scrollbars'),
        checkable=True,
        settings='View/show_scrollbars',
        callback='on_action_show_scrollbars',
    ),
    Action(
        id='show_menubar',
        # 默认不显示菜单栏（用户要求，2026-10-01）。菜单里的动作全都在：
        # 快捷键照旧，右键菜单里也有同一套（`menu_structure` 是共用的）。
        # 需要菜单栏时把设置 `View/show_menubar` 改成 true 重启即可。
        checked=False,
        text=_('Show &Menu Bar'),
        checkable=True,
        settings='View/show_menubar',
        callback='on_action_show_menubar',
    ),
    Action(
        id='show_titlebar',
        text=_('Show &Title Bar'),
        checkable=True,
        checked=True,
        callback='on_action_show_titlebar',
    ),
    Action(
        id='move_window',
        text=_('Move &Window'),
        shortcuts=['Ctrl+M'],
        callback='on_action_move_window',
    ),
    Action(
        id='fullscreen',
        text=_('&Fullscreen'),
        shortcuts=['F11'],
        checkable=True,
        callback='on_action_fullscreen',
    ),
    Action(
        id='always_on_top',
        text=_('&Always On Top'),
        checkable=True,
        callback='on_action_always_on_top',
    ),
    Action(
        id='settings',
        text=_('&Settings'),
        callback='on_action_settings',
    ),
    Action(
        id='keyboard_settings',
        text=_('&Keyboard && Mouse'),
        callback='on_action_keyboard_settings',
    ),
    Action(
        id='open_settings_dir',
        text=_('&Open Settings Folder'),
        callback='on_action_open_settings_dir',
    ),
    Action(
        id='toggle_side_panel',
        text=_('Toggle &Metadata Panel'),
        shortcuts=['Ctrl+Shift+M'],
        checkable=True,
        callback='on_action_toggle_side_panel',
    ),
    Action(
        id='edit_categories',
        text=_('Assign &Categories...'),
        callback='on_action_edit_categories',
        group='active_when_selection',
    ),
    Action(
        id='edit_notes',
        text=_('Edit &Notes'),
        shortcuts=['Ctrl+Shift+N'],
        callback='on_action_edit_notes',
        group='active_when_selection',
    ),
    Action(
        id='clear_filter',
        text=_('Clear All &Filters'),
        shortcuts=['Ctrl+Shift+F'],
        callback='on_action_clear_filter',
    ),
])
