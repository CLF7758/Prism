"""Prism inspector adapted to SurfaceStudio's preview and material-info layout."""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.widgets.material_cards import material_source_label, material_version_label, ASSETS
from prism.material_textures import SLOTS
from prism.widgets.material_slots import MaterialSlot


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

    def __init__(self):
        super().__init__()
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
        self.scroll = QtWidgets.QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.form = QtWidgets.QWidget()
        form = QtWidgets.QVBoxLayout(self.form)
        form.setContentsMargins(16, 18, 16, 20)
        form.setSpacing(12)
        form.addWidget(plain_label('材质效果'))
        self.preview = plain_label()
        self.preview.setObjectName('materialPreview')
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(190)
        self.preview.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                   QtWidgets.QSizePolicy.Policy.Fixed)
        self.preview.setStyleSheet('background: #202023; border: 1px solid #35353b; border-radius: 8px; color: #929ba8;')
        form.addWidget(self.preview)
        from prism.widgets.material_render import MaterialRender
        self.inline_render = MaterialRender()
        self.inline_render.setFixedHeight(190)
        self.inline_render.hide()
        form.addWidget(self.inline_render)
        self.preview_status = plain_label()
        self.preview_status.setProperty('uiRole', 'secondary')
        form.addWidget(self.preview_status)
        buttons = QtWidgets.QHBoxLayout()
        self.render_button = QtWidgets.QPushButton('重试预览')
        self.render_button.hide()
        self.open_button = QtWidgets.QPushButton('打开大预览')
        buttons.addWidget(self.render_button)
        buttons.addWidget(self.open_button)
        form.addLayout(buttons)
        self.render_button.clicked.connect(self.render_requested.emit)
        self.open_button.clicked.connect(self.open_requested.emit)
        self.operation_status = plain_label()
        form.addWidget(self.operation_status)
        settings = QtWidgets.QFormLayout()
        self.blend_mode = QtWidgets.QComboBox()
        for text, value in [('自动识别', 'auto'), ('强制不透明', 'opaque'), ('强制透明裁切', 'masked'), ('强制半透明', 'translucent')]:
            self.blend_mode.addItem(text, value)
        self.normal_convention = QtWidgets.QComboBox()
        for value in ('DirectX', 'OpenGL'):
            self.normal_convention.addItem(value, value)
        self.auto_mode = plain_label('自动：分析贴图后确定')
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
        form.addWidget(advanced)
        self.blend_mode.currentIndexChanged.connect(self.settings_requested.emit)
        self.normal_convention.currentIndexChanged.connect(self.settings_requested.emit)
        self.slots = {}
        for key, title in SLOTS.items():
            slot = MaterialSlot(key, title)
            self.slots[key] = slot
            slot.file_requested.connect(self.slot_file_requested.emit)
            slot.channel_requested.connect(self.slot_channel_requested.emit)
            slot.clear_requested.connect(self.slot_clear_requested.emit)
            slot.reuse_requested.connect(self.slot_reuse_requested.emit)
            slot.options_requested.connect(self.slot_options_requested.emit)
            form.addWidget(slot)
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
        form.removeWidget(self.name)
        form.insertWidget(0, self.name)
        self.status = plain_label()
        self.status.setProperty('uiRole', 'secondary')
        form.addWidget(self.status)
        form.addStretch(1)
        self.scroll.setWidget(self.form)
        # Keep the preview visible while scrolling through lower slots.
        self.header = QtWidgets.QWidget()
        header = QtWidgets.QVBoxLayout(self.header)
        header.setContentsMargins(16, 10, 16, 4)
        header.setSpacing(5)
        for widget in (self.name, self.preview, self.inline_render, self.preview_status):
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
        form.addStretch(1)
        self.metadata_scroll = QtWidgets.QScrollArea()
        self.metadata_scroll.setWidgetResizable(True)
        self.metadata_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.metadata_scroll.setWidget(metadata)
        self.sections = QtWidgets.QTabWidget()
        self.sections.addTab(self.scroll, '贴图槽位')
        self.sections.addTab(self.metadata_scroll, '版本与文件')
        layout.addWidget(self.header)
        layout.addWidget(self.sections, 1)
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
        self.shape = QtWidgets.QComboBox()
        for text, key in [('平面','plane'),('球体','sphere'),('圆柱体','cylinder'),('立方体','box')]:
            self.shape.addItem(text,key)
        self.shape.currentIndexChanged.connect(lambda: self.inline_render.set_shape(self.shape.currentData()))
        header.addWidget(self.shape)
        self.name.textEdited.connect(self.edited.emit)
        self.notes.textChanged.connect(self.edited.emit)
        self.show_record(None)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_preview()

    def _fit_preview(self):
        if self._pixmap is not None:
            self.preview.setPixmap(self._pixmap.scaled(
                max(1, self.width() - 36), 186, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

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
            self.inline_render.hide()
            self.preview.show()
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
        if changed_record:
            self.operation_status.clear()
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
                self.preview.setPixmap(QtGui.QPixmap())
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
