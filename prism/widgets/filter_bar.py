"""中央顶部的 36 px 筛选条（§8.3）。

规范那一条：**启用后中央出现 36 px 条：可关闭 Chip、结果数、"清除筛选"；
顶部必须显示"范围：当前画布"／"当前文件夹"。**
还有一条陷阱：**「清除恢复全部当前范围，不能偷偷切换页面」** —— 所以这个条的
"清除筛选"只清筛选，不碰当前页面、不碰左栏的选中。

它自己不持有筛选状态：状态在左栏那套（标签 / 评分 / 未标记）和颜色宿主里，
这里只是把它们**显示出来**，并把用户的关闭动作转回那边。
"""
import logging

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism import ui_tokens

logger = logging.getLogger(__name__)

#: 条高（§8.3）
BAR_HEIGHT = 36


class _Chip(QtWidgets.QFrame):
    """一个可关闭的筛选条件。"""

    removed = QtCore.pyqtSignal()

    def __init__(self, text, parent=None):
        super().__init__(parent)
        self.setObjectName('filterChip')
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 2, 4, 2)
        row.setSpacing(4)
        label = QtWidgets.QLabel(text)
        row.addWidget(label)
        close = QtWidgets.QToolButton()
        close.setText('×')
        close.setToolTip('去掉这个条件')
        close.setFixedSize(16, 16)
        close.clicked.connect(self.removed.emit)
        row.addWidget(close)


class FilterBar(QtWidgets.QWidget):
    """范围 + 生效条件 + 结果数 + 清除筛选 + 打开弹层。"""

    clear_requested = QtCore.pyqtSignal()
    open_requested = QtCore.pyqtSignal()
    chip_removed = QtCore.pyqtSignal(str, str)

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.setObjectName('filterBar')
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet('QWidget#filterBar { background: %s; border-bottom: 1px solid %s; }' % (
            ui_tokens.COLOURS_DARK['toolbar-bg'], ui_tokens.COLOURS_DARK['border-subtle']))
        self.setFixedHeight(BAR_HEIGHT)
        self._scope = '当前画布'
        self._setup_ui()

    def _setup_ui(self):
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(8, 0, 8, 0)
        row.setSpacing(8)

        self.scope_label = QtWidgets.QLabel(f'范围：{self._scope}')
        self.scope_label.setObjectName('filterScope')
        row.addWidget(self.scope_label)

        self.chips_host = QtWidgets.QWidget()
        self.chips_row = QtWidgets.QHBoxLayout(self.chips_host)
        self.chips_row.setContentsMargins(0, 0, 0, 0)
        self.chips_row.setSpacing(6)
        row.addWidget(self.chips_host)

        row.addStretch()

        self.count_label = QtWidgets.QLabel('')
        self.count_label.setObjectName('filterCount')
        row.addWidget(self.count_label)

        self.clear_button = QtWidgets.QPushButton('清除筛选')
        self.clear_button.setToolTip('只清筛选，不会切换当前页面')
        self.clear_button.clicked.connect(self.clear_requested.emit)
        self.clear_button.setFixedHeight(ui_tokens.COMPONENTS['chip-height'])
        row.addWidget(self.clear_button)

        self.filter_button = QtWidgets.QPushButton('筛选')
        self.filter_button.setCheckable(True)
        self.filter_button.clicked.connect(
            lambda _=False: self.open_requested.emit())
        self.filter_button.setMinimumHeight(
            ui_tokens.COMPONENTS['chip-height'])
        row.addWidget(self.filter_button)

    # ── 显示 ──────────────────────────────────────────────────────

    def set_scope(self, scope):
        """「范围：当前画布」／「当前文件夹」（§8.3 要求顶部写明）。"""
        self._scope = scope
        self.scope_label.setText(f'范围：{scope}')

    def set_entries(self, entries):
        """把生效的筛选列成 Chip；每个都能单独去掉。"""
        while self.chips_row.count():
            entry = self.chips_row.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        for field, label in entries:
            chip = _Chip(label, self.chips_host)
            chip.removed.connect(
                lambda f=field, l=label: self.chip_removed.emit(f, l))
            self.chips_row.addWidget(chip)
        self.clear_button.setEnabled(bool(entries))

    def set_count(self, visible):
        """筛选之后还剩多少项。"""
        self.count_label.setText(f'{visible} 项')

    def set_open(self, open_):
        self.filter_button.setChecked(bool(open_))
