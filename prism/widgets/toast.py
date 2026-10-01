"""可点的提示条（§6 Toast / §7.6 撤销 8 秒）。

`PrismNotification` 是个自动消失的纯文字提示（鼠标都能穿透），用在"已保存"
这种不需要回应的场合。这里这条不一样：它带一个「撤销」按钮，用户得能在
8 秒内点到它 —— 所以它**接受鼠标事件**，并且只有按钮被点或超时才会消失。

尺寸和落点按规范 §6：宽随内容 240–440、高至少 40、圆角 8、底部居中、
距状态栏 16 px（`ui_tokens.COMPONENTS` 里那三个 toast-* 就是这几个数）。
"""
from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism import ui_tokens


class UndoToast(QtWidgets.QWidget):
    """底部居中的一条提示，带一个「撤销」按钮。

    ``on_undo`` 在按钮被点时调用一次，然后自己消失；超时（默认 8 秒）也一样
    消失，只是不调回调。§505：常规 Toast 4 秒，**撤销 8 秒**。
    """

    #: 撤销类提示的存活时间（§7.6 / §505）
    UNDO_MS = 8000
    #: 普通提示（没有撤销按钮）的存活时间
    PLAIN_MS = 4000

    def __init__(self, parent, text, on_undo=None, *, timeout_ms=None,
                 undo_label='撤销'):
        super().__init__(parent)
        self._on_undo = on_undo
        self._done = False
        self.setObjectName('PrismToast')
        # 这条提示要能点，所以**不能**像 PrismNotification 那样让鼠标穿透。
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(12)
        self.label = QtWidgets.QLabel(text)
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)

        self.undo_button = None
        if on_undo is not None:
            self.undo_button = QtWidgets.QPushButton(undo_label)
            self.undo_button.setObjectName('PrismToastUndo')
            self.undo_button.setAutoDefault(False)
            self.undo_button.clicked.connect(self.undo)
            layout.addWidget(self.undo_button, 0)

        self.setMinimumSize(
            ui_tokens.COMPONENTS['toast-min-width'],
            max(ui_tokens.COMPONENTS['toast-min-height'],
                self.sizeHint().height()))
        self.setMaximumWidth(ui_tokens.COMPONENTS['toast-max-width'])

        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(
            timeout_ms if timeout_ms is not None
            else (self.UNDO_MS if on_undo is not None else self.PLAIN_MS))
        self._timer.timeout.connect(self.dismiss)

    # ── 生命周期 ──────────────────────────────────────────────────

    def show_at_bottom(self):
        """显示到底部居中（先 show 再量宽度，隐藏时宽高都是 0）。"""
        parent = self.parentWidget()
        self.show()
        self.adjustSize()
        if parent is not None:
            x = (parent.width() - self.width()) // 2
            y = max(0, parent.height() - self.height() -
                    ui_tokens.COMPONENTS['toast-bottom-margin'])
            self.move(int(x), int(y))
        self.raise_()
        self._timer.start()
        return self

    def undo(self):
        """点「撤销」：先回调，再消失。只生效一次。"""
        if self._done:
            return
        self._done = True
        callback, self._on_undo = self._on_undo, None
        self.dismiss()
        if callback is not None:
            callback()

    def dismiss(self):
        self._done = True
        self._timer.stop()
        self.hide()
        self.deleteLater()

    def is_alive(self):
        return not self._done
