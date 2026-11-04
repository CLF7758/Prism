"""E01 独立真值集：复杂表达式与过滤/排序逐 ID 和顺序对照。

**这份真值是手写的，不是产品算出来的。**

`ProjectAssetCatalog.query()` 内部用 `_matches()` 逐行判定、用
`_row_sort_key()` 排序。本测试**不复用**这两个函数：夹具是手写的固定表，
每条期望的「ID 列表 + 顺序」都是按公开契约（`project_catalog.query`
的 docstring、`prism/tags.py` 的查询语法）**人工列出并写死**的。
产品算出来的结果与这份表逐项相同才算通过。

覆盖（对应验收附件 E01）：

* **空结果**：表达式命中不到任何素材 → 空列表，不是「全部」。
* **全匹配**：不带任何过滤 → 全部素材，且顺序是契约写明的
  `(画布路径, 标题, 保存 id)`。
* **同值**：两条标题相同 → 靠保存 id 决出先后，不靠内存地址。
* **NULL**：没有标题的素材**不能丢**，它落到标题回退值上，并排在
  ASCII 标题之后（比较键是 `标题.casefold()` 的码位序）。
* **复杂表达式**：`a | b`（或）、`a -b`（且非）、命名空间 `ns:tag`。
* **其它过滤**：最低评分、文字搜索、画布范围、删除项、limit/max_rows。

目录（SQLite）分支的同类真值集需要真实 `.prism` 夹具，属于本包的
未完成部分，见 REPORT.md 的 E01 行。
"""
import pytest

from PyQt6 import QtGui

from prism.items import PrismPixmapItem
from prism.project_catalog import ProjectAssetCatalog


#: 手写夹具。键是**测试内部**的名字，不是产品的 id。
#:
#: 排序键是 `(画布路径, 标题.casefold(), 保存 id)`，全部素材都在同一张
#: **具名**画布 `main-canvas` 上，所以画布路径是常量，顺序只由标题和
#: 保存 id 决定。
#:
#:   键   保存 id  标题      标签              评分
#:   s1   '0001'   'Beta'    ['red']           3
#:   s2   '0002'   'Beta'    ['red', 'blue']   3   ← 与 s1 同标题
#:   s3   '0003'   'Alpha'   []                0   ← 未标签
#:   s4   '0004'   ''        ['red']           0   ← NULL 标题
#:   s5   '0005'   'Zeta'    ['blue']          5
#:   s6   '0006'   'Gamma'   []                0   ← 未标签
#:   s7   '0007'   'Alpha'   ['建筑/红墙']      0   ← 命名空间标签
#:
#: 为什么不用 `default-canvas`：产品口径里「未分类」= 默认画布 ∪ 归属已
#: 消失（`project_catalog.is_unclassified`），放在默认画布上的素材**全部**
#: 算未分类，真值就不可能同时覆盖「未标签」和「未分类」两个口径。
FIXTURE = (
    ('s1', '0001', 'Beta', ('red',), 3),
    ('s2', '0002', 'Beta', ('red', 'blue'), 3),
    ('s3', '0003', 'Alpha', (), 0),
    ('s4', '0004', '', ('red',), 0),
    ('s5', '0005', 'Zeta', ('blue',), 5),
    ('s6', '0006', 'Gamma', (), 0),
    ('s7', '0007', 'Alpha', ('建筑/红墙',), 0),
)

#: 夹具素材所在的**具名**画布。
CANVAS = 'main-canvas'
#: 一张没有素材的具名画布，用来测画布范围。
EMPTY_CANVAS = 'other-canvas'


def _build(view):
    """把手写夹具放进场景，返回 (catalog, 键 → save_id)。"""
    for page_id in (CANVAS, EMPTY_CANVAS):
        view.scene.workspace_pages.append(
            {'id': page_id, 'kind': 'canvas', 'title': page_id})
    for key, save_id, title, tags, rating in FIXTURE:
        image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#336699'))
        item = PrismPixmapItem(image)
        item.save_id = save_id
        item._title = title
        item._tags = list(tags)
        item._rating = rating
        item._canvas_id = CANVAS
        view.scene.addItem(item)
    panel = view.category_panel
    catalog = ProjectAssetCatalog(view.scene, panel.resource_service)
    by_save_id = {save_id: key for key, save_id, _t, _g, _r in FIXTURE}
    return catalog, by_save_id


def _keys(catalog, by_save_id, **kwargs):
    """查询结果 → 手写夹具的键列表（保持产品给出的顺序）。"""
    results = catalog.query(**kwargs)
    return [by_save_id.get(getattr(r.item, 'save_id', None),
                           '<?:%s>' % getattr(r.item, 'save_id', None))
            for r in results]


def test_the_full_match_returns_every_asset_in_the_contract_order(view):
    """全匹配：全部素材，顺序按 (画布路径, 标题, 保存 id)。

    'Alpha' 有两条（s3 与 s7），标题相同 → 保存 id 决出先后；
    s4 没有标题 → 落到回退标题 '未命名'，码位序在 ASCII 标题之后。
    """
    catalog, ids = _build(view)
    assert _keys(catalog, ids) == ['s3', 's7', 's1', 's2', 's6', 's5', 's4']


def test_an_expression_that_matches_nothing_returns_nothing(view):
    """空结果：不是「全部」，也不是「1 条」。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, tag_expression='nosuchtag') == []


def test_a_text_search_that_matches_nothing_returns_nothing(view):
    """空结果（文字分支）。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, text='nosuchword') == []


def test_the_or_expression_returns_the_union(view):
    """`a | b` 是「或」：命中任一即可。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, tag_expression='red | blue') == [
        's1', 's2', 's5', 's4']


def test_the_and_not_expression_excludes_the_second_term(view):
    """`a -b` 是「且非」：有 a 且没有 b。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, tag_expression='red -blue') == ['s1', 's4']


def test_the_namespace_expression_matches_the_nested_tag(view):
    """命名空间标签：写 `建筑` 能命中 `建筑/红墙`（契约：命中任意层级）。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, tag_expression='建筑') == ['s7']


def test_the_untagged_filter_is_not_the_same_as_the_unclassified_one(view):
    """未标签 ≠ 未分类，而且带命名空间的标签**也算标签**。

    s3 / s6 完全没有标签 → 未标签。
    s7 的标签是 `建筑/红墙` —— 命名空间标签**仍然算有效标签**，
    所以 s7 不在未标签里（第一版真值把它算进去了，是**真值写错**，
    不是产品错；命名空间语义见 `prism/tags.py` 的 `name_index`）。
    未分类是另一套判定：有归属画布的素材不算未分类。
    """
    catalog, ids = _build(view)
    assert _keys(catalog, ids, untagged_only=True) == ['s3', 's6']
    assert catalog.untagged_count() == 2
    # 未分类走的是另一套判定：夹具里的素材都有归属画布。
    assert catalog.unclassified_count() == 0


def test_the_minimum_rating_filter(view):
    """最低评分：评分 0 的素材不算「评分 ≥ 3」。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, min_rating=3) == ['s1', 's2', 's5']


def test_the_text_search_matches_the_title_case_insensitively(view):
    """文字搜索：大小写不敏感，命中标题。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, text='beta') == ['s1', 's2']


def test_the_canvas_scope_filters_by_canvas(view):
    """画布范围：范围外的画布一条都不出。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, canvas_scope=EMPTY_CANVAS) == []
    assert _keys(catalog, ids, canvas_scope=CANVAS) == [
        's3', 's7', 's1', 's2', 's6', 's5', 's4']


def test_the_deleted_view_returns_only_deleted_assets(view):
    """include_deleted=True **只**返回删除项；夹具里没有删除项 → 空。"""
    catalog, ids = _build(view)
    assert _keys(catalog, ids, include_deleted=True) == []


def test_the_limit_keeps_the_same_prefix(view):
    """limit 截断的是同一个排序键的前缀，不是另一批。"""
    catalog, ids = _build(view)
    full = _keys(catalog, ids)
    assert _keys(catalog, ids, limit=2) == full[:2]
    assert _keys(catalog, ids, limit=0) == []


def test_the_max_rows_keeps_the_same_prefix(view):
    """max_rows 与 limit 作用在同一个排序键上。"""
    catalog, ids = _build(view)
    full = _keys(catalog, ids)
    assert _keys(catalog, ids, max_rows=3) == full[:3]
    assert _keys(catalog, ids, max_rows=99) == full


def test_the_same_title_is_ordered_by_the_save_id(view):
    """同值：标题相同的两条，顺序由保存 id 决定，不随创建顺序漂移。

    这条单独再钉一次：把两条同标题素材的**创建顺序对调**，
    期望顺序不变。
    """
    catalog, ids = _build(view)
    assert _keys(catalog, ids, text='beta') == ['s1', 's2']

    # 重新建一个场景，插入顺序反过来
    for item in list(view.scene.items()):
        if isinstance(item, PrismPixmapItem):
            view.scene.removeItem(item)
    for key, save_id, title, tags, rating in reversed(FIXTURE):
        image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor('#336699'))
        item = PrismPixmapItem(image)
        item.save_id = save_id
        item._title = title
        item._tags = list(tags)
        item._rating = rating
        item._canvas_id = CANVAS
        view.scene.addItem(item)
    panel = view.category_panel
    catalog2 = ProjectAssetCatalog(view.scene, panel.resource_service)
    assert _keys(catalog2, ids, text='beta') == ['s1', 's2']


def test_a_null_title_is_never_dropped(view):
    """NULL 标题：不能因为「没有标题」被过滤掉。"""
    catalog, ids = _build(view)
    keys = _keys(catalog, ids)
    assert 's4' in keys
    result = [r for r in catalog.query() if r.item.save_id == '0004'][0]
    assert result.title == '未命名'


@pytest.mark.parametrize('expression', ['red', 'red | blue', 'red -blue'])
def test_the_expression_result_is_stable_across_calls(view, expression):
    """同一次查询重复执行必须给出同一批、同一顺序。"""
    catalog, ids = _build(view)
    first = _keys(catalog, ids, tag_expression=expression)
    second = _keys(catalog, ids, tag_expression=expression)
    assert first == second
