from PyQt6 import QtCore, QtGui, QtWidgets
from prism.canvas_tiles import CanvasTiles


def engine(qapp):
    view = QtWidgets.QGraphicsView()
    view.setScene(QtWidgets.QGraphicsScene())
    view.resize(700, 500)
    view.scene().setSceneRect(-10000, -10000, 20000, 20000)
    def render(image, target, source):
        image.fill(QtGui.QColor('red'))
    return view, CanvasTiles(view, render)


def paint(view, tiles):
    image = QtGui.QImage(view.viewport().size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    painter = QtGui.QPainter(image)
    tiles.paint(painter)
    painter.end()
    return image


def test_tiles_reuse_and_local_invalidation(qapp):
    view, tiles = engine(qapp)
    paint(view, tiles)
    while tiles.pending:
        tiles._work()
    before = {k: v[0].cacheKey() for k, v in tiles.tiles.items()}
    paint(view, tiles)
    assert all(tiles.tiles[k][0].cacheKey() == v for k, v in before.items())
    key = next(iter(tiles.visible))
    tiles.invalidate([tiles.rect(key).adjusted(1, 1, -1, -1)])
    assert key in tiles.stale
    assert len(tiles.stale) < len(tiles.tiles)
    paint(view, tiles)
    while tiles.pending:
        tiles._work()
    assert tiles.tiles[key][0].cacheKey() != before[key]
    view.close()


def test_negative_coordinates_and_budget(qapp):
    view, tiles = engine(qapp)
    assert tiles.keys(QtCore.QRectF(-512, -512, 512, 512), 0) == [(0, -1, -1)]
    tiles.BUDGET = 3 * 512 * 512 * 4
    for x in range(20):
        tiles._build((0, x, 0))
    assert tiles.bytes <= tiles.BUDGET
    assert len(tiles.tiles) <= 3
    view.close()


def test_zoom_uses_parent_cover(qapp):
    view, tiles = engine(qapp)
    image = paint(view, tiles)
    assert image.pixelColor(image.width()//2, image.height()//2).red() > 200
    view.scale(2, 2)
    image = paint(view, tiles)
    assert image.pixelColor(image.width()//2, image.height()//2).red() > 200
    assert tiles.pending
    view.close()


def test_existing_remote_cache_cannot_leave_new_viewport_black(qapp):
    view, tiles = engine(qapp)
    tiles._build((0, 15, 15))
    image = paint(view, tiles)
    assert tiles.coverage_missing == 0
    for x in range(20, image.width(), 80):
        for y in range(20, image.height(), 80):
            assert image.pixelColor(x, y).red() > 200
    view.close()


def test_prefetch_does_not_evict_the_visible_cover(qapp):
    view, tiles = engine(qapp)
    paint(view, tiles)
    tiles.BUDGET = max(8*512*512*4, tiles.bytes)
    for x in range(30, 60):
        tiles._build((tiles.level, x, 0))
    assert all(tiles._has_cover(k) for k in tiles.visible)
    assert tiles.bytes <= tiles.BUDGET
    view.close()
