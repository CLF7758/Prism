"""Right sidebar inspector panel (PureRef style).

Shows preview, title, notes, category, file info,
color distribution and histogram for selected item."""

import logging
import os
from datetime import datetime

from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt

from prism import commands
from prism.i18n import _
from prism import tags
from prism.fileio.image import (adjust_display_image,
                                false_color_preview, is_hdr_path,
                                load_image)
from prism.items import (PrismPixmapItem, PrismVideoItem, COLOR_GROUPS,
                         _color_group_from_rgb, _rgb_to_hsl)
from prism.widgets.tag_panel import tag_signals
from prism.widgets import components
from prism.widgets.components.icons import icon


logger = logging.getLogger(__name__)


def _format_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


class DetailPanel(QtWidgets.QWidget):
    """Right sidebar inspector panel."""

    def __init__(self, view, parent=None):
        super().__init__(parent)
        self.view = view
        self.scene = view.scene
        self._item = None
        self._displayed_tags = set()
        self._setup_ui()
        self.scene.selectionChanged.connect(self._on_selection_changed)
        self.scene.metadata_changed.connect(self._refresh)
        # Synonyms, renames and nesting done in the manager change how a
        # tag is shown here, without touching the items.
        tag_signals().changed.connect(self._refresh)

    def _setup_ui(self):
        self.setObjectName('prismInspector')
        self.setMinimumWidth(260)
        self.setMaximumWidth(16777215)

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QtWidgets.QFrame()
        header.setObjectName('inspectorHeader')
        header.setFixedHeight(48)
        header_row = QtWidgets.QHBoxLayout(header)
        header_row.setContentsMargins(16, 0, 16, 0)
        header_row.addWidget(components.RoleLabel('素材检查器', role='sectionTitle'))
        header_row.addStretch()
        self._selection_count = components.RoleLabel(role='secondary')
        header_row.addWidget(self._selection_count)
        outer.addWidget(header)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setStyleSheet(
            'QScrollArea { border: none; background: transparent; }')
        outer.addWidget(scroll, stretch=1)

        body = QtWidgets.QWidget()
        body.setObjectName('inspectorBody')
        self._body_layout = QtWidgets.QVBoxLayout(body)
        self._body_layout.setContentsMargins(16, 16, 16, 16)
        self._body_layout.setSpacing(12)
        scroll.setWidget(body)

        # Preview thumbnail
        self._preview_label = QtWidgets.QLabel()
        self._preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_label.setFixedHeight(160)
        self._preview_label.setObjectName('inspectorPreview')
        self._body_layout.addWidget(self._preview_label)

        # Title
        lbl_title = components.RoleLabel('\u6807\u9898', role='inspectorSection')
        self._body_layout.addWidget(lbl_title)
        self._title_edit = components.LineEdit()
        self._title_edit.editingFinished.connect(
            lambda: self._on_title_changed(self._title_edit.text()))
        self._body_layout.addWidget(self._title_edit)

        # Notes
        lbl_notes = components.RoleLabel('\u5907\u6ce8', role='inspectorSection')
        self._body_layout.addWidget(lbl_notes)
        self._notes_edit = components.TextEdit()
        self._notes_edit.setPlaceholderText('\u6dfb\u52a0\u5907\u6ce8\u2026')
        self._notes_edit.setFixedHeight(100)
        self._notes_edit.textChanged.connect(self._on_notes_changed)
        self._body_layout.addWidget(self._notes_edit)

        # Category dropdown
        lbl_cat = components.RoleLabel('\u5206\u7c7b', role='inspectorSection')
        self._body_layout.addWidget(lbl_cat)
        self._cat_combo = QtWidgets.QComboBox()
        self._cat_combo.currentIndexChanged.connect(
            self._on_category_changed)
        self._body_layout.addWidget(self._cat_combo)
        # Page type replaces the old single-choice category model. Keep the
        # controls alive for old file compatibility, but do not expose them.
        lbl_cat.hide()
        self._cat_combo.hide()

        # Rating (works for one item or a multi-selection)
        lbl_rating = components.RoleLabel(_('Rating'), role='inspectorSection')
        self._body_layout.addWidget(lbl_rating)
        rating_row = QtWidgets.QHBoxLayout()
        rating_row.setSpacing(2)
        self._rating_buttons = []
        for value in range(1, 6):
            button = QtWidgets.QToolButton()
            button.setIcon(icon('star'))
            button.setIconSize(QtCore.QSize(18, 18))
            button.setFixedSize(28, 28)
            button.setProperty('uiRole', 'rating')
            button.setToolTip(_('{value} stars').format(value=value))
            button.setAccessibleName(button.toolTip())
            button.clicked.connect(
                lambda checked=False, rating=value: self._set_rating(rating))
            rating_row.addWidget(button)
            self._rating_buttons.append(button)
        clear_rating = QtWidgets.QToolButton()
        clear_rating.setText(_('Clear'))
        clear_rating.clicked.connect(lambda: self._set_rating(0))
        rating_row.addWidget(clear_rating)
        rating_row.addStretch()
        self._body_layout.addLayout(rating_row)

        # Tagging happens on the selection, browsing happens on the left.
        # The inspector therefore lists the tags the selection carries (and
        # whatever the search box is asking for) instead of the whole
        # catalogue, which in a real project runs into the hundreds.
        tag_header = QtWidgets.QHBoxLayout()
        tag_header.setContentsMargins(0, 0, 0, 0)
        lbl_tags = components.RoleLabel(
            _('Tags of the selection'), role='inspectorSection')
        tag_header.addWidget(lbl_tags)
        tag_header.addStretch()
        # 「标签管理」的齿轮按钮原来在这儿。用户要求去掉右侧这一个：
        # 属性面板管的是"选中素材的属性"，而标签目录是全工程的东西，
        # 归左侧素材库那一栏管（那边还有一个齿轮，保留）。
        self._body_layout.addLayout(tag_header)
        # Which object is on the other end of these checkboxes is the one
        # thing a tag UI has to keep clear: the inspector tags the assets
        # in the selection, never a page.
        self._tag_scope = components.RoleLabel(role='secondary')
        self._tag_scope.setWordWrap(True)
        self._body_layout.addWidget(self._tag_scope)
        self._update_tag_scope(0)
        self._tag_search = components.LineEdit()
        self._tag_search.setPlaceholderText(
            _('Search a tag, or type a new one and press Enter'))
        self._tag_search.setClearButtonEnabled(True)
        self._tag_search.textChanged.connect(self._filter_tag_choices)
        self._tag_search.returnPressed.connect(self._add_typed_tag)
        self._body_layout.addWidget(self._tag_search)
        self._tags_container = QtWidgets.QWidget()
        self._tags_layout = QtWidgets.QVBoxLayout(self._tags_container)
        self._tags_layout.setContentsMargins(0, 0, 0, 0)
        self._tags_layout.setSpacing(1)
        self._body_layout.addWidget(self._tags_container)
        self._tag_checks = {}
        self._tags_hint = None
        self._tag_names_shown = None
        self._rebuild_tag_checks()

        # Info grid
        self._info_grid = QtWidgets.QFormLayout()
        self._info_grid.setContentsMargins(0, 2, 0, 0)
        self._info_grid.setSpacing(3)
        self._info_grid.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self._type_label = components.RoleLabel(role='inspectorValue')
        t1 = components.RoleLabel('\u7c7b\u578b', role='secondary')
        self._info_grid.addRow(t1, self._type_label)

        self._dims_label = components.RoleLabel(role='inspectorValue')
        t2 = components.RoleLabel('\u5c3a\u5bf8', role='secondary')
        self._info_grid.addRow(t2, self._dims_label)

        self._format_label = components.RoleLabel(role='inspectorValue')
        t_format = components.RoleLabel('\u683c\u5f0f', role='secondary')
        self._info_grid.addRow(t_format, self._format_label)

        self._size_label = components.RoleLabel(role='inspectorValue')
        t3 = components.RoleLabel('\u6587\u4ef6\u5927\u5c0f', role='secondary')
        self._info_grid.addRow(t3, self._size_label)

        self._date_label = components.RoleLabel(role='inspectorValue')
        t4 = components.RoleLabel('\u65f6\u95f4', role='secondary')
        self._info_grid.addRow(t4, self._date_label)

        self._path_label = components.RoleLabel(role='inspectorValue')
        self._path_label.setWordWrap(True)
        t5 = components.RoleLabel('\u8def\u5f84', role='secondary')
        self._info_grid.addRow(t5, self._path_label)

        info_heading = components.RoleLabel('文件信息', role='inspectorSection')
        self._body_layout.addWidget(info_heading)
        for value in (self._type_label, self._dims_label, self._format_label, self._size_label, self._date_label, self._path_label):
            value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop)
        self._body_layout.addLayout(self._info_grid)

        # Color distribution
        lbl_cd = components.RoleLabel(
            _('Color Distribution'), role='inspectorSection')
        self._body_layout.addWidget(lbl_cd)
        self._cd_container = QtWidgets.QWidget()
        self._cd_layout = QtWidgets.QVBoxLayout(self._cd_container)
        self._cd_layout.setContentsMargins(0, 0, 0, 0)
        self._cd_layout.setSpacing(2)
        self._body_layout.addWidget(self._cd_container)

        # Histogram
        lbl_hist = components.RoleLabel(_('Histogram'), role='inspectorSection')
        self._body_layout.addWidget(lbl_hist)
        self._hist_widget = _HistogramWidget()
        self._hist_widget.setMinimumHeight(180)
        self._hist_widget.setObjectName('inspectorHistogram')
        self._body_layout.addWidget(self._hist_widget)
        lbl_cd.hide()
        self._cd_container.hide()
        lbl_hist.hide()
        self._hist_widget.hide()

        # Frame extraction, shown only for videos.  The button is the whole
        # entry point: select a video, pull stills out of it.
        self._video_container = QtWidgets.QWidget()
        video_layout = QtWidgets.QVBoxLayout(self._video_container)
        video_layout.setContentsMargins(0, 0, 0, 0)
        video_layout.setSpacing(4)
        lbl_video = components.RoleLabel(_('Video'), role='inspectorSection')
        video_layout.addWidget(lbl_video)
        self.extract_button = QtWidgets.QPushButton(_('Extract Frames…'))
        self.extract_button.setToolTip(_(
            'Pull still frames out of this video and drop them on the '
            'canvas, skipping frames that look the same.'))
        self.extract_button.clicked.connect(self._on_extract_frames)
        video_layout.addWidget(self.extract_button)
        self._body_layout.addWidget(self._video_container)
        self._video_container.setVisible(False)

        # HDRI exposure / channel controls, shown only for EXR/HDR items
        # whose source file is still available to re-decode.
        self._hdr_container = QtWidgets.QWidget()
        hdr_layout = QtWidgets.QVBoxLayout(self._hdr_container)
        hdr_layout.setContentsMargins(0, 0, 0, 0)
        hdr_layout.setSpacing(4)

        lbl_hdr = components.RoleLabel(
            _('High Dynamic Range'), role='inspectorSection')
        hdr_layout.addWidget(lbl_hdr)

        exposure_row = QtWidgets.QHBoxLayout()
        exposure_row.addWidget(QtWidgets.QLabel(_('Exposure')))
        self._exposure_slider = QtWidgets.QSlider(Qt.Orientation.Horizontal)
        self._exposure_slider.setRange(-50, 50)     # -5.0 .. +5.0 stops
        self._exposure_slider.setToolTip(_('Exposure in stops'))
        self._exposure_slider.valueChanged.connect(self._on_hdr_changed)
        exposure_row.addWidget(self._exposure_slider, 1)
        self._exposure_value = QtWidgets.QLabel('0.0 EV')
        exposure_row.addWidget(self._exposure_value)
        hdr_layout.addLayout(exposure_row)

        channel_row = QtWidgets.QHBoxLayout()
        channel_row.addWidget(QtWidgets.QLabel(_('Channel')))
        self._channel_combo = QtWidgets.QComboBox()
        for value, label in (('rgb', _('RGB')), ('r', _('Red')),
                             ('g', _('Green')), ('b', _('Blue')),
                             ('alpha', _('Alpha')),
                             ('luminance', _('Luminance'))):
            self._channel_combo.addItem(label, value)
        self._channel_combo.currentIndexChanged.connect(self._on_hdr_changed)
        channel_row.addWidget(self._channel_combo, 1)
        hdr_layout.addLayout(channel_row)

        self._tonemap_row_widget = QtWidgets.QWidget()
        tonemap_row = QtWidgets.QHBoxLayout(self._tonemap_row_widget)
        tonemap_row.setContentsMargins(0, 0, 0, 0)
        tonemap_row.addWidget(QtWidgets.QLabel(_('Tone Mapping')))
        self._tonemap_combo = QtWidgets.QComboBox()
        for value, label in (('reinhard', _('Reinhard')),
                             ('linear', _('Linear')), ('aces', _('ACES'))):
            self._tonemap_combo.addItem(label, value)
        self._tonemap_combo.currentIndexChanged.connect(self._on_hdr_changed)
        tonemap_row.addWidget(self._tonemap_combo, 1)
        hdr_layout.addWidget(self._tonemap_row_widget)

        self._false_color_box = QtWidgets.QCheckBox(
            _('False colour (exposure map)'))
        self._false_color_box.setToolTip(_(
            'Paints tones as bands: blue where they are crushed, red where '
            'they are clipped. Shows where the exposure problem is, which a '
            'histogram only counts.'))
        self._false_color_box.toggled.connect(self._on_hdr_changed)
        hdr_layout.addWidget(self._false_color_box)

        self._hdr_reset_button = QtWidgets.QPushButton(_('Reset Exposure'))
        self._hdr_reset_button.clicked.connect(self._reset_hdr_controls)
        hdr_layout.addWidget(self._hdr_reset_button)

        self._hdr_hint = QtWidgets.QLabel()
        self._hdr_hint.setWordWrap(True)
        self._hdr_hint.setProperty('uiRole', 'secondary')
        hdr_layout.addWidget(self._hdr_hint)

        self._body_layout.addWidget(self._hdr_container)
        self._hdr_container.setVisible(False)

        self._body_layout.addStretch()

        self._show_empty()

    # ── Public API ──────────────────────────────────────────────

    def _show_empty(self):
        self._visual_key = None
        self._item = None
        self._preview_label.setText('\u672a\u9009\u4e2d\u7d20\u6750')
        self._title_edit.setEnabled(False)
        self._title_edit.clear()
        self._notes_edit.setEnabled(False)
        self._notes_edit.blockSignals(True)
        self._notes_edit.clear()
        self._notes_edit.blockSignals(False)
        self._cat_combo.blockSignals(True)
        self._cat_combo.clear()
        self._cat_combo.blockSignals(False)
        self._sync_tag_checks(set(), enabled=False)
        self._update_rating_buttons(0, enabled=False)
        self._type_label.setText('\u2014')
        self._dims_label.setText('\u2014')
        self._format_label.setText('\u2014')
        self._size_label.setText('\u2014')
        self._date_label.setText('\u2014')
        self._path_label.setText('\u2014')
        self._hist_widget.clear()
        self._hdr_container.setVisible(False)
        self._video_container.setVisible(False)

    def _update_tag_scope(self, count):
        """Say which object these checkboxes belong to, and how many."""
        self._selection_count.setText(f'已选 {count} 项' if count else '')
        self._tag_scope.setText(
            _('Tagging {count} selected assets').format(count=count) if count
            else _('Select assets first, then tag them'))

    def _on_selection_changed(self):
        self._rebuild_tag_checks()
        selected = self.scene.selectedItems(user_only=True)
        self._update_tag_scope(len(selected))
        if len(selected) != 1:
            self._show_empty()
            if selected:
                self._preview_label.setText(
                    _('{count} items selected').format(count=len(selected)))
                self._cat_combo.blockSignals(True)
                self._cat_combo.addItem(_('Choose a category to move to…'), None)
                self._cat_combo.addItem(_('Uncategorized'), [])
                for name in self._load_categories():
                    self._cat_combo.addItem(name, [name])
                self._cat_combo.blockSignals(False)
                common_tags = set(getattr(selected[0], '_tags', []))
                for item in selected[1:]:
                    common_tags.intersection_update(
                        getattr(item, '_tags', []))
                self._sync_tag_checks(common_tags, enabled=True)
                ratings = {getattr(item, '_rating', 0) for item in selected}
                self._update_rating_buttons(
                    ratings.pop() if len(ratings) == 1 else 0, enabled=True)
            return
        self._refresh_item(selected[0])

    def _known_tags(self):
        """Every tag the project knows about, sorted for display."""
        names = set(getattr(self.scene, 'tag_names', []))
        names |= set(self.scene.get_all_tags())
        for item in self.scene.items_for_save():
            names.update(getattr(item, '_tags', []))
        names.discard('')
        return sorted(names, key=str.casefold)

    #: How many suggestions the search box offers at once.
    MAX_SUGGESTIONS = 40

    def _visible_tag_names(self):
        """The tags this panel offers right now.

        With an empty search box that is what the selection carries -
        tagging is about these items.  With a search box it is what the
        text matches, by full path, by level or by a synonym.
        """
        text = self._tag_search.text().strip().casefold()
        system = tags.tag_system(self.scene)
        if not text:
            names = set()
            for item in self.scene.selectedItems(user_only=True):
                names.update(getattr(item, '_tags', []))
            return sorted(names, key=str.casefold)

        def matches(name):
            if text in name.casefold() or text in tags.leaf(name).casefold():
                return True
            return any(text in alias.casefold() for alias in system.group(name))

        found = [name for name in self._known_tags() if matches(name)]
        return found[:self.MAX_SUGGESTIONS]

    def _rebuild_tag_checks(self):
        """Offer one checkbox per tag that belongs in this panel now."""
        if not hasattr(self, '_tags_layout'):
            return
        names = self._visible_tag_names()
        system = tags.tag_system(self.scene)
        # Rebuilding drops the tick state, so only do it when the rows the
        # panel would show actually changed - including their labels, which
        # carry the other names of a synonym.
        rows = [(name, self._tag_label(name, system)) for name in names]
        if rows == getattr(self, '_tag_rows_shown', None):
            return
        self._tag_rows_shown = rows
        self._tag_names_shown = names
        while self._tags_layout.count():
            entry = self._tags_layout.takeAt(0)
            widget = entry.widget()
            if widget is not None:
                widget.deleteLater()
        self._tag_checks = {}
        if not names:
            typed = self._tag_search.text().strip()
            hint = components.RoleLabel(
                _('No tag matches “{text}”.  Press Enter to create it.')
                .format(text=typed) if typed else
                _('No tags yet - use + in the Tags group on the left'),
                role='secondary')
            hint.setWordWrap(True)
            self._tags_layout.addWidget(hint)
            self._tags_hint = hint
            return
        self._tags_hint = None
        for name in names:
            box = QtWidgets.QCheckBox(self._tag_label(name, system))
            color = tags.namespace_color(name)
            if color:
                box.setStyleSheet(f'color: {color};')
            box.setToolTip(self._tag_tooltip(name, system))
            box.toggled.connect(
                lambda checked, tag=name: self._on_tag_box_toggled(tag, checked))
            self._tags_layout.addWidget(box)
            self._tag_checks[name] = box

    @staticmethod
    def _tag_label(name, system):
        """A tag as shown in the inspector: the name plus its other names."""
        others = system.other_names(name)
        if others:
            return name + '  (' + '、'.join(others) + ')'
        return name

    @staticmethod
    def _tag_tooltip(name, system):
        lines = [name]
        others = system.other_names(name)
        if others:
            lines.append(_('also called: {names}').format(
                names='、'.join(others)))
        parents = sorted(system.implied_parents(name), key=str.casefold)
        if parents:
            lines.append(_('also counts as: {names}').format(
                names='、'.join(parents)))
        return '\n'.join(lines)

    def _sync_tag_checks(self, checked_tags, enabled):
        """Tick the tags the current selection carries."""
        selected = self.scene.selectedItems(user_only=True)
        for name, box in self._tag_checks.items():
            box.blockSignals(True)
            count = sum(name in getattr(item, '_tags', []) for item in selected)
            mixed = 0 < count < len(selected)
            box.setTristate(mixed)
            box.setCheckState(Qt.CheckState.PartiallyChecked if mixed else
                              Qt.CheckState.Checked if name in checked_tags else
                              Qt.CheckState.Unchecked)
            box.setToolTip(
                _('Some of the selected items have this tag') if mixed
                else self._tag_tooltip(name, tags.tag_system(self.scene)))
            box.blockSignals(False)
            box.setEnabled(enabled)
        self._tag_search.setEnabled(True)

    def _filter_tag_choices(self, text):
        """The search box decides which tags are offered at all."""
        self._rebuild_tag_checks()
        self._sync_tag_checks(
            self._tags_of_selection(), enabled=bool(self._item) or bool(
                self.scene.selectedItems(user_only=True)))

    def _tags_of_selection(self):
        selected = self.scene.selectedItems(user_only=True)
        if not selected:
            return set()
        common = set(getattr(selected[0], '_tags', []))
        for item in selected[1:]:
            common.intersection_update(getattr(item, '_tags', []))
        return common

    def _add_typed_tag(self):
        name = self._tag_search.text().strip()
        if not name or not self.scene.selectedItems(user_only=True):
            return
        names = list(self.scene.tag_names)
        name = next((tag for tag in names if tag.casefold() == name.casefold()), name)
        if name not in names:
            self.scene.tag_names.append(name)
            self.view.mark_content_dirty()
        self._on_tag_box_toggled(name, True)
        self._tag_search.clear()
        self._refresh()

    def _on_tag_box_toggled(self, name, checked):
        items = self.scene.selectedItems(user_only=True)
        if not items:
            return
        changed = []
        values = []
        for item in items:
            tags = set(getattr(item, '_tags', []))
            if checked == (name in tags):
                continue
            if checked:
                tags.add(name)
            else:
                tags.discard(name)
            changed.append(item)
            values.append(sorted(tags))
        if changed:
            self.scene.undo_stack.push(
                commands.ChangeMetadata(changed, 'tags', values))

    def _refresh(self):
        self._on_selection_changed()

    @staticmethod
    def _format_of(item):
        """File format shown in the info grid, e.g. PNG / EXR / MP4."""
        name = getattr(item, 'filename', None)
        url = getattr(item, '_video_url', None)
        if (not name) and url is not None and hasattr(url, 'toLocalFile'):
            name = url.toLocalFile()
        if not name:
            return '\u2014'
        suffix = os.path.splitext(str(name))[1].lstrip('.').upper()
        return suffix or '\u2014'

    @staticmethod
    def _inspection_pixmap(item):
        if isinstance(item, PrismPixmapItem) and not item._preview_adjusted:
            preview = item._preview_pixmap
            if preview is not None and not preview.isNull():
                return preview
        return getattr(item, 'pixmap', lambda: None)()

    def _refresh_item(self, item):
        changed_item = self._item is not item
        self._item = item
        if changed_item and getattr(item, 'TYPE', '') == 'pixmap':
            for widget in (self._exposure_slider, self._channel_combo,
                           self._tonemap_combo, self._false_color_box):
                blocker = QtCore.QSignalBlocker(widget)
                if widget is self._exposure_slider:
                    widget.setValue(0)
                elif widget is self._false_color_box:
                    widget.setChecked(False)
                else:
                    widget.setCurrentIndex(0)
                del blocker
            self._exposure_value.setText('0.0 EV')
            source = getattr(item, '_display_source_pixmap', None)
            if source is not None and not source.isNull():
                item.set_preview_pixmap(source, adjusted=False)

        # Preview
        pm = self._inspection_pixmap(item)
        if pm and not pm.isNull():
            scaled = pm.scaled(
                max(200, self.width() - 40), 145,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            self._preview_label.setPixmap(scaled)
        else:
            txt = '\u89c6\u9891' if isinstance(item, PrismVideoItem) else '\u65e0\u9884\u89c8'
            self._preview_label.setText(txt)

        # Title
        title = getattr(item, '_title', None) or getattr(
            item, 'filename', '\u672a\u547d\u540d')
        self._title_edit.setEnabled(True)
        self._title_edit.blockSignals(True)
        self._title_edit.setText(title)
        self._title_edit.blockSignals(False)

        # Notes
        notes = getattr(item, '_notes', '')
        self._notes_edit.setEnabled(True)
        self._notes_edit.blockSignals(True)
        if self._notes_edit.toPlainText() != notes:
            self._notes_edit.setPlainText(notes)
        self._notes_edit.blockSignals(False)

        # Category dropdown
        self._cat_combo.blockSignals(True)
        self._cat_combo.clear()
        self._cat_combo.addItem('\u672a\u5206\u7c7b', [])
        cats = self._load_categories()
        for c in sorted(cats):
            self._cat_combo.addItem(c, [c])
        item_cats = getattr(item, '_categories', [])
        if len(item_cats) > 1:
            self._cat_combo.addItem('、'.join(item_cats), list(item_cats))
            self._cat_combo.setCurrentIndex(self._cat_combo.count() - 1)
        elif item_cats:
            idx = self._cat_combo.findText(item_cats[0])
            if idx >= 0:
                self._cat_combo.setCurrentIndex(idx)
        self._cat_combo.blockSignals(False)

        self._sync_tag_checks(set(getattr(item, '_tags', [])), enabled=True)
        self._update_rating_buttons(getattr(item, '_rating', 0), enabled=True)

        # Info grid
        if isinstance(item, PrismPixmapItem):
            self._type_label.setText('\u56fe\u7247')
            if pm and not pm.isNull():
                self._dims_label.setText(
                    f'{item._image_size.width()} \u00d7 {item._image_size.height()}')
            else:
                self._dims_label.setText('\u672a\u77e5')
        elif isinstance(item, PrismVideoItem):
            self._type_label.setText('\u89c6\u9891')
            if pm and not pm.isNull():
                self._dims_label.setText(
                    f'{pm.width()} \u00d7 {pm.height()}')
            else:
                self._dims_label.setText('\u672a\u77e5')
        else:
            self._type_label.setText('\u672a\u77e5')
            self._dims_label.setText('\u2014')
        self._format_label.setText(self._format_of(item))

        # File size and path
        url = getattr(item, '_video_url', None)
        fn = getattr(item, 'filename', None)
        fpath = None
        if url and hasattr(url, 'toLocalFile'):
            fpath = url.toLocalFile()
        elif fn and os.path.isabs(fn):
            fpath = fn
        if fpath and os.path.exists(fpath):
            try:
                stat = os.stat(fpath)
                self._size_label.setText(_format_bytes(stat.st_size))
                mtime = datetime.fromtimestamp(stat.st_mtime)
                self._date_label.setText(mtime.strftime('%Y-%m-%d %H:%M'))
            except OSError:
                self._size_label.setText('\u672a\u77e5')
                self._date_label.setText('\u672a\u77e5')
            self._path_label.setText(fpath)
            self._path_label.setToolTip(fpath)
        else:
            self._size_label.setText('\u672a\u77e5')
            self._date_label.setText('\u672a\u77e5')
            self._path_label.setText(fn or '\u672a\u77e5')

        visual_key = (id(item), pm.cacheKey() if pm is not None else None,
                      getattr(item, '_preview_adjusted', False))
        visuals_changed = visual_key != getattr(self, '_visual_key', None)
        self._visual_key = visual_key
        # Text and tag edits must not recalculate the same image statistics.
        if isinstance(item, PrismPixmapItem):
            if visuals_changed:
                self._build_color_distribution(item)
        else:
            while hasattr(self, '_cd_layout') and self._cd_layout.count():
                w = self._cd_layout.takeAt(0).widget()
                if w:
                    w.deleteLater()

        # Histogram (only for images)
        if isinstance(item, PrismPixmapItem):
            if visuals_changed:
                self._hist_widget.update_from_pixmap(pm)
        else:
            self._hist_widget.clear()

        # Exposure / channel controls (only for scene-linear sources)
        self._update_hdr_controls(item)
        self._update_video_controls(item)

    # ── Color distribution ────────────────────────

    def _build_color_distribution(self, item):
        """Compute and render color distribution for a pixmap item."""
        # Clear existing
        while self._cd_layout.count():
            w = self._cd_layout.takeAt(0).widget()
            if w:
                w.deleteLater()

        pm = self._inspection_pixmap(item)
        if pm is None or pm.isNull():
            return

        # Sample pixels (downscale to max 72px like analyze_color_group)
        img = pm.toImage()
        max_side = 72
        w, h = img.width(), img.height()
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            img = img.scaled(
                max(1, int(w * scale)), max(1, int(h * scale)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)

        import numpy as np
        from prism.items import (_qimage_to_rgba, _hsl_arrays,
                                 _color_group_indices, _COLOR_INDEX)
        sample = _qimage_to_rgba(img)
        if sample is None:
            return
        hue, saturation, lightness = _hsl_arrays(
            sample[:, :, 0], sample[:, :, 1], sample[:, :, 2])
        indices = _color_group_indices(hue, saturation, lightness)
        amounts = (sample[:, :, 3] >= 180).astype(np.float64)
        neutral = np.isin(indices, [_COLOR_INDEX[name] for name in ('black', 'white', 'gray')])
        amounts *= np.where(neutral, 0.72, 1.0)
        amounts *= np.where(saturation > 0.5, 1.9, 1.0)
        totals = np.bincount(indices.ravel(), weights=amounts.ravel(), minlength=len(COLOR_GROUPS))
        weights = {group['id']: float(totals[index]) for index, group in enumerate(COLOR_GROUPS)}
        total = sum(weights.values())
        if total < 1:
            return

        # Sort by weight desc, keep top 5
        sorted_groups = sorted(
            [(gid, weights[gid]) for gid in weights if weights[gid] > 0],
            key=lambda x: -x[1])[:5]

        # Color bar
        bar = _ColorBar(sorted_groups, total)
        bar.setFixedHeight(10)
        self._cd_layout.addWidget(bar)

        # Labels row
        labels_row = QtWidgets.QWidget()
        labels_layout = QtWidgets.QGridLayout(labels_row)
        labels_layout.setContentsMargins(0, 4, 0, 0)
        labels_layout.setSpacing(6)
        color_map = {g['id']: g for g in COLOR_GROUPS}
        for row, (gid, wt) in enumerate(sorted_groups):
            pct = int(round(wt / total * 100))
            info = color_map.get(gid, {})
            label = _(info.get('label', gid))
            chip = QtWidgets.QLabel()
            chip.setFixedSize(10, 10)
            chip.setStyleSheet(
                'background: %s; border-radius: 5px;' % info.get('color', '#888'))
            labels_layout.addWidget(chip, row, 0)
            txt = components.RoleLabel(
                '%s %d%%' % (label, pct), role='secondary')
            labels_layout.addWidget(txt, row, 1)
        labels_layout.setColumnStretch(1, 1)
        self._cd_layout.addWidget(labels_row)

    def _on_title_changed(self, text):
        if not self._item:
            return
        old = getattr(self._item, '_title', '') or getattr(
            self._item, 'filename', '')
        if text == old:
            return
        self.scene.undo_stack.push(
            commands.ChangeMetadata([self._item], 'title', [text]))

    def _on_notes_changed(self):
        if not self._item:
            return
        new_notes = self._notes_edit.toPlainText()
        old_notes = getattr(self._item, '_notes', '')
        if new_notes == old_notes:
            return
        self.scene.undo_stack.push(
            commands.ChangeMetadata([self._item], 'notes', [new_notes]))

    def _on_category_changed(self, index):
        selected = self.scene.selectedItems(user_only=True)
        new_cats = self._cat_combo.currentData()
        if not selected or new_cats is None:
            return
        selected = [i for i in selected if list(i.categories) != new_cats]
        if not selected:
            return
        self.scene.undo_stack.push(
            commands.ChangeMetadata(selected, 'categories',
                                    [list(new_cats) for i in selected]))
        if hasattr(self.view, 'category_panel'):
            self.view.category_panel.update_counts()

    def _set_rating(self, rating):
        selected = self.scene.selectedItems(user_only=True)
        changed = [item for item in selected
                   if getattr(item, '_rating', 0) != rating]
        if changed:
            self.scene.undo_stack.push(commands.ChangeMetadata(
                changed, 'rating', [rating for item in changed]))
        self._update_rating_buttons(rating, enabled=bool(selected))

    def _update_rating_buttons(self, rating, enabled=True):
        for value, button in enumerate(self._rating_buttons, 1):
            button.setEnabled(enabled)
            button.setIcon(icon('star-filled' if value <= rating else 'star'))

    def _load_categories(self):
        return self.view.category_panel._load_categories()

    # ── HDRI exposure / channel controls ────────────────────────

    def _is_hdr_item(self, item):
        """Whether the item has a scene-linear source to re-decode."""
        if item is None or getattr(item, 'TYPE', '') != 'pixmap':
            return False
        filename = getattr(item, 'filename', None)
        return bool(filename) and is_hdr_path(filename)

    def _hdr_source_available(self, item):
        """HDR sources can only be re-decoded while the file still exists."""
        if not self._is_hdr_item(item):
            return False
        return bool(item.filename) and os.path.exists(item.filename)

    def _hdr_options(self):
        """The current exposure/channel/tone mapping selection."""
        return {
            'exposure': self._exposure_slider.value() / 10.0,
            'operator': self._tonemap_combo.currentData() or 'reinhard',
            'channel': self._channel_combo.currentData() or 'rgb',
            'false_color': self._false_color_box.isChecked(),
        }

    def _update_video_controls(self, item):
        """Offer frame extraction only while a video is selected."""
        available = (item is not None
                     and isinstance(item, PrismVideoItem)
                     and getattr(item, '_video_url', None) is not None)
        self._video_container.setVisible(available)
        return available

    def _on_extract_frames(self):
        item = self._item
        if item is None or not isinstance(item, PrismVideoItem):
            return
        from prism.widgets.extract_frames import ExtractFramesDialog

        dialog = ExtractFramesDialog(self.view, item, parent=self.window())
        dialog.exec()
        if dialog.images:
            self.view.add_frames_to_canvas(dialog.images, source=item)

    def _update_hdr_controls(self, item):
        """Expose non-destructive display controls for every still image."""
        available = item is not None and getattr(item, 'TYPE', '') == 'pixmap'
        self._hdr_container.setVisible(available)
        if available:
            hdr = self._hdr_source_available(item)
            self._tonemap_row_widget.setVisible(hdr)
            self._hdr_hint.setText(
                '仅改变预览，原始素材保持不变；导出时可选择原图或调整后图片。')
        return available

    def _on_hdr_changed(self, *_args):
        self._exposure_value.setText(
            f'{self._exposure_slider.value() / 10.0:+.1f} EV')
        self._apply_hdr_settings()

    def _reset_hdr_controls(self):
        widgets = (self._exposure_slider, self._channel_combo,
                   self._tonemap_combo, self._false_color_box)
        for widget in widgets:
            widget.blockSignals(True)
        self._exposure_slider.setValue(0)
        self._channel_combo.setCurrentIndex(0)
        self._tonemap_combo.setCurrentIndex(0)
        self._false_color_box.setChecked(False)
        for widget in widgets:
            widget.blockSignals(False)
        self._on_hdr_changed()

    def _apply_hdr_settings(self):
        """Re-decode the selected source with the current HDRI settings."""
        item = self._item
        if item is None or getattr(item, 'TYPE', '') != 'pixmap':
            return
        options = self._hdr_options()
        is_default_preview = (
            options['exposure'] == 0.0
            and options['channel'] == 'rgb'
            and options['operator'] == 'reinhard'
            and not options.get('false_color'))
        if self._hdr_source_available(item):
            try:
                image, _ = load_image(item.filename, options)
            except (OSError, ValueError, TypeError, RuntimeError) as exc:
                logger.exception('Image preview decode failed: %s', item.filename)
                self._hdr_hint.setText(f'预览更新失败，已保留原图：{exc}')
                return
        else:
            source = getattr(item, '_display_source_pixmap', None)
            if source is None:
                source = QtGui.QPixmap(item.pixmap())
                item._display_source_pixmap = source
            if is_default_preview and hasattr(item, '_display_source_pixmap'):
                # Resetting the controls should restore the original display
                # pixels, while the imported source bytes remain untouched.
                item.set_preview_pixmap(source, adjusted=False)
                return
            image = adjust_display_image(
                source.toImage(), options['exposure'], options['channel'])
        if image.isNull():
            logger.info(f'Could not re-decode {item.filename}')
            self._hdr_hint.setText('无法解码这张图片，已保留原来的预览。')
            return
        if options.get('false_color'):
            image = false_color_preview(image, options['exposure'])
        item.set_preview_pixmap(QtGui.QPixmap.fromImage(image), adjusted=True)


class _ColorBar(QtWidgets.QWidget):
    def __init__(self, groups_with_weights, total, parent=None):
        super().__init__(parent)
        self._segments = []
        color_map = {g['id']: g for g in COLOR_GROUPS}
        for gid, wt in groups_with_weights:
            ratio = wt / total if total > 0 else 0
            hex_color = color_map.get(gid, {}).get('color', '#888')
            self._segments.append((ratio, hex_color))

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        x = 0.0
        for i, (ratio, hex_color) in enumerate(self._segments):
            seg_w = ratio * w
            color = QtGui.QColor(hex_color)
            painter.fillRect(
                QtCore.QRectF(x, 0, seg_w + 0.5, h), color)
            x += seg_w
        painter.end()


class _HistogramWidget(QtWidgets.QWidget):
    """Professional brightness/color histogram with zone analysis."""

    # 5 tonal zones (Photoshop-style)
    ZONES = [
        ('Shadows',    0,   51,  '#4a4a6a'),   # Shadows
        ('Darks',   51,  102,  '#6a6a8a'),   # Darks
        ('Midtones', 102, 153, '#8a8aaa'),  # Midtones
        ('Lights',  153, 204,  '#aaaacc'),   # Lights
        ('Highlights',  204, 256,  '#ccccee'),   # Highlights
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._bins = None
        self._stats = None
        self.setMinimumHeight(180)

    def clear(self):
        self._bins = None
        self._stats = None
        self.update()

    def update_from_pixmap(self, pm):
        if pm is None or pm.isNull():
            self.clear()
            return
        img = pm.toImage()
        max_side = 120
        w, h = img.width(), img.height()
        longest = max(w, h)
        if longest > max_side:
            scale = max_side / longest
            img = img.scaled(
                max(1, int(w * scale)), max(1, int(h * scale)),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.FastTransformation)
        import numpy as np
        from prism.items import _qimage_to_rgba
        sample = _qimage_to_rgba(img)
        if sample is None:
            self.clear()
            return
        visible = sample[sample[:, :, 3] >= 180]
        channels = visible[:, :3].astype(np.float64)
        levels = (0.299 * channels[:, 0] + 0.587 * channels[:, 1] +
                  0.114 * channels[:, 2]).astype(np.int64)
        lum = np.bincount(levels, minlength=256).tolist()
        r_bins, g_bins, b_bins = [np.bincount(visible[:, channel], minlength=256).tolist()
                                for channel in range(3)]
        total_pixels = len(visible)
        sum_l = float(levels.sum())
        sum_l2 = float((levels.astype(np.float64) ** 2).sum())
        self._bins = (lum, r_bins, g_bins, b_bins)
        if total_pixels > 0:
            mean = sum_l / total_pixels
            variance = (sum_l2 / total_pixels) - (mean * mean)
            std = variance ** 0.5 if variance > 0 else 0
            zones = []
            for zname, zlo, zhi, zcolor in self.ZONES:
                cnt = sum(lum[zlo:zhi])
                pct = cnt / total_pixels * 100
                zones.append((zname, zlo, zhi, zcolor, pct))
            if std < 30:
                contrast_txt = _('Low contrast')
            elif std < 60:
                contrast_txt = _('Medium contrast')
            else:
                contrast_txt = _('High contrast')
            if mean < 64:
                expo_txt = _('Underexposed')
            elif mean < 100:
                expo_txt = _('Dark')
            elif mean < 160:
                expo_txt = _('Normal exposure')
            elif mean < 210:
                expo_txt = _('Bright')
            else:
                expo_txt = _('Overexposed')
            self._stats = {
                'mean': mean,
                'std': std,
                'zones': zones,
                'contrast': contrast_txt,
                'exposure': expo_txt,
                'total': total_pixels,
            }
        else:
            self._stats = None
        self.update()

    def paintEvent(self, event):
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
        w = self.width()
        h = self.height()
        if self._bins is None or self._stats is None or w < 10 or h < 10:
            painter.setPen(QtGui.QColor(120, 120, 120))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             _('No data'))
            painter.end()
            return

        # Layout: graph top 50%, zone bars 22%, stats text 28%
        graph_h = int(h * 0.50)
        zone_h = int(h * 0.22)
        stats_y = graph_h + zone_h + 4
        margin = 4
        pw = w - margin * 2
        ph = graph_h - margin * 2

        lum, r_bins, g_bins, b_bins = self._bins
        max_val = max(max(lum), max(r_bins), max(g_bins), max(b_bins), 1)

        # Draw zone background bands
        for zname, zlo, zhi, zcolor, pct in self._stats['zones']:
            x1 = margin + (zlo / 256.0) * pw
            x2 = margin + (zhi / 256.0) * pw
            color = QtGui.QColor(zcolor)
            color.setAlpha(25)
            painter.fillRect(
                QtCore.QRectF(x1, margin, x2 - x1, ph), color)

        # Draw channels
        channels = [
            (r_bins, QtGui.QColor(255, 80, 80, 80)),
            (g_bins, QtGui.QColor(80, 255, 80, 80)),
            (b_bins, QtGui.QColor(80, 120, 255, 80)),
            (lum, QtGui.QColor(220, 220, 220, 160)),
        ]
        for bins, color in channels:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            path = QtGui.QPainterPath()
            path.moveTo(margin, graph_h - margin)
            step = pw / 256.0
            for i in range(256):
                bar_h = (bins[i] / max_val) * ph
                path.lineTo(margin + i * step, graph_h - margin - bar_h)
            path.lineTo(margin + 255 * step, graph_h - margin)
            path.closeSubpath()
            painter.drawPath(path)

        # Draw mean line
        mean_x = margin + (self._stats['mean'] / 256.0) * pw
        painter.setPen(QtGui.QPen(QtGui.QColor(255, 200, 50), 1.5))
        painter.drawLine(
            QtCore.QPointF(mean_x, margin),
            QtCore.QPointF(mean_x, graph_h - margin))
        painter.setPen(QtGui.QColor(255, 200, 50))
        font = painter.font()
        font.setPixelSize(12)
        painter.setFont(font)
        painter.drawText(
            QtCore.QRectF(mean_x - 20, 0, 40, margin + 2),
            Qt.AlignmentFlag.AlignCenter,
            'μ=%d' % int(self._stats['mean']))

        # Zone percentage bars
        zone_y = graph_h + 2
        bar_total_w = pw
        bar_h_px = zone_h - 6
        x_cursor = margin
        for zname, zlo, zhi, zcolor, pct in self._stats['zones']:
            seg_w = ((zhi - zlo) / 256.0) * bar_total_w
            color = QtGui.QColor(zcolor)
            color.setAlpha(180)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawRoundedRect(
                QtCore.QRectF(x_cursor, zone_y, seg_w - 1, bar_h_px),
                2, 2)
            painter.setPen(QtGui.QColor(240, 240, 255))
            font.setPixelSize(12)
            painter.setFont(font)
            if seg_w > 28:
                label_text = '%s\n%d%%' % (_(zname), int(pct))
                painter.drawText(
                    QtCore.QRectF(x_cursor, zone_y, seg_w - 1, bar_h_px),
                    Qt.AlignmentFlag.AlignCenter, label_text)
            x_cursor += seg_w

        # Stats text row
        stats = self._stats
        painter.setPen(QtGui.QColor(180, 180, 200))
        font.setPixelSize(13)
        painter.setFont(font)
        stats_text = _('Mean: %d  Std dev: %d  %s  %s') % (
            int(stats['mean']), int(stats['std']),
            stats['contrast'], stats['exposure'])
        painter.drawText(
            QtCore.QRectF(margin, stats_y, pw, h - stats_y),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            stats_text)

        painter.end()
