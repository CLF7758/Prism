"""Desktop file drops and per-slot channel selection; no parameter sliders."""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.material_textures import CHANNELS
from prism.library_observability import event as library_event, observed


class TextureDrop(QtWidgets.QPushButton):
    file_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__('拖入图片，或点击选择', parent)
        self.setAcceptDrops(True)
        self.setMinimumHeight(68)
        self.setIconSize(QtCore.QSize(58, 58))
        self.setObjectName('materialTextureDrop')
        self.clicked.connect(self._choose)

    def _choose(self):
        # Keep the Qt slot's fixed arity: clicked(bool) must not leak into a
        # generic decorator and change the original no-argument contract.
        self._choose_observed()

    @observed('texture','file_picker')
    def _choose_observed(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择原始贴图', '', '贴图 (*.png *.jpg *.jpeg *.tif *.tiff *.tga *.bmp *.webp);;所有文件 (*)')
        parent=self.parentWidget()
        library_event('texture','file_picker','accepted' if path else 'cancelled',
                      slot=getattr(parent,'key',None),reason='local_file_chosen' if path else 'dialog_cancelled')
        if path:
            self.file_requested.emit(path)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            path = urls[0].toLocalFile()
            try:
                from prism.performance_trace import record as _record
                import os as _os
                _record('material_slot_drop', file_name=_os.path.basename(path),
                        extension=_os.path.splitext(path)[1].lower())
            except Exception:                      # noqa: BLE001 - logging only
                pass
            self.file_requested.emit(path)
            event.acceptProposedAction()


def configure_material_slot_column(layout):
    """Shared inspector spacing: spare height stays below the slot stack."""
    layout.setSpacing(12)
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)


class MaterialSlot(QtWidgets.QWidget):
    file_requested = QtCore.pyqtSignal(str, str)
    channel_requested = QtCore.pyqtSignal(str, str)
    clear_requested = QtCore.pyqtSignal(str)
    reuse_requested = QtCore.pyqtSignal(str)
    options_requested = QtCore.pyqtSignal(str, object)

    def __init__(self, key, title, parent=None):
        from .widget_construction import finish_construction
        finish_construction(self._construct(key, title, parent))

    def _construct(self, key, title, parent=None):
        super().__init__(parent)
        from prism.material_parameters import RANGES
        self.key = key
        self.setStyleSheet('QToolButton {min-width:0px; padding:1px 3px;} '
            'QPushButton#materialTextureDrop {min-width:32px; max-width:32px; min-height:32px; max-height:32px; padding:0px;} '
            'QDoubleSpinBox {padding:1px 3px;}')
        self._binding = {}
        self._had_binding = False
        self._showing = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 4)
        layout.setSpacing(3)
        # The child layout can run before the parent shrinks the folded slot.
        # Keep its heading at the top even during that old-height paint frame.
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        heading = QtWidgets.QHBoxLayout()
        self.fold = QtWidgets.QToolButton()
        self.fold.setText(title)
        self.fold.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.fold.setArrowType(Qt.ArrowType.DownArrow)
        self.fold.setCheckable(True)
        self.fold.setStyleSheet('QToolButton, QToolButton:checked {background:transparent; border:0; min-width:0px; padding:1px;}')
        self.fold.setChecked(key in ('basecolor', 'normal', 'roughness'))
        heading.addWidget(self.fold, 1)
        self.enabled = QtWidgets.QCheckBox('启用')
        self.enabled.setChecked(True)
        heading.addWidget(self.enabled)
        layout.addLayout(heading)
        yield
        self.body = QtWidgets.QWidget(self)
        body = QtWidgets.QVBoxLayout(self.body)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(3)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(4)
        self.drop = TextureDrop()
        self.drop.setText('+')
        self.drop.setFixedSize(36, 36)
        self.drop.setMinimumHeight(36)
        self.drop.setIconSize(QtCore.QSize(32, 32))
        row.addWidget(self.drop)
        self.filename = QtWidgets.QLabel('未添加贴图')
        self.filename.setTextFormat(Qt.TextFormat.PlainText)
        self.filename.setMinimumWidth(0)
        self.filename.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored, QtWidgets.QSizePolicy.Policy.Preferred)
        row.addWidget(self.filename, 1)
        self.clear = QtWidgets.QToolButton()
        self.clear.setText('×')
        self.clear.setToolTip('解除贴图关联，保留原文件')
        row.addWidget(self.clear)
        body.addLayout(row)
        yield
        channel_row = QtWidgets.QHBoxLayout()
        channel_row.addWidget(QtWidgets.QLabel('通道'))
        self.channel = QtWidgets.QComboBox()
        for value in CHANNELS:
            self.channel.addItem('整张 / RGB' if value == 'RGB' else value, value)
            from prism.material_parameters import validate_texture_channel
            try:
                validate_texture_channel(key, value)
            except ValueError:
                self.channel.model().item(self.channel.count()-1).setEnabled(False)
        channel_row.addWidget(self.channel, 1)
        self.reuse = QtWidgets.QToolButton()
        self.reuse.setText('复用')
        self.reuse.setToolTip('选择本材质已添加的原图')
        channel_row.addWidget(self.reuse)
        body.addLayout(channel_row)
        yield
        amount_row = QtWidgets.QHBoxLayout()
        self.amount_label = QtWidgets.QLabel('数值')
        amount_row.addWidget(self.amount_label)
        self.slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(0, 1000)
        amount_row.addWidget(self.slider, 1)
        self.amount = QtWidgets.QDoubleSpinBox()
        low, high, default = RANGES[key]
        self.amount.setRange(low, high)
        self.amount.setDecimals(3)
        self.amount.setSingleStep(.001 if key == 'displacement' else .01)
        self.amount.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.amount.setKeyboardTracking(False)
        self.amount.setValue(default)
        amount_row.addWidget(self.amount)
        body.addLayout(amount_row)
        yield
        tools = QtWidgets.QHBoxLayout()
        self.invert = QtWidgets.QCheckBox('反转')
        self.invert.setToolTip('反转所选通道的数值（1－输入），不修改原图')
        if key == 'normal':
            self.invert.setText('翻转绿通道')
            self.invert.setToolTip('只翻转法线 G 通道；保留 R/B，切换凹凸方向')
        tools.addWidget(self.invert)
        self.color = QtWidgets.QToolButton(self.body)
        self.color.setText('颜色')
        self.color.setVisible(key in ('basecolor', 'emissive', 'sss'))
        tools.addWidget(self.color)
        self.reset_color = QtWidgets.QToolButton(self.body)
        self.reset_color.setText('复原颜色')
        self.reset_color.setToolTip('恢复原始颜色（乘色白色，强度 1）')
        self.reset_color.setVisible(key in ('basecolor','emissive','sss'))
        self.reset_color.clicked.connect(lambda: self._reset_color())
        tools.addWidget(self.reset_color)
        tools.addStretch(1)
        self.flip_x = QtWidgets.QToolButton()
        self.flip_x.setText('↔')
        self.flip_x.setToolTip('仅此槽位水平翻转')
        self.flip_x.setCheckable(True)
        self.flip_x.hide()
        tools.addWidget(self.flip_x)
        self.flip_y = QtWidgets.QToolButton()
        self.flip_y.setText('↕')
        self.flip_y.setToolTip('仅此槽位垂直翻转')
        self.flip_y.setCheckable(True)
        self.flip_y.hide()
        tools.addWidget(self.flip_y)
        body.addLayout(tools)
        yield
        self.midpoint = QtWidgets.QDoubleSpinBox()
        self.midpoint.setRange(0, 1)
        self.midpoint.setDecimals(3)
        self.midpoint.setValue(.5)
        self.midpoint.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        midpoint_row = QtWidgets.QWidget(self.body)
        midpoint_layout = QtWidgets.QHBoxLayout(midpoint_row)
        midpoint_layout.setContentsMargins(0,0,0,0)
        midpoint_layout.addWidget(QtWidgets.QLabel('置换中点'),1)
        midpoint_layout.addWidget(self.midpoint)
        midpoint_row.setVisible(key == 'displacement')
        body.addWidget(midpoint_row)
        layout.addWidget(self.body)
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.HLine)
        layout.addWidget(line)
        self.fold.toggled.connect(self._fold)
        # Initial construction has no previous geometry to repair. Forcing
        # activation here polishes the entire unfinished inspector subtree;
        # its final layout will be activated by Qt when the panel is shown.
        expanded = self.fold.isChecked()
        self.body.setVisible(expanded)
        self.fold.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        self.drop.file_requested.connect(lambda path: self.file_requested.emit(key, path))
        self.channel.currentIndexChanged.connect(self._channel_changed)
        self.clear.clicked.connect(lambda: self.clear_requested.emit(key))
        self.reuse.clicked.connect(lambda: self.reuse_requested.emit(key))
        self.enabled.toggled.connect(lambda value: self._option(enabled=value))
        self.invert.toggled.connect(lambda value: self._option(invert=value))
        self.flip_x.toggled.connect(lambda value: self._option(flipX=value))
        self.flip_y.toggled.connect(lambda value: self._option(flipY=value))
        self.midpoint.valueChanged.connect(lambda value: self._option(midpoint=value))
        self.amount.valueChanged.connect(self._amount_changed)
        self.slider.valueChanged.connect(self._slider_changed)
        self.color.clicked.connect(self._choose_color)
        self._color = [1., 1., 1.]
        for widget in (self.fold,self.enabled):
            widget.setFixedHeight(24)
        for widget in (self.channel,self.reuse,self.amount,self.slider,self.invert,
                       self.color,self.reset_color,self.midpoint):
            widget.setFixedHeight(26)
        # Equal top/bottom spacing even for slots without a color swatch.
        for row in (heading, row, channel_row, amount_row, tools):
            row.setContentsMargins(0,0,0,0)
            row.setSpacing(4)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,QtWidgets.QSizePolicy.Policy.Fixed)
        for widget in [self] + self.findChildren(QtWidgets.QWidget):
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)
        for widget in (self.amount, self.midpoint):
            widget.ensurePolished()
            self._fit_number(widget)

    @staticmethod
    def _fit_number(widget):
        samples = [widget.locale().toString(value, 'f', widget.decimals())
                   for value in (widget.minimum(), widget.maximum(), widget.value())]
        width = max(widget.fontMetrics().horizontalAdvance(widget.prefix() + text + widget.suffix())
                    for text in samples)
        option = QtWidgets.QStyleOptionSpinBox()
        widget.initStyleOption(option)
        size = widget.style().sizeFromContents(QtWidgets.QStyle.ContentsType.CT_SpinBox,
            option, QtCore.QSize(width + 12, widget.fontMetrics().height()), widget)
        widget.setFixedWidth(max(widget.minimumSizeHint().width(), size.width() + 8))

    def eventFilter(self, watched, event):
        if (event.type() in (QtCore.QEvent.Type.FontChange, QtCore.QEvent.Type.StyleChange,
                             QtCore.QEvent.Type.LocaleChange, QtCore.QEvent.Type.DevicePixelRatioChange)
                and watched in (self.amount, self.midpoint)):
            self._fit_number(watched)
        if event.type() in (QtCore.QEvent.Type.DragEnter, QtCore.QEvent.Type.DragMove, QtCore.QEvent.Type.Drop):
            urls = event.mimeData().urls()
            if len(urls)==1 and urls[0].isLocalFile():
                event.acceptProposedAction()
                if event.type()==QtCore.QEvent.Type.Drop:
                    self.file_requested.emit(self.key,urls[0].toLocalFile())
                return True
        return super().eventFilter(watched,event)

    def _reset_color(self):
        self._set_color([1.,1.,1.])
        self._option(color=[1.,1.,1.],value=1.)

    def _set_color(self, values):
        self._color = list(values)
        c = QtGui.QColor.fromRgbF(*values)
        self.color.setStyleSheet('background-color:'+c.name()+';color:'+('#111' if c.lightnessF()>.5 else '#fff'))

    def _fold(self, expanded):
        self.body.setVisible(expanded)
        self.fold.setArrowType(Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow)
        # Refresh this child's size constraint before the owner's form reflow.
        # Otherwise its parent can still lay out the old expanded height.
        if expanded:
            self.body.layout().activate()
        self.layout().activate()
        self.updateGeometry()

    def _channel_changed(self):
        self._binding_signature = None
        self.channel_requested.emit(self.key, self.channel.currentData())

    def _option(self, **patch):
        if not self._showing:
            self._binding_signature = None
            self.options_requested.emit(self.key, patch)

    def _amount_changed(self, value):
        with QtCore.QSignalBlocker(self.slider):
            self.slider.setValue(round(max(0., min(1., value))*1000))
        self._option(**{('factor' if self._binding and self.key in ('roughness','metallic') else 'value'): value})

    def _slider_changed(self, value):
        self.amount.setValue(value/1000)

    def _choose_color(self):
        from prism.widgets.material_color import MaterialColorDialog
        dialog = MaterialColorDialog(self._color, self, anchor=self.color)
        try:
            if dialog.exec():
                values = dialog.selected_rgb()
                self._set_color(values)
                self._option(color=values)
        finally:
            dialog.deleteLater()

    def show_binding(self, binding, properties=None):
        from prism.material_parameters import slot_options
        # Preview/status callbacks repeatedly publish identical metadata.
        # Compare values, never the identity of mutable binding dictionaries.
        options = slot_options(properties or {}, self.key, bool(binding))
        from prism.presentation_evidence import fingerprint
        signature = fingerprint((binding, options))
        if getattr(self, '_binding_signature', None) == signature:
            self._binding = binding
            return
        self._showing = True
        try:
            if (self._binding.get('sha256'), self._binding.get('channel')) != (binding.get('sha256'), binding.get('channel')):
                self.drop.setIcon(QtGui.QIcon())
            if binding and not self._had_binding:
                self.fold.setChecked(True)
            self._had_binding = bool(binding)
            self._binding = binding
            with QtCore.QSignalBlocker(self.channel):
                self.channel.setCurrentIndex(max(0,self.channel.findData(binding.get('channel','RGB'))))
            self.channel.setEnabled(bool(binding))
            self.clear.setEnabled(bool(binding))
            self.filename.setText(binding.get('name', '未添加贴图'))
            self.filename.setToolTip((f"{binding['name']}\n{binding['width']} × {binding['height']} · 原文件完整收存" if binding else '从桌面或文件夹拖入原图，也可点击左侧缩略图选择'))
            self.drop.setToolTip(self.filename.toolTip())
            self.drop.setText('' if binding else '+')
            self.enabled.setChecked(options['enabled'])
            self.body.setEnabled(options['enabled'])
            self.invert.setChecked(options['invert'])
            self.flip_x.setChecked(options['flipX'])
            self.flip_y.setChecked(options['flipY'])
            self.midpoint.setValue(options['midpoint'])
            self.amount.setValue(options['amount'])
            self._amount_changed(options['amount'])
            self.amount_label.setText('贴图倍率' if binding and self.key in ('roughness','metallic') else '强度' if self.key not in ('roughness','metallic') else '数值')
            self._color = options['color']
            c=QtGui.QColor.fromRgbF(*self._color)
            self.color.setStyleSheet('background-color:'+c.name()+';color:'+('#111' if c.lightnessF()>.5 else '#fff'))
            self._binding_signature = signature
        finally:
            self._showing = False

    def show_channel_image(self, data):
        image = QtGui.QPixmap()
        if image.loadFromData(data):
            self.drop.setIcon(QtGui.QIcon(image))
