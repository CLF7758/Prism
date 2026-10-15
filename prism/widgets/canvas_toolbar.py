"""Native implementation of the approved web prototype's canvas toolbar."""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt
from prism import ui_tokens
from prism.items import COLOR_GROUPS, ARROW_END, ARROW_NONE
from prism.widgets.components.icons import icon


class ReadableComboBox(QtWidgets.QComboBox):
    """Popup width is measured independently from the compact closed control."""
    def __init__(self, parent=None):
        super().__init__(parent)
        popup = QtWidgets.QListView()
        popup.setTextElideMode(Qt.TextElideMode.ElideNone)
        popup.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setView(popup)
        popup.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setItemDelegate(QtWidgets.QStyledItemDelegate(self))
        self.setSizeAdjustPolicy(QtWidgets.QComboBox.SizeAdjustPolicy.AdjustToContents)

    def showPopup(self):
        metrics = self.fontMetrics()
        width = max(self.width(), max((metrics.horizontalAdvance(self.itemText(i)) for i in range(self.count())), default=0) + 48)
        self.view().setMinimumWidth(width)
        super().showPopup()
        self.view().setTextElideMode(Qt.TextElideMode.ElideNone)
        frame = self.view().window()
        frame.resize(max(frame.width(), width + 8), frame.height())


class ToolScrollArea(QtWidgets.QScrollArea):
    def wheelEvent(self, event):
        bar = self.horizontalScrollBar()
        bar.setValue(bar.value() - event.angleDelta().y())
        event.accept()


class CanvasToolbar(QtWidgets.QWidget):
    filter_requested = QtCore.pyqtSignal()

    def __init__(self, view, colour_host, parent=None, *, canvas=None, annotation_only=False):
        super().__init__(parent)
        self.view, self.colour_host = view, colour_host
        self._canvas_override = canvas
        self.setObjectName('canvasToolbar')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(56)
        c = ui_tokens.COLOURS_DARK
        self.setStyleSheet(
            'QWidget#canvasToolbar, QWidget#canvasTools { background: %(toolbar-bg)s; }'
            'QWidget#canvasToolbar { border: none; }'
            'QWidget#canvasToolbar QToolButton { padding: 0 5px; border: none; border-radius: 6px; background: transparent; }'
            'QWidget#canvasToolbar QToolButton:hover { background: %(hover-bg)s; }'
            'QWidget#canvasToolbar QToolButton:checked { background: %(selected-bg)s; }'
            'QWidget#canvasToolbar QToolButton[colourChip="true"] { padding: 0; border-radius: 9px; }'
            'QWidget#canvasToolbar QToolButton[colourChip="true"]:checked { border: 2px solid white; }'
            'QWidget#canvasToolbar QComboBox { min-height: 24px; max-height: 24px; padding: 0 4px; border-radius: 5px; }'
            'QWidget#canvasToolbar QScrollArea { background: transparent; border: none; }' % c)
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(4, 12, 4, 4)
        outer.setSpacing(0)
        self.scroll = ToolScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.viewport().setAutoFillBackground(True)
        palette = self.scroll.viewport().palette()
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(c['toolbar-bg']))
        self.scroll.viewport().setPalette(palette)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Expanding)
        content = QtWidgets.QWidget()
        content.setObjectName('canvasTools')
        content.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        content.setSizePolicy(QtWidgets.QSizePolicy.Policy.Minimum, QtWidgets.QSizePolicy.Policy.Preferred)
        row = QtWidgets.QHBoxLayout(content)
        row.setContentsMargins(4, 0, 4, 0)
        row.setSpacing(2)
        self.colours = {}
        for group in ([] if annotation_only else [{'id': 'all', 'label': '全部', 'color': '#aaaaaa'}] + list(COLOR_GROUPS)):
            button = self.button(row, group['label'], lambda checked=False, gid=group['id']: self.colour_clicked(gid))
            button.setProperty('colourChip', True)
            button.setCheckable(True)
            button.setFixedSize(18, 18)
            button.setIconSize(QtCore.QSize(18, 18))
            button.setIcon(self.colour_icon(group['color'], rainbow=group['id'] == 'all'))
            self.colours[group['id']] = button
        self.separator(row)
        self.gray = self.button(row, '灰度图模式', lambda: self.colour_clicked('grayscale'), text='灰度')
        self.gray.setIcon(self.colour_icon('#888888', grayscale=True))
        self.gray.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.gray.setCheckable(True)
        self.separator(row)
        self.tools = {}
        self.capture_button = self.button(row, '屏幕截图：钉在桌面或送入画布', view.on_action_screen_capture, image=icon('capture'), text='截图')
        self.capture_button.setObjectName('screenCaptureButton')
        self.pin_button = self.button(row, '将选中图片钉在桌面', view.on_action_pin_reference, image=icon('pin'), text='贴图')
        self.pin_button.setObjectName('pinReferenceButton')
        for key, title in [('select', '选择 / 退出绘制模式'), ('pen', '画笔'), ('eraser', '橡皮擦'), ('line', '直线'), ('arrow', '箭头'), ('text', '文字')]:
            button = self.button(row, title, lambda checked=False, tool=key: self.activate(tool), image=icon(key))
            button.setCheckable(True)
            button.setFixedWidth(30)
            self.tools[key] = button
        self.separator(row)
        self.swatch = self.button(row, '笔触颜色', view.pick_pen_colour)
        self.swatch.setObjectName('strokeSwatch')
        self.swatch.setFixedSize(26, 26)
        self.width_combo = ReadableComboBox()
        self.width_combo.setToolTip('笔触宽度（0.5–50 px）')
        self.width_combo.setFixedWidth(96)
        for width in (1, 2, 4, 8, 16, 24):
            self.width_combo.addItem(f'{width} px', float(width))
        self.width_combo.addItem('自定义…', None)
        self.width_combo.activated.connect(self.choose_width)
        row.addWidget(self.width_combo)
        self.style_combo = ReadableComboBox()
        self.style_combo.setFixedWidth(80)
        self.style_combo.setToolTip('线型')
        for label, value in [('实线', 'solid'), ('虚线', 'dashed'), ('点线', 'dotted')]:
            self.style_combo.addItem(label, value)
        self.style_combo.activated.connect(lambda: view.set_line_style(self.style_combo.currentData()))
        row.addWidget(self.style_combo)
        if not annotation_only:
            self.separator(row)
            self.button(row, '适应全部 (Ctrl+0)', self.fit_all, image=icon('fit-all'))
            self.button(row, '回到中心（保持缩放）', self.centre, image=icon('center'))
            row.addStretch()
        self.scroll.setWidget(content)
        outer.addWidget(self.scroll, 1)
        self.separator(outer)
        self.filter_button = self.button(outer, '筛选：标签 / 评分 / 颜色 / 未标记', self.filter_requested.emit, image=icon('filter'), text='筛选')
        self.filter_button.setFixedWidth(68)
        self.filter_button.setCheckable(True)
        if colour_host is not None:
            colour_host.filter_changed.connect(self.refresh)
        if annotation_only:
            for button in (self.gray, self.capture_button, self.pin_button, self.filter_button):
                button.hide()
            for line in self.findChildren(QtWidgets.QFrame):
                if line.frameShape() == QtWidgets.QFrame.Shape.VLine:
                    line.hide()
            outer.setContentsMargins(4, 4, 4, 4)
            self.setFixedHeight(36)
            content.layout().activate()
            self.setFixedWidth(content.sizeHint().width()+12)
        view.drawing_state_changed.connect(self.refresh)
        self.refresh()

    @staticmethod
    def colour_icon(colour, rainbow=False, grayscale=False):
        pixmap = QtGui.QPixmap(36, 36)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QtGui.QPainter(pixmap)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        if rainbow:
            brush = QtGui.QConicalGradient(18, 18, 0)
            for index, group in enumerate(list(COLOR_GROUPS)[:8]):
                brush.setColorAt(index / 8, QtGui.QColor(group['color']))
            brush.setColorAt(1, QtGui.QColor(COLOR_GROUPS[0]['color']))
        elif grayscale:
            brush = QtGui.QLinearGradient(0, 0, 36, 0)
            brush.setColorAt(0, QtGui.QColor('#ffffff'))
            brush.setColorAt(.48, QtGui.QColor('#ffffff'))
            brush.setColorAt(.52, QtGui.QColor('#888888'))
            brush.setColorAt(1, QtGui.QColor('#888888'))
        else:
            brush = QtGui.QColor(colour)
        painter.setBrush(QtGui.QBrush(brush))
        painter.drawEllipse(2, 2, 32, 32)
        painter.end()
        return QtGui.QIcon(pixmap)

    def button(self, layout, title, callback, image=None, text=''):
        button = QtWidgets.QToolButton()
        button.setToolTip(title)
        button.setAccessibleName(title)
        button.setText(text)
        button.setFixedHeight(28)
        button.setMinimumWidth(30)
        button.setIconSize(QtCore.QSize(16, 16))
        if image is not None:
            button.setIcon(image)
            if text:
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.clicked.connect(callback)
        layout.addWidget(button)
        return button

    @staticmethod
    def separator(layout):
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        line.setFixedSize(1, 22)
        layout.addSpacing(4)
        layout.addWidget(line)
        layout.addSpacing(4)

    def activate(self, tool):
        canvas = self.material_canvas()
        if canvas is not None:
            canvas.activate(tool)
            self.refresh()
            return
        if tool == 'select':
            self.view.cancel_draw_mode()
        elif tool == 'text':
            self.view.cancel_draw_mode()
            self.view._text_tool_armed = True
            self.view.viewport().setCursor(Qt.CursorShape.CrossCursor)
        else:
            self.view.start_draw_mode()
            self.view.set_drawing_tool('line' if tool == 'arrow' else tool)
            if tool in ('line', 'arrow'):
                self.view.set_arrow_style(ARROW_END if tool == 'arrow' else ARROW_NONE)
        self.refresh()

    def choose_width(self, index):
        width = self.width_combo.itemData(index)
        if width is None:
            width, accepted = QtWidgets.QInputDialog.getDouble(self, '笔触宽度', '宽度（0.5–50 px）', self.view._pen_width, .5, 50, 1)
            if not accepted:
                self.refresh()
                return
        self.view.set_pen_width(width)

    def centre(self):
        canvas = self.material_canvas()
        if canvas is not None:
            bounds = canvas.scene().itemsBoundingRect()
            canvas.centerOn(bounds.center())
            return
        items = [item for item in self.view.scene.items_for_save() if item.isVisible()]
        if items:
            bounds = items[0].sceneBoundingRect()
            for item in items[1:]:
                bounds = bounds.united(item.sceneBoundingRect())
            self.view.centerOn(bounds.center())

    def refresh(self):
        canvas = self.material_canvas()
        for group, button in self.colours.items():
            active_group = canvas.colour_group if canvas is not None else self.colour_host._active_group
            button.setChecked(group == (active_group or 'all'))
            chip = self.colour_host.all_chip if group == 'all' else self.colour_host.chips[group]
            button.setToolTip(chip.toolTip())
        self.gray.setChecked(canvas.display_grayscale if canvas is not None else self.colour_host._grayscale_active)
        tool = self.view._drawing_tool if self.view.active_mode == self.view.DRAW_MODE else 'select'
        if self.view._text_tool_armed:
            tool = 'text'
        if tool == 'line' and self.view._arrow != ARROW_NONE:
            tool = 'arrow'
        canvas = self.material_canvas()
        if canvas is not None:
            tool = canvas.tool
        for key, button in self.tools.items():
            button.setChecked(key == tool)
        self.swatch.setStyleSheet('QToolButton#strokeSwatch { background: %s; border: 1px solid #565D69; border-radius: 5px; }' % self.view._pen_color)
        self.width_combo.blockSignals(True)
        index = self.width_combo.findData(self.view._pen_width)
        if index < 0:
            self.width_combo.insertItem(self.width_combo.count() - 1, f'{self.view._pen_width:g} px', self.view._pen_width)
            index = self.width_combo.findData(self.view._pen_width)
        self.width_combo.setCurrentIndex(index)
        self.width_combo.blockSignals(False)
        self.style_combo.setCurrentIndex(self.style_combo.findData(self.view._line_style))

    def material_canvas(self):
        if self._canvas_override is not None:
            return self._canvas_override
        browser = getattr(self.view.parent, 'asset_browser', None)
        if browser is not None and self.view.parent.tabs.currentWidget() is browser:
            return browser.list
        return None

    def colour_clicked(self, group):
        canvas = self.material_canvas()
        if canvas is None:
            self.colour_host._on_chip_clicked(group)
            return
        if group == 'grayscale':
            canvas.display_grayscale = not canvas.display_grayscale
        else:
            canvas.colour_group = None if group in ('all', canvas.colour_group) else group
        canvas.viewport().update()
        self.refresh()

    def fit_all(self):
        canvas = self.material_canvas()
        if canvas is not None:
            bounds = canvas.scene().itemsBoundingRect()
            if not bounds.isEmpty():
                canvas.fitInView(bounds.adjusted(-20, -20, 20, 20), Qt.AspectRatioMode.KeepAspectRatio)
                callback = getattr(self.view.window(), '_update_status', None)
                if callback is not None:
                    callback({})
        else:
            self.view.on_action_fit_scene()
