"""Real canvas geometry contracts for the PID56008 hot-path correction."""
from array import array
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas, _CardRows


@pytest.fixture
def canvas(qapp):
    owner = QtWidgets.QWidget()
    owner.mode = 'all'
    owner.sort_key = 'name'
    owner.view = SimpleNamespace(
        scene=SimpleNamespace(project_id='selection-geometry'),
        on_can_undo_changed=lambda _value: None,
        on_can_redo_changed=lambda _value: None)
    host = AssetCanvas(owner)
    model = AssetModel(owner)
    host.setModel(model)
    model.replace([SimpleNamespace(_image_size=QtCore.QSizeF(256, 128),
                                   crop=QtCore.QRectF(0, 0, 256, 128),
                                   _canvas_id='a', _title=str(row))
                   for row in range(8)])
    host.arrange()
    yield host, model
    owner.close()


def select_rows(host, rows):
    selection = QtCore.QItemSelection()
    for row in rows:
        index = host.model().index(row)
        selection.select(index, index)
    host.selectionModel().select(
        selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)


@pytest.mark.parametrize('count', [64, 100000], ids=['small', 'dense100k'])
def test_selected_foreground_uses_geometry_without_facades(canvas, count):
    host, model = canvas
    # Dense overlap is constructed directly in the committed representation,
    # avoiding a 100k packing job. All methods under test remain real.
    model.items = [model.items[0]] * count
    host.cards = _CardRows(host, count)
    host._geometry.x = array('d', [20]) * count
    host._geometry.y = array('d', [20]) * count
    host._geometry.w = array('d', [156]) * count
    host._geometry.h = array('d', [78]) * count
    host._spatial = {(0, 0): set(range(count)), (1, 0): set(range(count))}
    select_rows(host, [0, count-1])

    assert host.selected_scene_bounds() == QtCore.QRectF(20, 20, 156, 78)
    image = QtGui.QImage(220, 140, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        host.drawForeground(painter, QtCore.QRectF(0, 0, 220, 140))
    finally:
        painter.end()

    assert all(card is None for card in host.cards.records)
    assert image.pixelColor(20, 50).alpha() > 0
    assert image.pixelColor(100, 50).alpha() == 0


def test_visible_rows_preserves_sorted_stacking_and_topmost_hit(canvas):
    host, _model = canvas
    for card in host.cards:
        card.setPos(20, 20)
    query = QtCore.QRectF(30, 30, 1, 1)
    host.cards = _CardRows(host, len(host.cards))

    rows = host.visible_rows(query)
    assert rows == list(range(8))
    assert all(card is None for card in host.cards.records)
    assert [card.row for card in host.visible_cards(query)] == rows
    point = host.mapFromScene(QtCore.QPointF(30, 30))
    assert host.indexAt(point).row() == 7
    assert host.visible_rows(QtCore.QRectF()) == []
    assert host.visible_rows(QtCore.QRectF(-500, -500, 1, 1)) == []
    assert len(host.visible_rows(query, limit=2)) == 2


def test_crop_updates_geometry_spatial_cells_and_selection_immediately(canvas):
    host, model = canvas
    for card in host.cards:
        card.setPos(-1000-card.row*200, -1000)
    cached = host.cards[0]
    cached.setPos(60, 30)
    select_rows(host, [0])
    old_bounds = host.selected_scene_bounds()
    revision = host._render_layout_revision
    model.items[0].crop = QtCore.QRectF(0, 0, 128, 256)

    host.catalog_rows_changed([0])

    expected = QtCore.QRectF(60, 30, 78, 156)
    assert host._card_rect(0) == expected
    assert cached.sceneBoundingRect() == expected
    assert host.selected_scene_bounds() == expected
    assert old_bounds != expected
    assert host._render_layout_revision > revision
    assert 0 not in host.visible_rows(QtCore.QRectF(180, 50, 1, 1))
    assert 0 in host.visible_rows(QtCore.QRectF(80, 170, 1, 1))
    assert 0 not in host._spatial.get((1, 0), ())
    assert 0 in host._spatial.get((0, 1), ())


def test_manual_move_and_commit_size_keep_facade_and_geometry_consistent(canvas):
    host, _model = canvas
    card = host.cards[0]
    select_rows(host, [0])
    host.selected_scene_bounds()

    card.setPos(-420, 530)

    assert host._card_rect(0) == card.sceneBoundingRect()
    assert host.selected_scene_bounds() == card.sceneBoundingRect()
    assert 0 in host.visible_rows(QtCore.QRectF(-410, 540, 1, 1))
    host._commit_card_geometry(0, -420, 530, 31, 47)
    assert card.sceneBoundingRect() == QtCore.QRectF(-420, 530, 31, 47)
    assert host._card_rect(0) == card.sceneBoundingRect()


def test_empty_selection_has_no_bounds_or_foreground(canvas):
    host, _model = canvas
    host.selectionModel().clearSelection()
    assert host.selected_scene_bounds().isEmpty()
    host.cards = _CardRows(host, len(host.cards))
    image = QtGui.QImage(40, 40, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtCore.Qt.GlobalColor.transparent)
    painter = QtGui.QPainter(image)
    try:
        host.drawForeground(painter, QtCore.QRectF(0, 0, 40, 40))
    finally:
        painter.end()
    assert all(card is None for card in host.cards.records)
    assert image.pixelColor(20, 20).alpha() == 0


def expand_canvas(host, model, count):
    model.items = [model.items[0]] * count
    host.cards = _CardRows(host, count)
    for name, value in [('x', 20), ('y', 20), ('w', 156), ('h', 78)]:
        setattr(host._geometry, name, array('d', [value])*count)


def test_large_model_notification_yields_between_bounded_geometry_batches(canvas, monkeypatch):
    host, model = canvas
    expand_canvas(host, model, 800)
    queued, visited = [], []
    original = host.catalog_rows_changed
    def collect(rows):
        rows = list(rows)
        visited.append(rows)
        original(rows)
    monkeypatch.setattr(host, 'catalog_rows_changed', collect)
    monkeypatch.setattr(QtCore.QTimer, 'singleShot', lambda delay, cb: queued.append(cb))
    host._model_data_changed(model.index(0), model.index(799))
    assert visited == []
    while queued:
        queued.pop(0)()
    assert [row for batch in visited for row in batch] == list(range(800))
    assert max(map(len, visited)) <= 256
    assert all(card is None for card in host.cards.records)
    assert host._metadata_refresh_state is None


def test_model_replacement_cancels_queued_old_metadata(canvas, monkeypatch):
    host, model = canvas
    expand_canvas(host, model, 800)
    queued, visited = [], []
    monkeypatch.setattr(host, 'catalog_rows_changed', lambda rows: visited.extend(rows))
    monkeypatch.setattr(QtCore.QTimer, 'singleShot', lambda delay, cb: queued.append(cb))
    host._model_data_changed(model.index(0), model.index(799))
    model.replace(model.items[:2])
    while queued:
        queued.pop(0)()
    assert visited == []


def test_manual_move_during_metadata_capture_is_not_rearranged_later(canvas, monkeypatch):
    host, model = canvas
    expand_canvas(host, model, 800)
    model.items[0].crop = QtCore.QRectF(0, 0, 128, 256)
    queued = []
    monkeypatch.setattr(QtCore.QTimer, 'singleShot', lambda delay, cb: queued.append(cb))
    host._model_data_changed(model.index(0), model.index(799))
    queued.pop(0)()
    host.cards[0].setPos(700, -200)
    while queued:
        queued.pop(0)()
    assert host._card_rect(0).topLeft() == QtCore.QPointF(700, -200)
    assert not host._metadata_layout_timer.isActive()


def test_new_model_does_not_inherit_previous_rows_dimensions(canvas):
    host, model = canvas
    model.replace([SimpleNamespace(_image_size=QtCore.QSizeF(64, 256),
                                   crop=QtCore.QRectF(), _canvas_id='b')])
    assert host._card_rect(0).size() == QtCore.QSizeF(39, 156)
    assert host.cards[0].boundingRect().size() == QtCore.QSizeF(39, 156)


def test_facade_recreated_during_pending_grid_uses_committed_geometry(canvas):
    host, model = canvas
    expected = host._card_rect(0)
    host.cards = _CardRows(host, len(host.cards))
    host.setGridSize(QtCore.QSize(220, 218))
    assert host.cards[0].sceneBoundingRect() == expected
    host._grid_timer.stop()
