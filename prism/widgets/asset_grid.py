"""Scrollable library grid with arithmetic geometry and viewport-only painting.

The query/model and thumbnail pipeline are shared with AssetBrowser. No scene,
packing worker, per-result rectangles or composite overview is built here.
"""
import math
import time
import weakref

from PyQt6 import QtCore, QtGui, QtWidgets

class AssetGrid(QtWidgets.QAbstractItemView):
    is_asset_grid = True
    card_size_changed = QtCore.pyqtSignal(int)

    def wheelEvent(self,event):
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            delta=event.angleDelta().y() or event.pixelDelta().y()
            if delta:
                current=self.thumbnailSize()
                width=max(96,min(280,current.width()+(12 if delta>0 else -12)))
                ratio=current.height()/max(1,current.width())
                self.setGridSize(QtCore.QSize(width,int(width*ratio)))
                self.card_size_changed.emit(width)
            event.accept();return
        super().wheelEvent(event)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._grid = QtCore.QSize(168, 166)
        self._build_state = None
        self._reset_frame = None
        self._geometry_version = 0
        self._layout_content_revisions = weakref.WeakKeyDictionary()
        self._item_rows = {}
        self._catalog_geometry_items = None
        self._catalog_geometry_rows = None
        self.colour_group = None
        self.display_grayscale = False
        self.tool = 'select'
        self.undo_stack = QtGui.QUndoStack(self)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setSelectionMode(self.SelectionMode.ExtendedSelection)
        self.setSelectionBehavior(self.SelectionBehavior.SelectItems)
        self.setEditTriggers(self.EditTrigger.NoEditTriggers)
        self.setVerticalScrollMode(self.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setMouseTracking(True)
        self.setAutoScroll(True)
        palette = self.viewport().palette()
        palette.setColor(QtGui.QPalette.ColorRole.Base, QtGui.QColor('#1b1b1d'))
        self.viewport().setPalette(palette)
        self.viewport().setAutoFillBackground(True)
        self._display_timer = QtCore.QTimer(self)
        self._display_timer.setSingleShot(True)
        self._display_timer.setInterval(40)
        self._display_timer.timeout.connect(self.viewport().update)
        self._display_timer.timeout.connect(self._refresh_native_images)
        self.native_cards = None
        self._native_rows = {}
        self._native_requests = set()
        self._native_items = []
        self._native_count = 0
        self._native_query = None
        self._native_prefix_check = None
        self._native_timer = QtCore.QTimer(self)
        self._native_timer.setSingleShot(True)
        self._native_timer.timeout.connect(self._sync_native_cards)

    def setModel(self, model):
        previous = self.model()
        if previous is not None:
            previous.modelReset.disconnect(self._rows_reset)
        super().setModel(model)
        if model is not None:
            model.modelReset.connect(self._rows_reset)
        self._rows_reset()

    def dataChanged(self, first, last, roles=None):
        # Cells have arithmetic, fixed-size geometry. Qt 6.7's default handler
        # calls visualRect for every changed index, freezing large refreshes.
        if last.row()-first.row() < 256 or self.state() == self.State.EditingState:
            super().dataChanged(first, last, roles or [])
        else:
            self.viewport().update()
        if self.native_cards is not None:
            self._native_prefix_check = None
            self._native_timer.start(0)

    def _active(self):
        return getattr(self.parent(), 'list', self) is self

    def _rows_reset(self):
        self._native_prefix_check = None
        self._item_rows.clear()
        self._catalog_geometry_items = None
        self._catalog_geometry_rows = None
        self.verticalScrollBar().setValue(0)
        self.updateGeometries()
        if self._active():
            self.viewport().update()
        self._native_timer.start(0)

    @staticmethod
    def _resource_id(item):
        return (item['result_id'] if isinstance(item, dict) else
                'asset:' + str(getattr(item, 'save_id', None) if getattr(item, 'save_id', None) is not None else id(item)))

    def _native_prefix_matches(self, previous, items, query):
        """Keep the existing cards while certifying a replacement in slices."""
        count = self._native_count
        if previous is items:
            return True
        if count > len(previous):
            return False
        check = self._native_prefix_check
        if (check is None or check['previous'] is not previous or check['items'] is not items
                or check['query'] != query or check['count'] != count
                or check['length'] != len(items)):
            check = self._native_prefix_check = dict(previous=previous, items=items,
                query=query, count=count, length=len(items), row=0, result=None)
        if check['result'] is not None:
            return check['result']
        deadline = time.perf_counter()+.002
        stop = min(count, check['row']+512)
        while check['row'] < stop:
            row = check['row']
            if self._resource_id(previous[row]) != self._resource_id(items[row]):
                check['result'] = False
                return False
            check['row'] += 1
            if time.perf_counter() >= deadline:
                break
        if check['row'] == count:
            check['result'] = True
            return True
        self._native_timer.start(1)
        return None

    def _sync_native_cards(self):
        if self.model() is None or not hasattr(self.model(), 'items'):
            return
        items = self.model().items
        check = self._native_prefix_check
        mixed = (check is not None and check['items'] is items and check['length'] == len(items))
        if not mixed:
            mixed = any(isinstance(item, dict) and item.get('kind') in ('model', 'material') for item in items)
        if not mixed and self.native_cards is None:
            return
        if self.native_cards is None:
            from prism.widgets.material_cards import MaterialCards
            self.native_cards = MaterialCards(self.viewport())
            layout = QtWidgets.QVBoxLayout(self.viewport())
            layout.setContentsMargins(0, 0, 0, 0)
            layout.addWidget(self.native_cards)
            self.native_cards.thumbnail_requested.connect(self._native_thumbnail)
            self.native_cards.selection_toggled.connect(self._native_select)
            self.native_cards.selection_replaced.connect(self._native_select_many)
            self.native_cards.restore_requested.connect(lambda ids: self.parent().restore_resources([
                self.model().items[self._native_rows[key]] for key in ids if key in self._native_rows]))
            self.native_cards.open_requested.connect(lambda key: self.doubleClicked.emit(
                self.model().index(self._native_rows[key], 0)) if key in self._native_rows else None)
            self.native_cards.card_size_requested.connect(self.card_size_changed.emit)
        if not mixed:
            self.native_cards.hide()
            self.native_cards.set_display_assets([], virtual=True)
            self._native_items = []
            self._native_count = 0
            self._native_rows.clear()
            self._native_requests.clear()
            self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
            return
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.native_cards.show()
        previous = self._native_items
        host = self.parent()
        query = (host._scene_refresh_key(), getattr(host, '_catalog_requested_digest', None))
        append = (self.native_cards._virtual and self._native_query == query
                  and self._native_count <= len(items))
        if append:
            append = self._native_prefix_matches(previous, items, query)
            if append is None:
                return
        self._native_query = query
        self._native_prefix_check = None
        start = self._native_count if append else 0
        if not append:
            self._native_rows.clear()
            self._native_requests.clear()
        scene = self.parent().view.scene
        materials = {r['id']: r for r in getattr(scene, 'material_records', ())}
        models = {r['id']:r for r in getattr(scene,'model_records',())}
        assets = []
        for row in range(start, len(items)):
            item = items[row]
            key = self._resource_id(item)
            self._native_rows[key] = row
            kind = item.get('kind') if isinstance(item, dict) else 'asset'
            name = item.get('title', '') if isinstance(item, dict) else getattr(item, '_title', '') or getattr(item, 'filename', '')
            asset = dict(id=key, name=name, tags=item.get('tags', []) if isinstance(item, dict) else list(getattr(item, '_tags', ())),
                         assetKind=kind, thumbnail='', thumbnailPending=True, sourceLabel='', versionLabel='',
                         resolution='', fileCount=0, maps=[], metaLabel='', dragEnabled=False,
                         renameEnabled=False, exportEnabled=False, previewEnabled=True,
                         restoreEnabled=self.parent().mode == 'trash')
            if kind == 'material':
                from prism.widgets.material_cards import material_card_fields
                record = materials.get(item.get('material_id'), {})
                asset.update(material_card_fields(record))
                asset.pop('metaLabel')
            elif kind == 'model':
                from prism.widgets.material_cards import model_card_fields
                asset.update(model_card_fields(models.get(item.get('model_id'),{}).get('format','')))
            assets.append(asset)
        self._native_items = items
        self._native_count = len(items)
        if append:
            if assets:
                self.native_cards.append_display_assets(assets)
        else:
            self.native_cards.set_display_assets(assets, virtual=True)
        size = self.thumbnailSize().width()
        if self.native_cards.mode != 'grid' or self.native_cards.card_size != size:
            self.native_cards.set_layout('grid', size)

    def _native_select(self, key, additive):
        if key not in self._native_rows:
            return
        index = self.model().index(self._native_rows[key], 0)
        flags = (QtCore.QItemSelectionModel.SelectionFlag.Toggle if additive else
                 QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self._native_selecting = True
        try:
            self.selectionModel().select(index, flags)
            self.selectionModel().setCurrentIndex(index, QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._native_selecting = False

    def _native_select_many(self, keys, current, additive):
        selection = QtCore.QItemSelection()
        for key in keys:
            if key in self._native_rows:
                index = self.model().index(self._native_rows[key], 0)
                selection.select(index, index)
        self._native_selecting = True
        try:
            self.selectionModel().select(selection, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
            if current in self._native_rows:
                self.selectionModel().setCurrentIndex(self.model().index(self._native_rows[current], 0),
                                                      QtCore.QItemSelectionModel.SelectionFlag.NoUpdate)
        finally:
            self._native_selecting = False

    def _native_thumbnail(self, key):
        if self.native_cards is None or key not in self._native_rows:
            return
        row = self._native_rows[key]
        if row >= self.model().rowCount():
            return
        item = self.model().items[row]
        if self._resource_id(item) != key:
            return
        self._native_requests.add(key)
        if len(self._native_requests) > 256:
            self._native_requests = {key}
        if isinstance(item, dict) and item.get('kind') in ('material', 'model'):
            scene = self.parent().view.scene
            kind = item['kind']
            resource_id = item.get('material_id' if kind == 'material' else 'model_id')
            record = next((record for record in getattr(scene,
                'material_records' if kind == 'material' else 'model_records', ())
                if record.get('id') == resource_id), None)
            cover = (record.get('preview' if kind == 'material' else 'thumbnail') or {}) if record is not None else item.get('cover') or {}
            if item['kind'] == 'material':
                from prism.widgets.material_cards import material_thumbnail
                thumbnail = material_thumbnail({'id': item.get('material_id'),
                    'preview': cover}, scene.attachments)
            else:
                from pathlib import Path
                from prism.model_library.repository import cached_model_cover
                root = Path(QtCore.QStandardPaths.writableLocation(
                    QtCore.QStandardPaths.StandardLocation.CacheLocation)) / 'model-library' / 'covers'
                thumbnail = cached_model_cover(cover, root)
            self.native_cards.update_thumbnail({'id': key}, thumbnail)
            self._native_requests.discard(key)
            return
        else:
            if getattr(item, '_catalog_proxy', False) and not item._catalog_display_ready:
                item.request_catalog_display(self)
                return
            image = QtGui.QImage(240, 160, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QtGui.QColor('#222225'))
            painter = QtGui.QPainter(image)
            option = QtWidgets.QStyleOptionViewItem()
            self.initViewItemOption(option)
            option.rect = image.rect()
            try:
                if isinstance(item, dict):
                    from prism.widgets.asset_browser import AssetDelegate
                    AssetDelegate(self).paint(painter, option, self.model().index(row, 0))
                else:
                    from prism import lod
                    if not lod.draw_image(painter, item, QtCore.QRectF(image.rect()), self):
                        return
            finally:
                painter.end()
        import base64
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        self.native_cards.update_thumbnail({'id': key},
            'data:image/png;base64,' + base64.b64encode(bytes(buffer.data())).decode('ascii'))
        self._native_requests.discard(key)

    def _refresh_native_images(self):
        if self.native_cards is not None and self.native_cards.isVisible():
            for key in list(self._native_requests):
                self._native_thumbnail(key)

    def refresh_resource_covers(self, kind, resource_id=None):
        if self.model() is None or self.native_cards is None:
            return
        scene = self.parent().view.scene
        records = getattr(scene, 'model_records' if kind == 'model' else 'material_records', ())
        covers = {str(record.get('id')):record.get('thumbnail' if kind == 'model' else 'preview') or {}
                  for record in records if resource_id is None or str(record.get('id')) == str(resource_id)}
        for key, cover in covers.items():
            identity = kind+':'+key
            row = self._native_rows.get(identity)
            if row is not None and row < len(self.model().items):
                item = self.model().items[row]
                if isinstance(item,dict) and self._resource_id(item) == identity:
                    item['cover'] = cover
                    self._native_thumbnail(identity)

    def columns(self):
        return max(1, self.viewport().width() // max(1, self._grid.width()))

    def _column_at(self, x):
        # Match the integer boundaries used by visualRect, including the
        # remainder pixels distributed across the row.
        width = max(1, self.viewport().width())
        return ((x + 1) * self.columns() - 1) // width

    def thumbnailSize(self):
        return QtCore.QSize(self._grid.width()-12,self._grid.height()-48)

    def gridSize(self):
        return QtCore.QSize(self._grid)

    def setGridSize(self, size):
        # Cell dimensions include image, two text lines and spacing.
        self._grid = QtCore.QSize(max(80, size.width()+12), max(104, size.height()+48))
        self.updateGeometries()
        self.viewport().update()
        if self.native_cards is not None:
            self.native_cards.set_layout('grid', size.width())

    def updateGeometries(self):
        count = self.model().rowCount() if self.model() is not None else 0
        height = math.ceil(count / self.columns()) * self._grid.height()
        bar = self.verticalScrollBar()
        bar.setPageStep(self.viewport().height())
        bar.setSingleStep(max(20, self._grid.height() // 3))
        bar.setRange(0, max(0, height-self.viewport().height()))
        self.horizontalScrollBar().setRange(0, 0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.updateGeometries()

    def rowsInserted(self, parent, start, end):
        super().rowsInserted(parent, start, end)
        self.updateGeometries()
        self.viewport().update()
        self._native_timer.start(40)

    def horizontalOffset(self):
        return 0

    def verticalOffset(self):
        return self.verticalScrollBar().value()

    def isIndexHidden(self, index):
        return False

    def visualRect(self, index):
        if not index.isValid() or index.model() is not self.model():
            return QtCore.QRect()
        line, column = divmod(index.row(), self.columns())
        width, columns = max(1, self.viewport().width()), self.columns()
        left, right = column*width//columns, (column+1)*width//columns
        return QtCore.QRect(left,
                            line*self._grid.height()-self.verticalOffset(),
                            right-left, self._grid.height())

    def indexAt(self, point):
        if self.model() is None or not self.viewport().rect().contains(point):
            return QtCore.QModelIndex()
        column = self._column_at(point.x())
        if column >= self.columns():
            return QtCore.QModelIndex()
        row = ((point.y()+self.verticalOffset()) // self._grid.height())*self.columns()+column
        return self.model().index(row, 0) if row < self.model().rowCount() else QtCore.QModelIndex()

    def scrollTo(self, index, hint=QtWidgets.QAbstractItemView.ScrollHint.EnsureVisible):
        if not index.isValid() or getattr(self, '_native_selecting', False):
            return
        if self.native_cards is not None and self.native_cards.isVisible() and self.native_cards.ready:
            import json
            key = self._resource_id(index.data(QtCore.Qt.ItemDataRole.UserRole))
            self.native_cards.web.page().runJavaScript('prismCards.reveal(' + json.dumps(self.native_cards._dom_id(key)) + ')')
            return
        rect = self.visualRect(index)
        value = self.verticalOffset()
        if hint == self.ScrollHint.PositionAtTop:
            value += rect.top()
        elif hint == self.ScrollHint.PositionAtBottom:
            value += rect.bottom()-self.viewport().height()+1
        elif hint == self.ScrollHint.PositionAtCenter:
            value += rect.center().y()-self.viewport().height()//2
        elif rect.top() < 0:
            value += rect.top()
        elif rect.bottom() >= self.viewport().height():
            value += rect.bottom()-self.viewport().height()+1
        self.verticalScrollBar().setValue(value)

    def moveCursor(self, action, modifiers):
        count = self.model().rowCount() if self.model() is not None else 0
        if not count:
            return QtCore.QModelIndex()
        current = self.currentIndex().row()
        columns = self.columns()
        page = max(1, self.viewport().height()//self._grid.height())*columns
        offsets = {self.CursorAction.MoveLeft: -1, self.CursorAction.MoveRight: 1,
                   self.CursorAction.MoveUp: -columns, self.CursorAction.MoveDown: columns,
                   self.CursorAction.MovePrevious: -1, self.CursorAction.MoveNext: 1,
                   self.CursorAction.MovePageUp: -page, self.CursorAction.MovePageDown: page}
        target = current+offsets.get(action, 0)
        if action == self.CursorAction.MoveHome:
            target = 0
        elif action == self.CursorAction.MoveEnd:
            target = count-1
        return self.model().index(min(count-1, max(0, target)), 0)

    def setSelection(self, rect, flags):
        if self.model() is None or not self.model().rowCount():
            return
        rect = rect.normalized()
        columns = self.columns()
        left = max(0, self._column_at(rect.left()))
        right = min(columns-1, self._column_at(rect.right()))
        first = max(0, (rect.top()+self.verticalOffset())//self._grid.height())
        last = min((self.model().rowCount()-1)//columns,
                   (rect.bottom()+self.verticalOffset())//self._grid.height())
        selection = QtCore.QItemSelection()
        for line in range(first, last+1):
            low, high = line*columns+left, min(self.model().rowCount()-1, line*columns+right)
            if low <= high:
                selection.select(self.model().index(low, 0), self.model().index(high, 0))
        self.selectionModel().select(selection, flags)

    def visualRegionForSelection(self, selection):
        # Updating the viewport is bounded even for Select All on 100k rows.
        return QtGui.QRegion(self.viewport().rect()) if not selection.isEmpty() else QtGui.QRegion()

    def _visible_rows(self, margin=0):
        count = self.model().rowCount() if self.model() is not None else 0
        columns = self.columns()
        first = max(0, self.verticalOffset()//self._grid.height()-margin)*columns
        last = (math.ceil((self.verticalOffset()+self.viewport().height())/self._grid.height())+margin)*columns
        return range(first, min(count, last))

    def _row_identity(self, item):
        token = getattr(item, '_canvas_identity', None)
        if token is None:
            saved = getattr(item, 'save_id', None)
            token = ('save', saved) if saved is not None else ('grid', id(item))
        return token, getattr(item, '_canvas_id', None)

    def paintEvent(self, event):
        if self.native_cards is not None and self.native_cards.isVisible():
            return
        if self.model() is None:
            return
        painter = QtGui.QPainter(self.viewport())
        painter.setClipRegion(event.region())
        self._item_rows.clear()
        try:
            for row in self._visible_rows(margin=1):
                index = self.model().index(row, 0)
                item = index.data(QtCore.Qt.ItemDataRole.UserRole)
                if item is not None:
                    self._item_rows[id(item)] = row
                    self._item_rows[self._row_identity(item)] = row
                rect = self.visualRect(index).adjusted(5, 5, -5, -9)
                if not rect.intersects(event.rect()):
                    continue
                option = QtWidgets.QStyleOptionViewItem()
                self.initViewItemOption(option)
                option.rect = rect
                if self.selectionModel().isSelected(index):
                    option.state |= QtWidgets.QStyle.StateFlag.State_Selected
                if self.currentIndex() == index and self.hasFocus():
                    option.state |= QtWidgets.QStyle.StateFlag.State_HasFocus
                self.itemDelegate().paint(painter, option, index)
        finally:
            painter.end()

    def scrollContentsBy(self, dx, dy):
        self.viewport().update()

    def needs_display(self, item):
        if self.native_cards is not None and self.native_cards.isVisible():
            return self._resource_id(item) in self._native_requests
        row = self._item_rows.get(id(item))
        return bool(self.isVisible() and row is not None and self.model() is not None
                    and row < self.model().rowCount()
                    and self.model().items[row] is item
                    and self.visualRect(self.model().index(row, 0)).intersects(
                        self.viewport().rect().adjusted(0, -self._grid.height(), 0, self._grid.height())))

    def display_priority(self, item):
        row = self._item_rows.get(id(item))
        if row is None:
            return float('inf')
        delta = self.visualRect(self.model().index(row, 0)).center()-self.viewport().rect().center()
        return delta.x()*delta.x()+delta.y()*delta.y()

    def request_display_refresh(self, item=None):
        if item is None or self.needs_display(item):
            self.wake_display_queue()

    def wake_display_queue(self):
        if not self._display_timer.isActive():
            self._display_timer.start()

    def queue_visible_details(self):
        self.wake_display_queue()
        return True

    def catalog_rows_changed(self, rows):
        self.viewport().update()

    def clear_annotations(self):
        # Library grids have no annotation scene or editable card positions.
        pass

    def arrange(self, **kwargs):
        self.updateGeometries()
        self.viewport().update()

    def arrange_selected(self, mode):
        self.arrange()

    def snapshot_catalog_layout(self, items, *, token=None):
        return None  # Geometry is computed directly from row/column, not cached.

    def _invalidate_display_frame(self, *args):
        self.viewport().update()

    def _publish_model_counts(self):
        host = getattr(self.parent(), 'window_host', None)
        if host is not None:
            host._update_status({}, interaction=True)

    def transform(self):
        return QtGui.QTransform()  # Status compatibility; grids do not zoom.

    def select_all(self):
        self.selectAll()

    def reset_position(self):
        self.verticalScrollBar().setValue(0)
        if self.native_cards is not None:
            self.native_cards.set_scroll_position(0)


class GridDelegate(QtWidgets.QStyledItemDelegate):
    """Labels around the existing asset renderer, without another decoder."""

    def __init__(self, grid, image_delegate):
        super().__init__(grid)
        self.image_delegate = image_delegate

    def paint(self, painter, option, index):
        from prism.widgets.asset_browser import asset_name
        item = index.data(QtCore.Qt.ItemDataRole.UserRole)
        painter.save()
        rect = option.rect
        if option.state & QtWidgets.QStyle.StateFlag.State_Selected:
            painter.setPen(QtGui.QPen(QtGui.QColor('#629bea'), 1))
            painter.setBrush(QtGui.QColor('#303e54'))
            painter.drawRoundedRect(rect, 5, 5)
        image_option = QtWidgets.QStyleOptionViewItem(option)
        image_option.rect = rect.adjusted(3, 3, -3, -43)
        self.image_delegate.paint(painter, image_option, index)
        name = asset_name(item) if item is not None else ''
        metrics = option.fontMetrics
        painter.setFont(option.font)
        painter.setPen(QtGui.QColor('#e6e6e6'))
        name_rect = QtCore.QRect(rect.left()+3, rect.bottom()-38, rect.width()-6, 19)
        painter.drawText(name_rect, QtCore.Qt.AlignmentFlag.AlignCenter,
                         metrics.elidedText(name, QtCore.Qt.TextElideMode.ElideMiddle, name_rect.width()))
        size = getattr(item, '_image_size', None)
        dimensions = (f'{int(size.width())} × {int(size.height())}'
                      if size is not None and not size.isEmpty() else '')
        painter.setPen(QtGui.QColor('#999999'))
        painter.drawText(name_rect.translated(0, 18), QtCore.Qt.AlignmentFlag.AlignCenter, dimensions)
        painter.restore()
