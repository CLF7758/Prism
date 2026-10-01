"""查重对话框的「在画布中定位」。

用户报的现象：**「跳转到第一张画布，然后重复的图片闪一下就消失了」**。

定位本身是准的（视图偏差 0），坏在后面那一步：`setVisible(True)` 和
`setSelected(True)` 都会让 `QGraphicsScene` 发 `changed`，侧栏收到它会排一次
300ms 后的重算（`update_counts` → `_apply_filter`）。那次重算按当前筛选重新
算可见性 —— 而「定位」要找的图往往正是被筛掉的那张，于是刚显示出来就被藏了
回去。

所以这里用**真实的重算定时器**验，不手动调 `update_counts`：手动调测的是
「重算会隐藏它」，而用户看到的是「定时器把它带走了」。
"""
import time

import pytest
from PyQt6 import QtCore, QtGui

from prism.items import PrismPixmapItem
from prism.widgets.duplicate_finder import DuplicateFinderDialog


def _photo(size=200):
    image = QtGui.QImage(size, int(size * 0.75),
                         QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    return PrismPixmapItem(image)


def _pump(app, seconds):
    """让事件循环真的跑起来 —— 单次定时器要靠它才会触发。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)


def _dialog_for(view, item):
    view.resize(900, 600)
    view.show()
    view.scene.addItem(item)
    dialog = DuplicateFinderDialog(view, [[item, item]])
    dialog.show()
    dialog.image_list.setCurrentRow(0)
    return dialog


# ── 定位本身 ─────────────────────────────────────────────────────


def test_a_normal_photo_is_located_and_says_so(view, qapp):
    target = _photo()
    target.set_pos_center(QtCore.QPointF(6000, 4000))
    dialog = _dialog_for(view, target)

    dialog._locate_selected_images()

    centre = view.mapToScene(view.viewport().rect().center())
    wanted = target.sceneBoundingRect().center()
    assert (centre - wanted).manhattanLength() < 5
    assert 'Located' in dialog.feedback.text()
    dialog.close()


def test_a_photo_without_a_size_does_not_claim_success(view, qapp):
    """``boundingRect()`` 为空的图元会让 ``fit_rect()`` 静默什么都不做。

    判断不能用 ``boundingRect()``：item 一旦被选中，选择手柄会把矩形撑开，
    永远不为空。
    """
    target = _photo()
    target.set_pos_center(QtCore.QPointF(6000, 4000))
    dialog = _dialog_for(view, target)
    target._image_size = QtCore.QSize(0, 0)
    target.crop = QtCore.QRectF()

    dialog._locate_selected_images()

    feedback = dialog.feedback.text()
    assert feedback, '没定位成功时不能一声不吭'
    assert 'Located' not in feedback
    dialog.close()


def test_moving_the_view_reports_whether_it_happened(view, qapp):
    good = _photo()
    good.set_pos_center(QtCore.QPointF(1000, 1000))
    dialog = _dialog_for(view, good)
    assert dialog._move_out_of_the_way([good]) is True

    empty = _photo()
    empty._image_size = QtCore.QSize(0, 0)
    empty.crop = QtCore.QRectF()
    assert dialog._move_out_of_the_way([empty]) is False
    dialog.close()


# ── 那张图必须活过重算 ───────────────────────────────────────────


def _hidden_by_the_filter(view):
    """摆一个「当前筛选正好把它排除掉」的场景，返回 (图元, 侧栏)。"""
    view.resize(900, 600)
    view.show()
    target = _photo()
    target._categories = ['Stray']
    target.set_pos_center(QtCore.QPointF(3000, 2000))
    view.scene.addItem(target)
    library = view.category_panel
    library._active_filter = ('category', '别的分类')
    library._apply_filter()             # 用户当前确实看不到它
    assert not target.isVisible()
    return target, library


def test_the_located_photo_survives_the_recount(view, qapp):
    """回归：定位后 300ms 的那次重算不许把它藏回去。"""
    target, library = _hidden_by_the_filter(view)
    dialog = DuplicateFinderDialog(view, [[target, target]])
    dialog.show()
    dialog.image_list.setCurrentRow(0)

    dialog._locate_selected_images()
    assert target.isVisible(), '定位之后就该看得见'

    _pump(qapp, 1.5)                    # 重算定时器是 300ms
    assert target.isVisible(), '闪一下就消失了 —— 重算把它藏了回去'
    dialog.close()


def test_locating_does_not_schedule_a_recount(view, qapp):
    """定位本身不该再排一次重算 —— 选中和显示都做了静默处理。

    （测试要先让之前排下的重算跑完：摆场景时 `addItem` 和 `_apply_filter`
    都会排一次，那是正常现象，不是定位造成的。）
    """
    target, library = _hidden_by_the_filter(view)
    dialog = DuplicateFinderDialog(view, [[target, target]])
    dialog.show()
    dialog.image_list.setCurrentRow(0)
    _pump(qapp, 0.6)
    assert not library._recount_timer.isActive()

    dialog._locate_selected_images()

    assert not library._recount_timer.isActive(), '定位不该再排一次重算'
    dialog.close()


def test_the_selection_still_reaches_the_sidebar(view, qapp):
    """屏蔽的是那次重算，不是把选中也吞了。"""
    target, library = _hidden_by_the_filter(view)
    dialog = DuplicateFinderDialog(view, [[target, target]])
    dialog.show()
    dialog.image_list.setCurrentRow(0)
    seen = []
    view.scene.selectionChanged.connect(lambda: seen.append(True))

    dialog._locate_selected_images()

    assert seen, '侧栏和其它监听者要能收到一次 selectionChanged'
    assert target.isSelected()
    dialog.close()


def test_the_filter_still_works_afterwards(view, qapp):
    """定位只是把这一张显出来，不是把筛选关掉。"""
    target, library = _hidden_by_the_filter(view)
    dialog = DuplicateFinderDialog(view, [[target, target]])
    dialog.show()
    dialog.image_list.setCurrentRow(0)
    dialog._locate_selected_images()
    other = _photo()
    other._categories = ['别的分类']
    view.scene.addItem(other)

    library._apply_filter()             # 用户主动点了一次筛选

    assert not target.isVisible(), '用户改筛选时它必须照常被筛掉'
    assert other.isVisible()
    dialog.close()
