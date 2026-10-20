"""Vertical material picker using vcolorpicker's MIT HSV/SV controls."""
from PyQt6 import QtCore, QtGui, QtWidgets
from prism.vendor.vcolorpicker import ColorPicker, hsv2rgb


class MaterialColorDialog(QtWidgets.QDialog):
    def __init__(self, rgb, parent=None):
        super().__init__(parent)
        self.setWindowTitle('选择材质颜色')
        self.setFixedWidth(290)
        self.setStyleSheet('QDialog {background:#232326;color:#eee;} '
            'QLabel {color:#eee;background:transparent;} '
            'QPushButton,QToolButton {min-width:0px;padding:4px 8px;} '
            'QLineEdit {min-width:0px;background:#19191b;color:#eee;padding:4px;}')
        self.core = ColorPicker(lightTheme=False,useAlpha=False)
        self.core.hide()
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(24,16,24,20)
        layout.setSpacing(14)
        title=QtWidgets.QLabel('选择材质颜色')
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)
        # The upstream square supplies the gradients, selector, mouse handling
        # and color conversion. Reparent it; do not reimplement a color picker.
        layout.addWidget(self.core.ui.color_view,0,QtCore.Qt.AlignmentFlag.AlignHCenter)
        self.hue=QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.hue.setRange(0,1000)
        self.hue.setStyleSheet('QSlider::groove:horizontal {height:12px;border-radius:6px;background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 red,stop:0.167 yellow,stop:0.333 lime,stop:0.5 cyan,stop:0.667 blue,stop:0.833 magenta,stop:1 red);} QSlider::sub-page:horizontal{background:transparent;}')
        layout.addWidget(self.hue)
        row=QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('HEX'))
        row.addWidget(self.core.ui.hex,1)
        self.copy=QtWidgets.QToolButton();self.copy.setText('复制')
        self.copy.clicked.connect(lambda: QtWidgets.QApplication.clipboard().setText('#'+self.core.ui.hex.text()))
        row.addWidget(self.copy)
        layout.addLayout(row)
        swatches=QtWidgets.QHBoxLayout()
        for value in ('#ff0000','#ffff00','#00ff00','#00ffff','#0000ff','#ffffff','#808080','#000000'):
            button=QtWidgets.QToolButton();button.setFixedSize(22,22)
            button.setStyleSheet('background:'+value+';border:1px solid #666;padding:0;')
            button.clicked.connect(lambda checked=False,value=value:self.set_color(QtGui.QColor(value)))
            swatches.addWidget(button)
        layout.addLayout(swatches)
        controls=QtWidgets.QDialogButtonBox()
        controls.addButton('应用',QtWidgets.QDialogButtonBox.ButtonRole.AcceptRole)
        controls.addButton('取消',QtWidgets.QDialogButtonBox.ButtonRole.RejectRole)
        controls.accepted.connect(self.accept);controls.rejected.connect(self.reject)
        layout.addWidget(controls)
        self.hue.valueChanged.connect(self._hue_changed)
        self.core.ui.hex.textEdited.connect(self._sync)
        self.set_color(QtGui.QColor.fromRgbF(*rgb))

    def set_color(self,color):
        self.core.setRGB((color.red(),color.green(),color.blue()))
        self.core.rgbChanged()
        self._sync()

    def _sync(self,*args):
        with QtCore.QSignalBlocker(self.hue):
            self.hue.setValue(round(self.core.color[0]*10))

    def _hue_changed(self,value):
        _,s,v=self.core.color
        self.core.setHSV((value/10,s,v))
        self.core.hsvChanged()

    def selected_rgb(self):
        return [min(1.,max(0.,v/255)) for v in hsv2rgb(self.core.color)]

    def done(self,result):
        self.core.close()
        super().done(result)
