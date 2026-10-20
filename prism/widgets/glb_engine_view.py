"""Babylon's official glTF loader/viewer, preserving the original PBR materials."""
import tempfile
from pathlib import Path

from PyQt6 import QtCore, QtWidgets

from prism.widgets.material_render import MaterialRender


class GLBEngineWindow(QtWidgets.QDialog):
    def __init__(self, *, blob=None, path=None, filename='', parent=None):
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(filename or 'GLB / glTF 预览')
        self.resize(900, 720)
        self._temp = None
        if blob:
            self._temp = tempfile.TemporaryDirectory(prefix='prism-glb-')
            extension = '.gltf' if filename.lower().endswith('.gltf') else '.glb'
            source = Path(self._temp.name) / ('model' + extension)
            source.write_bytes(blob)
        elif path and Path(path).is_file():
            source = Path(path).resolve()
        else:
            raise ValueError('原始模型文件不存在')
        layout = QtWidgets.QVBoxLayout(self)
        self.viewer = MaterialRender(self)
        layout.addWidget(self.viewer, 1)
        self.status = QtWidgets.QLabel('使用 Babylon.js 官方 glTF 加载器读取原始材质…')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        reset = QtWidgets.QPushButton('回到正面')
        reset.clicked.connect(self.viewer.reset_camera)
        layout.addWidget(reset)
        self.viewer.failed.connect(lambda error: self.status.setText('模型加载失败：' + error))
        self.viewer.model_loaded.connect(lambda: self.status.setText('拖动旋转，滚轮缩放。材质由 Babylon.js 官方 glTF 加载器读取。'))
        self.viewer.load_material({'model':source.as_uri(), 'signature':'glb'}, 'glb')
