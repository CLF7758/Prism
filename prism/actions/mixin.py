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

from collections import defaultdict
from functools import partial
import os.path

from PyQt6 import QtCore, QtGui, QtWidgets

from .actions import Action, actions
from .menu_structure import menu_structure, MENU_SEPARATOR
from prism.i18n import _


#: 这些动作动的是**画布上的东西**：选区、图元、画布视图、画布自己的撤销栈。
#: 它们的快捷键必须跟着画布走。
#:
#: 做成窗口级会有两个后果：网页面板（脑图、文档编辑器）拿到焦点时这些按键
#: 照样会响 —— 脑图里 Ctrl+Z 撤的是画布、Ctrl+C 复制的是画布、Del 删的是
#: 画布上的图元，而且在节点名里打一个 "v" 会把画布上的图元上下翻转；
#: 更糟的是 Qt 先匹配快捷键，**焦点控件根本收不到那个按键**，面板自己的
#: 快捷键（脑图的 Del / Ctrl+Z / Ctrl+C）就永远不触发。
#:
#: 故意**不在**名单里的：select_all / deselect_all / fit_scene /
#: fit_selection / arrange_optimal —— 这几个动作在素材浏览器页是**同一个
#: 动作**（``view._material_canvas()`` 会把它们转给浏览器的画布），收窄了
#: 浏览器就没有 Ctrl+A 了。脑图因此还拿不到 Ctrl+A，这是已知缺口，
#: 要补得让素材浏览器自己拥有一个 Ctrl+A 快捷键，不在这次范围里。
CANVAS_SCOPED_ACTIONS = frozenset((
    'undo', 'redo',
    'copy', 'cut', 'paste', 'delete',
    'insert_text', 'insert_images',
    'grayscale', 'crop',
    'flip_horizontally', 'flip_vertically', 'reset_transforms',
    'group_selection', 'ungroup_selection', 'edit_notes',
    'actual_size',
    'export_scene',
))


class ActionsMixin:

    def actiongroup_set_enabled(self, group, value):
        for action in self.prism_actiongroups[group]:
            blocked = getattr(self, '_material_blocked_actions', {})
            if action in blocked:
                blocked[action] = value
            else:
                action.setEnabled(value)

    def set_material_navigation_active(self, active):
        """Keep canvas shortcuts from editing a hidden canvas in the library."""
        blocked = getattr(self, '_material_blocked_actions', {})
        if active:
            if blocked:
                return
            canvas_groups = {
                'active_when_selection', 'active_when_single_image',
                'active_when_items_in_scene', 'active_when_can_undo',
                'active_when_can_redo',
            }
            canvas_ids = {
                'paste', 'select_all', 'deselect_all', 'insert_images',
                'insert_text', 'fit_scene', 'actual_size',
            }
            for definition in actions.values():
                action = definition.qaction
                if (definition.id=='fit_scene' and action is not None
                        and action.parent() is self):
                    # Reset delegates to the displayed aggregate canvas; it
                    # is safe even while scene-editing shortcuts are blocked.
                    # The keyboard shortcut itself now belongs to the canvas
                    # (see CANVAS_SCOPED_ACTIONS), but the menu entry stays
                    # usable from here.
                    blocked[action]=action.isEnabled()
                    action.setEnabled(True)
                    continue
                if action is not None and action.parent() is self and (
                        definition.group in canvas_groups
                        or definition.id in canvas_ids):
                    blocked[action] = action.isEnabled()
                    action.setEnabled(False)
            self._material_blocked_actions = blocked
        else:
            self._material_blocked_actions = {}
            for action, enabled in blocked.items():
                action.setEnabled(enabled)

    def build_menu_and_actions(self):
        """Creates a new menu or rebuilds the given menu."""
        self.context_menu = QtWidgets.QMenu(self)
        self.toplevel_menus = []
        self.prism_actiongroups = defaultdict(list)
        self._post_create_functions = []
        self._create_actions()
        self._create_menu(self.context_menu, menu_structure)
        for func, arg in self._post_create_functions:
            func(arg)
        del self._post_create_functions

    def update_menu_and_actions(self):
        self._build_recent_files()

    def create_menubar(self):
        menu_bar = QtWidgets.QMenuBar()
        for menu in self.toplevel_menus:
            menu_bar.addMenu(menu)
        return menu_bar

    def _store_checkable_setting(self, key, value):
        self.settings.setValue(key, value)

    def _init_action_checkable(self, actiondef, qaction):
        qaction.setCheckable(True)
        callback = getattr(self, actiondef.callback)
        qaction.toggled.connect(callback)
        settings_key = actiondef.settings
        checked = actiondef.checked
        qaction.setChecked(checked)
        if settings_key:
            val = self.settings.value(settings_key, checked, type=bool)
            qaction.setChecked(val)
            self._post_create_functions.append((callback, val))
            qaction.toggled.connect(
                partial(self._store_checkable_setting, settings_key))

    def _create_actions(self):
        for action in actions.values():
            qaction = QtGui.QAction(action.text, self)
            qaction.setAutoRepeat(False)
            shortcuts = action.get_shortcuts()
            if shortcuts:
                qaction.setShortcuts(shortcuts)
            if action.checkable:
                self._init_action_checkable(action, qaction)
            else:
                qaction.triggered.connect(getattr(self, action.callback))
            self.addAction(qaction)
            if action.id in CANVAS_SCOPED_ACTIONS:
                # 画布的动作，画布自己扛：别的面板拿到焦点时，这些按键
                # 得留给那个面板（脑图的 Ctrl+Z、文档编辑器的 Ctrl+V）。
                qaction.setShortcutContext(QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
            qaction.setEnabled(action.enabled)
            if action.group:
                self.prism_actiongroups[action.group].append(qaction)
                qaction.setEnabled(False)
            action.qaction = qaction

    def _create_menu(self, menu, items):
        if isinstance(items, str):
            getattr(self, items)(menu)
            return menu
        for item in items:
            if isinstance(item, str):
                menu.addAction(actions[item].qaction)
            if item == MENU_SEPARATOR:
                menu.addSeparator()
            if isinstance(item, dict):
                submenu = menu.addMenu(item['menu'])
                if menu == self.context_menu:
                    self.toplevel_menus.append(submenu)
                self._create_menu(submenu, item['items'])

        return menu

    def _build_recent_files(self, menu=None):
        if menu:
            self._recent_files_submenu = menu
        self._clear_recent_files()

        files = self.settings.get_recent_files(existing_only=True)
        items = []

        for i in range(10):
            action_id = f'recent_files_{i}'
            key = 0 if i == 9 else i + 1
            # Definitions and keyboard settings outlive the dynamic menu rows.
            # Recreating ten Actions here performs repeated settings I/O and
            # also leaves their restore-defaults signal connections alive.
            action = actions.get(action_id)
            if action is None:
                action = Action(id=action_id,
                                menu_id='_build_recent_files',
                                text=_(f'File {i + 1}'),
                                shortcuts=[f'Ctrl+{key}'])
                actions[action_id] = action
            else:
                action.text = _(f'File {i + 1}')

            if i < len(files):
                filename = files[i]
                from prism.storage.entrypoint import display_project_name
                qaction = QtGui.QAction(display_project_name(filename), self)
                qaction.setToolTip(filename)
                qaction.setShortcuts(action.get_shortcuts())
                qaction.triggered.connect(
                    partial(self.on_action_open_recent_file, filename))
                self.addAction(qaction)
                action.qaction = qaction
                self._recent_files_submenu.addAction(qaction)
                items.append(action_id)

    def _clear_recent_files(self):
        for action in self._recent_files_submenu.actions():
            self.removeAction(action)
        self._recent_files_submenu.clear()
        for key in list(actions.keys()):
            if key.startswith('recent_files_'):
                actions[key].qaction = None
