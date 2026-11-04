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
import itertools
import logging
import os
import threading
from collections import OrderedDict
from pathlib import Path
import sqlite3

logger = logging.getLogger(__name__)

#: 每张附件表一个编号。缓存的键里要能认出「同一张表」，否则两个工程的
#: 同号附件会互相串味。
_STORE_UIDS = itertools.count(1)

# 单个工程的附件 blob 驻留上限（字节）。附件可以随时按 id 从工程文件重读，
# 因此这里只保留最近使用的一批；参考工程有 406 个附件共 123.9 MB，
# 无上限的 dict 会把它们全部留在内存里。
DEFAULT_CACHE_BYTES = 32 * 1024 * 1024


def default_cache_bytes():
    try:
        mb = int(os.environ.get('PRISM_ATTACHMENT_CACHE_MB', '32'))
    except ValueError:
        mb = 32
    return max(1, mb) * 1024 * 1024


def sqlite_attachment_loader(filename):
    """构造一个按 id 读附件 blob 的加载器。

    每次读开一个只读连接再关掉，而不是常驻一个。常驻连接会一直占着
    工程文件，用户想移动、改名或备份它就做不到；开一次连接大约 1 毫秒，
    而附件不是热路径 —— 只在显示文档或脑图时才读。
    """
    def load(attachment_id):
        from prism.storage.managed import root_for, read_bytes
        if root_for(filename) is not None:
            return read_bytes(filename, 'attachments', attachment_id)
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
    load.source_path = str(Path(filename).resolve())
    return load


class StoredAttachments(collections.abc.MutableMapping):
    """A dict-shaped view over attachments whose blobs live in the file.

    ``_meta`` holds ``id -> (name, mime)`` for everything in the project;
    ``_cache`` holds entries whose blob has already been read.  Reading is
    deferred to ``_loader`` so opening a project does not pay for pictures
    nobody is looking at.

    The resident blobs are bounded: ``_cache`` is an LRU capped by
    ``_cache_limit`` bytes, and the oldest clean entries are dropped when the
    cap is exceeded.  Dirty entries (written in this session) are never
    dropped -- they would otherwise be lost before the next save.
    """

    def __init__(self, loader=None, entries=None, cache_bytes=None):
        self._meta = {}
        self._cache = OrderedDict()
        self._cache_bytes = 0
        self._dirty = set()
        self._loader = loader
        self._cache_limit = default_cache_bytes() if cache_bytes is None else max(0, int(cache_bytes))
        # 后台线程会读这里的 blob（脑图把图片读出来编码时），所以驻留缓存
        # 的读写必须互斥：`_cache_bytes` 的记账是「先减后加」，两个线程同时
        # 改就会算错，淘汰也会把别人正在用的条目丢掉。
        self._lock = threading.RLock()
        # 每个条目的内容版本号。写入即换号，所以缓存可以按它判断
        # 「这份字节还是不是原来那份」。
        self._versions = {}
        self._version_counter = 0
        self._uid = next(_STORE_UIDS)
        if entries:
            for key, value in entries.items():
                self[key] = value
        try:
            from prism.resource_budget import shared_cache_budget
            shared_cache_budget().register(self, category='attachment_bytes',
                                           usage='total_loaded_bytes',
                                           trim='_trim_cache_to', priority=20)
        except Exception:  # pragma: no cover - the budget is best effort
            logger.debug('Attachment cache budget unavailable', exc_info=True)

    # ── 读 ────────────────────────────────────────────────────────

    def __getitem__(self, key):
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            if key not in self._meta:
                raise KeyError(key)
            name, mime = self._meta[key]
        blob = self._loader(key) if self._loader is not None else None
        entry = (name, mime, blob)
        # A transient SQLite lock must not poison the cache with a missing blob.
        if blob is not None:
            with self._lock:
                # 读文件的这段时间里可能有人写了同一个条目：那份才是最新的，
                # 缓存和返回值都用它。
                cached = self._cache.get(key)
                if cached is not None:
                    self._cache.move_to_end(key)
                    return cached
                self._store(key, entry)
        return entry

    def read_bytes(self, key, default=None):
        """The bytes of one entry, read on demand, cache left alone.

        This is the read-on-demand interface the background workers use:
        it never stores what it read, so a worker reading a picture while
        the GUI thread stores another one cannot disturb the resident
        cache or its byte accounting.

        ``default`` (None) means the table holds no such entry; a tuple
        whose blob is None means it holds one whose bytes cannot be read.
        The two are different failures and callers must be able to tell
        them apart.
        """
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
                return entry[1], entry[2]
            if key not in self._meta:
                return default
            name, mime = self._meta[key]
        blob = self._loader(key) if self._loader is not None else None
        with self._lock:
            entry = self._cache.get(key)
            if entry is not None:
                self._cache.move_to_end(key)
                return entry[1], entry[2]
        return (mime or 'image/png'), blob

    def source_key(self):
        """Identity of the place these bytes come from.

        Two tables that share a source hold the same bytes, so a cache
        keyed by this may be shared between them.  A table keeps its own
        identity for good: re-binding it to another project file (Save As)
        does not change the content, so the cache stays valid.
        """
        return (self._uid, getattr(self._loader, 'source_path', None))

    def content_version(self, key):
        """A token that changes whenever this entry's bytes may have changed."""
        with self._lock:
            return self._versions.get(key, 0)

    def mime_of(self, key, default=None):
        """The mime type one entry is stored under, without reading its bytes."""
        with self._lock:
            meta = self._meta.get(key)
        if meta is None:
            return default
        return meta[1] or 'image/png'

    # ── 写 ────────────────────────────────────────────────────────

    def __setitem__(self, key, value):
        name, mime, blob = value
        with self._lock:
            self._meta[key] = (name, mime)
            self._version_counter += 1
            self._versions[key] = self._version_counter
            self._store(key, (name, mime, blob))
            self._dirty.add(key)

    def __delitem__(self, key):
        with self._lock:
            del self._meta[key]
            self._versions.pop(key, None)
            self._forget(key)
            self._dirty.discard(key)

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
            # A miss here is what a card eventually shows as "no image"; the
            # caller only sees `default`, so this is the only place that knows
            # the difference between "not there" and "empty".
            try:
                from prism.performance_trace import record as _record
                _record('material_attachment_miss', attachment=key,
                        cached=bool(getattr(self, '_meta', {}).get(key)))
            except Exception:                      # noqa: BLE001 - logging only
                pass
            return default

    def peek(self, key, default=None):
        """Read an already-resident entry without triggering disk IO."""
        with self._lock:
            cached = self._cache.get(key, default)
            if cached is not None:
                self._cache.move_to_end(key)
            return cached

    def read_uncached(self, key, default=None):
        """Stream a persisted source for background rendering/export.

        Newly imported/modified entries remain authoritative in _cache. Clean
        file-backed originals need not remain resident after generating a proxy.
        """
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            if key not in self._meta:
                return default
            name, mime = self._meta[key]
        blob = self._loader(key) if self._loader is not None else None
        return name, mime, blob

    def keys(self):
        return self._meta.keys()

    def snapshot_for_save(self):
        """Detach the save's membership/revisions without loading clean blobs."""
        snapshot = StoredAttachments(loader=self._loader, cache_bytes=self._cache_limit)
        with self._lock:
            snapshot._meta = dict(self._meta)
            snapshot._cache = OrderedDict(self._cache)
            snapshot._cache_bytes = self._cache_bytes
            snapshot._dirty = set(self._dirty)
            snapshot._versions = dict(self._versions)
            snapshot._version_counter = self._version_counter
        return snapshot

    def entries_for_save(self, connection, filename):
        """Leave unchanged blobs in place; never read our own write lock.

        SQLite can take an exclusive lock when a large transaction spills its
        page cache. A second read connection then waits on this very writer.
        Save As still streams originals from the other source file.
        """
        source = getattr(self._loader, 'source_path', None)
        same_file = source is not None and Path(source).resolve() == Path(filename).resolve()
        for key in list(self._meta):
            if same_file and key not in self._dirty:
                if connection.execute('SELECT 1 FROM attachments WHERE id=?', (key,)).fetchone():
                    continue
                raise ValueError(f'Original attachment {key} is missing; save stopped')
            entry = self.read_uncached(key)
            if entry is None or entry[2] is None:
                raise ValueError(f'Original attachment {key} is unreadable; save stopped')
            yield key, entry

    def relocate_source(self, old_path, new_path):
        source = getattr(self._loader, 'source_path', None)
        if source is not None and Path(source).resolve() == Path(old_path).resolve():
            self._loader = sqlite_attachment_loader(str(new_path))

    def saved_to(self, filename, versions):
        """Rebind lazy reads after Save As; retain edits made during the write."""
        self._loader = sqlite_attachment_loader(str(filename))
        for key, saved_entry in versions:
            if self._cache.get(key) is saved_entry:
                self._dirty.discard(key)

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
        """已经读进内存的 blob 字节数（测试和排查用）。

        记账与淘汰共用同一份数字：淘汰时减去、写入时加上，因此这里始终等于
        当前真正驻留的字节数。
        """
        return self._cache_bytes

    def cache_limit(self):
        """驻留上限（字节）。"""
        return self._cache_limit

    # ── 驻留上限与淘汰 ────────────────────────────────────────────

    @staticmethod
    def _blob_size(entry):
        blob = entry[2] if entry else None
        return len(blob) if blob else 0

    def _store(self, key, entry):
        """记住一个已读入的条目，并按上限淘汰最久未用的干净条目。"""
        with self._lock:
            previous = self._cache.pop(key, None)
            if previous is not None:
                self._cache_bytes -= self._blob_size(previous)
            self._cache[key] = entry
            self._cache_bytes += self._blob_size(entry)
            if self._cache_bytes > self._cache_limit:
                self._trim_cache_to(self._cache_limit, keep=key)

    def _forget(self, key):
        with self._lock:
            entry = self._cache.pop(key, None)
            if entry is not None:
                self._cache_bytes -= self._blob_size(entry)

    def _trim_cache_to(self, target_bytes, keep=None):
        """把驻留 blob 压到 ``target_bytes`` 以下（LRU）。

        本次会话写入的条目（``_dirty``）绝不淘汰：它们还没落盘，丢掉就等于
        丢数据。干净条目可以随时按 id 从工程文件重读。``keep`` 是刚写入、
        正在返回给调用方的条目，不参与本轮淘汰。
        """
        with self._lock:
            for key in list(self._cache.keys()):
                if self._cache_bytes <= max(0, int(target_bytes)):
                    break
                if key in self._dirty or key == keep:
                    continue
                self._forget(key)

    def __getstate__(self):
        """A copy of this table must not carry the lock with it."""
        state = dict(self.__dict__)
        state.pop('_lock', None)
        return state

    def __setstate__(self, state):
        self.__dict__.update(state)
        self._lock = threading.RLock()

    def __repr__(self):
        return (f'<StoredAttachments {len(self._meta)} 条，'
                f'已读 {len(self._cache)} 条，{self._cache_bytes:,} 字节>')


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


# ── 按需读取的统一入口 ──────────────────────────────────────────────
#
# 场景里的附件表有两种：存过盘的工程是 StoredAttachments（blob 在文件里，
# 读一次要开一次 SQLite），还没存过的是普通 dict（blob 就在内存里）。
# 后台线程要用同一套办法读两者，所以这层只认「像 dict 的表」。


def attachment_source_key(attachments):
    """Identity of the table a picture's bytes come from.

    Two tables that share a source hold the same bytes, so a cache keyed
    by this may be shared between them.
    """
    if attachments is None:
        return None
    source = getattr(attachments, 'source_key', None)
    if callable(source):
        return source()
    return ('mapping', id(attachments))


def attachment_content_version(attachments, key):
    """A token that changes whenever this entry's bytes may have changed.

    For a file-backed table this is a counter the table bumps on every
    write, so it costs nothing to ask.  For a plain dict the bytes are
    already in memory, so their hash stands in for the counter.
    """
    if attachments is None:
        return None
    version = getattr(attachments, 'content_version', None)
    if callable(version):
        return version(key)
    entry = attachments.get(key)
    if entry is None or len(entry) != 3:
        return None
    name, mime, blob = entry
    return (name, mime, hash(blob) if blob is not None else None)


def attachment_mime(attachments, key):
    """The mime type one entry is stored under, without reading its bytes."""
    if attachments is None:
        return None
    mime = getattr(attachments, 'mime_of', None)
    if callable(mime):
        return mime(key)
    entry = attachments.get(key)
    if entry is None or len(entry) != 3:
        return None
    return entry[1] or 'image/png'


def read_attachment_bytes(attachments, key):
    """One attachment's ``(mime, blob)``, read on demand.

    ``None`` means the table holds no such entry.  ``(mime, None)`` means
    it holds one whose bytes could not be read.  Callers must be able to
    tell the two apart: a picture that is not there may be skipped, one
    that cannot be read has to be reported.
    """
    if attachments is None:
        return None
    reader = getattr(attachments, 'read_bytes', None)
    if callable(reader):
        return reader(key)
    entry = attachments.get(key)
    if entry is None or len(entry) != 3:
        return None
    name, mime, blob = entry
    return (mime or 'image/png'), blob
