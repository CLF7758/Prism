"""Prism inspector adapted to SurfaceStudio's preview and material-info layout."""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.widgets.material_cards import material_source_label, material_version_label, ASSETS
from prism.material_textures import SLOTS
from prism.widgets.material_slots import MaterialSlot, configure_material_slot_column


CHANNEL_NAMES = {
    'albedo': '颜色', 'basecolor': '颜色', 'diffuse': '颜色',
    'normal': '法线', 'roughness': '粗糙度', 'metalness': '金属度',
    'metallic': '金属度', 'ao': '环境遮蔽', 'opacity': '透明度',
    'emissive': '自发光', 'emission': '自发光', 'sss': 'SSS',
    'displacement': '置换', 'height': '置换', 'orm': 'ORM 打包通道',
}


def plain_label(text=''):
    label = QtWidgets.QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    return label


class _StatusLine(QtWidgets.QLabel):
    """A reserved, elided line whose QLabel text remains the complete status."""

    def __init__(self, text=''):
        super().__init__(text)
        self._updating_height = False
        self._window = None
        self._screen = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(False)
        policy = QtWidgets.QSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                      QtWidgets.QSizePolicy.Policy.Fixed)
        policy.setRetainSizeWhenHidden(True)
        self.setSizePolicy(policy)
        self.setToolTip(text)
        self._refresh_row_height()

    def setText(self, text):
        super().setText(text)
        self.setToolTip(text)
        self.update()

    def clear(self):
        self.setText('')

    def setMargin(self, margin):
        super().setMargin(margin)
        self._refresh_row_height()

    def sizeHint(self):
        return QtCore.QSize(0, self.minimumHeight())

    def minimumSizeHint(self):
        return self.sizeHint()

    def _refresh_row_height(self, *_args):
        if getattr(self, '_updating_height', True):
            return
        self._updating_height = True
        try:
            margins = self.contentsMargins()
            height = max(1, self.fontMetrics().height() + margins.top() + margins.bottom()
                         + 2 * self.margin())
            if self.minimumHeight() != height or self.maximumHeight() != height:
                self.setFixedHeight(height)
        finally:
            self._updating_height = False
        self.update()

    def changeEvent(self, event):
        # Let Qt resolve the inherited font first; never setFont from FontChange.
        super().changeEvent(event)
        if event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange):
            self._refresh_row_height()

    def event(self, event):
        result = super().event(event)
        if event.type() in (QtCore.QEvent.Type.ContentsRectChange,
                            QtCore.QEvent.Type.Polish,
                            QtCore.QEvent.Type.DevicePixelRatioChange):
            self._refresh_row_height()
        return result

    def _bind_screen(self, screen):
        if self._screen is not screen:
            if self._screen is not None:
                try:
                    self._screen.logicalDotsPerInchChanged.disconnect(self._refresh_row_height)
                except (TypeError, RuntimeError):
                    pass
            self._screen = screen
            if screen is not None:
                screen.logicalDotsPerInchChanged.connect(self._refresh_row_height)
        self._refresh_row_height()

    def showEvent(self, event):
        super().showEvent(event)
        window = self.window().windowHandle()
        if self._window is not window:
            if self._window is not None:
                try:
                    self._window.screenChanged.disconnect(self._bind_screen)
                except (TypeError, RuntimeError):
                    pass
            self._window = window
            if window is not None:
                window.screenChanged.connect(self._bind_screen)
        self._bind_screen(window.screen() if window is not None else self.screen())

    def _text_rect(self):
        margin = self.margin()
        rect = self.contentsRect().adjusted(margin, margin, -margin, -margin)
        indent = self.indent()
        if indent < 0:
            indent = self.fontMetrics().horizontalAdvance('x') // 2 if self.frameWidth() else 0
        if self.alignment() & Qt.AlignmentFlag.AlignRight:
            rect.adjust(0, 0, -indent, 0)
        else:
            rect.adjust(indent, 0, 0, 0)
        return rect

    def _elided_text(self):
        return self.fontMetrics().elidedText(
            ' '.join(self.text().split()), Qt.TextElideMode.ElideRight,
            max(0, self._text_rect().width()))

    def paintEvent(self, event):
        # Paint the normal frame/background, but not QLabel's unbounded text.
        QtWidgets.QFrame.paintEvent(self, event)
        painter = QtGui.QPainter(self)
        painter.setClipRect(self._text_rect())
        self.style().drawItemText(
            painter, self._text_rect(), int(self.alignment()) | int(Qt.TextFlag.TextSingleLine),
            self.palette(), self.isEnabled(), self._elided_text(), QtGui.QPalette.ColorRole.WindowText)


class _PreviewCanvas(QtWidgets.QLabel):
    """A preview surface whose size is decided by the layout, not by the image.

    A QLabel that carries a pixmap reports a size hint taken from that
    pixmap, so a wide thumbnail pushes the panel's minimum width and
    reallocates the header.  This surface keeps the image out of the size
    negotiation and paints it itself, scaled into the visible content area
    so the picture stays proportional and nothing is cropped.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._source = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWordWrap(False)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)

    def set_source(self, pixmap):
        """Show an image without letting it take part in the layout."""
        self._source = pixmap
        self.update()

    def source_size(self):
        if self._source is None or self._source.isNull():
            return None
        return (self._source.width(), self._source.height(),
                self._source.devicePixelRatio())

    def clear(self):
        self._source = None
        super().clear()

    def sizeHint(self):
        # The layout gives this surface its size; the image never does.
        return QtCore.QSize(0, 0)

    def minimumSizeHint(self):
        return QtCore.QSize(0, 0)

    def paintEvent(self, event):
        super().paintEvent(event)
        if self._source is None or self._source.isNull():
            return
        target = self.contentsRect()
        if target.width() <= 0 or target.height() <= 0:
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(target.topLeft(), self._source.scaled(
            target.size(), Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation))


class MaterialDetails(QtWidgets.QWidget):
    edited = QtCore.pyqtSignal()
    tag_requested = QtCore.pyqtSignal(str)
    slot_file_requested = QtCore.pyqtSignal(str, str)
    slot_channel_requested = QtCore.pyqtSignal(str, str)
    slot_clear_requested = QtCore.pyqtSignal(str)
    slot_reuse_requested = QtCore.pyqtSignal(str)
    render_requested = QtCore.pyqtSignal()
    open_requested = QtCore.pyqtSignal()
    export_requested = QtCore.pyqtSignal()
    settings_requested = QtCore.pyqtSignal()
    slot_options_requested = QtCore.pyqtSignal(str, object)

    def __init__(self, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(parent))

    def _construct(self, parent=None):
        from .widget_construction import widget_steps
        super().__init__(parent)
        self._record_id = None
        self._thumbnail = None
        self._pixmap = None
        self._tag_values = None
        self.setObjectName('materialDetails')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.empty = plain_label('选择一张材质卡片，查看效果和资料。')
        self.empty.setContentsMargins(16, 20, 16, 16)
        layout.addWidget(self.empty)
        self.sections = QtWidgets.QTabWidget(self)
        layout.addWidget(self.sections, 1)
        self.scroll = QtWidgets.QScrollArea()
        self.sections.addTab(self.scroll, '贴图槽位')
        self.metadata_scroll = QtWidgets.QScrollArea()
        self.metadata_scroll.setWidgetResizable(True)
        self.metadata_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.sections.addTab(self.metadata_scroll, '版本与文件')
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Reserve the same gutter through every fold; no width/preview reflow.
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.form = QtWidgets.QWidget()
        self.scroll.setWidget(self.form)
        yield
        form = QtWidgets.QVBoxLayout(self.form)
        form.setContentsMargins(16, 18, 16, 20)
        configure_material_slot_column(form)
        form.addWidget(plain_label('材质效果'))
        self.preview = _PreviewCanvas()
        self.preview.setObjectName('materialPreview')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(190)
        self.preview.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                   QtWidgets.QSizePolicy.Policy.Fixed)
        self.preview.setStyleSheet('background: #202023; border: 1px solid #35353b; border-radius: 8px; color: #929ba8;')
        from prism.widgets.material_render import MaterialRender
        self.inline_render = yield from widget_steps(MaterialRender)
        self.inline_render.setFixedHeight(190)
        self.preview_stack = QtWidgets.QStackedWidget()
        self.preview_stack.setFixedHeight(190)
        self.preview_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                         QtWidgets.QSizePolicy.Policy.Fixed)
        self.preview_stack.addWidget(self.preview)
        self.preview_stack.addWidget(self.inline_render)
        self.set_live_preview(False)
        form.addWidget(self.preview_stack)
        self.preview_status = _StatusLine()
        self.preview_status.setProperty('uiRole', 'secondary')
        form.addWidget(self.preview_status)
        buttons = QtWidgets.QHBoxLayout()
        self.render_button = QtWidgets.QPushButton('重试预览')
        retry_policy = self.render_button.sizePolicy()
        retry_policy.setRetainSizeWhenHidden(True)
        self.render_button.setSizePolicy(retry_policy)
        self.render_button.hide()
        self.open_button = QtWidgets.QPushButton('打开大预览')
        buttons.addWidget(self.render_button)
        buttons.addWidget(self.open_button)
        form.addLayout(buttons)
        self.render_button.clicked.connect(self.render_requested.emit)
        self.open_button.clicked.connect(self.open_requested.emit)
        self.operation_status = _StatusLine()
        form.addWidget(self.operation_status)
        settings = QtWidgets.QFormLayout()
        self.blend_mode = QtWidgets.QComboBox()
        for text, value in [('自动识别', 'auto'), ('强制不透明', 'opaque'), ('强制透明裁切', 'masked'), ('强制半透明', 'translucent')]:
            self.blend_mode.addItem(text, value)
        self.normal_convention = QtWidgets.QComboBox()
        for value in ('DirectX', 'OpenGL'):
            self.normal_convention.addItem(value, value)
        self.auto_mode = _StatusLine('自动：分析贴图后确定')
        form.addWidget(self.auto_mode)
        global_uv = QtWidgets.QHBoxLayout()
        self.flip_x = QtWidgets.QCheckBox('整体水平翻转')
        self.flip_y = QtWidgets.QCheckBox('整体垂直翻转')
        self.flip_x.hide()
        self.flip_y.hide()
        global_uv.addWidget(self.flip_x)
        global_uv.addWidget(self.flip_y)
        form.addLayout(global_uv)
        self.flip_x.toggled.connect(self.settings_requested.emit)
        self.flip_y.toggled.connect(self.settings_requested.emit)
        self.advanced_toggle = QtWidgets.QToolButton()
        self.advanced_toggle.setText('高级：透明覆盖与法线格式')
        self.advanced_toggle.setCheckable(True)
        form.addWidget(self.advanced_toggle)
        advanced = QtWidgets.QWidget()
        advanced.setLayout(settings)
        advanced.hide()
        self.advanced_toggle.toggled.connect(advanced.setVisible)
        settings.addRow('透明判断', self.blend_mode)
        self.alpha_cutoff = QtWidgets.QDoubleSpinBox()
        self.alpha_cutoff.setRange(0, 1)
        self.alpha_cutoff.setSingleStep(.01)
        self.alpha_cutoff.setValue(.5)
        self.alpha_cutoff.valueChanged.connect(self.settings_requested.emit)
        settings.addRow('裁切阈值', self.alpha_cutoff)
        self.ignore_base_alpha = QtWidgets.QCheckBox('忽略颜色图 A 通道')
        self.ignore_base_alpha.toggled.connect(self.settings_requested.emit)
        settings.addRow(self.ignore_base_alpha)
        settings.addRow('法线格式', self.normal_convention)
        yield
        form.addWidget(advanced)
        self.blend_mode.currentIndexChanged.connect(self.settings_requested.emit)
        self.normal_convention.currentIndexChanged.connect(self.settings_requested.emit)
        self.slots = {}
        for key, title in SLOTS.items():
            slot = yield from widget_steps(MaterialSlot, key, title, self.form)
            self.slots[key] = slot
            slot.file_requested.connect(self.slot_file_requested.emit)
            slot.channel_requested.connect(self.slot_channel_requested.emit)
            slot.clear_requested.connect(self.slot_clear_requested.emit)
            slot.reuse_requested.connect(self.slot_reuse_requested.emit)
            slot.options_requested.connect(self.slot_options_requested.emit)
            form.addWidget(slot)
            # MaterialSlot's visibility handler is connected first. Complete
            # the parent layout and scroll geometry before its queued paint.
            slot.fold.toggled.connect(self._reflow_slots)
            yield
        self.name = QtWidgets.QLineEdit()
        self.name.setMaxLength(160)
        self.name.setPlaceholderText('材质名称')
        form.addWidget(self.name)
        self.location = plain_label()
        form.addWidget(self.location)
        self.info = {}
        self.info_labels = {}
        info_layout = QtWidgets.QFormLayout()
        info_layout.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        for key, title in [('source', '资料类型'), ('engine', 'UE 版本'),
                           ('revision', '收集版本'), ('template', '默认模板')]:
            self.info[key] = plain_label()
            title_label = plain_label(title)
            self.info_labels[key] = title_label
            info_layout.addRow(title_label, self.info[key])
        form.addLayout(info_layout)
        form.addWidget(plain_label('材质属性 · 只读资料'))
        properties = QtWidgets.QFormLayout()
        self.properties = {}
        for key, title in [('blendMode', '透明方式'), ('metallic', '金属度'),
                           ('roughness', '粗糙度'), ('normal', '法线'),
                           ('emissive', '自发光'), ('sss', 'SSS 散射')]:
            self.properties[key] = plain_label('未提供')
            properties.addRow(title, self.properties[key])
        form.addLayout(properties)
        form.addWidget(plain_label('标签'))
        self.tags = QtWidgets.QWidget()
        self._tag_layout = QtWidgets.QGridLayout(self.tags)
        self._tag_layout.setContentsMargins(0, 0, 0, 0)
        self._tag_layout.setSpacing(4)
        form.addWidget(self.tags)
        self.tag_edit = QtWidgets.QLineEdit()
        self.tag_edit.setPlaceholderText('标签，用逗号分隔')
        self.tag_edit.editingFinished.connect(self.settings_requested.emit)
        form.addWidget(self.tag_edit)
        form.addWidget(plain_label('贴图明细'))
        self.texture_info = plain_label('尚无贴图')
        form.addWidget(self.texture_info)
        self.native_info = plain_label()
        form.addWidget(self.native_info)
        self.export_button = QtWidgets.QPushButton('导出原始贴图')
        self.export_button.clicked.connect(self.export_requested.emit)
        form.addWidget(self.export_button)
        form.addWidget(plain_label('说明'))
        self.notes = QtWidgets.QPlainTextEdit()
        self.notes.setPlaceholderText('来源、适用场景或使用备注')
        self.notes.setFixedHeight(100)
        form.addWidget(self.notes)
        yield
        form.removeWidget(self.name)
        form.insertWidget(0, self.name)
        self.status = plain_label()
        self.status.setProperty('uiRole', 'secondary')
        form.addWidget(self.status)
        form.addStretch(1)
        # Keep the preview visible while scrolling through lower slots.
        self.header = QtWidgets.QWidget(self)
        layout.insertWidget(layout.indexOf(self.sections), self.header)
        header = QtWidgets.QVBoxLayout(self.header)
        header.setContentsMargins(16, 10, 16, 4)
        header.setSpacing(5)
        for widget in (self.name, self.preview_stack, self.preview_status):
            form.removeWidget(widget)
            header.addWidget(widget)
        def detach_layout(child):
            for index in range(form.count()):
                if form.itemAt(index).layout() is child:
                    form.takeAt(index)
                    child.setParent(None)
                    return
        detach_layout(buttons)
        header.addLayout(buttons)
        for widget in (self.operation_status, self.auto_mode):
            form.removeWidget(widget)
            header.addWidget(widget)
        detach_layout(global_uv)
        header.addLayout(global_uv)
        for index in reversed(range(form.count())):
            item = form.itemAt(index)
            widget = item.widget()
            if isinstance(widget, QtWidgets.QLabel) and widget.text() == '材质效果':
                form.takeAt(index)
                widget.deleteLater()
        metadata = QtWidgets.QWidget()
        self.metadata_scroll.setWidget(metadata)
        metadata_layout = QtWidgets.QVBoxLayout(metadata)
        metadata_layout.setContentsMargins(16, 10, 16, 12)
        start = form.indexOf(self.location)
        while form.count() > start:
            item = form.takeAt(start)
            if item.widget():
                metadata_layout.addWidget(item.widget())
            elif item.layout():
                item.layout().setParent(None)
                metadata_layout.addLayout(item.layout())
            else:
                metadata_layout.addItem(item)
            yield
        form.addStretch(1)
        footer = QtWidgets.QWidget()
        footer_layout = QtWidgets.QFormLayout(footer)
        footer_layout.setContentsMargins(16,4,16,8)
        footer_layout.setSpacing(6)
        self.notes.setFixedHeight(54)
        footer_layout.addRow('标签', self.tag_edit)
        footer_layout.addRow('备注', self.notes)
        layout.addWidget(footer)
        self.footer = footer
        self.export_button.setParent(self.header)
        self.export_button.setText('导出原始贴图…')
        buttons.addWidget(self.export_button)
        for button in (self.render_button, self.open_button, self.export_button):
            policy = button.sizePolicy()
            policy.setHorizontalPolicy(QtWidgets.QSizePolicy.Policy.Ignored)
            button.setSizePolicy(policy)
            button.setToolTip(button.text())
        self.shape = QtWidgets.QComboBox()
        for text, key in [('平面','plane'),('球体','sphere'),('圆柱体','cylinder'),('立方体','box')]:
            self.shape.addItem(text,key)
        self.shape.currentIndexChanged.connect(lambda: self.inline_render.set_shape(self.shape.currentData()))
        header.addWidget(self.shape)
        self.name.textEdited.connect(self.edited.emit)
        self.notes.textChanged.connect(self.edited.emit)
        self.show_record(None)

    def set_live_preview(self, enabled: bool):
        """Select live render or thumbnail in one fixed-height preview slot.

        QStackedWidget drives the actual visibility events, allowing the live
        renderer to resume/pause work normally when its page changes.
        """
        self.preview_stack.setCurrentWidget(self.inline_render if enabled else self.preview)

    def _reflow_slots(self, _expanded=False):
        """Commit a fold's content geometry and scroll range in one turn."""
        layout = self.form.layout()
        if layout is not None:
            layout.invalidate()
            layout.activate()
        self.form.updateGeometry()
        # QScrollArea handles this viewport event by resizing its content and
        # updating/clamping the range. Sending just that notification avoids
        # pumping unrelated events or painting an intermediate empty gap.
        QtWidgets.QApplication.sendEvent(
            self.scroll.viewport(), QtCore.QEvent(QtCore.QEvent.Type.LayoutRequest))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_preview()

    def _fit_preview(self):
        """Show the current image inside the preview's real content area.

        The surface paints the image itself, scaled into its visible
        contents rect, so the picture is always proportional and never
        cropped.  The panel width is no longer used as a guess for the
        image size: that guess was wider than the visible area and cut the
        picture off on the right.
        """
        self.preview.set_source(self._pixmap)

    def _set_tags(self, tags):
        if self._tag_values == tags:
            return
        self._tag_values = list(tags)
        while self._tag_layout.count():
            item = self._tag_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        if not tags:
            self._tag_layout.addWidget(plain_label('暂无标签'), 0, 0)
        for index, tag in enumerate(tags):
            button = QtWidgets.QToolButton()
            button.setText(tag)
            button.setToolTip('筛选标签：' + tag)
            button.setMaximumWidth(130)
            button.clicked.connect(lambda _checked=False, value=tag: self.tag_requested.emit(value))
            self._tag_layout.addWidget(button, index // 2, index % 2)

    def show_record(self, record, location='', thumbnail=None):
        record_id = record['id'] if record else None
        changed_record = record_id != self._record_id
        if changed_record:
            self.set_live_preview(False)
            self.operation_status.clear()
        self._record_id = record_id
        self.empty.setVisible(record is None)
        self.header.setVisible(record is not None)
        self.footer.setVisible(record is not None)
        self.sections.setVisible(record is not None)
        with QtCore.QSignalBlocker(self.name), QtCore.QSignalBlocker(self.notes):
            name = record['name'] if record else ''
            notes = record['notes'] if record else ''
            if changed_record or self.name.text() != name:
                self.name.setText(name)
            if changed_record or self.notes.toPlainText() != notes:
                self.notes.setPlainText(notes)
        if changed_record:
            self.scroll.verticalScrollBar().setValue(0)
        if not record:
            self._pixmap = None
            self._thumbnail = None
            self.preview.clear()
            return
        textures = record.get('textures', {})
        for key, slot in self.slots.items():
            slot.show_binding(textures.get(key, {}), record.get('preview', {}).get('properties', {}))
        props = record.get('preview', {}).get('properties', {})
        with QtCore.QSignalBlocker(self.blend_mode), QtCore.QSignalBlocker(self.normal_convention):
            self.blend_mode.setCurrentIndex(max(0, self.blend_mode.findData(props.get('blendMode', 'auto'))))
            self.normal_convention.setCurrentIndex(max(0, self.normal_convention.findData(props.get('normalConvention', 'DirectX'))))
        with QtCore.QSignalBlocker(self.flip_x), QtCore.QSignalBlocker(self.flip_y), QtCore.QSignalBlocker(self.alpha_cutoff), QtCore.QSignalBlocker(self.ignore_base_alpha):
            self.flip_x.setChecked(props.get('flipX', False))
            self.flip_y.setChecked(props.get('flipY', False))
            self.alpha_cutoff.setValue(props.get('alphaCutoff', .5))
            self.ignore_base_alpha.setChecked(props.get('ignoreBaseAlpha', False))
        if changed_record or not self.tag_edit.hasFocus():
            self.tag_edit.setText(', '.join(record.get('tags', [])))
        self.export_button.setEnabled(bool(record.get('files')))
        self.info['engine'].setVisible(bool(record.get('nativePackages')))
        self.info_labels['engine'].setVisible(bool(record.get('nativePackages')))
        self.location.setText('分类：' + location)
        self.info['source'].setText(material_source_label(record))
        self.info['engine'].setText(material_version_label(record).removeprefix('UE '))
        self.info['revision'].setText(str(record.get('assetVersion') or '未提供'))
        template = record.get('ueTemplate', {})
        self.info['template'].setText(
            str(template.get('name', '未绑定')) if isinstance(template, dict) else '未绑定')
        self._set_tags(record.get('tags', []))
        properties = record.get('preview', {}).get('properties', {})
        if not isinstance(properties, dict):
            properties = {}
        aliases = {'metallic': ('metallic', 'metalness'), 'normal': ('normal',),
                   'roughness': ('roughness',), 'emissive': ('emissive', 'emission'), 'sss': ('sss',)}
        texture_keys = {str(key).casefold(): key for key in textures}
        for key, label in self.properties.items():
            value = properties.get(key)
            if key == 'blendMode':
                text = {'opaque': '不透明', 'masked': '透明裁切', 'translucent': '半透明'}.get(value, '未提供') if isinstance(value, str) else '未提供'
            elif isinstance(value, bool):
                text = '开启' if value else '关闭'
            elif isinstance(value, (str, int, float)):
                text = str(value)
            elif any(alias in texture_keys for alias in aliases.get(key, ())):
                text = '使用贴图'
            else:
                text = '未提供'
            label.setText(text)
        texture_lines = []
        for channel, entry in textures.items():
            label = CHANNEL_NAMES.get(str(channel).casefold(), str(channel))
            if isinstance(entry, dict):
                name = str(entry.get('name') or '文件名未提供')
                width, height = entry.get('width'), entry.get('height')
                dimensions = f'{width} × {height}' if isinstance(width, int) and isinstance(height, int) else '尺寸未提供'
                texture_lines.append(f'{label} · {name}\n{dimensions}')
            else:
                texture_lines.append(f'{label} · 文件资料未提供')
        self.texture_info.setText('\n\n'.join(texture_lines) if texture_lines else '尚无贴图')
        native = record.get('nativePackages', [])
        self.native_info.setText(f'UE 原件：{len(native)} 份；完整性未提供' if native else 'UE 原件：尚未收集')
        self.status.setText('草稿 · 资料未收集完整' if record['status'] == 'draft' else '材质资料')
        if thumbnail != self._thumbnail or changed_record:
            self._thumbnail = thumbnail
            self._pixmap = None
            if thumbnail:
                raw = QtCore.QByteArray.fromBase64(thumbnail.split(',', 1)[1].encode('ascii'))
                pixmap = QtGui.QPixmap()
                if pixmap.loadFromData(raw):
                    self._pixmap = pixmap
            if self._pixmap is None:
                self.preview.set_source(None)
                self.preview.setText('尚未生成材质球' if textures else '待添加材质贴图')
                self.preview_status.setText('尚无球体展示。' if textures else '这条记录还没有贴图资料。')
                default = QtGui.QPixmap(str(ASSETS / 'default-sphere.png'))
                if not default.isNull():
                    self._pixmap = default
                    self._fit_preview()
                    self.preview_status.setText('默认灰球 · 等待更新预览' if textures else '默认灰球 · 尚未配置贴图')
            else:
                self.preview.setText('')
                self._fit_preview()
                self.preview_status.setText('材质展示缩略图')
        if record.get('preview', {}).get('stale'):
            self.preview_status.setText('配置已保存，预览待更新')
