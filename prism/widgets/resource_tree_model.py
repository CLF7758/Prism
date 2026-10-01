"""Qt model for the persistent folder/page hierarchy.

Unlike the legacy list sidebar, hierarchy is represented by QModelIndex
parents, never indentation characters in row text.  The model tracks the
service's stable IDs and emits row-local changes for ordinary edits.
"""

import uuid

from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt
from PyQt6.QtCore import QSize

from prism.workspace_service import TREE_SECTIONS
from prism import ui_tokens
from prism.widgets.components.icons import tinted_icon


UI_SECTIONS = TREE_SECTIONS + ('tag',)
BLANK_PREFIX = 'blank:'
#: 临时节点的 key 前缀。草稿行只在模型里存在 —— 名字确认之前服务层一个字节
#: 都没写过，所以按 Esc 取消不会留下任何痕迹（连"已修改"都不会标）。
DRAFT_PREFIX = 'draft:'
#: 拖放用的 MIME 类型。只在本应用内用（视图开的是 InternalMove），
#: 但如果哪天要和别的视图互换，这里也不用改。
NODE_MIME = 'application/x-prism-tree-nodes'
SECTION_TITLES = {'canvas': '画布', 'mindmap': '脑图',
                  'document': '文档', 'tag': '标签'}
NODE_ID_ROLE = Qt.ItemDataRole.UserRole + 1
SECTION_ROLE = Qt.ItemDataRole.UserRole + 2
NODE_TYPE_ROLE = Qt.ItemDataRole.UserRole + 3
TAG_NAME_ROLE = Qt.ItemDataRole.UserRole + 4


class ResourceTreeModel(QtCore.QAbstractItemModel):
    #: 一个新节点就地命名成功；参数是服务返回的节点字典。
    draft_committed = QtCore.pyqtSignal(object)
    #: 命名被拒（重名/非法字符）；参数是给用户看的原因。
    draft_refused = QtCore.pyqtSignal(str)

    def __init__(self, service, parent=None):
        super().__init__(parent)
        self.service = service
        self._nodes = {}
        self._children = {}
        self._key_to_token = {}
        self._token_to_key = {}
        self._draft = None
        self._tag_records = {}
        self._load()
        service.changed.connect(self._on_service_changed)

    def _load(self):
        self._draft = None
        self._nodes = {node['id']: dict(node) for node in self.service.nodes
                       if node.get('deletedAt') is None}
        self._nodes.update(self._tag_records)
        self._children = {None: list(UI_SECTIONS)}
        for section in UI_SECTIONS:
            self._children[section] = []
        for node in self._nodes.values():
            if node['nodeType'] == 'folder':
                self._children[node['id']] = []
        for node in self._nodes.values():
            parent = node.get('parentId') or node['section']
            if parent in self._children:
                self._children[parent].append(node['id'])
            else:
                self._children[parent] = [node['id']]
        for parent, ids in self._children.items():
            if parent is not None:
                ids.sort(key=lambda node_id: (
                    self._nodes[node_id]['order'], node_id))
        for section in UI_SECTIONS:
            self._children[section].append(BLANK_PREFIX + section)

    def _key(self, index):
        return self._token_to_key.get(index.internalId()) if index.isValid() else None

    def _make_index(self, row, key):
        token = self._key_to_token.get(key)
        if token is None:
            token = len(self._key_to_token) + 1
            self._key_to_token[key] = token
            self._token_to_key[token] = key
        return self.createIndex(row, 0, token)

    def _index_for(self, key):
        if key is None:
            return QtCore.QModelIndex()
        if key in UI_SECTIONS:
            return self._make_index(UI_SECTIONS.index(key), key)
        if key.startswith(BLANK_PREFIX):
            section = key[len(BLANK_PREFIX):]
            return self._make_index(self._children[section].index(key), key)
        if key.startswith(DRAFT_PREFIX):
            draft = self._draft or {}
            parent_key = draft.get('parentId') or draft.get('section')
            rows = self._children.get(parent_key, [])
            if key not in rows:
                return QtCore.QModelIndex()
            return self._make_index(rows.index(key), key)
        node = self._nodes.get(key)
        if node is None:
            return QtCore.QModelIndex()
        parent = node.get('parentId') or node['section']
        siblings = self._children.get(parent, [])
        if key not in siblings:
            return QtCore.QModelIndex()
        return self._make_index(siblings.index(key), key)

    def index(self, row, column, parent=QtCore.QModelIndex()):
        if column != 0 or row < 0 or parent.column() > 0:
            return QtCore.QModelIndex()
        children = self._children.get(self._key(parent), [])
        if row >= len(children):
            return QtCore.QModelIndex()
        return self._make_index(row, children[row])

    def parent(self, index):
        if not index.isValid():
            return QtCore.QModelIndex()
        key = self._key(index)
        if key in UI_SECTIONS:
            return QtCore.QModelIndex()
        if key.startswith(BLANK_PREFIX):
            return self._index_for(key[len(BLANK_PREFIX):])
        if key.startswith(DRAFT_PREFIX):
            draft = self._draft or {}
            return self._index_for(draft.get('parentId') or draft.get('section'))
        node = self._nodes.get(key)
        if node is None:
            return QtCore.QModelIndex()
        return self._index_for(node.get('parentId') or node['section'])

    def rowCount(self, parent=QtCore.QModelIndex()):
        if parent.column() > 0:
            return 0
        return len(self._children.get(self._key(parent), []))

    def columnCount(self, parent=QtCore.QModelIndex()):
        return 1

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        key = self._key(index)
        if key.startswith(BLANK_PREFIX):
            section = key[len(BLANK_PREFIX):]
            if role == SECTION_ROLE:
                return section
            if role == NODE_TYPE_ROLE:
                return 'blank'
            if role == Qt.ItemDataRole.ToolTipRole:
                return '右键创建页面或文件夹' if section != 'tag' else '右键创建标签'
            if role == Qt.ItemDataRole.SizeHintRole:
                height = 32
                return QSize(0, height)
            return None
        if key.startswith(DRAFT_PREFIX):
            draft = self._draft or {}
            if role in (Qt.ItemDataRole.DisplayRole,
                        Qt.ItemDataRole.EditRole):
                return draft.get('title', '')
            if role == SECTION_ROLE:
                return draft.get('section')
            if role == NODE_TYPE_ROLE:
                return draft.get('nodeType')
            if role == NODE_ID_ROLE:
                return None
            return None
        node = self._nodes.get(key)
        if role == Qt.ItemDataRole.DisplayRole:
            return SECTION_TITLES[key] if node is None else node['title']
        if role == Qt.ItemDataRole.SizeHintRole:
            return QSize(0, 32)
        if role == Qt.ItemDataRole.DecorationRole:
            name = key if node is None else ('folder' if node['nodeType'] == 'folder' else 'tag' if node['section'] == 'tag' else node['section'])
            colour = (ui_tokens.FOLDER_COLOURS.get((node or {}).get('colour', 'blue'), '#79AEFF')
                      if name == 'folder' else ui_tokens.SECTION_ICON_COLOURS.get(name, '#9EA5B1'))
            return tinted_icon(name, colour)
        if role == Qt.ItemDataRole.ForegroundRole and node is None:
            return QtGui.QColor(ui_tokens.COLOURS_DARK['text-secondary'])
        if role == Qt.ItemDataRole.FontRole and node is None:
            font = QtGui.QFont()
            font.setPixelSize(12)
            font.setWeight(QtGui.QFont.Weight.DemiBold)
            return font
        if role == NODE_ID_ROLE:
            return None if node is None else key
        if role == SECTION_ROLE:
            return key if node is None else node['section']
        if role == NODE_TYPE_ROLE:
            return 'section' if node is None else node['nodeType']
        if role == TAG_NAME_ROLE:
            return (node or {}).get('tagName')
        if role == Qt.ItemDataRole.ToolTipRole and (node or {}).get('nodeType') == 'tag':
            return node['tagName'] + '\n' + '、'.join(node.get('aliases', []))
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        key = self._key(index)
        if key in UI_SECTIONS:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
        if key.startswith(BLANK_PREFIX):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsDropEnabled
        if key in self._tag_records:
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        if key.startswith(DRAFT_PREFIX):
            # 草稿行只能改名，不能拖、不能删 —— 它还不是数据。
            return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                    Qt.ItemFlag.ItemIsEditable)
        return (Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                Qt.ItemFlag.ItemIsEditable | Qt.ItemFlag.ItemIsDragEnabled |
                Qt.ItemFlag.ItemIsDropEnabled)

    def setData(self, index, value, role=Qt.ItemDataRole.EditRole):
        if role != Qt.ItemDataRole.EditRole or not index.isValid():
            return False
        key = self._key(index)
        if key in UI_SECTIONS:
            return False
        if key.startswith(BLANK_PREFIX):
            return False
        if key.startswith(DRAFT_PREFIX):
            return self._commit_draft(value)
        if key in self._tag_records:
            return False
        try:
            self.service.rename(key, value)
        except ValueError:
            return False
        return True

    # ── 就地新建：草稿行 ──────────────────────────────────────────

    def begin_draft(self, section, node_type, parent_id, title):
        """Show a temporary row for a node that does not exist yet.

        Nothing is written to the service until the name is confirmed:
        pressing Esc removes the row again and leaves no trace - no node,
        no dirty flag, no undo entry (C5 / §7.4).
        """
        self.clear_draft()
        if section not in TREE_SECTIONS or node_type not in ('folder', 'page'):
            raise ValueError('未知的节点类型')
        key = DRAFT_PREFIX + uuid.uuid4().hex[:8]
        parent_key = parent_id or section
        rows = self._children.setdefault(parent_key, [])
        # 插在分区的空白创建行之前，看起来就像"这个分区的新成员"。
        at = len(rows)
        if rows and rows[-1].startswith(BLANK_PREFIX):
            at = len(rows) - 1
        self.beginInsertRows(self._index_for(parent_key), at, at)
        self._draft = {'key': key, 'section': section, 'nodeType': node_type,
                       'parentId': parent_id, 'title': str(title)}
        rows.insert(at, key)
        self.endInsertRows()
        return self._index_for(key)

    def draft_active(self):
        return self._draft is not None

    def draft_index(self):
        return self._index_for(self._draft['key']) if self._draft \
            else QtCore.QModelIndex()

    def clear_draft(self):
        """Drop the temporary row without touching the service."""
        if not self._draft:
            return
        key = self._draft['key']
        self._draft = None
        for parent_key, rows in self._children.items():
            if key not in rows:
                continue
            row = rows.index(key)
            self.beginRemoveRows(self._index_for(parent_key), row, row)
            rows.remove(key)
            self.endRemoveRows()
            break

    def _commit_draft(self, title):
        """Turn the temporary row into a real node, or refuse and keep it."""
        draft = dict(self._draft or {})
        if not draft:
            return False
        text = str(title)
        # 先收掉草稿行：服务一旦写入，_on_service_changed 会重建兄弟列表，
        # 那时行还在的话模型和视图就对不上了。
        self.clear_draft()
        try:
            if draft['nodeType'] == 'folder':
                node = self.service.createFolder(
                    draft['section'], text, draft['parentId'])
            else:
                node = self.service.createPage(
                    draft['section'], text, uuid.uuid4().hex,
                    draft['parentId'])
        except ValueError as exc:
            # 重名 / 非法字符：把行放回去，并保留用户刚才输入的文字，
            # 让他接着改（§7.4「输入保留」）。
            self.begin_draft(draft['section'], draft['nodeType'],
                             draft['parentId'], text)
            self.draft_refused.emit(str(exc))
            return False
        self.draft_committed.emit(node)
        return True

    def index_for_id(self, node_id):
        return self._index_for(node_id)

    def set_tags(self, nodes):
        """Project tags are a live view, never duplicated in workspace storage."""
        records = {}
        for order, node in enumerate(nodes):
            key = 'catalogue-tag:' + node.name
            parent_name = getattr(node.parent, 'name', node.parent)
            records[key] = {'id': key, 'section': 'tag', 'nodeType': 'tag',
                'tagName': node.name, 'aliases': list(node.aliases),
                'title': f'{node.label}  ({node.count})', 'order': order,
                'parentId': 'catalogue-tag:' + parent_name if parent_name else None}
        if records != self._tag_records:
            self._tag_records = records
            self._on_service_changed({})

    # ── 拖放（C6 / §7.5）──────────────────────────────────────────

    def supportedDropActions(self):
        return Qt.DropAction.MoveAction

    def supportedDragActions(self):
        return Qt.DropAction.MoveAction

    def mimeTypes(self):
        return [NODE_MIME]

    def mimeData(self, indexes):
        """拖动的东西：被拖节点的 id（多选就是一批，服务层会归一化祖先）。"""
        ids = []
        for index in indexes:
            if index.isValid():
                node_id = index.data(NODE_ID_ROLE)
                if node_id and node_id not in ids:
                    ids.append(node_id)
        data = QtCore.QMimeData()
        data.setData(NODE_MIME, '\n'.join(ids).encode('utf-8'))
        return data

    def dragged_ids(self, data):
        if not data.hasFormat(NODE_MIME):
            return []
        try:
            text = bytes(data.data(NODE_MIME)).decode('utf-8')
        except UnicodeDecodeError:
            return []
        return [part for part in text.split('\n') if part]

    def can_accept(self, node_ids, mode, target_id, section=None):
        """能不能放：给视图用，能放返回空字符串，不能放返回原因。

        和真正落地时用的是同一套服务层校验，所以这里说能放、落地就不会失败。
        """
        node_ids = list(node_ids)
        if not node_ids:
            return '没有可移动的项目'
        if mode in ('before', 'after', 'into'):
            target = self._nodes.get(target_id)
            if target is None:
                return '目标已经不存在了'
            if target_id in node_ids:
                return '不能移动到自己身上'
            for node_id in node_ids:
                node = self._nodes.get(node_id)
                if node is not None and node['section'] != target['section']:
                    return '不能跨分区移动'
                if target_id in self.service._descendants(node_id):
                    return '不能移动到自己的子项里'
            if mode == 'into' and target['nodeType'] != 'folder':
                return '页面不能包含子项'
        elif mode == 'append':
            if section not in TREE_SECTIONS:
                return '未知分区'
        else:
            return '不能移动到这里'
        for node_id in node_ids:
            node = self._nodes.get(node_id)
            if node is None:
                return '项目已经不存在了'
            if mode == 'append' and node['section'] != section:
                return '不能跨分区移动'
            if node['nodeType'] == 'draft':
                return '还没确认的新项目不能移动'
        return ''

    def apply_drop(self, node_ids, mode, target_id, section=None):
        """把一次拖放落到服务上；成功返回 True。

        ``mode``：
        - ``into``   —— 移进目标文件夹
        - ``before`` / ``after`` —— 与目标同级，插到它前/后
        - ``append`` —— 移到分区根级的末尾（拖到分区行或空白创建区）
        """
        reason = self.can_accept(node_ids, mode, target_id, section)
        if reason:
            return False
        node_ids = list(dict.fromkeys(node_ids))
        if mode == 'append':
            parent_id = None
        else:
            target = self._nodes.get(target_id)
            section = target['section']
            parent_id = target_id if mode == 'into' else target.get('parentId')
        try:
            moved = self.service.moveBatch(node_ids, parent_id, section)
        except ValueError:
            return False
        if not moved:
            return False
        if mode == 'into':
            return True
        # 同级插入：把刚移过来的那几个放到目标的前面或后面。
        siblings = [n['id'] for n in self.service.listChildren(section, parent_id)]
        rest = [node_id for node_id in siblings if node_id not in moved]
        if mode == 'append' or target_id is None:
            order = rest + list(moved)
        else:
            at = rest.index(target_id)
            if mode == 'after':
                at += 1
            order = rest[:at] + list(moved) + rest[at:]
        try:
            self.service.reorder(section, parent_id, order)
        except ValueError:
            return False
        return True

    def dropMimeData(self, data, action, row, column, parent):
        """Qt 的默认投放路径（视图自己处理时不会走到，留着给别的视图用）。"""
        if action == Qt.DropAction.IgnoreAction:
            return False
        ids = self.dragged_ids(data)
        if not ids:
            return False
        if parent.isValid():
            kind = parent.data(NODE_TYPE_ROLE)
            if kind == 'folder':
                return self.apply_drop(ids, 'into', parent.data(NODE_ID_ROLE))
            if kind in ('section', 'blank'):
                section = parent.data(SECTION_ROLE)
                return self.apply_drop(ids, 'append', None, section=section)
            return False
        return False

    def _on_service_changed(self, _change):
        """Update changed sibling groups without clearing the entire tree."""
        # 服务被别处改了（导入、外部新建…）：草稿行必须先按正常流程收掉，
        # 否则下面的重建会把它从 _children 里无声抹掉，模型和视图就对不上了。
        if self._draft is not None:
            self.clear_draft()
        old_nodes = self._nodes
        old_children = self._children
        new_nodes = {node['id']: dict(node) for node in self.service.nodes
                     if node.get('deletedAt') is None}
        new_nodes.update(self._tag_records)
        new_children = {None: list(UI_SECTIONS)}
        for section in UI_SECTIONS:
            new_children[section] = []
        for node in new_nodes.values():
            if node['nodeType'] == 'folder':
                new_children[node['id']] = []
        for node in new_nodes.values():
            parent = node.get('parentId') or node['section']
            new_children.setdefault(parent, []).append(node['id'])
        for parent, ids in new_children.items():
            if parent is not None:
                ids.sort(key=lambda i: (new_nodes[i]['order'], i))
        for section in UI_SECTIONS:
            new_children[section].append(BLANK_PREFIX + section)

        # A pure rename never changes model shape.
        if old_children == new_children:
            self._nodes = new_nodes
            for node_id in old_nodes.keys() & new_nodes.keys():
                if old_nodes[node_id] != new_nodes[node_id]:
                    index = self._index_for(node_id)
                    self.dataChanged.emit(index, index, [Qt.ItemDataRole.DisplayRole])
            return

        added = new_nodes.keys() - old_nodes.keys()
        removed = old_nodes.keys() - new_nodes.keys()
        if len(added) == 1 and not removed:
            node_id = next(iter(added))
            node = new_nodes[node_id]
            parent_key = node.get('parentId') or node['section']
            old_rows = old_children.get(parent_key, [])
            new_rows = new_children[parent_key]
            if [i for i in new_rows if i != node_id] == old_rows:
                row = new_rows.index(node_id)
                parent_index = self._index_for(parent_key)
                self.beginInsertRows(parent_index, row, row)
                self._nodes[node_id] = node
                self._children[parent_key] = list(new_rows)
                self._children[node_id] = []
                self.endInsertRows()
                return

        # Removing a folder also removes its descendants.  Only the visible
        # root row needs a removal notification; children vanish with it.
        if removed and not added:
            roots = [i for i in removed if old_nodes[i].get('parentId') not in removed]
            if len(roots) == 1:
                node_id = roots[0]
                node = old_nodes[node_id]
                parent_key = node.get('parentId') or node['section']
                old_rows = old_children[parent_key]
                new_rows = new_children[parent_key]
                if [i for i in old_rows if i != node_id] == new_rows:
                    row = old_rows.index(node_id)
                    parent_index = self._index_for(parent_key)
                    self.beginRemoveRows(parent_index, row, row)
                    self._children[parent_key] = list(new_rows)
                    for key in removed:
                        self._nodes.pop(key, None)
                        self._children.pop(key, None)
                    self.endRemoveRows()
                    return

        if not added and not removed:
            moved = [i for i in old_nodes if old_nodes[i].get('parentId') !=
                     new_nodes[i].get('parentId')]
            if len(moved) == 1:
                node_id = moved[0]
                before = old_nodes[node_id]
                after = new_nodes[node_id]
                source_key = before.get('parentId') or before['section']
                target_key = after.get('parentId') or after['section']
                old_source = old_children[source_key]
                new_source = new_children[source_key]
                old_target = old_children[target_key]
                new_target = new_children[target_key]
                if (source_key != target_key and
                        [i for i in old_source if i != node_id] == new_source and
                        [i for i in new_target if i != node_id] == old_target):
                    source_row = old_source.index(node_id)
                    target_row = new_target.index(node_id)
                    self.beginMoveRows(self._index_for(source_key),
                                       source_row, source_row,
                                       self._index_for(target_key), target_row)
                    self._children[source_key] = list(new_source)
                    self._children[target_key] = list(new_target)
                    self._nodes[node_id] = after
                    self.endMoveRows()
                    return

        # Structural edits can affect two sibling groups (a move) or an
        # entire subtree (trash). layoutChanged preserves stable IDs without
        # the old QListWidget's clear-and-rebuild flicker.
        self.layoutAboutToBeChanged.emit()
        self._nodes = new_nodes
        self._children = new_children
        self.layoutChanged.emit()
