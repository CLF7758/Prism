"""Large-preview UI shell.

The shell is deliberately free of engine and file IO. It is instantiated in
the preview helper process, where AI 2 creates the native widget and calls
:meth:`ModelPreviewWindow.install_viewport`. ``install_viewport`` is **not** a
cross-process interface: no ``QWidget`` ever crosses a process boundary. The
main process drives the preview through the preview port and receives mode,
camera, cover and close events over IPC.

The shell must work without any of the main window's global state, because
the helper process has none. Everything it needs arrives through
:meth:`set_capabilities` and :meth:`install_viewport`.
"""
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

import logging

from prism.model_library.contracts import ModelCapabilities, ModelViewMode

#: The toolbar buttons that stay disabled until a real capability unlocks them.
#: "纯线框" is not the native show_edges mode: it is only offered when the
#: backend proved it can draw a true wireframe.
MODE_LABELS = (
    ('完整材质', ModelViewMode.MATERIAL),
    ('颜色', ModelViewMode.BASECOLOR),
    ('法线', ModelViewMode.NORMAL),
    ('粗糙度', ModelViewMode.ROUGHNESS),
    ('金属度', ModelViewMode.METALLIC),
    ('AO', ModelViewMode.AO),
    ('透明遮罩 / 透明度', ModelViewMode.OPACITY),
    ('自发光', ModelViewMode.EMISSIVE),
    ('SSS', ModelViewMode.SSS),
    ('置换', ModelViewMode.DISPLACEMENT),
    ('材质＋边线', ModelViewMode.MATERIAL_EDGES),
    ('纯线框', ModelViewMode.WIREFRAME),
)


class ModelPreviewWindow(QtWidgets.QDialog):
    """No engine or file IO. Instantiate in the preview process, not per card."""

    #: The user asked for another view mode. Payload is a ModelViewMode value.
    view_mode_requested = QtCore.pyqtSignal(str)
    #: The user asked to reset the camera.
    reset_requested = QtCore.pyqtSignal()
    #: The user asked to use the current frame as the model cover.
    cover_requested = QtCore.pyqtSignal()
    #: The window is closing; the host releases the native resources.
    close_requested = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        # The preview lives in a helper process, so transient parenting alone
        # does not keep it above the inspector when the main window is clicked.
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle('模型预览')
        self.resize(1000, 780)
        self._capabilities = ModelCapabilities()
        self._channel_reasons = {}
        self._viewport = None
        layout = QtWidgets.QVBoxLayout(self)
        toolbar = QtWidgets.QHBoxLayout()
        self.mode = QtWidgets.QComboBox()
        for label, mode in MODE_LABELS:
            self.mode.addItem(label, mode.value)
        self.reset_button = QtWidgets.QPushButton('回到正面')
        self.cover_button = QtWidgets.QPushButton('设为封面')
        for control in (self.mode, self.reset_button, self.cover_button):
            control.setEnabled(False)
            control.setToolTip('模型预览渲染器尚未就绪')
            toolbar.addWidget(control)
        toolbar.addStretch()
        close = QtWidgets.QPushButton('返回模型库')
        close.clicked.connect(self.close)
        toolbar.addWidget(close)
        layout.addLayout(toolbar)
        self.viewport_host = QtWidgets.QStackedWidget()
        self.placeholder = QtWidgets.QLabel('三维交互预览尚未接入')
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet('background:#111;color:#929ba8;')
        self.viewport_host.addWidget(self.placeholder)
        layout.addWidget(self.viewport_host, 1)
        self.status = QtWidgets.QLabel('等待模型后端')
        self.status.setTextFormat(Qt.TextFormat.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        self.reset_button.clicked.connect(self.reset_requested.emit)
        self.cover_button.clicked.connect(self.cover_requested.emit)
        self._sync_modes(False)

    # ------------------------------------------------------------------
    # Host wiring
    # ------------------------------------------------------------------
    def set_capabilities(self, capabilities):
        """Unlock only what the backend really provides."""
        self._capabilities = capabilities or ModelCapabilities()
        preview = bool(getattr(self._capabilities, 'preview', False))
        wireframe = bool(getattr(self._capabilities, 'wireframe', False))
        self.mode.setEnabled(preview)
        self.reset_button.setEnabled(preview)
        self.cover_button.setEnabled(preview)
        if not preview:
            self.mode.setToolTip('模型预览渲染器尚未就绪')
            self.reset_button.setToolTip('模型预览渲染器尚未就绪')
            self.cover_button.setToolTip('模型预览渲染器尚未就绪')
        else:
            self.mode.setToolTip('')
            self.reset_button.setToolTip('')
            self.cover_button.setToolTip('')
        self._sync_modes(wireframe)
        self.show_mode(self.mode.currentData())
        self.status.setText('等待模型后端' if not preview else '预览已就绪')

    def _sync_modes(self, wireframe):
        for index in range(self.mode.count()):
            value = self.mode.itemData(index)
            reason = ''
            if value == 'wireframe' and not wireframe:
                reason = '原生运行时未实现纯线框'
            elif value not in ('material', 'material_edges', 'wireframe'):
                reason = self._channel_reasons.get(value, '模型通道正在读取')
            item = self.mode.model().item(index)
            if item is not None:
                item.setEnabled(not reason)
                item.setToolTip(reason)

    def set_available_channels(self, reasons):
        self._channel_reasons = dict(reasons)
        self._sync_modes(self._capabilities.wireframe)

    def show_mode(self, value):
        with QtCore.QSignalBlocker(self.mode):
            self.mode.setCurrentIndex(self.mode.findData(value))
        channel = value not in ('material', 'material_edges')
        self.cover_button.setEnabled(self._capabilities.preview and not channel)
        self.cover_button.setToolTip('返回完整材质后再设为封面' if channel else '')

    def install_viewport(self, widget):
        """Install the native viewport widget.

        Called in the preview process only, right after the native widget
        was created. The widget is re-parented into this shell; it is never
        sent across a process boundary.
        """
        if widget is None:
            self.viewport_host.setCurrentWidget(self.placeholder)
            self._viewport = None
            return None
        if not isinstance(widget, QtWidgets.QWidget):
            raise TypeError('install_viewport 只接受同进程的 QWidget')
        self._viewport = widget
        self.viewport_host.addWidget(widget)
        self.viewport_host.setCurrentWidget(widget)
        self.placeholder.hide()
        return widget

    @property
    def viewport(self):
        """The installed native widget, or ``None``."""
        return self._viewport

    def set_status(self, text):
        self.status.setText(text)

    def _mode_changed(self, index):
        value = self.mode.itemData(index)
        if value:
            self.view_mode_requested.emit(value)

    def closeEvent(self, event):
        # The host owns the native runtime; it must be told that the window
        # is going away so the resources are released exactly once.
        self.close_requested.emit()
        super().closeEvent(event)


class PreviewWindowKeeper(QtCore.QObject):
    """Keep a preview window alive when the user closes it.

    The shell has ``WA_DeleteOnClose``: a closed window is deleted, and with
    it the native widget inside it.  A host that wants to keep its native
    runtime - and the OpenGL context that belongs to it - installs this
    filter instead of letting the close happen: the window is hidden, the
    host is told, and the next request can show the same window again.

    The helper process is the reason: it costs about 2.7 s to start, so a
    preview that ends the process makes the next preview pay that again.
    """

    def __init__(self, shell, on_closed):
        super().__init__(shell)
        self.shell = shell
        self.on_closed = on_closed
        shell.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() != QtCore.QEvent.Type.Close:
            return False
        # The window must not be deleted, only hidden.
        event.ignore()
        obj.hide()
        try:
            self.on_closed()
        except Exception:                     # pragma: no cover - defensive
            logging.getLogger(__name__).exception('预览窗口关闭后的清理失败')
        return True
