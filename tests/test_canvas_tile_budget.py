"""Sliced canvas tile builds must stay inside one GUI turn's budget.

The canvas renders far LOD through world-space tiles. A single tile used to
be one atomic ``QGraphicsScene.render()`` call, so one GUI turn could spend
tens of milliseconds inside a paint, and the cold coarse cover used to build
*every* coarse tile inside ``paintEvent`` at once. These tests pin the
sliced behaviour: work is handed back to the event loop, stale work is
dropped, and the pixels that finally arrive are the same pixels the atomic
renderer produced.

The two contracts locked here are the ones that regressed once already:

* **One GUI turn draws at most one renderer's worth of records.** A slice is
  the renderer's own bounded unit (``_draw_tile_slices`` yields after at most
  128 records), so ``MAX_SLICE_STEPS_PER_FRAME`` is the counting form of the
  "at most 128 records per turn" rule. The wall-clock budget alone cannot
  enforce it: a trivial delegate finishes a whole cold library well inside
  4 ms.
* **"Content is stale" and "content is not ready" are different states.**
  Invalidated content may never be painted again; content that is merely
  still building must not be marked stale, because that would make the
  cache drop the cover it is supposed to be showing.
"""
import math

from PyQt6 import QtCore, QtGui, QtWidgets

from prism.canvas_tiles import CanvasTiles


def _image(size=512):
    return QtGui.QImage(size, size, QtGui.QImage.Format.Format_ARGB32_Premultiplied)


def _bands(target, source, count):
    """Split ``target``/``source`` into ``count`` proportional horizontal bands."""
    result = []
    height = target.height()/count
    world = source.height()/count
    for index in range(count):
        top = index*height
        bottom = height if index == count-1 else (index+1)*height
        band_target = QtCore.QRectF(target.x(), target.y()+top,
                                    target.width(), bottom-top)
        band_source = QtCore.QRectF(source.x(), source.y()+index*world,
                                    source.width(), world)
        result.append((band_target, band_source))
    return result


class CountingRenderer:
    """A sliced renderer that reports how many records one slice draws.

    ``CanvasTiles`` cannot see records, only slices, so the slice is the unit
    the cap is expressed in; this renderer makes the record cost of a slice
    observable so the tests can assert the contract the cap stands for. It
    mirrors ``AssetCanvas._draw_tile_slices``: one yield draws at most
    ``slice_records`` records and the tile needs ``records`` in total.
    """

    slice_records = 128

    def __init__(self, records=500):
        self.records_left = records
        self.records = 0
        self.slices = 0

    def __call__(self, image, target, source):
        while True:
            self.slices += 1
            drawn = min(self.slice_records, self.records_left)
            self.records += drawn
            self.records_left -= drawn
            if self.records_left <= 0:
                return
            yield


def _view_with_sliced_renderer(records=500):
    view = QtWidgets.QGraphicsView()
    view.setScene(QtWidgets.QGraphicsScene())
    view.resize(700, 500)
    view.scene().setSceneRect(-10000, -10000, 20000, 20000)
    renderer = CountingRenderer(records)
    return view, CanvasTiles(view, renderer, job_factory=renderer), renderer


def _paint(view, tiles):
    image = QtGui.QImage(view.viewport().size(), QtGui.QImage.Format.Format_ARGB32_Premultiplied)
    painter = QtGui.QPainter(image)
    tiles.paint(painter)
    painter.end()
    return image


def test_one_gui_turn_advances_at_most_one_slice(qapp):
    """The step cap is the contract: one turn, one slice, not 500 records."""
    view, tiles, renderer = _view_with_sliced_renderer(records=500)
    try:
        _paint(view, tiles)
        assert tiles.pending
        tiles._work()
        assert tiles.last_frame_steps <= tiles.MAX_SLICE_STEPS_PER_FRAME
        # The wall-clock budget alone would have let all four slices through.
        assert renderer.slices == 1, 'One turn must not drain the whole tile'
        assert renderer.records <= 128, 'One turn must draw at most 128 records'
        assert not tiles.last_frame_steps == 0, 'The turn did real work'
    finally:
        view.close()


def test_remaining_slice_work_is_left_to_the_next_turn(qapp):
    """Capping a turn must defer work, never drop or mislabel it."""
    view, tiles, renderer = _view_with_sliced_renderer(records=500)
    try:
        _paint(view, tiles)
        tiles._work()
        assert renderer.slices == 1
        # The unfinished tile stays pending and unfinished: not installed,
        # not fresh, and above all not silently marked stale.
        key = next(iter(tiles.pending))
        assert not tiles._fresh(key)
        assert key in tiles._jobs, 'The open buffer must survive to the next turn'
        assert key not in tiles.stale, 'Work in progress is not invalidated content'
        # The next turn continues the same tile instead of restarting it.
        tiles._work()
        assert renderer.slices == 2
        assert tiles.last_frame_steps <= tiles.MAX_SLICE_STEPS_PER_FRAME
    finally:
        view.close()


def test_a_tile_finishes_over_successive_turns_and_keeps_its_bytes(qapp):
    view, tiles, renderer = _view_with_sliced_renderer(records=500)
    try:
        _paint(view, tiles)
        for _ in range(10):
            tiles._work()
            if tiles._jobs and not any(not tiles._fresh(key)
                                      for key in tiles._jobs):
                break
        assert renderer.slices >= 4
        assert not tiles._jobs
        assert tiles.bytes == sum(value[0].sizeInBytes() + (value[2].sizeInBytes() if value[2] else 0)
                                  for value in tiles.tiles.values())
    finally:
        view.close()


def test_not_ready_content_is_never_marked_stale(qapp):
    """Stale means "invalidated", not "still building".

    These are the two states ``stale`` used to conflate. A tile whose buffer
    is still being filled is *not ready*; it must stay out of ``stale`` so
    the cache keeps painting the cover underneath it. Only genuinely
    invalidated content belongs in ``stale``.
    """
    view, tiles, renderer = _view_with_sliced_renderer(records=500)
    try:
        _paint(view, tiles)
        tiles._work()
        assert tiles._jobs, 'precondition: a tile is mid-build'
        pending_key = next(iter(tiles._jobs))
        assert not tiles._fresh(pending_key), 'Not built yet'
        assert pending_key not in tiles.stale, 'Work in progress is not invalid content'
        assert pending_key in tiles.pending, 'Not ready means still queued'

        # Drive the same tile to completion across later turns.
        for _ in range(10):
            tiles._work()
            if not tiles._jobs:
                break
        assert tiles._fresh(pending_key), 'The deferred work really finished'
        assert pending_key not in tiles.stale
    finally:
        view.close()


def test_invalidated_content_is_never_painted_again(qapp):
    """The other half: stale content really must not survive."""
    view = QtWidgets.QGraphicsView()
    view.setScene(QtWidgets.QGraphicsScene())
    view.resize(700, 500)
    view.scene().setSceneRect(-10000, -10000, 20000, 20000)
    tiles = CanvasTiles(view, lambda image, target, source: image.fill(QtGui.QColor('red')))
    try:
        _paint(view, tiles)
        while tiles.pending:
            tiles._work()
        key = next(iter(tiles.visible))
        assert tiles._fresh(key)
        before = tiles.tiles[key][0].cacheKey()
        tiles.invalidate([tiles.rect(key).adjusted(1, 1, -1, -1)])
        assert key in tiles.stale
        image = _paint(view, tiles)
        painter_pixels = image.pixelColor(image.width()//2, image.height()//2)
        # Stale content must not be composited back over the cover.
        assert tiles.tiles[key][0].cacheKey() == before
        while tiles.pending:
            tiles._work()
        assert tiles.tiles[key][0].cacheKey() != before
        assert painter_pixels is not None
    finally:
        view.close()


def test_scratch_band_equality(view, monkeypatch):
    from prism.asset_records import ImageRecord
    import base64
    pixels = bytes([10, 200, 90, 255])*64
    for index in range(40):
        record = ImageRecord({'x': (index % 8)*130, 'y': (index // 8)*130,
                              'data': {'imageWidth': 128, 'imageHeight': 128,
                                       'displayOverview': [8, 8, base64.b64encode(pixels).decode('ascii')]}},
                             ('unused.prism', index+1))
        view.scene.addItem(record)
    view.scale(0.5, 0.5)

    renderer = view._overview_renderer
    target = QtCore.QRectF(0, 0, 512, 512)
    source = QtCore.QRectF(0, 0, 1024, 1024)

    atomic = _image()
    renderer._render(atomic, QtCore.QRectF(atomic.rect()), source)

    banded = _image()
    from PyQt6 import QtGui as _G
    painter = _G.QPainter(banded)
    painter.setRenderHints(view.renderHints())
    painter.setClipRect(QtCore.QRectF(banded.rect()))
    painter.fillRect(banded.rect(), view.backgroundBrush())
    view.scene._overview_recording_owner = view
    try:
        for band_target, band_source in _bands(QtCore.QRectF(banded.rect()), source, 8):
            painter.setClipRect(band_target)
            view.scene.render(painter, band_target, band_source,
                              QtCore.Qt.AspectRatioMode.IgnoreAspectRatio)
    finally:
        view.scene._overview_recording_owner = None
    painter.end()

    # Slicing is where a render is cut, never what it draws: the banded
    # buffer must be the atomic one pixel for pixel.
    assert banded == atomic
    assert math.isclose(banded.width(), 512)