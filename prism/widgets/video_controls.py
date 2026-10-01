# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Floating playback controls for video items."""

import logging

from PyQt6 import QtCore, QtWidgets


logger = logging.getLogger(__name__)


def format_time(ms):
    """Format milliseconds as ``m:ss``."""
    total_s = max(0, int(ms // 1000))
    return f'{total_s // 60}:{total_s % 60:02d}'


class BeeVideoControlBar(QtWidgets.QWidget):
    """Play/pause button, seek slider and time label for a video item."""

    REFRESH_INTERVAL = 250  # ms

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.item = item

        style = self.style()
        self._play_icon = style.standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_MediaPlay)
        self._pause_icon = style.standardIcon(
            QtWidgets.QStyle.StandardPixmap.SP_MediaPause)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(10, 3, 10, 3)
        layout.setSpacing(8)

        self.play_button = QtWidgets.QToolButton(self)
        self.play_button.setAutoRaise(True)
        self.play_button.setIcon(self._play_icon)
        self.play_button.setIconSize(QtCore.QSize(16, 16))
        self.play_button.setToolTip('Play / Pause')
        self.play_button.clicked.connect(self.item.toggle_play)
        layout.addWidget(self.play_button)

        self.slider = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal, self)
        self.slider.setRange(0, 1000)
        self.slider.sliderMoved.connect(self.on_slider_moved)
        self.slider.sliderReleased.connect(self.on_slider_released)
        layout.addWidget(self.slider, 1)

        self.time_label = QtWidgets.QLabel('0:00 / 0:00', self)
        layout.addWidget(self.time_label)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(self.REFRESH_INTERVAL)
        self._timer.timeout.connect(self.refresh)

        self.setObjectName('videoControlBar')

        self.update_state(False)

    def showEvent(self, event):
        super().showEvent(event)
        self.refresh()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)

    def on_slider_moved(self, value):
        duration = self.item.duration() or 0
        if duration > 0:
            self.item.seek(int(value / 1000 * duration))

    def on_slider_released(self):
        duration = self.item.duration() or 0
        if duration > 0:
            self.item.seek(int(self.slider.value() / 1000 * duration))

    def refresh(self):
        position = self.item.position()
        duration = self.item.duration() or 0
        # Don't move slider while user is dragging it
        if duration > 0 and not self.slider.isSliderDown():
            self.slider.setValue(int(position / duration * 1000))
        self.time_label.setText(
            f'{format_time(position)} / {format_time(duration)}')

    def update_state(self, playing):
        self.play_button.setIcon(
            self._pause_icon if playing else self._play_icon)
        self.refresh()


class PrismVideoControlProxy(QtWidgets.QGraphicsProxyWidget):
    """Attaches the control bar to a video item as a child item."""

    BAR_HEIGHT = 30

    def __init__(self, item):
        super().__init__(item)
        self._item = item
        self._bar = BeeVideoControlBar(item)
        self.setWidget(self._bar)
        self.setAcceptedMouseButtons(QtCore.Qt.MouseButton.LeftButton)
        self.setZValue(10)
        self.reposition()
        self.hide()

    def reposition(self):
        rect = self._item.boundingRect()
        width = max(rect.width(), 80)
        self._bar.setFixedWidth(int(width))
        self.setGeometry(QtCore.QRectF(
            0, rect.height() - self.BAR_HEIGHT, width, self.BAR_HEIGHT))

    def showEvent(self, event):
        self.reposition()
        super().showEvent(event)

    def update_state(self, playing):
        self._bar.update_state(playing)
