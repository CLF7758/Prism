"""Window-owned project search, independent of editor and asset selections."""
import html
import time
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism import tags
from prism.i18n import _
from prism.project_search import search_project, item_key, searchable_items
from prism.widgets.search_canvas import SearchCanvas, result_kind
from prism.item_lifecycle import is_deleted


class SearchInput(QtWidgets.QLineEdit):
    composition_finished = QtCore.pyqtSignal()
    composing = False

    def inputMethodEvent(self, event):
        self.composing = bool(event.preeditString())
        super().inputMethodEvent(event)
        if not self.composing:
            self.composition_finished.emit()


class SearchController(QtCore.QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.view = window, window.view
        self.library = self.view.category_panel
        self._asset_index = None
        self._asset_index_key = None
        self._deleted_asset_pages = set()
        self.library.resource_nodes_changed.connect(self._invalidate_asset_index)
        self.view.undo_stack.cleanChanged.connect(self._invalidate_asset_index)
        self.generation = 0
        self.active = False
        self.origin = None
        self.project_id = self.view.scene.project_id
        self.scope = ('project', None)
        self.min_rating, self.color, self.active_tags = 0, None, set()
        self.opened = None
        self.match_index = 0
        self.located = None
        self.results = []
        self.snapshot_pending = None
        self.wait_key = None
        self.page = SearchCanvas(self.resolve_asset, preview_text=self.preview_text)
        window.tabs.addTab(self.page, _('Search results'))
        self.page.selected.connect(self.show_detail)
        self.page.open_result.connect(self.open_result)
        self.page.type_requested.connect(self.library.set_search_kind)
        self.page.suggestion_requested.connect(self.choose_suggestion)
        self.header = QtWidgets.QWidget()
        self.header.setObjectName('projectSearchHeader')
        outer = QtWidgets.QVBoxLayout(self.header)
        outer.setContentsMargins(0, 0, 0, 4)
        outer.setSpacing(4)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        outer.addLayout(row)
        self.scope_combo = QtWidgets.QComboBox()
        self.scope_combo.setMaximumWidth(165)
        self.scope_combo.addItem(_('Entire project'), ('project', None))
        self.scope_combo.addItem(_('Current page'), ('current', None))
        self.scope_combo.activated.connect(self.scope_changed)
        self.scope_combo.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        # Move the already approved selector out of the page-specific toolbar.
        self.type_combo = window.asset_browser.search_type
        window.asset_browser.toolbar.layout().removeWidget(self.type_combo)
        row.addWidget(self.type_combo)
        old_search = window.asset_browser.search
        old_search.hide()
        old_search.textChanged.disconnect(window.asset_browser.schedule)
        self.input = SearchInput()
        self.input.setPlaceholderText(_('Search assets, document text, mind map nodes and canvas content'))
        self.input.setClearButtonEnabled(True)
        self.input.addAction(window.asset_browser.search.actions()[0].icon(), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.input.setToolTip(self.library.search.toolTip())
        self.input.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        row.addWidget(self.input, 1)
        self.input.textChanged.connect(self.schedule)
        self.input.composition_finished.connect(self.schedule)
        self.input.returnPressed.connect(self.submit)
        # Keep the browser's old public API usable without another visible input.
        window.asset_browser.search = self.input
        self.filter_button = QtWidgets.QPushButton(_('Search filters'))
        self.filter_button.clicked.connect(self.filter_menu)
        options = QtWidgets.QHBoxLayout()
        options.setSpacing(4)
        options.addWidget(self.scope_combo, 1)
        options.addWidget(self.filter_button)
        outer.addLayout(options)
        self.scope_combo.hide()
        self.filter_button.hide()
        self.info = QtWidgets.QLabel()
        self.info.setMaximumWidth(180)
        outer.addWidget(self.info)
        self.info.hide()
        navigation = QtWidgets.QHBoxLayout()
        navigation.setSpacing(4)
        outer.addLayout(navigation)
        self.back_button = QtWidgets.QPushButton(_('Back to results'))
        self.back_button.clicked.connect(self.return_to_results)
        navigation.addWidget(self.back_button)
        self.exit_button = QtWidgets.QPushButton(_('Exit search'))
        self.exit_button.clicked.connect(self.exit)
        self.exit_button.setParent(self.header)
        self.exit_button.hide()
        self.back_button.hide()
        self.exit_button.hide()
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(180)
        self.timer.timeout.connect(self.submit)
        self.library.search.hide()
        self.library.search_type.hide()
        self.library.search_results.hide()
        self.match_bar = QtWidgets.QWidget()
        match_row = QtWidgets.QHBoxLayout(self.match_bar)
        self.match_label = QtWidgets.QLabel()
        match_row.addWidget(self.match_label)
        for label, delta in [('Previous match', -1), ('Next match', 1)]:
            button = QtWidgets.QPushButton(_(label))
            button.clicked.connect(lambda checked=False, d=delta: self.navigate_match(d))
            match_row.addWidget(button)
        self.match_bar.hide()
        self.shortcuts = []
        for key, callback in [('Ctrl+F', self.focus), ('Ctrl+Alt+F', self.focus_global)]:
            shortcut = QtGui.QShortcut(QtGui.QKeySequence(key), window)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self._detail_actions = None
        from prism.visual_search_controller import VisualSearchController
        self.visual_search = VisualSearchController(self)

    def focus(self):
        self.input.setFocus()
        self.input.selectAll()

    def focus_global(self):
        self.set_scope('project', None, _('Entire project'))
        self.focus()

    def schedule(self, *_args):
        visual = getattr(self, 'visual_search', None)
        if visual is not None and visual.active:
            visual.schedule()
            return
        self.generation += 1
        if not self.input.composing:
            self.timer.start()

    def content_changed(self):
        if self.active and self.snapshot_pending is None:
            self.schedule()

    def scope_changed(self, index):
        kind, value = self.scope_combo.itemData(index)
        if kind == 'current':
            value = (self.window.current_page_kind, self.window.current_page_id)
            kind = 'page'
        self.set_scope(kind, value, self.scope_combo.itemText(index))

    def set_scope(self, kind, value, title):
        self.scope = (kind, value)
        data = self.scope
        blocker = QtCore.QSignalBlocker(self.scope_combo)
        index = self.scope_combo.findData(data)
        if index < 0:
            self.scope_combo.addItem(title, data)
            index = self.scope_combo.count()-1
        self.scope_combo.setCurrentIndex(index)
        del blocker
        self.schedule()

    def choose_suggestion(self, result):
        if result['kind'] == 'folder':
            self.set_scope('folder', result['node_id'], result['path'])
        elif result['kind'] == 'tag':
            self.active_tags = {result['tag']}
            self.schedule()

    def submit(self):
        visual = getattr(self, 'visual_search', None)
        if visual is not None and visual.active:
            visual.submit()
            return
        self.timer.stop()
        if self.input.composing:
            return
        if self.view.scene.project_id != self.project_id:
            self.reset_project()
            return
        if not self.input.text().strip():
            if self.active:
                self.exit(clear=False)
            return
        self.generation += 1
        token, project = self.generation, self.project_id
        current = self.window.tabs.currentWidget()
        wait_key = (self.input.text(), self.scope, self.library.search_kind, self.project_id)
        if wait_key != self.wait_key:
            self.wait_key, self.wait_until = wait_key, time.monotonic()+3
        self.snapshot_pending = token
        self.page.status.setText(_('Updating current page content'))
        self.info.setText(_('Updating current page content'))
        self.info.show()

        def ready(ok=True):
            if self.snapshot_pending == token:
                self.snapshot_pending = None
            if token != self.generation or project != self.view.scene.project_id:
                return
            if not ok:
                self.page.status.setText(_('Current page content is still updating; try again shortly'))
                self.info.setText(_('Current page content is still updating; try again shortly'))
                if time.monotonic() < self.wait_until:
                    QtCore.QTimer.singleShot(120, lambda: self.submit() if token == self.generation else None)
                return
            self.wait_key = None
            self._run_query()
        if hasattr(current, 'search_snapshot'):
            current.search_snapshot(ready)
        else:
            ready()

    def _capture_origin(self):
        current = self.window.tabs.currentWidget()
        self.origin = dict(widget=current, project_id=self.project_id,
                           kind=self.window.current_page_kind, page_id=self.window.current_page_id,
                           transform=QtGui.QTransform(self.view.transform()),
                           center=self.view.mapToScene(self.view.viewport().rect().center()),
                           selection=[item_key(i) for i in self.view.scene.selectedItems(user_only=True)],
                           tree=self.library._capture_tree_state(), editor_state=None)
        origin = self.origin
        if hasattr(current, 'capture_search_view'):
            def captured(state):
                if self.origin is origin and origin['project_id'] == self.view.scene.project_id:
                    origin['editor_state'] = state
            current.capture_search_view(captured)

    def _run_query(self):
        if not self.active:
            self.scope = ('project', None)
            self.scope_combo.setCurrentIndex(0)
            self._capture_origin()
            self.active = True
        hits = self._query_hits()
        suggestions = [r for r in hits if r['kind'] in ('folder', 'tag')]
        hits = [r for r in hits if r['kind'] not in ('folder', 'tag') and self.in_scope(r)]
        if self.active_tags:
            system = tags.tag_system(self.view.scene)
            hits = [r for r in hits if self.active_tags.issubset(tags.name_index(r.get('tags', []), system))]
        if self.min_rating or self.color:
            hits = [r for r in hits if result_kind(r) == 'asset' and
                    getattr(r['item'], '_rating', 0) >= self.min_rating and
                    (self.color is None or getattr(r['item'], 'color_group', None) == self.color)]
        counts = {k: sum(result_kind(r) == k for r in hits) for k in ('asset', 'document', 'mindmap', 'canvas')}
        counts['all'] = len(hits)
        kind = self.library.search_kind
        hits = [r for r in hits if kind == 'all' or result_kind(r) == kind]
        self.results = [dict((k, v) for k, v in r.items() if k != 'item') |
                        {'project_id': self.project_id, 'query': self.input.text()} for r in hits]
        key = (self.input.text(), self.scope, kind, self.min_rating, self.color, tuple(sorted(self.active_tags)))
        if self.opened and key != self.page.query_key:
            self._release_location()
            self.opened = None
            self.back_button.hide()
            self.match_bar.hide()
        self.page.update_results(self.results, key, counts, kind, suggestions)
        self.info.clear()
        self.info.hide()
        self.page.status.setText(self.page.status.text() + ' · ' +
                                 (_('Entire project') if self.scope[0] == 'project' else self.scope_combo.currentText()) +
                                 self.filter_description())
        self.library.resource_tree.show()
        self.library.search_results.hide()
        self.exit_button.hide()
        if self.opened is None:
            self.window.tabs.setCurrentWidget(self.page)

    def _query_hits(self):
        panels = list(getattr(self.window, '_page_panels', {}).values())
        panels += [self.window.document_panel, self.window.mindmap_panel]
        text, expression = self.library._split_query(self.input.text())
        return search_project(self.view.scene, self.library.resource_service, text, panels, expression,
                              self.library._pages_with_implicit_defaults())

    def in_scope(self, result):
        kind, value = self.scope
        if kind == 'project':
            return True
        if kind == 'page':
            return result.get('page_id') == value[1]
        if kind == 'folder':
            service = self.library.resource_service
            page_ids = {n.get('pageId') for i in service._descendants(value)
                        if (n := service._by_id(i)) and not n.get('deletedAt')}
            return result.get('page_id') in page_ids
        if kind == 'category':
            item = result.get('item')
            return item is not None and any(c == value or tags.is_descendant(c, value)
                                            for c in getattr(item, '_categories', []))
        if kind == 'navigation':
            if result['kind'] != 'asset':
                return False
            item = result['item']
            if value == 'unclassified':
                return self.library.catalog.is_unclassified(item)
            if value == 'untagged':
                return not tags.name_index(getattr(item, '_tags', []), tags.tag_system(self.view.scene))
            if value == 'recent':
                return bool(getattr(item, '_last_used', ''))
            return True
        return False

    def _invalidate_asset_index(self, *_):
        self._asset_index_key = None

    def resolve_asset(self, result):
        scene = self.view.scene
        if result.get('project_id', self.project_id) != scene.project_id:
            return None
        key = (scene.project_id, scene._asset_revision)
        if self._asset_index is None or self._asset_index_key != key:
            self._asset_index = {item_key(item): item for item in searchable_items(scene)}
            self._asset_index_key = key
            self._deleted_asset_pages = {n.get('pageId') for n in self.library.resource_service.nodes
                                        if n.get('deletedAt') and n.get('pageId')}
        item = self._asset_index.get(result.get('item_id'))
        if item is None or is_deleted(item) or item.scene() is not scene or getattr(item, '_deleted_at', None):
            return None
        if getattr(item, '_canvas_id', None) in self._deleted_asset_pages:
            return None
        return item

    def valid_result(self, result):
        if result.get('project_id') != self.view.scene.project_id:
            return False
        if result_kind(result) == 'asset':
            return self.resolve_asset(result) is not None
        page_id = result.get('page_id')
        return any(p['id'] == page_id for p in self.library._pages_with_implicit_defaults()) and not any(
            n.get('pageId') == page_id and n.get('deletedAt') for n in self.library.resource_service.nodes)

    def preview_text(self, result):
        from prism.project_search import plain_text, text_of_tree, asset_search_text
        kind = result_kind(result)
        if kind == 'asset':
            item = self.resolve_asset(result)
            return asset_search_text(item) if item is not None else _('Content changed; refresh results')
        page_id = result.get('page_id')
        panel = self.window._page_panels.get(page_id)
        if page_id == 'default-document':
            panel = self.window.document_panel
        elif page_id == 'default-mindmap':
            panel = self.window.mindmap_panel
        page = next((p for p in self.library._pages_with_implicit_defaults() if p['id'] == page_id), {})
        if kind == 'document':
            content = getattr(panel, '_html', None)
            return plain_text(content if content is not None else page.get('content') or self.view.scene.note_html)
        if kind == 'mindmap':
            content = getattr(panel, '_tree', None)
            return text_of_tree(content if content is not None else page.get('content') or self.view.scene.mindmap_tree)
        return result.get('snippet', '')

    def show_detail(self, result):
        panel = self.view._detail_panel
        if result is None:
            panel.show_collection(_('Search results'), len(self.results), None)
            return
        if result_kind(result) == 'asset':
            panel.show_search_asset(result)
            return
        panel.show_search_result(result, lambda: self.open_result(result),
                                 lambda: self.open_result(result | {'open_original': True}),
                                 item=self.resolve_asset(result) if result_kind(result) == 'asset' else None)

    def open_result(self, result):
        if not self.valid_result(result) or result.get('query', self.input.text()) != self.input.text():
            self.page.status.setText(_('Content changed; refresh results'))
            self.schedule()
            return
        fresh = next((r for r in self._query_hits() if r['result_id'] == result['result_id']), None)
        if fresh is None:
            self.page.status.setText(_('Content changed; refresh results'))
            self.schedule()
            return
        result = {k: v for k, v in fresh.items() if k != 'item'} | {
            'project_id': self.project_id, 'query': self.input.text(),
            'open_original': result.get('open_original', False)}
        self._release_location()
        self.opened = result | {'matches': [m for m in result.get('matches', [])
                               if m['field'] in ('body', 'node', 'canvas_item', 'asset')]}
        self.match_index = 0
        self.back_button.show()
        if result_kind(result) == 'asset':
            item = self.resolve_asset(result)
            kind, page_id = self.library.catalog.locate(item)
            self.window.open_workspace_page(kind, page_id)
            self._locate_item(item)
        else:
            self.window.open_workspace_page(result['page_kind'], result['page_id'])
            self.locate_match()
        self.match_bar.setVisible(bool(self.opened.get('matches')))

    def _locate_item(self, item):
        if item is None:
            return
        self.located = (item_key(item), item.isVisible(), getattr(item, '_keep_visible', False),
                        [item_key(i) for i in self.view.scene.selectedItems(user_only=True)])
        item._keep_visible = True
        item.setVisible(True)
        self.view.scene.clearSelection()
        item.setSelected(True)
        self.view.centerOn(item)

    def locate_match(self):
        result = self.opened
        if not result or not self.valid_result(result):
            return
        fresh = next((r for r in self._query_hits() if r['result_id'] == result['result_id']), None)
        if fresh is None:
            self.match_label.setText(_('Content changed; refresh results'))
            return
        matches = [m for m in fresh.get('matches', []) if m['field'] in ('body', 'node', 'canvas_item', 'asset')]
        self.opened['matches'] = matches
        result = self.opened
        if not matches:
            return
        match = matches[self.match_index % len(matches)]
        kind = result_kind(result)
        panel = self.window._page_panels.get(result.get('page_id'))
        if panel is None:
            panel = self.window.document_panel if kind == 'document' else self.window.mindmap_panel
        if kind == 'document' and match.get('field') == 'body':
            panel.locate_search_result(match)
        elif kind == 'mindmap' and match.get('field') == 'node':
            panel.locate_search_result({'path': match['node_path'], 'uid': match.get('node_uid')})
        elif kind == 'canvas' and match.get('item_id'):
            self._release_location()
            self._locate_item(self.resolve_asset(result | {'item_id': match['item_id']}))
        self.match_index %= len(matches)
        self.match_label.setText(_('{index} / {count} matches').format(index=self.match_index+1, count=len(matches)))

    def navigate_match(self, delta):
        if self.opened and self.opened.get('matches'):
            self.match_index = (self.match_index+delta) % len(self.opened['matches'])
            self.locate_match()

    def _release_location(self):
        if self.located:
            key, visible, keep, selected = self.located
            item = self.resolve_asset({'item_id': key})
            if item is not None:
                item._keep_visible = keep
                item.setVisible(visible)
                if self.view.scene.selectedItems(user_only=True) == [item]:
                    self.view.scene.clearSelection()
                    for selected_id in selected:
                        previous = self.resolve_asset({'item_id': selected_id})
                        if previous is not None:
                            previous.setSelected(True)
            self.located = None
        for panel in [*self.window._page_panels.values(), self.window.mindmap_panel]:
            if hasattr(panel, 'end_search_location'):
                panel.end_search_location()

    def return_to_results(self):
        self._release_location()
        self.opened = None
        self.match_bar.hide()
        self.back_button.hide()
        self.page.close_preview()
        self.window.tabs.setCurrentWidget(self.page)
        self.page.view.setFocus()
        self.show_detail(self.page.selected_result())

    def exit(self, clear=True):
        visual = getattr(self, 'visual_search', None)
        if visual is not None and visual.active:
            visual.exit()
            return
        self.generation += 1
        self.snapshot_pending = None
        self.timer.stop()
        self._release_location()
        self.active = False
        self.opened = None
        self.back_button.hide()
        self.exit_button.hide()
        self.match_bar.hide()
        self.page.close_preview()
        origin, self.origin = self.origin, None
        if clear:
            blocker = QtCore.QSignalBlocker(self.input)
            self.input.clear()
            del blocker
        if origin and origin['project_id'] == self.view.scene.project_id:
            widget = origin['widget']
            if not is_deleted(widget):
                if widget is self.window.asset_browser:
                    self.window.tabs.setCurrentWidget(widget)
                elif origin['page_id'] and any(p['id'] == origin['page_id'] for p in self.library._pages_with_implicit_defaults()):
                    self.window.open_workspace_page(origin['kind'], origin['page_id'])
                else:
                    self.window.tabs.setCurrentWidget(widget)
                if hasattr(widget, 'restore_search_view') and origin['editor_state'] is not None:
                    widget.restore_search_view(origin['editor_state'])
            self.view.setTransform(origin['transform'])
            self.view.centerOn(origin['center'])
            self.view.scene.clearSelection()
            for key in origin['selection']:
                item = self.resolve_asset({'item_id': key})
                if item is not None:
                    item.setSelected(True)
            self.library._restore_tree_state(origin['tree'])

    def reset_project(self):
        visual = getattr(self, 'visual_search', None)
        if visual is not None and visual.active:
            visual.cancel()
            visual.active = False
            visual.clear_action.setVisible(False)
            self.input.setPlaceholderText(visual.previous_placeholder)
            self.type_combo.setEnabled(True)
            self.info.hide()
            self.scope_combo.hide()
            self.filter_button.hide()
            self.window.asset_browser.visual_results = []
        self.generation += 1
        self.snapshot_pending = None
        self.timer.stop()
        self.origin = None
        self.located = None
        self.active, self.opened = False, None
        self.project_id = self.view.scene.project_id
        self.results = []
        self.page.view.scene().clear()
        self.page.cards.clear()
        self.page.close_preview()
        blocker = QtCore.QSignalBlocker(self.input)
        self.input.clear()
        del blocker
        self.scope = ('project', None)
        self.scope_combo.setCurrentIndex(0)
        self.min_rating, self.color, self.active_tags = 0, None, set()
        self.library.search_kind = 'all'
        blocker = QtCore.QSignalBlocker(self.type_combo)
        self.type_combo.setCurrentIndex(0)
        del blocker
        self.back_button.hide()
        self.exit_button.hide()
        self.match_bar.hide()

    def set_asset_filter(self, rating=0, color=None):
        self.min_rating, self.color = rating, color
        if rating or color:
            self.library.set_search_kind('asset')
        self.schedule()

    def type_changed(self, kind):
        if kind != 'asset':
            self.min_rating, self.color = 0, None
        if self.active or self.input.text().strip():
            self.schedule()

    def filter_description(self):
        parts = sorted(self.active_tags)
        if self.min_rating:
            parts.append('★'*self.min_rating + '+')
        if self.color:
            parts.append(str(self.color))
        return (' · ' + ' · '.join(parts)) if parts else ''

    def filter_menu(self):
        menu = QtWidgets.QMenu(self.window)
        menu.addAction(_('Clear search filters'), self.clear_filters)
        rating_menu = menu.addMenu(_('Minimum rating (assets)'))
        for value in range(6):
            rating_menu.addAction(_('Any rating') if not value else '★'*value,
                                  lambda v=value: self.set_asset_filter(v, self.color))
        from prism.items import COLOR_GROUPS
        colors = menu.addMenu(_('Color (assets)'))
        for group in COLOR_GROUPS:
            colors.addAction(_(group['label']), lambda c=group['id']: self.set_asset_filter(self.min_rating, c))
        tag_menu = menu.addMenu(_('Tags'))
        for name in sorted(tags.scene_tag_names(self.view.scene)):
            action = tag_menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in self.active_tags)
            action.toggled.connect(lambda checked, n=name: self.set_tag(n, checked))
        menu.exec(self.filter_button.mapToGlobal(QtCore.QPoint(0, self.filter_button.height())))

    def set_tag(self, name, checked):
        self.active_tags.add(name) if checked else self.active_tags.discard(name)
        self.schedule()

    def clear_filters(self):
        self.min_rating, self.color, self.active_tags = 0, None, set()
        self.schedule()
