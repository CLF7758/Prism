"""工程里的页面：新增、删除、复制、重命名、排序、恢复。

任务书 T9 要求「服务不得持有整个 `PrismGraphicsView`，依赖必须显式传入」。
这个服务拿的是一个 pages 列表（`scene.workspace_pages`），不碰任何控件
—— 所以它能脱离界面测试，界面那边只把菜单动作转成调用。

**页面长什么样**（沿用现有形状，不改）：

    {'id': str,                      # uuid4().hex，或者隐式页面的固定 id
     'kind': 'canvas'|'document'|'mindmap',
     'title': str,
     'content': None | str | dict,   # 文档正文 / 脑图树
     'tags': [str, ...],             # 页面标签，和素材标签分开
     'parent': str | None,           # 二级页面的父页面 id
     'permanent': bool}              # 可选；隐式页面（默认画布等）

**服务不生产隐式页面**。默认画布 / 默认脑图 / 默认文档的 id 是固定的
（`default-canvas` 等），而且和界面上的页签绑定 —— 那属于
`PrismMainWindow._materialise_implicit_page`。这里只管用户自建的页面。
"""
import copy
import logging
import uuid

from PyQt6 import QtCore

logger = logging.getLogger(__name__)

#: 三种页面类型，和现有代码里的取值一致。
KINDS = ('canvas', 'document', 'mindmap')

TREE_SECTIONS = ('canvas', 'mindmap', 'document')


def legacy_page_nodes(pages, project_id):
    """Preview a lossless *index* for old pages; never alter page content.

    A former parent page becomes an overview page inside a same-title folder.
    Missing/cyclic parent references are made visible in a recovery folder.
    Legacy canvas categories are deliberately not accepted here: they remain
    asset filters, not cloned canvas pages.
    """
    pages = list(pages or [])
    by_id = {}
    for page in pages:
        page_id = page.get('id')
        if (not page_id or page_id in by_id or
                page.get('kind') not in TREE_SECTIONS):
            raise ValueError('旧页面 ID 或类型无效，不能安全迁移')
        by_id[page_id] = page

    def stable_id(prefix, value):
        return uuid.uuid5(uuid.NAMESPACE_URL,
                          f'prism:{project_id}:{prefix}:{value}').hex

    invalid = set()
    for page in pages:
        seen = {page['id']}
        current = page
        while current.get('parent'):
            parent_id = current['parent']
            parent = by_id.get(parent_id)
            if (parent is None or parent.get('kind') != page['kind'] or
                    parent_id in seen):
                invalid.add(page['id'])
                break
            seen.add(parent_id)
            current = parent

    valid_parent = {
        p['id']: p.get('parent') if p['id'] not in invalid else None
        for p in pages}
    parents_with_children = {parent_id for parent_id in valid_parent.values()
                             if parent_id is not None}
    nodes = []
    recovery_ids = {}
    for section in TREE_SECTIONS:
        if any(p['kind'] == section and p['id'] in invalid for p in pages):
            folder_id = stable_id('recovery', section)
            recovery_ids[section] = folder_id
            nodes.append({
                'id': folder_id, 'projectId': project_id,
                'section': section, 'nodeType': 'folder',
                'parentId': None, 'title': '恢复项', 'order': 999000,
                'pageId': None, 'color': None, 'deletedAt': None})

    sibling_names = {}

    def unique_title(section, parent_id, node_type, base):
        title = ResourceTreeService._title(base)
        key = (section, parent_id, node_type)
        taken = sibling_names.setdefault(key, set())
        candidate = title
        counter = 2
        while candidate in taken:
            suffix = f' {counter}'
            candidate = title[:80 - len(suffix)] + suffix
            counter += 1
        taken.add(candidate)
        return candidate

    for order, page in enumerate(pages, 1):
        page_id = page['id']
        section = page['kind']
        old_parent = valid_parent[page_id]
        parent_id = (stable_id('folder', old_parent) if old_parent else
                     recovery_ids.get(section) if page_id in invalid else None)
        has_children = page_id in parents_with_children
        if has_children:
            folder_id = stable_id('folder', page_id)
            nodes.append({
                'id': folder_id, 'projectId': project_id,
                'section': section, 'nodeType': 'folder',
                'parentId': parent_id,
                'title': unique_title(section, parent_id, 'folder',
                                      page.get('title') or section),
                'order': order * 1000, 'pageId': None,
                'color': None, 'deletedAt': None})
            parent_id = folder_id
        nodes.append({
            'id': stable_id('page', page_id), 'projectId': project_id,
            'section': section, 'nodeType': 'page',
            'parentId': parent_id,
            'title': unique_title(section, parent_id, 'page',
                                  '概述' if has_children else
                                  page.get('title') or section),
            'order': order * 1000, 'pageId': page_id,
            'color': None, 'deletedAt': None})
    ResourceTreeService(project_id, nodes)
    return nodes


class ResourceTreeService(QtCore.QObject):
    """Stable-ID folder/page hierarchy, kept separate from legacy pages.

    ``nodes`` is an owned list of dictionaries suitable for JSON persistence.
    Edits are validated before mutation so a failed batch cannot half-move a
    tree.  The existing WorkspaceService remains the legacy page adapter.
    """

    changed = QtCore.pyqtSignal(object)

    def __init__(self, project_id, nodes=None, parent=None):
        super().__init__(parent)
        self.project_id = project_id
        self.nodes = copy.deepcopy(list(nodes or []))
        self._validate_all()

    def _by_id(self, node_id):
        return next((n for n in self.nodes if n['id'] == node_id), None)

    @staticmethod
    def _title(value):
        title = str(value).strip()
        if not 1 <= len(title) <= 80 or any(
                ord(ch) < 32 or ch in '/\\' for ch in title):
            raise ValueError('名称须为 1–80 字，且不能包含控制字符或 /\\')
        return title

    def _validate_parent(self, section, parent_id):
        if parent_id is None:
            return
        parent = self._by_id(parent_id)
        if (parent is None or parent['projectId'] != self.project_id or
                parent['section'] != section or parent['nodeType'] != 'folder' or
                parent.get('deletedAt') is not None):
            raise ValueError('父节点必须是同工程、同分区的有效文件夹')

    def _validate_all(self):
        ids = [n.get('id') for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError('资源树节点 ID 重复')
        for node in self.nodes:
            if (node.get('projectId') != self.project_id or
                    node.get('section') not in TREE_SECTIONS or
                    node.get('nodeType') not in ('folder', 'page')):
                raise ValueError('资源树节点类型或归属无效')
            self._title(node.get('title', ''))
            parent_id = node.get('parentId')
            if parent_id is not None:
                parent = self._by_id(parent_id)
                if (parent is None or parent.get('section') != node['section'] or
                        parent.get('nodeType') != 'folder'):
                    raise ValueError('资源树父节点无效')
            seen = {node['id']}
            while parent_id is not None:
                if parent_id in seen:
                    raise ValueError('资源树不能形成循环')
                seen.add(parent_id)
                parent_id = self._by_id(parent_id).get('parentId')

    def listChildren(self, section, parent_id=None, *, include_deleted=False):
        if section not in TREE_SECTIONS:
            raise ValueError('未知分区')
        return sorted((copy.deepcopy(n) for n in self.nodes
                       if n['section'] == section and
                       n.get('parentId') == parent_id and
                       (include_deleted or n.get('deletedAt') is None)),
                      key=lambda n: (n['order'], n['id']))

    def _siblings(self, section, parent_id, node_type=None, exclude=None):
        return [n for n in self.nodes if n['section'] == section and
                n.get('parentId') == parent_id and
                n.get('deletedAt') is None and n['id'] != exclude and
                (node_type is None or n['nodeType'] == node_type)]

    def _unique_title(self, section, parent_id, node_type, title, exclude=None):
        if any(n['title'] == title for n in self._siblings(
                section, parent_id, node_type, exclude)):
            raise ValueError('此位置已有同名项目')

    def unique_suggestion(self, section, parent_id, node_type, base):
        """A free default name for a new node: 「未命名文档」/「未命名文档 2」…

        Only *suggestions* are numbered like this.  A name the user typed is
        never adjusted behind their back - a clash there is refused with
        「此位置已有同名项目」 and the input stays as it was.
        """
        title = self._title(base)
        taken = {n['title'] for n in self._siblings(
            section, parent_id, node_type)}
        if title not in taken:
            return title
        index = 2
        while f'{title} {index}' in taken:
            index += 1
        return f'{title} {index}'

    def _create(self, section, parent_id, node_type, title, page_id=None):
        if section not in TREE_SECTIONS:
            raise ValueError('未知分区')
        self._validate_parent(section, parent_id)
        title = self._title(title)
        self._unique_title(section, parent_id, node_type, title)
        siblings = self._siblings(section, parent_id)
        node = {'id': uuid.uuid4().hex, 'projectId': self.project_id,
                'section': section, 'nodeType': node_type,
                'parentId': parent_id, 'title': title,
                'order': max((n['order'] for n in siblings), default=0) + 1000,
                'pageId': page_id if node_type == 'page' else None,
                'color': None, 'deletedAt': None}
        self.nodes.append(node)
        self.changed.emit({'ids': [node['id']], 'old': None,
                           'new': copy.deepcopy(node), 'undo': '删除新建项目'})
        return copy.deepcopy(node)

    def createFolder(self, section, title, parent_id=None):
        return self._create(section, parent_id, 'folder', title)

    def createPage(self, section, title, page_id, parent_id=None):
        if not page_id:
            raise ValueError('页面 ID 不能为空')
        if any(n.get('pageId') == page_id for n in self.nodes):
            raise ValueError('页面 ID 重复')
        return self._create(section, parent_id, 'page', title, page_id)

    def rename(self, node_id, title):
        node = self._by_id(node_id)
        if node is None or node.get('deletedAt') is not None:
            raise ValueError('节点不存在')
        title = self._title(title)
        self._unique_title(node['section'], node['parentId'],
                           node['nodeType'], title, node_id)
        old = node['title']
        node['title'] = title
        self.changed.emit({'ids': [node_id], 'old': old,
                           'new': title, 'undo': '恢复原名称'})
        return copy.deepcopy(node)

    def _descendants(self, node_id):
        result = set()
        pending = [node_id]
        while pending:
            parent_id = pending.pop()
            children = [n['id'] for n in self.nodes
                        if n.get('parentId') == parent_id]
            result.update(children)
            pending.extend(children)
        return result

    def moveBatch(self, node_ids, parent_id, section):
        """Move only highest selected ancestors; validate before writing."""
        selected = set(node_ids)
        if not selected or section not in TREE_SECTIONS:
            return []
        roots = []
        for node_id in node_ids:
            node = self._by_id(node_id)
            if (node is None or node.get('deletedAt') is not None or
                    node['section'] != section):
                raise ValueError('不能跨分区移动或移动不存在的节点')
            ancestor = node.get('parentId')
            while ancestor is not None and ancestor not in selected:
                ancestor = self._by_id(ancestor).get('parentId')
            if ancestor is None and node_id not in roots:
                roots.append(node_id)
        self._validate_parent(section, parent_id)
        if parent_id in roots or any(parent_id in self._descendants(i)
                                     for i in roots):
            raise ValueError('不能移入自身或后代')
        for node_id in roots:
            node = self._by_id(node_id)
            self._unique_title(section, parent_id, node['nodeType'],
                               node['title'], node_id)
        names = [(self._by_id(i)['nodeType'], self._by_id(i)['title'])
                 for i in roots]
        if len(names) != len(set(names)):
            raise ValueError('此位置已有同名项目')
        old = {i: self._by_id(i)['parentId'] for i in roots}
        next_order = max((n['order'] for n in self._siblings(
            section, parent_id)), default=0)
        for node_id in roots:
            node = self._by_id(node_id)
            next_order += 1000
            node['parentId'] = parent_id
            node['order'] = next_order
        self.changed.emit({'ids': roots, 'old': old, 'new': parent_id,
                           'undo': '移回原文件夹'})
        return roots

    def reorder(self, section, parent_id, node_ids):
        """Set one sibling group's complete order without moving its nodes."""
        current = self.listChildren(section, parent_id)
        if set(node_ids) != {n['id'] for n in current} or len(node_ids) != len(current):
            raise ValueError('排序必须包含同一位置的全部有效节点')
        old = {n['id']: n['order'] for n in current}
        for index, node_id in enumerate(node_ids, 1):
            self._by_id(node_id)['order'] = index * 1000
        self.changed.emit({'ids': list(node_ids), 'old': old,
                           'new': list(node_ids), 'undo': '恢复原排序'})
        return list(node_ids)

    def trash(self, node_ids, deleted_at):
        """Soft-delete selected roots and their descendants together."""
        if not deleted_at:
            raise ValueError('删除时间不能为空')
        selected = set(node_ids)
        if not selected:
            return []
        for node_id in selected:
            node = self._by_id(node_id)
            if node is None or node.get('deletedAt') is not None:
                raise ValueError('节点不存在')
        affected = selected.copy()
        for node_id in selected:
            affected.update(self._descendants(node_id))
        old = {i: self._by_id(i).get('deletedAt') for i in affected}
        for node_id in affected:
            self._by_id(node_id)['deletedAt'] = deleted_at
        ids = sorted(affected)
        self.changed.emit({'ids': ids, 'old': old,
                           'new': deleted_at, 'undo': '从回收站恢复'})
        return ids

    def restore_deleted(self, node_ids):
        """Restore a trashed subtree; reject a still-trashed parent."""
        selected = set(node_ids)
        if not selected:
            return []
        affected = selected.copy()
        for node_id in selected:
            node = self._by_id(node_id)
            if node is None or node.get('deletedAt') is None:
                raise ValueError('节点不在回收站')
            affected.update(self._descendants(node_id))
        for node_id in affected:
            node = self._by_id(node_id)
            parent_id = node.get('parentId')
            if parent_id and parent_id not in affected and self._by_id(
                    parent_id).get('deletedAt') is not None:
                raise ValueError('须先恢复父文件夹')
        old = {i: self._by_id(i)['deletedAt'] for i in affected}
        for node_id in affected:
            self._by_id(node_id)['deletedAt'] = None
        ids = sorted(affected)
        self.changed.emit({'ids': ids, 'old': old,
                           'new': None, 'undo': '移回回收站'})
        return ids

    def search(self, query, section=None, *, include_deleted=False):
        if section is not None and section not in TREE_SECTIONS:
            raise ValueError('未知分区')
        needle = str(query).strip().casefold()
        if not needle:
            return []
        return [copy.deepcopy(n) for n in self.nodes
                if (section is None or n['section'] == section) and
                (include_deleted or n.get('deletedAt') is None) and
                needle in n['title'].casefold()]

    # ── 回收站（§7.6）─────────────────────────────────────────────

    def trashed_nodes(self):
        """回收站里的东西：先按删除时间，再按标题。"""
        return sorted(
            (copy.deepcopy(n) for n in self.nodes if n.get('deletedAt')),
            key=lambda n: (n.get('deletedAt') or '', n['title']))

    def purge(self, node_ids):
        """永久删除：把节点和它的后代从工程里真正拿掉。

        只接受**回收站里**的节点 —— §7.6「永久删除仅在回收站执行」。
        返回被拿掉的 id；对应的页面正文由调用方决定怎么处理。
        """
        selected = set(node_ids)
        if not selected:
            return []
        for node_id in selected:
            node = self._by_id(node_id)
            if node is None or node.get('deletedAt') is None:
                raise ValueError('只能永久删除回收站里的项目')
        affected = selected.copy()
        for node_id in selected:
            affected.update(self._descendants(node_id))
        old = {i: self._by_id(i).get('deletedAt') for i in affected}
        # 原地改列表：scene.workspace_nodes 和别人拿到的引用都指着它。
        self.nodes[:] = [n for n in self.nodes if n['id'] not in affected]
        ids = sorted(affected)
        self.changed.emit({'ids': ids, 'old': old, 'new': None,
                           'undo': '永久删除'})
        return ids


#: 新页面没给名字时用的默认标题（英文原文，界面那边走翻译）。
DEFAULT_TITLES = {'canvas': 'Canvas', 'document': 'Document',
                  'mindmap': 'Mind Map'}


class WorkspaceService(QtCore.QObject):
    """页面列表的增删改查。

    所有方法都只动 `scene.workspace_pages` 里的数据并发信号，不碰控件、
    不碰数据库。撤销由调用方用它自己的撤销栈做 —— 服务提供 `snapshot()`
    和 `restore()` 就够了。
    """

    #: 列表变了。参数是变化的类型，给界面决定要不要重建树。
    changed = QtCore.pyqtSignal(str)

    def __init__(self, scene, parent=None):
        super().__init__(parent)
        self._scene = scene

    # ── 读 ────────────────────────────────────────────────────────

    @property
    def pages(self):
        """当前页面列表。没有就补一个空列表上去。

        现有代码到处写 `getattr(scene, 'workspace_pages', []) or []`，
        说明这个属性可能是 None 也可能不存在。集中在这里处理一次。
        """
        pages = getattr(self._scene, 'workspace_pages', None)
        if pages is None:
            pages = []
            self._scene.workspace_pages = pages
        return pages

    def find(self, page_id):
        for page in self.pages:
            if page.get('id') == page_id:
                return page
        return None

    def by_kind(self, kind):
        return [p for p in self.pages if p.get('kind') == kind]

    def children_of(self, page_id):
        return [p for p in self.pages if p.get('parent') == page_id]

    def is_permanent(self, page_id):
        """隐式页面不能删 —— 它们是工程结构的一部分。"""
        page = self.find(page_id)
        return bool(page and page.get('permanent'))

    # ── 快照与恢复（撤销用）──────────────────────────────────────

    def snapshot(self):
        """整表的深拷贝。

        深拷贝而不是浅的：调用方拿它做撤销，浅拷贝会让"删除前"的快照
        跟着后续的改动一起变，撤销就撤销不回去了。
        """
        return copy.deepcopy(self.pages)

    def restore(self, pages):
        """整表恢复。"""
        self._scene.workspace_pages = copy.deepcopy(list(pages or []))
        self.changed.emit('restore')

    # ── 改名 ──────────────────────────────────────────────────────

    def next_title(self, kind, base=None):
        """给新页面起个不重名的标题。

        "Canvas" / "Canvas 2" / "Canvas 3" —— 用户看到重复的名字会分不清
        哪个是哪个，而自动编号比自己再改一次省事。
        """
        base = (base or DEFAULT_TITLES.get(kind, 'Page')).strip() or 'Page'
        taken = {p.get('title') for p in self.pages}
        if base not in taken:
            return base
        index = 2
        while f'{base} {index}' in taken:
            index += 1
        return f'{base} {index}'

    # ── 增 ────────────────────────────────────────────────────────

    def add(self, kind, title=None, parent=None, page_id=None):
        """加一个页面并放到末尾，返回它。

        ``title`` 不给就用不重名的默认标题。``page_id`` 一般不用传 ——
        留出这个参数是为了让导入那条路能沿用自己算好的 id。
        """
        if kind not in KINDS:
            raise ValueError(f'未知的页面类型：{kind}')
        page = {
            'id': page_id or uuid.uuid4().hex,
            'kind': kind,
            'title': (title or '').strip() or self.next_title(kind),
            'content': None,
            'tags': [],
            'parent': parent,
        }
        self.pages.append(page)
        self.changed.emit('add')
        return page

    def restore_page(self, page):
        """Put a previously saved page object back into the workspace.

        This is intentionally separate from :meth:`add`: recovery and the
        "restore deleted page" action must retain the original id, content,
        tags and permanent flag.  The copy prevents a caller's undo stash
        from becoming an alias for live workspace data.
        """
        if not isinstance(page, dict) or not page.get('id'):
            raise ValueError('无效的页面数据')
        restored = copy.deepcopy(page)
        self.pages.append(restored)
        self.changed.emit('restore-page')
        return restored

    # ── 复制 ──────────────────────────────────────────────────────

    def duplicate(self, page_id, title=None):
        """复制一个页面，返回新页面（找不到返回 None）。

        **只复制页面本身，不复制画布上的素材。** 素材挂在 ``_canvas_id``
        上，复制它们意味着同一张图在工程里出现两份，查重会立刻报一堆
        重复 —— 而用户说"复制页面"通常是想再开一块地方摆图，不是想把
        现有的图克隆一份。
        """
        source = self.find(page_id)
        if source is None:
            return None
        copy_of = {
            'id': uuid.uuid4().hex,
            'kind': source.get('kind'),
            'title': (title or '').strip()
            or self.next_title(source.get('kind'),
                               f"{source.get('title', '')} 副本"),
            'content': copy.deepcopy(source.get('content')),
            'tags': list(source.get('tags') or []),
            'parent': source.get('parent'),
        }
        if source.get('permanent'):
            # 副本不是隐式页面：它能被删、能被改名。这里什么都不用做，
            # 只是明确一下这个类型的差异是有意为之。
            logger.debug('Duplicated a permanent page; the copy is ordinary')

        self.pages.append(copy_of)
        self.changed.emit('duplicate')
        return copy_of

    # ── 删 ────────────────────────────────────────────────────────

    def remove(self, page_id):
        """删掉一个页面**以及它的二级页面**，返回被删掉的 id 列表。

        连带删除而不是拒绝：留一堆 parent 指向已删页面的孤儿页面，比
        多点一次确认更麻烦。返回值给调用方做撤销用。

        隐式页面（``permanent``）删不掉，返回空列表。
        """
        page = self.find(page_id)
        if page is None or page.get('permanent'):
            return []
        doomed = [page_id]
        # 二级页面可能还有三级，一层层收
        pending = [page_id]
        while pending:
            current = pending.pop()
            for child in self.children_of(current):
                if child['id'] not in doomed:
                    doomed.append(child['id'])
                    pending.append(child['id'])
        removed = [p for p in self.pages if p.get('id') in set(doomed)]
        self._scene.workspace_pages = [
            p for p in self.pages if p.get('id') not in set(doomed)]
        self.changed.emit('remove')
        return [p['id'] for p in removed]

    # ── 排序 ──────────────────────────────────────────────────────

    def move(self, page_id, offset):
        """在列表里上下移动 ``offset`` 个位置，返回是否真的动了。

        只挪一格一步更容易讲清楚，也更容易测：一次挪到位的话，"挪到
        第几位"和"挪几位"这两种理解会让人用错。
        """
        pages = self.pages
        index = next((i for i, p in enumerate(pages)
                      if p.get('id') == page_id), None)
        if index is None or not offset:
            return False
        target = index + int(offset)
        if target < 0 or target >= len(pages):
            return False
        page = pages.pop(index)
        pages.insert(target, page)
        self.changed.emit('move')
        return True

    def move_to(self, page_id, position):
        """挪到指定位置（从 0 数）。"""
        pages = self.pages
        index = next((i for i, p in enumerate(pages)
                      if p.get('id') == page_id), None)
        if index is None:
            return False
        position = max(0, min(len(pages) - 1, int(position)))
        if position == index:
            return False
        page = pages.pop(index)
        pages.insert(position, page)
        self.changed.emit('move')
        return True

    # ── 改 ────────────────────────────────────────────────────────

    def rename(self, page_id, title):
        """改名。空白名字不接受 —— 那样页签会变成一片空白。"""
        page = self.find(page_id)
        if page is None:
            return False
        clean = (title or '').strip()
        if not clean or clean == page.get('title'):
            return False
        page['title'] = clean
        self.changed.emit('rename')
        return True

    def set_tags(self, page_id, names):
        """设置页面标签。

        页面标签和素材标签是两回事：前者整理页面，后者找素材。这里只
        写进 ``page['tags']``，不碰任何素材。
        """
        page = self.find(page_id)
        if page is None:
            return False
        cleaned = []
        for name in names or ():
            # 先挡掉 None：str(None) 是 'None'，那不是空字符串，会被
            # 当成一个叫 "None" 的标签收进来。
            if name is None:
                continue
            text = str(name).strip()
            if text and text not in cleaned:
                cleaned.append(text)
        page['tags'] = cleaned
        self.changed.emit('tags')
        return True
