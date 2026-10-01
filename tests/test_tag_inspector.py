"""The inspector's tag block: tagging the selection instead of browsing."""
from PyQt6 import QtCore

from prism import tags
from prism.widgets import tag_panel
from tests.test_material_features import image_item


def panel_of(view):
    view._detail_panel._on_selection_changed()
    return view._detail_panel


def test_only_the_tags_of_the_selection_are_offered(view):
    item = image_item(view.scene, ['雨夜', '红墙'])
    view.scene.tag_names = ['雨夜', '红墙', '草图', '材质', '灯光']
    item.setSelected(True)
    detail = panel_of(view)
    assert set(detail._tag_checks) == {'雨夜', '红墙'}


def test_searching_offers_the_matching_tags(view):
    item = image_item(view.scene, ['红墙'])
    view.scene.tag_names = ['红墙', '建筑/红墙', '草图']
    item.setSelected(True)
    detail = panel_of(view)
    detail._tag_search.setText('红墙')
    assert set(detail._tag_checks) == {'红墙', '建筑/红墙'}


def test_a_carried_tag_is_ticked(view):
    item = image_item(view.scene, ['雨夜'])
    item.setSelected(True)
    detail = panel_of(view)
    assert detail._tag_checks['雨夜'].checkState() == \
        QtCore.Qt.CheckState.Checked


def test_a_partly_shared_tag_is_shown_as_partly_checked(view):
    first = image_item(view.scene, ['雨夜', '红墙'])
    second = image_item(view.scene, ['雨夜'])
    first.setSelected(True)
    second.setSelected(True)
    detail = panel_of(view)
    assert detail._tag_checks['雨夜'].checkState() == \
        QtCore.Qt.CheckState.Checked
    assert detail._tag_checks['红墙'].checkState() == \
        QtCore.Qt.CheckState.PartiallyChecked


def test_unticking_a_tag_removes_it_from_every_selected_item(view):
    first = image_item(view.scene, ['雨夜', '红墙'])
    second = image_item(view.scene, ['雨夜'])
    first.setSelected(True)
    second.setSelected(True)
    detail = panel_of(view)
    detail._tag_checks['雨夜'].setChecked(False)
    assert first.tags == ['红墙']
    assert second.tags == []
    view.undo_stack.undo()
    assert second.tags == ['雨夜']


def test_typing_a_new_tag_creates_and_assigns_it(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    detail = panel_of(view)
    detail._tag_search.setText('场景:雨夜')
    detail._add_typed_tag()
    assert set(item.tags) == {'红墙', '场景:雨夜'}
    assert '场景:雨夜' in view.scene.tag_names
    assert detail._tag_search.text() == ''


def test_creating_a_nested_tag_keeps_the_path(view):
    item = image_item(view.scene, [])
    item.setSelected(True)
    detail = panel_of(view)
    detail._tag_search.setText('建筑/红墙')
    detail._add_typed_tag()
    assert item.tags == ['建筑/红墙']


def test_a_synonym_shows_both_names(view):
    item = image_item(view.scene, ['霓虹'])
    item.setSelected(True)
    tag_panel.set_synonym(view, '霓虹', 'neon')
    detail = panel_of(view)
    # The row keeps the name the item carries and mentions the other one.
    box = detail._tag_checks['霓虹']
    assert 'neon' in box.text()


def test_implied_parents_are_explained_in_the_tooltip(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    tag_panel.set_parent(view, '红墙', '建筑')
    detail = panel_of(view)
    assert '建筑' in detail._tag_checks['红墙'].toolTip()


def test_namespace_tags_are_coloured(view):
    item = image_item(view.scene, ['场景:雨夜'])
    item.setSelected(True)
    detail = panel_of(view)
    colour = tags.namespace_color('场景:雨夜')
    assert colour in detail._tag_checks['场景:雨夜'].styleSheet()


def test_the_tag_search_box_is_never_disabled(view):
    detail = view._detail_panel
    detail._tag_search.setText('anything')
    assert detail._tag_search.isEnabled()


def test_no_selection_keeps_the_panel_quiet(view):
    view.scene.tag_names = ['红墙', '草图']
    detail = panel_of(view)
    assert detail._tag_checks == {}
    assert detail._tags_hint is not None


def test_a_search_without_hits_suggests_creating_the_tag(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    detail = panel_of(view)
    detail._tag_search.setText('没有这个标签')
    assert detail._tags_hint is not None
    assert '没有这个标签' in detail._tags_hint.text()


def test_the_inspector_follows_the_manager(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    detail = panel_of(view)
    tag_panel.set_synonym(view, '红墙', '砖墙')
    assert '砖墙' in detail._tag_checks['红墙'].text()


def test_a_renamed_tag_follows_the_selection(view):
    item = image_item(view.scene, ['红墙'])
    item.setSelected(True)
    detail = panel_of(view)
    tag_panel.rename_tag(view, '红墙', '砖墙')
    assert '砖墙' in detail._tag_checks
    assert item.tags == ['砖墙']
