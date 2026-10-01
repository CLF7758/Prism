"""补上任务书列的第五种扫描范围：当前页面。

任务书 §6 的「范围必须显式指定」列了五种：

    当前画布 / 当前页面 / 全部画布页面 / 全部素材 / 当前选中素材

代码里原来只有四种 —— 缺「当前页面」。任务书把「当前画布」和「当前页面」
分开列，说明不是一回事：

  * **当前画布**按 `_canvas_id` 匹配画布上的图
  * **当前页面**是当前**工作区页面**。它可能是文档页或脑图页

区别在文档/脑图页上才显出来：**那些页面的图片是附件，住在 `attachments`
表里，不在 `items` 表**（见 `sql.py` 的 `read_board_extras`），所以它们
根本不进画布、也扫不到。这时正确的行为是**如实返回空**，而不是悄悄退化成
"扫全项目"。
"""
import pytest
from PyQt6 import QtGui

from prism.duplicate_scan import ScanScope, collect_sources
from prism.i18n import _


class FakeScene:
    def __init__(self, items):
        self._items = items
        self.workspace_pages = [
            {'id': 'canvas-1', 'kind': 'canvas', 'title': '画布一'},
            {'id': 'doc-1', 'kind': 'document', 'title': '笔记'},
        ]

    def items_for_save(self):
        return list(self._items)


def make_item(canvas_id, colour=(100, 120, 140)):
    from prism.items import PrismPixmapItem

    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(*colour))
    item = PrismPixmapItem(image)
    item._canvas_id = canvas_id
    return item


@pytest.fixture
def scene(qapp):
    return FakeScene([make_item('canvas-1'),
                      make_item('canvas-1', (200, 100, 50)),
                      make_item('other-canvas', (50, 200, 100))])


# ── 范围本身 ────────────────────────────────────────────────────

def test_current_page_is_a_known_scope():
    assert ScanScope.CURRENT_PAGE in ScanScope.ALL


def test_all_five_scopes_the_task_book_lists_are_present():
    """任务书数了五种，这里数一遍 —— 少一种就是漏了一条验收。"""
    assert len(ScanScope.ALL) == 5
    assert set(ScanScope.ALL) == {
        ScanScope.SELECTION,
        ScanScope.CURRENT_CANVAS,
        ScanScope.CURRENT_PAGE,
        ScanScope.ALL_CANVASES,
        ScanScope.ALL_ASSETS,
    }


def test_current_page_has_a_label():
    assert ScanScope.label(ScanScope.CURRENT_PAGE).strip()


def test_every_scope_has_a_label_that_is_not_the_raw_value():
    """每个范围都要有给人看的名字。

    这条是冲着修掉的那个 bug 来的：对话框原来直接把 `self.scope` 填进句子，
    用户看到的是 `Scanned the "all-canvases" category only.` —— 机器码。
    """
    for scope in ScanScope.ALL:
        label = ScanScope.label(scope)
        assert label and label != scope, f'{scope} 没有可读的名字'


# ── 画布页 ──────────────────────────────────────────────────────

def test_current_page_on_a_canvas_takes_that_canvas(scene):
    sources = collect_sources(
        scene, ScanScope.CURRENT_PAGE,
        current_page={'id': 'canvas-1', 'kind': 'canvas'})
    assert len(sources) == 2


def test_current_page_matches_current_canvas_on_a_canvas(scene):
    """画布页上两者结果相同 —— 它们本来就是一回事，分开列是为了文档页。"""
    by_page = collect_sources(
        scene, ScanScope.CURRENT_PAGE,
        current_page={'id': 'canvas-1', 'kind': 'canvas'})
    by_canvas = collect_sources(
        scene, ScanScope.CURRENT_CANVAS, current_canvas_id='canvas-1')
    assert len(by_page) == len(by_canvas)


# ── 文档页和脑图页 ──────────────────────────────────────────────

@pytest.mark.parametrize('kind', ['document', 'mindmap'])
def test_current_page_on_a_document_is_empty(scene, kind):
    """文档/脑图页的图片是附件，不在画布上 —— 没有素材可查。

    这里要的是**空**而不是"全项目"：悄悄扩大范围比报空更糟，用户会以为
    自己只扫了这一页，实际拿全局结果当依据去删文件。
    """
    sources = collect_sources(
        scene, ScanScope.CURRENT_PAGE,
        current_page={'id': 'doc-1', 'kind': kind})
    assert sources == []


def test_document_page_does_not_fall_back_to_everything(scene):
    """上面那条的另一面：确认它没有退化成 ALL_ASSETS。"""
    on_doc = collect_sources(
        scene, ScanScope.CURRENT_PAGE,
        current_page={'id': 'doc-1', 'kind': 'document'})
    everything = collect_sources(scene, ScanScope.ALL_ASSETS)
    assert len(on_doc) < len(everything)


# ── 没告诉页面时 ────────────────────────────────────────────────

def test_missing_current_page_falls_back_to_the_canvas(scene):
    """没传 current_page 时按画布页处理 —— 那是默认页面的情况。"""
    sources = collect_sources(
        scene, ScanScope.CURRENT_PAGE, current_canvas_id='canvas-1')
    assert len(sources) == 2


def test_page_without_a_kind_is_treated_as_a_canvas(scene):
    """老数据可能只有 id 没有 kind。别因为缺字段就把结果清空。"""
    sources = collect_sources(
        scene, ScanScope.CURRENT_PAGE, current_page={'id': 'canvas-1'})
    assert len(sources) == 2


def test_empty_current_page_does_not_crash(scene):
    assert collect_sources(scene, ScanScope.CURRENT_PAGE,
                           current_page={}) is not None
    assert collect_sources(scene, ScanScope.CURRENT_PAGE,
                           current_page=None) is not None


# ── 其它四种没被带坏 ────────────────────────────────────────────

def test_other_scopes_still_work(scene):
    assert len(collect_sources(scene, ScanScope.ALL_ASSETS)) == 3
    assert len(collect_sources(scene, ScanScope.CURRENT_CANVAS,
                               current_canvas_id='other-canvas')) == 1


def test_selection_scope_ignores_the_page(scene):
    """选中范围看的是传进来的那批，和当前页面无关。"""
    item = scene.items_for_save()[0]
    sources = collect_sources(scene, ScanScope.SELECTION, selected=[item],
                              current_page={'id': 'doc-1',
                                            'kind': 'document'})
    assert len(sources) == 1


def test_labels_are_translated():
    """界面全中文是硬要求 —— 这几个范围会出现在界面上。"""
    from prism.i18n.zh_CN import zh_CN

    for scope in ScanScope.ALL:
        english = {
            ScanScope.SELECTION: 'Selected images only',
            ScanScope.CURRENT_CANVAS: 'The current canvas',
            ScanScope.CURRENT_PAGE: 'The current page',
            ScanScope.ALL_CANVASES: 'All canvases',
            ScanScope.ALL_ASSETS: 'Everything in the project',
        }[scope]
        assert english in zh_CN, f'{english} 没有中文翻译'


def test_the_summary_line_is_translated():
    """对话框摘要用的那两句也要有中文。"""
    from prism.i18n.zh_CN import zh_CN

    assert 'Scanned: {scope}' in zh_CN
    assert 'Scanned every canvas in the project.' in zh_CN


def test_dead_category_wording_is_gone():
    """「按分类扫描」那两句已经没人用了，翻译表里也该清掉。

    留着会让人以为界面上还有这两句，翻译新语言时也会照抄。
    """
    from prism.i18n.zh_CN import zh_CN

    assert 'Scanned the "{name}" category only.' not in zh_CN
    assert 'Scanned every canvas and category in the project.' not in zh_CN
