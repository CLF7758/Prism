"""ProjectAssetCatalog 全项目素材查询服务的单元测试（方案 §5、§12 A 组）。

覆盖：
- 全项目计数跨画布，不随当前画布切换漂移（A01、A04）
- 未标签计数（A03）
- 无 canvasId 旧素材只计一次、不漂移（A07）
- 注释（画笔/文字）不计入素材（A08）
- 画布计数字典供资源树递归汇总（A09）
- 查询过滤：文字、标签、未标签、评分、画布范围
- 定位返回有效画布，无归属回落
"""
from PyQt6 import QtGui

from prism.items import PrismPixmapItem, PrismTextItem
from prism.project_catalog import (
    ProjectAssetCatalog, UNASSIGNED_CANVAS, UNASSIGNED_LABEL)


def photo(view, tags=(), canvas=None, rating=0, title='', notes=''):
    image = QtGui.QImage(24, 24, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#336699'))
    item = PrismPixmapItem(image)
    item._tags = list(tags)
    item._rating = rating
    item._title = title
    item._notes = notes
    item._canvas_id = canvas if canvas is not None else view.current_canvas_id
    view.scene.addItem(item)
    return item


def make_catalog(view):
    panel = view.category_panel
    return ProjectAssetCatalog(view.scene, panel.resource_service)


def test_total_count_spans_all_canvases(view):
    """A01：全项目计数跨画布，切到只有 22 张的画布仍显示总数。"""
    photo(view, canvas='default-canvas')
    photo(view, canvas='default-canvas')
    photo(view, canvas='other-canvas')
    view.scene.workspace_pages.append(
        {'id': 'other-canvas', 'kind': 'canvas', 'title': 'Other'})
    catalog = make_catalog(view)
    assert catalog.total_count() == 3
    # 切换当前画布不影响全项目总数
    view.current_canvas_id = 'other-canvas'
    assert catalog.total_count() == 3
    view.current_canvas_id = 'default-canvas'
    assert catalog.total_count() == 3


def test_untagged_count(view):
    """A03：给 3 张无标签图中的 1 张加标签，未标签从 3 变 2。"""
    photo(view, canvas='default-canvas')
    photo(view, canvas='default-canvas')
    tagged = photo(view, canvas='default-canvas')
    catalog = make_catalog(view)
    assert catalog.untagged_count() == 3
    tagged._tags = ['reference']
    assert catalog.untagged_count() == 2


def test_untagged_spans_canvases(view):
    """A04：未标签检索整个项目，含其他画布。"""
    photo(view, canvas='default-canvas')
    photo(view, tags=['x'], canvas='default-canvas')
    photo(view, canvas='other-canvas')
    view.scene.workspace_pages.append(
        {'id': 'other-canvas', 'kind': 'canvas', 'title': 'Other'})
    catalog = make_catalog(view)
    assert catalog.untagged_count() == 2
    results = catalog.query(untagged_only=True)
    assert len(results) == 2
    canvas_ids = {r.canvas_id for r in results}
    assert 'other-canvas' in canvas_ids


def test_annotations_not_counted(view):
    """A08：画笔线和文字注释不计入素材数。"""
    photo(view, canvas='default-canvas')
    photo(view, canvas='default-canvas')
    note = PrismTextItem('A note')
    note._canvas_id = 'default-canvas'
    view.scene.addItem(note)
    catalog = make_catalog(view)
    assert catalog.total_count() == 2


def test_unassigned_canvas_counted_once(view):
    """A07：无 canvasId 的旧素材全项目只计一次，切画布不漂移。"""
    photo(view, canvas='default-canvas')
    orphan = photo(view, canvas='default-canvas')
    # 模拟旧文件：canvasId 缺失
    orphan._canvas_id = None
    catalog = make_catalog(view)
    assert catalog.total_count() == 2
    counts = catalog.canvas_counts()
    assert counts.get(UNASSIGNED_CANVAS) == 1
    assert counts.get('default-canvas') == 1
    # 切换当前画布，无归属素材仍只计一次，不跑到别的画布
    view.current_canvas_id = 'other-canvas'
    counts2 = catalog.canvas_counts()
    assert counts2.get(UNASSIGNED_CANVAS) == 1
    assert counts2.get('default-canvas') == 1
    assert 'other-canvas' not in counts2


def test_canvas_counts_dict(view):
    """A09：画布计数字典供资源树递归汇总。"""
    photo(view, canvas='c1')
    photo(view, canvas='c1')
    photo(view, canvas='c2')
    for cid in ('c1', 'c2'):
        view.scene.workspace_pages.append(
            {'id': cid, 'kind': 'canvas', 'title': cid})
    catalog = make_catalog(view)
    counts = catalog.canvas_counts()
    assert counts['c1'] == 2
    assert counts['c2'] == 1


def test_query_text_filter(view):
    photo(view, title='sunset beach', canvas='default-canvas')
    photo(view, title='mountain', canvas='default-canvas')
    catalog = make_catalog(view)
    results = catalog.query(text='sunset')
    assert len(results) == 1
    assert results[0].title == 'sunset beach'
    # 多词 AND
    results2 = catalog.query(text='sunset mountain')
    assert len(results2) == 0


def test_query_tag_expression(view):
    photo(view, tags=['scenery'], canvas='default-canvas')
    photo(view, tags=['portrait'], canvas='default-canvas')
    catalog = make_catalog(view)
    results = catalog.query(tag_expression='scenery')
    assert len(results) == 1


def test_query_rating_filter(view):
    photo(view, rating=3, canvas='default-canvas')
    photo(view, rating=1, canvas='default-canvas')
    catalog = make_catalog(view)
    results = catalog.query(min_rating=2)
    assert len(results) == 1
    assert results[0].rating == 3


def test_query_canvas_scope(view):
    photo(view, canvas='c1')
    photo(view, canvas='c2')
    for cid in ('c1', 'c2'):
        view.scene.workspace_pages.append(
            {'id': cid, 'kind': 'canvas', 'title': cid})
    catalog = make_catalog(view)
    results = catalog.query(canvas_scope=['c1'])
    assert len(results) == 1
    assert results[0].canvas_id == 'c1'


def test_locate_returns_canvas(view):
    item = photo(view, canvas='default-canvas')
    catalog = make_catalog(view)
    kind, page_id = catalog.locate(item)
    assert kind == 'canvas'
    assert page_id == 'default-canvas'


def test_locate_unassigned_falls_back(view):
    orphan = photo(view, canvas='default-canvas')
    orphan._canvas_id = None
    catalog = make_catalog(view)
    kind, page_id = catalog.locate(orphan)
    assert kind == 'canvas'
    # 回落到 default-canvas（工程里存在）
    assert page_id == 'default-canvas'


def test_result_path_shows_unassigned_label(view):
    orphan = photo(view, canvas='default-canvas')
    orphan._canvas_id = None
    catalog = make_catalog(view)
    results = catalog.query()
    assert len(results) == 1
    assert results[0].path == UNASSIGNED_LABEL
    assert results[0].is_unassigned


def test_deleted_items_excluded_from_active(view):
    """软删除素材不计入活跃总数（Stage C 引入 _deleted_at）。"""
    photo(view, canvas='default-canvas')
    deleted = photo(view, canvas='default-canvas')
    deleted._deleted_at = '2026-10-12T10:00:00'
    catalog = make_catalog(view)
    assert catalog.total_count() == 1
    assert catalog.trash_asset_count() == 1


def test_empty_query_returns_all_active(view):
    """空查询返回全部活跃素材（方案 §6.4：不能简单空字符串复用旧搜索）。"""
    photo(view, canvas='default-canvas')
    photo(view, canvas='default-canvas')
    catalog = make_catalog(view)
    results = catalog.query()
    assert len(results) == 2
