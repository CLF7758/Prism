"""T7 第一阶段：标签文字、对象边界、AND/OR 和未打标签筛选。

对应任务书 430-437 行：

a. 标签文字始终完整可见。
b. 标签支持多选、搜索、创建、重命名和删除。
c. 明确显示当前是给素材还是页面打标签。
d. 支持 AND/OR 筛选。
e. 未打标签素材可以筛选。
f. 页面标签不会自动混入素材筛选。

**2026-10-01**：左栏的「筛选与旧分类」旧列表按用户要求删掉了。侧栏的
标签行、"未打标签"行和勾选框都不再有界面，所以依赖这些行的测试标了
skip（原因写在每条的 reason 里）；能直接驱动剩余筛选状态
（`_active_tags` / 搜索框）的，改写成那个形式，语义不变。标签管理器
（`tag_panel.TagPanel`）里的行仍然完整，a 条的覆盖留在
`test_the_manager_rows_fit_their_text_too`。

这个文件只钉第一阶段的行为，不碰第二阶段（命名空间、父子、别名、颜色引用）
里已有的实现细节。
"""
import pytest
from PyQt6 import QtWidgets
from PyQt6.QtCore import Qt

from prism import tags
from prism.widgets import tag_panel
from tests.test_material_features import image_item

#: 一条深到第七层的标签：缩进最容易把名字挤出侧栏的情形。
DEEP = '建筑/红墙/材质/石材/大理石/意大利/卡拉拉'

_OLD_LIST_REASON = (
    '反射旧 UI：侧栏标签行 / 「未打标签」行随左栏「筛选与旧分类」列表'
    '一起删除（用户要求）。筛选语义仍在 panel._active_tags 与搜索框里，'
    '待资源树标签行或中央「筛选」弹层落地后重新覆盖')


def indent_of(text):
    return len(text) - len(text.lstrip(' '))


def tag_panel_of(view):
    panel = view.category_panel
    panel.update_counts()
    return panel


# ── a. 标签文字始终完整可见 ───────────────────────────────────────────

@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_a_deep_tag_keeps_its_whole_text_visible(view):
    """行宽不能小于文字宽，否则 Qt 会画省略号，名字就读不全了。"""
    view.scene.tag_names = [DEEP]
    panel = tag_panel_of(view)
    metrics = panel.list_widget.fontMetrics()
    for name, row in panel._tag_items.items():
        assert row.sizeHint().width() >= metrics.horizontalAdvance(row.text()), \
            f'{name} 的文字放不下：{row.text()!r}'
    panel.list_widget.horizontalScrollBar().deleteLater()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_the_indent_stops_at_the_limit(view):
    """嵌套再深，缩进也不能一直吃宽度。"""
    view.scene.tag_names = [DEEP]
    panel = tag_panel_of(view)
    limit = 4 * (tag_panel.MAX_TAG_INDENT_LEVELS + 1)
    assert indent_of(panel._tag_items[DEEP].text()) <= limit


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_a_shallow_tag_keeps_the_documented_indent(view):
    """缩进上限不能把浅层标签的层级感也抹掉。"""
    view.scene.tag_names = ['建筑', '建筑/红墙']
    panel = tag_panel_of(view)
    assert indent_of(panel._tag_items['建筑'].text()) == 4
    assert indent_of(panel._tag_items['建筑/红墙'].text()) == 8


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_a_wide_sidebar_fills_the_row(view):
    """侧栏挺宽时行宽跟着视口走，高亮背景不会短一截。"""
    view.scene.tag_names = ['草图']
    panel = tag_panel_of(view)
    row = panel._tag_items['草图']
    assert row.sizeHint().width() >= panel.list_widget.viewport().width()


def test_the_manager_rows_fit_their_text_too(view):
    view.scene.tag_names = [DEEP]
    panel = tag_panel.TagPanel(view)
    metrics = panel._tree.fontMetrics()
    for name, row in panel._rows.items():
        assert row.sizeHint().width() >= metrics.horizontalAdvance(row.text()), \
            f'{name} 的文字放不下：{row.text()!r}'
    panel.close()


# ── b. 多选、搜索、创建、重命名和删除 ─────────────────────────────────

def test_several_tags_are_one_filter(view):
    """勾选是加法的：两个标签一起勾就是「同时包含」。

    勾选框随左栏旧列表删掉了，这里直接把筛选状态设成"勾了两个"，
    验的还是同一个语义。
    """
    both = image_item(view.scene, ['甲', '乙'])
    only_one = image_item(view.scene, ['甲'])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel._active_tags = {'甲', '乙'}
    panel._apply_filter()
    assert both.isVisible() and not only_one.isVisible()


def test_the_manager_searches_creates_renames_and_deletes(view):
    image_item(view.scene, ['红墙'])
    panel = tag_panel.TagPanel(view)
    tag_panel.add_tags(view, ['场景:雨夜', '场景:白天'])
    panel._search.setText('雨夜')
    assert not panel._rows['场景:雨夜'].isHidden()
    assert panel._rows['场景:白天'].isHidden()

    panel._search.setText('')
    tag_panel.rename_tag(view, '场景:雨夜', '雨夜')
    assert '场景:雨夜' not in panel._rows
    assert '场景:白天' in panel._rows

    tag_panel.delete_tag(view, '场景:白天')
    assert '场景:白天' not in panel._rows
    assert '场景:白天' not in view.scene.tag_names
    panel.close()


# ── c. 明确显示当前是给素材还是页面打标签 ─────────────────────────────

def test_the_asset_menu_says_how_many_assets_it_tags(view):
    first = image_item(view.scene, ['红墙'])
    second = image_item(view.scene, ['红墙'])
    first.setSelected(True)
    second.setSelected(True)
    menu = QtWidgets.QMenu()
    view.category_panel.build_tag_menu(menu)
    header = menu.actions()[0]
    assert not header.isEnabled(), '提示行只是说明，不该能点'
    assert '2' in header.text()
    assert 'asset' in header.text().lower()
    menu.deleteLater()


def test_the_asset_menu_asks_for_a_selection_first(view):
    image_item(view.scene, ['红墙'])
    menu = QtWidgets.QMenu()
    view.category_panel.build_tag_menu(menu)
    header = menu.actions()[0]
    assert not header.isEnabled()
    assert 'asset' in header.text().lower()
    menu.deleteLater()


def test_the_inspector_says_it_tags_assets(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    detail = view._detail_panel
    detail._on_selection_changed()
    assert 'asset' in detail._tag_scope.text().lower()
    assert '1' in detail._tag_scope.text()


def test_the_inspector_counts_the_whole_selection(view):
    first = image_item(view.scene, ['红墙'])
    second = image_item(view.scene, ['红墙'])
    first.setSelected(True)
    second.setSelected(True)
    detail = view._detail_panel
    detail._on_selection_changed()
    assert '2' in detail._tag_scope.text()


@pytest.mark.skip(reason=(
    '反射旧 UI：侧栏那行「标签筛选只作用于本画布的素材…」说明随红框一起'
    '删除（用户要求）。「筛选只认素材标签」这条规则本身仍由 _apply_filter '
    '保证，覆盖保留在 test_a_page_only_tag_finds_no_asset 里'))
def test_the_sidebar_says_which_of_the_two_objects_it_filters(view):
    panel = view.category_panel
    assert 'asset' in panel.tag_scope.text().lower()
    assert 'page' in panel.tag_scope.text().lower()


def test_the_page_tag_dialog_says_it_tags_a_page(view, monkeypatch):
    scene = view.scene
    scene.workspace_pages = [{'id': 'pg', 'kind': 'canvas',
                              'title': '秋雨', 'tags': []}]
    seen = {}

    def fake_get_text(parent, title, label, *args, **kwargs):
        seen['title'] = title
        seen['label'] = label
        return '', False

    monkeypatch.setattr(QtWidgets.QInputDialog, 'getText',
                        staticmethod(fake_get_text))
    view.parent._workspace_page_action('tag', 'pg')
    assert 'page' in seen['label'].lower()
    assert '秋雨' in seen['label']


def test_the_manager_says_which_object_a_tag_can_hang_on(view):
    panel = tag_panel.TagPanel(view)
    text = panel._scope.text().lower()
    assert 'asset' in text and 'page' in text
    panel.close()


# ── d. AND / OR 筛选 ─────────────────────────────────────────────────

@pytest.mark.skip(reason=(
    '反射旧 UI：「同时包含 / 任意包含」下拉随红框一起删除（用户要求）。'
    '多选标签的语义固定为「同时包含」（test_several_tags_are_one_filter '
    '仍钉着它）；「任意包含」要回来时得先有新的界面入口'))
def test_the_sidebar_can_switch_between_all_of_and_any_of(view):
    both = image_item(view.scene, ['甲', '乙'])
    only_one = image_item(view.scene, ['甲'])
    panel = view.category_panel
    panel._active_filter = None
    panel._active_tags = {'甲', '乙'}
    panel.tag_match_mode.setCurrentIndex(0)
    panel._apply_filter()
    assert both.isVisible() and not only_one.isVisible()
    panel.tag_match_mode.setCurrentIndex(1)
    assert both.isVisible() and only_one.isVisible()


def test_filter_items_takes_any_of_the_tags(view):
    both = image_item(view.scene, ['甲', '乙'])
    only_one = image_item(view.scene, ['甲'])
    other = image_item(view.scene, ['丙'])
    view.scene.filter_items(tags=['甲', '乙'], tag_match='any')
    assert both.opacity() == 1.0
    assert only_one.opacity() == 1.0
    assert other.opacity() == view.scene._FILTER_OPACITY


def test_filter_items_still_defaults_to_all_of_them(view):
    both = image_item(view.scene, ['甲', '乙'])
    only_one = image_item(view.scene, ['甲'])
    view.scene.filter_items(tags=['甲', '乙'])
    assert both.opacity() == 1.0
    assert only_one.opacity() == view.scene._FILTER_OPACITY


def test_filter_items_uses_the_same_tag_index_as_the_sidebar(view):
    """父标签要能搜到子标签，否则两个筛选入口各说各话。"""
    child = image_item(view.scene, ['建筑/红墙'])
    other = image_item(view.scene, ['人物/老人'])
    view.scene.filter_items(tags=['建筑'], tag_match='any')
    assert child.opacity() == 1.0
    assert other.opacity() == view.scene._FILTER_OPACITY


def test_an_unknown_match_mode_falls_back_to_all_of_them(view):
    both = image_item(view.scene, ['甲', '乙'])
    only_one = image_item(view.scene, ['甲'])
    view.scene.filter_items(tags=['甲', '乙'], tag_match='也许')
    assert both.opacity() == 1.0
    assert only_one.opacity() == view.scene._FILTER_OPACITY


# ── e. 未打标签素材可以筛选 ───────────────────────────────────────────

@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_the_untagged_row_hides_everything_that_carries_a_tag(view):
    tagged = image_item(view.scene, ['甲'])
    clean = image_item(view.scene, [])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel._untagged_item.setCheckState(Qt.CheckState.Checked)
    assert clean.isVisible() and not tagged.isVisible()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_the_untagged_row_counts_only_assets(view):
    image_item(view.scene, [])
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['页面的']}]
    panel = tag_panel_of(view)
    assert '(1)' in panel._untagged_item.text()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_the_untagged_row_untick_restores_the_view(view):
    tagged = image_item(view.scene, ['甲'])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel._untagged_item.setCheckState(Qt.CheckState.Checked)
    assert not tagged.isVisible()
    panel._untagged_item.setCheckState(Qt.CheckState.Unchecked)
    assert tagged.isVisible()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_untagged_and_a_tag_are_either_or_in_any_of_mode(view):
    tagged = image_item(view.scene, ['甲'])
    clean = image_item(view.scene, [])
    other = image_item(view.scene, ['乙'])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel.tag_match_mode.setCurrentIndex(1)
    panel._tag_items['甲'].setCheckState(Qt.CheckState.Checked)
    panel._untagged_item.setCheckState(Qt.CheckState.Checked)
    assert tagged.isVisible() and clean.isVisible() and not other.isVisible()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_untagged_in_all_of_mode_asks_for_both_at_once(view):
    """「同时包含」下，未打标签和某个标签是一个矛盾条件，谁都不该亮。"""
    tagged = image_item(view.scene, ['甲'])
    clean = image_item(view.scene, [])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel.tag_match_mode.setCurrentIndex(0)
    panel._tag_items['甲'].setCheckState(Qt.CheckState.Checked)
    panel._untagged_item.setCheckState(Qt.CheckState.Checked)
    assert not tagged.isVisible() and not clean.isVisible()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_clear_filter_also_clears_the_untagged_row(view):
    clean = image_item(view.scene, [])
    panel = view.category_panel
    panel.update_counts()
    panel._untagged_item.setCheckState(Qt.CheckState.Checked)
    panel.clear_filter()
    assert not panel._untagged_only
    assert panel._untagged_item.checkState() == Qt.CheckState.Unchecked
    assert clean.isVisible()


def test_the_query_box_can_also_ask_for_untagged_assets(view):
    tagged = image_item(view.scene, ['甲'])
    clean = image_item(view.scene, [])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel.search.setText('-*')
    from prism.project_search import search_project
    hits = search_project(view.scene, panel.resource_service, '', tag_expression='-*')
    assert [r['item'] for r in hits if r['kind'] == 'asset'] == [clean]
    assert clean.isVisible() and tagged.isVisible(), '项目搜索不修改画布可见性'


def test_the_query_box_explains_the_untagged_shortcut(view):
    panel = view.category_panel
    assert '-*' in panel.search.toolTip()


# ── f. 页面标签不混入素材筛选 ─────────────────────────────────────────

@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_a_page_only_tag_is_marked_in_the_sidebar(view):
    view.scene.tag_names = ['页面专属']
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['页面专属']}]
    panel = tag_panel_of(view)
    assert '(page only)' in panel._tag_items['页面专属'].text()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_an_asset_tag_is_not_marked_as_page_only(view):
    image_item(view.scene, ['素材的'])
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['素材的']}]
    panel = tag_panel_of(view)
    assert '(page only)' not in panel._tag_items['素材的'].text()


def test_a_page_only_tag_finds_no_asset(view):
    item = image_item(view.scene, ['甲'])
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['页面专属']}]
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel._active_tags = {'页面专属'}
    panel._apply_filter()
    assert not item.isVisible()


def test_filtering_with_a_page_tag_writes_nothing(view):
    item = image_item(view.scene, ['甲'])
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['页面专属']}]
    panel = view.category_panel
    panel.update_counts()
    panel._active_tags = {'页面专属'}
    panel._apply_filter()
    assert item.tags == ['甲']
    assert view.scene.workspace_pages[0]['tags'] == ['页面专属']


def test_the_tag_tree_still_knows_both_kinds(view):
    """目录里两种标签都看得见，只是筛选只认素材那一边。"""
    image_item(view.scene, ['素材的'])
    view.scene.workspace_pages = [{'id': 'p', 'kind': 'canvas',
                                   'title': '页', 'tags': ['页面的']}]
    names = tags.scene_tag_names(view.scene)
    assert '素材的' in names and '页面的' in names
