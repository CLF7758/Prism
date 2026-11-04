"""`max_rows` 的口径守卫与红线守卫（A-1）。

`ProjectAssetCatalog.query()` 的排序键是 `(画布路径, 标题, tie-break)`，
而目录读取（`ProjectOpenScope.iter_query`）是**按 `id` 升序**的 keyset
分页。把 `max_rows` 直接前推给目录读取，拿到的是"按 id 序的前 N 行"，
不是"按排序键的前 N 行"——**静默返回错的结果，比慢更糟**。

这里钉死三件事：

1. `max_rows >= 真实命中数` 时，返回结果集与不传 `max_rows` 时**逐项相同**；
2. `max_rows < 真实命中数` 时，给出的是**按排序键**的前 N 行，**不是**按 id
   序的前 N 行；目录读取**没有**收到 `max_rows`（A-2 落地与否都不前推）；
3. 计数侧：`scope.summaries_grouped` 在场时走**一次**往返，缺席时退回逐条
   `summaries()`，**两条路径的计数逐项相同**。
"""
import json
import sqlite3
from types import SimpleNamespace

import pytest

from prism.project_catalog import (ProjectAssetCatalog, catalog_scope_counters,
                                   reset_catalog_scope_counters)
from prism.project_open_scope import ProjectOpenScope

#: 目录里的行。**id 顺序与排序键顺序故意不同**：画布 A（路径 'AAA'）排在
#: 画布 B（路径 'BBB'）前面，与 id 无关；A 内部按文件名排（mmm < zzz）。
#:
#: 画布 id 用**多字符**（`canvas-a` / `canvas-b`）：单字符 id 会让
#: `set('A') == {'A'}` 恰好等价，证伪不了"字符串被拆成字符"这个洞。
RECORDS = [
    (1, 'pixmap', dict(canvasId='canvas-b', filename='aaa.png')),
    (2, 'pixmap', dict(canvasId='canvas-a', filename='zzz.png')),
    (3, 'pixmap', dict(canvasId='canvas-b', filename='bbb.png')),
    (4, 'pixmap', dict(canvasId='canvas-a', filename='mmm.png')),
    (5, 'pixmap', dict(canvasId='canvas-b', filename='ccc.png')),
]

#: 按 `(画布路径, 标题, tie-break)` 排出来的完整顺序。
SORTED_IDS = ['4', '2', '1', '3', '5']

#: 按 id 升序的前两行——**如果有人把 `max_rows` 直接前推给目录读取，
#: 结果就会是这两行**（静默错答）。
ID_ORDER_PREFIX = ['1', '2']

#: 计数用例的目录行。
COUNT_RECORDS = [
    (1, 'pixmap', dict(canvasId='canvas-a', tags=[])),
    (2, 'pixmap', dict(canvasId='canvas-a', tags=['old'])),
    (3, 'pixmap', dict(canvasId='canvas-b', tags=[])),
]


def _source(tmp_path, records=RECORDS, name='max-rows.prism'):
    source = tmp_path / name
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,"
                         "x,y,z,scale,rotation,flip,data);"
                         "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key, kind, extra in records:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0,
                        json.dumps(extra, ensure_ascii=False)))
    return source


def _scope(tmp_path, scope_cls=ProjectOpenScope, records=RECORDS,
           name='max-rows.prism'):
    source = _source(tmp_path, records, name)
    scope = scope_cls(source, tmp_path / (name + '.sqlite'))
    assert scope.build().complete
    return scope


class _FakeScene:
    """目录分支会造显示代理，代理要 `weakref.ref(scene)` ⇒ 不能用 SimpleNamespace。"""

    def __init__(self, live=()):
        self._live = list(live)
        self.project_id = 'max-rows-test'
        self.workspace_pages = [dict(id='canvas-a', kind='canvas', title='AAA'),
                                dict(id='canvas-b', kind='canvas', title='BBB')]

    def items(self):
        return list(self._live)

    def iter_assets(self):
        return iter(list(self._live))


def _scene(live=()):
    return _FakeScene(live)


def _catalog(scope, scene=None):
    return ProjectAssetCatalog(scene or _scene(), None,
                               scope_provider=lambda: scope)


def _ids(results):
    return [str(result.item.save_id) for result in results]


def _shape(results):
    """结果集的逐项形状（路径、标题、画布、行标识）。"""
    return [(result.path, result.title, result.canvas_id,
             str(result.item.save_id)) for result in results]


# ── 1. `max_rows >= 真实命中数` ⇒ 结果集逐项相同 ────────────────────────

def test_max_rows_at_or_above_the_hit_count_returns_the_same_rows(tmp_path):
    catalog = _catalog(_scope(tmp_path))
    full = catalog.query()
    assert _ids(full) == SORTED_IDS

    for cap in (len(full), len(full) + 3, 100):
        capped = catalog.query(max_rows=cap)
        assert _ids(capped) == _ids(full)
        assert _shape(capped) == _shape(full)


def test_max_rows_does_not_change_the_scene_branch(tmp_path):
    """没有目录时（scene 口径）上限照样生效，且不影响排序。"""
    live = [SimpleNamespace(save_id=2, TYPE='pixmap', _canvas_id='canvas-a',
                             _tags=[], _deleted_at=None, isVisible=lambda: True,
                             _title='zzz', filename='zzz.png'),
            SimpleNamespace(save_id=1, TYPE='pixmap', _canvas_id='canvas-b',
                            _tags=[], _deleted_at=None, isVisible=lambda: True,
                            _title='aaa', filename='aaa.png')]
    catalog = ProjectAssetCatalog(_scene(live), None, scope_provider=lambda: None)
    assert _ids(catalog.query()) == ['2', '1']
    assert _ids(catalog.query(max_rows=1)) == ['2']
    assert _ids(catalog.query(max_rows=5)) == ['2', '1']


# ── 2. 红线：`max_rows` 不能变成"按 id 序的前 N 行" ─────────────────────

def test_max_rows_below_the_hit_count_keeps_the_sort_key_order(tmp_path):
    catalog = _catalog(_scope(tmp_path))
    capped = catalog.query(max_rows=2)
    assert _ids(capped) == SORTED_IDS[:2]
    # 按 id 序的前两行是 1、2。前推 `max_rows` 就会得到这两个 —— 错的。
    assert _ids(capped) != ID_ORDER_PREFIX


class _SpyScope(ProjectOpenScope):
    """A-2 已落地：`iter_query` 有具名的 `max_rows` 形参。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = []

    def iter_query(self, *, cancel=None, limit=512, max_rows=None, **filters):
        self.calls.append({'max_rows': max_rows, 'limit': limit,
                           'filters': dict(filters)})
        return super().iter_query(cancel=cancel, limit=limit, **filters)


class _LegacyScope(ProjectOpenScope):
    """A-2 还没落地：`iter_query` 没有 `max_rows` 形参。"""

    def iter_query(self, *, cancel=None, limit=512, **filters):
        return super().iter_query(cancel=cancel, limit=limit, **filters)


def test_the_catalog_read_never_receives_max_rows(tmp_path):
    """目录读取**没有**收到 `max_rows`——即使 scope 支持它。"""
    scope = _scope(tmp_path, _SpyScope)
    catalog = _catalog(scope)
    assert _ids(catalog.query(max_rows=2)) == SORTED_IDS[:2]
    assert scope.calls, 'the directory branch must have been used'
    assert all(call['max_rows'] is None for call in scope.calls)


def test_max_rows_works_when_the_scope_has_no_max_rows_parameter(tmp_path):
    """A-2 未落地：不崩、不改行为。"""
    scope = _scope(tmp_path, _LegacyScope)
    catalog = _catalog(scope)
    assert _ids(catalog.query()) == SORTED_IDS
    assert _ids(catalog.query(max_rows=2)) == SORTED_IDS[:2]
    assert _ids(catalog.query(max_rows=len(SORTED_IDS))) == SORTED_IDS


def test_not_pushing_max_rows_is_observable(tmp_path):
    """"没有前推"这件事本身要能被验收组看到。"""
    scope = _scope(tmp_path, _SpyScope)
    catalog = _catalog(scope)
    reset_catalog_scope_counters()
    catalog.query(max_rows=2)
    counters = catalog_scope_counters()
    assert counters.get('catalog_max_rows_not_pushed') == 1
    assert counters.get(
        'catalog_max_rows_not_pushed:sort_key_not_id_order') == 1


def test_a_scope_without_max_rows_is_not_reported_as_pushed_or_not(tmp_path):
    """能力探测失败时既不前推，也不谎报"前推被拒"（A-2 未落地）。"""
    scope = _scope(tmp_path, _LegacyScope)
    catalog = _catalog(scope)
    reset_catalog_scope_counters()
    catalog.query(max_rows=2)
    counters = catalog_scope_counters()
    assert 'catalog_max_rows_not_pushed' not in counters


# ── 3. `max_rows` 的参数契约 ───────────────────────────────────────────

@pytest.mark.parametrize('value', ['10', 1.5, True, [10]])
def test_max_rows_rejects_values_that_are_not_integers(tmp_path, value):
    catalog = _catalog(_scope(tmp_path))
    with pytest.raises(TypeError):
        catalog.query(max_rows=value)


@pytest.mark.parametrize('value', [0, -1])
def test_max_rows_rejects_caps_below_one(tmp_path, value):
    catalog = _catalog(_scope(tmp_path))
    with pytest.raises(ValueError):
        catalog.query(max_rows=value)


def test_max_rows_and_limit_take_the_smaller_cap(tmp_path):
    catalog = _catalog(_scope(tmp_path))
    assert _ids(catalog.query(max_rows=2, limit=3)) == SORTED_IDS[:2]
    assert _ids(catalog.query(max_rows=4, limit=2)) == SORTED_IDS[:2]
    assert _ids(catalog.query(max_rows=4, limit=4)) == SORTED_IDS[:4]


# ── 5. 画布范围：裸字符串 = 一个画布 id（不是字符集合） ──────────────────
#
# 旧行为：`set(canvas_scope)` 把 'canvas-a' 拆成
# `{'c','a','n','v','s','-','a'}` ⇒ 匹配不到任何行 ⇒ **0 行且不报错**
# （静默错答）。口径必须与 scope 层 `_as_canvas_ids`、索引层
# `_canvas_id_tuple` 一致：裸字符串 = 一个画布 id。

def test_a_string_canvas_scope_selects_that_canvas(tmp_path):
    catalog = _catalog(_scope(tmp_path))
    rows = catalog.query(canvas_scope='canvas-a')
    assert rows, 'a string canvas scope selected nothing -- the silent empty answer'
    assert {row.canvas_id for row in rows} == {'canvas-a'}
    assert _ids(rows) == ['4', '2']

    other = catalog.query(canvas_scope='canvas-b')
    assert {row.canvas_id for row in other} == {'canvas-b'}
    assert _ids(other) == ['1', '3', '5']


def test_a_string_canvas_scope_matches_the_equivalent_list(tmp_path):
    """字符串与等价列表必须给出**逐项相同**的结果集。"""
    catalog = _catalog(_scope(tmp_path))
    from_string = catalog.query(canvas_scope='canvas-a')
    from_list = catalog.query(canvas_scope=['canvas-a'])
    assert _ids(from_string) == _ids(from_list)
    assert [(r.path, r.title, r.canvas_id) for r in from_string] == \
           [(r.path, r.title, r.canvas_id) for r in from_list]


def test_a_string_canvas_scope_works_in_the_scene_branch(tmp_path):
    """内存分支同样不能把字符串拆成字符（否则两边口径会漂）。"""
    live = [SimpleNamespace(save_id=2, TYPE='pixmap', _canvas_id='canvas-a',
                             _tags=[], _deleted_at=None, isVisible=lambda: True,
                             _title='zzz', filename='zzz.png'),
            SimpleNamespace(save_id=1, TYPE='pixmap', _canvas_id='canvas-b',
                            _tags=[], _deleted_at=None, isVisible=lambda: True,
                            _title='aaa', filename='aaa.png')]
    catalog = ProjectAssetCatalog(_scene(live), None, scope_provider=lambda: None)
    assert _ids(catalog.query(canvas_scope='canvas-a')) == ['2']
    assert _ids(catalog.query(canvas_scope='canvas-b')) == ['1']


@pytest.mark.parametrize('value', [42, 3.5, object()])
def test_a_non_iterable_canvas_scope_is_rejected(tmp_path, value):
    catalog = _catalog(_scope(tmp_path))
    with pytest.raises(TypeError):
        catalog.query(canvas_scope=value)


@pytest.mark.parametrize('value', [[1, 2], ('canvas-a', 3), [b'canvas-a']])
def test_non_string_canvas_scope_entries_are_rejected(tmp_path, value):
    catalog = _catalog(_scope(tmp_path))
    with pytest.raises(TypeError):
        catalog.query(canvas_scope=value)


# ── 4. 计数侧：批量摘要与逐条摘要的口径一致 ────────────────────────────

class _CountSpyScope(ProjectOpenScope):
    """记录三条摘要路径的调用次数。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.summary_calls = []
        self.grouped_calls = []
        self.bucket_calls = []
        self.uncovered_calls = []

    def summaries(self, item_ids, *, cancel=None):
        self.summary_calls.append(list(item_ids))
        return super().summaries(item_ids, cancel=cancel)

    def summaries_grouped(self, item_ids, *, cancel=None):
        self.grouped_calls.append(list(item_ids))
        return super().summaries_grouped(item_ids, cancel=cancel)

    def summaries_buckets(self, item_ids, *, cancel=None):
        self.bucket_calls.append(list(item_ids))
        return super().summaries_buckets(item_ids, cancel=cancel)

    def summaries_uncovered(self, item_ids, *, cancel=None):
        """scope 层没转发这个接口，这里直接用目录层的那一个（A-3）。"""
        self.uncovered_calls.append(list(item_ids))
        return self.index.summaries_uncovered(item_ids)


def _count_scene():
    live = [SimpleNamespace(save_id=1, TYPE='pixmap', _canvas_id='canvas-a',
                             _tags=['edited'], _deleted_at=None,
                             isVisible=lambda: True)]
    return _scene(live)


def test_counts_use_one_round_trip_and_match_the_per_row_path(tmp_path,
                                                               monkeypatch):
    scope = _scope(tmp_path, _CountSpyScope, COUNT_RECORDS, 'count.prism')
    catalog = _catalog(scope, _count_scene())

    grouped = catalog.count_snapshot()
    # 计数桶路径：一次往返；逐条路径与批量逐行路径**一次都没走**。
    assert len(scope.bucket_calls) == 1
    assert scope.summary_calls == []
    assert scope.grouped_calls == []

    # 能力缺失：退回逐条 `summaries()`，计数必须**逐项相同**。
    monkeypatch.setattr(scope, 'summaries_buckets', None)
    monkeypatch.setattr(scope, 'summaries_grouped', None)
    fallback = catalog.count_snapshot()
    assert scope.summary_calls, 'the per-row fallback must be used'
    assert fallback == grouped

    # 口径本身：3 条素材（1 条被会话覆盖：磁盘行被覆盖层顶掉），1 条无标签。
    assert grouped['total'] == 3
    assert grouped['untagged'] == 1
    assert grouped['canvases'] == {'canvas-a': 2, 'canvas-b': 1}


def test_counts_match_when_only_the_batch_row_api_exists(tmp_path, monkeypatch):
    """只有批量逐行接口时：一次往返 + 本层算桶，口径仍然相同。"""
    scope = _scope(tmp_path, _CountSpyScope, COUNT_RECORDS, 'count-batch.prism')
    catalog = _catalog(scope, _count_scene())

    grouped = catalog.count_snapshot()
    assert len(scope.bucket_calls) == 1

    monkeypatch.setattr(scope, 'summaries_buckets', None)
    batched = catalog.count_snapshot()
    assert len(scope.grouped_calls) == 1
    assert scope.summary_calls == []
    assert batched == grouped


def test_all_three_reconciliation_paths_give_the_same_numbers(tmp_path,
                                                               monkeypatch):
    """覆盖边 / 未覆盖边 / 逐条边：**数字必须完全一样**。

    覆盖过半时本层改读"没被覆盖"的那一边（成本 ∝ 剩余数）；两条边的桶
    口径一致，选哪条只影响耗时。
    """
    records = [(1, 'pixmap', dict(canvasId='canvas-a', tags=[])),
               (2, 'pixmap', dict(canvasId='canvas-a', tags=['old'])),
               (3, 'pixmap', dict(canvasId='canvas-b', tags=[])),
               (4, 'pixmap', dict(canvasId='canvas-b', tags=[]))]
    scope = _scope(tmp_path, _CountSpyScope, records, 'three-paths.prism')
    live = [SimpleNamespace(save_id=1, TYPE='pixmap', _canvas_id='canvas-a',
                             _tags=[], _deleted_at=None, isVisible=lambda: True),
            SimpleNamespace(save_id=2, TYPE='pixmap', _canvas_id='canvas-a',
                             _tags=['old'], _deleted_at=None, isVisible=lambda: True),
            SimpleNamespace(save_id=3, TYPE='pixmap', _canvas_id='canvas-b',
                             _tags=[], _deleted_at=None, isVisible=lambda: True)]
    catalog = _catalog(scope, _scene(live))

    # 1) 覆盖过半 ⇒ 走"没被覆盖"的那一边
    uncovered = catalog.count_snapshot()
    assert len(scope.uncovered_calls) == 1, 'the uncovered side must have been used'
    assert scope.bucket_calls == []
    assert scope.summary_calls == []

    # 2) 关掉未覆盖边 ⇒ 走"被覆盖"的那一边
    monkeypatch.setattr(scope, 'summaries_uncovered', None)
    monkeypatch.setattr(scope.index, 'summaries_uncovered', None)
    covered = catalog.count_snapshot()
    assert len(scope.bucket_calls) == 1
    assert scope.summary_calls == []
    assert covered == uncovered

    # 3) 只剩逐条边 ⇒ 旧路径
    monkeypatch.setattr(scope, 'summaries_buckets', None)
    monkeypatch.setattr(scope, 'summaries_grouped', None)
    per_row = catalog.count_snapshot()
    assert scope.summary_calls
    assert per_row == uncovered
