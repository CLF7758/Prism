"""Ephemeral scene widget, deliberately excluded from saved assets and layouts."""
from PyQt6 import QtCore, QtWidgets


class DragTitle(QtWidgets.QLabel):
    """Visible drag affordance; the proxy owns the actual scene drag."""
    def __init__(self, node):
        super().__init__('下载视频 · 拖动标题移动')
        self.node = node
        self.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        self.setMinimumHeight(28)


class DownloadNode(QtWidgets.QGraphicsProxyWidget):
    start_requested = QtCore.pyqtSignal()
    cancel_requested = QtCore.pyqtSignal()
    remove_requested = QtCore.pyqtSignal()
    import_requested = QtCore.pyqtSignal()
    available_quality_requested = QtCore.pyqtSignal()
    session_requested = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.dragging = False
        self._drag_anchor = None
        self.setFlag(self.GraphicsItemFlag.ItemIgnoresTransformations)
        self.setFlag(self.GraphicsItemFlag.ItemIsMovable)
        self.setZValue(1_000_000)
        panel = QtWidgets.QFrame()
        panel.setObjectName('downloadNode')
        panel.setFixedWidth(340)
        panel.setStyleSheet('''
            QFrame#downloadNode {background:#25272b; border:1px solid #454952; border-radius:8px;}
            QLabel {color:#e4e6eb; border:0; background:transparent;}
            QLineEdit {color:#e4e6eb; background:#1d1f22; border:1px solid #484d56; border-radius:4px; padding:7px;}
            QPushButton {color:#e4e6eb; background:#343840; border:1px solid #4c5360; border-radius:4px; padding:5px 10px;}
            QPushButton:hover {background:#42669c;}
            QPushButton:disabled {color:#7e838b;}
            QProgressBar {color:#e4e6eb; background:#191b1e; border:0; border-radius:3px; height:8px;}
            QProgressBar::chunk {background:#7baaff; border-radius:3px;}
        ''')
        layout = QtWidgets.QVBoxLayout(panel)
        title = QtWidgets.QHBoxLayout()
        self.title = DragTitle(self)
        title.addWidget(self.title, 1)
        close = QtWidgets.QPushButton('×')
        close.setToolTip('移除节点并取消尚未完成的下载')
        close.setFixedWidth(28)
        close.clicked.connect(self.remove_requested)
        title.addWidget(close)
        layout.addLayout(title)
        self.url = QtWidgets.QLineEdit()
        self.url.setPlaceholderText('粘贴视频链接或分享文字，按回车下载')
        self.url.setMaxLength(8192)
        self.url.returnPressed.connect(self.start_requested)
        layout.addWidget(self.url)
        self.status = QtWidgets.QLabel('后台下载，不影响画布操作')
        self.status.setTextFormat(QtCore.Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        self.status.setMaximumHeight(96)
        layout.addWidget(self.status)
        self.bar = QtWidgets.QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setValue(0)
        self.bar.setTextVisible(False)
        layout.addWidget(self.bar)
        buttons = QtWidgets.QHBoxLayout()
        self.start = QtWidgets.QPushButton('开始下载')
        self.start.clicked.connect(self.start_requested)
        self.cancel = QtWidgets.QPushButton('取消')
        self.cancel.clicked.connect(self.cancel_requested)
        self.cancel.setEnabled(False)
        self.import_button = QtWidgets.QPushButton('放入原画布')
        self.import_button.clicked.connect(self.import_requested)
        self.import_button.hide()
        buttons.addWidget(self.start)
        buttons.addWidget(self.cancel)
        buttons.addWidget(self.import_button)
        layout.addLayout(buttons)
        self.available_quality = QtWidgets.QPushButton()
        self.available_quality.clicked.connect(self.available_quality_requested)
        self.available_quality.hide()
        layout.addWidget(self.available_quality)
        self.session_button = QtWidgets.QPushButton('提供 B站会话，获取原分辨率…')
        self.session_button.clicked.connect(self.session_requested)
        self.session_button.hide()
        layout.addWidget(self.session_button)
        self.url.textChanged.connect(self.clear_quality_options)
        self.setWidget(panel)

    def clear_quality_options(self):
        self.available_quality.hide()
        self.session_button.hide()

    def quality_limited(self, available):
        width, height = available.get('width'), available.get('height')
        if not isinstance(width, int) or not isinstance(height, int) or min(width,height) <= 0:
            return
        self.available_quality.setText(f'下载当前 {width}×{height}（不压缩）')
        self.available_quality.show()
        self.session_button.show()
        self.widget().adjustSize()

    def mousePressEvent(self, event):
        if event.button() == QtCore.Qt.MouseButton.LeftButton and self.title.geometry().contains(event.pos().toPoint()):
            self._drag_anchor = event.screenPos()
            self._drag_origin = QtCore.QPointF(self.pos())
            self.dragging = True
            self.title.setCursor(QtCore.Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_anchor is not None and self.scene():
            view = self.scene().views()[0]
            current = view.mapToScene(view.viewport().mapFromGlobal(event.screenPos()))
            start = view.mapToScene(view.viewport().mapFromGlobal(self._drag_anchor))
            self.setPos(self._drag_origin+current-start)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self._drag_anchor is not None:
            self._drag_anchor = None
            self.dragging = False
            self.title.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def itemChange(self, change, value):
        if change == self.GraphicsItemChange.ItemVisibleHasChanged and not value:
            self._drag_anchor = None
            self.dragging = False
            if hasattr(self, 'title'):
                self.title.setCursor(QtCore.Qt.CursorShape.OpenHandCursor)
        return super().itemChange(change, value)

    def busy(self, value):
        self.url.setEnabled(not value)
        self.start.setEnabled(not value)
        self.cancel.setEnabled(value)
        if value:
            self.clear_quality_options()
            self.import_button.hide()
            self.bar.setRange(0, 0)
            self.status.setText('排队中…')

    def progress(self, data):
        phase = data.get('phase')
        if phase == 'session':
            self.status.setText('正在连接视频平台…')
            self.bar.setRange(0, 0)
            return
        text = {'starting':'正在启动…', 'inspect':'正在解析视频…',
                'download':'正在下载', 'postprocess':'正在合并音视频…', 'verify':'正在检查视频…', 'store':'正在保留视频原件…'}.get(phase, '正在处理…')
        total, done = data.get('total'), data.get('downloaded', 0)
        if phase == 'download' and total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(min(1000, int(1000*done/total)))
            text += f' {100*done/total:.1f}%'
        else:
            self.bar.setRange(0, 0)
        if data.get('tracks', 1) > 1:
            text += f" · 音视频流 {data['track']}/{data['tracks']}"
        self.status.setText(text)

    def terminal(self, message, success=False):
        self.busy(False)
        self.status.setText(message)
        self.status.setToolTip(message)
        self.bar.setRange(0, 1000)
        self.bar.setValue(1000 if success else 0)
        self.start.setText('重新下载')
