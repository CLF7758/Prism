"""Image-query UI lives in the existing project search input and asset canvas."""
from pathlib import Path
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.project_search import item_key
from prism.visual_search_service import VisualSearchService, source_snapshot
from prism.widgets.components.icons import icon


class VisualSearchController(QtCore.QObject):
    def __init__(self, search):
        super().__init__(search)
        self.search = search
        self.window, self.view = search.window, search.view
        self.active = False
        self.source = None
        self.source_project = None
        self.request = None
        self.dirty = False
        self.service = VisualSearchService(self)
        self.service.progress.connect(self.progress)
        self.service.results.connect(self.finished)
        self.service.failed.connect(self.failed)
        self.service.preview.connect(self.preview)
        self.action = search.input.addAction(icon('image-search'), QtWidgets.QLineEdit.ActionPosition.TrailingPosition)
        self.action.setText('以图搜图')
        self.action.setToolTip('以图搜图：在本地素材库中查找相似图片')
        self.action.triggered.connect(self.menu)
        self.clear_action = search.input.addAction(icon('close'), QtWidgets.QLineEdit.ActionPosition.LeadingPosition)
        self.clear_action.setToolTip('退出以图搜图')
        self.clear_action.triggered.connect(self.exit)
        self.clear_action.setVisible(False)
        search.input.setAcceptDrops(True)
        search.input.installEventFilter(self)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(200)
        self.timer.timeout.connect(self.submit)
        self.view.scene.assets_changed.connect(self.schedule)
        self.view.scene.metadata_changed.connect(self.schedule)

    def menu(self):
        menu = QtWidgets.QMenu(self.search.input)
        menu.addAction('选择图片…', self.choose)
        paste = menu.addAction('粘贴剪贴板图片', self.paste)
        paste.setEnabled(QtWidgets.QApplication.clipboard().mimeData().hasImage())
        if self.active:
            menu.addSeparator()
            menu.addAction('取消当前检索', self.cancel)
            menu.addAction('退出以图搜图', self.exit)
        menu.exec(self.search.input.mapToGlobal(self.search.input.rect().bottomRight()))

    def choose(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self.window, '选择查询图片', '', '图片 (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff)')
        if path:
            self.start(path)

    def paste(self):
        image = QtWidgets.QApplication.clipboard().image()
        if not image.isNull():
            self.start(image)

    def eventFilter(self, watched, event):
        if event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.Drop):
            mime = event.mimeData()
            path = next((url.toLocalFile() for url in mime.urls() if url.isLocalFile()), None) if mime.hasUrls() else None
            accepted = mime.hasImage() or (path and Path(path).suffix.lower() in ('.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'))
            if accepted:
                if event.type() == QtCore.QEvent.Type.Drop:
                    image = mime.imageData()
                    self.start(path or (image.toImage() if isinstance(image, QtGui.QPixmap) else image))
                event.acceptProposedAction()
                return True
        return False

    def start(self, source):
        if self.search.active:
            self.search.exit()
        if not self.active:
            self.previous_text = self.search.input.text()
            self.previous_placeholder = self.search.input.placeholderText()
            self.previous_tab = self.window.tabs.currentWidget()
            browser = self.window.asset_browser
            self.previous_browser = (browser.mode, browser.scope_pages, getattr(browser, 'title', '素材'))
        self.cancel()
        self.search.timer.stop()
        self.active = True
        self.source = source_snapshot(source) if getattr(source, 'TYPE', None) == 'pixmap' else source
        self.source_project = self.view.scene.project_id
        self.previous_page = self.view.current_canvas_id
        blocker = QtCore.QSignalBlocker(self.search.input)
        self.search.input.clear()
        del blocker
        self.search.input.setPlaceholderText('以图搜图：继续输入名称或标签筛选')
        self.clear_action.setVisible(True)
        self.search.scope_combo.show()
        self.search.filter_button.show()
        self.search.info.show()
        self.search.info.setText('准备本地以图搜图…')
        self.search.scope = ('project', None)
        blocker = QtCore.QSignalBlocker(self.search.scope_combo)
        self.search.scope_combo.setCurrentIndex(0)
        del blocker
        self.search.type_combo.setEnabled(False)
        self.schedule()

    def schedule(self, *_):
        if self.active:
            self.dirty = True
            self.timer.start()

    def candidates(self):
        text, expression = self.search.library._split_query(self.search.input.text())
        results = self.search.library.catalog.query(text=text, tag_expression=expression,
            min_rating=max(self.search.library._min_rating, self.search.min_rating))
        from prism import tags
        active_tags = self.search.library._active_tags | self.search.active_tags
        system = tags.tag_system(self.view.scene)
        return [row.item for row in results if getattr(row.item, 'TYPE', '') == 'pixmap'
                and active_tags.issubset(tags.name_index(row.tags, system))
                and (not self.search.color or row.item.color_group == self.search.color)
                and self.search.in_scope(dict(kind='asset', item=row.item,
                    page_id=getattr(row.item, '_canvas_id', None) or 'default-canvas'))]

    def submit(self):
        if not self.active:
            return
        if self.source_project != self.view.scene.project_id:
            self.exit()
            return
        if self.service.active:
            return  # A single latest rescan replaces repeated filter changes.
        self.dirty = False
        items = self.candidates()
        self.item_map = {item_key(item): item for item in items}
        try:
            self.request = self.service.search(self.source, [(item_key(item), source_snapshot(item)) for item in items])
            self.search.info.setText('本地检索中…')
        except (OSError, ValueError, RuntimeError) as error:
            self.failed(str(error))

    def progress(self, value):
        if not self.active:
            return
        if value.get('phase') == 'index':
            self.search.info.setText(f'建立索引 {value["done"]}/{value["total"]}')
        else:
            self.search.info.setText(value.get('message', '正在本地检索…'))
        self.search.info.setToolTip(value.get('message', ''))

    def preview(self, image):
        if self.active:
            self.clear_action.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(image)))
            self.clear_action.setToolTip('查询图片：点击移除图像条件')

    def finished(self, identifier, value):
        if not self.active or identifier != self.request or self.source_project != self.view.scene.project_id:
            return
        if self.dirty:
            self.timer.start(0)
            return
        rows = [(self.item_map[row['key']], row['score']) for row in value['results'] if row['key'] in self.item_map]
        browser = self.window.asset_browser
        browser.visual_results = rows
        browser.visual_project = self.source_project
        browser.open('visual', title='以图搜图', record=False)
        self.search.info.setText(f'{value["indexed"]}/{value["total"]} 可检索 · {value["device"].upper()}')
        self.search.info.setToolTip(f'已复用 {value["cached"]} 张索引；{value["failed"]} 张原图不可用或格式不支持')
        self.search.info.setToolTip(self.search.info.toolTip()+'\n综合比较内容、主色和明暗布局；相关度不是匹配概率，也不保证同一画风。')

    def failed(self, message):
        if self.active:
            self.search.info.setText('本地检索未完成')
            self.search.info.setToolTip(message)
            QtWidgets.QMessageBox.warning(self.window, '以图搜图', message)

    def cancel(self):
        self.timer.stop()
        self.service.stop()
        self.request = None

    def exit(self):
        if not self.active:
            return
        self.cancel()
        self.active = False
        self.clear_action.setVisible(False)
        self.search.input.setPlaceholderText(self.previous_placeholder)
        blocker = QtCore.QSignalBlocker(self.search.input)
        self.search.input.setText(self.previous_text)
        del blocker
        self.search.info.hide()
        self.search.scope_combo.hide()
        self.search.filter_button.hide()
        self.search.type_combo.setEnabled(True)
        self.window.asset_browser.visual_results = []
        if self.previous_tab is self.window.asset_browser:
            mode, scope, title = self.previous_browser
            self.window.asset_browser.open(mode if mode != 'visual' else 'all', scope, title)
        else:
            from PyQt6 import sip
            if self.previous_tab is not None and not sip.isdeleted(self.previous_tab):
                self.window.tabs.setCurrentWidget(self.previous_tab)
