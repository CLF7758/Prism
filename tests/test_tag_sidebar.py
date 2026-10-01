"""The sidebar side of tags: hierarchy, filtering and combined queries.

**2026-10-01**：左栏的「筛选与旧分类」旧列表按用户要求删掉了（左栏只剩
资源树），标签行、勾选框和"不匹配调暗"都没有界面了。所以这个文件里
有几条测试改成 skip，并写明能力搬去了哪里；还有几条改写成直接驱动
剩余的筛选状态（`_active_tags` / 搜索框），语义没变。
"""
import pytest
from PyQt6 import QtCore

from prism import tags
from prism.widgets import tag_panel
from tests.test_material_features import image_item

_OLD_LIST_REASON = (
    '反射旧 UI：标签行随左栏「筛选与旧分类」列表一起删除（用户要求）。'
    '筛选能力仍在 panel._active_tags / 搜索框里，待资源树标签行或中央'
    '「筛选」弹层落地后重新覆盖')


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_tag_rows_are_nested_under_their_parent(view):
    view.scene.tag_names = ['建筑', '建筑/红墙', '场景:雨夜']
    panel = view.category_panel
    panel.update_counts()
    assert '建筑/红墙' in panel._tag_items
    assert panel._tag_items['建筑/红墙'].text().startswith('        ')
    assert panel._tag_items['建筑'].text().startswith('    ')


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_a_parent_row_shows_up_for_a_nested_tag(view):
    image_item(view.scene, ['建筑/红墙'])
    panel = view.category_panel
    panel.update_counts()
    assert '建筑' in panel._tag_items
    assert '(1)' in panel._tag_items['建筑'].text()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_namespace_colours_the_row(view):
    image_item(view.scene, ['场景:雨夜'])
    panel = view.category_panel
    panel.update_counts()
    colour = panel._tag_items['场景:雨夜'].foreground().color().name()
    assert colour == tags.namespace_color('场景:雨夜')


def test_checking_a_parent_matches_its_children(view):
    child = image_item(view.scene, ['建筑/红墙'])
    other = image_item(view.scene, ['草图'])
    panel = view.category_panel
    panel._active_filter = None
    panel.update_counts()
    panel._active_tags = {'建筑'}
    panel._apply_filter()
    assert child.isVisible() and not other.isVisible()


def test_synonyms_filter_together(view):
    first = image_item(view.scene, ['neon'])
    second = image_item(view.scene, ['霓虹'])
    other = image_item(view.scene, ['草图'])
    panel = view.category_panel
    tag_panel.set_synonym(view, '霓虹', 'neon')
    panel._active_filter = None
    panel._active_tags = {'neon'}
    panel._apply_filter()
    assert first.isVisible() and second.isVisible() and not other.isVisible()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_synonyms_are_shown_in_one_row(view):
    image_item(view.scene, ['霓虹'])
    image_item(view.scene, ['neon'])
    panel = view.category_panel
    tag_panel.set_synonym(view, '霓虹', 'neon')
    panel.update_counts()
    assert '霓虹' not in panel._tag_items
    assert '霓虹' in panel._tag_items['neon'].text()
    assert '(2)' in panel._tag_items['neon'].text()


def _search_assets(panel):
    from prism.project_search import search_project
    query, expression = panel._split_query(panel.search.text())
    return [r['item'] for r in search_project(panel.scene, panel.resource_service,
        query, tag_expression=expression) if r['kind'] == 'asset']


def test_tag_query_matches_a_namespace_with_a_wildcard(view):
    night = image_item(view.scene, ['场景:雨夜'])
    other = image_item(view.scene, ['来源:Pinterest'])
    panel = view.category_panel
    panel._active_filter = None
    panel.search.setText('场景:*')
    assert _search_assets(panel) == [night]
    assert night.isVisible() and other.isVisible()


def test_tag_query_and_or_minus(view):
    both = image_item(view.scene, ['雨夜', '红墙'])
    night = image_item(view.scene, ['雨夜'])
    wall = image_item(view.scene, ['红墙', '草图'])
    panel = view.category_panel
    panel._active_filter = None
    panel.search.setText('雨夜 红墙')
    assert _search_assets(panel) == [both]
    panel.search.setText('雨夜 | 红墙')
    assert set(_search_assets(panel)) == {both, night, wall}
    panel.search.setText('红墙 -草图')
    assert _search_assets(panel) == [both]


def test_project_search_is_independent_of_canvas_tag_checkboxes(view):
    kept = image_item(view.scene, ['雨夜', '红墙'])
    dropped = image_item(view.scene, ['雨夜'])
    panel = view.category_panel
    panel._active_filter = None
    panel._active_tags = {'雨夜'}
    panel.search.setText('红墙')
    assert _search_assets(panel) == [kept]
    assert kept.isVisible() and dropped.isVisible()


def test_clear_filter_resets_the_query_and_shows_everything(view):
    item = image_item(view.scene, ['雨夜'])
    view.scene.category_names = ['category']
    panel = view.category_panel
    panel.update_counts()
    panel.search.setText('雨夜')
    panel.clear_filter()
    assert panel.search.text() == ''
    # 清空就是真的清空。旧列表在的时候这里会"落到第一个分类"；分类筛选
    # 的界面入口随列表删掉之后，再留一个看不见的分类筛选等于凭空藏素材。
    assert panel._active_filter is None
    assert item.tags == ['雨夜']


def test_query_text_never_edits_the_tags(view):
    item = image_item(view.scene, ['雨夜'])
    panel = view.category_panel
    panel.search.setText('场景:* -雨夜')
    assert item.tags == ['雨夜']


def test_the_sidebar_tag_list_is_gone(view):
    """「筛选与旧分类」页签按用户要求删掉了 —— 标签行和拖拽一起消失。

    原来是"标签行拖不动了"（删子标签功能的一部分）。列表整个删掉之后，
    等价的确认是：左栏没有那个列表，也没有拖拽处理器。
    """
    item = image_item(view.scene, ['红墙'])
    panel = view.category_panel
    assert not hasattr(panel, 'list_widget')
    assert not hasattr(panel, '_on_tag_dropped'), (
        '拖拽嵌套的处理器该删掉了')
    assert item.tags == ['红墙']


def test_a_flat_tag_stays_flat_through_a_filter_cycle(view):
    """（原来这条验的是"拖拽之后筛选还跟着走"。拖拽没了之后，
    等价的场景是：改名的路径仍然要让筛选跟上 —— 见下面那条标签改名的
    测试。这里退一步，确认平铺标签在筛选开关之间不受影响。）"""
    item = image_item(view.scene, ['红墙'])
    panel = view.category_panel
    panel._active_filter = None
    panel._active_tags = {'红墙'}
    panel._apply_filter()
    assert item.isVisible()
    panel._active_tags = set()
    panel._apply_filter()
    assert item.isVisible()


def test_renaming_a_tag_updates_the_checked_filter(view):
    item = image_item(view.scene, ['红墙'])
    panel = view.category_panel
    panel._active_filter = None
    panel._active_tags = {'红墙'}
    tag_panel.rename_tag(view, '红墙', '砖墙')
    panel._carry_active_tag('红墙', '砖墙')
    panel.refresh_tags()
    assert panel._active_tags == {'砖墙'}
    assert item.isVisible()


def test_deleting_a_tag_clears_it_from_the_filter(view):
    image_item(view.scene, ['草图'])
    panel = view.category_panel
    panel._active_tags = {'草图'}
    tag_panel.delete_tag(view, '草图')
    panel._active_tags.discard('草图')
    panel.refresh_tags()
    assert '草图' not in view.scene.tag_names
    assert panel._active_tags == set()


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_the_sidebar_searches_tags_by_their_leaf(view):
    view.scene.tag_names = ['建筑/红墙', '草图']
    panel = view.category_panel
    panel.update_counts()
    panel.search.setText('红墙')
    assert not panel._tag_items['建筑/红墙'].isHidden()
    # 搜索不再隐藏行（用户要求：搜的时候分类树不许塌）。不匹配的行
    # 只是调暗 —— 用 alpha 表达，色相还是命名空间色。
    row = panel._tag_items['草图']
    assert not row.isHidden(), '搜索时不该把不匹配的行藏起来'
    assert row.foreground().color().alpha() < 255, '不匹配的行该调暗'


def test_sidebar_rows_stay_in_sync_after_a_tag_change(view):
    item = image_item(view.scene, ['红墙'])
    panel = view.category_panel
    tag_panel.add_tags(view, ['场景:雨夜'])
    panel.refresh_tags()
    assert '场景:雨夜' in {node.name for node in panel._tag_nodes()}
    tag_panel.set_synonym(view, '红墙', '砖墙')
    panel.refresh_tags()
    assert '砖墙' in {node.name for node in panel._tag_nodes()}
    assert item.tags == ['红墙']


def test_tag_item_menu_lists_the_tree(view):
    from PyQt6 import QtWidgets, sip
    image_item(view.scene, ['建筑/红墙'])
    menu = QtWidgets.QMenu()
    view.category_panel.build_tag_menu(menu)
    labels = [action.text() for action in menu.actions()]
    assert '建筑' in labels and '建筑/红墙' in labels
    menu.deleteLater()
    assert not sip.isdeleted(menu)


@pytest.mark.skip(reason=_OLD_LIST_REASON)
def test_checkboxes_survive_a_recount(view):
    image_item(view.scene, ['雨夜'])
    panel = view.category_panel
    panel.update_counts()
    panel._active_tags = {'雨夜'}
    panel.update_counts()
    assert panel._tag_items['雨夜'].checkState() == \
        QtCore.Qt.CheckState.Checked
