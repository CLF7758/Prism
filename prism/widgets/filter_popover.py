"""Compact tag/rating filter panel; colour controls live on the canvas toolbar."""
import logging

from PyQt6 import QtCore, QtWidgets
from PyQt6.QtCore import Qt

from prism import ui_tokens
from prism.widgets.color_filter import COLOR_GROUPS, ColorFilterBar

logger = logging.getLogger(__name__)

#: 弹层宽度（§8.3）
POPOVER_WIDTH = 320


class FilterPopover(QtWidgets.QWidget):
    """按需启用的筛选弹层；不自带模态，由筛选条负责开关。"""

    filters_changed = QtCore.pyqtSignal()

    def __init__(self, view, parent=None, color_host=None):
        super().__init__(parent)
        self.view = view
        self.panel = view.category_panel
        # 颜色筛选的逻辑宿主：不显示，只借它的算法与计数。
        self.color_host = color_host if color_host is not None else ColorFilterBar(view, self)
        self.colour_buttons = {}  # No duplicate colour controls in the popup.
        self.color_host.filter_changed.connect(self._on_color_changed)
        self.setObjectName('filterPopover')
        c = ui_tokens.COLOURS_DARK
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            'QWidget#filterPopover { background: %(overlay-bg)s; border: 1px solid %(border-strong)s; border-radius: 8px; }'
            'QWidget#filterPopover QWidget#filterBody { background: %(overlay-bg)s; }'
            'QWidget#filterPopover QLabel { background: transparent; border: none; }'
            'QWidget#filterPopover QLabel#filterTitle { color: %(text-primary)s; font-size: 15px; font-weight: 600; }'
            'QWidget#filterPopover QLabel#filterSectionTitle { color: %(text-primary)s; font-size: 12px; font-weight: 600; }'
            'QWidget#filterPopover QPushButton { min-width: 0; min-height: 0; padding: 0 8px; font-size: 12px; background: transparent; border: none; color: %(accent)s; }'
            'QWidget#filterPopover QPushButton:hover { background: %(hover-bg)s; }'
            'QWidget#filterPopover QLineEdit, QWidget#filterPopover QComboBox { min-height: 28px; max-height: 28px; padding: 2px 8px; border: 1px solid %(border-subtle)s; border-radius: 6px; background: %(input-bg)s; }'
            'QWidget#filterPopover QCheckBox { font-size: 13px; min-height: 26px; }'
            'QWidget#filterPopover QCheckBox:hover { background: %(hover-bg)s; border-radius: 4px; }'
            'QWidget#filterPopover QScrollArea { background: transparent; border: none; }' % c)
        self.setFixedWidth(POPOVER_WIDTH)
        self._building = False
        self._setup_ui()
        self.refresh()

    def sizeHint(self):
        return QtCore.QSize(POPOVER_WIDTH, 330 + self.tags_scroll.height())

    # ── 界面 ──────────────────────────────────────────────────────

    def _setup_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QtWidgets.QWidget()
        body.setObjectName('filterBody')
        scroll.setWidget(body)
        layout.addWidget(scroll)
        layout = QtWidgets.QVBoxLayout(body)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel('筛选素材')
        title.setObjectName('filterTitle')
        header.addWidget(title)
        header.addStretch()
        self.reset_button = QtWidgets.QPushButton('重置')
        self.reset_button.setFixedHeight(26)
        self.reset_button.clicked.connect(self.clear_all)
        header.addWidget(self.reset_button)
        layout.addLayout(header)
        subtitle = QtWidgets.QLabel('即时生效 · 多个标签需同时包含')
        subtitle.setProperty('uiRole', 'secondary')
        layout.addWidget(subtitle)

        layout.addWidget(self._build_rating_group())
        layout.addWidget(self._build_labels_group())
        layout.addWidget(self._build_untagged_group())
        layout.addStretch()

        self.hint = QtWidgets.QLabel('仅筛选显示结果，不修改素材。颜色请使用画布顶部的色点筛选。')
        self.hint.setWordWrap(True)
        self.hint.setProperty('uiRole', 'secondary')
        layout.addWidget(self.hint)

    def _group(self, title):
        box = QtWidgets.QWidget()
        box.setObjectName('filterGroup')
        inner = QtWidgets.QVBoxLayout(box)
        inner.setContentsMargins(0, 0, 0, 0)
        inner.setSpacing(8)
        label = QtWidgets.QLabel(title)
        label.setObjectName('filterSectionTitle')
        inner.addWidget(label)
        return box, inner

    def _build_labels_group(self):
        box, inner = self._group('标签')

        self.tag_search = QtWidgets.QLineEdit()
        self.tag_search.setPlaceholderText('搜索标签')
        self.tag_search.setClearButtonEnabled(True)
        self.tag_search.setMinimumHeight(
            ui_tokens.COMPONENTS['input-height'])
        self.tag_search.textChanged.connect(self._rebuild_tag_rows)
        inner.addWidget(self.tag_search)

        self.tags_container = QtWidgets.QWidget()
        self.tags_layout = QtWidgets.QVBoxLayout(self.tags_container)
        self.tags_layout.setContentsMargins(0, 0, 0, 0)
        self.tags_layout.setSpacing(2)
        self.tags_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        tags_scroll = QtWidgets.QScrollArea()
        self.tags_scroll = tags_scroll
        tags_scroll.setWidgetResizable(True)
        tags_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        tags_scroll.setFixedHeight(144)
        tags_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tags_scroll.setWidget(self.tags_container)
        inner.addWidget(tags_scroll)
        return box

    def _build_rating_group(self):
        box, inner = self._group('最低评分')
        row = QtWidgets.QHBoxLayout()
        self.rating_combo = QtWidgets.QComboBox()
        self.rating_combo.addItems(['不限评分', '★  1 星及以上', '★★  2 星及以上',
                                    '★★★  3 星及以上', '★★★★  4 星及以上', '★★★★★  5 星'])
        self.rating_combo.currentIndexChanged.connect(self._on_rating_changed)
        row.addWidget(self.rating_combo, 1)
        inner.addLayout(row)
        return box

    def _build_untagged_group(self):
        box, inner = self._group('标记')
        self.untagged_box = QtWidgets.QCheckBox('只看没有标签的素材')
        self.untagged_box.setToolTip(
            '只显示没有任何标签的素材；与其他筛选条件同时生效。')
        self.untagged_box.toggled.connect(self._on_untagged_toggled)
        inner.addWidget(self.untagged_box)
        return box

    # ── 状态 ──────────────────────────────────────────────────────

    def refresh(self):
        """把界面对齐到当前的筛选状态（左栏/别处改过也能跟上）。"""
        self._building = True
        try:
            self.rating_combo.setCurrentIndex(int(self.panel._min_rating))
            self.untagged_box.setChecked(bool(
                getattr(self.panel, '_untagged_only', False)))
            self._rebuild_tag_rows()
            self.color_host.recount()
            for group_id, button in self.colour_buttons.items():
                if group_id == 'grayscale':
                    active = bool(self.color_host._grayscale_active)
                else:
                    active = self.color_host._active_group == group_id
                button.setChecked(active)
        finally:
            self._building = False

    def _known_tags(self):
        names = set(getattr(self.panel.scene, 'tag_names', []) or [])
        names |= set(self.panel.scene.get_all_tags())
        names.discard('')
        return sorted(names, key=str.casefold)

    def _rebuild_tag_rows(self, *_args):
        if not hasattr(self, 'tags_layout'):
            return
        query = self.tag_search.text().strip().casefold()
        active = {str(name).casefold() for name in self.panel._active_tags}
        while self.tags_layout.count():
            entry = self.tags_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        shown = [name for name in self._known_tags()
                 if not query or query in name.casefold()]
        self.tags_scroll.setFixedHeight(max(32, min(144, len(shown) * 28)))
        if self.isVisible():
            self.adjustSize()
        if not shown:
            hint = QtWidgets.QLabel('没有匹配的标签' if query else '还没有标签')
            hint.setProperty('uiRole', 'secondary')
            hint.setWordWrap(True)
            self.tags_layout.addWidget(hint)
            return
        for name in shown:
            box = QtWidgets.QCheckBox(name)
            box.setChecked(name.casefold() in active)
            box.toggled.connect(
                lambda checked, tag=name: self._on_tag_toggled(tag, checked))
            self.tags_layout.addWidget(box)

    # ── 用户操作 ──────────────────────────────────────────────────

    def _on_tag_toggled(self, name, checked):
        if self._building:
            return
        if checked:
            self.panel._active_tags.add(name)
        else:
            self.panel._active_tags.discard(name)
        self._apply()

    def _on_rating_changed(self, index):
        if self._building:
            return
        self.panel._min_rating = int(index)
        # 左栏那条「最低评分」还在（收进弹层要先跟左栏的负责人对齐）：
        # 让它跟上，两边说的是同一件事。
        selector = getattr(self.panel, 'rating_filter', None)
        if selector is not None and selector.currentIndex() != int(index):
            selector.blockSignals(True)
            selector.setCurrentIndex(int(index))
            selector.blockSignals(False)
        self._apply()

    def _on_untagged_toggled(self, checked):
        if self._building:
            return
        self.panel._untagged_only = bool(checked)
        self._apply()

    def _on_colour_clicked(self, group_id):
        if self._building:
            return
        active = (self.color_host._active_group == group_id
                  if group_id != 'grayscale'
                  else self.color_host._grayscale_active)
        self.color_host.apply_filter('all' if active else group_id)

    def _on_color_changed(self):
        if self._building:
            return
        for group_id, button in self.colour_buttons.items():
            if group_id == 'grayscale':
                active = bool(self.color_host._grayscale_active)
            else:
                active = self.color_host._active_group == group_id
            button.setChecked(active)
        self.filters_changed.emit()

    def _apply(self):
        """把筛选交给左栏那套逻辑（它负责可见性），再通知外面刷新。"""
        self.panel._apply_filter()
        self.filters_changed.emit()

    # ── 对外 ──────────────────────────────────────────────────────

    def active_summary(self):
        """现在生效的筛选，给筛选条上的 Chip 用。"""
        entries = []
        for name in sorted(self.panel._active_tags, key=str.casefold):
            entries.append(('tag', name))
        if self.panel._min_rating:
            entries.append(('rating', '★' * int(self.panel._min_rating)
                            + ' 以上'))
        if getattr(self.panel, '_untagged_only', False):
            entries.append(('untagged', '未打标签'))
        group = self.color_host._active_group
        if group:
            label = next((g['label'] for g in COLOR_GROUPS
                          if g['id'] == group), group)
            entries.append(('colour', label))
        if self.color_host._grayscale_active:
            entries.append(('grayscale', '灰度'))
        return entries

    def clear_all(self):
        """清掉这一层里的筛选（标签 / 评分 / 未标记 / 颜色）。"""
        self._building = True
        try:
            self.panel._active_tags.clear()
            self.panel._min_rating = 0
            self.panel._untagged_only = False
            self.color_host.clear_filter()
            selector = getattr(self.panel, 'rating_filter', None)
            if selector is not None:
                with QtCore.QSignalBlocker(selector):
                    selector.setCurrentIndex(0)
            self.rating_combo.setCurrentIndex(0)
            self.untagged_box.setChecked(False)
            self._rebuild_tag_rows()
            for button in self.colour_buttons.values():
                button.setChecked(False)
        finally:
            self._building = False
        self._apply()
