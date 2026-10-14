"""Temporary reference cards; never insert source objects into this scene."""
import html
import re
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.i18n import _
from prism import ui_tokens

TYPE_LABELS = {'asset': 'Assets', 'document': 'Documents',
               'mindmap': 'Mind maps', 'canvas': 'Canvases'}


def result_kind(result):
    return 'asset' if result['kind'] == 'asset' else result.get('page_kind', result['kind'])


def highlighted_text(result, value):
    words = sorted({m['text'] for m in result.get('matches', []) if m.get('text')}, key=len, reverse=True)
    if not words:
        return html.escape(value)
    pattern = re.compile('|'.join(re.escape(word) for word in words), re.IGNORECASE)
    parts, start = [], 0
    for match in pattern.finditer(value):
        parts.append(html.escape(value[start:match.start()]))
        parts.append('<b>' + html.escape(match.group()) + '</b>')
        start = match.end()
    parts.append(html.escape(value[start:]))
    return ''.join(parts)


class SearchCard(QtWidgets.QGraphicsObject):
    WIDTH, HEIGHT = 236, 214

    def __init__(self, result, host):
        super().__init__()
        self.result, self.host = result, host
        self.setFlags(self.GraphicsItemFlag.ItemIsSelectable |
                      self.GraphicsItemFlag.ItemIsMovable)
        self.setToolTip(result['title'] + '\n' + result.get('path', '') +
                        '\n' + result.get('snippet', ''))

    def boundingRect(self):
        return QtCore.QRectF(0, 0, self.WIDTH, self.HEIGHT)

    def paint(self, painter, option, widget=None):
        colors = ui_tokens.active_tokens()
        painter.setFont(self.host.font())
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(QtGui.QPen(QtGui.QColor(colors['accent'] if self.isSelected() else colors['border-subtle']), 2 if self.isSelected() else 1))
        painter.setBrush(QtGui.QColor(colors['selected-bg'] if self.isSelected() else colors['panel-bg']))
        painter.drawRoundedRect(self.boundingRect().adjusted(1, 1, -1, -1), 7, 7)
        preview = QtCore.QRectF(10, 10, self.WIDTH-20, 104)
        kind = result_kind(self.result)
        item = self.host.resolve(self.result) if kind == 'asset' else None
        from prism import lod
        if item is not None and lod.draw_image(painter, item, preview, self.host):
            pass
        else:
            document = QtGui.QTextDocument()
            document.setDefaultFont(self.host.font())
            document.setDefaultStyleSheet('body { color: '+colors['text-secondary']+'; } b { color: '+colors['accent']+'; }')
            document.setHtml('<body>'+highlighted_text(self.result, self.result.get('snippet') or _(TYPE_LABELS.get(kind, 'Assets')))+'</body>')
            document.setTextWidth(preview.width())
            painter.save()
            painter.translate(preview.topLeft())
            document.drawContents(painter, QtCore.QRectF(0, 0, preview.width(), preview.height()))
            painter.restore()
        painter.setPen(QtGui.QColor(colors['text-primary']))
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        title = QtGui.QFontMetrics(font).elidedText(self.result['title'], QtCore.Qt.TextElideMode.ElideRight, self.WIDTH-20)
        painter.drawText(QtCore.QRectF(10, 120, self.WIDTH-20, 24), title)
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QtGui.QColor(colors['text-secondary']))
        count = len(self.result.get('matches', []))
        painter.drawText(QtCore.QRectF(10, 149, self.WIDTH-20, 24),
                         _(TYPE_LABELS.get(kind, 'Assets')) + ' · ' + _('{count} matches').format(count=count))
        path = QtGui.QFontMetrics(font).elidedText(self.result.get('path', ''), QtCore.Qt.TextElideMode.ElideMiddle, self.WIDTH-20)
        painter.drawText(QtCore.QRectF(10, 177, self.WIDTH-20, 23), path)

    def mouseDoubleClickEvent(self, event):
        self.host.open_result.emit(self.result)
        event.accept()

    def mousePressEvent(self, event):
        self._drag_start = self.pos()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if self.pos() != getattr(self, '_drag_start', self.pos()):
            self.host.manual_layout = True


class ResultView(QtWidgets.QGraphicsView):
    preview_requested = QtCore.pyqtSignal()
    viewport_resized = QtCore.pyqtSignal()

    def __init__(self, parent):
        super().__init__(parent)
        self.setScene(QtWidgets.QGraphicsScene(self))
        self.setBackgroundBrush(QtGui.QColor(ui_tokens.active_tokens()['app-bg']))
        self.setDragMode(self.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(self.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.setFocusPolicy(QtCore.Qt.FocusPolicy.StrongFocus)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft | QtCore.Qt.AlignmentFlag.AlignTop)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.viewport_resized.emit()

    def wheelEvent(self, event):
        if event.modifiers() & QtCore.Qt.KeyboardModifier.ControlModifier:
            factor = 1.15 if event.angleDelta().y() > 0 else 1/1.15
            self.zoom(factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def zoom(self, factor):
        value = self.transform().m11() * factor
        if .15 <= value <= 4:
            self.scale(factor, factor)

    def keyPressEvent(self, event):
        if event.key() == QtCore.Qt.Key.Key_Space:
            self.preview_requested.emit()
            event.accept()
        elif event.key() == QtCore.Qt.Key.Key_Delete:
            event.accept()  # A reference card cannot delete project content.
        else:
            super().keyPressEvent(event)


class SearchCanvas(QtWidgets.QWidget):
    selected = QtCore.pyqtSignal(object)
    open_result = QtCore.pyqtSignal(object)
    type_requested = QtCore.pyqtSignal(str)
    suggestion_requested = QtCore.pyqtSignal(object)

    def __init__(self, resolve, parent=None, preview_text=None):
        super().__init__(parent)
        self.resolve = resolve
        self.preview_text = preview_text
        self.cards, self.query_key = {}, None
        self.manual_layout = False
        self.next_slot = 0
        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        row = QtWidgets.QHBoxLayout()
        self.type_buttons = {}
        for kind, label in [('all', 'All content'), *TYPE_LABELS.items()]:
            button = QtWidgets.QPushButton(_(label))
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, k=kind: self.type_requested.emit(k))
            row.addWidget(button)
            self.type_buttons[kind] = button
        row.addStretch()
        for label, callback in [('Zoom out', lambda: self.view.zoom(1/1.15)),
                                ('Zoom in', lambda: self.view.zoom(1.15)),
                                ('Fit results', self.fit), ('Arrange results', self.arrange)]:
            button = QtWidgets.QPushButton(_(label))
            button.clicked.connect(callback)
            row.addWidget(button)
        outer.addLayout(row)
        self.suggestions = QtWidgets.QComboBox()
        self.suggestions.activated.connect(self._suggestion)
        self.suggestions.hide()
        outer.addWidget(self.suggestions)
        self.status = QtWidgets.QLabel()
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.view = ResultView(self)
        self.layout_timer = QtCore.QTimer(self)
        self.layout_timer.setSingleShot(True)
        self.layout_timer.timeout.connect(self.reflow)
        self.view.viewport_resized.connect(lambda: self.layout_timer.start(0))
        self.view.scene().selectionChanged.connect(self._selection_changed)
        self.view.preview_requested.connect(self.preview_selected)
        self.stack = QtWidgets.QStackedWidget()
        self.stack.addWidget(self.view)
        self.preview = QtWidgets.QTextBrowser()
        self.preview.setOpenExternalLinks(False)
        self.preview.installEventFilter(self)
        self.stack.addWidget(self.preview)
        outer.addWidget(self.stack, 1)
        self.preview_toolbar = QtWidgets.QWidget()
        preview_row = QtWidgets.QHBoxLayout(self.preview_toolbar)
        for label, callback in [('Back to results', self.close_preview),
                                ('Open original location', self.open_selected)]:
            button = QtWidgets.QPushButton(_(label))
            button.clicked.connect(callback)
            preview_row.addWidget(button)
        self.preview_toolbar.hide()
        outer.addWidget(self.preview_toolbar)
        self.video_play = QtWidgets.QPushButton(_('Play / pause video'))
        self.video_play.clicked.connect(self.toggle_video)
        preview_row.addWidget(self.video_play)
        self.video_play.hide()
        self.player = None
        self.video_temp = None
        from prism.lod import cache
        cache().changed.connect(self._display_ready)

    def needs_display(self, item):
        from prism.project_search import item_key
        card = self.cards.get('asset:'+item_key(item))
        return card is not None and card.sceneBoundingRect().intersects(self.view.mapToScene(self.view.viewport().rect()).boundingRect())

    @QtCore.pyqtSlot(object)
    def _display_ready(self, item):
        if self.isVisible() and self.needs_display(item):
            self.view.viewport().update()

    def update_results(self, results, key, counts, kind, suggestions=()):
        fresh = key != self.query_key
        self.query_key = key
        previous = self.view.scene().blockSignals(True)
        try:
            if fresh:
                self.view.scene().clear()
                self.cards.clear()
                self.manual_layout = False
                self.next_slot = 0
                self.close_preview()
            ids = {r['result_id'] for r in results}
            for key_id in list(self.cards):
                if key_id not in ids:
                    card = self.cards.pop(key_id)
                    self.view.scene().removeItem(card)
            for result in results:
                key_id = result['result_id']
                if key_id not in self.cards:
                    card = SearchCard(result, self)
                    self.view.scene().addItem(card)
                    slot = self.next_slot
                    self.next_slot += 1
                    columns, step = self.layout_dimensions()
                    card.setPos(12+(slot % columns)*step, 12+(slot // columns)*230)
                    self.cards[key_id] = card
                else:
                    self.cards[key_id].result = result
                    self.cards[key_id].update()
        finally:
            self.view.scene().blockSignals(previous)
        self.view.setSceneRect(self.view.scene().itemsBoundingRect().adjusted(-30, -30, 30, 30))
        for key_kind, button in self.type_buttons.items():
            label = 'All results' if key_kind == 'all' else TYPE_LABELS[key_kind]
            button.setText(_(label) + f' ({counts.get(key_kind, 0)})')
            button.setChecked(key_kind == kind)
        self.suggestions.clear()
        self.suggestions.addItem(_('Matching locations'), None)
        for suggestion in suggestions:
            self.suggestions.addItem(suggestion.get('path') or suggestion['title'], suggestion)
        self.suggestions.setVisible(bool(suggestions))
        self.status.setText(_('Showing {shown} of {total} results').format(shown=len(results), total=counts.get('all', len(results))) if results else _('No matching results'))
        if fresh and results:
            self.view.resetTransform()
            self.arrange()
            self.view.horizontalScrollBar().setValue(0)
            self.view.verticalScrollBar().setValue(0)
        self._selection_changed()

    def _suggestion(self, index):
        value = self.suggestions.itemData(index)
        if value:
            self.suggestion_requested.emit(value)

    def _selection_changed(self):
        cards = self.view.scene().selectedItems()
        self.selected.emit(cards[0].result if len(cards) == 1 else None)

    def selected_result(self):
        cards = self.view.scene().selectedItems()
        return cards[0].result if len(cards) == 1 else None

    def arrange(self):
        self.manual_layout = False
        columns, step = self.layout_dimensions()
        for slot, card in enumerate(self.cards.values()):
            card.setPos(12+(slot % columns)*step, 12+(slot // columns)*230)
        self.next_slot = len(self.cards)
        width = max(self.view.viewport().width()/self.view.transform().m11(), self.view.scene().itemsBoundingRect().right()+12)
        self.view.setSceneRect(QtCore.QRectF(0, 0, width, max(self.view.scene().itemsBoundingRect().bottom()+12, 1)))

    def layout_dimensions(self):
        available = self.view.viewport().width()/self.view.transform().m11()
        columns = max(1, int((available-24+16)/(SearchCard.WIDTH+16)))
        step = (available-24-SearchCard.WIDTH)/(columns-1) if columns > 1 else SearchCard.WIDTH+16
        return columns, step

    def reflow(self):
        if not self.manual_layout:
            self.arrange()

    def fit(self):
        rect = self.view.scene().itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect.adjusted(-15, -15, 15, 15), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def preview_selected(self):
        result = self.selected_result()
        if result is None:
            return
        item = self.resolve(result) if result_kind(result) == 'asset' else None
        if result_kind(result) == 'asset' and item is None:
            return
        self.preview.clear()
        self.close_preview()
        self.preview.setHtml('<h2>' + html.escape(result['title']) + '</h2><p>' +
                             html.escape(result.get('path', '')) + '</p><p>' +
                             highlighted_text(result, self.preview_text(result) if self.preview_text else result.get('snippet', '')).replace('\n', '<br>') + '</p>')
        if item is not None and hasattr(item, 'pixmap'):
            pm = getattr(item, '_preview_pixmap', None)
            if pm is None or pm.isNull():
                pm = item.pixmap()
            if not pm.isNull():
                cursor = self.preview.textCursor()
                cursor.movePosition(cursor.MoveOperation.End)
                cursor.insertBlock()
                self.preview.document().addResource(QtGui.QTextDocument.ResourceType.ImageResource,
                                                    QtCore.QUrl('prism-preview'), pm)
                cursor.insertImage('prism-preview')
        if item is not None and getattr(item, 'TYPE', '') == 'video':
            self.prepare_video(item)
        self.stack.setCurrentWidget(self.preview)
        self.preview.setFocus()
        self.preview_toolbar.show()

    def open_selected(self):
        result = self.selected_result()
        if result:
            self.open_result.emit(result | {'open_original': True})

    def close_preview(self):
        if hasattr(self, 'stack'):
            if getattr(self, 'player', None) is not None:
                self.player.stop()
                self.player.setSource(QtCore.QUrl())
            if getattr(self, 'video_temp', None):
                import os
                try:
                    os.unlink(self.video_temp)
                except OSError:
                    pass
                self.video_temp = None
            if hasattr(self, 'video_play'):
                self.video_play.hide()
            if hasattr(self, 'video_widget'):
                self.video_widget.hide()
            self.stack.setCurrentWidget(self.view)
            self.preview.clear()
            self.preview_toolbar.hide()

    def prepare_video(self, item):
        """Load only the explicitly previewed video, using a separate player."""
        import os
        import tempfile
        from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
        from PyQt6.QtMultimediaWidgets import QVideoWidget
        if self.player is None:
            self.player = QMediaPlayer(self)
            self.audio = QAudioOutput(self)
            self.audio.setMuted(True)
            self.video_widget = QVideoWidget()
            self.player.setAudioOutput(self.audio)
            self.player.setVideoOutput(self.video_widget)
            self.stack.addWidget(self.video_widget)
            self.video_widget.installEventFilter(self)
        source = getattr(item, '_video_url', None)
        blob = getattr(item, '_video_blob', None)
        if blob:
            suffix = os.path.splitext(getattr(item, 'filename', '') or '')[1] or '.mp4'
            with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
                handle.write(blob)
                self.video_temp = handle.name
            source = QtCore.QUrl.fromLocalFile(self.video_temp)
        if source is not None:
            self.player.setSource(source)
            self.video_widget.show()
            self.video_play.show()

    def toggle_video(self):
        if self.player is not None:
            from PyQt6.QtMultimedia import QMediaPlayer
            self.stack.setCurrentWidget(self.video_widget)
            if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
                self.player.pause()
            else:
                self.player.play()

    def eventFilter(self, watched, event):
        if event.type() == QtCore.QEvent.Type.KeyPress and event.key() == QtCore.Qt.Key.Key_Space:
            self.close_preview()
            self.view.setFocus()
            return True
        return super().eventFilter(watched, event)

    def showEvent(self, event):
        self.layout_timer.start(0)
        super().showEvent(event)

    def hideEvent(self, event):
        if self.player is not None:
            self.player.pause()
        super().hideEvent(event)
