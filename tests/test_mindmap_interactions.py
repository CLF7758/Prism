"""脑图的三个交互缺陷：框选、右键粘贴、快捷键被画布抢走。

脑图页面跑在 QWebEngine 里，起不来也派发不了输入事件，所以页面那一半
只能盯着源码里的接线；Qt 那一半（谁拿走了 Ctrl+C/V）是真的跑起来量的。

三件事的证据都在这里：
  1. 左键框选：库的 ``useLeftKeySelectionRightKeyDrag`` 默认是「右键框选、
     左键平移」，右键又是弹出菜单的手势，于是普通左键拖动只能平移，
     框选得按住 Ctrl。改成 true 之后左键框选、右键/中键平移。
  2. 右键粘贴：菜单原来直接 ``execCommand('PASTE_NODE')``，那个命令要
     带数据，没数据就什么也不粘。现在走 ``renderer.paste()``，也就是
     工具栏和 Ctrl+V 走的同一条路，并且剪贴板读失败时退回图内的复制缓冲。
  3. 快捷键：画布的 Ctrl+C/V 原本是窗口级的，脑图这个网页面板拿到焦点
     时也会触发它 —— 粘贴会把剪贴板里的内容塞进画布，脑图自己的 Ctrl+V
     反而收不到按键。画布的快捷键必须只属于画布。
"""
import os
import re

from PyQt6 import QtCore, QtGui, QtWidgets

from prism.widgets import mindmap_panel as mm


def _page_source():
    path = os.path.join(mm.MINDMAP_DIR, 'index.html')
    with open(path, encoding='utf-8') as handle:
        return handle.read()


# ── 1. 左键框选 ────────────────────────────────────────────────────────

def test_the_left_button_box_selects_and_the_right_button_pans():
    """框选要能用普通左键拖出来。"""
    source = _page_source()
    options = re.search(r'new MindMap\(\{(.*?)\n\s*\}\);', source, re.S)
    assert options, '找不到 MindMap 的构造参数'
    assert 'useLeftKeySelectionRightKeyDrag: true' in options.group(1), \
        '框选仍然绑在右键上，普通左键拖动只会平移'


def test_the_library_default_is_not_left_selection():
    """守住这个开关的含义：库里默认是右键框选。"""
    vendor = os.path.join(mm.MINDMAP_DIR, 'vendor', 'simpleMindMap.esm.min.js')
    with open(vendor, encoding='utf-8') as handle:
        text = handle.read()
    assert 'useLeftKeySelectionRightKeyDrag:!1' in text, \
        '库的默认值变了，这个开关的含义要重新核对'


# ── 2. 右键粘贴 ────────────────────────────────────────────────────────

def test_the_context_menu_paste_passes_the_clipboard_content():
    """菜单里的粘贴要把剪贴板内容带进去，不能只敲一条空命令。"""
    source = _page_source()
    menu = re.search(r'const MENU_ITEMS = \[(.*?)\n    \];', source, re.S)
    assert menu, '找不到右键菜单'
    items = menu.group(1)
    paste = re.search(r"\['粘贴'[^\]]*\]", items)
    assert paste, '右键菜单里没有粘贴项'
    assert 'renderer.paste()' in paste.group(0), \
        '菜单粘贴没有走读剪贴板的路径'
    assert not re.search(r"execCommand\('PASTE_NODE'\)\s*\]", items), \
        '菜单粘贴仍然是不带数据的空命令，那样什么也粘不上'


def test_the_paste_falls_back_to_the_copy_buffer():
    """剪贴板读不到时，要用图内复制出来的分支兜底。"""
    source = _page_source()
    assert 'read_clipboard_error' in source, \
        '没有接住剪贴板读取失败'
    assert "mindMap.execCommand('PASTE_NODE', buffer)" in source, \
        '剪贴板读失败时没有退回复制缓冲'


def test_every_paste_entry_point_goes_through_the_renderer():
    """工具栏、右键菜单、库的 Ctrl+V 必须走同一条路。"""
    source = _page_source()
    assert 'mindMap.renderer.paste(); break;' in source, '工具栏粘贴绕开了统一入口'
    assert 'mindMap.renderer.paste.bind(mindMap.renderer)' in source, \
        '库的 Ctrl+V 没有被同一个入口接管'


# ── 3. 快捷键归属 ──────────────────────────────────────────────────────

def test_the_canvas_clipboard_shortcuts_belong_to_the_canvas(view):
    """复制/粘贴/剪切是画布的动作，快捷键作用域必须跟着画布走。"""
    from prism.actions import actions as action_defs

    for name in ('copy', 'cut', 'paste'):
        action = action_defs.actions[name].qaction
        assert action.shortcutContext() == \
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut, \
            f'{name} 的快捷键仍然是窗口级的，会抢走网页面板的按键'
        assert action.parent() is view, \
            f'{name} 的快捷键必须挂在画布上'


def test_every_canvas_action_is_scoped_to_the_canvas(view):
    """画布的动作一个都不能漏 —— 漏一个就有一个键被面板抢走。

    脑图和文档编辑器自己都绑了 Del / Ctrl+A / Ctrl+Z / Ctrl+C / Ctrl+I，
    画布上的同名动作必须让开。
    """
    from prism.actions import actions as action_defs
    from prism.actions.mixin import CANVAS_SCOPED_ACTIONS

    for name in CANVAS_SCOPED_ACTIONS:
        assert name in action_defs.actions, \
            f'{name} 不是注册表里的动作，写进清单等于没写'
        action = action_defs.actions[name].qaction
        assert action.shortcutContext() == \
            QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut, \
            f'{name} 仍然会在别的面板上抢按键'
        assert action.parent() is view, \
            f'{name} 的快捷键必须挂在画布上'


def test_the_application_shortcuts_stay_available_everywhere(view):
    """打开/保存/退出是应用级的：焦点在哪个面板都得能用。"""
    from prism.actions import actions as action_defs

    for name in ('open', 'save', 'save_as', 'quit', 'import_mindmap',
                 'export_mindmap', 'export_note_word', 'screen_capture',
                 'toggle_side_panel'):
        action = action_defs.actions[name].qaction
        assert action.shortcutContext() == \
            QtCore.Qt.ShortcutContext.WindowShortcut, \
            f'{name} 是应用级动作，不该被收窄到画布'


def test_the_actions_the_asset_browser_shares_stay_window_wide(view):
    """素材浏览器借用的那几个动作必须留在窗口级。

    ``view._material_canvas()`` 会把 select_all / fit_scene 这些动作转给
    浏览器自己的画布 —— 收窄了它们，浏览器的 Ctrl+A 就没了。
    """
    from prism.actions import actions as action_defs

    for name in ('select_all', 'deselect_all', 'fit_scene', 'fit_selection',
                 'arrange_optimal'):
        action = action_defs.actions[name].qaction
        assert action.shortcutContext() == \
            QtCore.Qt.ShortcutContext.WindowShortcut, \
            f'{name} 同时服务素材浏览器，不能收窄到画布'


def test_the_canvas_still_answers_its_own_copy_shortcut(view, qtbot):
    """作用域收窄之后，画布自己有焦点时 Ctrl+C 还得能用。"""
    from prism.actions import actions as action_defs

    action = action_defs.actions['copy'].qaction
    was_enabled = action.isEnabled()
    fired = []

    def record():
        fired.append('copy')

    action.triggered.connect(record)
    try:
        # 复制动作平时随画布选区开关，这里临时打开。
        action.setEnabled(True)
        view.setFocus()
        qtbot.wait(20)
        qtbot.keyClick(view, QtCore.Qt.Key.Key_C,
                       QtCore.Qt.KeyboardModifier.ControlModifier)
    finally:
        action.setEnabled(was_enabled)
        action.triggered.disconnect(record)
    assert fired == ['copy'], '画布自己有焦点时，复制快捷键必须照常触发'


def test_a_window_wide_shortcut_steals_the_key_from_the_panel(qtbot):
    """这是改之前的那个行为：窗口级快捷键在网页面板拿到焦点时照样响。"""
    window = QtWidgets.QMainWindow()
    canvas = QtWidgets.QWidget(window)
    panel = QtWidgets.QWidget(window)
    layout = QtWidgets.QVBoxLayout(window)
    layout.addWidget(canvas)
    layout.addWidget(panel)
    fired = []
    action = QtGui.QAction('Copy', canvas)
    action.setShortcuts([QtGui.QKeySequence('Ctrl+C')])
    action.setShortcutContext(QtCore.Qt.ShortcutContext.WindowShortcut)
    action.triggered.connect(lambda: fired.append('copy'))
    canvas.addAction(action)
    window.show()
    qtbot.addWidget(window)
    panel.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(panel, QtCore.Qt.Key.Key_C,
                   QtCore.Qt.KeyboardModifier.ControlModifier)

    assert fired == ['copy'], \
        '窗口级快捷键没有抢走按键 —— 那这次改动的前提就变了'


def test_the_canvas_scoped_shortcut_leaves_the_key_to_the_panel(qtbot):
    """改之后：面板拿到焦点时，画布的快捷键不再响。"""
    window = QtWidgets.QMainWindow()
    canvas = QtWidgets.QWidget(window)
    panel = QtWidgets.QWidget(window)
    layout = QtWidgets.QVBoxLayout(window)
    layout.addWidget(canvas)
    layout.addWidget(panel)
    fired = []
    action = QtGui.QAction('Copy', canvas)
    action.setShortcuts([QtGui.QKeySequence('Ctrl+C')])
    action.setShortcutContext(
        QtCore.Qt.ShortcutContext.WidgetWithChildrenShortcut)
    action.triggered.connect(lambda: fired.append('copy'))
    canvas.addAction(action)
    window.show()
    qtbot.addWidget(window)

    panel.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(panel, QtCore.Qt.Key.Key_C,
                   QtCore.Qt.KeyboardModifier.ControlModifier)
    assert fired == [], '画布的快捷键仍然抢了网页面板的按键'

    canvas.setFocus()
    qtbot.wait(10)
    qtbot.keyClick(canvas, QtCore.Qt.Key.Key_C,
                   QtCore.Qt.KeyboardModifier.ControlModifier)
    assert fired == ['copy'], '画布自己有焦点时快捷键必须照常'
