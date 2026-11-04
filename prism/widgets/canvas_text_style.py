"""Native canvas text controls; reuse the existing style/save/undo contract."""
from PyQt6 import QtCore, QtGui, QtWidgets, sip
from prism import commands, ui_tokens
from prism.i18n import _


class CanvasTextStyle(QtWidgets.QWidget):
    def __init__(self, view, targets, parent=None):
        super().__init__(parent)
        self.view, self.targets = view, targets
        self._built = False
        self._syncing = False
        self._follow_active = False
        self.setObjectName('canvasTextStyle')
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_StyledBackground, True)
        palette = ui_tokens.COLOURS_DARK
        self.setStyleSheet(
            'QWidget#canvasTextStyle { background: %(toolbar-bg)s; '
            'border: 1px solid %(border-subtle)s; border-radius: 6px; }' % palette)
        self._position_timer = QtCore.QTimer(self)
        self._position_timer.setSingleShot(True)
        self._position_timer.timeout.connect(self._place)
        view.viewport().installEventFilter(self)
        self.setVisible(False)

    def _build(self):
        if self._built:
            return
        self._built = True
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(6, 5, 6, 5)
        row.setSpacing(4)
        self.bold = QtWidgets.QToolButton()
        self.bold.setText('B')
        font = self.bold.font()
        font.setBold(True)
        self.bold.setFont(font)
        self.bold.setToolTip(_('Bold'))
        self.bold.setAccessibleName(_('Bold'))
        self.bold.setCheckable(True)
        self.bold.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.bold.setMinimumSize(30, 28)
        self.bold.toggled.connect(lambda checked: self.change(bold=checked))
        row.addWidget(self.bold)
        self.size = QtWidgets.QDoubleSpinBox()
        self.size.setRange(1, 512)
        self.size.setDecimals(1)
        self.size.setSingleStep(1)
        self.size.setSuffix(' pt')
        self.size.setFixedWidth(max(108, self.size.fontMetrics().horizontalAdvance('512.0 pt') + 44))
        self.size.setKeyboardTracking(False)
        self.size.setToolTip(_('Font size'))
        self.size.setAccessibleName(_('Font size'))
        self.size.valueChanged.connect(lambda size: self.change(size=size))
        row.addWidget(self.size)
        self.colour = QtWidgets.QToolButton()
        self.colour.setText(_('Text color'))
        self.colour.setToolButtonStyle(QtCore.Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.colour.setFocusPolicy(QtCore.Qt.FocusPolicy.NoFocus)
        self.colour.clicked.connect(self.choose_colour)
        row.addWidget(self.colour)

    def refresh(self, *, armed=False):
        items = self.targets()
        visible = bool(items)
        self._follow_active = visible
        if not visible:
            self._position_timer.stop()
            self.hide()
            return
        self._build()
        style = items[0].style() if items else getattr(self.view, '_text_insert_style', {})
        self._syncing = True
        blockers = [QtCore.QSignalBlocker(self.bold), QtCore.QSignalBlocker(self.size)]
        try:
            self.bold.setChecked(bool(style.get('bold', False)))
            points = float(style.get('size') or QtWidgets.QApplication.font().pointSizeF())
            self.size.setValue(points if points > 0 else 12)
            colour = QtGui.QColor(style.get('color') or '#eeeeee')
            swatch = QtGui.QPixmap(16, 16)
            swatch.fill(colour)
            self.colour.setIcon(QtGui.QIcon(swatch))
            self.colour.setToolTip(_('Text color') + ': ' + colour.name())
        finally:
            del blockers
            self._syncing = False
        self._place()

    def eventFilter(self, watched, event):
        if watched is self.view.viewport() and self._follow_active:
            if event.type() in (QtCore.QEvent.Type.Paint, QtCore.QEvent.Type.Resize,
                                QtCore.QEvent.Type.Wheel, QtCore.QEvent.Type.MouseMove,
                                QtCore.QEvent.Type.Show):
                if not self._position_timer.isActive():
                    self._position_timer.start(0)
        return False

    def _place(self):
        items = self.targets()
        if not items or not self._built:
            self._follow_active = False
            self.hide()
            return
        bounds = QtCore.QRectF()
        for item in items:
            bounds = bounds.united(item.mapRectToScene(item.bounding_rect_unselected()))
        viewport = self.view.viewport()
        frame = self.view.mapFromScene(bounds).boundingRect()
        # No floating chrome for a selection scrolled entirely off-screen.
        # Selection/scroll paint events can show it again when it returns.
        if not viewport.rect().intersects(frame):
            self.setVisible(False)
            return
        self.adjustSize()
        margin, gap = 6, 8
        x = max(margin, min(frame.left(), viewport.width()-self.width()-margin))
        y = frame.top()-self.height()-gap
        if y < margin:
            y = frame.bottom()+gap
        y = max(margin, min(y, viewport.height()-self.height()-margin))
        position = QtCore.QPoint(x, y)
        if self.pos() != position:
            self.move(position)
        if self.isHidden():
            self.show()
        self.raise_()

    def change(self, *, _items=None, **changes):
        if self._syncing:
            return
        items = self.targets() if _items is None else _items
        defaults = dict(getattr(self.view, '_text_insert_style', {}))
        defaults.update(changes)
        self.view._text_insert_style = defaults
        updates = []
        scene = self.view.scene
        for item in items:
            if sip.isdeleted(item) or item.scene() is not scene:
                continue
            old = item.style()
            new = dict(old, **changes)
            if new != old:
                updates.append((item, new, old))
        if len(updates) > 1:
            self.view.undo_stack.beginMacro(_('Change text style'))
        try:
            for item, new, old in updates:
                self.view.undo_stack.push(commands.ChangeTextStyle(item, new, old))
        finally:
            if len(updates) > 1:
                self.view.undo_stack.endMacro()
        self.refresh(armed=getattr(self.view, '_text_tool_armed', False))

    def choose_colour(self):
        # Capture targets before the modal dialog; never colour a newer project
        # or a different text selection after queued navigation runs.
        items = list(self.targets())
        scene = self.view.scene
        project = scene.project_id
        style = items[0].style() if items else getattr(self.view, '_text_insert_style', {})
        from prism.widgets.compact_color import choose_color
        current = QtGui.QColor(style.get('color') or '#eeeeee')
        colour = choose_color(current, self, _('Text color'))
        if (colour.isValid() and self.view.scene is scene and scene.project_id == project):
            self.change(_items=items, color=colour.name())
