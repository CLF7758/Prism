"""Editing tags through the panel: synonym, nesting, rename, undo."""
from PyQt6 import QtWidgets

from prism import tags
from prism.widgets import tag_panel
from tests.test_material_features import image_item


def names_of(scene):
    return tags.scene_tag_names(scene)


def test_panel_opens_with_the_project_tags(view):
    from PyQt6.QtCore import Qt
    view.scene.tag_names = ['建筑', '建筑/红墙']
    panel = tag_panel.TagPanel(view)
    shown = [item.data(Qt.ItemDataRole.UserRole)['name']
             for item in panel._tree.findItems('*', Qt.MatchFlag.MatchWildcard)]
    assert '建筑' in shown and '建筑/红墙' in shown
    panel.close()


def test_tag_rows_are_no_longer_draggable(view):
    """拖拽嵌套去掉了 —— 用户要求标签是平铺的。

    以前拖一个标签到另一个上面就变成它的子标签。现在拖不动了：
    `TagDropList` 整个不做拖放。数据层（`tags.py`）没动，所以已有的
    `建筑/红墙` 这种两级标签照常能用、能筛选。
    """
    from PyQt6.QtWidgets import QAbstractItemView

    view.scene.tag_names = ['建筑', '红墙']
    image_item(view.scene, ['红墙'])
    panel = tag_panel.TagPanel(view)

    # 列表既拖不动、也不接受别人放进来
    assert panel._tree.dragEnabled() is False, '标签不该能拖'
    assert panel._tree.acceptDrops() is False, '标签行不该接受放下'
    assert panel._tree.dragDropMode() == \
        QAbstractItemView.DragDropMode.NoDragDrop

    # 层级一个都没变
    assert '红墙' in names_of(view.scene)
    assert '建筑/红墙' not in names_of(view.scene)
    assert list(view.scene.items_for_save())[0].tags == ['红墙']
    panel.close()


def test_nesting_a_tag_to_its_own_child_does_nothing(view):
    """（这条原来验的是"不能套成环"。现在连嵌套都不存在了，
    所以它验的是"什么都不发生"—— 保留它是因为那个约束本身还有效：
    底层的 `rename_tag` 仍然拒绝这种改法。）"""
    view.scene.tag_names = ['建筑', '建筑/红墙']
    panel = tag_panel.TagPanel(view)
    assert tag_panel.rename_tag(view, '建筑', '红墙',
                                new_parent='建筑/红墙') is None
    assert '建筑' in names_of(view.scene)
    assert '建筑/红墙' in names_of(view.scene)
    panel.close()


def test_a_tag_keeps_its_path_when_nothing_is_dropped(view):
    """（原来验的是"拖到空白处就回到顶层"。拖拽没了之后，
    路径只能靠改名改，不能靠拖动 —— 这条确认层级不会自己变。）"""
    image_item(view.scene, ['建筑/红墙'])
    panel = tag_panel.TagPanel(view)
    assert '建筑/红墙' in names_of(view.scene)
    assert tags.is_descendant('建筑/红墙', '建筑')
    panel.close()


def test_renaming_a_tag_takes_its_children_along(view):
    image_item(view.scene, ['建筑/红墙', '建筑/红墙/旧'])
    panel = tag_panel.TagPanel(view)
    tag_panel.rename_tag(view, '建筑', '构筑')
    assert '构筑/红墙' in names_of(view.scene)
    assert '构筑/红墙/旧' in names_of(view.scene)
    assert set(list(view.scene.items_for_save())[0].tags) == \
        {'构筑/红墙', '构筑/红墙/旧'}
    panel.close()


def test_renaming_a_tag_keeps_the_synonym_table_pointing_at_it(view):
    image_item(view.scene, ['霓虹'])
    panel = tag_panel.TagPanel(view)
    tag_panel.set_synonym(view, '霓虹', 'neon')
    tag_panel.rename_tag(view, 'neon', '灯光')
    assert tags.tag_system(view.scene).canonical('霓虹') == '灯光'
    panel.close()


def test_deleting_a_tag_removes_it_from_items_and_catalogue(view):
    image_item(view.scene, ['草图', '红墙'])
    panel = tag_panel.TagPanel(view)
    tag_panel.delete_tag(view, '草图')
    assert '草图' not in names_of(view.scene)
    assert list(view.scene.items_for_save())[0].tags == ['红墙']
    panel.close()


def test_deleting_a_parent_keeps_its_children_when_asked(view):
    image_item(view.scene, ['建筑', '建筑/红墙'])
    panel = tag_panel.TagPanel(view)
    tag_panel.delete_tag(view, '建筑', keep_children=True)
    assert '建筑' not in names_of(view.scene)
    assert '红墙' in names_of(view.scene)
    assert sorted(list(view.scene.items_for_save())[0].tags) == ['红墙']
    panel.close()


def test_every_edit_can_be_undone(view):
    item = image_item(view.scene, ['霓虹'])
    panel = tag_panel.TagPanel(view)
    tag_panel.set_synonym(view, '霓虹', 'neon')
    assert tags.tag_system(view.scene).canonical('霓虹') == 'neon'
    view.undo_stack.undo()
    assert tags.tag_system(view.scene).canonical('霓虹') == '霓虹'
    tag_panel.rename_tag(view, '霓虹', '灯光')
    view.undo_stack.undo()
    assert item.tags == ['霓虹']
    assert '霓虹' in names_of(view.scene)
    panel.close()


def test_adding_tags_does_not_touch_the_items(view):
    item = image_item(view.scene, ['红墙'])
    panel = tag_panel.TagPanel(view)
    tag_panel.add_tags(view, ['场景:雨夜'])
    assert '场景:雨夜' in names_of(view.scene)
    assert item.tags == ['红墙']
    panel.close()


def test_the_panel_refreshes_when_the_tags_change(view):
    panel = tag_panel.TagPanel(view)
    tag_panel.add_tags(view, ['新标签'])
    assert '新标签' in panel._rows
    panel.close()


def test_synonyms_show_up_in_one_row(view):
    image_item(view.scene, ['霓虹'])
    panel = tag_panel.TagPanel(view)
    tag_panel.set_synonym(view, '霓虹', 'neon')
    row = panel._rows.get('neon')
    assert row is not None
    assert '霓虹' in row.text()
    assert '霓虹' not in panel._rows
    panel.close()


def test_the_manager_keeps_the_sidebar_and_inspector_in_step(view):
    panel = tag_panel.TagPanel(view)
    tag_panel.add_tags(view, ['场景:雨夜'])
    library = view.category_panel
    # 左栏标签行随「筛选与旧分类」列表一起删掉了，同步的对象换成面板的
    # 标签目录（管理器加一个标签，面板这一侧要立刻看得见）。
    assert '场景:雨夜' in {node.name for node in library._tag_nodes()}
    panel.close()


def test_imported_tree_is_added_as_nested_tags(view):
    panel = tag_panel.TagPanel(view)
    tag_panel.add_tags(view, tags.parse_tree_text('场景\n    雨夜\n    白天'))
    assert '场景/雨夜' in names_of(view.scene)
    assert '场景/白天' in names_of(view.scene)
    panel.close()


def test_close_button_disconnects_the_signal(view):
    panel = tag_panel.TagPanel(view)
    panel.close()
    # Changing the tags after the dialog is gone must not call into it.
    tag_panel.add_tags(view, ['之后'])
    assert QtWidgets.QApplication.instance() is not None
