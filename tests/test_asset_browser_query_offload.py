"""`prism/widgets/asset_browser.py` —— 查询卸载（query offload）的**语义等价性**回归。

这一组用例证明的不是"跑得通"，而是"**结果与改前完全一致**"：

* **改动前**（`artifacts/wave2-baseline/prism/widgets/asset_browser.py` 的
  `refresh()`，39,113 B 副本）：全项目查询在 **GUI 线程**上一次同步做完 ——
  目录不可用时 `catalog.query()` 退化成场景内存扫描，十万级素材下就是秒级阻塞。
* **改动后**：目录不可用**且工程大**时改成"GUI 线程分片扫描 → 后台线程纯数据
  过滤排序 → 回 GUI 线程一次性装模型"；**工程不大时仍然走原来的同步路径**，
  语义与改动前逐字相同（结果当场可用，不改调用方的同步/异步预期）。

所以每条用例都拿**改动前的算法**当参照物：`_baseline_items()` 照抄基线
`refresh()` 的过滤与排序顺序，用**原始原语**（`tags.name_index`、
`catalog.query`、按对象身份的规则求交），**不复用**被测代码里的任何新逻辑 ——
比较的才是"结果是否一样"，而不是"两条新路径是不是一样"。

本文件**不改** `tests/test_asset_browser.py`（只参考它的 fixture 写法）。
"""
import pytest
from PyQt6 import QtGui

from prism import tags as tags_module
from prism.widgets import asset_browser as asset_browser_module
from prism.widgets.asset_browser import asset_name

from tests.test_asset_browser import photo


# ── 路径开关：同一个场景分别走"同步路径"与"分片扫描路径" ──────────────

def _force_synchronous_path(monkeypatch):
    """第一回合就把扫描源读空 → 走**同步**路径（`catalog.query()`）。

    改动后的判据是"第一回合读空了源 ⇒ 工程不大 ⇒ 回到同步路径"。把单回合
    条数上限抬到天上，第一回合必然读空。
    """
    monkeypatch.setattr(asset_browser_module, 'SCENE_SCAN_SLICE_ITEMS', 10 ** 9)


def _force_sliced_path(monkeypatch):
    """第一回合**读不空** → 真正分片扫描（后台过滤后异步回填）。

    单回合条数上限压到 1：第一回合最多处理 1 条，源还没读完 ⇒ 分片。
    """
    monkeypatch.setattr(asset_browser_module, 'SCENE_SCAN_SLICE_ITEMS', 1)


def _drain_scan(browser, limit=1000000):
    """在**当前线程**里把分片扫描跑完（不靠事件循环的定时器）。

    `_scene_scan_step` 每次只处理一个切片；这里反复调用，直到扫描源读完
    （`_scan_state` 变 None）。定时器里排队的那些回调随后触发时，会因为
    `_scan_state is None` 直接返回，不会重复扫描。
    """
    steps = 0
    while browser._scan_state is not None:
        browser._scene_scan_step()
        steps += 1
        assert steps < limit, 'scene scan did not finish'
    return steps


# ── 改动前算法的参照物 ────────────────────────────────────────────────

def _baseline_items(browser, *, search_text='', mode='all', scope_pages=None, rule=None):
    """**改动前** `refresh()` 的算法（原样照抄），当语义等价性的参照物。

    源：`artifacts/wave2-baseline/prism/widgets/asset_browser.py:268-344`。
    逐条照抄它的过滤与排序顺序，用**原始原语**（`tags.name_index`、
    `catalog.query`、按对象身份的规则求交）。这里刻意**不**调用被测代码里
    的 `_TagIndexCache` / `_RowWorker` / `_build_row` —— 否则比的就是
    "新实现和自己一致"，证明不了"与改前一致"。
    """
    panel = browser.view.category_panel
    catalog = browser.catalog
    system = tags_module.tag_system(browser.view.scene)
    text, expression = panel._split_query(search_text)
    kind = panel.search_kind
    if mode in ('unclassified', 'untagged') and kind == 'all':
        kind = 'asset'
    unified = mode != 'visual' and (bool(search_text.strip()) or kind not in ('all', 'asset'))
    results = catalog.query(text='' if unified else text, tag_expression=expression,
                             untagged_only=mode == 'untagged',
                             min_rating=panel._min_rating, canvas_scope=scope_pages)
    items = [r.item for r in results
             if panel._active_tags.issubset(tags_module.name_index(r.tags, system))]
    if mode == 'unclassified':
        known = catalog._known_canvas_ids()
        items = [i for i in items if catalog.is_unclassified(i, known)]
    if mode == 'recent':
        items = sorted([i for i in items if getattr(i, '_last_used', '')],
                       key=lambda i: i._last_used, reverse=True)
    if mode == 'random':
        import random
        random.Random(getattr(browser, '_random_seed', 0)).shuffle(items)
    if rule:
        # 改动前的写法：**第二次**查询，再按**对象身份**求交。
        matches = catalog.query(text=rule.get('text', ''),
                                 tag_expression=rule.get('tag_expression', ''),
                                 min_rating=int(rule.get('rating', 0)))
        allowed = {r.item for r in matches}
        items = [i for i in items if i in allowed]
    if scope_pages is not None:
        items = [i for i in items if (getattr(i, '_canvas_id', None) or 'default-canvas') in scope_pages]
    if unified:
        hits = [r for r in panel.project_search_results(search_text)
                if r['kind'] not in ('folder', 'tag')]
        allowed = set(items)
        asset_scope = mode != 'all' or rule or scope_pages is not None
        hits = [r for r in hits if
                (kind != 'asset' or r['kind'] == 'asset') and
                (r.get('item') in allowed if r['kind'] == 'asset' else
                 (not asset_scope or kind in ('document', 'mindmap', 'canvas')))]
        items = [r['item'] if r['kind'] == 'asset' else r for r in hits]
    if mode == 'visual':
        allowed = set(items)
        items = [item for item, _score in getattr(browser, 'visual_results', []) if item in allowed]
        if getattr(browser, 'visual_project', None) != browser.view.scene.project_id:
            items = []
    elif browser.sort_key == 'name':
        items.sort(key=lambda i: asset_name(i).casefold())
    elif browser.sort_key == 'rating':
        items.sort(key=lambda i: getattr(i, '_rating', 0), reverse=True)
    return items


# ── 可控的后台 worker：让测试自己决定"什么时候、以什么顺序交付" ────────

class _ControllableWorker(asset_browser_module._RowWorker):
    """假后台 worker：自己不跑，等测试决定什么时候交付。"""

    def run(self):
        """不自动交付 —— 真实 worker 的交付时机由测试控制。"""

    def deliver(self):
        """在 **GUI 线程**交付结果（与真实 worker 的排队信号同线程直达）。"""
        self.signals.result.emit((self.token, self._process(self.rows), None))


class _FakePool:
    """只收不跑：收集 worker，交付顺序由测试决定。"""

    def __init__(self):
        self.workers = []

    def start(self, worker):
        self.workers.append(worker)


def _use_controllable_workers(monkeypatch):
    pool = _FakePool()
    monkeypatch.setattr(asset_browser_module, '_RowWorker', _ControllableWorker)
    monkeypatch.setattr(asset_browser_module, 'catalog_thread_pool', lambda: pool)
    return pool


class _FakeIndex:
    """只带 `data(role)` 的假索引（`select_assets` 只用它取 UserRole）。"""

    def __init__(self, item):
        self._item = item

    def data(self, role):
        return self._item


# ── 场景 ──────────────────────────────────────────────────────────────

def _build_scene(window):
    """一个能区分各种筛选语义的小场景。

    * `alpha` 只含 "alpha"，`beta` 只含 "beta"，`both` 两个词都含 ——
      **多词 AND 与 OR 的区别**就在这里。
    * `red` / `blue` 标签分别挂在不同素材上。
    * `rated` 评分 3，其余 0。
    * `classified` 在一张**新建**画布上 → 已分类；默认画布上的算未分类。
    """
    page = window.workspace_service.add('canvas', 'Other')['id']
    scene = {
        'alpha': photo(window, 'default-canvas', 'Alpha', ['red']),
        'beta': photo(window, 'default-canvas', 'Beta', ['blue']),
        'both': photo(window, 'default-canvas', 'Alpha Beta', ['red', 'blue']),
        'rated': photo(window, 'default-canvas', 'Rated', ['red']),
        'classified': photo(window, page, 'Classified', ['red']),
    }
    scene['rated']._rating = 3
    # `both` 的评分 1：让"规则里的 rating >= 1"这条约束**真的能命中**，
    # 否则合并查询与原两次查询都会返回空集，等于没测。
    scene['both']._rating = 1
    # `recent` 模式用 `_last_used`：给两条素材不同的使用时间，其余没有 ——
    # 于是"最近使用"的结果既有内容也有**顺序**可查。
    scene['alpha']._last_used = '2026-01-01T00:00:00+00:00'
    scene['both']._last_used = '2026-02-02T00:00:00+00:00'
    return scene


SCENARIOS = [
    pytest.param({}, id='plain'),
    pytest.param({'rule': {'text': 'alpha beta'}}, id='rule-text-and'),
    pytest.param({'rule': {'tag_expression': 'blue'}}, id='rule-tag-expression'),
    pytest.param({'rule': {'text': 'alpha', 'tag_expression': 'blue', 'rating': 1}},
                 id='rule-all-three'),
    pytest.param({'min_rating': 3}, id='min-rating'),
    pytest.param({'active_tags': ['red']}, id='active-tags'),
    pytest.param({'mode': 'untagged'}, id='untagged'),
    pytest.param({'mode': 'unclassified'}, id='unclassified'),
    pytest.param({'scope_pages': ('default-canvas',)}, id='canvas-scope'),
    pytest.param({'search_text': 'alpha'}, id='unified-search'),
    # 下面两个是**顺序敏感**的模式：同步路径排的是 items，分片路径排的是
    # 纯数据行 —— 两条路径只要有一处顺序不一致，这里就会炸。
    pytest.param({'mode': 'recent'}, id='recent-order'),
    pytest.param({'mode': 'random'}, id='random-order'),
]


def _apply_scenario(browser, scenario):
    """把筛选条件装到浏览器上（只动浏览器自己的状态，不走 UI 菜单）。"""
    panel = browser.view.category_panel
    browser.saved_rule = scenario.get('rule')
    browser.search.setText(scenario.get('search_text', ''))
    browser.timer.stop()
    panel._min_rating = scenario.get('min_rating', 0)
    panel._active_tags.clear()
    panel._active_tags.update(scenario.get('active_tags', []))
    browser.scope_pages = scenario.get('scope_pages')
    browser.mode = scenario.get('mode', 'all')


# ── (a) 分片扫描路径 vs 同步路径：同一组 items，同一个顺序 ─────────────

def test_sliced_scan_and_synchronous_path_agree_on_items_and_order(main_window, qtbot, monkeypatch):
    """两条路径给出**同一批素材、同一个顺序**（不只是"都能出结果"）。"""
    window = main_window
    browser = window.asset_browser
    _build_scene(window)

    _force_synchronous_path(monkeypatch)
    browser.refresh()
    synchronous = list(browser.model.items)
    assert len(synchronous) == 5, 'the synchronous path must produce every row'

    browser.model.replace([])
    _force_sliced_path(monkeypatch)
    browser.refresh()
    _drain_scan(browser)
    qtbot.waitUntil(lambda: browser._row_worker is None, timeout=10000)
    assert list(browser.model.items) == synchronous


# ── (b) 筛选语义等价：逐条件对照**改前**行为 ─────────────────────────

@pytest.mark.parametrize('scenario', SCENARIOS)
def test_filter_semantics_match_the_pre_change_algorithm(main_window, qtbot, monkeypatch, scenario):
    """每种筛选条件下，两条路径都等于改动前的算法（集合相同、顺序相同）。"""
    window = main_window
    browser = window.asset_browser
    _build_scene(window)

    for path in ('sync', 'sliced'):
        _apply_scenario(browser, scenario)
        if path == 'sync':
            _force_synchronous_path(monkeypatch)
        else:
            _force_sliced_path(monkeypatch)
        browser.model.replace([])
        browser.refresh()
        if path == 'sync':
            got = list(browser.model.items)
        else:
            _drain_scan(browser)
            qtbot.waitUntil(lambda: browser._row_worker is None, timeout=10000)
            got = list(browser.model.items)
        reference = _baseline_items(
            browser,
            search_text=scenario.get('search_text', ''),
            mode=scenario.get('mode', 'all'),
            scope_pages=scenario.get('scope_pages'),
            rule=scenario.get('rule'))
        assert got == reference, f'{path} path differs from the pre-change algorithm'


def test_multi_word_text_is_conjunctive_not_disjunctive(main_window, monkeypatch):
    """多词文本是 **AND**：只命中一个词的素材必须被排除。

    这条单独钉死：若哪天把 AND 写成 OR，集合会变大，而上面的"两条路径
    一致"仍然会通过 —— 所以需要一个**独立于新实现**的强断言。
    """
    window = main_window
    browser = window.asset_browser
    scene = _build_scene(window)
    browser.saved_rule = {'text': 'alpha beta'}
    _force_synchronous_path(monkeypatch)
    browser.refresh()
    assert list(browser.model.items) == [scene['both']]
    _force_sliced_path(monkeypatch)
    browser.model.replace([])
    browser.refresh()
    _drain_scan(browser)
    assert [row.item for row in browser._row_worker.rows] == [scene['both']]


# ── (c) saved_rule 合并后 == 原「两次查询求交」 ──────────────────────

def test_saved_rule_merge_equals_the_original_two_query_intersection(main_window, monkeypatch):
    """合并成一次查询后，结果集合与「两次查询求交」完全相同。"""
    window = main_window
    browser = window.asset_browser
    _build_scene(window)
    browser.saved_rule = {'title': 'Rule', 'text': 'alpha',
                          'tag_expression': 'blue', 'rating': 1}
    _force_synchronous_path(monkeypatch)
    browser.refresh()
    merged = list(browser.model.items)
    assert merged, 'the saved rule must select something'

    # 改动前的写法：第二次查询 + 按**对象身份**求交。
    catalog = browser.catalog
    matches = catalog.query(text='alpha', tag_expression='blue', min_rating=1)
    allowed = {r.item for r in matches}
    original = [r.item for r in catalog.query() if r.item in allowed]

    assert set(merged) == set(original)
    assert len(merged) == len(original), 'the merged query must not duplicate rows'
    assert merged == original, 'the merged query must keep the original order'


# ── (d) 防过期：第一次的结果不得覆盖第二次 ───────────────────────────

def test_older_refresh_result_never_overwrites_the_newer_one(main_window, monkeypatch):
    """连续两次 refresh：旧的那次后到，也不得覆盖新的一次。"""
    window = main_window
    browser = window.asset_browser
    pool = _use_controllable_workers(monkeypatch)
    _force_sliced_path(monkeypatch)

    first = photo(window, 'default-canvas', 'First')
    browser.model.replace([])
    browser.refresh()
    _drain_scan(browser)
    older = pool.workers[-1]

    second = photo(window, 'default-canvas', 'Second')
    browser.refresh()
    _drain_scan(browser)
    newer = pool.workers[-1]

    assert older is not newer
    assert older.token != newer.token, 'each refresh must carry its own token'
    # 旧结果后到：必须被丢弃
    older.deliver()
    assert browser.model.items == []
    # 新结果到达：才落模型
    newer.deliver()
    assert browser.model.items == [first, second]


# ── (e) 乱序完成：先发起的扫描后返回，不得覆盖后发起的 ───────────────

def test_out_of_order_completion_keeps_the_newest_result(main_window, monkeypatch):
    """新的先到、旧的后到：模型里必须仍然是新的那一份。"""
    window = main_window
    browser = window.asset_browser
    pool = _use_controllable_workers(monkeypatch)
    _force_sliced_path(monkeypatch)

    first = photo(window, 'default-canvas', 'First')
    browser.model.replace([])
    browser.refresh()
    _drain_scan(browser)
    older = pool.workers[-1]

    second = photo(window, 'default-canvas', 'Second')
    browser.refresh()
    _drain_scan(browser)
    newer = pool.workers[-1]

    newer.deliver()
    assert browser.model.items == [first, second]
    older.deliver()
    assert browser.model.items == [first, second], 'a stale result overwrote the newer one'


# ── (f) 分片预算：单个 GUI 回合有上界，且总进度不丢 ──────────────────

def test_slice_budget_bounds_each_turn_and_loses_no_progress(main_window, monkeypatch):
    """单个回合扫描条数 ≤ `SCENE_SCAN_SLICE_ITEMS`；十万条一条不丢。"""
    window = main_window
    browser = window.asset_browser
    pool = _use_controllable_workers(monkeypatch)
    total = 100000
    yielded = []

    class _FakeAsset:
        """只带扫描要读的标量字段（十万个 Qt 图元太重，这里不用真图元）。"""

        __slots__ = ('_title', '_tags', '_rating', '_canvas_id', 'save_id',
                     '_last_used', 'filename')

        def __init__(self, index):
            self._title = f'Item {index:06d}'
            self.filename = f'item-{index:06d}.png'
            self._tags = []
            self._rating = 0
            self._canvas_id = 'default-canvas'
            self.save_id = index + 1
            self._last_used = ''

    def fake_asset_items():
        for index in range(total):
            yielded.append(index)
            yield _FakeAsset(index)

    monkeypatch.setattr(browser.catalog, 'asset_items', fake_asset_items)

    # 记录每个 GUI 回合处理了多少条。
    slice_sizes = []
    real_step = browser._scene_scan_step

    def counting_step(*args, **kwargs):
        before = len(yielded)
        real_step(*args, **kwargs)
        slice_sizes.append(len(yielded) - before)

    monkeypatch.setattr(browser, '_scene_scan_step', counting_step)

    browser.refresh()
    _drain_scan(browser)

    assert pool.workers, 'the scan must hand the rows to the background worker'
    worker = pool.workers[-1]
    # 总进度不丢：源被读完了，一条不差。
    assert len(yielded) == total
    assert len(worker.rows) == total
    # 每个回合的条数有上界。
    assert slice_sizes, 'the scan must have taken at least one turn'
    assert max(slice_sizes) <= asset_browser_module.SCENE_SCAN_SLICE_ITEMS
    # 条数上限真的起作用了：回合数不少于 ceil(total / 上限)。
    assert len(slice_sizes) >= -(-total // asset_browser_module.SCENE_SCAN_SLICE_ITEMS)
    # 过滤与排序之后，仍然一条不少。
    filtered = worker._process(worker.rows)
    assert len(filtered) == total
    assert [row.sort_key for row in filtered] == sorted(row.sort_key for row in filtered)


# ── (g) LRU 淘汰后，代理不得被当成可编辑对象 ─────────────────────────

def test_evicted_display_proxy_is_never_treated_as_editable(main_window, monkeypatch):
    """淘汰 → 重建 → 代理身份判定不变，`select_assets` 不把它当可编辑对象。"""
    from prism.catalog_browser import CatalogAssetProxy
    window = main_window
    browser = window.asset_browser
    scene = window.view.scene

    class _Reader:
        """`CatalogAssetProxy` 只弱引用它 —— 一个普通对象就够。"""

    def make_proxy(save_id):
        row = {'id': save_id, 'title': f'Row {save_id}', 'filename': f'{save_id}.png',
               'canvas_id': 'default-canvas', 'width': 4, 'height': 4, 'tags': '[]',
               'rating': 0, 'notes': '', 'group_id': None, 'kind': 'pixmap'}
        return CatalogAssetProxy(row, 'unused.prism', scene, _Reader())

    proxy = make_proxy(987654321)
    browser._catalog_proxy_cache[987654321] = proxy
    # 再塞一条，让缓存**超过**上限，裁剪才会动手（LRU：弹最旧的）。
    browser._catalog_proxy_cache[987654322] = make_proxy(987654322)
    monkeypatch.setattr(asset_browser_module, 'CATALOG_PROXY_CACHE_LIMIT', 1)
    browser._trim_catalog_proxy_cache()
    assert len(browser._catalog_proxy_cache) == 1
    assert 987654321 not in browser._catalog_proxy_cache, 'the proxy should have been evicted'
    assert 987654322 in browser._catalog_proxy_cache, 'the most recent proxy must survive'
    # 淘汰只影响缓存，**不影响身份判定**。
    assert getattr(proxy, '_catalog_proxy', False) is True
    # 重建：同一行再来一遍，仍然是代理。
    rebuilt = make_proxy(987654321)
    assert rebuilt is not proxy
    assert getattr(rebuilt, '_catalog_proxy', False) is True

    # `select_assets`：代理不得被当成可编辑对象选中进场景。
    monkeypatch.setattr(browser.list, 'selectedIndexes', lambda: [_FakeIndex(proxy)])
    browser.select_assets(None)
    assert scene.selectedItems(user_only=True) == [], 'a proxy was selected as an editable object'
    assert 'not loaded' in browser.summary.text()

    # 重建出来的代理同样不是可编辑对象。
    monkeypatch.setattr(browser.list, 'selectedIndexes', lambda: [_FakeIndex(rebuilt)])
    browser.select_assets(None)
    assert scene.selectedItems(user_only=True) == []

    # 对照组：真素材照常被选中（证明上面两条不是"什么都没发生"）。
    real = photo(window, 'default-canvas', 'Real')
    monkeypatch.setattr(browser.list, 'selectedIndexes', lambda: [_FakeIndex(real)])
    browser.select_assets(None)
    assert scene.selectedItems(user_only=True) == [real]


# ── (h) 在途扫描期间 hideEvent()：扫描作废，结果不落模型 ──────────────

def test_hide_event_voids_an_in_flight_scan(main_window, monkeypatch):
    """隐藏浏览器时作废在途扫描：后到的结果不得落进模型。"""
    window = main_window
    browser = window.asset_browser
    pool = _use_controllable_workers(monkeypatch)
    _force_sliced_path(monkeypatch)

    photo(window, 'default-canvas', 'First')
    browser.model.replace([])
    browser.refresh()
    _drain_scan(browser)
    worker = pool.workers[-1]

    browser.hideEvent(QtGui.QHideEvent())
    worker.deliver()
    assert browser.model.items == [], 'a voided scan still wrote into the model'


# ── 回归护栏：小工程必须保持**同步**语义 ─────────────────────────────

def test_small_project_keeps_the_synchronous_result(main_window):
    """小工程的结果必须**当场**可用（改动前就是同步的）。

    第 4 轮的一个真实回归：目录不可用时一律走分片扫描 → 结果变成异步 →
    所有"刷新后立刻断言"的调用方（包括既有测试）全灭。现在小工程回到
    同步路径，这条用例把那条护栏钉住。
    """
    window = main_window
    browser = window.asset_browser
    first = photo(window, 'default-canvas', 'First')
    browser.model.replace([])
    browser.refresh()
    assert browser.model.items == [first], 'a small project must refresh synchronously'
    assert browser._scan_state is None, 'a small project must not start a sliced scan'
    assert browser._row_worker is None, 'a small project must not need a background worker'
