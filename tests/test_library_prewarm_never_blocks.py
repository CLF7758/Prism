"""预热不得再霸占窗口：让路、限速、加速，以及 Chromium 的成本提前付。

线上会话 `Prism-operations-156040.jsonl`（用户实测：模型面板约 1.59 秒、
材质面板约 1.87 秒）里预热期间的真实构成：

* GUI 线程实占 **1374.1ms**（模型库 914ms / 材质库 460ms），落在 1.795 秒
  的窗口里 —— 窗口一直被占着，用户看到的就是"卡住"。
* 最长的一次**硬冻结 536ms** = `materials.cards.create_surface`：
  Chromium 每个进程只付一次的启动成本（真实工程 cProfile：3 次 `page()`
  合计 199ms），它落在可见窗口里。
* 材质库侧最长间隙只有 26.9ms —— 那一侧本来就是健康的。

这里逐条钉住"以后不会再退化"：

1. Chromium 的启动成本在**窗口出现之前**付掉（`warm_web_engine`）；
2. 构造在**用户操作时让路**；
3. 构造**限速**，片与片之间给事件循环留出空隙；
4. 用户**点击**某个库时，构造解除限速，不必等完整节奏跑完。
"""
from __future__ import annotations

import time

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism.user_activity import USER_ACTIVITY
from prism.widgets.deferred_library import DeferredLibrary


@pytest.fixture
def quiet_user():
    """The activity marker is process wide: hand the test a clean slate."""
    USER_ACTIVITY.reset()
    yield USER_ACTIVITY
    USER_ACTIVITY.reset()


class _Page(QtWidgets.QWidget):
    content_ready = QtCore.pyqtSignal()

    def __init__(self):
        super().__init__()
        self.details = QtWidgets.QWidget()
        self.ready = True

    def is_content_ready(self):
        return self.ready


def _stepping_factory(steps):
    """A panel that is assembled in several slices, like the real ones."""
    def factory():
        page = _Page()
        steps.append('a')
        yield
        steps.append('b')
        yield
        return page
    return factory


# ── Chromium 的成本提前付 ──────────────────────────────────────

def test_the_web_engine_starts_before_the_window_is_visible(qtbot):
    """第一个 Web 面贵 200ms 以上；预热之后必须便宜。"""
    from prism.webengine_runtime import warm_web_engine

    app = QtWidgets.QApplication.instance()
    warm = warm_web_engine(app)
    assert warm is not None

    from PyQt6 import QtWebEngineWidgets
    started = time.perf_counter()
    view = QtWebEngineWidgets.QWebEngineView()
    view.page()
    view.setHtml('<html><body></body></html>')
    app.processEvents()
    elapsed_ms = (time.perf_counter() - started) * 1000
    view.setPage(None)
    view.deleteLater()
    app.processEvents()
    # 便宜的意思是"不再是那 200 多毫秒的一次性成本"。
    assert elapsed_ms < 100, f'预热之后仍然要 {elapsed_ms:.1f}ms'


def test_web_engine_warmup_reuses_one_invisible_surface(qapp):
    from prism.webengine_runtime import warm_web_engine
    first = warm_web_engine(qapp)
    assert warm_web_engine(qapp) is first
    assert not first.isVisible()
    assert first.testAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)


def test_the_font_engine_starts_before_the_window_is_visible(qapp):
    """第一个文本控件贵 138-202ms；预热之后必须便宜。

    实测（本机，windows 平台）：第一个 QLabel 的 `show()` 花 138.6ms，
    第二个只花 18.3ms。真实会话里这笔账落在窗口出现之后 —— trace 把
    502.4ms 的停顿记在 `welcome_overlay.py:110`（窗口里的第一个 QLabel）。
    """
    from prism.webengine_runtime import warm_text_layout

    warm = warm_text_layout(qapp)
    assert warm is not None
    assert warm_text_layout(qapp) is warm
    assert not warm.isVisible()
    assert warm.testAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)

    started = time.perf_counter()
    label = QtWidgets.QLabel('<h3>Recent Files</h3>')
    label.resize(240, 60)
    label.show()
    qapp.processEvents()
    elapsed_ms = (time.perf_counter() - started) * 1000
    label.setParent(None)
    label.deleteLater()
    qapp.processEvents()
    # 便宜的意思是"不再是那 138-202ms 的一次性成本"。
    assert elapsed_ms < 100, f'预热之后仍然要 {elapsed_ms:.1f}ms'


def test_drag_motion_is_activity_but_hover_is_not(qapp, qtbot, quiet_user):
    receiver = QtWidgets.QWidget()
    qtbot.addWidget(receiver)
    def motion(buttons):
        return QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(2, 2),
            QtCore.QPointF(2, 2), QtCore.Qt.MouseButton.NoButton, buttons,
            QtCore.Qt.KeyboardModifier.NoModifier)
    qapp.notify(receiver, motion(QtCore.Qt.MouseButton.NoButton))
    assert quiet_user.seconds_since_input() == float('inf')
    qapp.notify(receiver, motion(QtCore.Qt.MouseButton.LeftButton))
    assert not quiet_user.is_idle(.35)


# ── 让路 ───────────────────────────────────────────────────────

def test_the_prewarm_waits_while_the_user_is_working(qtbot, quiet_user):
    """用户正在操作时，面板组装必须停手。"""
    steps = []
    host = DeferredLibrary(_stepping_factory(steps))
    qtbot.addWidget(host)

    quiet_user.note_input()
    job = host.prewarm()
    qtbot.wait(200)
    assert steps == [], f'用户还在操作，组装却推进了：{steps}'

    quiet_user.reset()
    qtbot.waitUntil(lambda: host.initialized, timeout=10000)
    assert steps == ['a', 'b']


def test_the_prewarm_continues_when_the_user_is_idle(qtbot, quiet_user):
    """没有人操作时，组装照常进行（不能被让路逻辑卡死）。"""
    steps = []
    host = DeferredLibrary(_stepping_factory(steps))
    qtbot.addWidget(host)

    host.prewarm()
    qtbot.waitUntil(lambda: host.initialized, timeout=10000)
    assert steps == ['a', 'b']


# ── 点击时加速 ─────────────────────────────────────────────────

def test_a_click_expedites_the_construction(qtbot, quiet_user):
    """用户在等某个库：组装不能还在按预热节奏慢慢来。"""
    steps = []
    host = DeferredLibrary(_stepping_factory(steps))
    qtbot.addWidget(host)

    job = host.prewarm()
    assert job.expedited is False
    host.ensure_created()
    assert job.expedited is True
    assert host.initialized


# ── 限速：窗口不能被占满 ───────────────────────────────────────

def test_the_prewarm_never_holds_the_window_for_long(main_window, qtbot, quiet_user):
    """预热期间 GUI 最长间隙必须有上限（窗口不能被占住不放）。"""
    from prism.webengine_runtime import warm_web_engine

    window = main_window
    window.resize(1440, 900)
    window.show()
    # 应用启动时已经付过 Chromium 的成本；这里保持一致。
    warm_web_engine(QtWidgets.QApplication.instance())

    ticks = [time.perf_counter()]
    timer = QtCore.QTimer(window)
    timer.setInterval(16)
    timer.timeout.connect(lambda: ticks.append(time.perf_counter()))
    timer.start()
    try:
        window.schedule_library_prewarm()
        qtbot.waitUntil(lambda: window.material_library.initialized, timeout=60000)
    finally:
        timer.stop()
        timer.deleteLater()
    ticks.append(time.perf_counter())
    longest_ms = max(b - a for a, b in zip(ticks, ticks[1:])) * 1000
    assert longest_ms < 150, f'预热期间 GUI 最长间隙 {longest_ms:.1f}ms'


def test_the_prewarm_starts_right_after_the_open_settles(main_window, qtbot):
    """预热不能被推迟：推迟只会让用户更可能在面板建好之前点进来。"""
    window = main_window
    window.show()
    window.schedule_library_prewarm()
    # 预热是**限速**的（片与片之间给事件循环留空隙），所以它不会在一拍之内
    # 把整个面板建完；但它必须立刻开始，不能拖着。
    qtbot.wait(50)
    assert window._library_prewarm_scheduled is False
    assert window.model_library._construction is not None
