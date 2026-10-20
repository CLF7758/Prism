"""Current folder hierarchy, plus legacy category filtering/undo compatibility.

The former category-list rows and their indentation strings were replaced by
ResourceTreeModel parents. Verify the same hierarchy/count/visibility guarantees
through the current model instead of restoring the deleted sidebar API.
"""
import pytest
from PyQt6.QtCore import Qt
from prism.widgets.resource_tree_model import ASSET_COUNT_ROLE
from tests.test_material_features import image_item


def categorise(scene, categories):
    item = image_item(scene)
    item._categories = list(categories)
    return item


@pytest.fixture
def hierarchy(view):
    service = view.category_panel.resource_service
    model = view.category_panel.resource_tree.model()
    top = service.createFolder('canvas', 'Top')
    middle = service.createFolder('canvas', 'Middle', top['id'])
    deep = service.createFolder('canvas', 'Deep', middle['id'])
    for node, page_id in ((top, 'top-page'), (middle, 'middle-page'), (deep, 'deep-page')):
        service.createPage('canvas', 'Board', page_id, node['id'])
    model.set_asset_counts({'top-page': 1, 'middle-page': 2, 'deep-page': 1})
    return service, model, top, middle, deep


def test_flat_folder_counts_its_direct_page(hierarchy):
    service, model, top, middle, deep = hierarchy
    model.set_asset_counts({'top-page': 2})
    assert model.index_for_id(top['id']).data(ASSET_COUNT_ROLE) == 2
    assert model.index_for_id(middle['id']).data(ASSET_COUNT_ROLE) == 0


def test_parent_counts_its_own_and_descendant_pages(hierarchy):
    service, model, top, middle, deep = hierarchy
    assert model.index_for_id(top['id']).data(ASSET_COUNT_ROLE) == 4
    assert model.index_for_id(middle['id']).data(ASSET_COUNT_ROLE) == 3
    assert model.index_for_id(deep['id']).data(ASSET_COUNT_ROLE) == 1


def test_ancestor_rows_remain_visible_with_only_deep_assets(hierarchy):
    service, model, top, middle, deep = hierarchy
    model.set_asset_counts({'deep-page': 1})
    for node in (top, middle, deep):
        index = model.index_for_id(node['id'])
        assert index.isValid()
        assert index.data(ASSET_COUNT_ROLE) == 1


def test_three_levels_use_actual_index_parents(hierarchy):
    service, model, top, middle, deep = hierarchy
    assert model.parent(model.index_for_id(deep['id'])) == model.index_for_id(middle['id'])
    assert model.parent(model.index_for_id(middle['id'])) == model.index_for_id(top['id'])
    assert model.parent(model.index_for_id(top['id'])).data() == '画布'


def test_created_empty_folder_is_still_visible(hierarchy):
    service, model, top, middle, deep = hierarchy
    empty = service.createFolder('canvas', 'Empty', top['id'])
    index = model.index_for_id(empty['id'])
    assert index.isValid() and index.data() == 'Empty'
    assert index.data(ASSET_COUNT_ROLE) == 0


def test_row_titles_do_not_encode_tree_indentation(hierarchy):
    service, model, top, middle, deep = hierarchy
    for node in (top, middle, deep):
        assert model.index_for_id(node['id']).data() == node['title']


def test_tooltip_follows_full_path_after_rename_and_move(hierarchy):
    service, model, top, middle, deep = hierarchy
    tip = lambda: model.index_for_id(deep['id']).data(Qt.ItemDataRole.ToolTipRole)
    assert tip() == 'Top/Middle/Deep'
    service.rename(top['id'], 'Renamed')
    assert tip() == 'Renamed/Middle/Deep'
    service.moveBatch([deep['id']], top['id'], 'canvas')
    assert tip() == 'Renamed/Deep'


def test_selecting_a_parent_also_shows_children(view):
    """**核心那条**：点父分类要能看到子分类里的素材。

    不然父分类的数字（自己 + 子）和看到的东西对不上。
    """
    direct = categorise(view.scene, ['古风'])
    child = categorise(view.scene, ['古风/山水'])
    grand = categorise(view.scene, ['古风/山水/秋雨'])
    other = categorise(view.scene, ['风景'])

    panel = view.category_panel
    panel._active_filter = ('category', '古风')
    panel._apply_filter()

    assert direct.isVisible(), '直接挂父级的该看到'
    assert child.isVisible(), '一级子分类该看到'
    assert grand.isVisible(), '二级子分类也该看到'
    assert not other.isVisible(), '别的分类不该看到'


def test_selecting_a_child_does_not_show_the_parent(view):
    """反过来不成立：点子分类只看子分类，不看父级那些。"""
    direct = categorise(view.scene, ['古风'])
    child = categorise(view.scene, ['古风/山水'])

    panel = view.category_panel
    panel._active_filter = ('category', '古风/山水')
    panel._apply_filter()

    assert child.isVisible()
    assert not direct.isVisible(), '父级的素材不该因为选了子分类就出现'


# ── 改名和删除要带上子分类 ──────────────────────────────────────

def test_renaming_a_parent_renames_its_children(view):
    """改了父级的名字，子分类要跟着改。

    `古风` 改成 `国风` 之后，`古风/山水` 得变成 `国风/山水` —— 不然
    子分类会挂在半空中（目录里改好了，素材上还指着旧名字）。
    """
    from prism.widgets.media_library import ChangeCategory

    item = categorise(view.scene, ['古风/山水'])
    panel = view.category_panel
    view.scene.category_names = ['古风', '古风/山水']

    view.scene.undo_stack.push(ChangeCategory(
        panel,
        ['国风', '国风/山水'],
        '古风', '国风'))

    assert item.categories == ['国风/山水'], f'实得 {item.categories}'
    assert '国风' in view.scene.category_names
    assert '古风' not in view.scene.category_names


def test_deleting_a_parent_removes_it_from_children_too(view):
    """删父分类时，挂在子分类上的素材也要被摘掉。"""
    from prism.widgets.media_library import ChangeCategory

    item = categorise(view.scene, ['古风/山水'])
    panel = view.category_panel
    view.scene.category_names = ['古风', '古风/山水']

    view.scene.undo_stack.push(ChangeCategory(panel, [], '古风', None))

    assert item.categories == [], f'实得 {item.categories}'


def test_undoing_a_rename_brings_the_children_back(view):
    """撤销要把子分类也还原 —— 那正是走撤销栈的意义。"""
    from prism.widgets.media_library import ChangeCategory

    item = categorise(view.scene, ['古风/山水'])
    panel = view.category_panel
    view.scene.category_names = ['古风', '古风/山水']

    view.scene.undo_stack.push(ChangeCategory(
        panel, ['国风', '国风/山水'], '古风', '国风'))
    assert item.categories == ['国风/山水']

    view.scene.undo_stack.undo()
    assert item.categories == ['古风/山水'], f'实得 {item.categories}'
