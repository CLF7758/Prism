"""T10：分类分配对话框。

从 `view.py` 搬出来之前，那段代码有 43 行，混着界面构建和业务判断，还
写了两处硬编码中文（源码里是 ``'\\u5206\\u7c7b'`` 那种字面转义，不走
翻译表）。现在对话框自己能被测。
"""
import pytest

from prism.widgets.category_dialog import CategoryAssignmentDialog


def _texts(dialog):
    return [dialog._list.item(i).text()
            for i in range(dialog._list.count())]


def _checked(dialog):
    return [dialog._list.item(i).text()
            for i in range(dialog._list.count())
            if dialog._list.item(i).checkState()
            == dialog._list.item(i).checkState().Checked]


# ── 列表内容 ─────────────────────────────────────────────────────

def test_it_lists_the_available_categories_sorted(qapp):
    """按 Unicode 码点排，不是拼音 —— '夜'(U+591C) 在 '建'(U+5EFA) 前面。"""
    names = ['夜景', '建筑', '人物']
    dialog = CategoryAssignmentDialog(list(names))
    assert _texts(dialog) == sorted(names)


def test_duplicates_collapse(qapp):
    dialog = CategoryAssignmentDialog(['建筑', '建筑', '夜景'])
    assert _texts(dialog) == sorted(['建筑', '夜景'])


def test_blank_names_are_dropped(qapp):
    """str(None) 是 'None'，不是空字符串 —— 先挡 None 再 strip。"""
    dialog = CategoryAssignmentDialog(['建筑', '', '   ', None])
    assert _texts(dialog) == ['建筑']


def test_an_empty_catalogue_gives_an_empty_list(qapp):
    assert _texts(CategoryAssignmentDialog([])) == []
    assert _texts(CategoryAssignmentDialog(None)) == []


# ── 勾选状态 ─────────────────────────────────────────────────────

def test_existing_categories_start_checked(qapp):
    dialog = CategoryAssignmentDialog(['人物', '建筑', '夜景'],
                                      current=['建筑'])

    assert dialog.chosen() == ['建筑']


def test_nothing_is_checked_without_a_current_selection(qapp):
    dialog = CategoryAssignmentDialog(['人物', '建筑'])
    assert dialog.chosen() == []


def test_chosen_returns_them_in_list_order(qapp):
    dialog = CategoryAssignmentDialog(['人物', '建筑', '夜景'],
                                      current=['夜景', '人物'])
    assert dialog.chosen() == ['人物', '夜景'], '按列表顺序，不是传入顺序'


def test_checked_states_can_be_changed(qapp):
    dialog = CategoryAssignmentDialog(['人物', '建筑'])

    assert dialog.set_checked('建筑') is True
    assert dialog.chosen() == ['建筑']

    assert dialog.set_checked('建筑', False) is True
    assert dialog.chosen() == []


def test_setting_an_unknown_name_reports_false(qapp):
    dialog = CategoryAssignmentDialog(['人物'])
    assert dialog.set_checked('没这个') is False


def test_current_names_not_in_the_catalogue_are_ignored(qapp):
    """素材上挂着一个已经从目录里删掉的分类，不该炸。"""
    dialog = CategoryAssignmentDialog(['人物'], current=['已删除的分类'])
    assert dialog.chosen() == []
    assert _texts(dialog) == ['人物']


# ── 它只管问，不管写 ─────────────────────────────────────────────

def test_the_dialog_does_not_touch_the_scene(qapp):
    """写回和推撤销栈是调用方的事 —— 这个对话框不碰 scene。

    所以它不需要 view 参数，能独立构造、独立测。
    """
    dialog = CategoryAssignmentDialog(['建筑'])
    assert not hasattr(dialog, 'scene')
    assert not hasattr(dialog, 'view')
