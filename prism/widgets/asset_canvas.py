"""An independent, pannable material canvas backed by the browser's model."""
import math
import time
import threading
import weakref
from array import array
from collections import OrderedDict, deque
from prism.performance_trace import measured
from prism.performance_trace import measured_hotpath

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism import asset_paint_evidence as paint_evidence


#: How many card facades stay materialised at once. The facade is a light
#: view of the committed geometry, not the geometry itself, so a library of
#: any size can be navigated while only the cards near the camera are alive.
CARD_RESIDENCY_CAPACITY = 4096


def _card_content_size(item, grid):
    """The content size of one card.

    Shared by the card facade and by the bulk layout path so that both
    derive the SAME number from the same source dimensions. The layout
    turns this size into a pack rectangle with ``math.ceil(...)``, so a
    difference of even one ULP could change the packing; keeping one
    implementation removes that risk instead of measuring it away.
    """
    if isinstance(item, dict):
        from prism.widgets.search_canvas import SearchCard
        width = grid.width()
        return QtCore.QSizeF(width, width*SearchCard.HEIGHT/SearchCard.WIDTH)
    crop = getattr(item, 'crop', QtCore.QRectF())
    size = crop.size() if not crop.isEmpty() else QtCore.QSizeF(getattr(item, '_image_size', grid))
    return size.scaled(QtCore.QSizeF(grid.width(), grid.width()),
                       QtCore.Qt.AspectRatioMode.KeepAspectRatio)


class _RowGeometry:
    """The committed layout geometry of every row, in four flat arrays.

    One Python object per row is what the canvas used to keep: a card
    facade plus a Qt ``QSizeF`` per card, for every row of the result, even
    though only the rows near the camera are ever drawn. The geometry
    itself is four numbers per row, so it is kept as four float arrays and
    the facades are materialised on demand.

    The arrays are the SOURCE OF TRUTH for a row's position: a manual move
    writes back here, so a facade that is dropped and rebuilt later comes
    back at the place the user put it.
    """

    __slots__ = ('x', 'y', 'w', 'h')

    def __init__(self):
        self.x = array('d')
        self.y = array('d')
        self.w = array('d')
        self.h = array('d')

    def resize(self, count):
        for values in (self.x, self.y, self.w, self.h):
            if len(values) < count:
                values.extend(array('d', [0.0]) * (count-len(values)))
            del values[count:]

    def set(self, row, x, y, w, h):
        self.x[row] = x
        self.y[row] = y
        self.w[row] = w
        self.h[row] = h

    def position(self, row):
        try:
            return (self.x[row], self.y[row])
        except IndexError:
            return None

    def copy(self):
        """An independent copy, so a snapshot does not alias the live arrays."""
        clone = _RowGeometry()
        clone.x = array('d', self.x)
        clone.y = array('d', self.y)
        clone.w = array('d', self.w)
        clone.h = array('d', self.h)
        return clone

    def size(self, row):
        try:
            return (self.w[row], self.h[row])
        except IndexError:
            return None

    def clear(self):
        for values in (self.x, self.y, self.w, self.h):
            del values[:]



#: 呈现 marker 的 pkg3 接口（懒加载一次；pkg3 不依赖 widgets，不会成环）。
_PKG3_MARKER_API = None

#: 呈现 marker 的事件名（规格 §8）。名字里保留规格里的原词，便于检索。
MARKER_SCOPE_INTERACTIVE = 'assets.current_scope_interactive'
MARKER_CONTENT_FIRST_PAINT = 'assets.current_content_first_paint'
MARKER_FOCUS_QUALITY_READY = 'assets.focus_quality_ready'
MARKER_FOCUS_QUALITY_PENDING = 'assets.focus_quality_pending'
MARKER_FOCUS_QUALITY_PAINTED = 'assets.focus_quality_painted'
MARKER_FOCUS_QUALITY_UNKNOWN = 'assets.focus_quality_unknown'
MARKER_VIEWPORT_QUALITY_READY = 'assets.viewport_quality_ready'
MARKER_VIEWPORT_QUALITY_PENDING = 'assets.viewport_quality_pending'
MARKER_VIEWPORT_QUALITY_PAINTED = 'assets.viewport_quality_painted'
MARKER_VIEWPORT_QUALITY_UNKNOWN = 'assets.viewport_quality_unknown'


def _pkg3_marker_api():
    """取 pkg3 的 marker 接口。拿不到就返回 ``None``：没有 marker 不是错误。"""
    global _PKG3_MARKER_API
    if _PKG3_MARKER_API is None:
        try:
            from prism.project_catalog_index import (FRAME_EVENT, PaintRollup,
                                                     page_scope_fields, record_pkg3)
        except Exception:  # pragma: no cover - pkg3 是可选的
            _PKG3_MARKER_API = False
        else:
            _PKG3_MARKER_API = (PaintRollup, page_scope_fields, record_pkg3,
                                FRAME_EVENT)
    return _PKG3_MARKER_API or None


class _LayoutResult(QtCore.QObject):
    ready = QtCore.pyqtSignal(object)


class _RowIndex(dict):
    """Row map for material rows.

    The primary key is a stable item identity, never a recycled ``id()``:
    a deleted material and a freshly created one must never share a row.
    ``catalog_browser`` still looks rows up by ``id(item)``; that legacy
    spelling keeps working through an index over the same rows, built only
    when it is actually used.
    """

    def __init__(self, items=None):
        super().__init__()
        self._items = items if items is not None else []
        self._by_id = None

    def _legacy_row(self, key):
        if not isinstance(key, int) or isinstance(key, bool):
            return None
        index = self._by_id
        if index is None:
            index = self._by_id = {id(item): row for row, item in enumerate(self._items)}
        return index.get(key)

    def __missing__(self, key):
        row = self._legacy_row(key)
        if row is None:
            raise KeyError(key)
        return row

    def get(self, key, default=None):
        row = dict.get(self, key)
        if row is None:
            row = self._legacy_row(key)
        return default if row is None else row


class _PrepareLayout(QtCore.QRunnable):
    def __init__(self, state, signals):
        super().__init__()
        self.state, self.signals = state, signals

    def run(self):
        state = self.state
        try:
            from prism.asset_layout_preparation import prepare_asset_layout_result
            from prism.canvas_composites import CanvasComposites
            # The worker already computes the group geometry from pure
            # scalars. Returning it means the GUI thread does not have to
            # walk the rows a second time to rebuild the same groups.
            result = prepare_asset_layout_result(
                state['sizes'],
                state['canvas_codes'] if state['grouped'] else None,
                content_sizes=state['content'],
                flat_sizes=True, flat_content=True,
                visual=state['visual'], viewport_width=state['width'],
                cancelled=state['cancel'].is_set,
                min_group_items=CanvasComposites.MIN_ITEMS)
            error = None
        except Exception as failure:
            result, error = None, str(failure)
        if not state['cancel'].is_set():
            try:
                self.signals.ready.emit((state, result, error))
            except RuntimeError:
                pass


class MaterialCard:
    """Lightweight layout record. A material does not need a Qt scene object.

    The facade holds no geometry of its own: the position comes from the
    canvas' committed geometry arrays and the content size is derived from
    the row's item. That is what makes a facade cheap to drop and rebuild,
    and it is why a library of any size can keep only the rows near the
    camera materialised.
    """
    __slots__ = ('host', 'row', '_x', '_y', '_size')

    def __init__(self, host, row):
        self.host, self.row = host, row
        self._x = self._y = 0.0
        item = host.model().items[row]
        committed = host._card_size(row)
        self._size = (QtCore.QSizeF(*committed)
                      if committed is not None and min(committed) > 0
                      else _card_content_size(item, host.gridSize()))
        position = host._card_position(row)
        if position is not None:
            self._x, self._y = position

    def pos(self):
        return QtCore.QPointF(self._x, self._y)

    def x(self):
        return self._x

    def y(self):
        return self._y

    def setPos(self, x, y=None):
        point = QtCore.QPointF(x) if y is None else QtCore.QPointF(x, y)
        self.host.move_card(self, point)

    def isSelected(self):
        return self.row in self.host._selected_rows

    def setSelected(self, selected):
        flag = QtCore.QItemSelectionModel.SelectionFlag.Select if selected else QtCore.QItemSelectionModel.SelectionFlag.Deselect
        self.host.selectionModel().select(self.host.model().index(self.row), flag)

    def boundingRect(self):
        return QtCore.QRectF(QtCore.QPointF(), self._size)

    def sceneBoundingRect(self):
        return self.boundingRect().translated(self._x, self._y)


class _CardRows:
    """The canvas card list.

    ``len()`` is the number of rows as soon as the model is installed, but
    the MaterialCard records are only materialised when a row is touched.
    A 100k material library therefore learns its row count without freezing
    the GUI thread.

    Materialisation is BOUNDED: the records are an LRU cache over the rows
    near the camera, not a per-row object kept forever. Dropping a facade
    loses nothing, because the committed geometry lives in the canvas'
    flat arrays and a manual move is written back to them.
    """

    __slots__ = ('_host', '_count', 'records', '_capacity', '_order')

    def __init__(self, host, count, capacity=CARD_RESIDENCY_CAPACITY):
        self._host = host
        self._count = int(count)
        self.records = [None] * self._count
        self._capacity = max(0, int(capacity))
        self._order = OrderedDict()

    def __len__(self):
        return self._count

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self[position] for position in range(*index.indices(self._count))]
        position = index + self._count if index < 0 else index
        if not 0 <= position < self._count:
            raise IndexError(index)
        return self.materialize(position)

    def materialize(self, row):
        """Create the card record of one row once, then return it."""
        record = self.records[row]
        if record is None:
            record = self.records[row] = MaterialCard(self._host, row)
        if self._capacity:
            self._order.pop(row, None)
            self._order[row] = None
            self._evict()
        return record

    def _evict(self):
        """Drop the least recently used facades above the capacity.

        A facade that is currently referenced elsewhere (a composite being
        built, a drag in progress) is kept: dropping it would only move the
        cost, and the composite asks for it again anyway.
        """
        while len(self._order) > self._capacity:
            victim, _ = self._order.popitem(last=False)
            if self.records[victim] is None:
                continue
            self.records[victim] = None

    def resident_count(self):
        """How many facades are currently materialised."""
        return sum(1 for record in self.records if record is not None)

    def __iter__(self):
        for position in range(self._count):
            yield self.materialize(position)


class MaterialsLayer(QtWidgets.QGraphicsObject):
    def __init__(self, host):
        super().__init__()
        self.host = host
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.NoButton)
        self.setFlag(self.GraphicsItemFlag.ItemUsesExtendedStyleOption)

    def boundingRect(self):
        return self.host._content_bounds

    def paint(self, painter, option, widget=None):
        host = self.host
        viewport = getattr(host, '_tile_recording_rect', None) or host.mapToScene(host.viewport().rect()).boundingRect()
        viewport = viewport.intersected(option.exposedRect)
        cards = host._composites.paint(painter, viewport)
        if cards is None:
            cards = host.visible_cards(viewport)
        else:
            # The composite layer hands back the ROWS it could not draw as a
            # group image. Their facades are built here, for the rows that are
            # actually painted, instead of being kept alive for the library.
            cards = [host.cards[row] for row in cards]
        host._last_painted_count = len(cards)
        style = QtWidgets.QStyleOptionViewItem()
        style.rect = QtCore.QRect(QtCore.QPoint(), host.gridSize())
        style.font = host.font()
        for card in cards:
            painter.save()
            painter.translate(card._x, card._y)
            style.rect = card.boundingRect().toAlignedRect()
            style.state = QtWidgets.QStyle.StateFlag.State_Enabled
            if card.isSelected() and getattr(host, '_tile_recording_rect', None) is None:
                style.state |= QtWidgets.QStyle.StateFlag.State_Selected
            with paint_evidence.delegate_scope(host, card, painter):
                host.delegate.paint(painter, style, host.model().index(card.row))
            painter.restore()


class AssetCanvas(QtWidgets.QGraphicsView):
    doubleClicked = QtCore.pyqtSignal(QtCore.QModelIndex)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setBackgroundBrush(QtGui.QColor('#1B1B1D'))
        self.setDragMode(self.DragMode.RubberBandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
        self._model = None
        self._build_state = None
        self._reset_frame = None
        self._layout_signals = _LayoutResult(self)
        self._layout_signals.ready.connect(self._layout_prepared)
        self._item_rows = _RowIndex()
        self._identity_seq = 0
        self._identity_registry = {}
        self._grid = QtCore.QSize(156, 154)
        self.cards = _CardRows(self, 0)
        # The committed layout geometry of every row: four flat float arrays
        # instead of one Python object per row. Card facades are built from
        # it on demand, so the resident set follows the camera, not the size
        # of the library.
        self._geometry = _RowGeometry()
        self._selected_rows = set()
        self._selection_bounds = None
        self._spatial = {}
        self._moved_rows = set()
        self._default_columns = 1
        self._default_step = self._grid.width()+12
        self._content_bounds = QtCore.QRectF()
        self._layout_in_progress = False
        # 布局复用（5A）：实例级键控缓存。同范围重复进入复用位置；手工摆放、
        # 项目切换会清空，缓存只回放纯计算位置，不覆盖用户手工布局。
        from prism.compact_layout import StableLayoutCache
        self._layout_cache = StableLayoutCache(capacity=8, max_elements=131072)
        self._last_project_id = None
        self._applied_layout_key = None
        self._render_layout_revision = 0
        self._geometry_version = 0
        self._layout_content_revisions = weakref.WeakKeyDictionary()
        self._layer = MaterialsLayer(self)
        self.scene().addItem(self._layer)
        self._drag_cards = None
        self._rubber_start = None
        self._rubberband = QtWidgets.QRubberBand(QtWidgets.QRubberBand.Shape.Rectangle, self.viewport())
        self.undo_stack = QtGui.QUndoStack(self)
        self.undo_stack.canUndoChanged.connect(self._history_changed)
        self.undo_stack.canRedoChanged.connect(self._history_changed)
        self._syncing = False
        self._pan = None
        self.tool = 'select'
        self.colour_group = None
        self.display_grayscale = False
        self.preview_cache = {}
        self._stroke = None
        self._stroke_start = None
        from prism.canvas_tiles import CanvasTiles
        self._tiles = CanvasTiles(self, self._render_tile)
        from prism.canvas_composites import CanvasComposites
        self._composites = CanvasComposites(self)
        self._display_frame = None
        self._display_frame_key = None
        self._display_dirty = {}
        # 呈现 marker 的状态（规格 §8）。一帧只是加计数器，不是两千条日志。
        self._paint_rollup = None
        self._paint_markers = {}
        self._detail_quality = None
        self._paint_quality = None
        self._display_generation = 0
        self._display_timer = QtCore.QTimer(self)
        self._display_timer.setObjectName('assets.publish_display')
        self._display_timer.setSingleShot(True)
        self._display_timer.setInterval(100)
        self._display_timer.timeout.connect(self._publish_display)
        self.scene().changed.connect(self._invalidate_display_frame)

    def _invalidate_display_frame(self, *_args):
        self._display_dirty.clear()
        self._details_key = None
        self._composites.invalidate()
        regions = _args[0] if _args and isinstance(_args[0], list) else None
        self._tiles.invalidate(regions)
        self._display_frame = None
        self.viewport().update()

    def request_display_refresh(self, item=None):
        if item is not None:
            row = self._item_rows.get(self._row_identity(item))
            self._composites.invalidate(row, preserve=True)
            if row is not None:
                self._display_dirty[row] = self._card_rect(row)
        else:
            # Explicit refresh keeps its whole-view semantics. Decoder queue
            # wakeups use wake_display_queue and do not invalidate pixels.
            self._display_dirty[-1] = self.mapToScene(self.viewport().rect()).boundingRect()
        self.wake_display_queue()

    def wake_display_queue(self):
        if self._display_frame is None and not self._tiles.tiles:
            self.viewport().update()
        elif not self._display_timer.isActive():
            self._display_timer.start()

    def _invalidate_overview_span(self, *_):
        self._overview_pixel_span = float('inf')
        self._overview_scan_cursor = 0
        self._overview_scan_max = 0.0

    def _scan_overview_span(self):
        started = time.perf_counter()
        cursor = getattr(self, '_overview_scan_cursor', 0)
        maximum = getattr(self, '_overview_scan_max', 0.0)
        items = self.model().items
        while cursor < len(self.cards):
            # The committed geometry carries the card size, so the scan does
            # not have to materialise a facade for every row of the library.
            size = self._card_size(cursor)
            if size is None:
                card = self.cards[cursor]
                size = (card._size.width(), card._size.height())
            item = items[cursor]
            source = getattr(item, '_image_size', None)
            crop = getattr(item, 'crop', QtCore.QRectF())
            if source is None:
                source_span = max(size)
            else:
                source_span = max(source.width(), source.height())
            cropped = max(crop.width(), crop.height(), 1) if not crop.isEmpty() else max(source_span, 1)
            maximum = max(maximum, max(size)*source_span/cropped)
            cursor += 1
            if time.perf_counter()-started >= .004:
                break
        self._overview_scan_cursor, self._overview_scan_max = cursor, maximum
        if cursor == len(self.cards):
            self._overview_pixel_span = maximum
            return True
        return False

    def _spatial_row_candidates(self, rect):
        """Yield existing row references, with checkpoints for empty cells."""
        if not self.cards or rect.isEmpty():
            return
        area = rect.width()*rect.height()/((self._grid.width()+12)*(self._grid.height()+12))
        if self._spatial and area > len(self._spatial)*2:
            yield from self._moved_rows
        elif self._spatial:
            for cell in self._cells(rect):
                yield from self._spatial.get(cell, ())
                yield None

    def _detail_rows(self, rect, focus):
        """Bounded focal probing, then all remaining intersecting row IDs.

        Checkpoints let the caller yield even in a dense cell with no hits.
        No card facade or per-row Qt rectangle is retained by this iterator.
        """
        if len(self.cards) <= 256:
            rows = self.visible_rows(rect)
            rows.sort(key=lambda row: (self._card_rect(row).center()-focus).manhattanLength())
            yield from rows
            return
        seen = bytearray(len(self.cards))
        near = QtCore.QRectF(focus.x()-self._grid.width()*2,
                            focus.y()-self._grid.height()*2,
                            self._grid.width()*4, self._grid.height()*4).intersected(rect)
        seeds = []
        for probe, row in enumerate(AssetCanvas._spatial_row_candidates(self, near)):
            if row is not None and not seen[row]:
                bounds = self._card_rect(row)
                if bounds.intersects(near):
                    seen[row] = 1
                    seeds.append(((bounds.center()-focus).manhattanLength(), row))
            yield None
            if len(seeds) >= 64 or probe >= 127:
                break
        for _distance, row in sorted(seeds):
            yield row
        for row in AssetCanvas._spatial_row_candidates(self, rect):
            if row is None or seen[row]:
                yield None
                continue
            seen[row] = 1
            yield row if self._card_rect(row).intersects(rect) else None

    def _few_visible_rows(self, rect):
        """Select the direct renderer only when fewer than 128 is proven.

        Ambiguous/high-fanout cells use the exact tiled renderer; this is a
        backend choice, never a cap on what images are drawn.
        """
        key = (id(self.cards), self._geometry_version, self._render_layout_revision,
               rect.x(), rect.y(), rect.width(), rect.height())
        cached = getattr(self, '_few_rows_cache', None)
        if cached is not None and cached[0] == key:
            return cached[1]
        seen, count = set(), 0
        result = True
        for checked, row in enumerate(self._spatial_row_candidates(rect)):
            if row is not None and row not in seen:
                seen.add(row)
                if self._card_rect(row).intersects(rect):
                    count += 1
                    if count >= 128:
                        result = False
                        break
            if checked >= 511:
                result = False
                break
        self._few_rows_cache = key, result
        return result

    @measured_hotpath('assets.queue_visible_details', 16)
    def queue_visible_details(self):
        from prism import lod
        from prism.item_lifecycle import is_pixmap_item
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        scale = abs(self.transform().m11())*self.viewport().devicePixelRatioF()
        self._detail_quality = None
        index = self.currentIndex()
        focus_row = index.row() if index.isValid() else None
        if (self.gridSize().width()*scale <= lod.OVERVIEW_MAX_PIXELS
                and math.isinf(getattr(self, '_overview_pixel_span', float('inf')))):
            if not self._scan_overview_span():
                self._display_timer.start(0)
                return False
        if scale*getattr(self, '_overview_pixel_span', float('inf')) <= lod.OVERVIEW_MAX_PIXELS:
            return True
        focus = self.mapToScene(getattr(self, '_detail_focus', self.viewport().rect().center()))
        key = (id(self.cards), self._render_layout_revision, rect.x(), rect.y(),
               rect.width(), rect.height(), scale, focus.x(), focus.y())
        if getattr(self, '_details_key', None) != key:
            self._details_key = key
            self._details_state = dict(source=iter(AssetCanvas._detail_rows(
                                       weakref.proxy(self), rect, focus)),
                                       pending=deque(), rows=array('I'), scan_done=False,
                                       failed=deque(), retry_failed_after=0.0, blocked=False)
        state = self._details_state
        state['blocked'] = False
        checked = detailed_count = 0
        focus_ready = None
        started = time.perf_counter()
        # Retry each pending row at most once in this turn. New discovery gets
        # the remaining capacity, with at most 64 unresolved rows in flight.
        retries = len(state['pending'])
        failed_retries = 0
        if state['failed'] and started >= state['retry_failed_after']:
            failed_retries = min(8, len(state['failed']))
            state['retry_failed_after'] = started+.5
        for _step in range(256):
            if failed_retries:
                row = state['failed'].popleft()
                failed_retries -= 1
            elif retries:
                row = state['pending'].popleft()
                retries -= 1
            elif not state['scan_done'] and len(state['pending']) < 64:
                try:
                    row = next(state['source'])
                except StopIteration:
                    state['scan_done'] = True
                    break
                if row is None:
                    if time.perf_counter()-started >= .004:
                        break
                    continue
                state['rows'].append(row)
            else:
                break
            item = self.model().items[row]
            failed = bool(getattr(item, '_catalog_display_failed', False)
                          or getattr(item, '_display_decode_failed', False))
            detailed = True
            if getattr(item, '_catalog_proxy', False) and not getattr(item, '_catalog_display_ready', False):
                if not failed:
                    item.request_catalog_display(self)
                detailed = False
            elif is_pixmap_item(item):
                width, height = self._card_size(row) or (0, 0)
                size = item._image_size
                crop = item.crop
                span = max(crop.width(), crop.height()) if not crop.isEmpty() else max(size.width(), size.height())
                pixels = max(width, height)*scale*max(size.width(), size.height())/max(span, 1)
                if pixels > lod.OVERVIEW_MAX_PIXELS:
                    preview = lod.display_pixmap(item, pixels, self)
                    needed = min(lod.level_for_pixels(pixels), max(size.width(), size.height()))
                    detailed = (preview is not None and not preview.isNull()
                                and max(preview.width(), preview.height()) >= needed)
            checked += 1
            detailed_count += int(detailed)
            if row == focus_row:
                focus_ready = bool(detailed)
            if not detailed:
                if failed:
                    state['failed'].append(row)
                else:
                    state['pending'].append(row)
                state['blocked'] = True
                decoder = getattr(QtWidgets.QApplication.instance(), '_prism_display_decoder', None)
                if decoder is not None and len(decoder.requests) >= decoder.max_pending:
                    break
            if time.perf_counter()-started >= .004:
                break
        ready = state['scan_done'] and not state['pending'] and not state['failed']
        self._detail_quality = dict(visible=checked, detailed=detailed_count,
                                   covered=checked,
                                   total=len(state['rows']) if state['scan_done'] else None,
                                   enumeration_complete=state['scan_done'],
                                   focus_ready=focus_ready)
        if not ready and not self._display_timer.isActive():
            self._display_timer.start(100 if state['blocked'] or state['pending'] else 0)
        return ready

    def _publish_display(self):
        ready = self.queue_visible_details()
        # Publish actual per-image completions even while other visible rows
        # are loading or failed. Keep explicit whole-view refreshes behind the
        # existing readiness barrier, retaining their no-flicker semantics.
        completed = [key for key in self._display_dirty if ready or key != -1]
        regions = [self._display_dirty.pop(key) for key in completed]
        if regions:
            self._tiles.invalidate(regions)
            self.viewport().update()
        if not ready:
            from prism.lod import OVERVIEW_MAX_PIXELS
            scanning = (math.isinf(getattr(self, '_overview_pixel_span', float('inf')))
                        and self.gridSize().width()*abs(self.transform().m11())*self.viewport().devicePixelRatioF() <= OVERVIEW_MAX_PIXELS)
            state = getattr(self, '_details_state', None)
            progressing = (state is not None and not state['scan_done']
                           and not state['pending'] and not state['blocked'])
            self._display_timer.start(0 if scanning or progressing else 100)

    def _scope_marker_context(self):
        """当前呈现范围的身份（规格 §8）：project + generation + scope。

        ``None`` 与空集合严格区分；拿不到就是 ``unknown``，不猜。
        """
        api = _pkg3_marker_api()
        browser = self.parent()
        scope_pages = getattr(browser, 'scope_pages', None)
        mode = getattr(browser, 'mode', None)
        raw = getattr(self, '_last_project_id', None)
        project_id = None
        if isinstance(raw, (tuple, list)) and len(raw) >= 2:
            project_id = raw[1]
        elif raw is not None:
            project_id = raw
        if project_id is not None:
            project_id = str(project_id)
        if api:
            _PaintRollup, page_scope_fields, _record_pkg3, _frame_event = api
            scope_fields = page_scope_fields(scope_pages)
        else:
            scope_fields = {'scope_pages_known': scope_pages is not None,
                            'scope_pages_count': None,
                            'scope_pages_fingerprint': None}
        barrier = 'unknown'
        host = getattr(browser, 'window_host', None)
        if host is not None and hasattr(host, '_navigation_is_committed'):
            try:
                barrier = 'committed' if host._navigation_is_committed() else 'pending'
            except Exception:
                barrier = 'unknown'
        return dict(project_id=project_id, generation=self._display_generation,
                    scope_mode=mode, commit_barrier=barrier,
                    scope_opening=getattr(browser, '_opening', None),
                    **scope_fields)

    def _emit_quality_marker(self, family, state, painted, quality, context):
        """质量 marker：只在状态翻转时发一条。

        判据不足就是 ``unknown`` + 原因 —— 队列空不等于清晰，缓存装入也不等于
        已经画出来。
        """
        api = _pkg3_marker_api()
        if api is None:
            return
        _PaintRollup, _page_scope_fields, record_pkg3, _frame_event = api
        signature = (family, state, bool(painted))
        if self._paint_markers.get('quality:'+family) == signature:
            return
        self._paint_markers['quality:'+family] = signature
        if family == 'focus':
            ready_event = MARKER_FOCUS_QUALITY_READY
            pending_event = MARKER_FOCUS_QUALITY_PENDING
            painted_event = MARKER_FOCUS_QUALITY_PAINTED
            unknown_event = MARKER_FOCUS_QUALITY_UNKNOWN
        else:
            ready_event = MARKER_VIEWPORT_QUALITY_READY
            pending_event = MARKER_VIEWPORT_QUALITY_PENDING
            painted_event = MARKER_VIEWPORT_QUALITY_PAINTED
            unknown_event = MARKER_VIEWPORT_QUALITY_UNKNOWN
        extra = {}
        if quality:
            extra = {'quality_covered': quality.get('covered'),
                     'quality_total': quality.get('total'),
                     'quality_detailed': quality.get('detailed'),
                     'quality_basis': quality.get('basis'),
                     'capture_id': quality.get('capture_id')}
        if state == 'ready':
            record_pkg3(ready_event, status='ready', painted=bool(painted),
                        **extra, **context)
            if painted:
                record_pkg3(painted_event, status='painted', **extra, **context)
        elif state == 'pending':
            record_pkg3(pending_event, status='pending', painted=bool(painted),
                        reason='required_level_not_installed', **extra, **context)
        else:
            record_pkg3(unknown_event, status='unknown', painted=bool(painted),
                        reason='no_reliable_level_judgement', **extra, **context)

    def _record_paint_frame(self, *, content, cards, quality=None):
        """一帧呈现的计数 + 呈现 marker（规格 §8）。

        **不逐图写日志。** 一帧只是把一个整数加进计数器；真正发出的事件只有
        汇总行（默认每秒一次）和几个阈值 marker（首绘、交互就绪、质量翻转）。
        """
        api = _pkg3_marker_api()
        if api is None:
            return
        PaintRollup, _page_scope_fields, record_pkg3, frame_event = api
        if self._paint_rollup is None:
            self._paint_rollup = PaintRollup(interval=1.0)
        context = self._scope_marker_context()
        # Only the just-completed renderer census is accepted, not the detail
        # scheduler's readiness estimate or a previous frame's cache state.
        quality_state = 'unknown'
        if quality and quality.get('total', 0) > 0:
            quality_state = ('ready' if quality['detailed'] == quality['total']
                             and quality['covered'] == quality['total'] else 'pending')
        self._paint_rollup.count_frame(content=content, cards=cards,
                                       quality=quality_state)
        if self._paint_rollup.due():
            payload = self._paint_rollup.take()
            if payload.get('frames'):
                record_pkg3(frame_event, **dict(payload, **context,
                            cards_units='model_rows', content_basis='observed_content_draw',
                            viewport_drawn_known=False))
        key = (context.get('project_id'), context.get('generation'),
               context.get('scope_pages_fingerprint'))
        if content:
            if not self._paint_markers.get('first_paint'):
                self._paint_markers['first_paint'] = key
                record_pkg3(MARKER_CONTENT_FIRST_PAINT, status='painted',
                            painted=True, **context)
            interactive = (self._build_state is None
                           and not self._layout_in_progress
                           and self._model is not None)
            if interactive and not self._paint_markers.get('interactive'):
                self._paint_markers['interactive'] = key
                record_pkg3(MARKER_SCOPE_INTERACTIVE, status='interactive',
                            **context)
        focus_state = 'unknown'
        if quality and quality.get('visible'):
            if quality.get('focus_ready') is None:
                focus_state = 'unknown'
            elif quality.get('focus_ready'):
                focus_state = 'ready'
            else:
                focus_state = 'pending'
        # A partial update may prove retained viewport coverage, but is not
        # itself a fresh paint of the whole scope or selected focus image.
        quality_painted = bool(content and quality and quality.get('full_viewport_repaint'))
        self._emit_quality_marker('focus', focus_state, quality_painted, quality, context)
        self._emit_quality_marker('viewport', quality_state,
                                   quality_painted if quality_state == 'ready' else content, quality,
                                   context)

    def _observe_asset_pixmap(self, painter, item, target, pm, source, pixels, revision):
        paint_evidence.observe_pixmap(self, painter, item, target, pm, source, pixels, revision)

    def _observe_asset_missing(self, painter, target):
        paint_evidence.observe_missing(self, painter, target)

    def paintEvent(self, event):
        frame = paint_evidence.begin_frame(self, event)
        try:
            self._paint_asset_event(event)
        finally:
            self._paint_quality = paint_evidence.finish_frame(self, frame)
            try:
                self._record_paint_frame(content=bool(frame and frame.drawn),
                                         cards=len(self.cards), quality=self._paint_quality)
            except Exception:
                pass  # Evidence failures never change drawing.

    def _paint_asset_event(self, event):
        if self._build_state is not None and not self._build_state.get('keep_layout'):
            painter = QtGui.QPainter(self.viewport())
            painter.fillRect(self.viewport().rect(), self.backgroundBrush())
            if self._reset_frame is not None:
                painter.drawPixmap(0, 0, self._reset_frame)
            painter.setPen(QtGui.QColor('#eeeeee'))
            message = self._build_state.get('error') or f"Preparing layout: {self._build_state.get('cursor', 0):,}/{len(self._build_state['items']):,}"
            painter.drawText(self.viewport().rect(), QtCore.Qt.AlignmentFlag.AlignBottom | QtCore.Qt.AlignmentFlag.AlignHCenter, message)
            painter.end()
            # Layout text and the reset cover are not current content.
            sink = getattr(self, '_asset_paint_sink', None)
            if sink is not None:
                sink.placeholder += 1
                if self._reset_frame is not None:
                    sink.fallback += 1
                    sink.unknown += 1
                    sink.categories['reset_cover'] = 1
            return
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        if self.gridSize().width()*abs(self.transform().m11()) > 8:
            # Submit the current focal region first, before drawing delegates
            # in stacking order fills the bounded decode queue with edge images.
            self.queue_visible_details()
        far_groups = (self._composites.groups and
                      self.gridSize().width()*abs(self.transform().m11()) < self._composites.DETAIL_PIXELS)
        if len(self.cards) < 2000 or (not far_groups and self._few_visible_rows(rect)):
            self._display_frame = None
            # The camera has left tiled overview mode. Do not finish old
            # far-camera tiles while the user is waiting for close detail.
            self._tiles.timer.stop()
            self._tiles.pending.clear()
            self._tiles._cancel_jobs()
            super().paintEvent(event)
            return
        painter = QtGui.QPainter(self.viewport())
        self._tiles.paint(painter)
        painter.setWorldTransform(self.viewportTransform())
        self.drawForeground(painter, rect)
        painter.end()

    def prepare_display_tiles(self):
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        self._composites.paint(None, rect)

    def can_build_display_tile(self, key):
        # Existing parent tiles already cover the viewport. Prepare group
        # previews first instead of doing another huge per-image tile render
        # in the middle of a wheel gesture. Near views still render normally.
        viewport = self.mapToScene(self.viewport().rect()).boundingRect()
        if not self._tiles.rect(key).intersects(viewport):
            return False
        if self.gridSize().width()*abs(self.transform().m11()) <= 8:
            # A far tile can span tens of thousands of pictures. Partial
            # group readiness must not trigger another raw render of it.
            return not self._composites.pending
        return (self._composites.ready_for(self._tiles.rect(key)) or
                self.gridSize().width()*abs(self.transform().m11()) >= self._composites.DETAIL_PIXELS)

    def _render_tile(self, image, target, rect):
        painter = QtGui.QPainter(image)
        painter.setRenderHints(self.renderHints())
        self._tile_recording_rect = rect
        try:
            with paint_evidence.resource_scope(self, image):
                self.scene().render(painter, target, rect, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            self._tile_recording_rect = None
            painter.end()
        paint_evidence.generated(self, image, complete=True)

    def create_display_tile_job(self, image, target, rect):
        """Yield between bounded Qt draw slices for the image-only surface.

        Arbitrary scene annotations must retain Qt scene stacking, clipping
        and opacity semantics; leave that path on the existing scene renderer.
        """
        if (not self._layer.isVisible() or self._layer.opacity() != 1
                or not self._layer.transform().isIdentity()
                or any(item is not self._layer for item in self.scene().items())):
            return None
        return self._draw_tile_slices(image, target, rect)

    def _tile_draw_operations(self, rect):
        composites = self._composites
        if composites.groups and self.gridSize().width()*abs(self.transform().m11()) < composites.DETAIL_PIXELS:
            scale = abs(self.transform().m11())*self.viewport().devicePixelRatioF()
            for group, (bounds, rows) in enumerate(composites.groups):
                if not bounds.intersects(rect):
                    continue
                wanted = max(32, 2**math.ceil(math.log2(max(1, max(bounds.width(), bounds.height())*scale))))
                available = composites.by_group.get(group)
                if wanted <= composites.MAX_EDGE and available:
                    key = group, wanted, composites.epoch
                    if key not in composites.cache:
                        key = max(available, key=lambda entry: entry[1])
                    yield bounds, composites.cache[key]
                else:
                    # A group holds ROW references, not card facades: the
                    # facade of a row that is actually painted is built here
                    # and dropped again when the canvas evicts it.
                    for row in rows:
                        if self._card_rect(row).intersects(rect):
                            yield self.cards[row], None
                        else:
                            yield None, None
        else:
            seen = bytearray(len(self.cards))
            for row in self._spatial_row_candidates(rect):
                if row is None or seen[row]:
                    yield None, None
                    continue
                seen[row] = 1
                if self._card_rect(row).intersects(rect):
                    yield self.cards[row], None
                else:
                    yield None, None

    def _draw_tile_slices(self, image, target, rect):
        operations = iter(self._tile_draw_operations(rect))
        finished = False
        while not finished:
            started = time.perf_counter()
            painter = QtGui.QPainter(image)
            painter.setRenderHints(self.renderHints())
            painter.setClipRect(target)
            painter.translate(target.x(), target.y())
            painter.scale(target.width()/rect.width(), target.height()/rect.height())
            painter.translate(-rect.x(), -rect.y())
            option = QtWidgets.QStyleOptionViewItem()
            option.font = self.font()
            option.state = QtWidgets.QStyle.StateFlag.State_Enabled
            self._tile_recording_rect = rect
            try:
                with paint_evidence.resource_scope(self, image):
                    for _ in range(128):
                        try:
                            card_or_bounds, composite = next(operations)
                        except StopIteration:
                            finished = True
                            break
                        if card_or_bounds is None:
                            if time.perf_counter()-started >= .004:
                                break
                            continue
                        if composite is not None:
                            painter.drawImage(card_or_bounds, composite)
                            paint_evidence.observe_image(self, painter, card_or_bounds, composite,
                                category='group', stale=any(self._composites.cache.get(k) is composite
                                    for k in self._composites.stale))
                        else:
                            card = card_or_bounds
                            painter.save()
                            painter.translate(card._x, card._y)
                            option.rect = card.boundingRect().toAlignedRect()
                            with paint_evidence.delegate_scope(self, card, painter):
                                self.delegate.paint(painter, option, self.model().index(card.row))
                            painter.restore()
                        if time.perf_counter()-started >= .004:
                            break
            finally:
                self._tile_recording_rect = None
                painter.end()
            paint_evidence.generated(self, image, complete=finished)
            if not finished:
                yield

    def setModel(self, model):
        self._model = model
        # 换模型就是换呈现范围：代数 +1，首绘/交互 marker 重新计一次。
        self._display_generation += 1
        self._paint_markers = {}
        self._selection = QtCore.QItemSelectionModel(model, self)
        self._selection.selectionChanged.connect(self._model_selection)
        model.modelReset.connect(self.rebuild)
        model.modelAboutToBeReset.connect(self._capture_reset_frame)
        model.dataChanged.connect(self._model_data_changed)

    def _model_data_changed(self, first, last, _roles=None):
        if getattr(self.parent(), 'list', self) is not self:
            return
        start, stop = max(0, first.row()), min(len(self.cards), last.row()+1)
        if stop-start <= 256:
            self.catalog_rows_changed(range(start, stop))
            return
        state = getattr(self, '_metadata_refresh_state', None)
        if state is None or state['items'] is not self.model().items:
            state = dict(items=self.model().items, ranges=[], updated=0, slices=0,
                         layout_token=getattr(self.model(), 'layout_token', None),
                         work_ms=0.0, max_slice_ms=0.0, started=time.perf_counter(),
                         geometry_changed=False)
            self._metadata_refresh_state = state
            QtCore.QTimer.singleShot(0, lambda: self._refresh_metadata_slice(state))
        # Merge only ranges already requested by dataChanged. Unlike a min/max
        # envelope this does not scan unrelated gaps between sparse updates.
        ranges = sorted(state['ranges'] + [(start, stop)])
        merged = []
        for low, high in ranges:
            if merged and low <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((low, high))
        state['ranges'] = merged

    def _refresh_metadata_slice(self, state):
        if sip.isdeleted(self) or state is not getattr(self, '_metadata_refresh_state', None):
            return
        if self.model().items is not state['items']:
            if (state['layout_token'] is not None and
                    state['layout_token'] == getattr(self.model(), 'layout_token', None)):
                # A validated hot publication can copy the reference array
                # without changing its content. Finish its outstanding refresh.
                state['items'] = self.model().items
            else:
                self._metadata_refresh_state = None
                return
        tick = time.perf_counter()
        low, high = state['ranges'][0]
        stop = min(high, low+256)
        if stop == high:
            state['ranges'].pop(0)
        else:
            state['ranges'][0] = (stop, high)
        self.catalog_rows_changed(range(low, stop))
        elapsed = (time.perf_counter()-tick)*1000
        state['updated'] += stop-low
        state['slices'] += 1
        state['work_ms'] += elapsed
        state['max_slice_ms'] = max(state['max_slice_ms'], elapsed)
        if state['ranges']:
            QtCore.QTimer.singleShot(0, lambda: self._refresh_metadata_slice(state))
            return
        self._metadata_refresh_state = None
        if state['geometry_changed'] and not state.get('preserve_manual_layout'):
            self._metadata_layout_timer.start(0)
        from prism.performance_trace import record
        record('assets.metadata_refreshed', rows_updated=state['updated'],
               slices=state['slices'], work_ms=state['work_ms'],
               max_slice_ms=state['max_slice_ms'],
               elapsed_ms=(time.perf_counter()-state['started'])*1000)

    def catalog_rows_changed(self, rows):
        """Refresh exact metadata rows without scanning a sparse range."""
        geometry_changed = False
        changed_rows = array('I')
        identities = self._applied_layout_key[1] if self._applied_layout_key else ()
        for row in dict.fromkeys(rows):
            if row < 0 or row >= len(self.cards):
                continue
            item = self.model().items[row]
            size = _card_content_size(item, self.gridSize())
            previous_size = self._card_size(row)
            changed_rows.append(row)
            if previous_size is None or size != QtCore.QSizeF(*previous_size):
                old_rect = self._card_rect(row)
                self._layer.prepareGeometryChange()
                for cell in self._cells(old_rect):
                    bucket = self._spatial.get(cell)
                    if bucket is not None:
                        bucket.discard(row)
                        if not bucket:
                            del self._spatial[cell]
                self._commit_card_geometry(row, old_rect.x(), old_rect.y(),
                                           size.width(), size.height())
                new_rect = self._card_rect(row)
                for cell in self._cells(new_rect):
                    self._spatial.setdefault(cell, set()).add(row)
                self._moved_rows.add(row)
                self._content_bounds = self._content_bounds.united(new_rect)
                self._selection_bounds = None
                # The card's painted geometry changed, so the render caches
                # keyed on the layout revision are stale right now. The full
                # re-layout stays coalesced below, but the revision must not
                # wait for it: tiles and composites would otherwise keep
                # serving the pixels of the old crop.
                self._render_layout_revision += 1
                geometry_changed = True
            identity = self._row_identity(item)
            if row < len(identities) and identities[row] != identity:
                geometry_changed = True
            if self._item_rows.get(identity) != row:
                # This row now holds a different object. Refresh the map so a
                # recycled id() can never point the row at the old material.
                self._item_rows[identity] = row
        if geometry_changed:
            self._geometry_version += 1
            self._bump_content_geometry_revision()
            pending_layout = self._build_state
            if pending_layout is not None:
                pending_layout['cancel'].set()
            # Visible metadata arrives in several small asynchronous batches.
            # Coalesce their full layout work, but keep a bounded wait rather
            # than restarting the timer indefinitely during continuous loads.
            if not hasattr(self, '_metadata_layout_timer'):
                self._metadata_layout_timer = QtCore.QTimer(self)
                self._metadata_layout_timer.setSingleShot(True)
                self._metadata_layout_timer.timeout.connect(self._finish_metadata_layout)
            self._metadata_layout_model = self.model().items
            refresh = getattr(self, '_metadata_refresh_state', None)
            if refresh is not None:
                refresh['geometry_changed'] = True
            elif not self._metadata_layout_timer.isActive():
                self._metadata_layout_timer.start(80)
        self._invalidate_overview_span()
        if geometry_changed:
            self._invalidate_display_frame()
        else:
            # Pixel/label changes do not move cards. Preserve unrelated tiles
            # and group previews while their changed regions refresh normally.
            self._details_key = None
            if len(changed_rows) > 256:
                # A whole-model notification needs one invalidation, not a
                # facade and an ever-growing dirty rectangle map for each row.
                self._composites.invalidate(preserve=True)
                self._display_dirty[-1] = QtCore.QRectF(self._content_bounds)
                self.wake_display_queue()
            else:
                for row in changed_rows:
                    self.request_display_refresh(self.model().items[row])

    def _finish_metadata_layout(self):
        if self._metadata_layout_model is self.model().items:
            self.arrange(preserve_view=True)
        self._metadata_layout_model = None

    @QtCore.pyqtSlot(bool)
    def _history_changed(self, _enabled):
        source = self.parent().view
        source.on_can_undo_changed(self.undo_stack.canUndo() or source.undo_stack.canUndo())
        source.on_can_redo_changed(self.undo_stack.canRedo() or source.undo_stack.canRedo())

    def model(self):
        return self._model

    def selectionModel(self):
        return self._selection

    def setItemDelegate(self, delegate):
        self.delegate = delegate

    def selectedIndexes(self):
        return self._selection.selectedIndexes()

    def currentIndex(self):
        return self._selection.currentIndex()

    def setCurrentIndex(self, index):
        self._selection.setCurrentIndex(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def indexAt(self, position):
        point = self.mapToScene(position)
        cards = self.visible_cards(QtCore.QRectF(point, QtCore.QSizeF(.01, .01)))
        return self._model.index(cards[-1].row) if cards else QtCore.QModelIndex()

    def _cells(self, rect):
        width, height = self._grid.width()+12, self._grid.height()+12
        for y in range(math.floor(rect.top()/height), math.floor((rect.bottom()-.001)/height)+1):
            for x in range(math.floor(rect.left()/width), math.floor((rect.right()-.001)/width)+1):
                yield (x, y)

    def visible_rows(self, rect, *, limit=None):
        """Return intersecting row numbers without allocating card facades."""
        if not self.cards or rect.isEmpty():
            return []
        rows = set()
        area = rect.width()*rect.height()/((self._grid.width()+12)*(self._grid.height()+12))
        if self._spatial and area > len(self._spatial)*2:
            rows.update(self._moved_rows)
        elif self._spatial:
            for cell in self._cells(rect):
                rows.update(self._spatial.get(cell, ()))
        if limit is not None:
            # Only diagnostics request a bounded candidate census. Normal
            # rendering retains its exact sorted stacking order below.
            result = []
            for row in rows:
                if self._card_rect(row).intersects(rect):
                    result.append(row)
                    if len(result) >= limit:
                        break
            return result
        return [row for row in sorted(rows) if self._card_rect(row).intersects(rect)]

    def visible_cards(self, rect, *, limit=None):
        return [self.cards[row] for row in self.visible_rows(rect, limit=limit)]

    def needs_display(self, item):
        row = self._item_rows.get(self._row_identity(item))
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        margin = min(rect.width(), rect.height())/4
        return row is not None and self._card_rect(row).intersects(
            rect.adjusted(-margin, -margin, margin, margin))

    def display_priority(self, item):
        row = self._item_rows.get(self._row_identity(item))
        if row is None:
            return float('inf')
        focus = self.mapToScene(getattr(self, '_detail_focus', self.viewport().rect().center()))
        delta = self._card_rect(row).center()-focus
        return delta.x()*delta.x()+delta.y()*delta.y()

    def move_card(self, card, point):
        # Manual placement can change a group's bounds and overlap order.
        # Revert to the existing exact renderer until the next arrangement.
        if not self._layout_in_progress:
            self._geometry_version += 1
            self._bump_content_geometry_revision()
            refresh = getattr(self, '_metadata_refresh_state', None)
            if refresh is not None:
                refresh['preserve_manual_layout'] = True
            timer = getattr(self, '_metadata_layout_timer', None)
            if timer is not None:
                timer.stop()
                self._metadata_layout_model = None
            if self._build_state is not None and self._build_state.get('keep_layout'):
                self._build_state['cancel'].set()
                self._build_state = None
            self._composites.reset()
            self._clear_layout_cache()
        self._details_key = None
        if card.isSelected():
            self._selection_bounds = None
        old = card.sceneBoundingRect()
        if not self._layout_in_progress and card.row in self._moved_rows:
            for cell in self._cells(old):
                bucket = self._spatial.get(cell)
                if bucket is not None:
                    bucket.discard(card.row)
                    if not bucket:
                        del self._spatial[cell]
        card._x, card._y = point.x(), point.y()
        # The committed geometry is the source of truth, so a manual move
        # has to be written into it: a facade that is later evicted and
        # rebuilt must come back at the place the user put it.
        self._set_card_position(card.row, card._x, card._y)
        self._render_layout_revision += 1
        rect = card.sceneBoundingRect()
        if not self._layout_in_progress:
            self._moved_rows.add(card.row)
            for cell in self._cells(rect):
                self._spatial.setdefault(cell, set()).add(card.row)
            self._layer.prepareGeometryChange()
            self._content_bounds = self._content_bounds.united(rect)
            self.scene().update(old.united(rect))

    def gridSize(self):
        return self._grid

    # -- committed geometry ------------------------------------------------
    def _card_position(self, row):
        """The committed position of one row, or None when there is none."""
        return self._geometry.position(row)

    def _card_size(self, row):
        """The committed content size of one row, or None when there is none."""
        return self._geometry.size(row)

    def _card_rect(self, row):
        """The committed scene rectangle of one row.

        Built from the committed geometry, so a caller that only needs the
        rectangle does not have to materialise a card facade.
        """
        position = self._geometry.position(row)
        size = self._geometry.size(row)
        if position is None or size is None:
            card = self.cards[row]
            return card.sceneBoundingRect()
        return QtCore.QRectF(position[0], position[1], size[0], size[1])

    def _set_card_position(self, row, x, y):
        """Write a row's position back into the committed geometry.

        A manual move must survive the facade being dropped and rebuilt,
        so the geometry arrays -- not the facade -- hold the truth.
        """
        try:
            self._geometry.x[row] = x
            self._geometry.y[row] = y
        except IndexError:
            pass

    def _commit_card_geometry(self, row, x, y, w, h):
        """Publish one row's committed geometry and refresh a live facade."""
        self._geometry.set(row, x, y, w, h)
        if row in self._selected_rows:
            self._selection_bounds = None
        record = self.cards.records[row]
        if record is not None:
            record._x, record._y = x, y
            record._size = QtCore.QSizeF(w, h)

    def setGridSize(self, size):
        if size == self._grid:
            return
        self._grid = size
        # 尺寸变化推进几何版本：布局键自然失效，下次 arrange 重排。
        self._geometry_version = getattr(self, '_geometry_version', 0) + 1
        # Invalidate immediately, including during the slider debounce. An
        # already queued collector or worker reply must not install old sizes.
        if self._build_state is not None:
            self._build_state['cancel'].set()
        # Slider events arrive faster than a large library can be packed.
        # Keep the final requested size and do one layout on the next quiet
        # interval instead of blocking input with every intermediate value.
        if not hasattr(self, '_grid_timer'):
            self._grid_timer = QtCore.QTimer(self)
            self._grid_timer.setSingleShot(True)
            self._grid_timer.setInterval(80)
            self._grid_timer.timeout.connect(self._apply_grid_size)
        self._grid_timer.start()

    def _apply_grid_size(self):
        size = self._grid
        if len(self.cards) >= 5000:
            self.arrange()
            return
        for card in self.cards:
            old = card._size
            card._size = old.scaled(QtCore.QSizeF(size.width(), size.width()), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        self.arrange()

    def _model_selection(self, *_):
        self._selected_rows = {index.row() for index in self.selectedIndexes()}
        self._selection_bounds = None
        self.viewport().update()

    def clear_annotations(self):
        self._stroke = None
        self._stroke_start = None
        for item in list(self.scene().items()):
            if item is not self._layer:
                self.scene().removeItem(item)
        self.undo_stack.clear()
        # Annotations are separate scene items. Their removal does not change
        # image geometry or pixels; retain the expensive image caches when
        # leaving and returning to the library.
        self.viewport().update()

    def erase_annotation(self, position):
        for item in self.items(position):
            if isinstance(item, QtWidgets.QGraphicsPathItem):
                self.scene().removeItem(item)
                return

    def _bump_content_geometry_revision(self):
        token = getattr(self.model(), 'layout_token', None)
        if token is not None:
            self._layout_content_revisions[token] = (
                self._layout_content_revisions.get(token, 0)+1)

    def snapshot_catalog_layout(self, items, *, token=None):
        """Snapshot compact committed geometry, without retaining row proxies."""
        if (self._build_state is not None or getattr(self, '_metadata_refresh_state', None) is not None
                or self.model().items is not items
                or token is None or token != getattr(self.model(), 'layout_token', None)
                or token != getattr(self.parent(), '_catalog_layout_token', None)):
            return None
        return dict(token=token, project=self._layout_project_id(),
                    geometry_version=self._geometry_version, pack_schema=1,
                    visual=getattr(self, '_committed_layout_visual', None),
                    content_geometry_revision=self._layout_content_revisions.get(token, 0),
                    grid=(self._grid.width(), self._grid.height()),
                    width=self.viewport().width(),
                    dpr=self.viewport().devicePixelRatioF(), row_count=len(items),
                    geometry=self._geometry.copy(),
                    spatial=self._spatial, item_rows=dict(self._item_rows),
                    moved=self._moved_rows, span=self._overview_pixel_span,
                    groups=self._composites.groups, row_group=self._composites.row_group,
                    bounds=QtCore.QRectF(self._content_bounds),
                    scene_rect=QtCore.QRectF(self.sceneRect()),
                    transform=QtGui.QTransform(self.transform()),
                    centre=self.mapToScene(self.viewport().rect().center()))

    def restore_catalog_layout(self, state):
        token = getattr(self.parent(), '_catalog_layout_token', None)
        if (state is None or token is None or state.get('token') != token
                or token != getattr(self.model(), 'layout_token', None)
                or state['project'] != self._layout_project_id()
                or state.get('content_geometry_revision') != self._layout_content_revisions.get(token, 0)
                or state.get('pack_schema') != 1
                or state['grid'] != (self._grid.width(), self._grid.height())
                or state['width'] != self.viewport().width()
                or state.get('dpr') != self.viewport().devicePixelRatioF()
                or state['row_count'] != len(self.model().items)):
            return False
        self._committed_layout_visual = state.get('visual')
        self._composites.reset()
        self.cards = _CardRows(self, len(self.model().items))
        self._spatial = state['spatial']
        self._geometry = state['geometry'].copy()
        self._item_rows = _RowIndex(self.model().items)
        self._item_rows.update(state['item_rows'])
        self._moved_rows = state['moved']
        self._overview_pixel_span = state['span']
        self._composites.groups, self._composites.row_group = state['groups'], state['row_group']
        self._layer.prepareGeometryChange()
        self._content_bounds = QtCore.QRectF(state['bounds'])
        self.scene().setSceneRect(state['scene_rect'])
        self.setTransform(state['transform'])
        self.centerOn(state['centre'])
        self._render_layout_revision += 1
        self._details_key = None
        self._reset_frame = None
        from prism import camera_policy
        camera_policy.reset_if_stale(self)
        self.viewport().update()
        from prism.performance_trace import record
        record('assets.layout_reused', rows=len(self.cards),
               content_token_matched=True, model_identity_unchanged=False,
               evaluated_rows=0)
        return True

    def suspend_for_grid(self):
        """Release optional board presentation when the library uses its grid."""
        if self._build_state is not None:
            self._build_state['cancel'].set()
            self._build_state = None
        for name in ('_grid_timer', '_metadata_layout_timer', '_display_timer'):
            timer = getattr(self, name, None)
            if timer is not None:
                timer.stop()
        self._metadata_refresh_state = None
        self._metadata_layout_model = None
        self._details_state = None
        self._catalog_geometry_items = None
        self._catalog_geometry_rows = None
        self._layer.prepareGeometryChange()
        self._geometry.clear()
        self.cards = _CardRows(self, 0)
        self._item_rows = _RowIndex()
        self._spatial.clear()
        self._moved_rows.clear()
        self._selected_rows.clear()
        self._selection_bounds = None
        self._content_bounds = QtCore.QRectF()
        self._composites.reset()
        self._tiles.reset()
        self._display_frame = None
        self._reset_frame = None
        self._display_dirty.clear()
        self._clear_layout_cache()

    def rebuild(self):
        if getattr(self.parent(), 'list', self) is not self:
            return  # Hidden canvas must not pack/index the grid's 100k results.
        self._metadata_refresh_state = None
        self._details_key = None
        self._details_state = None
        # Hydration caches are aliases to a model generation. Drop them at the
        # reset boundary, not at the next thumbnail delivery (which may never
        # happen for an empty/new result).
        self._catalog_geometry_items = None
        self._catalog_geometry_rows = None
        if self._build_state is not None:
            self._build_state['cancel'].set()
            self._build_state = None
        if hasattr(self, '_grid_timer'):
            self._grid_timer.stop()
        self._display_dirty.clear()
        self._tiles.reset()
        self.undo_stack.clear()
        # 项目切换/重开时清布局缓存：旧项目的范围/位置不得复用。
        project_id = self._layout_project_id()
        if getattr(self, '_last_project_id', None) != project_id:
            self._last_project_id = project_id
            self._clear_layout_cache()
        self._applied_layout_key = None
        rows = self._model.rowCount()
        # The row count has to be known as soon as the model is installed.
        # The card records are materialised on demand, so a 100k library
        # learns its row count without freezing the GUI thread.
        self.cards = _CardRows(self, rows)
        # These rows may have entirely different dimensions from the previous
        # result. Only an identity-validated snapshot may restore old geometry.
        self._geometry = _RowGeometry()
        self._geometry.resize(rows)
        self._selected_rows.clear()
        self._selection_bounds = None
        restore = getattr(self.parent(), '_catalog_restore_layout', None)
        if self.restore_catalog_layout(restore):
            return
        if rows >= 5000:
            self._start_staged_rebuild()
            return
        self._reset_frame = None
        self._item_rows = self._build_row_index(self._model.items)
        self.arrange()

    def _capture_reset_frame(self):
        if getattr(self.parent(), 'list', self) is not self:
            return
        if len(self.cards) >= 5000 and self.isVisible() and self._build_state is None:
            self._reset_frame = self.viewport().grab()

    def _start_staged_rebuild(self, *, keep_layout=False, preserve_view=False):
        previous = self._build_state
        if previous is not None:
            previous['cancel'].set()
        if not keep_layout:
            self._spatial = {}
            self._item_rows = _RowIndex(self.model().items)
            self._moved_rows = set()
            self._selected_rows.clear()
            self._selection_bounds = None
            self._composites.reset()
        cards = _CardRows(self, len(self.cards)) if keep_layout else self.cards
        state = dict(items=self.model().items, cards=cards,
                     grid=QtCore.QSize(self.gridSize()),
                     geometry_version=self._geometry_version,
                     sizes=array('i'), content=array('d'),
                     canvas_codes=array('i'), canvas_ids={},
                     cursor=0,
                     keep_layout=keep_layout, preserve_view=preserve_view,
                     started=time.perf_counter(),
                     cancel=threading.Event(), grouped=True,
                     visual=getattr(self.parent(), 'mode', None) == 'visual',
                     width=self.viewport().width(), spatial={},
                     item_rows=_RowIndex(self.model().items), moved=set(),
                     span=0.0, groups=[], row_group={}, buckets={})
        self._build_state = state
        QtCore.QTimer.singleShot(0, lambda: self._collect_layout_slice(state))
        self.viewport().update()

    def _build_current(self, state):
        if sip.isdeleted(self):
            state['cancel'].set()
            return False
        return (self._build_state is state and not state['cancel'].is_set()
                and self.model().items is state['items']
                and state['geometry_version'] == self._geometry_version
                and state['grid'] == self.gridSize())

    def _collect_layout_slice(self, state):
        if not self._build_current(state):
            return
        started = time.perf_counter()
        stop = min(len(state['items']), state['cursor'] + 256)
        grid = state['grid']
        items = state['items']
        while state['cursor'] < stop:
            row = state['cursor']
            item = items[row]
            # The content size is computed with the SAME helper the card
            # facade uses, so the pack rectangle derived from it here and
            # the size a rebuilt facade reports can never disagree.
            size = _card_content_size(item, grid)
            width = size.width()
            height = size.height()
            state['sizes'].append(max(1, math.ceil(width))+3)
            state['sizes'].append(max(1, math.ceil(height))+3)
            state['content'].append(width)
            state['content'].append(height)
            canvas_id = getattr(item, '_canvas_id', None)
            codes = state['canvas_codes']
            code = state['canvas_ids'].get(canvas_id)
            if code is None:
                code = len(state['canvas_ids'])
                state['canvas_ids'][canvas_id] = code
            codes.append(code)
            state['grouped'] = state['grouped'] and not isinstance(item, dict)
            state['cursor'] += 1
            if time.perf_counter()-started >= .004:
                break
        if state['cursor'] < len(state['items']):
            QtCore.QTimer.singleShot(0, lambda: self._collect_layout_slice(state))
        else:
            QtCore.QThreadPool.globalInstance().start(_PrepareLayout(state, self._layout_signals))

    @QtCore.pyqtSlot(object)
    def _layout_prepared(self, result):
        state, prepared, error = result
        if not self._build_current(state):
            return
        if error:
            state['error'] = error
            self.viewport().update()
            return  # Keep old cover; never fall back to synchronous full work.
        state['prepared'] = prepared
        state['cursor'] = 0
        self._install_layout_slice(state)

    def _install_composite_groups(self, state):
        """Install the group geometry the layout worker already computed.

        The worker derives the groups from pure scalars -- the same rows, the
        same order and the same 128-row batches the GUI used to rebuild by
        hand. Reusing them removes a second full traversal of the rows from
        the GUI thread and, more importantly, lets a group hold ROW
        REFERENCES instead of the card facades: a group no longer keeps a
        facade of every row of the library alive.
        """
        prepared = state['prepared']
        groups = []
        row_group = {}
        if state['grouped'] and not state['visual'] and len(state['cards']) >= self._composites.MIN_ITEMS:
            for index, group in enumerate(prepared.groups):
                bounds = group.bounds
                rect = QtCore.QRectF(12+bounds[0], 12+bounds[1], bounds[2], bounds[3])
                groups.append((rect, group.rows))
                for row in group.rows:
                    row_group[row] = index
            if len(row_group) != len(state['cards']):
                # The worker grouped a different row set than the canvas
                # installed. Fall back to no grouping rather than to a group
                # map that does not match the rows on screen.
                groups = []
                row_group = {}
        self._composites.groups = groups
        self._composites.row_group = row_group

    def _install_layout_slice(self, state):
        previous_camera=QtGui.QTransform(self.transform())
        if not self._build_current(state):
            return
        started = time.perf_counter()
        stop = min(len(state['cards']), state['cursor'] + 256)
        items = state['items']
        content = state['content']
        positions = state['prepared'].positions
        while state['cursor'] < stop:
            row = state['cursor']
            item = items[row]
            x, y = positions[row]
            width = content[row*2]
            height = content[row*2+1]
            # The committed geometry is four flat numbers per row; the card
            # facade is not created here. It is built when a row is drawn,
            # inspected or interacted with.
            self._commit_card_geometry(row, 12+x, 12+y, width, height)
            rect = QtCore.QRectF(12+x, 12+y, width, height)
            for cell in self._cells(rect):
                state['spatial'].setdefault(cell, set()).add(row)
            state['item_rows'][self._row_identity(item)] = row
            state['moved'].add(row)
            source = getattr(item, '_image_size', None)
            crop = getattr(item, 'crop', QtCore.QRectF())
            if source is None:
                source_span = max(width, height)
            else:
                source_span = max(source.width(), source.height())
            crop_span = max(crop.width(), crop.height(), 1) if not crop.isEmpty() else max(source_span, 1)
            state['span'] = max(state['span'], max(width, height)*source_span/crop_span)
            state['cursor'] += 1
            if time.perf_counter()-started >= .004:
                break
        if state['cursor'] < len(state['cards']):
            QtCore.QTimer.singleShot(0, lambda: self._install_layout_slice(state))
            return
        self._layer.prepareGeometryChange()
        camera = ((self.transform(), self.mapToScene(self.viewport().rect().center()))
                  if state['preserve_view'] else None)
        if state['keep_layout']:
            self._tiles.reset()
            self._composites.reset()
        self.cards, self._spatial = state['cards'], state['spatial']
        self._item_rows, self._moved_rows = state['item_rows'], state['moved']
        self._overview_pixel_span = state['span']
        self._install_composite_groups(state)
        width, height = state['prepared'].bounds
        self._content_bounds = QtCore.QRectF(12, 12, max(1, width), max(1, height))
        self._render_layout_revision += 1
        self._details_key = None
        self._build_state = None
        self._reset_frame = None
        self.scene().setSceneRect(-100000, -100000, 200000, max(200000, height+100024))
        if camera is None:
            self.resetTransform()
            self.horizontalScrollBar().setValue(0)
            self.verticalScrollBar().setValue(0)
        else:
            self.setTransform(camera[0])
            self.centerOn(camera[1])
        self._selection_bounds = None
        from prism.performance_trace import record
        from prism import camera_policy
        camera_policy.layout_completed(self,previous_camera)
        self._committed_layout_visual = state['visual']
        record('assets.layout_committed', rows=len(self.cards),
               asynchronous=True, retained_previous_layout=state['keep_layout'],
               preserved_camera=bool(camera),
               elapsed_ms=(time.perf_counter()-state['started'])*1000)
        self.viewport().update()
        self._publish_model_counts()

    def _publish_model_counts(self):
        host = getattr(self.parent(), 'window_host', None)
        if host is not None:
            host._update_status({}, interaction=True)

    def _layout_project_id(self):
        # AssetModel has no view attribute. The owner owns the project view;
        # reopening even the same filename creates a new scene project id.
        source = getattr(self.parent(), 'view', None)
        scene = getattr(source, 'scene', None)
        if callable(scene):
            scene = scene()
        return (id(scene), getattr(scene, 'project_id', None))

    def _identity_token(self, item):
        """Stable identity for one material row.

        ``id()`` cannot serve as an identity: CPython recycles the address
        of a freed object, so a deleted material and a freshly created one
        can share the same ``id()`` and therefore the same layout key.
        A material that is already saved is identified by its project
        ``save_id``; anything else gets a monotonic token that is attached
        to the object itself, so the token never depends on a recycled
        address and never changes while the object lives.
        """
        token = getattr(item, '_canvas_identity', None)
        if token is not None:
            return token
        save_id = getattr(item, 'save_id', None)
        if save_id is not None:
            token = ('save', save_id)
        else:
            self._identity_seq += 1
            token = ('token', self._identity_seq)
        try:
            item._canvas_identity = token
        except (AttributeError, TypeError):
            # Objects that cannot carry an attribute (search result dicts)
            # keep their token in a registry that holds the object alive, so
            # its id() can never be recycled while the token is in use.
            self._identity_registry[id(item)] = (token, item)
        return token

    def _row_identity(self, item):
        """Identity of one row: the stable token plus its canvas grouping."""
        return (self._identity_token(item), getattr(item, '_canvas_id', None))

    def _build_row_index(self, items):
        """Row map for the current rows, keyed by stable item identity."""
        index = _RowIndex(items)
        for row, item in enumerate(items):
            index[self._row_identity(item)] = row
        self._prune_identity_registry(items)
        return index

    def _prune_identity_registry(self, items):
        """Release registry entries for rows that no longer exist."""
        if not self._identity_registry:
            return
        live = {id(item) for item in items}
        for key in [key for key in self._identity_registry if key not in live]:
            del self._identity_registry[key]

    def _layout_range_version(self):
        # Exact ordered identity, not a sample or a bare hash: any reordered
        # row or changed canvas grouping changes the packing input.
        return tuple(self._row_identity(item) for item in self.model().items)

    def _layout_cache_key(self, sizes):
        from prism.compact_layout import layout_key
        owner = self.parent()
        mode = 'visual' if getattr(owner, 'mode', None) == 'visual' else 'pack'
        viewport_width = self.viewport().width() if mode == 'visual' else 0
        # Include exact fractional geometry, crop and source dimensions too:
        # equal rounded pack sizes may have different painted/hit-test bounds.
        geometry = tuple((card._size.width(), card._size.height(),
                          *self._source_geometry(item))
                         for card, item in zip(self.cards, self.model().items))
        return layout_key(self._layout_project_id(), self._layout_range_version(),
                          getattr(owner, 'sort_key', None), mode,
                          (self._grid.width(), self._grid.height(), geometry),
                          viewport_width)

    @staticmethod
    def _source_geometry(item):
        crop = getattr(item, 'crop', QtCore.QRectF())
        size = getattr(item, '_image_size', QtCore.QSizeF())
        return (size.width(), size.height(), crop.x(), crop.y(),
                crop.width(), crop.height())

    def _clear_layout_cache(self):
        """手工摆放/项目切换时清空自动布局缓存，避免回放旧位置。"""
        self._applied_layout_key = None
        if hasattr(self, '_layout_cache'):
            self._layout_cache.clear()

    @measured('assets.arrange', 0)
    def arrange(self, *, preserve_view=False):
        if len(self.cards) >= 5000:
            # Initial loading already has this bounded path; metadata arrivals
            # must not bypass it and synchronously repack/index 100k rows.
            active = self._build_state
            keep = active is None or active.get('keep_layout', False)
            self._start_staged_rebuild(keep_layout=keep, preserve_view=preserve_view)
            return
        camera = (self.transform(), self.mapToScene(self.viewport().rect().center())) if preserve_view else None
        try:
            self._arrange_contents()
        finally:
            if camera is not None:
                self.setTransform(camera[0])
                self.centerOn(camera[1])

    def _arrange_contents(self):
        if hasattr(self, '_grid_timer'):
            self._grid_timer.stop()
        from prism.compact_layout import pack_sizes
        sizes = [(max(1, math.ceil(c._size.width()))+3, max(1, math.ceil(c._size.height()))+3) for c in self.cards]
        # 布局复用（5A）：同键重复进入直接复用上次位置，不再 pack。手工摆放
        # （move_card）已清空缓存，这里不会回放旧自动位置覆盖手工布局。
        cache_key = self._layout_cache_key(sizes)
        if self._applied_layout_key == cache_key:
            # Nothing changed: keep spatial indexes, group pixels, manual
            # camera position and zoom. A cache hit is not a reason to rebuild.
            return
        self._details_key = None
        cached = self._layout_cache.get(cache_key)
        if cached is not None:
            positions, bounds = cached
            self._apply_positions(positions, bounds)
            self._applied_layout_key = cache_key
            return
        if getattr(self.parent(), 'mode', None) == 'visual':
            # Similarity order is meaningful: packing by rectangle size can
            # put the 150th result before the best match on screen.
            limit = max(400, self.viewport().width()-24)
            x, y, height, width, positions = 0, 0, 0, 0, []
            for w, h in sizes:
                if x and x+w > limit:
                    x, y, height = 0, y+height, 0
                positions.append((x, y))
                x += w
                width, height = max(width, x), max(height, h)
            bounds = (width, y+height)
        elif len(sizes) >= self._composites.MIN_ITEMS and all(not isinstance(i, dict) for i in self.model().items):
            # Keep each canvas's pictures spatially together. Packing all
            # rectangles globally can scatter one canvas across the entire
            # library; its mostly-empty composite would then be huge and
            # thrash the bounded cache instead of reducing draw work.
            from collections import OrderedDict
            canvases = OrderedDict()
            for row, item in enumerate(self.model().items):
                canvases.setdefault(getattr(item, '_canvas_id', None), []).append(row)
            batches = [rows[start:start+128] for rows in canvases.values()
                       for start in range(0, len(rows), 128)]
            layouts = [pack_sizes([sizes[row] for row in rows]) for rows in batches]
            origins, bounds = pack_sizes([(width+12, height+12) for _positions, (width, height) in layouts])
            positions = [(0, 0)]*len(sizes)
            for rows, (local, _size), (ox, oy) in zip(batches, layouts, origins):
                for row, (x, y) in zip(rows, local):
                    positions[row] = x+ox, y+oy
        else:
            positions, bounds = pack_sizes(sizes)
        self._layout_cache.put(cache_key, positions, bounds)
        self._apply_positions(positions, bounds)
        self._applied_layout_key = cache_key

    def _apply_positions(self, positions, bounds):
        """写回卡片位置并刷新场景/合成；缓存命中与未命中共用这一段。"""
        previous_camera=QtGui.QTransform(self.transform())
        self._layer.prepareGeometryChange()
        self._selection_bounds = None
        self._render_layout_revision += 1
        self._spatial.clear()
        self._moved_rows.clear()
        self._layout_in_progress = True
        self._overview_pixel_span = 0.0
        items = self.model().items
        for row, (x, y) in enumerate(positions):
            item = items[row]
            card = self.cards[row]
            source = getattr(item, '_image_size', None)
            crop = getattr(item, 'crop', QtCore.QRectF())
            if source is None:
                source_span = max(card._size.width(), card._size.height())
            else:
                source_span = max(source.width(), source.height())
            crop_span = max(crop.width(), crop.height(), 1) if not crop.isEmpty() else max(source_span, 1)
            span = max(card._size.width(), card._size.height())*source_span/crop_span
            self._overview_pixel_span = max(self._overview_pixel_span, span)
            self._commit_card_geometry(row, 12+x, 12+y,
                                       card._size.width(), card._size.height())
            self._moved_rows.add(row)
            for cell in self._cells(self._card_rect(row)):
                self._spatial.setdefault(cell, set()).add(row)
        self._layout_in_progress = False
        self._content_bounds = QtCore.QRectF(12, 12, max(1, bounds[0]), max(1, bounds[1]))
        self._composites.arrange()
        height = max(self.viewport().height(), bounds[1]+24)
        self.scene().setSceneRect(-100000, -100000, 200000, max(200000, height+100000))
        self.resetTransform()
        # Set the top-left camera explicitly; layout can run before the tab
        # has its final size, making centerOn shift all cards off-screen.
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
        from prism import camera_policy
        camera_policy.layout_completed(self,previous_camera)
        self.viewport().update()

    def selected_scene_bounds(self):
        """Exact selection bounds from committed geometry, without facades."""
        if self._selection_bounds is None:
            self._selection_bounds = QtCore.QRectF()
            for row in self._selected_rows:
                self._selection_bounds = self._selection_bounds.united(self._card_rect(row))
        return QtCore.QRectF(self._selection_bounds)

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if not self._selected_rows:
            return
        bounds = self.selected_scene_bounds()
        pen = QtGui.QPen(QtGui.QColor(30, 30, 32, 220), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for row in sorted(self._selected_rows):
            card_rect = self._card_rect(row)
            if card_rect.intersects(rect):
                painter.drawRect(card_rect)
        painter.drawRect(bounds)
        painter.setBrush(QtGui.QColor('#F0F0F2'))
        radius = 5/max(abs(self.transform().m11()), .001)
        for corner in (bounds.topLeft(), bounds.topRight(), bounds.bottomLeft(), bounds.bottomRight()):
            painter.drawEllipse(corner, radius, radius)

    def select_all(self):
        selection = QtCore.QItemSelection()
        if self._model.rowCount():
            selection.select(self._model.index(0), self._model.index(self._model.rowCount()-1))
        self._selection.select(selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)

    def arrange_selected(self, mode):
        cards = sorted((c for c in self.cards if c.isSelected()),
                       key=lambda c: str(self._model.data(self._model.index(c.row))).casefold())
        if not cards:
            return
        origin = QtCore.QPointF(min(c.x() for c in cards), min(c.y() for c in cards))
        columns = (len(cards) if mode == 'horizontal' else 1 if mode == 'vertical' else
                   max(1, math.ceil(math.sqrt(len(cards))) if mode == 'square' else
                       int(self.viewport().width()/((self._grid.width()+12)*self.transform().m11()))))
        positions = [origin+QtCore.QPointF((i % columns)*(self._grid.width()+12),
                     (i//columns)*(self._grid.height()+12)) for i in range(len(cards))]
        if mode == 'optimal':
            from prism.compact_layout import pack_sizes
            packed, _ = pack_sizes([(math.ceil(c._size.width())+3, math.ceil(c._size.height())+3) for c in cards])
            positions = [origin+QtCore.QPointF(x, y) for x, y in packed]
        class Arrange(QtGui.QUndoCommand):
            def __init__(self):
                super().__init__('排列素材')
                self.old = [QtCore.QPointF(c.pos()) for c in cards]
            def redo(self):
                for card, pos in zip(cards, positions):
                    card.setPos(pos)
            def undo(self):
                for card, pos in zip(cards, self.old):
                    card.setPos(pos)
        self.undo_stack.push(Arrange())

    def wheelEvent(self, event):
        steps = event.angleDelta().y()/120
        if not steps:
            steps = event.pixelDelta().y()/100
        if not steps:
            event.ignore()
            return
        factor = 1.2**max(-120, min(120, steps))
        current = self.transform().m11()
        bounds = self._content_bounds
        fit = min(self.viewport().width()/max(1, bounds.width()),
                  self.viewport().height()/max(1, bounds.height()))
        minimum = max(1e-6, min(.05, fit/4))
        scale = max(minimum, min(8, current*factor))
        if scale != current:
            self._detail_focus = event.position().toPoint()
            self._details_key = None
            self.scale(scale/current, scale/current)
            self.parent().window_host._update_status({}, interaction=True)
        event.accept()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self._pan = event.position().toPoint()
            self.viewport().setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
        elif event.button() == QtCore.Qt.MouseButton.LeftButton and self.tool != 'select':
            point = self.mapToScene(event.position().toPoint())
            if self.tool == 'eraser':
                self.erase_annotation(event.position().toPoint())
            elif self.tool == 'text':
                text, accepted = QtWidgets.QInputDialog.getText(self, '文字', '输入文字')
                if accepted and text:
                    item = self.scene().addText(text)
                    item.setDefaultTextColor(QtGui.QColor(self.parent().view._pen_color))
                    item.setPos(point)
                    item.setFlag(item.GraphicsItemFlag.ItemIsMovable)
            else:
                self._stroke_start = point
                path = QtGui.QPainterPath(point)
                self._stroke = self.scene().addPath(path, self.drawing_pen())
                self._stroke.setZValue(1)
            event.accept()
        elif event.button() == QtCore.Qt.MouseButton.LeftButton:
            index = self.indexAt(event.position().toPoint())
            flags = QtCore.QItemSelectionModel.SelectionFlag
            additive = bool(event.modifiers() & (QtCore.Qt.KeyboardModifier.ControlModifier | QtCore.Qt.KeyboardModifier.ShiftModifier))
            if index.isValid():
                if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:
                    anchor=getattr(self,'_range_anchor',self._selection.currentIndex().row())
                    anchor=max(0,anchor)
                    a,b=sorted((anchor,index.row()))
                    selection=QtCore.QItemSelection(self.model().index(a,0),self.model().index(b,0))
                    mode=flags.Select if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier else flags.ClearAndSelect
                    self._selection.select(selection,mode)
                elif additive:
                    self._range_anchor=index.row()
                    self._selection.select(index, flags.Toggle)
                elif index.row() not in self._selected_rows:
                    self._selection.select(index, flags.ClearAndSelect)
                if not event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier:self._range_anchor=index.row()
                self._selection.setCurrentIndex(index, flags.NoUpdate)
                self._drag_cards = (self.mapToScene(event.position().toPoint()),
                    {row: self.cards[row].pos() for row in self._selected_rows})
            else:
                if not additive:
                    self._selection.clearSelection()
                self._rubber_start = event.position().toPoint()
                self._rubberband.setGeometry(QtCore.QRect(self._rubber_start, QtCore.QSize()))
                self._rubberband.show()
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.tool == 'eraser' and event.buttons() & QtCore.Qt.MouseButton.LeftButton:
            self.erase_annotation(event.position().toPoint())
            event.accept()
            return
        if self._pan is not None:
            point = event.position().toPoint()
            delta = point-self._pan
            self._pan = point
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value()-delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value()-delta.y())
            event.accept()
        elif self._stroke is not None:
            point = self.mapToScene(event.position().toPoint())
            path = self._stroke.path() if self.tool == 'pen' else QtGui.QPainterPath(self._stroke_start)
            path.lineTo(point)
            if self.tool == 'arrow':
                delta = point-self._stroke_start
                distance = math.hypot(delta.x(), delta.y())
                pen = self.drawing_pen()
                if distance > 1e-6:
                    direction = delta/distance
                    length = min(max(12, pen.widthF()*4), distance*.8)
                    base = point-direction*length
                    perpendicular = QtCore.QPointF(-direction.y(), direction.x())*length*.42
                    head = QtGui.QPainterPath(point)
                    head.lineTo(base+perpendicular)
                    head.lineTo(base-perpendicular)
                    head.closeSubpath()
                    path = head
                    trim = length*.75
                    if distance-trim > pen.widthF()/2:
                        shaft = QtGui.QPainterPath(self._stroke_start)
                        shaft.lineTo(point-direction*trim)
                        stroker = QtGui.QPainterPathStroker()
                        stroker.setWidth(pen.widthF())
                        stroker.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
                        if pen.style() != QtCore.Qt.PenStyle.SolidLine:
                            stroker.setDashPattern(pen.dashPattern())
                        path = path.united(stroker.createStroke(shaft))
                self._stroke.setPen(QtGui.QPen(QtCore.Qt.PenStyle.NoPen))
                self._stroke.setBrush(pen.color())
            self._stroke.setPath(path)
            event.accept()
        elif self._drag_cards is not None:
            start, positions = self._drag_cards
            delta = self.mapToScene(event.position().toPoint())-start
            for row, pos in positions.items():
                self.cards[row].setPos(pos+delta)
            event.accept()
        elif self._rubber_start is not None:
            rect = QtCore.QRect(self._rubber_start, event.position().toPoint()).normalized()
            self._rubberband.setGeometry(rect)
            event.accept()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._pan is not None and event.button() == QtCore.Qt.MouseButton.MiddleButton:
            self._pan = None
            self.viewport().unsetCursor()
            event.accept()
        elif self._stroke is not None and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self._stroke = None
            event.accept()
        elif event.button() == QtCore.Qt.MouseButton.LeftButton:
            if self._rubber_start is not None:
                rect = self.mapToScene(self._rubberband.geometry()).boundingRect()
                selection = QtCore.QItemSelection()
                for card in self.visible_cards(rect):
                    index = self._model.index(card.row)
                    selection.select(index, index)
                flags = QtCore.QItemSelectionModel.SelectionFlag
                self._selection.select(selection, flags.Select if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier else flags.ClearAndSelect)
                self._rubberband.hide()
                self._rubber_start = None
            self._drag_cards = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and event.button() == QtCore.Qt.MouseButton.LeftButton:
            self.doubleClicked.emit(index)
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def activate(self, tool):
        self.tool = tool
        self.setDragMode(self.DragMode.RubberBandDrag if tool == 'select' else self.DragMode.NoDrag)
        self.viewport().setCursor(QtCore.Qt.CursorShape.ArrowCursor if tool == 'select' else QtCore.Qt.CursorShape.CrossCursor)

    def drawing_pen(self):
        source = self.parent().view
        pen = QtGui.QPen(QtGui.QColor(source._pen_color), source._pen_width)
        pen.setStyle({'solid': QtCore.Qt.PenStyle.SolidLine, 'dashed': QtCore.Qt.PenStyle.DashLine,
                      'dotted': QtCore.Qt.PenStyle.DotLine}.get(source._line_style, QtCore.Qt.PenStyle.SolidLine))
        pen.setCapStyle(QtCore.Qt.PenCapStyle.RoundCap)
        return pen

    def keyPressEvent(self, event):
        if event.matches(QtGui.QKeySequence.StandardKey.SelectAll):
            self.activate('select')
            self.select_all()
            self.parent().window_host.canvas_toolbar.refresh()
        elif event.matches(QtGui.QKeySequence.StandardKey.Undo):
            self.parent().view.on_action_undo()
        elif event.matches(QtGui.QKeySequence.StandardKey.Redo):
            self.parent().view.on_action_redo()
        elif event.key() == QtCore.Qt.Key.Key_0 and event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            self.parent().window_host.canvas_toolbar.fit_all()
        else:
            super().keyPressEvent(event)

    def event(self, event):
        if event.type() == QtCore.QEvent.Type.ShortcutOverride and any(
                event.matches(key) for key in (QtGui.QKeySequence.StandardKey.SelectAll,
                QtGui.QKeySequence.StandardKey.Undo, QtGui.QKeySequence.StandardKey.Redo)):
            event.accept()
            return True
        return super().event(event)
