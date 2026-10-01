"""T4：脑图面板的行为。

脑图页面跑在 QWebEngine 里，启动很贵，所以这里分开测：
纯函数（签名、载荷解析）完全不碰界面；面板逻辑通过把 _ready 摆成
两种状态来覆盖，不需要真的把页面拉起来。

重点守两条真实踩过的坑：
  1. 导入会绕过面板替换掉树，面板必须能发现并重新推送（否则页面
     还显示旧图）
  2. 页面还没就绪时推送不能丢，要排队等它 ready
"""
import json

import pytest

from prism.widgets import mindmap_panel as mm


# ── content_signature：忽略视图滚动位置 ────────────────────────────────

def test_signature_ignores_the_scroll_position():
    """_prism_view 是页面滚到哪儿了，不该算内容变化。"""
    first = {'root': 'A', 'children': [], '_prism_view': {'x': 0, 'y': 0}}
    second = {'root': 'A', 'children': [], '_prism_view': {'x': 999, 'y': 42}}

    assert mm.content_signature(first) == mm.content_signature(second)


def test_signature_notices_real_changes():
    first = {'root': 'A', 'children': []}
    second = {'root': 'B', 'children': []}

    assert mm.content_signature(first) != mm.content_signature(second)


def test_signature_does_not_care_about_key_order():
    first = {'root': 'A', 'children': [{'root': 'B'}]}
    second = {'children': [{'root': 'B'}], 'root': 'A'}

    assert mm.content_signature(first) == mm.content_signature(second)


def test_signature_of_a_non_tree_is_empty():
    for value in (None, '', 42, [], 'not a tree'):
        assert mm.content_signature(value) == ''


# ── parse_tree_payload：页面发回来的东西 ───────────────────────────────

def test_payload_may_be_empty():
    assert mm.parse_tree_payload(None) == (None, {})
    assert mm.parse_tree_payload('') == (None, {})


def test_payload_that_is_not_json_is_ignored():
    """页面偶尔会发半截数据，不能因此崩掉整个面板。"""
    assert mm.parse_tree_payload('{not json at all') == (None, {})


def test_payload_carries_the_tree_and_its_images():
    payload = json.dumps({'tree': {'root': 'A'}, 'images': {'i.png': 'data'}})
    tree, images = mm.parse_tree_payload(payload)

    assert tree == {'root': 'A'}
    assert images == {'i.png': 'data'}


def test_a_bare_tree_is_accepted():
    """老页面直接发树，没有外层的 {tree, images}。"""
    payload = json.dumps({'root': 'A'})
    tree, images = mm.parse_tree_payload(payload)

    assert tree == {'root': 'A'}
    assert images == {}


def test_broken_images_field_does_not_poison_the_tree():
    payload = json.dumps({'tree': {'root': 'A'}, 'images': 'nonsense'})
    tree, images = mm.parse_tree_payload(payload)

    assert tree == {'root': 'A'}
    assert images == {}


# ── 资源可用性 ─────────────────────────────────────────────────────────

def test_assets_are_shipped():
    """页面和它那一份 vendor 包必须都在，否则脑图会静默不可用。"""
    assert mm.mindmap_available() is True


# ── 面板：页面还没就绪时 ───────────────────────────────────────────────

@pytest.fixture
def panel(view, monkeypatch):
    widget = mm.MindMapPanel(view)
    # _ensure_loaded() 会真的建一个 QWebEngineView 并把页面拉起来。无头
    # 环境下那一步会让进程直接挂掉（0xC0000409），而这一组测试要验的是
    # 「页面就绪前后面板怎么决策」，不是页面本身能不能画出来 —— 挡掉它。
    # 构造本身是安全的，见 test_requests_are_harmless_before_the_page_is_ready。
    monkeypatch.setattr(widget, '_ensure_loaded', lambda: None)
    widget._ready = False
    yield widget
    widget.deleteLater()


def test_a_tree_arriving_before_the_page_is_queued(panel, view):
    view.scene.mindmap_tree = {'root': 'A', 'children': []}
    panel.load_from_scene()

    assert panel._pending_tree == {'root': 'A', 'children': []}
    assert panel._sent_tree is None, '还没推送过，不该记成已发'


def test_nothing_is_pushed_while_the_page_is_offline(panel):
    calls = []
    panel._push_tree_to_page = lambda tree: calls.append(tree)
    panel._ready = False            # 明确：页面没起来

    panel.load_from_scene()

    assert calls == [], '页面没就绪时不该推送'


# ── 面板：页面就绪之后 ─────────────────────────────────────────────────

def test_the_same_tree_is_not_pushed_twice(panel, view):
    """每切一次页就重推一遍整张图会闪，内容没变就该跳过。"""
    view.scene.mindmap_tree = {'root': 'A', 'children': []}
    panel._ready = True
    panel._sent_tree = {'root': 'A', 'children': []}

    calls = []
    panel._push_tree_to_page = lambda tree: calls.append(tree)
    panel.load_from_scene()

    assert calls == []


def test_a_changed_tree_is_pushed(panel, view):
    view.scene.mindmap_tree = {'root': 'B', 'children': []}
    panel._ready = True
    panel._sent_tree = {'root': 'A', 'children': []}

    calls = []
    panel._push_tree_to_page = lambda tree: calls.append(tree)
    panel.load_from_scene()

    assert calls == [{'root': 'B', 'children': []}]


def test_a_tree_replaced_behind_the_panels_back_is_pushed_again(panel, view):
    """导入脑图会直接改 scene.mindmap_tree，绕过面板。

    面板如果只跟自己那份 _tree 比，就会以为「没变」而跳过推送，
    页面上还留着旧图 —— 这个坑真踩过。
    """
    panel._ready = True
    panel._tree = {'root': 'A', 'children': []}
    panel._sent_tree = {'root': 'A', 'children': []}

    view.scene.mindmap_tree = {'root': 'imported', 'children': []}

    calls = []
    panel._push_tree_to_page = lambda tree: calls.append(tree)
    panel.load_from_scene()

    assert calls == [{'root': 'imported', 'children': []}], \
        '导入换掉的树必须重新推给页面'


# ── store_to_scene ────────────────────────────────────────────────────

def test_storing_without_a_tree_leaves_the_scene_alone(panel, view):
    view.scene.mindmap_tree = {'root': 'keep me', 'children': []}
    panel._tree = None

    panel.store_to_scene()

    assert view.scene.mindmap_tree == {'root': 'keep me', 'children': []}


def test_storing_writes_the_tree_back(panel, view):
    panel._tree = {'root': 'edited', 'children': []}

    panel.store_to_scene()

    assert view.scene.mindmap_tree == {'root': 'edited', 'children': []}


# ── 页面没就绪时的其余入口 ────────────────────────────────────────────

def test_requests_are_harmless_before_the_page_is_ready(panel):
    """这些是菜单项和按钮直接调的，不能因为页面没起来就抛异常。"""
    panel._ready = False

    panel.request_tree()
    panel.reset_view()


def test_png_export_says_so_when_the_page_never_started(panel):
    """没有页面就没法截图，但要说清楚原因，而不是丢一个空文件。"""
    panel._ready = False

    with pytest.raises(mm.MindMapExportError):
        panel.export_snapshot('whatever.png')


def test_unavailable_panel_still_reports_a_status(panel):
    panel._update_status()
    assert isinstance(panel._status.text(), str)
