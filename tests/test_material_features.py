"""Cross-component regressions for material organization and external drag."""
from PyQt6 import QtCore, QtGui

from prism import commands
from prism.items import PrismPixmapItem
from prism.fileio.sql import SQLiteIO
from prism.similarity import difference_hash, duplicate_groups, hamming_distance


def image_item(scene, tags=(), rating=0):
    image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image)
    item._tags = list(tags)
    item._rating = rating
    scene.addItem(item)
    return item


def test_rating_tags_save_roundtrip(view, tmp_path):
    item = image_item(view.scene, ['夜景', '红墙'], 4)
    path = str(tmp_path / 'metadata.prism')
    SQLiteIO(path, view.scene, create_new=True).write()
    view.scene.clear()
    SQLiteIO(path, view.scene, readonly=True).read()
    view.scene.add_queued_items()
    restored = list(view.scene.items_for_save())[0]
    assert restored.tags == ['夜景', '红墙']
    assert restored.rating == 4
    assert restored.create_copy().rating == 4


def test_combined_filter_and_undo(view):
    a = image_item(view.scene, ['night', 'red'], 4)
    b = image_item(view.scene, ['night'], 5)
    panel = view.category_panel
    panel.update_counts()
    panel._active_tags = {'night', 'red'}
    panel.rating_filter.setCurrentIndex(4)
    panel._apply_filter()
    assert a.isVisible() and not b.isVisible()
    view.undo_stack.push(commands.ChangeMetadata([a], 'rating', [2]))
    assert not a.isVisible()
    view.undo_stack.undo()
    assert a.rating == 4 and a.isVisible()
    panel.clear_filter()
    assert a.isVisible() and b.isVisible()
    assert panel._active_tags == set(), '清空筛选也要把勾上的标签放下'


def test_duplicate_scan_never_hides_canvas_items(view):
    a, b = image_item(view.scene), image_item(view.scene)
    panel = view.category_panel
    groups = duplicate_groups([a, b])
    panel.update_counts()
    assert groups == [[a, b]]
    assert a.isVisible() and b.isVisible()


def test_embedded_image_external_path(view):
    item = image_item(view.scene)
    path = view._external_path_for_item(item, 1)
    assert not QtGui.QImage(path).isNull()


def test_hash_same_image(view):
    item = image_item(view.scene)
    assert hamming_distance(difference_hash(item.pixmap()),
                            difference_hash(item.pixmap().toImage())) == 0


def test_duplicate_scan_rejects_flat_color_hash_collision(view):
    red = image_item(view.scene)
    blue_image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
    blue_image.fill(QtGui.QColor('blue'))
    blue = PrismPixmapItem(blue_image)
    view.scene.addItem(blue)
    assert duplicate_groups([red, blue]) == []


def test_batch_tags_keep_individual_tags(view):
    a = image_item(view.scene, ['common', 'one'])
    b = image_item(view.scene, ['common', 'two'])
    a.setSelected(True)
    b.setSelected(True)
    detail = view._detail_panel
    detail._on_selection_changed()
    # Tags are added through the search box now; the checkboxes only list
    # tags the project already knows about.
    detail._tag_search.setText('added')
    detail._add_typed_tag()
    assert set(a.tags) == {'common', 'one', 'added'}
    assert set(b.tags) == {'common', 'two', 'added'}
    view.undo_stack.undo()
    assert set(a.tags) == {'common', 'one'}
    assert set(b.tags) == {'common', 'two'}


def test_new_tag_does_not_activate_category_filter(view):
    a = image_item(view.scene)
    view.scene.category_names = ['category']
    panel = view.category_panel
    panel.update_counts()
    panel.clear_filter()
    # 清空就是真的清空：分类筛选的界面入口随左栏旧列表删掉了，不会再
    # "落到第一个分类" —— 那样会留下一个用户看不见的筛选。
    assert panel._active_filter is None
    before = panel._active_filter
    a.tags = ['new tag']
    # Adding a tag must not change what the board is showing.
    assert panel._active_filter == before
