"""三级分类（用户的第 7 条）。

用户的原话是「现在的画布脑图文档都是两级分类，增加到三级分类」。

先说清现状（`_all_categories` 的注释里写着）：**画布下面是扁平的分类
列表，连两级都算不上** —— 文档和脑图有"页面"那一层，画布没有。所以这
一条实际是"给分类加上嵌套"。

做法照标签当年那一套：分类名里带 `/` 就是一层（`古风/山水/秋雨`）。
`tags.py` 的 `split_path` / `join_path` / `is_descendant` 是纯字符串
工具，直接复用 —— 不重新发明。

**兼容是自动的**：旧工程的分类不含 `/`，`split_path` 返回单元素、
`leaf` 返回原名，所以计数、显示、筛选和以前一模一样。
"""
import pytest

from tests.test_material_features import image_item


def categorise(scene, categories):
    """建一个挂了这些分类的素材。"""
    item = image_item(scene)
    item._categories = list(categories)
    return item


# ── 计数 ────────────────────────────────────────────────────────

def test_a_flat_category_counts_as_before(view):
    """**兼容那条**：旧工程的单层分类，计数和以前一样。"""
    categorise(view.scene, ['古风'])
    categorise(view.scene, ['古风'])
    counts = view.category_panel._all_categories()
    assert counts['古风'] == 2
    assert len(counts) == 1, f'不该多出别的行：{sorted(counts)}'


def test_a_parent_counts_its_own_plus_its_children(view):
    """**核心那条**：父分类的数字 = 自己 + 所有子分类。

    点"古风"要能同时看到"古风/山水"里的图，那个数字得对得上 ——
    不然用户点进去发现数量和显示的不一样。
    """
    categorise(view.scene, ['古风'])                 # 直接挂在父级上
    categorise(view.scene, ['古风/山水'])
    categorise(view.scene, ['古风/山水'])
    categorise(view.scene, ['古风/山水/秋雨'])
    counts = view.category_panel._all_categories()

    assert counts['古风'] == 4, '自己 1 + 子 3'
    assert counts['古风/山水'] == 3, '自己 2 + 孙 1'
    assert counts['古风/山水/秋雨'] == 1


def test_an_ancestor_row_appears_for_free(view):
    """`古风/山水` 出现时，`古风` 也得有一行。

    不然子分类挂在半空中，看着像父级丢了 —— 而且用户没法点父级看全部。
    """
    categorise(view.scene, ['古风/山水'])
    counts = view.category_panel._all_categories()
    assert '古风' in counts, f'父级没建出来：{sorted(counts)}'


def test_three_levels_are_supported(view):
    """三层 —— 用户要的正是这个。"""
    categorise(view.scene, ['古风/山水/秋雨'])
    counts = view.category_panel._all_categories()
    for name in ('古风', '古风/山水', '古风/山水/秋雨'):
        assert name in counts, f'{name} 没出现'


def test_a_created_but_empty_category_still_shows(view):
    """建了还没用的分类要显示（计数 0）—— 不然刚建的看不见。"""
    view.scene.category_names = ['古风', '古风/山水']
    counts = view.category_panel._all_categories()
    assert counts['古风'] == 0
    assert counts['古风/山水'] == 0


# ── 界面 ────────────────────────────────────────────────────────

def test_rows_are_indented_by_depth(view):
    """缩进按层级来。显示最后一级的名字，层级由缩进表达。"""
    categorise(view.scene, ['古风/山水/秋雨'])
    panel = view.category_panel
    panel.rebuild_workspace_tree()

    top = panel._cat_items['古风'].text()
    middle = panel._cat_items['古风/山水'].text()
    deep = panel._cat_items['古风/山水/秋雨'].text()

    assert top.startswith('    ') and not top.startswith('        ')
    assert middle.startswith('        ') and not middle.startswith('    ' * 3)
    assert deep.startswith('            ')
    assert '古风' in top and '山水' in middle and '秋雨' in deep


def test_the_tooltip_gives_the_full_path(view):
    """缩进只说层级，不说挂在谁下面 —— 悬停给全路径。"""
    categorise(view.scene, ['古风/山水'])
    panel = view.category_panel
    panel.rebuild_workspace_tree()
    assert panel._cat_items['古风/山水'].toolTip() == '古风/山水'


# ── 筛选 ────────────────────────────────────────────────────────

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
