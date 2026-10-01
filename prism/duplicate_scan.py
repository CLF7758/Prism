"""Duplicate scanning with an explicit scope, a content-keyed cache and progress.

Why this exists as its own service rather than living in the dialog:

* **The scope is always passed in.**  The old code read the category filter
  out of the media library panel, so "what gets scanned" depended on
  whatever the user happened to have selected in the UI.  A scan now takes
  a :class:`ScanScope` value and never looks at a widget.

* **Hashing is cacheable and thread-friendly.**  Hashing only needs the
  stored bytes, which can be decoded with ``QImage.fromData`` on a worker
  thread - no QPixmap, no GUI access.  Results are keyed by the content
  digest, so renaming or moving a file still hits the cache.

The service never touches the scene, the undo stack or the selection; the
caller hands in the image sources and owns whatever happens next.
"""
import collections
import hashlib
import logging

from PyQt6 import QtCore, QtGui

from prism.similarity import (average_color, color_distance, difference_hash,
                              hamming_distance)

logger = logging.getLogger(__name__)

#: Difference-hash distance at or below which two images count as the same.
DEFAULT_MAX_DISTANCE = 4

#: Colour signature ceiling, matching duplicate_groups().
DEFAULT_COLOR_DISTANCE = 45

#: How many distinct images the in-memory hash cache keeps.
DEFAULT_CACHE_SIZE = 4096


class ScanScope:
    """What a scan should look at.  Always supplied, never guessed.

    The dialog and the menu both build one of these explicitly, so the same
    scan can be reproduced without replicating the UI state that produced
    it.
    """

    SELECTION = 'selection'
    CURRENT_CANVAS = 'current-canvas'
    #: 当前工作区页面。和 CURRENT_CANVAS 在画布页上结果相同，但**对文档页
    #: 和脑图页是另一个意思** —— 那些页面的图片是附件（住在 attachments
    #: 表里），不在画布上，所以没有素材可查。见 `collect_sources`。
    CURRENT_PAGE = 'current-page'
    ALL_CANVASES = 'all-canvases'
    ALL_ASSETS = 'all-assets'

    ALL = (SELECTION, CURRENT_CANVAS, CURRENT_PAGE, ALL_CANVASES,
           ALL_ASSETS)

    @classmethod
    def label(cls, scope):
        from prism.i18n import _
        return {
            cls.SELECTION: _('Selected images only'),
            cls.CURRENT_CANVAS: _('The current canvas'),
            cls.CURRENT_PAGE: _('The current page'),
            cls.ALL_CANVASES: _('All canvases'),
            cls.ALL_ASSETS: _('Everything in the project'),
        }.get(scope, scope)



class ImageSource:
    """One image to compare, detached from any Qt item.

    ``prepare`` runs on the GUI thread and grabs the bytes; everything
    after that is safe to do on a worker.
    """

    __slots__ = ('key', 'name', 'bytes_', 'location', 'item')

    def __init__(self, key, name, data, location='', item=None):
        self.key = key
        self.name = name
        self.bytes_ = data
        self.location = location
        self.item = item

    def digest(self):
        return hashlib.sha256(self.bytes_).hexdigest()

    @staticmethod
    def from_item(item, location=''):
        """Build a source from a scene item, on the GUI thread."""
        data = None
        getter = getattr(item, 'original_bytes', None)
        if callable(getter):
            data = getter()
        name = (getattr(item, '_title', '')
                or getattr(item, 'filename', '') or '?')
        if data is None:
            # No stored original (a generated image, say).  Grab the pixels
            # now, while we are still on the GUI thread.
            pixmap = getattr(item, 'pixmap', lambda: None)()
            if pixmap is not None and not pixmap.isNull():
                buffer = QtCore.QBuffer()
                buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
                if pixmap.save(buffer, 'PNG'):
                    data = bytes(buffer.data())
        if data is None:
            return None
        return ImageSource(hashlib.sha256(data).hexdigest(), name, data,
                           location, item)


class HashCache:
    """Content digest -> (difference hash, colour signature, aspect).

    Keyed by content rather than by path: moving or renaming a file still
    hits, and two copies of the same bytes share one entry.  Simple LRU,
    because the point is to avoid re-decoding within a session.
    """

    def __init__(self, max_entries=DEFAULT_CACHE_SIZE):
        self.max_entries = int(max_entries)
        self._entries = collections.OrderedDict()

    def get(self, digest):
        try:
            value = self._entries.pop(digest)
        except KeyError:
            return None
        self._entries[digest] = value          # mark as most recent
        return value

    def put(self, digest, value):
        self._entries[digest] = value
        self._entries.move_to_end(digest)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    def __len__(self):
        return len(self._entries)

    def clear(self):
        self._entries.clear()


class DuplicateScanService(QtCore.QObject):
    """Find visually duplicate images among a prepared set of sources."""

    progress = QtCore.pyqtSignal(int, int)
    finished = QtCore.pyqtSignal(list, dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None, cache=None,
                 max_distance=DEFAULT_MAX_DISTANCE,
                 color_distance_limit=DEFAULT_COLOR_DISTANCE):
        super().__init__(parent)
        self.cache = cache if cache is not None else HashCache()
        self.max_distance = int(max_distance)
        self.color_distance_limit = int(color_distance_limit)
        self._cancel = False

    def cancel(self):
        self._cancel = True

    # ── The scan ──────────────────────────────────────────────

    def scan(self, sources, scope=ScanScope.ALL_ASSETS):
        """Compare the given sources and emit the duplicate groups.

        Runs synchronously so it can be pushed onto a worker thread; the
        caller decides which thread that is.
        """
        self._cancel = False
        usable = [s for s in sources if s is not None]
        total = len(usable)
        stats = {'scope': scope, 'scanned': total, 'cache_hits': 0,
                 'unreadable': 0, 'cancelled': False}

        fingerprinted = []
        for index, source in enumerate(usable):
            if self._cancel:
                stats['cancelled'] = True
                self.finished.emit([], stats)
                return
            self.progress.emit(index, total)
            digest = source.digest()
            cached = self.cache.get(digest)
            if cached is not None:
                stats['cache_hits'] += 1
                value, colour, aspect = cached
            else:
                entry = self._fingerprint(source)
                if entry is None:
                    stats['unreadable'] += 1
                    continue
                value, colour, aspect = entry
                self.cache.put(digest, entry)
            fingerprinted.append((source, value, colour, aspect))

        stats['compared'] = len(fingerprinted)
        groups = self._group(fingerprinted)
        if groups is None:
            # Cancelling during the pair-comparison phase used to leave us
            # with a partially built union-find graph.  Showing those
            # partial groups is unsafe: a user can act on an incomplete
            # result set.  A cancelled scan therefore has no result.
            stats['cancelled'] = True
            self.finished.emit([], stats)
            return
        self.finished.emit(groups, stats)

    def _fingerprint(self, source):
        """Decode one source and compute its fingerprint."""
        image = QtGui.QImage.fromData(source.bytes_)
        if image.isNull():
            # Qt cannot decode this one; leave it out rather than guess.
            return None
        value = difference_hash(image)
        if value is None:
            return None
        colour = average_color(image)
        aspect = image.width() / max(image.height(), 1)
        return (value, colour, aspect)

    def _group(self, fingerprinted):
        """Union-find over the fingerprint list."""
        parent = list(range(len(fingerprinted)))

        def find(index):
            while parent[index] != index:
                parent[index] = parent[parent[index]]
                index = parent[index]
            return index

        def union(left, right):
            left_root, right_root = find(left), find(right)
            if left_root != right_root:
                parent[right_root] = left_root

        for left in range(len(fingerprinted)):
            for right in range(left + 1, len(fingerprinted)):
                if self._cancel:
                    return None
                if self._close_enough(fingerprinted[left],
                                      fingerprinted[right]):
                    union(left, right)

        buckets = {}
        for index, entry in enumerate(fingerprinted):
            buckets.setdefault(find(index), []).append(entry[0])

        groups = [members for members in buckets.values() if len(members) > 1]
        groups.sort(key=lambda members: (-len(members),
                                         members[0].name.casefold()))
        return groups

    def _close_enough(self, left, right):
        if hamming_distance(left[1], right[1]) > self.max_distance:
            return False
        if color_distance(left[2], right[2]) > self.color_distance_limit:
            return False
        largest = max(left[3], right[3])
        return abs(left[3] - right[3]) <= largest * 0.02


def canvas_label(item, pages=()):
    """这个素材在哪儿 —— 页面名，后面跟分类名。

    任务书 §6 的验收要求「同一图片在不同页面出现时能够显示页面来源」。
    同一个素材可能没有页面（老工程里 `_canvas_id` 为空）也可能没有分类，
    所以两头都留空也能返回空串，由调用方决定显示成什么。

    抽成公共函数是因为结果对话框也要显示这个 —— 两边各拼一次字符串的话
    早晚会不一致（改之前就是：一个填 `_categories`，另一个显示
    "Category: ..."，页面名谁都没管）。
    """
    parts = []
    canvas_id = getattr(item, '_canvas_id', None)
    if canvas_id:
        for page in pages or ():
            if page.get('id') == canvas_id:
                title = page.get('title') or ''
                if title:
                    parts.append(title)
                break
    categories = '、'.join(getattr(item, '_categories', []) or [])
    if categories:
        parts.append(categories)
    return ' · '.join(parts)


def collect_sources(scene, scope, current_canvas_id=None, selected=(),
                    current_page=None, progress=None):
    """Gather the image sources a scan should look at.

    Written as a free function taking plain values, so the caller decides
    the scope; nothing here reads a widget.

    ``current_page`` 是 ``{'id': ..., 'kind': ...}``，只有 CURRENT_PAGE 用。
    传 None 时按画布页处理（那是默认页面的情况）。

    ``progress`` 是 ``callback(done, total)``，每收一小批调一次。

    **为什么要它**：下面那个循环要在 GUI 线程逐个读素材的字节（
    `ImageSource.from_item`）—— 用户的工程 1563 个素材，几秒钟。不给
    调用方机会把进度画出来的话，用户看到的就是"点了查重，卡住几秒，
    窗口才出来"。
    """
    from prism.items import PrismPixmapItem

    everything = [item for item in scene.items_for_save()
                  if isinstance(item, PrismPixmapItem)]

    if scope == ScanScope.SELECTION:
        chosen = [item for item in selected
                  if isinstance(item, PrismPixmapItem)]
    elif scope == ScanScope.CURRENT_PAGE:
        # 文档页和脑图页的图片是附件（attachments 表），不在画布上 ——
        # 所以那两种页面上没有可以查重的素材。如实给空，不假装扫了。
        kind = (current_page or {}).get('kind', 'canvas')
        if kind != 'canvas':
            logger.debug('当前页面是 %s，上面没有画布素材可查',
                         kind)
            return []
        page_id = (current_page or {}).get('id') or current_canvas_id
        chosen = [item for item in everything
                  if getattr(item, '_canvas_id', None) == page_id]
    elif scope == ScanScope.CURRENT_CANVAS:
        chosen = [item for item in everything
                  if getattr(item, '_canvas_id', None) == current_canvas_id]
    elif scope == ScanScope.ALL_CANVASES:
        pages = [p.get('id') for p in
                 (getattr(scene, 'workspace_pages', []) or [])
                 if p.get('kind') == 'canvas']
        known = set(pages) | {'default-canvas'}
        chosen = [item for item in everything
                  if getattr(item, '_canvas_id', 'default-canvas') in known]
    else:
        chosen = list(everything)

    pages = getattr(scene, 'workspace_pages', []) or []
    sources = []
    total = len(chosen)
    for index, item in enumerate(chosen):
        # 页面名 + 分类。以前这里只有分类，于是「同一个文件出现在两个
        # 画布上」时两条记录看起来一模一样，看不出是哪来的。
        where = canvas_label(item, pages)
        source = ImageSource.from_item(item, where)
        if source is not None:
            sources.append(source)
        # 每 16 个报一次进度：报太勤反而慢（改界面比读字节还贵），
        # 报太稀进度条看着像卡住。
        if progress is not None and (index + 1) % 16 == 0:
            progress(index + 1, total)
    if progress is not None and total:
        progress(total, total)
    return sources
