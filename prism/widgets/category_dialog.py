"""给选中的素材分配分类。

从 `view.py` 搬出来的：那段代码在 `PrismGraphicsView` 里建了一个 43 行的
对话框，混着界面构建和业务判断，而且写了两处**硬编码中文**
（源码里是 ``'\u5206\u7c7b'`` 这样的字面转义）—— 那两处不走翻译表，
和工程里其它地方的界面文字不一致。

这一层只负责"问用户要哪些分类"：它不推撤销栈、不碰素材，`chosen()` 把
结果交回给调用方。所以能脱离窗口单独测。
"""
import logging

from PyQt6 import QtCore, QtWidgets

from prism.i18n import _

logger = logging.getLogger(__name__)


class CategoryAssignmentDialog(QtWidgets.QDialog):
    """一个可勾选的分类列表。

    勾选状态来自 ``current``，确认后 ``chosen()`` 给出全部被勾中的名字
    （不只是变化的那几个）—— 调用方要的是最终状态，而撤销栈记的是
    "改成什么"，不是"改了什么"。
    """

    def __init__(self, available, current=(), parent=None):
        super().__init__(parent)
        self.setWindowTitle(_('Assign categories'))

        layout = QtWidgets.QVBoxLayout(self)
        self._list = QtWidgets.QListWidget()
        marked = {str(name) for name in (current or ())}
        # 先收进集合再去掉空白：str(None) 是 'None'，不是空字符串，直接
        # 判真值会把它当成一个叫 "None" 的分类列进来。strip() 之后再判，
        # 全空白的名字也一并丢掉。
        names = set()
        for name in available or ():
            if name is None:
                continue
            text = str(name).strip()
            if text:
                names.add(text)
        for name in sorted(names):
            entry = QtWidgets.QListWidgetItem(name)
            entry.setFlags(entry.flags()
                           | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(
                QtCore.Qt.CheckState.Checked if name in marked
                else QtCore.Qt.CheckState.Unchecked)
            self._list.addItem(entry)
        layout.addWidget(self._list)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def chosen(self):
        """当前勾选的名字，按列表顺序。"""
        names = []
        for index in range(self._list.count()):
            entry = self._list.item(index)
            if entry.checkState() == QtCore.Qt.CheckState.Checked:
                names.append(entry.text())
        return names

    def set_checked(self, name, checked=True):
        """按名字设置勾选状态。给测试和调用方用。"""
        for index in range(self._list.count()):
            entry = self._list.item(index)
            if entry.text() == name:
                entry.setCheckState(
                    QtCore.Qt.CheckState.Checked if checked
                    else QtCore.Qt.CheckState.Unchecked)
                return True
        return False
