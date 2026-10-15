"""Virtualised project asset browser alongside the original canvas editor."""
import os
from datetime import datetime, timezone
from prism.performance_trace import measured, record as trace_record

from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism.widgets.components.icons import icon, icon_button
from prism.i18n import _
from prism.item_lifecycle import is_deleted

def asset_name(item):
    if isinstance(item, dict):
        return item['title']
    return getattr(item, "_title", "") or os.path.basename(getattr(item, "filename", "") or "") or "未命名素材"


class AssetModel(QtCore.QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.items = []

    def rowCount(self, parent=QtCore.QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index, role=QtCore.Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.items):
            return None
        item = self.items[index.row()]
        if role == QtCore.Qt.ItemDataRole.UserRole:
            return item
        if role in (QtCore.Qt.ItemDataRole.DisplayRole, QtCore.Qt.ItemDataRole.ToolTipRole):
            return asset_name(item)

    def replace(self, items):
        if items == self.items:
            if items:
                self.dataChanged.emit(self.index(0), self.index(len(items)-1))
            return
        self.beginResetModel()
        self.items = items
        self.endResetModel()


class AssetDelegate(QtWidgets.QStyledItemDelegate):
    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        item = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if item is None or (not isinstance(item, dict) and is_deleted(item)):
            return
        painter.save()
        canvas = self.parent()
        if getattr(canvas, 'colour_group', None) and getattr(item, 'color_group', None) != canvas.colour_group:
            painter.setOpacity(.2)
        painter.setClipRect(option.rect)
        image_rect = option.rect
        from prism import lod
        lod.draw_image(painter, item, QtCore.QRectF(image_rect), canvas,
                       grayscale=getattr(canvas, 'display_grayscale', False))
        painter.restore()



class AssetBrowser(QtWidgets.QWidget):
    def __init__(self, window):
        super().__init__()
        self.window_host = window
        self.view = window.view
        self.catalog = self.view.category_panel.catalog
        self.mode = 'all'
        self.history = []
        self.history_index = -1
        self.sort_key = 'default'
        self.card_size = 156
        self.compact = False
        self.scope_pages = None
        self.saved_rule = None
        self._selecting = False
        self.setObjectName('assetBrowser')
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        toolbar = QtWidgets.QFrame()
        self.toolbar = toolbar
        toolbar.setObjectName('assetBrowserToolbar')
        toolbar.installEventFilter(self)
        toolbar.setFixedHeight(40)
        row = QtWidgets.QHBoxLayout(toolbar)
        row.setContentsMargins(8, 0, 10, 0)
        row.setSpacing(5)
        self.back = icon_button('chevron-left', '后退', size=26)
        self.forward = icon_button('chevron-right', '前进', size=26)
        self.back.clicked.connect(lambda: self.travel(-1))
        self.forward.clicked.connect(lambda: self.travel(1))
        row.addWidget(self.back)
        row.addWidget(self.forward)
        self.breadcrumb = QtWidgets.QLabel(_('All assets'))
        row.addWidget(self.breadcrumb)
        row.addStretch()
        zoom = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        zoom.setRange(96, 280)
        zoom.setValue(156)
        zoom.setFixedWidth(120)
        zoom.setToolTip('缩略图大小')
        zoom.valueChanged.connect(self.resize_cards)
        row.addWidget(zoom)
        row.addStretch()
        self.sort_button = icon_button('layout', '排列与排序', size=28)
        self.sort_button.clicked.connect(self.sort_menu)
        row.addWidget(self.sort_button)
        filters = icon_button('filter', '筛选素材', size=28)
        filters.clicked.connect(self.filter_menu)
        row.addWidget(filters)
        quick = icon_button('star', '批量评分 / 标签', size=28)
        quick.clicked.connect(self.quick_actions)
        row.addWidget(quick)
        self.search_type = self.view.category_panel.make_search_type_combo()
        row.addWidget(self.search_type)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText(_('Search names and content'))
        self.search.setClearButtonEnabled(True)
        self.search.addAction(icon('search'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.search.setMaximumWidth(260)
        self.search.textChanged.connect(self.schedule)
        row.addWidget(self.search)
        outer.addWidget(toolbar)
        from prism.widgets.asset_canvas import AssetCanvas
        self.list = AssetCanvas(self)
        self.list.setObjectName('assetCanvas')
        self.list.setContextMenuPolicy(QtCore.Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self.context_menu)
        self.list.installEventFilter(self)
        self.model = AssetModel(self)
        self.list.setModel(self.model)
        self.list.setItemDelegate(AssetDelegate(self.list))
        self.list.selectionModel().selectionChanged.connect(self.select_assets)
        self.list.doubleClicked.connect(self.locate)
        self.resize_cards(156)
        outer.addWidget(self.list, 1)
        self.empty = QtWidgets.QLabel(_('No matching results'))
        self.empty.setObjectName('assetEmpty')
        self.empty.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty, 1)
        self.empty.hide()
        self.summary = QtWidgets.QLabel()
        self.summary.setObjectName('assetBrowserSummary')
        outer.addWidget(self.summary)
        self.summary.hide()
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.refresh)
        self.view.scene.assets_changed.connect(self.schedule)
        self.view.scene.metadata_changed.connect(self.schedule)
        from prism.lod import cache
        cache().changed.connect(self._display_ready)

    @QtCore.pyqtSlot(object)
    def _display_ready(self, item):
        if self.isVisible() and self.list.needs_display(item):
            self.list.request_display_refresh()

    def schedule(self, *_args):
        if self.isVisible() and not self._selecting and not self.timer.isActive():
            self.timer.start()

    def showEvent(self, event):
        super().showEvent(event)
        if not getattr(self, '_opening', False):
            self.refresh()

    def hideEvent(self, event):
        self.list.clear_annotations()
        super().hideEvent(event)

    def resize_cards(self, value):
        self.card_size = value
        self.list.setGridSize(QtCore.QSize(value, int(value*(.40 if self.compact else .72))+8))

    @measured('assets.open', 0)
    def open(self, mode='all', scope_pages=None, title=None, record=True):
        trace_record('asset_scope', mode=mode, scope_pages=scope_pages, title=title)
        self.list.clear_annotations()
        self.mode = mode
        self.sort_button.setEnabled(mode != 'visual')
        if mode == 'random':
            import random
            self._random_seed = random.randrange(2**32)
        self.scope_pages = scope_pages
        self.title = title or {'all':_('All assets'), 'untagged':'未标签', 'unclassified':'未分类',
                               'recent':'最近使用', 'random':'随机模式'}.get(mode, '素材')
        if record or not self.history:
            self.history = self.history[:self.history_index+1] + [(mode, scope_pages, self.title, dict(self.saved_rule) if self.saved_rule else None, self.search.text())]
            self.history_index = len(self.history)-1
        self._opening = True
        try:
            self.window_host.tabs.setCurrentWidget(self)
        finally:
            self._opening = False
        self.list.selectionModel().clearSelection()
        self.view.scene.clearSelection()
        self.window_host.filter_bar.hide()
        previous = self.model.items
        self.refresh()
        if self.model.items is previous and not getattr(self.view, '_project_opening', False):
            self.list.arrange()
        self.window_host.canvas_toolbar.refresh()
        self.view.category_panel._update_navigation()
        self.view.category_panel.resource_tree.show()
        self.view.category_panel.search_results.hide()

    def travel(self, offset):
        target = self.history_index + offset
        if 0 <= target < len(self.history):
            self.history_index = target
            mode, scope, title, rule, query = self.history[target]
            self.saved_rule = rule
            self.search.setText(query)
            self.open(mode, scope, title, record=False)

    def refresh(self):
        if getattr(self.view, '_project_opening', False):
            self.timer.start()
            return
        self.catalog = self.view.category_panel.catalog
        text, expression = self.view.category_panel._split_query(self.search.text())
        kind = 'asset' if self.mode in ('all', 'unclassified', 'untagged') else self.view.category_panel.search_kind
        unified = self.mode != 'visual' and (bool(self.search.text().strip()) or kind not in ('all', 'asset'))
        results = self.catalog.query(text='' if unified else text, tag_expression=expression,
            untagged_only=self.mode == 'untagged', min_rating=self.view.category_panel._min_rating,
            canvas_scope=self.scope_pages)
        from prism import tags
        system = tags.tag_system(self.view.scene)
        active_tags = self.view.category_panel._active_tags
        items = [r.item for r in results if active_tags.issubset(tags.name_index(r.tags, system))]
        if self.mode == 'unclassified':
            known = self.catalog._known_canvas_ids()
            items = [i for i in items if self.catalog.is_unclassified(i, known)]
        if self.mode == 'recent':
            items = sorted([i for i in items if getattr(i, '_last_used', '')],
                           key=lambda i: i._last_used, reverse=True)
        if self.mode == 'random':
            import random
            random.Random(getattr(self, '_random_seed', 0)).shuffle(items)
        if self.saved_rule:
            matches = self.catalog.query(text=self.saved_rule.get('text', ''),
                                         tag_expression=self.saved_rule.get('tag_expression', ''),
                                         min_rating=int(self.saved_rule.get('rating', 0)))
            allowed = {r.item for r in matches}
            items = [i for i in items if i in allowed]
        if self.scope_pages is not None:
            items = [i for i in items if (getattr(i, '_canvas_id', None) or 'default-canvas') in self.scope_pages]
        self.search_hits = []
        if unified:
            hits = [r for r in self.view.category_panel.project_search_results(self.search.text())
                    if r['kind'] not in ('folder', 'tag')]
            allowed = set(items)
            asset_scope = self.mode != 'all' or self.saved_rule or self.scope_pages is not None
            self.search_hits = [r for r in hits if
                                (kind != 'asset' or r['kind'] == 'asset') and
                                (r.get('item') in allowed if r['kind'] == 'asset' else
                                 (not asset_scope or kind in ('document', 'mindmap', 'canvas')))]
            items = [r['item'] if r['kind'] == 'asset' else r for r in self.search_hits]
        if self.mode == 'visual':
            allowed = set(items)
            items = [item for item, score in getattr(self, 'visual_results', []) if item in allowed]
            if getattr(self, 'visual_project', None) != self.view.scene.project_id:
                items = []
        elif self.sort_key == 'name': items.sort(key=lambda i: asset_name(i).casefold())
        elif self.sort_key == 'rating': items.sort(key=lambda i: getattr(i, '_rating', 0), reverse=True)
        self._selecting = True
        if items != self.model.items:
            self.view.scene.clearSelection()
        self.model.replace(items)
        self._selecting = False
        self.back.setEnabled(self.history_index > 0)
        self.forward.setEnabled(self.history_index < len(self.history)-1)
        self.breadcrumb.setText(f'{getattr(self, "title", _("All assets"))} / 搜索结果 ({len(items)})' if self.search.text() else getattr(self, 'title', _('All assets')))
        self.summary.setText(f'{len(items):,} 个素材 · 滚轮缩放 · 中键平移 · 双击定位到画布')
        self.empty.setVisible(not items)
        self.list.setVisible(bool(items))
        if not self.view.scene.selectedItems(user_only=True):
            self.show_scope_info(items)
        if hasattr(self.window_host, '_status_text'):
            self.window_host._update_status({})

    def show_scope_info(self, items):
        panel = self.view._detail_panel
        panel.show_collection(getattr(self, 'title', _('All assets')), len(items), self.storage_bytes(items))

    def storage_bytes(self, items):
        from prism.source_store import storage_bytes
        if not hasattr(self, '_storage_cache'):
            self._storage_cache = {}
        return storage_bytes(items, self._storage_cache)

    def select_assets(self, *_args):
        if self._selecting:
            return
        self._selecting = True
        blocker = QtCore.QSignalBlocker(self.view.scene)
        self.view.scene.clearSelection()
        selected = [index.data(QtCore.Qt.ItemDataRole.UserRole) for index in self.list.selectedIndexes()]
        for item in selected:
            if isinstance(item, dict):
                continue
            item.setVisible(True)
            item.setSelected(True)
            item._last_used = datetime.now(timezone.utc).isoformat()
        del blocker
        self.view.scene.selectionChanged.emit()
        self.view._detail_panel._on_selection_changed()
        if not selected:
            self.show_scope_info(self.model.items)
        self._selecting = False

    def locate(self, index):
        item = index.data(QtCore.Qt.ItemDataRole.UserRole)
        if isinstance(item, dict):
            self.view.category_panel.activate_project_result(item)
            return
        if item is None or is_deleted(item):
            return
        window = self.window_host
        kind, page_id = self.catalog.locate(item)
        window.open_workspace_page(kind, page_id)
        self.view.scene.clearSelection()
        item.setVisible(True)
        item.setSelected(True)
        self.view.centerOn(item)

    def sort_menu(self):
        menu = QtWidgets.QMenu(self)
        for compact, title in [(False, '标准缩略图'), (True, '紧凑缩略图')]:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.compact == compact)
            action.triggered.connect(lambda checked=False, v=compact: self.set_layout(v))
        menu.addSeparator()
        for key, title in [('default','默认顺序'),('name','名称'),('rating','评分')]:
            action = menu.addAction(title)
            action.setCheckable(True)
            action.setChecked(self.sort_key == key)
            action.triggered.connect(lambda checked=False, value=key: self.set_sort(value))
        menu.exec(self.sort_button.mapToGlobal(QtCore.QPoint(0, 28)))

    def set_layout(self, compact):
        self.compact = compact
        self.resize_cards(self.card_size)

    def set_sort(self, key):
        self.sort_key = key
        self.refresh()

    def filter_menu(self):
        menu = QtWidgets.QMenu(self)
        menu.addAction('清除筛选', self.view.category_panel.clear_filter)
        rating = menu.addMenu('最低评分')
        for value in range(6):
            action = rating.addAction('不限' if not value else '★'*value)
            action.triggered.connect(lambda checked=False, v=value: self.set_rating(v))
        tags_menu = menu.addMenu('标签')
        for tag in sorted(self.view.scene.get_all_tags()):
            action = tags_menu.addAction(tag)
            action.setCheckable(True)
            action.setChecked(tag in self.view.category_panel._active_tags)
            action.toggled.connect(lambda checked, t=tag: self.set_tag(t, checked))
        menu.exec(QtGui.QCursor.pos())

    def quick_actions(self):
        menu = QtWidgets.QMenu(self)
        selected = self.view.scene.selectedItems(user_only=True)
        if not selected:
            action = menu.addAction('请先选择素材')
            action.setEnabled(False)
        else:
            self.view.category_panel.build_tag_menu(menu.addMenu('标签'))
            rating = menu.addMenu('评分')
            for value in range(6):
                action = rating.addAction('清除评分' if not value else '★' * value)
                action.triggered.connect(lambda checked=False, v=value: self.view._detail_panel._set_rating(v))
            menu.addAction('移到回收站', self.view.on_action_delete_items)
        menu.exec(QtGui.QCursor.pos())

    def set_rating(self, value):
        self.view.category_panel.rating_filter.setCurrentIndex(value)
        self.refresh()

    def set_tag(self, tag, checked):
        own = self.view.category_panel._active_tags
        own.add(tag) if checked else own.discard(tag)
        self.refresh()

    def context_menu(self, pos):
        index = self.list.indexAt(pos)
        if index.isValid() and index not in self.list.selectedIndexes():
            self.list.selectionModel().select(index, QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
        self.view._remove_dynamic_menu_items()
        self.view._inject_canvas_tools()
        self.view._inject_category_menu()
        self.view.context_menu.exec(self.list.viewport().mapToGlobal(pos))

    def eventFilter(self, watched, event):
        if watched is self.toolbar:
            if event.type() == QtCore.QEvent.Type.MouseButtonDblClick and event.button() == QtCore.Qt.MouseButton.LeftButton:
                window = self.window_host
                window.showNormal() if window.isMaximized() else window.showMaximized()
                return True
            if event.type() == QtCore.QEvent.Type.MouseButtonPress and event.button() == QtCore.Qt.MouseButton.LeftButton:
                handle = self.window_host.windowHandle()
                if handle:
                    handle.startSystemMove()
                return True
        if watched is getattr(self, 'list', None) and event.type() == QtCore.QEvent.Type.KeyPress and event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.locate(self.list.currentIndex())
            return True
        if watched is getattr(self, 'list', None) and event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Delete:
            self.view.on_action_delete_items()
            self.refresh()
            return True
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Delete:
            self.view.on_action_delete_items()
            self.refresh()
        elif event.key() in (QtCore.Qt.Key.Key_Return, QtCore.Qt.Key.Key_Enter):
            self.locate(self.list.currentIndex())
        else:
            super().keyPressEvent(event)
