from unittest.mock import Mock
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas
from types import SimpleNamespace


class Rectangles(QtWidgets.QStyledItemDelegate):
    def __init__(self, parent):
        super().__init__(parent)
        self.draws = 0

    def paint(self, painter, option, index):
        self.draws += 1
        # Alpha is intentional: progressive publication must not repeatedly
        # composite the same operation into its source buffer.
        painter.fillRect(option.rect, QtGui.QColor(30+index.row()%150, 150, 220, 150))


def host(qapp):
    owner = QtWidgets.QWidget()
    owner.mode, owner.sort_key = 'all', 'name'
    owner.view = SimpleNamespace(scene=SimpleNamespace(project_id='a'),
                                 on_can_undo_changed=lambda _: None,
                                 on_can_redo_changed=lambda _: None)
    canvas = AssetCanvas(owner)
    model = AssetModel(owner)
    canvas.setModel(model)
    canvas._composites.MIN_ITEMS = 100000
    canvas.setItemDelegate(Rectangles(canvas))
    model.replace([SimpleNamespace(_image_size=QtCore.QSizeF(200, 100),
                                   crop=QtCore.QRectF(0, 0, 200, 100),
                                   _canvas_id='a', _title=str(i)) for i in range(500)])
    canvas._tiles.revision = canvas._tiles.content_revision()
    return owner, canvas


def test_one_tile_yields_with_real_pixels_then_matches_atomic_qt(qapp):
    owner, canvas = host(qapp)
    tiles, key = canvas._tiles, (-5, 0, 0)
    assert not tiles._step_build(key)
    assert 0 < canvas.delegate.draws <= 128
    assert key in tiles._jobs and not tiles._fresh(key)
    partial = tiles._jobs[key][0]
    assert partial.sizeInBytes() == tiles.bytes
    assert any(partial.pixelColor(x, y).alpha() for x in range(0,512,8) for y in range(0,512,8))
    for _ in range(100):
        if tiles._step_build(key):
            break
    assert not tiles._jobs and tiles._fresh(key)
    expected = QtGui.QImage(512,512,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    expected.fill(canvas.backgroundBrush().color())
    canvas._render_tile(expected, QtCore.QRectF(expected.rect()), tiles.rect(key))
    assert tiles.tiles[key][0] == expected
    owner.close()


def test_cold_asset_paint_never_calls_atomic_scene_renderer(qapp):
    owner, canvas = host(qapp)
    canvas.scale(.03, .03)
    tiles = canvas._tiles
    atomic = tiles.render = Mock(side_effect=AssertionError('blocking scene.render'))
    output = QtGui.QImage(canvas.viewport().size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    painter = QtGui.QPainter(output)
    tiles.paint(painter)
    painter.end()
    atomic.assert_not_called()
    assert tiles.pending and canvas.delegate.draws == 0
    tiles._work()
    assert canvas.delegate.draws <= 128
    # A new camera must drop the unfinished buffer and its accounted bytes.
    canvas.scale(2, 2)
    painter = QtGui.QPainter(output)
    tiles.paint(painter)
    painter.end()
    assert not tiles._jobs
    assert tiles.bytes == sum(v[0].sizeInBytes() + (v[2].sizeInBytes() if v[2] else 0)
                              for v in tiles.tiles.values())
    tiles.reset()
    owner.close()


def test_annotation_surface_keeps_complete_qt_scene_path(qapp):
    owner, canvas = host(qapp)
    marker = canvas.scene().addRect(1,1,50,50,brush=QtGui.QColor('red'))
    image = QtGui.QImage(512,512,QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    assert canvas.create_display_tile_job(image, QtCore.QRectF(image.rect()),
                                           QtCore.QRectF(0,0,512,512)) is None
    calls = canvas._tiles.render = Mock(wraps=canvas._tiles.render)
    assert canvas._tiles._step_build((0,0,0))
    calls.assert_called_once()
    owner.close()


def test_shared_budget_accounts_for_active_tile_and_preserves_it(qapp):
    owner, canvas = host(qapp)
    tiles = canvas._tiles
    key = (-5,0,0)
    assert not tiles._step_build(key)
    active = tiles._jobs[key][0].sizeInBytes()
    tiles._trim_cache_to(0)
    assert key in tiles._jobs
    assert tiles.budget_bytes() == active
    tiles.invalidate([QtCore.QRectF()])
    assert not tiles._jobs and tiles.budget_bytes() == 0
    owner.close()
