"""附件存储：元数据常驻，blob 按需从工程文件读。

任务书 T5 要求打开工程时"不得加载所有文档正文"。附件（文档和脑图里的
图片）就在这条线上 —— 以前 read_board_extras() 会把它们连同 blob 一次
全读进内存，秋雨参考.prism 里是 406 个共 123.9 MB。

而它们只在三处用得到：

  - 文档面板显示内嵌图片
  - 脑图显示内嵌图片
  - 导出（XMind / DOCX / HTML）

打开工程时一处都用不到。所以这里只保留 id -> (name, mime) 的索引，
blob 在真正取的时候才读。

继承 MutableMapping，所以 get / keys / items / in / len / 遍历 / 下标
读写全都和普通 dict 一样 —— 文档面板、脑图面板和导出那边不用改一行。
"""
import collections.abc
import logging
import sqlite3

logger = logging.getLogger(__name__)


def sqlite_attachment_loader(filename):
    """构造一个按 id 读附件 blob 的加载器。

    每次读开一个只读连接再关掉，而不是常驻一个。常驻连接会一直占着
    工程文件，用户想移动、改名或备份它就做不到；开一次连接大约 1 毫秒，
    而附件不是热路径 —— 只在显示文档或脑图时才读。
    """
    def load(attachment_id):
        connection = None
        try:
            connection = sqlite3.connect(
                f'file:{filename}?mode=ro', uri=True)
            row = connection.execute(
                'SELECT blob FROM attachments WHERE id = ?',
                (attachment_id,)).fetchone()
            return row[0] if row else None
        except sqlite3.Error:
            logger.warning('Could not read attachment %s from %s',
                           attachment_id, filename, exc_info=True)
            return None
        finally:
            if connection is not None:
                connection.close()
    return load


class StoredAttachments(collections.abc.MutableMapping):
    """A dict-shaped view over attachments whose blobs live in the file.

    ``_meta`` holds ``id -> (name, mime)`` for everything in the project;
    ``_cache`` holds entries whose blob has already been read.  Reading is
    deferred to ``_loader`` so opening a project does not pay for pictures
    nobody is looking at.
    """

    def __init__(self, loader=None, entries=None):
        self._meta = {}
        self._cache = {}
        self._loader = loader
        if entries:
            for key, value in entries.items():
                self[key] = value

    # ── 读 ────────────────────────────────────────────────────────

    def __getitem__(self, key):
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        if key not in self._meta:
            raise KeyError(key)
        name, mime = self._meta[key]
        blob = self._loader(key) if self._loader is not None else None
        entry = (name, mime, blob)
        self._cache[key] = entry
        return entry

    # ── 写 ────────────────────────────────────────────────────────

    def __setitem__(self, key, value):
        name, mime, blob = value
        self._meta[key] = (name, mime)
        self._cache[key] = (name, mime, blob)

    def __delitem__(self, key):
        del self._meta[key]
        self._cache.pop(key, None)

    # ── 形状 ──────────────────────────────────────────────────────

    def __iter__(self):
        return iter(self._meta)

    def __len__(self):
        return len(self._meta)

    def __contains__(self, key):
        # 只查索引，不触发读取 —— `id in attachments` 不该有 IO 代价
        return key in self._meta

    def get(self, key, default=None):
        try:
            return self[key]
        except KeyError:
            return default

    def keys(self):
        return self._meta.keys()

    def values(self):
        for key in self._meta:
            yield self[key]

    def items(self):
        """遍历会把 blob 读一遍 —— 那是保存路径的成本，不是打开的。

        ``write_board_extras()`` 要写出全部附件，所以这条路上必然要读。
        保持默认行为，不额外优化：保存本来就要把每个 blob 写进文件。

        **遍历的是 ``_meta`` 的副本。** 调用方一边遍历一边写（保存时
        新增的附件会进 ``_meta``），遍历原字典会抛
        ``RuntimeError: dictionary changed size during iteration`` ——
        日志里出现过 10 次，每次都让保存半途而废。
        """
        for key in list(self._meta):
            yield key, self[key]

    # ── 便于排查 ──────────────────────────────────────────────────

    def loaded_count(self):
        """已经读进内存的条目数（测试和排查用）。"""
        return len(self._cache)

    def total_loaded_bytes(self):
        total = 0
        for entry in self._cache.values():
            blob = entry[2] if entry else None
            if blob:
                total += len(blob)
        return total

    def __repr__(self):
        return (f'<StoredAttachments {len(self._meta)} 条，'
                f'已读 {len(self._cache)} 条>')


def load_attachments(io_object, board_id=0, filename=None):
    """Read attachment metadata only, leaving the blobs in the file.

    ``io_object`` is the SQLiteIO doing the loading (for its cursor), and
    ``filename`` is the project path the loader will read from later.
    """
    loader = sqlite_attachment_loader(filename) if filename else None
    store = StoredAttachments(loader=loader)
    for row in io_object.fetchall(
            'SELECT id, name, mime FROM attachments WHERE board_id = ?',
            (board_id,)):
        store._meta[row[0]] = (row[1], row[2])
    return store
