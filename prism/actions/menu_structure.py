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

from prism.i18n import _

MENU_SEPARATOR = 0

menu_structure = [
    {
        'menu': _('&File'),
        'items': [
            'new_scene',
            'open',
            {
                'menu': _('Open &Recent'),
                'items': '_build_recent_files',
            },
            MENU_SEPARATOR,
            'save',
            'save_as',
            'export_images',
            'import_document',
            'import_mindmap',
            'clipboard_inbox',
            MENU_SEPARATOR,
            'export_note_word',
            'export_mindmap',
            MENU_SEPARATOR,
            'quit',
        ],
    },
    {
        'menu': _('&Edit'),
        'items': [
            'undo',
            'redo',
            MENU_SEPARATOR,
            'select_all',
            'deselect_all',
            MENU_SEPARATOR,
            'group_selection',
            'ungroup_selection',
            'edit_group_note',
            MENU_SEPARATOR,
            'cut',
            'copy',
            'paste',
            'delete',
            MENU_SEPARATOR,
            'raise_to_top',
            'lower_to_bottom',
        ],
    },
    {
        'menu': _('&View'),
        'items': [
            'fit_scene',
            'fit_selection',
            'actual_size',
            MENU_SEPARATOR,
            'fullscreen',
            'always_on_top',
            'show_scrollbars',
            'show_menubar',
            MENU_SEPARATOR,
            'move_window',
        ],
    },
    {
        'menu': _('&Insert'),
        'items': [
            'insert_images',
            'insert_text',
        ],
    },
    {
        'menu': _('&Transform'),
        'items': [
            'crop',
            'flip_horizontally',
            'flip_vertically',
            MENU_SEPARATOR,
            'reset_scale',
            'reset_rotation',
            'reset_flip',
            'reset_crop',
            'reset_transforms',
        ],
    },
    {
        'menu': _('&Arrange'),
        'items': [
            'arrange_optimal',
            'arrange_horizontal',
            'arrange_vertical',
            'arrange_square',
        ],
    },
    {
        'menu': _('&Images'),
        'items': [
            'change_opacity',
            'grayscale',
            MENU_SEPARATOR,
            'sample_color',
            MENU_SEPARATOR,
            'find_duplicates',
        ],
    },
    {
        'menu': _('&Settings'),
        'items': [
            'settings',
            'keyboard_settings',
            'open_settings_dir',
        ],
    },
    {
        'menu': _('&Help'),
        'items': [
            'help',
            'about',
            'debuglog',
        ],
    },
]
