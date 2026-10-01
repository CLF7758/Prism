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

"""Colour-filter chip bar: 11 coloured circles + "All" to dim items that
don't belong to the selected colour group."""

import logging

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism.items import COLOR_GROUPS, PrismPixmapItem, PrismVideoItem
from prism.i18n import _
from prism import ui_tokens

logger = logging.getLogger(__name__)

CHIP_SIZE = 24
CHIP_MARGIN = 4
DIM_OPACITY = 0.3


class ColorChip(QtWidgets.QWidget):
    """A single circular colour swatch with an item-count label."""

    clicked = QtCore.pyqtSignal(str)

    def __init__(self, group_id, label, hex_color, parent=None):
        super().__init__(parent)
        self.group_id = group_id
        self.hex_color = hex_color
        self.label_text = label
        self._count = 0
        self._active = False
        self._hover = False
        self.setFixedSize(52, 56)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f'{label} ({self._count})')

    def set_count(self, n):
        self._count = n
        self.setToolTip(f'{self.label_text} ({n})')
        self.update()

    def set_active(self, active):
        self._active = active
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.group_id)

    def enterEvent(self, event):
        self._hover = True
        self.update()

    def leaveEvent(self, event):
        self._hover = False
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        cx = self.width() / 2
        r = CHIP_SIZE / 2

        if self._active:
            painter.setPen(QtGui.QPen(
                QtGui.QColor(ui_tokens.COLOURS_DARK['accent']), 2.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(QtCore.QPointF(cx, r + 2), r + 3, r + 3)

        color = QtGui.QColor(self.hex_color)
        if self._hover and not self._active:
            color = color.lighter(120)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QtGui.QBrush(color))
        painter.drawEllipse(QtCore.QPointF(cx, r + 2), r, r)

        # Chinese label
        painter.setPen(QtGui.QColor(ui_tokens.COLOURS_DARK['text-primary']))
        font = self.font()
        font.setPixelSize(13)
        painter.setFont(font)
        painter.drawText(
            0, CHIP_SIZE + 6, self.width(), 18,
            Qt.AlignmentFlag.AlignCenter,
            self.label_text)

        painter.end()


class ColorFilterBar(QtWidgets.QWidget):
    """Horizontal bar of colour chips that can filter canvas items."""

    filter_changed = QtCore.pyqtSignal()

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self._active_group = None
        self._saved_opacities = {}
        self._grayscale_chip = None
        self._grayscale_active = False
        self.setObjectName('colorFilterBar')
        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(CHIP_MARGIN)

        self.all_chip = ColorChip('all', _('All'), '#888888')
        self.all_chip.clicked.connect(self._on_chip_clicked)
        layout.addWidget(self.all_chip)

        sep = QtWidgets.QFrame()
        sep.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep.setObjectName('filterSeparator')
        sep.setFixedWidth(2)
        layout.addWidget(sep)

        self.chips = {}
        for g in COLOR_GROUPS:
            chip = ColorChip(g['id'], _(g['label']), g['color'])
            chip.clicked.connect(self._on_chip_clicked)
            layout.addWidget(chip)
            self.chips[g['id']] = chip

        sep2 = QtWidgets.QFrame()
        sep2.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        sep2.setObjectName('filterSeparator')
        sep2.setFixedWidth(2)
        layout.addWidget(sep2)

        # Grayscale toggle chip (last in bar)
        self._grayscale_chip = ColorChip(
            'grayscale', _('Grayscale'), '#a0a0a0')
        self._grayscale_chip.clicked.connect(self._on_chip_clicked)
        self._grayscale_chip.setFixedSize(76, 56)
        layout.addWidget(self._grayscale_chip)

        layout.addStretch()

    def recount(self):
        counts = {g['id']: 0 for g in COLOR_GROUPS}
        total = 0
        grayscale_count = 0
        for item in self.view.scene.items():
            if not hasattr(item, 'save_id'):
                continue
            is_pixmap = isinstance(item, PrismPixmapItem)
            is_video = isinstance(item, PrismVideoItem)
            if not (is_pixmap or is_video):
                continue
            # Lazy-analyze color group if not yet set (pixmap only)
            if is_pixmap and not item.color_group:
                item.analyze_color_group()
            if is_pixmap and item.grayscale:
                grayscale_count += 1
            gid = getattr(item, 'color_group', None)
            if gid and gid in counts:
                counts[gid] += 1
                total += 1
        for gid, chip in self.chips.items():
            c = counts[gid]
            chip.set_count(c)
            chip.setVisible(True)  # Keep every color available, including empty green.
        self.all_chip.set_count(total)
        if self._grayscale_chip:
            self._grayscale_chip.set_count(grayscale_count)

    def clear_filter(self):
        self._restore_opacities()
        self._active_group = None
        self._grayscale_active = False
        for chip in self.chips.values():
            chip.set_active(False)
        self.all_chip.set_active(True)
        if self._grayscale_chip:
            self._grayscale_chip.set_active(False)
        self.filter_changed.emit()

    def apply_filter(self, group_id):
        if group_id == 'all':
            self.clear_filter()
            return
        if group_id == 'grayscale':
            self._toggle_grayscale()
            return
        self._save_opacities()
        self._active_group = group_id
        for chip in self.chips.values():
            chip.set_active(chip.group_id == group_id)
        self.all_chip.set_active(False)
        if self._grayscale_chip:
            self._grayscale_chip.set_active(False)
        for item in self.view.scene.items():
            if not hasattr(item, 'save_id'):
                continue
            if isinstance(item, (PrismPixmapItem, PrismVideoItem)):
                item_color = getattr(item, 'color_group', None)
                if item_color != group_id:
                    item.setOpacity(DIM_OPACITY)
                else:
                    orig = self._saved_opacities.get(id(item), 1.0)
                    item.setOpacity(orig)
        self.filter_changed.emit()

    def _on_chip_clicked(self, group_id):
        if group_id == 'grayscale':
            if self._grayscale_active:
                self._toggle_grayscale()  # turn off
            else:
                self._toggle_grayscale()  # turn on
            return
        if group_id == 'all' or group_id == self._active_group:
            self.clear_filter()
        else:
            self.apply_filter(group_id)

    def _toggle_grayscale(self):
        """Toggle grayscale on all pixmap items."""
        self._grayscale_active = not self._grayscale_active
        for item in self.view.scene.items():
            if isinstance(item, PrismPixmapItem) and hasattr(item, 'grayscale'):
                item.grayscale = self._grayscale_active
        if self._grayscale_chip:
            self._grayscale_chip.set_active(self._grayscale_active)
        self.filter_changed.emit()

    def _save_opacities(self):
        if self._saved_opacities:
            return
        for item in self.view.scene.items():
            if hasattr(item, 'save_id'):
                self._saved_opacities[id(item)] = item.opacity()

    def _restore_opacities(self):
        for item in self.view.scene.items():
            orig = self._saved_opacities.pop(id(item), None)
            if orig is not None:
                item.setOpacity(orig)
        self._saved_opacities.clear()
