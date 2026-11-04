"""Persistent page-owned thumbnail sizing control, outside title-bar stacks."""
from PyQt6 import QtWidgets


def thumbnail_zoom_bar(slider):
    bar=QtWidgets.QWidget()
    bar.setObjectName('thumbnailZoomBar')
    bar.setFixedHeight(32)
    row=QtWidgets.QHBoxLayout(bar);row.setContentsMargins(8,2,8,2);row.setSpacing(8)
    row.addStretch()
    smaller=QtWidgets.QToolButton();smaller.setText(chr(0x2212));smaller.setToolTip('缩小缩略图')
    larger=QtWidgets.QToolButton();larger.setText('+');larger.setToolTip('放大缩略图')
    for button in (smaller,larger):
        button.setFixedSize(24,24)
        button.setStyleSheet('QToolButton{min-width:0;min-height:0;padding:0;border:0;}')
    smaller.clicked.connect(lambda:slider.setValue(slider.value()-12))
    larger.clicked.connect(lambda:slider.setValue(slider.value()+12))
    slider.setFixedWidth(140);slider.setMinimumHeight(22)
    slider.setToolTip('缩略图大小 / Ctrl + 滚轮')
    slider.setStyleSheet('QSlider::groove:horizontal{height:3px;background:#656565;}'
        'QSlider::handle:horizontal{width:10px;margin:-4px 0;background:#efefef;border-radius:5px;}')
    row.addWidget(smaller);row.addWidget(slider);row.addWidget(larger)
    bar.smaller=smaller;bar.larger=larger
    return bar
