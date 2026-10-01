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

"""The tag manager: one place to shape the tag tree of a project.

The sidebar answers "which material do I want to see", this dialog answers
"how should my tags be organised".  It offers the tree itself (drag a tag
onto another one to nest it), renaming, deleting, synonyms that fold two
names into one, implied parents for tags that are not nested, and
importing a tag tree from plain text.

Every edit goes through :class:`TagEdit`, so all of it is undoable and the
tags stay plain strings on the items.
"""

import logging

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from PyQt6.QtCore import Qt

from prism import tags
from prism.i18n import _
from prism.widgets import components
from prism.widgets.components.icons import icon

logger = logging.getLogger(__name__)

#: Emitted after anything changed the tags of the project, so the sidebar
#: and the inspector can refresh without knowing who made the change.
_TAG_SIGNALS = None


class _TagSignals(QtCore.QObject):
    changed = QtCore.pyqtSignal()


def tag_signals():
    global _TAG_SIGNALS
    if _TAG_SIGNALS is None:
        _TAG_SIGNALS = _TagSignals()
    return _TAG_SIGNALS


def notify_tag_change(view):
    """Tell the rest of the window that the tags changed."""
    if view is not None and hasattr(view, 'mark_content_dirty'):
        view.mark_content_dirty()
    tag_signals().changed.emit()


# ── The tree of one project ───────────────────────────────────────────

def project_tag_nodes(scene):
    """The project's tags as one tree: catalogue, assets and pages.

    The sidebar and the tag manager show the same tree, so this is the
    one place that assembles it.  ``count`` is what the row displays - how
    many things carry the tag - while ``asset`` keeps the part of it that
    is assets, which is what tells a pure page tag apart.
    """
    asset_counts = tags.tag_names_of_items(scene)
    totals = dict(asset_counts)
    for page in getattr(scene, 'workspace_pages', []) or []:
        for name in page.get('tags', []) or []:
            totals[name] = totals.get(name, 0) + 1
    return tags.build_tree(tags.scene_tag_names(scene), totals,
                           tags.tag_system(scene), asset_counts=asset_counts)


# ── Rows ──────────────────────────────────────────────────────────────

#: How deep a row is indented at most.  Deeper tags stay in the tree,
#: they just stop being pushed further right: indentation is width the
#: tag name needs, and a deep enough tree pushed the name out of the
#: sidebar completely.
MAX_TAG_INDENT_LEVELS = 2


def tag_indent(depth):
    """Four spaces per level, up to :data:`MAX_TAG_INDENT_LEVELS`."""
    level = max(0, min(int(depth or 0), MAX_TAG_INDENT_LEVELS))
    return '    ' * (level + 1)


def page_only(node):
    """Whether only pages carry this tag.

    Such a row is worth showing - the pages are organised with it - but
    no asset will ever match it, and the sidebar says so instead of
    leaving the user with an empty board and no explanation.
    """
    return bool(node.direct) and not getattr(node, 'asset', 0)


def tag_tooltip(node):
    """Hover text for a tag row: full path, other names, implied parents."""
    text = [node.name]
    if node.aliases:
        text.append(_('also called: {names}').format(
            names='、'.join(node.aliases)))
    if page_only(node):
        text.append(_('Only pages wear this tag - no asset can match it'))
    elif node.virtual and node.children:
        text.append(_('no item carries this tag itself'))
    if node.count:
        text.append(_('{count} items in total, {direct} with this tag')
                    .format(count=node.count, direct=node.direct))
    return '\n'.join(text)


def row_size(list_widget, text, height=None):
    """A row at least as wide as its own text.

    Qt elides a row narrower than its text, and that is how a nested or
    simply long tag became unreadable: the name was cut off with an
    ellipsis.  The row is widened instead - the list scrolls sideways
    when it has to - and never falls below the width of the viewport, so
    the highlight still fills a wide sidebar.
    """
    metrics = list_widget.fontMetrics()
    width = metrics.horizontalAdvance(text) + 24
    viewport = list_widget.viewport().width()
    return QtCore.QSize(int(max(width, viewport)),
                        int(height or metrics.height() + 12))


def make_tag_row(node, checked=None, list_widget=None):
    """One list row for a tag node, indented to its depth.

    ``checked`` adds a checkbox, which is how the sidebar turns a row into
    a filter.  The manager leaves it out: there it is about the structure,
    not about the current view.  ``list_widget`` sizes the row so that no
    part of the name is elided.
    """
    item = QtWidgets.QListWidgetItem(tag_row_text(node))
    item.setData(Qt.ItemDataRole.UserRole, {
        'type': 'tag', 'name': node.name, 'section': 'tag',
        'aliases': list(node.aliases), 'count': node.count})
    item.setToolTip(tag_tooltip(node))
    if checked is not None:
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(Qt.CheckState.Checked if checked
                           else Qt.CheckState.Unchecked)
    if node.color:
        item.setForeground(QtGui.QColor(node.color))
    if list_widget is not None:
        item.setSizeHint(row_size(list_widget, item.text()))
    return item


def tag_row_text(node):
    """The label of a tag row: indentation, name, other names and count."""
    label = tag_indent(node.depth) + node.label
    if node.aliases:
        label += ' (' + '、'.join(node.aliases) + ')'
    if page_only(node):
        label += _(' (page only)')
    return f'{label}   ({node.count})'


class TagDropList(QtWidgets.QListWidget):
    """A list whose tag rows can be dragged onto each other to nest them.

    Only tag rows take part: the sidebar shares this widget with the
    canvas, mind map and document rows, and moving one of those onto a tag
    would be meaningless.  The panel rebuilds the list after a drop, so
    Qt's own reordering is deliberately not used.
    """

    tag_dropped = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._dragging = None
        # 拖拽嵌套去掉了（用户要求标签是平铺的）。原来这里把拖拽、
        # 接受放下、InternalMove 都打开了 —— 那是嵌套功能的引擎。
        # 全关掉：列表照常点选，只是拖不动、也不接受别人放进来。
        self.setDragEnabled(False)
        self.setAcceptDrops(False)
        self.setDropIndicatorShown(False)
        self.setDragDropMode(
            QtWidgets.QAbstractItemView.DragDropMode.NoDragDrop)

    @staticmethod
    def _data(item):
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    def _tag_at(self, point):
        data = self._data(self.itemAt(point))
        if data and data.get('type') == 'tag':
            return data.get('name')
        return None

    def startDrag(self, supported_actions):
        # 拖拽嵌套标签的功能去掉了（用户要求不再有子标签）。
        # 这里直接不启动拖拽 —— 列表本身照常可用，只是拖不动。
        return

    def dragMoveEvent(self, event):
        event.ignore()

    def dropEvent(self, event):
        event.ignore()


def can_nest(tag, new_parent):
    """Whether ``tag`` may be nested under ``new_parent``.

    A tag cannot become its own parent, and a tag cannot be moved below
    one of its own children - that would cut the branch off the tree.
    """
    if not tag or tag == new_parent:
        return False
    if new_parent and tags.is_descendant(new_parent, tag):
        return False
    if new_parent and tags.leaf(tag) == tags.leaf(new_parent):
        return False
    return True


# ── Undoable edits ────────────────────────────────────────────────────

def rename_prefix(name, old, new):
    """Rewrite ``old`` (and everything below it) to ``new``."""
    if name == old:
        return new
    if tags.is_descendant(name, old):
        tail = tags.split_path(name)[len(tags.split_path(old)):]
        return tags.join_path(tags.split_path(new) + tail)
    return name


class TagEdit(QtGui.QUndoCommand):
    """One undoable change to the project's tags.

    It stores the catalogue, the mapping table and the tags of the touched
    items before and after, so undo puts all three back - renaming a tag
    moves it on the items, in the catalogue and in the synonyms at once.
    """

    def __init__(self, view, text, names=None, table=None, retags=None):
        super().__init__(text)
        self.view = view
        self.scene = view.scene if view is not None else None
        self.before_names = self._catalogue()
        self.after_names = list(names) if names is not None else self.before_names
        system = tags.tag_system(self.scene) if self.scene is not None else None
        self.before_table = system.to_dict() if system is not None else None
        self.after_table = dict(table) if table is not None else self.before_table
        self.items = list(retags or [])
        self.before_tags = [list(getattr(item, '_tags', []))
                            for item in self.items]
        self.after_tags = [sorted(retags[item]) for item in self.items]

    def _catalogue(self):
        if self.scene is None:
            return []
        return list(getattr(self.scene, 'tag_names', []) or [])

    def _apply(self, names, table, values):
        if self.scene is None or sip.isdeleted(self.scene):
            return
        self.scene.tag_names = list(names)
        if table is not None:
            self.scene.tag_system = tags.TagSystem.from_dict(table)
        for item, value in zip(self.items, values):
            item._tags = list(value)
        if self.items and hasattr(self.scene, 'metadata_changed'):
            self.scene.metadata_changed.emit()
        notify_tag_change(self.view)

    def redo(self):
        self._apply(self.after_names, self.after_table, self.after_tags)

    def undo(self):
        self._apply(self.before_names, self.before_table, self.before_tags)


def _catalogue_with(scene, names):
    """The catalogue plus the names that are not in it yet."""
    catalogue = list(getattr(scene, 'tag_names', []) or [])
    for name in names:
        name = str(name).strip()
        if name and name not in catalogue:
            catalogue.append(name)
    return catalogue


def _retagged_items(scene, rewrite):
    """Items whose tags change under ``rewrite``, as item -> new tag set."""
    changed = {}
    for item in scene.items_for_save():
        old = [str(name) for name in getattr(item, '_tags', []) or []]
        if not old:
            continue
        new = sorted({rewrite(name) for name in old} - {''})
        if new != sorted(old):
            changed[item] = new
    return changed


def add_tags(view, names, text=None):
    """Add tag names to the catalogue without touching any item."""
    scene = view.scene
    fresh = [name for name in names if name
             and name not in (getattr(scene, 'tag_names', []) or [])]
    if not fresh:
        return None
    command = TagEdit(view, text or _('Add tags'),
                      names=_catalogue_with(scene, fresh))
    scene.undo_stack.push(command)
    return command


def rename_tag(view, old, new, new_parent=None):
    """Rename or re-nest one tag, children and mappings included.

    ``new`` is the new leaf name and ``new_parent`` the path it should sit
    under; leaving ``new_parent`` out keeps the current one.
    """
    scene = view.scene
    old = str(old).strip()
    leaf = str(new).strip()
    if not old or not leaf:
        return None
    parent = tags.split_path(old)[:-1] if new_parent is None \
        else tags.split_path(new_parent)
    target = tags.join_path(parent + [leaf])
    if target == old:
        return None
    catalogue = list(getattr(scene, 'tag_names', []) or [])
    if target in catalogue and target not in (old,):
        return None
    if tags.is_descendant(target, old):
        return None

    def rewrite(name):
        return rename_prefix(name, old, target)

    changed = _retagged_items(scene, rewrite)
    names = {rewrite(name) for name in catalogue}
    names |= {tag for item in changed for tag in changed[item]}
    system = tags.tag_system(scene).copy()
    system.rename_prefix(old, target)
    command = TagEdit(view, _('Rename tag'), names=sorted(
        names, key=lambda n: [seg.casefold() for seg in tags.split_path(n)]),
        table=system.to_dict(), retags=changed)
    scene.undo_stack.push(command)
    return command


def delete_tag(view, name, keep_children=False):
    """Delete a tag, its children, its mappings and its use on items.

    With ``keep_children`` the tags below it survive and move up to the
    root instead of disappearing with it.
    """
    scene = view.scene
    name = str(name).strip()
    catalogue = list(getattr(scene, 'tag_names', []) or [])
    if not name:
        return None
    doomed = {name} if keep_children else {name} | set(
        tags.descendants(catalogue, name))
    system = tags.tag_system(scene).copy()

    def rewrite(tag):
        if tag in doomed:
            return ''
        if keep_children and tags.is_descendant(tag, name):
            tail = tags.split_path(tag)[len(tags.split_path(name)):]
            return tags.join_path(tail)
        return tag

    changed = _retagged_items(scene, rewrite)
    names = {rewrite(entry) for entry in catalogue} - {''}
    names |= {tag for item in changed for tag in changed[item]}
    for alias, canonical in list(system.siblings().items()):
        if alias in doomed or canonical in doomed:
            system.remove_sibling(alias)
    for child, parents in list(system.parents().items()):
        for parent in parents:
            if child in doomed or parent in doomed:
                system.remove_parent(child, parent)
    command = TagEdit(view, _('Delete tag'), names=sorted(
        names, key=lambda n: [seg.casefold() for seg in tags.split_path(n)]),
        table=system.to_dict(), retags=changed)
    scene.undo_stack.push(command)
    return command


def set_synonym(view, alias, canonical):
    """Treat ``alias`` as another name for ``canonical``."""
    return _edit_table(view, _('Set synonym'), [alias, canonical],
                       lambda system: system.add_sibling(alias, canonical))


def remove_synonym(view, alias):
    return _edit_table(view, _('Remove synonym'), None,
                       lambda system: system.remove_sibling(alias))


def set_parent(view, child, parent):
    """Tagging ``child`` also counts as tagging ``parent``."""
    return _edit_table(view, _('Set implied parent'), [child, parent],
                       lambda system: system.add_parent(child, parent))


def remove_parent(view, child, parent):
    return _edit_table(view, _('Remove implied parent'), None,
                       lambda system: system.remove_parent(child, parent))


def _edit_table(view, text, names, change):
    scene = view.scene
    system = tags.tag_system(scene).copy()
    if not change(system):
        return None
    command = TagEdit(view, text,
                      names=_catalogue_with(scene, names or []),
                      table=system.to_dict())
    scene.undo_stack.push(command)
    return command


# ── Asking the user for an edit ───────────────────────────────────────
#
# The sidebar and the manager offer the same handful of operations, so the
# questions live here and both call them.

def pick_tag(parent, candidates, title, label):
    """Let the user pick an existing tag or type a new name."""
    choice, ok = QtWidgets.QInputDialog.getItem(
        parent, title, label, list(candidates), 0, True)
    return choice.strip() if ok and choice.strip() else None


def ask_create_tag(view, parent=None, parent_tag=None):
    """Ask for a tag name and add it to the catalogue."""
    title = _('New tag')
    label = _('Tag name (use / for a level, : for a namespace):')
    if parent_tag:
        title = _('New child tag')
        label = _('Name below “{parent}”:').format(parent=parent_tag)
    name, ok = QtWidgets.QInputDialog.getText(parent, title, label)
    if not ok or not name.strip():
        return None
    full = tags.join_path(tags.split_path(parent_tag or '')
                          + tags.split_path(name))
    if add_tags(view, [full]) is None:
        QtWidgets.QMessageBox.information(
            parent, _('Tags'), _('“{name}” already exists.').format(name=full))
        return None
    return full


def ask_rename_tag(view, name, parent=None):
    """Ask for a new name, keeping the tag where it is."""
    new, ok = QtWidgets.QInputDialog.getText(
        parent, _('Rename tag'), _('New name:'), text=tags.leaf(name))
    new = str(new or '').strip()
    if not ok or not new or new == tags.leaf(name):
        return None
    if rename_tag(view, name, new) is None:
        QtWidgets.QMessageBox.warning(
            parent, _('Tags'), _('“{name}” already exists.').format(name=new))
        return None
    return tags.join_path(tags.split_path(name)[:-1] + [new])


def ask_move_tag(view, name, parent=None):
    """Ask which tag should become the parent of this one."""
    if not name:
        return None
    choices = [_('Top level')] + [other for other
                                  in tags.scene_tag_names(view.scene)
                                  if can_nest(name, other)]
    choice, ok = QtWidgets.QInputDialog.getItem(
        parent, _('Change level'), _('Move “{name}” under:').format(name=name),
        choices, 0, False)
    if not ok or not choice:
        return None
    new_parent = '' if choice == choices[0] else choice
    if rename_tag(view, name, tags.leaf(name), new_parent=new_parent) is None:
        QtWidgets.QMessageBox.warning(
            parent, _('Tags'), _('“{name}” is already there.')
            .format(name=choice))
        return None
    return tags.move_path(name, new_parent)


def ask_delete_tag(view, name, parent=None):
    """Ask before deleting a tag, its children and its use on items."""
    if not name:
        return False
    children = tags.descendants(tags.scene_tag_names(view.scene), name)
    question = _('Delete “{name}” and remove it from every item?') \
        .format(name=name)
    if children:
        question += '\n' + _('Its {count} child tags move up to the top '
                             'level.').format(count=len(children))
    answer = QtWidgets.QMessageBox.question(parent, _('Delete tag'), question)
    if answer != QtWidgets.QMessageBox.StandardButton.Yes:
        return False
    delete_tag(view, name, keep_children=bool(children))
    return True


def ask_set_synonym(view, name, parent=None):
    """Ask which other tag means the same as this one."""
    if not name:
        return None
    system = tags.tag_system(view.scene)
    candidates = [other for other in tags.scene_tag_names(view.scene)
                  if other != name and other not in system.group(name)]
    alias = pick_tag(parent, candidates, _('Set synonym'),
                     _('Which tag means the same as “{name}”?')
                     .format(name=name))
    if not alias or not set_synonym(view, alias, name):
        return None
    return alias


def ask_set_parent(view, name, parent=None):
    """Ask which other tag this one should imply."""
    if not name:
        return None
    system = tags.tag_system(view.scene)
    candidates = [other for other in tags.scene_tag_names(view.scene)
                  if other != name
                  and other not in system.implied_parents(name)]
    implied = pick_tag(parent, candidates, _('Add implied parent'),
                       _('Tagging “{name}” should also mean:')
                       .format(name=name))
    if not implied or not set_parent(view, name, implied):
        return None
    return implied


# ── The dialog ────────────────────────────────────────────────────────

class TagPanel(QtWidgets.QDialog):
    """Manage the tag tree, its synonyms and its implied parents."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.scene = view.scene
        self._current = None
        self._rows = {}
        self._building = False
        self.setWindowTitle(_('Manage tags'))
        self.resize(880, 620)
        self._setup_ui()
        self._connect_scene()
        self.tag_signals_connection = tag_signals().changed.connect(
            self._refresh)
        self._refresh()

    def _setup_ui(self):
        self.setObjectName('tagManagerDialog')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        toolbar = QtWidgets.QHBoxLayout()
        self._buttons = {}
        # 「新建子标签」「改变层级」「导入标签树」「导出标签树」都不在这儿了
        # —— 用户要求去掉标签的层级功能（见下方 _build_tree_side 里的说明）。
        # 底层的 tags.py 一行没动：旧工程里已经存在的 `古风/山水` 这类
        # 两级标签原样保留、照样能筛选和改名，只是不能再新建层级。
        for key, text, slot in (
                ('new', _('New tag'), self.create_tag),
                ('rename', _('Rename'), self.rename_current),
                ('delete', _('Delete'), self.delete_current)):
            button = QtWidgets.QPushButton(text)
            button.setIcon(icon({'new': 'plus', 'rename': 'rename',
                                 'delete': 'trash'}[key]))
            button.setIconSize(QtCore.QSize(16, 16))
            button.clicked.connect(slot)
            toolbar.addWidget(button)
            self._buttons[key] = button
        toolbar.addStretch()
        layout.addLayout(toolbar)

        # Which object a tag hangs on is the first thing to get right
        # here: a tag can be on assets and on pages at the same time, but
        # only the assets side takes part in filtering.
        self._scope = QtWidgets.QLabel(
            _('This manages the tag catalogue: a tag can hang on assets '
              'and on pages, but only assets take part in the tag filter.'))
        self._scope.setWordWrap(True)
        self._scope.setProperty('uiRole', 'secondary')
        layout.addWidget(self._scope)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_tree_side())
        splitter.addWidget(self._build_detail_side())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        self._hint = QtWidgets.QLabel()
        self._hint.setWordWrap(True)
        self._hint.setProperty('uiRole', 'secondary')
        layout.addWidget(self._hint)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _build_tree_side(self):
        side = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        self._search = components.LineEdit()
        self._search.setPlaceholderText(_('Search tags'))
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._filter_rows)
        box.addWidget(self._search)
        self._tree = TagDropList()
        self._tree.currentItemChanged.connect(self._on_row_selected)
        # （原来这里把 tag_dropped 连到 _on_tag_dropped 做嵌套。拖拽嵌套
        # 已经去掉了，那个方法也删了，所以不再连。）
        box.addWidget(self._tree, 1)
        hint = QtWidgets.QLabel(
            _('Tags are a flat list. Selecting one shows what it is '
              'attached to.'))
        hint.setWordWrap(True)
        hint.setProperty('uiRole', 'secondarySmall')
        box.addWidget(hint)
        return side

    def _build_detail_side(self):
        side = QtWidgets.QWidget()
        box = QtWidgets.QVBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(8)

        self._title = QtWidgets.QLabel()
        self._title.setProperty('uiRole', 'inspectorTitle')
        box.addWidget(self._title)
        self._stats = QtWidgets.QLabel()
        self._stats.setWordWrap(True)
        self._stats.setProperty('uiRole', 'secondary')
        box.addWidget(self._stats)

        group = QtWidgets.QGroupBox(_('Synonyms (the same tag under another name)'))
        synonyms = QtWidgets.QVBoxLayout(group)
        self._aliases = QtWidgets.QListWidget()
        self._aliases.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
        synonyms.addWidget(self._aliases)
        row = QtWidgets.QHBoxLayout()
        self._add_alias = QtWidgets.QPushButton(_('Add…'))
        self._add_alias.setIcon(icon('plus'))
        self._add_alias.clicked.connect(self.add_synonym)
        self._remove_alias = QtWidgets.QPushButton(_('Remove'))
        self._remove_alias.setIcon(icon('trash'))
        self._remove_alias.clicked.connect(self.remove_synonym)
        row.addWidget(self._add_alias)
        row.addWidget(self._remove_alias)
        row.addStretch()
        synonyms.addLayout(row)
        box.addWidget(group)

        # 「隐含父级」那块去掉了 —— 它和「子标签」是层级功能的两个方向
        # （"A 隐含 B" 等于 "B 是 A 的父亲"）。留着它就没法真的去掉层级：
        # 用户还能通过它建出父子关系，只是绕了个说法。
        # 底层 tags.py 的 implied-parents 数据不动，旧工程照常能筛选。
        return side

    def _connect_scene(self):
        if hasattr(self.scene, 'metadata_changed'):
            self.scene.metadata_changed.connect(self._refresh)

    # ── Building the tree ─────────────────────────────────────────────

    def _alive(self):
        return self.scene is not None and not sip.isdeleted(self.scene)

    def _refresh(self):
        if self._building or not self._alive():
            return
        self._building = True
        try:
            self._rebuild_rows()
            self._filter_rows(self._search.text())
            self._show_current()
        finally:
            self._building = False

    def _tree_nodes(self):
        return project_tag_nodes(self.scene)

    def _rebuild_rows(self):
        selected = self._current
        self._tree.blockSignals(True)
        self._tree.clear()
        self._rows = {}
        for node in self._tree_nodes():
            row = make_tag_row(node, list_widget=self._tree)
            self._tree.addItem(row)
            self._rows[node.name] = row
        self._tree.blockSignals(False)
        self._reselect(selected)

    def _reselect(self, name):
        row = self._rows.get(name)
        if row is None and self._rows:
            row = next(iter(self._rows.values()))
        if row is not None:
            self._tree.setCurrentItem(row)
        else:
            self._current = None

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_rows()

    def _fit_rows(self):
        """Keep every row wide enough for its own text.

        A width that was right when the rows were built can be too narrow
        a moment later - the dialog is resizable.
        """
        for row in getattr(self, '_rows', {}).values():
            row.setSizeHint(row_size(self._tree, row.text()))

    def _filter_rows(self, text):
        query = str(text or '').strip().casefold()
        for name, row in self._rows.items():
            row.setHidden(bool(query) and query not in name.casefold()
                          and query not in name.split('/')[-1].casefold())

    def _on_row_selected(self, current, previous=None):
        data = current.data(Qt.ItemDataRole.UserRole) if current else None
        self._current = data.get('name') if data else None
        if not self._building:
            self._show_current()

    def _show_current(self):
        name = self._current
        system = tags.tag_system(self.scene)
        has_tag = bool(name)
        # 按钮表里只剩 new / rename / delete 了（child 和 move 去掉了），
        # 所以这里也不该再找它们。
        for key in ('rename', 'delete'):
            self._buttons[key].setEnabled(has_tag)
        self._tree.setDragEnabled(False)
        self._aliases.clear()
        if not has_tag:
            self._title.setText(_('Select a tag on the left'))
            self._stats.setText('')
            for widget in (self._aliases, self._add_alias,
                           self._remove_alias):
                widget.setEnabled(False)
            return
        for widget in (self._aliases, self._add_alias, self._remove_alias):
            widget.setEnabled(True)
        node = next((node for node in self._tree_nodes()
                     if node.name == name), None)
        title = name
        if node is not None and node.namespace:
            title += _('  ·  namespace {name}').format(name=node.namespace)
        self._title.setText(title)
        if node is not None:
            self._stats.setText(
                _('{total} items in total, {direct} of them carry this tag '
                  'directly.').format(total=node.count, direct=node.direct))
        for alias in system.aliases(name):
            self._aliases.addItem(alias)

    # ── Actions ───────────────────────────────────────────────────────

    def create_tag(self):
        name = ask_create_tag(self.view, self)
        if name:
            self._select(name)

    def rename_current(self):
        if not self._current:
            return
        new = ask_rename_tag(self.view, self._current, self)
        if new:
            self._select(new)

    def delete_current(self):
        if not self._current:
            return
        if ask_delete_tag(self.view, self._current, self):
            self._current = None
            self._refresh()

    def add_synonym(self):
        if self._current:
            ask_set_synonym(self.view, self._current, self)

    def remove_synonym(self):
        row = self._aliases.currentItem()
        if row is not None and self._current:
            remove_synonym(self.view, row.text())

    def _select(self, name):
        self._current = name
        self._refresh()

    def done(self, result):
        try:
            tag_signals().changed.disconnect(self.tag_signals_connection)
        except (TypeError, RuntimeError):
            pass
        super().done(result)
