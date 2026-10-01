"""文档面板与脑图面板的端到端测试。

**这个文件曾经只能靠子进程绕。**

这两个面板靠 QWebEngine（内嵌 Chromium）。在 pytest 里建 `QWebEnginePage`
会 **0xC0000409** —— 进程被立即杀掉，连 `faulthandler` 都来不及输出。
查过一圈，**都不是**原因：import 顺序、`pytest-qt`（禁了还是崩）、
`conftest` 的模块级代码、`CommandlineArgs`（建 app 前
`QApplication.instance()` 是 `None`）。

**真根因：`qapp` fixture 用了 `PrismApplication([])` —— 空的参数表。**

三组对照实验（都在真实平台、都带 `-s`，其余完全相同）：

    QApplication([sys.argv[0]])        -> 通过
    PrismApplication([sys.argv[0]])    -> 通过
    PrismApplication([])               -> exit=-1073740791（崩）

`QApplication(argv)` 的 `argv` 不能为空：Qt 会保留 `argv[0]` 当程序名，
空列表等于让后面用它的东西踩空指针。在 Chromium 初始化那里就炸了。
`tests/conftest.py` 已经改成 `PrismApplication([sys.argv[0] or 'prism'])`。

**剩下的唯一限制是平台**：`QT_QPA_PLATFORM=offscreen` 下 WebEngine 建不
起来（那是平台限制，不是产品问题）。所以这组测试在 offscreen 会话里
跳过 —— 跑它们要真实平台：

    $env:QT_QPA_PLATFORM=''      # 或者删掉这个变量
    .\\.venv\\Scripts\\python -u -m pytest tests/test_web_panels.py

**⚠️ import 顺序**：这两个面板的 import 放在模块顶层。建了 `QApplication`
之后再 import `QtWebEngine*` 会**卡死在 Chromium 初始化**（实测 5 分钟没
动静，不报错也不崩），先 import 的话两秒完成。所以**别把这些 import
挪进 fixture**。
"""
import os
import sys
import time

import pytest

# ① import 在最前（见文件开头的"import 顺序"）
from PyQt6 import QtGui

from prism.view import PrismGraphicsView
from prism.widgets.document_panel import DocumentPanel, editor_available
from prism.widgets.mindmap_panel import MindMapPanel, mindmap_available


def _headless():
    name = (os.environ.get('QT_QPA_PLATFORM') or '').strip().lower()
    return name in ('offscreen', 'minimal', 'minimalegl')


pytestmark = pytest.mark.skipif(
    _headless(),
    reason='offscreen 平台下 QWebEnginePage 建不起来 —— '
           '那是平台限制，不是产品问题。要跑这组就用真实平台'
           '（不设 QT_QPA_PLATFORM）')


def spin(qapp, seconds=0.02):
    """转一会儿事件循环 —— WebEngine 全是异步的。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        time.sleep(0.005)


def wait_for(qapp, predicate, seconds=25):
    """跑到条件成立或超时。不依赖 pytest-qt 的 waitUntil，少一层变数。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    return False


def make_window(qapp):
    """一个带 view 的窗口。

    两个面板要的都是 **view**（`DocumentPanel._scene_html()` 走
    `self.view.scene`），而 view 自己需要一个 parent
    （它会 `self.parent.setWindowTitle(...)`）。
    """
    from PyQt6 import QtWidgets

    window = QtWidgets.QMainWindow()
    view = PrismGraphicsView(qapp, window)
    return window, view


@pytest.fixture
def host(qapp):
    window, view = make_window(qapp)
    yield window, view
    window.close()


@pytest.fixture
def document(qapp, host):
    """起好的文档面板。"""
    window, view = host
    panel = DocumentPanel(view)
    panel.resize(900, 600)
    window.setCentralWidget(panel)
    window.show()
    panel.show()
    assert wait_for(qapp, lambda: getattr(panel, '_ready', False), 30), \
        '文档面板 30 秒没起来'
    yield panel
    panel.close()
    spin(qapp, 0.05)


@pytest.fixture
def mindmap(qapp, host):
    """起好的脑图面板。

    **共用 `host` 的 view** —— `actions` 是模块级单例，建第二个 view 时
    `build_menu_and_actions()` 会取到 `action.callback = None` 然后抛
    `TypeError`（`conftest` 的 `reset_prism_actions` fixture 就是管这个的）。
    """
    window, view = host
    panel = MindMapPanel(view)
    panel.resize(900, 600)
    window.setCentralWidget(panel)
    window.show()
    panel.show()
    assert wait_for(qapp, lambda: getattr(panel, '_ready', False), 30), \
        '脑图面板 30 秒没起来'
    yield panel
    panel.close()
    spin(qapp, 0.05)


# ── 面板起得来 ──────────────────────────────────────────────────

def test_the_editor_assets_are_there():
    assert editor_available() is True, '编辑器资源不在'


def test_the_mindmap_assets_are_there():
    assert mindmap_available() is True, '脑图资源不在'


def test_the_document_panel_starts(qapp, document):
    """Chromium 起来了、编辑器页面加载完了。"""
    assert getattr(document, '_ready', False) is True
    assert document._web_view is not None


def test_the_mindmap_panel_starts(qapp, mindmap):
    assert getattr(mindmap, '_ready', False) is True
    assert mindmap.web_view is not None


# ── 文档：内容往返 ──────────────────────────────────────────────

def test_text_comes_back(qapp, document):
    """**核心那条**：推进去的文字要能原样读回来。

    这条是整个文档功能的地基 —— 用户打的字存不下来，别的都白搭。
    """
    document.set_html('<p>第一段</p><p>第二段</p>')
    assert wait_for(qapp, lambda: '第一段' in (document.html() or ''), 20), \
        '推过去的 HTML 没读回来'
    got = document.html() or ''
    assert '第一段' in got
    assert '第二段' in got


def test_formatting_survives(qapp, document):
    """加粗、斜体、标题、列表都要保住 —— "富文本"的全部意义。"""
    document.set_html('<p><b>粗体</b>和<i>斜体</i></p>'
                      '<h1>大标题</h1><ul><li>第一项</li></ul>')
    assert wait_for(qapp, lambda: '第一项' in (document.html() or ''), 20), \
        '内容没回来'
    got = (document.html() or '').lower()
    assert '<b' in got or '<strong' in got, '加粗丢了'
    assert '<i' in got or '<em' in got, '斜体丢了'
    assert '<h1' in got, '标题层级丢了'
    assert '<li' in got, '列表项丢了'


def test_the_text_is_plain(qapp, document):
    """`text()` 给纯文本，不带标签。"""
    document.set_html('<p>甲</p><p>乙</p>')
    assert wait_for(qapp, lambda: '甲' in (document.text() or ''), 20)
    plain = document.text() or ''
    assert '甲' in plain and '乙' in plain
    assert '<' not in plain, f'纯文本里不该有标签：{plain!r}'


def test_special_characters_survive(qapp, document):
    """emoji、尖括号、和号 —— 编码和转义都容易出错的地方。

    `<` `>` `&` 是 HTML 保留字符；emoji 超出基本多文种平面。
    """
    document.set_html('<p>中文 emoji 😀 符号 &lt;tag&gt; &amp; 结束</p>')
    assert wait_for(qapp, lambda: 'emoji' in (document.html() or ''), 20)
    plain = document.text() or ''
    assert '😀' in plain, f'emoji 丢了：{plain!r}'
    assert '<tag>' in plain, f'尖括号丢了：{plain!r}'
    assert '&' in plain, f'和号丢了：{plain!r}'


def test_an_empty_document_is_handled(qapp, document):
    """空文档不该炸 —— 新页面就是这个状态。"""
    document.set_html('')
    spin(qapp, 0.3)
    assert isinstance(document.html(), str)
    assert isinstance(document.text(), str)


def test_the_editor_shim_forwards(qapp, document):
    """`panel.editor` 那三个方法要转发到面板自己。

    `prism/view.py` 是通过 `panel.editor.setHtml()` 这些调的（老接口），
    而底下没有 QTextDocument —— shim 得转发对。
    """
    document.set_html('<p>走 shim</p>')
    assert wait_for(qapp, lambda: '走 shim' in (document.html() or ''), 20), \
        'editor shim 没转发到面板'


# ── 文档：未保存状态（任务书 §8 第一类）────────────────────────

def test_typing_marks_the_project_dirty(qapp, document):
    """**§8 第一类的端到端验收**：「文档输入和格式修改必须触发未保存状态」。

    走 `_on_content_changed()` —— 那是 Quill 页面对 Python 的回报，
    也就是用户真打字时的那条路。
    """
    view = document.view
    view.undo_stack.setClean()
    view._content_dirty = False
    assert view.is_dirty() is False, '前置：该是干净的'

    document._on_content_changed('<p>用户打了一些字</p>')
    assert view.is_dirty() is True, (
        '在文档里打字之后工程该是脏的 —— '
        '不然关窗口时这段内容会被静默丢掉')


def test_loading_a_document_does_not_mark_dirty(qapp, document):
    """反过来**不该**标脏：程序化加载不算用户的改动。

    `set_html()` 是从工程加载走的那条路 —— 读一遍文件怎么会算改动。
    要是那种也标脏，用户打开工程之后随便点两下就会被问"要保存吗"。
    """
    view = document.view
    view.undo_stack.setClean()
    view._content_dirty = False

    document.set_html('<p>从文件读进来的</p>')
    assert wait_for(qapp, lambda: '从文件读' in (document.html() or ''), 20)
    assert view.is_dirty() is False, '程序化 set_html 不该标脏'


# ── 脑图：导出 ──────────────────────────────────────────────────

def test_exporting_a_snapshot_writes_a_png(qapp, mindmap, tmp_path):
    """**核心那条**：导出快照要真的落一个 PNG。

    任务书 T5 要求「导出脑图为 PNG 或 SVG」—— 这条验 PNG 那条。
    """
    target = str(tmp_path / '脑图.png')
    mindmap.export_snapshot(target)

    assert wait_for(qapp, lambda: os.path.exists(target), 25), \
        'export_snapshot 没写出文件'

    size = os.path.getsize(target)
    assert size > 1000, f'导出的 PNG 才 {size} 字节，太小了'

    with open(target, 'rb') as handle:
        header = handle.read(8)
    assert header == b'\x89PNG\r\n\x1a\n', f'不是一个 PNG：{header!r}'


def test_the_exported_png_has_real_size(qapp, mindmap, tmp_path):
    """尺寸要像样 —— 0×0 或者 1×1 也算"写出文件了"，但那是坏的。"""
    target = str(tmp_path / '尺寸.png')
    mindmap.export_snapshot(target)
    assert wait_for(qapp, lambda: os.path.exists(target), 25), '没导出'

    image = QtGui.QImage(target)
    assert not image.isNull(), 'PNG 读不回来'
    assert image.width() > 50, f'宽才 {image.width()}'
    assert image.height() > 50, f'高才 {image.height()}'


def test_empty_mindmap_operations_are_safe(qapp, mindmap):
    """空脑图上重置视图、要树，都不该炸。"""
    mindmap.reset_view()
    mindmap.request_tree()
    spin(qapp, 0.3)
