"""T6 验收里的「页面来源」——同一图片在不同页面出现时要看得出来。

核查时发现的：`ImageSource.location` 有位置但填的是**分类**，而且结果
对话框根本没读它（自己在 `_show_group` 里重新拼 "Category: xxx"）。
所以「同一个文件出现在两个画布上」时，两条记录看起来一模一样，分不出
哪条是哪来的。

修法是抽一个 `canvas_label(item, pages)` 给两边共用 —— 各拼一次字符串
早晚会不一致，改之前就已经不一致了。
"""
import pytest

from prism.duplicate_scan import canvas_label


class FakeItem:
    def __init__(self, canvas_id=None, categories=()):
        self._canvas_id = canvas_id
        self._categories = list(categories)


PAGES = [
    {'id': 'p1', 'kind': 'canvas', 'title': '画布一'},
    {'id': 'p2', 'kind': 'canvas', 'title': '参考图'},
    {'id': 'doc1', 'kind': 'document', 'title': '笔记'},
]


# ── 页面名 ──────────────────────────────────────────────────────

def test_label_starts_with_the_page_title():
    label = canvas_label(FakeItem('p1'), PAGES)
    assert label.startswith('画布一')


def test_two_pages_give_different_labels():
    """这正是任务书要的那条：同一张图在两个页面上要能分出哪条是哪条。"""
    first = canvas_label(FakeItem('p1'), PAGES)
    second = canvas_label(FakeItem('p2'), PAGES)
    assert first != second
    assert '画布一' in first and '参考图' in second


def test_page_comes_before_categories():
    """页面在前 —— 用户找的是"哪一页"，分类是次要信息。"""
    label = canvas_label(FakeItem('p1', ['静物', '参考']), PAGES)
    assert label.index('画布一') < label.index('静物')


def test_label_joins_both_with_a_separator():
    label = canvas_label(FakeItem('p1', ['静物']), PAGES)
    assert '画布一' in label and '静物' in label
    assert '·' in label


# ── 缺东西的时候 ────────────────────────────────────────────────

def test_no_canvas_id_falls_back_to_categories():
    """老工程里的素材没有 _canvas_id，只显示分类。"""
    assert canvas_label(FakeItem(None, ['静物']), PAGES) == '静物'


def test_unknown_canvas_id_does_not_invent_a_page():
    label = canvas_label(FakeItem('没了', ['静物']), PAGES)
    assert '静物' in label
    assert '画布' not in label


def test_no_categories_still_gives_the_page():
    assert canvas_label(FakeItem('p1'), PAGES) == '画布一'


def test_neither_gives_an_empty_string():
    """两头都没有就返回空串，让调用方决定显示成什么。"""
    assert canvas_label(FakeItem(), PAGES) == ''


def test_no_pages_at_all_does_not_crash():
    assert canvas_label(FakeItem('p1', ['静物']), ()) == '静物'
    assert canvas_label(FakeItem('p1'), None) == ''


def test_page_without_a_title_is_skipped():
    """页面存在但标题是空的 —— 别显示一个"· 静物"那种开头的分隔符。"""
    pages = [{'id': 'p9', 'title': ''}]
    assert canvas_label(FakeItem('p9', ['静物']), pages) == '静物'


def test_label_handles_a_bare_object():
    """不是所有素材都有那两个属性（文本素材之类）。"""

    class Bare:
        pass

    assert canvas_label(Bare(), PAGES) == ''


# ── 和 collect_sources 接上 ─────────────────────────────────────

def test_collect_sources_fills_location_with_the_page(qapp):
    """`collect_sources` 填进 ImageSource 的 location 要带页面名。"""
    from PyQt6 import QtGui

    from prism.duplicate_scan import ScanScope, collect_sources
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(10, 20, 30))

    class FakeScene:
        workspace_pages = PAGES

        def __init__(self, items):
            self._items = items

        def items_for_save(self):
            return list(self._items)

    item = PrismPixmapItem(image)
    item._canvas_id = 'p2'
    item._categories = ['参考']
    scene = FakeScene([item])

    sources = collect_sources(scene, ScanScope.ALL_CANVASES)
    assert len(sources) == 1
    assert '参考图' in sources[0].location, (
        'location 要带页面名，实得 %r' % (sources[0].location,))
    assert '参考' in sources[0].location
