import os, sys, faulthandler
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
faulthandler.enable()
if sys.platform == 'win32':
    import ctypes
    ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.workspace_service import ResourceTreeService
from prism.widgets.resource_tree_model import ResourceTreeModel
from prism.widgets.resource_tree_view import ResourceTreeView
app = QtWidgets.QApplication(['startup-paint-probe'])
root = Path(__file__).resolve().parents[1]
qss = (root/'prism/assets/theme.qss').read_text(encoding='utf-8')
qss = qss.replace('url(assets/icons/', 'url('+(root/'prism/assets/icons').as_posix()+'/')
app.setStyleSheet(qss)
service = ResourceTreeService('startup-paint-probe')
for i in range(80):
    service.createFolder('canvas', 'Folder '+str(i))
tree = ResourceTreeView()
tree.setModel(ResourceTreeModel(service))
frame = tree
tree.expand(tree.model()._index_for('canvas'))
frame.resize(350, 650)
frame.ensurePolished()
tree.doItemsLayout()
image = QtGui.QImage(frame.size(), QtGui.QImage.Format.Format_ARGB32)
image.fill(QtCore.Qt.GlobalColor.transparent)
for _ in range(3):
    frame.render(image)
tree.verticalScrollBar().setValue(tree.verticalScrollBar().maximum() // 2)
frame.render(image)
assert not tree._sticky_section.isHidden()
frame.render(image)
print('PAINT_OK', flush=True)
frame.close()
