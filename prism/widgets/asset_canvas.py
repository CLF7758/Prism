"""An independent, pannable material canvas backed by the browser's model."""
import math
from prism.performance_trace import measured

from PyQt6 import QtCore, QtGui, QtWidgets


class MaterialCard:
    """Lightweight layout record. A material does not need a Qt scene object."""
    __slots__ = ('host', 'row', '_x', '_y', '_size')

    def __init__(self, host, row):
        self.host, self.row = host, row
        self._x = self._y = 0.0
        item = host.model().items[row]
        crop = getattr(item, 'crop', QtCore.QRectF())
        size = crop.size() if not crop.isEmpty() else QtCore.QSizeF(getattr(item, '_image_size', host.gridSize()))
        self._size = size.scaled(QtCore.QSizeF(host.gridSize().width(), host.gridSize().width()),
                                 QtCore.Qt.AspectRatioMode.KeepAspectRatio)

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
        cards = host.visible_cards(viewport.intersected(option.exposedRect))
        host._last_painted_count = len(cards)
        pixels = host.gridSize().width()*abs(painter.worldTransform().m11())
        if pixels < 4:
            # At sub-pixel overview density, one colour patch represents each
            # image. No decode or thousands of graphics objects are needed.
            groups = {}
            for card in cards:
                item = host.model().items[card.row]
                color = getattr(item, 'dominant_color', None) or '#757B82'
                if not isinstance(color, str):
                    color = '#757B82'
                path = groups.setdefault(color, QtGui.QPainterPath())
                path.addRect(card.sceneBoundingRect().adjusted(4, 4, -4, -4))
            painter.setPen(QtCore.Qt.PenStyle.NoPen)
            for color, path in groups.items():
                painter.fillPath(path, QtGui.QColor(color))
            return
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
        self._item_rows = {}
        self._grid = QtCore.QSize(156, 154)
        self.cards = []
        self._selected_rows = set()
        self._selection_bounds = None
        self._spatial = {}
        self._moved_rows = set()
        self._default_columns = 1
        self._default_step = self._grid.width()+12
        self._content_bounds = QtCore.QRectF()
        self._layout_in_progress = False
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
        self._display_frame = None
        self._display_frame_key = None
        self._display_timer = QtCore.QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.setInterval(100)
        self._display_timer.timeout.connect(self._publish_display)
        self.scene().changed.connect(self._invalidate_display_frame)

    def _invalidate_display_frame(self, *_args):
        regions = _args[0] if _args and isinstance(_args[0], list) else None
        self._tiles.invalidate(regions)
        self._display_frame = None
        self.viewport().update()

    def request_display_refresh(self):
        if self._display_frame is None and not self._tiles.tiles:
            self.viewport().update()
        elif not self._display_timer.isActive():
            self._display_timer.start()

    def queue_visible_details(self):
        from prism import lod
        from prism.item_lifecycle import is_pixmap_item
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        scale = abs(self.transform().m11())*self.viewport().devicePixelRatioF()
        key = (id(self.cards), rect.x(), rect.y(), rect.width(), rect.height(), scale)
        if getattr(self, '_details_key', None) != key:
            self._details_key = key
            self._details_cards = self.visible_cards(rect)
            self._details_cursor = 0
        ready = True
        prefix_ready = True
        for index in range(self._details_cursor, len(self._details_cards)):
            card = self._details_cards[index]
            item = self.model().items[card.row]
            if not is_pixmap_item(item):
                if prefix_ready:
                    self._details_cursor = index+1
                continue
            crop = item.crop.size()
            size = item._image_size
            pixels = max(card._size.width(), card._size.height())*scale*max(size.width(), size.height())/max(crop.width(), crop.height(), 1)
            if not lod.base_preview_ready(item, pixels, self):
                ready = False
                prefix_ready = False
                decoder = getattr(QtWidgets.QApplication.instance(), '_prism_display_decoder', None)
                if decoder is not None and len(decoder.requests) >= decoder.max_pending:
                    break
            elif prefix_ready:
                self._details_cursor = index+1
        return ready

    def _publish_display(self):
        if self.queue_visible_details():
            rect = self.mapToScene(self.viewport().rect()).boundingRect()
            self._tiles.invalidate([rect])
            self.viewport().update()
        else:
            self._display_timer.start()

    def paintEvent(self, event):
        rect = self.mapToScene(self.viewport().rect()).boundingRect()
        if len(self.cards) < 2000 or len(self.visible_cards(rect)) < 128:
            self._display_frame = None
            return super().paintEvent(event)
        painter = QtGui.QPainter(self.viewport())
        self._tiles.paint(painter)
        painter.setWorldTransform(self.viewportTransform())
        self.drawForeground(painter, rect)
        painter.end()

    def _render_tile(self, image, target, rect):
        painter = QtGui.QPainter(image)
        painter.setRenderHints(self.renderHints())
        self._tile_recording_rect = rect
        try:
            self.scene().render(painter, target, rect, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio)
        finally:
            self._tile_recording_rect = None
            painter.end()

    def setModel(self, model):
        self._model = model
        self._selection = QtCore.QItemSelectionModel(model, self)
        self._selection.selectionChanged.connect(self._model_selection)
        model.modelReset.connect(self.rebuild)
        model.dataChanged.connect(self._invalidate_display_frame)

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

    def indexAt(self, position):
        point = self.mapToScene(position)
        cards = self.visible_cards(QtCore.QRectF(point, QtCore.QSizeF(.01, .01)))
        return self._model.index(cards[-1].row) if cards else QtCore.QModelIndex()

    def _cells(self, rect):
        width, height = self._grid.width()+12, self._grid.height()+12
        for y in range(math.floor(rect.top()/height), math.floor((rect.bottom()-.001)/height)+1):
            for x in range(math.floor(rect.left()/width), math.floor((rect.right()-.001)/width)+1):
                yield (x, y)

    def visible_cards(self, rect):
        if not self.cards or rect.isEmpty():
            return []
        rows = set()
        area = rect.width()*rect.height()/((self._grid.width()+12)*(self._grid.height()+12))
        if self._spatial and area > len(self._spatial)*2:
            rows.update(self._moved_rows)
        elif self._spatial:
            for cell in self._cells(rect):
                rows.update(self._spatial.get(cell, ()))
        return [self.cards[row] for row in sorted(rows) if self.cards[row].sceneBoundingRect().intersects(rect)]

    def needs_display(self, item):
        row = self._item_rows.get(id(item))
        return row is not None and self.cards[row].sceneBoundingRect().intersects(self._tiles.prefetch_rect())

    def display_priority(self, item):
        row = self._item_rows.get(id(item))
        if row is None:
            return float('inf')
        delta = self.cards[row].sceneBoundingRect().center()-self.mapToScene(self.viewport().rect().center())
        return delta.x()*delta.x()+delta.y()*delta.y()

    def move_card(self, card, point):
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

    def setGridSize(self, size):
        self._grid = size
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
        self._tiles.reset()
        self.viewport().update()

    def erase_annotation(self, position):
        for item in self.items(position):
            if isinstance(item, QtWidgets.QGraphicsPathItem):
                self.scene().removeItem(item)
                return

    def rebuild(self):
        self._tiles.reset()
        self.undo_stack.clear()
        self.cards = [MaterialCard(self, row) for row in range(self._model.rowCount())]
        self._item_rows = {id(item): row for row, item in enumerate(self._model.items)}
        self._selected_rows.clear()
        self._selection_bounds = None
        self.arrange()

    @measured('assets.arrange', 0)
    def arrange(self):
        self._details_key = None
        from prism.compact_layout import pack_sizes
        sizes = [(max(1, math.ceil(c._size.width()))+3, max(1, math.ceil(c._size.height()))+3) for c in self.cards]
        positions, bounds = pack_sizes(sizes)
        self._layer.prepareGeometryChange()
        self._selection_bounds = None
        self._spatial.clear()
        self._moved_rows.clear()
        self._layout_in_progress = True
        for card, (x, y) in zip(self.cards, positions):
            card._x, card._y = 12+x, 12+y
            self._moved_rows.add(card.row)
            for cell in self._cells(card.sceneBoundingRect()):
                self._spatial.setdefault(cell, set()).add(card.row)
        self._layout_in_progress = False
        self._content_bounds = QtCore.QRectF(12, 12, max(1, bounds[0]), max(1, bounds[1]))
        height = max(self.viewport().height(), bounds[1]+24)
        self.scene().setSceneRect(-100000, -100000, 200000, max(200000, height+100000))
        self.resetTransform()
        # Set the top-left camera explicitly; layout can run before the tab
        # has its final size, making centerOn shift all cards off-screen.
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
        self.viewport().update()

    def drawForeground(self, painter, rect):
        super().drawForeground(painter, rect)
        if not self._selected_rows:
            return
        if self._selection_bounds is None:
            self._selection_bounds = QtCore.QRectF()
            for row in self._selected_rows:
                self._selection_bounds = self._selection_bounds.united(self.cards[row].sceneBoundingRect())
        bounds = self._selection_bounds
        pen = QtGui.QPen(QtGui.QColor(30, 30, 32, 220), 1)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.BrushStyle.NoBrush)
        for card in self.visible_cards(rect):
            if card.isSelected():
                painter.drawRect(card.sceneBoundingRect())
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
        factor = 1.15 if event.angleDelta().y() > 0 else 1/1.15
        scale = self.transform().m11()*factor
        if .05 <= scale <= 8:
            self.scale(factor, factor)
            self.parent().window_host._update_status({})
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
                if additive:
                    self._selection.select(index, flags.Toggle)
                elif index.row() not in self._selected_rows:
                    self._selection.select(index, flags.ClearAndSelect)
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
