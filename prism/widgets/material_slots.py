"""Desktop file drops and per-slot channel selection; no parameter sliders."""
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.material_textures import CHANNELS


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
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, '选择原始贴图', '', '贴图 (*.png *.jpg *.jpeg *.tif *.tiff *.tga *.bmp *.webp);;所有文件 (*)')
        if path:
            self.file_requested.emit(path)

    def dragEnterEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            event.acceptProposedAction()

    def dropEvent(self, event):
        urls = event.mimeData().urls()
        if len(urls) == 1 and urls[0].isLocalFile():
            self.file_requested.emit(urls[0].toLocalFile())
            event.acceptProposedAction()


class MaterialSlot(QtWidgets.QWidget):
    file_requested = QtCore.pyqtSignal(str, str)
    channel_requested = QtCore.pyqtSignal(str, str)
    clear_requested = QtCore.pyqtSignal(str)
    reuse_requested = QtCore.pyqtSignal(str)
    options_requested = QtCore.pyqtSignal(str, object)

    def __init__(self, key, title):
        super().__init__()
        from prism.material_parameters import RANGES
        self.key = key
        self.setStyleSheet('QToolButton {min-width:0px; padding:1px 3px;} '
            'QPushButton#materialTextureDrop {min-width:32px; max-width:32px; min-height:32px; max-height:32px; padding:0px;} '
            'QDoubleSpinBox {min-width:55px; max-width:55px; padding:1px 3px;}')
        self._binding = {}
        self._had_binding = False
        self._showing = False
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 4)
        layout.setSpacing(3)
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
        self.body = QtWidgets.QWidget()
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
        channel_row = QtWidgets.QHBoxLayout()
        channel_row.addWidget(QtWidgets.QLabel('通道'))
        self.channel = QtWidgets.QComboBox()
        for value in CHANNELS:
            self.channel.addItem('整张 / RGB' if value == 'RGB' else value, value)
        channel_row.addWidget(self.channel, 1)
        self.reuse = QtWidgets.QToolButton()
        self.reuse.setText('复用')
        self.reuse.setToolTip('选择本材质已添加的原图')
        channel_row.addWidget(self.reuse)
        body.addLayout(channel_row)
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
        self.amount.setMaximumWidth(78)
        self.amount.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.amount.setKeyboardTracking(False)
        self.amount.setValue(default)
        amount_row.addWidget(self.amount)
        body.addLayout(amount_row)
        tools = QtWidgets.QHBoxLayout()
        self.invert = QtWidgets.QCheckBox('反转')
        self.invert.setToolTip('反转所选通道的数值（1－输入），不修改原图')
        if key == 'normal':
            self.invert.setText('翻转绿通道')
            self.invert.setToolTip('只翻转法线 G 通道；保留 R/B，切换凹凸方向')
        tools.addWidget(self.invert)
        self.color = QtWidgets.QToolButton()
        self.color.setText('颜色')
        self.color.setVisible(key in ('basecolor', 'emissive', 'sss'))
        tools.addWidget(self.color)
        self.reset_color = QtWidgets.QToolButton()
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
        self.midpoint = QtWidgets.QDoubleSpinBox()
        self.midpoint.setRange(0, 1)
        self.midpoint.setDecimals(3)
        self.midpoint.setValue(.5)
        self.midpoint.setButtonSymbols(QtWidgets.QAbstractSpinBox.ButtonSymbols.NoButtons)
        midpoint_row = QtWidgets.QWidget()
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
        self._fold(self.fold.isChecked())
        self.drop.file_requested.connect(lambda path: self.file_requested.emit(key, path))
        self.channel.currentIndexChanged.connect(lambda: self.channel_requested.emit(key, self.channel.currentData()))
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

    def eventFilter(self, watched, event):
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

    def _option(self, **patch):
        if not self._showing:
            self.options_requested.emit(self.key, patch)

    def _amount_changed(self, value):
        low, high = self.amount.minimum(), self.amount.maximum()
        with QtCore.QSignalBlocker(self.slider):
            self.slider.setValue(round((value-low)/(high-low)*1000))
        self._option(**{('factor' if self._binding and self.key in ('roughness','metallic') else 'value'): value})

    def _slider_changed(self, value):
        self.amount.setValue(self.amount.minimum()+(self.amount.maximum()-self.amount.minimum())*value/1000)

    def _choose_color(self):
        from prism.widgets.material_color import MaterialColorDialog
        dialog = MaterialColorDialog(self._color, self)
        if dialog.exec():
            values = dialog.selected_rgb()
            self._set_color(values)
            self._option(color=values)

    def show_binding(self, binding, properties=None):
        from prism.material_parameters import slot_options
        self._showing = True
        try:
            if (self._binding.get('sha256'), self._binding.get('channel')) != (binding.get('sha256'), binding.get('channel')):
                self.drop.setIcon(QtGui.QIcon())
            if binding and not self._had_binding:
                self.fold.setChecked(True)
            self._had_binding = bool(binding)
            self._binding = binding
            options = slot_options(properties or {}, self.key, bool(binding))
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
        finally:
            self._showing = False

    def show_channel_image(self, data):
        image = QtGui.QPixmap()
        if image.loadFromData(data):
            self.drop.setIcon(QtGui.QIcon(image))
