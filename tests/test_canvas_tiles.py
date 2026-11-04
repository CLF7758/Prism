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


def test_existing_cover_remains_while_group_previews_are_preparing(qapp):
    view, tiles = engine(qapp)
    paint(view, tiles)
    before = tiles.builds
    view.can_build_display_tile = lambda key: False
    tiles._work()
    assert tiles.builds == before
    assert tiles.pending
    assert tiles.coverage_missing == 0
    view.can_build_display_tile = lambda key: True
    tiles._work()
    assert tiles.builds > before
    view.close()


def test_ready_region_is_not_blocked_by_an_unfinished_neighbour(qapp):
    view, tiles = engine(qapp)
    tiles._build((-1, 0, 0))
    blocked, ready = (0, 0, 0), (0, 1, 0)
    tiles.pending[blocked] = tiles.pending[ready] = None
    view.can_build_display_tile = lambda key: key == ready
    tiles._work()
    assert ready in tiles.tiles
    assert blocked not in tiles.tiles
    assert blocked in tiles.pending
    assert tiles._has_cover(blocked), 'The old cover must remain during local refinement'
    view.close()


def test_uncovered_offscreen_prefetch_cannot_bypass_the_work_budget(qapp):
    view, tiles = engine(qapp)
    visible, offscreen = (0,0,0), (0,100,100)
    tiles._build(visible)
    tiles.visible = {visible}
    tiles.pending[offscreen] = None
    view.can_build_display_tile = lambda key: False
    before = tiles.builds
    tiles._work()
    assert tiles.builds == before
    assert offscreen not in tiles.tiles
    assert tiles._has_cover(visible)
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


def test_returning_to_canvas_reuses_pixels_then_refreshes(qapp):
    view, tiles = engine(qapp)
    tiles.switch_context('canvas-a')
    paint(view, tiles)
    while tiles.pending:
        tiles._work()
    before = {key: entry[0].cacheKey() for key, entry in tiles.tiles.items()}
    tiles.switch_context('canvas-b')
    paint(view, tiles)
    tiles.switch_context('canvas-a')
    assert {key: entry[0].cacheKey() for key, entry in tiles.tiles.items()} == before
    assert tiles.stale == set(before)
    builds = tiles.builds
    paint(view, tiles)
    assert tiles.builds == builds  # No synchronous cold-cover rebuild.
    assert tiles.pending  # Cached pixels do not hide later edits.
    tiles.reset()
    assert not tiles.contexts and not tiles.tiles
    view.close()


def test_canvas_contexts_share_one_memory_budget(qapp):
    view, tiles = engine(qapp)
    tiles.BUDGET = 3*512*512*4
    for index in range(20):
        tiles.switch_context(str(index))
        tiles._build((0, index, 0))
        assert tiles.bytes + sum(state[1] for state in tiles.contexts.values()) <= tiles.BUDGET
    assert len(tiles.contexts) <= 2
    view.close()


def test_blank_quadrants_do_not_force_full_library_repaint(qapp):
    view, tiles = engine(qapp)
    view._content_bounds = QtCore.QRectF(0, 0, 512, 256)
    tiles._build((1, 0, 0))
    assert not tiles._has_cover((0, 0, 0)), 'Both populated quadrants need coverage'
    tiles._build((1, 1, 0))
    assert tiles._has_cover((0, 0, 0))
    assert tiles._has_cover((0, 0, 1)), 'A wholly blank region needs no image work'
    image = QtGui.QImage(512, 512, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QtGui.QColor('black'))
    painter = QtGui.QPainter(image)
    tiles._fallback(painter, (0, 0, 0), QtCore.QRectF(0, 0, 512, 512))
    painter.end()
    assert image.pixelColor(100, 100).red() > 200
    assert image.pixelColor(400, 100).red() > 200
    assert image.pixelColor(100, 400) == QtGui.QColor('black')
    view.close()


def test_canvas_method_is_not_treated_as_packed_content_bounds(qapp):
    view, tiles = engine(qapp)
    view._content_bounds = lambda: QtCore.QRectF(0, 0, 100, 100)
    assert not tiles._has_cover((0, 0, 0))
    for x in range(2):
        for y in range(2):
            tiles._build((1, x, y))
    assert tiles._has_cover((0, 0, 0))
    view.close()


def test_far_content_cover_survives_lru_pressure_from_deep_zoom(qapp):
    view, tiles = engine(qapp)
    tiles._build((-7, 0, 0))
    tiles._build((-7, 1, 0))
    before = {key:tiles.tiles[key][0].cacheKey() for key in tiles.tiles}
    for x in range(90):
        tiles._build((0, x, 0))
    assert tiles.bytes <= tiles.BUDGET
    assert all(key in tiles.tiles and tiles.tiles[key][0].cacheKey()==value for key,value in before.items())
    assert tiles._has_cover((-6, 2, 0))
    view.close()
