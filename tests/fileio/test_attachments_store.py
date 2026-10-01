"""T5：附件按需加载。

任务书要求打开工程时「不得加载所有文档正文」。附件是这条线上最大的一块
—— 秋雨参考.prism 里 406 个附件共 123.9 MB，而打开工程时一张都不需要
显示：它们只在文档面板、脑图面板和导出时用到。

这些测试钉住的核心只有一句：**不取就不读。**
"""
import os
import sqlite3

import pytest

from prism.fileio.attachments_store import (StoredAttachments,
                                            load_attachments,
                                            sqlite_attachment_loader)


class CountingLoader:
    """记录每次读取的假加载器。"""

    def __init__(self, blobs=None):
        self.blobs = blobs or {}
        self.calls = []

    def __call__(self, key):
        self.calls.append(key)
        return self.blobs.get(key)


def _store():
    loader = CountingLoader({key: b'blob-%d' % key for key in (1, 2, 3)})
    store = StoredAttachments(loader=loader)
    for key, name, mime in ((1, 'a.png', 'image/png'),
                            (2, 'b.jpg', 'image/jpeg'),
                            (3, 'c.gif', 'image/gif')):
        store._meta[key] = (name, mime)
    return store, loader


# ── 核心：不取就不读 ─────────────────────────────────────────────

def test_constructing_reads_nothing():
    _, loader = _store()
    assert loader.calls == []


def test_reading_one_entry_reads_only_that_one():
    store, loader = _store()

    entry = store[1]

    assert entry == ('a.png', 'image/png', b'blob-1')
    assert loader.calls == [1], '只该读被取的那一个'


def test_membership_never_triggers_a_read():
    store, loader = _store()
    assert 1 in store
    assert 99 not in store
    assert loader.calls == [], '`in` 不该有 IO 代价'


def test_length_and_keys_never_trigger_reads():
    store, loader = _store()
    assert len(store) == 3
    assert sorted(store.keys()) == [1, 2, 3]
    assert loader.calls == []


def test_iterating_keys_never_triggers_reads():
    store, loader = _store()
    assert sorted(iter(store)) == [1, 2, 3]
    assert loader.calls == [], '遍历键不该读 blob'


def test_repeated_reads_hit_the_cache():
    store, loader = _store()
    store[1]
    store[1]
    store[1]
    assert loader.calls == [1], '同一个附件只该读一次'


# ── 写入 ─────────────────────────────────────────────────────────

def test_setting_an_entry_does_not_go_back_to_the_loader():
    store, loader = _store()
    store[9] = ('new.png', 'image/png', b'fresh')

    assert store[9] == ('new.png', 'image/png', b'fresh')
    assert loader.calls == [], '刚写进去的不该再去读'


def test_a_written_entry_shows_up_in_the_shape():
    store, _ = _store()
    store[9] = ('new.png', 'image/png', b'fresh')

    assert 9 in store
    assert len(store) == 4
    assert 9 in list(store.keys())


def test_deleting_drops_it_from_both_sides():
    store, loader = _store()
    store[1]                       # 先读一次，进缓存
    del store[1]

    assert 1 not in store
    assert len(store) == 2
    assert 1 not in store._cache
    assert loader.calls == [1]


# ── 形状要像 dict ────────────────────────────────────────────────

def test_get_returns_the_default_without_reading():
    store, loader = _store()
    assert store.get(99) is None
    assert store.get(99, 'x') == 'x'
    assert loader.calls == [], '取不存在的键不该去读'


def test_a_missing_key_raises_keyerror():
    store, _ = _store()
    with pytest.raises(KeyError):
        store[99]                                          # noqa: B018


def test_items_reads_everything():
    """保存路径要走 items()，那时全读是正确的 —— 本来就要写出去。"""
    store, loader = _store()
    pairs = dict(store.items())

    assert sorted(pairs) == [1, 2, 3]
    assert sorted(loader.calls) == [1, 2, 3]


def test_max_of_keys_works_like_a_dict():
    """view.py 用 max(attachments.keys(), default=0) + 1 分配新 id。"""
    store, _ = _store()
    assert max(store.keys(), default=0) + 1 == 4


def test_an_empty_store_looks_falsy():
    """sql.py 里写着 `or {}`，空的必须是假值。"""
    store = StoredAttachments()
    assert not store
    assert len(store) == 0


def test_loaded_count_tracks_what_was_read():
    store, _ = _store()
    assert store.loaded_count() == 0
    store[2]
    assert store.loaded_count() == 1
    assert store.total_loaded_bytes() == len(b'blob-2')


# ── 读不出来不能崩 ───────────────────────────────────────────────

def test_a_loader_returning_none_yields_none():
    store = StoredAttachments(loader=lambda key: None)
    store._meta[1] = ('gone.png', 'image/png')
    assert store[1] == ('gone.png', 'image/png', None)


def test_a_raising_loader_is_not_swallowed():
    """异常要原样穿出去 —— 静默吞掉会让调用方以为附件本来就是空的。"""
    def broken(key):
        raise RuntimeError('读不出来')

    store = StoredAttachments(loader=broken)
    store._meta[1] = ('x.png', 'image/png')
    with pytest.raises(RuntimeError):
        store[1]                                          # noqa: B018


def test_no_loader_still_returns_the_metadata():
    store = StoredAttachments()
    store._meta[1] = ('x.png', 'image/png')
    assert store[1] == ('x.png', 'image/png', None)


# ── 真实 sqlite 往返 ─────────────────────────────────────────────

def _make_db(path, blobs):
    connection = sqlite3.connect(str(path))
    connection.execute('CREATE TABLE attachments (id INTEGER PRIMARY KEY, '
                       'name TEXT, mime TEXT, blob BLOB)')
    for key, name, mime, blob in blobs:
        connection.execute('INSERT INTO attachments VALUES (?, ?, ?, ?)',
                           (key, name, mime, blob))
    connection.commit()
    connection.close()


def test_sqlite_loader_reads_a_real_blob(tmp_path):
    path = tmp_path / 'att.db'
    _make_db(path, [(1, 'a.png', 'image/png', b'real-bytes')])

    loader = sqlite_attachment_loader(str(path))

    assert loader(1) == b'real-bytes'
    assert loader(99) is None, '查不到要返回 None，不是抛'


def test_sqlite_loader_survives_a_missing_file(tmp_path):
    loader = sqlite_attachment_loader(str(tmp_path / 'nope.db'))
    assert loader(1) is None, '文件没了也要返回 None，不能崩'


def test_sqlite_loader_does_not_hold_the_file_open(tmp_path):
    """读完就要放开文件，否则用户移动或备份工程会被挡住。"""
    path = tmp_path / 'att2.db'
    _make_db(path, [(1, 'a.png', 'image/png', b'x')])

    sqlite_attachment_loader(str(path))(1)

    moved = tmp_path / 'att2-moved.db'
    os.replace(str(path), str(moved))       # 文件被占着这句会失败
    assert moved.exists()


# ── load_attachments：只查元数据 ─────────────────────────────────

class _FakeIO:
    def __init__(self, rows):
        self.rows = rows
        self.queries = []

    def fetchall(self, sql, params=None):
        self.queries.append(sql)
        return self.rows


def test_loading_attachments_never_selects_the_blob_column():
    fake = _FakeIO([(1, 'a.png', 'image/png'), (2, 'b.jpg', 'image/jpeg')])

    store = load_attachments(fake, board_id=0, filename=None)

    assert len(store) == 2
    assert len(fake.queries) == 1
    assert 'blob' not in fake.queries[0].lower(), \
        f'加载时不该查 blob 列：{fake.queries[0]}'


def test_loading_attachments_leaves_the_blobs_unread():
    fake = _FakeIO([(1, 'a.png', 'image/png')])
    store = load_attachments(fake, board_id=0, filename=None)
    assert store.loaded_count() == 0, '加载完不该已经在内存里'
