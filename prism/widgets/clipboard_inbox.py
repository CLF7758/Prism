"""The clipboard inbox window.

Clipboard captures land here instead of straight on the canvas, so a
screenshot taken for something else never ends up inside the page being
worked on.  Filing them is an explicit action, and this dialog is where
that happens.
"""
import logging

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.i18n import _
from prism.widgets import components

logger = logging.getLogger(__name__)


class ClipboardInboxDialog(QtWidgets.QDialog):
    """Review clipboard captures and file them when they are wanted."""

    #: Emitted with (list of InboxItems, target page id or None).
    #: ``None`` means "the canvas the user is looking at right now".
    import_requested = QtCore.pyqtSignal(list, object)

    def __init__(self, service, parent=None, pages_provider=None):
        super().__init__(parent)
        self.service = service
        # 交给调用方决定「有哪些页面可以放」，对话框自己不去读工作区。
        self.pages_provider = pages_provider
        self.setWindowTitle(_('Clipboard Inbox'))
        self.resize(760, 520)
        self.setModal(False)

        layout = QtWidgets.QVBoxLayout(self)

        self.summary = QtWidgets.QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
        self.list_widget = QtWidgets.QListWidget()
        self.list_widget.setIconSize(QtCore.QSize(96, 72))
        self.list_widget.setSelectionMode(
            QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
        splitter.addWidget(self.list_widget)

        self.preview = components.RoleLabel(role='secondary')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setMinimumWidth(240)
        self.preview.setObjectName('inboxPreview')
        splitter.addWidget(self.preview)
        splitter.setSizes([420, 300])
        layout.addWidget(splitter, 1)

        # Filing row: where the captures should land, and what to tag
        # them with.  Both apply to every selected capture at once, which
        # is the point - filing one screenshot at a time is the slow path.
        filing = QtWidgets.QHBoxLayout()
        filing.addWidget(QtWidgets.QLabel(_('Target page:')))
        self.page_box = QtWidgets.QComboBox()
        self.page_box.setToolTip(_(
            'Captures are added to this page. "Current canvas" follows '
            'whichever page you are looking at.'))
        filing.addWidget(self.page_box, 1)
        self.tag_button = components.Button(_('Add Tags…'))
        self.tag_button.setToolTip(_(
            'Attach tags to the selected captures. They travel with the '
            'captures when you file them.'))
        self.tag_button.clicked.connect(self._assign_tags)
        filing.addWidget(self.tag_button)
        layout.addLayout(filing)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.close)
        self.import_button = buttons.addButton(
            _('Add to Canvas'),
            QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        self.import_button.clicked.connect(self._import_selected)
        self.remove_button = buttons.addButton(
            _('Remove'),
            QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
        self.remove_button.clicked.connect(self._remove_selected)
        self.clear_button = buttons.addButton(
            _('Clear All'),
            QtWidgets.QDialogButtonBox.ButtonRole.DestructiveRole)
        self.clear_button.clicked.connect(self._clear_all)
        layout.addWidget(buttons)

        self.feedback = components.RoleLabel(role='secondary')
        self.feedback.setWordWrap(True)
        layout.addWidget(self.feedback)

        self.list_widget.currentItemChanged.connect(self._show_preview)
        self.list_widget.itemSelectionChanged.connect(self._update_buttons)
        service.item_added.connect(self._on_added)
        service.item_removed.connect(lambda _item: self._reload())
        service.changed.connect(self._update_buttons)
        service.url_status_changed.connect(self._on_url_status)

        # 先填一次，免得在调用方 refresh_pages() 之前下拉是空的。
        self.refresh_pages()
        self._reload()

    # ── Filling ───────────────────────────────────────────────

    def refresh_pages(self):
        """Rebuild the target-page list.

        Called before the dialog is shown, so newly created pages appear
        without reopening the window.
        """
        remembered = self.page_box.currentData()
        self.page_box.clear()
        self.page_box.addItem(_('Current canvas'), None)
        if self.pages_provider is None:
            return
        try:
            pages = list(self.pages_provider())
        except Exception:                       # noqa: BLE001
            logger.exception('Could not list workspace pages')
            return
        for page in pages:
            if not isinstance(page, dict) or not page.get('id'):
                continue
            title = page.get('title') or _('Untitled')
            kind = page.get('kind') or 'canvas'
            self.page_box.addItem(f'{title}  ·  {kind}', page['id'])
        if remembered is not None:
            index = self.page_box.findData(remembered)
            if index >= 0:
                self.page_box.setCurrentIndex(index)

    def _reload(self):
        current = self.list_widget.currentRow()
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for item in self.service.items:
            row = QtWidgets.QListWidgetItem(item.label)
            if item.preview is not None and not item.preview.isNull():
                row.setIcon(QtGui.QIcon(QtGui.QPixmap.fromImage(item.preview)))
            row.setToolTip(f'{item.snapshot.source_format}  '
                           f'{len(item.data)} bytes'
                           + (f'\n{item.snapshot.source_url}'
                              if item.snapshot.source_url else ''))
            row.setData(Qt.ItemDataRole.UserRole, item)
            self.list_widget.addItem(row)
        self.list_widget.blockSignals(False)

        if self.list_widget.count():
            row = min(max(current, 0), self.list_widget.count() - 1)
            self.list_widget.setCurrentRow(row)
        else:
            self.preview.clear()
            self.preview.setText(_('Nothing captured yet.'))
        self._update_summary()
        self._update_buttons()

    def _update_summary(self):
        count = len(self.service)
        total = self.service.total_bytes() / 1024 / 1024
        text = _('{count} capture(s) waiting, {size:.1f} MB in total.'
                 ).format(count=count, size=total)
        if self.service.ignored:
            text += ' ' + _('{n} duplicate(s) were skipped.').format(
                n=self.service.ignored)
        self.summary.setText(text)

    def _update_buttons(self):
        chosen = len(self.list_widget.selectedItems())
        self.import_button.setEnabled(bool(chosen))
        self.remove_button.setEnabled(bool(chosen))
        self.clear_button.setEnabled(bool(self.service))
        self.import_button.setText(
            _('Add {n} to Canvas').format(n=chosen) if chosen
            else _('Add to Canvas'))

    def _show_preview(self, current, _previous=None):
        if current is None:
            self.preview.clear()
            return
        item = current.data(Qt.ItemDataRole.UserRole)
        if item is None or item.preview is None:
            self.preview.setText(_('No preview available.'))
            return
        pixmap = QtGui.QPixmap.fromImage(item.preview)
        self.preview.setPixmap(pixmap.scaled(
            self.preview.size(),
            QtCore.Qt.AspectRatioMode.KeepAspectRatio,
            QtCore.Qt.TransformationMode.SmoothTransformation))

    # ── Actions ───────────────────────────────────────────────

    def _selected_items(self):
        return [row.data(Qt.ItemDataRole.UserRole)
                for row in self.list_widget.selectedItems()
                if row.data(Qt.ItemDataRole.UserRole) is not None]

    def _import_selected(self):
        items = self._selected_items()
        if not items:
            self._report(_('Select a capture in the list first.'))
            return
        # Hand them over; the window decides where they land.  Only after
        # that succeeds are they taken out of the inbox.
        page_id = self.page_box.currentData()
        self.import_requested.emit(items, page_id)
        self.service.take(items)
        target = self.page_box.currentText()
        self._report(_('Sent {n} capture(s) to "{target}".').format(
            n=len(items), target=target))
        self._reload()

    def _assign_tags(self):
        """Attach tags to every selected capture, in one go."""
        items = self._selected_items()
        if not items:
            self._report(_('Select a capture in the list first.'))
            return
        current = sorted({tag for item in items for tag in item.tags})
        text, accepted = QtWidgets.QInputDialog.getText(
            self, _('Add Tags'),
            _('Tags, separated by commas:'),
            text=', '.join(current))
        if not accepted:
            return
        tags = [part.strip() for part in text.split(',') if part.strip()]
        for item in items:
            item.tags = list(tags)
        self._report(_('Tagged {n} capture(s) with: {tags}').format(
            n=len(items), tags=', '.join(tags) or _('(none)')))
        self._reload()

    def _remove_selected(self):
        items = self._selected_items()
        if not items:
            self._report(_('Select a capture in the list first.'))
            return
        for item in items:
            self.service.remove(item)
        self.feedback.setText(_('Removed {n} capture(s).').format(
            n=len(items)))
        self._reload()

    def _clear_all(self):
        if not len(self.service):
            return
        answer = QtWidgets.QMessageBox.question(
            self, _('Clear the Clipboard Inbox'),
            _('Discard all {n} capture(s)?').format(n=len(self.service)))
        if answer != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        self.service.clear()
        self.feedback.setText(_('Inbox cleared.'))
        self._reload()

    def _report(self, text):
        self.feedback.setText(text)

    def _on_added(self, _item):
        self._reload()

    def _on_url_status(self, url, status, detail):
        if status == 'downloading':
            text = _('Downloading image from {url}').format(url=url)
        elif status == 'complete':
            text = _('Downloaded image from {url}').format(url=url)
        else:
            text = _('Could not download {url}: {reason}').format(
                url=url, reason=detail)
        self.feedback.setText(text)
