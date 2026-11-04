"""Layout invalidation, exact keys and bounded recursive packing contracts."""
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6 import QtCore, QtWidgets

from prism import compact_layout
from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas


def canvas(qapp, count=150):
    owner = QtWidgets.QWidget()
    owner.mode = 'all'
    owner.sort_key = 'name'
    owner.view = SimpleNamespace(
        scene=SimpleNamespace(project_id='a'),
        on_can_undo_changed=lambda value: None,
        on_can_redo_changed=lambda value: None)
    host = AssetCanvas(owner)
    model = AssetModel(owner)
    host.setModel(model)
    model.replace([SimpleNamespace(_image_size=QtCore.QSizeF(256, 128),
                                  crop=QtCore.QRectF(0, 0, 256, 128),
                                  _canvas_id='a', _title=str(i)) for i in range(count)])
    return owner, host, model


def test_unchanged_layout_keeps_camera_indexes_and_group_pixels(qapp):
    owner, host, model = canvas(qapp)
    host.scale(1.8, 1.8)
    host.horizontalScrollBar().setValue(80)
    transform = host.transform()
    scroll = host.horizontalScrollBar().value()
    revision = host._render_layout_revision
    epoch = host._composites.epoch
    index = host._spatial
    with patch.object(host, '_apply_positions', wraps=host._apply_positions) as apply:
        host.arrange()
        apply.assert_not_called()
    assert host._spatial is index
    assert host._render_layout_revision == revision
    assert host._composites.epoch == epoch
    assert host.transform() == transform
    assert host.horizontalScrollBar().value() == scroll
    owner.close()


def test_key_covers_unsampled_rows_grouping_sort_and_project(qapp):
    owner, host, model = canvas(qapp, 200)
    key = host._layout_cache_key([])
    model.items[1], model.items[2] = model.items[2], model.items[1]
    assert key != host._layout_cache_key([])
    key = host._layout_cache_key([])
    model.items[1]._canvas_id = 'b'
    assert key != host._layout_cache_key([])
    key = host._layout_cache_key([])
    owner.sort_key = 'date'
    assert key != host._layout_cache_key([])
    key = host._layout_cache_key([])
    owner.view.scene.project_id = 'reopened'
    assert key != host._layout_cache_key([])
    owner.close()


def test_crop_data_change_updates_hit_geometry_and_spatial_index(qapp):
    owner, host, model = canvas(qapp)
    old_size = QtCore.QSizeF(host.cards[1]._size)
    revision = host._render_layout_revision
    model.items[1].crop = QtCore.QRectF(0, 0, 128, 256)
    model.dataChanged.emit(model.index(1), model.index(1))
    assert host.cards[1]._size != old_size
    assert host._render_layout_revision > revision
    card = host.cards[1]
    assert card in host.visible_cards(card.sceneBoundingRect())
    owner.close()


def test_same_bounds_model_change_invalidates_render_revision(qapp):
    owner, host, model = canvas(qapp, 2)
    before = host._tiles.content_revision()
    model.replace(list(reversed(model.items)))
    assert before != host._tiles.content_revision()
    owner.close()


def test_manual_move_invalidates_applied_layout_and_render_revision(qapp):
    owner, host, model = canvas(qapp, 2)
    before = host._tiles.content_revision()
    host.cards[0].setPos(30, 40)
    assert host._applied_layout_key is None
    assert before != host._tiles.content_revision()
    owner.close()


def test_packing_parent_layers_also_remain_bounded():
    compact_layout._group_cache.clear()
    compact_layout._pack_cache.clear()
    original = compact_layout.rpack.pack
    sizes = [(10 + i % 7, 8 + i % 5) for i in range(1601)]
    calls = []

    def bounded(rectangles, **kwargs):
        calls.append(len(rectangles))
        assert len(rectangles) <= compact_layout.GROUP
        return original(rectangles, **kwargs)

    with patch.object(compact_layout.rpack, 'pack', side_effect=bounded):
        positions, bounds = compact_layout.pack_sizes(sizes)
    assert calls and len(positions) == len(sizes)
    for (x, y), (w, h) in zip(positions, sizes):
        assert x >= 0 and y >= 0
        assert x+w <= bounds[0] and y+h <= bounds[1]
    # At this scale a sweep is cheap and checks parent offsets, not just sizes.
    for i, ((x, y), (w, h)) in enumerate(zip(positions, sizes)):
        for (xx, yy), (ww, hh) in zip(positions[i+1:], sizes[i+1:]):
            assert x+w <= xx or xx+ww <= x or y+h <= yy or yy+hh <= y
