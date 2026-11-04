"""跨画布移动的**命令入口**（拖拽之外的那条路）。

把素材拖到左栏资源树的画布行上，一直是能用的（`move_items_to_canvas`）。
这个文件钉的是**另一条**路：一个 QAction + 快捷键，外加左栏资源树右键
菜单里的「把选中的素材移到这里」。

要钉住的事：

  · 入口真的在：动作挂在主窗口上、右键菜单里有那一项，而且快捷键没跟
    别的动作撞车 —— 撞车的那个动作等于永远失灵；
  · 目标必须是工程里还活着的**画布**：回收站里的页、文档页、脑图页、根
    本不存在的 id 一律拒绝；拒绝的时候不报成功、不压撤销栈、不改归属；
  · 保存会话认下这次移动，存出来的文件归属正确；撤销 / 重做之后归属跟
    着回来，再存一次还是对的；
  · 移动只改归属：原始字节和附件一个字节都不动。
"""
import json
import sqlite3

import pytest
from PyQt6 import QtCore, QtGui

from prism.items import PrismPixmapItem
from prism.partial_project import PartialProjectSession
from prism.widgets.media_library import MOVE_TO_CANVAS_SHORTCUT

from tests.test_partial_project_gui import CANVAS_A, CANVAS_B, project, rows


def page(page_id, kind='canvas', title=None):
    return {'id': page_id, 'kind': kind, 'title': title or page_id, 'tags': []}


def image_item(scene, canvas_id, filename='needle.png'):
    image = QtGui.QImage(10, 10, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    item = PrismPixmapItem(image, filename=filename)
    item._canvas_id = canvas_id
    scene.addItem(item)
    return item


def canvas_session(project, *, pages=('A', 'B'), scene_pages=None,
                   default_canvas=True, pages_complete=False):
    """把画布页写进工程文件，并给当前这一页开一个保存会话。

    ``scene_pages`` 说**打开的场景**知道哪些页。文件里有、场景里没有的
    页就是「按页打开」的那种：工程文件知道它，会话只能凭自己那份小页
    记录来认这次移动。

    ``default_canvas=False`` 造的是老工程：文件里根本没有默认画布这一
    行，只有隐式的默认画布。
    """
    with sqlite3.connect(project.source) as db:
        for key in pages:
            db.execute('INSERT OR REPLACE INTO workspace_pages VALUES(?,?,?,?,?)',
                       (key, 'canvas', key, '{}', 3))
        # The resource tree and the material library are whole-table payloads:
        # a save session that does not cover them refuses to save as soon as
        # the scene holds any of them.  The shared fixture's rows predate the
        # "payload carries its own id" rule, so they are dropped here and the
        # session covers the (now empty) domain instead.
        db.execute('DELETE FROM material_records')
        db.execute('DELETE FROM workspace_nodes')
        if default_canvas:
            db.execute("UPDATE workspace_pages SET kind='canvas' "
                       "WHERE id='default-canvas'")
        else:
            db.execute("DELETE FROM workspace_pages WHERE id='default-canvas'")
    project.scene.workspace_pages = ([page('A')] if scene_pages is None
                                     else list(scene_pages))
    panel = project.view.category_panel
    panel.rebuild_workspace_tree()
    # The scene has to really **be** on canvas 'A'.  The panel isolates
    # canvases: an asset that belongs to another canvas is hidden
    # (``MediaLibraryPanel._apply_filter``), and Qt refuses to select an
    # invisible item -- so a scene that still sits on 'default-canvas' has
    # no selection at all and the command bails out before it moves
    # anything.  ``open_workspace_page`` is the regular way to switch
    # canvas; it sets ``view.current_canvas_id`` and recomputes the filter.
    project.window.open_workspace_page('canvas', 'A')
    session = PartialProjectSession.open(
        project.source, item_ids=CANVAS_A,
        page_ids=tuple(p['id'] for p in project.scene.workspace_pages),
        pages_complete=pages_complete, include_boards=True, include_domains=True)
    session.attach(project.scene)
    return session


def select(item):
    item.setSelected(True)
    return item


def follow(project, item, page_id):
    """Come back to the canvas the asset now lives on and select it again."""
    project.window.open_workspace_page('canvas', page_id)
    select(item)


def move_and_follow(project, panel, item, page_id):
    """Move the asset, then come back to the canvas it now lives on.

    Moving an asset to another canvas takes it **off** the current canvas
    -- that is the canvas-ownership rule in ``_apply_filter`` -- so it is
    no longer visible and no longer selected.  The user gets it back by
    switching to that canvas, and so has the test before it can move the
    same asset a second time.
    """
    assert panel.move_selected_to_canvas(page_id) is True
    follow(project, item, page_id)


def node_of(panel, page_id):
    node = next(n for n in panel.resource_service.nodes
                if n.get('pageId') == page_id)
    return node


# ── 入口本身 ──────────────────────────────────────────────────

def test_the_command_is_registered_on_the_window_with_a_shortcut(main_window):
    window = main_window
    panel = window.view.category_panel
    action = panel.move_to_canvas_action

    assert action.shortcut() == QtGui.QKeySequence(MOVE_TO_CANVAS_SHORTCUT)
    assert action in window.actions(), '动作要挂在主窗口上，左栏收起也能用'
    assert action.shortcutContext() == QtCore.Qt.ShortcutContext.WindowShortcut


def test_the_shortcut_is_not_taken_by_another_action(main_window):
    """两个动作抢一个键只有一个能触发 —— 那个动作等于永远失灵。"""
    from prism.actions.actions import actions
    used = set()
    for action in actions.values():
        used.update(str(shortcut) for shortcut in action.get_shortcuts())
    assert MOVE_TO_CANVAS_SHORTCUT not in used


def test_the_tree_right_click_offers_the_entry_on_a_canvas_row(main_window):
    window = main_window
    panel = window.view.category_panel
    window.view.scene.workspace_pages = [
        page('canvas-a', title='Canvas A'),
        page('canvas-b', title='Canvas B'),
        page('doc-1', kind='document', title='Doc'),
    ]
    panel.rebuild_workspace_tree()
    model = panel.resource_tree.model()

    def labels(page_id):
        index = model.index_for_id(node_of(panel, page_id)['id'])
        assert index.isValid()
        return [action.text() for action in panel.resource_tree.menu_for_index(index).actions()]

    assert any('Move the selected assets here' in text for text in labels('canvas-b'))
    assert not any('Move the selected assets here' in text
                   for text in labels('doc-1')), '文档页不是画布，不能给这个入口'


# ── 移动本身 ──────────────────────────────────────────────────

def test_the_command_moves_the_selected_assets_to_a_loaded_canvas(project):
    canvas_session(project, pages=('A', 'B'),
                   scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True

    assert item.canvas_id == 'B'
    assert project.scene.undo_stack.count() == 1
    assert 'moved' in project.window.statusBar().currentMessage().lower()


def test_the_command_moves_to_a_canvas_the_file_knows_but_the_scene_did_not(project):
    """按页打开：文件里有 B，场景里没装 B —— 移动照样成立。"""
    canvas_session(project, pages=('A', 'B'), scene_pages=[page('A')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True

    assert item.canvas_id == 'B'
    assert project.scene.undo_stack.count() == 1


def test_the_command_moves_to_a_canvas_whose_assets_were_never_loaded(project):
    """B 在场景的页面清单里，但 B 的素材一个都没载入。"""
    canvas_session(project, pages=('A', 'B'),
                   scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True
    assert item.canvas_id == 'B'
    assert [i for i in project.scene.items()
            if getattr(i, 'canvas_id', None) == 'B'
            and getattr(i, 'save_id', None) in CANVAS_B] == []


def test_the_default_canvas_and_a_normal_canvas_move_both_ways(project):
    """普通页 ↔ 默认页，双向都成立。"""
    canvas_session(project, pages=('A', 'B'),
                   scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])

    move_and_follow(project, panel, item, 'default-canvas')
    assert item.canvas_id == 'default-canvas'
    move_and_follow(project, panel, item, 'B')
    assert item.canvas_id == 'B'


def test_the_legacy_default_canvas_is_a_valid_destination(project):
    """老工程没有默认画布这一行，但隐式的默认画布照样能去。"""
    canvas_session(project, pages=('A', 'B'), scene_pages=[page('A'), page('B')],
                   default_canvas=False)
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('default-canvas') is True
    assert item.canvas_id == 'default-canvas'


# ── 非法目标 ──────────────────────────────────────────────────

@pytest.mark.parametrize('target', ['trashed', 'document', 'mindmap', 'missing'])
def test_illegal_destinations_are_refused(project, target):
    session = canvas_session(project, pages=('A', 'B'),
                             scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])
    before = project.scene.undo_stack.count()
    project.window.statusBar().clearMessage()

    if target == 'trashed':
        panel.resource_service.trash([node_of(panel, 'B')['id']],
                                     '2026-01-01T00:00:00')
        destination = 'B'
    elif target in ('document', 'mindmap'):
        kind = 'document' if target == 'document' else 'mindmap'
        project.scene.workspace_pages.append(page('other', kind=kind))
        panel.rebuild_workspace_tree()
        destination = 'other'
    else:
        destination = 'no-such-canvas'

    assert panel.move_selected_to_canvas(destination) is False

    # 失败不报成功：不压撤销栈、不改归属、状态栏说的是原因。
    assert project.scene.undo_stack.count() == before
    assert item.canvas_id == 'A'
    message = project.window.statusBar().currentMessage()
    assert message, '拒绝的时候必须给用户一句话'
    assert 'moved' not in message.lower()
    assert session.invalid is None


# ── 保存 ──────────────────────────────────────────────────────

def test_the_saved_file_carries_the_new_owner(project, qtbot):
    session = canvas_session(project, pages=('A', 'B'),
                             scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True
    target = project.source.parent / 'moved.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'B'
    assert session.invalid is None


def test_undo_and_redo_keep_the_owner_and_the_saved_file_follows(project, qtbot):
    session = canvas_session(project, pages=('A', 'B'),
                             scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])
    target = project.source.parent / 'moved.prism'

    assert panel.move_selected_to_canvas('B') is True
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)
    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'B'

    project.scene.undo_stack.undo()
    assert item.canvas_id == 'A'
    project.view.do_save(str(target), create_new=False)
    qtbot.waitUntil(lambda: session.revision == 2, timeout=15000)
    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'A'

    project.scene.undo_stack.redo()
    assert item.canvas_id == 'B'
    project.view.do_save(str(target), create_new=False)
    qtbot.waitUntil(lambda: session.revision == 3, timeout=15000)
    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'B'
    assert session.invalid is None


def test_a_second_move_during_the_save_session_is_authorized(project, qtbot):
    """保存会话进行中再移一次：两次都得被认下，保存不能冲突。"""
    session = canvas_session(project, pages=('A', 'B', 'C'),
                             scene_pages=[page('A'), page('B'), page('C')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True
    follow(project, item, 'B')
    assert panel.move_selected_to_canvas('C') is True
    assert item.canvas_id == 'C'

    target = project.source.parent / 'moved-twice.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'C'
    assert session.invalid is None


def test_the_move_leaves_the_original_bytes_and_the_attachments_alone(project, qtbot):
    """移动只改归属：原始字节、附件一个字节都不动。"""
    session = canvas_session(project, pages=('A', 'B'),
                             scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True
    target = project.source.parent / 'moved.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    assert rows(target, 'sqlar') == rows(project.source, 'sqlar')
    assert rows(target, 'attachments') == rows(project.source, 'attachments')


# ── 计数 / 目录 / 搜索 ────────────────────────────────────────

def test_the_counts_and_the_search_follow_the_new_owner(main_window):
    window = main_window
    panel = window.view.category_panel
    window.view.scene.workspace_pages = [
        page('canvas-a', title='Canvas A'),
        page('canvas-b', title='Canvas B'),
    ]
    panel.rebuild_workspace_tree()
    window.open_workspace_page('canvas', 'canvas-a')
    item = image_item(window.view.scene, 'canvas-a')
    panel.update_counts()

    counts = panel.catalog.count_snapshot()['canvases']
    assert counts.get('canvas-a') == 1
    assert not counts.get('canvas-b')
    found = [r for r in panel.project_search_results('needle')
             if r['kind'] == 'asset']
    assert found and found[0]['path'] == 'Canvas A'

    select(item)
    assert panel.move_selected_to_canvas('canvas-b') is True

    counts = panel.catalog.count_snapshot()['canvases']
    assert not counts.get('canvas-a')
    assert counts.get('canvas-b') == 1
    found = [r for r in panel.project_search_results('needle')
             if r['kind'] == 'asset']
    assert found and found[0]['path'] == 'Canvas B'


# ── 失败不显示成功 ────────────────────────────────────────────

def test_without_a_selection_the_command_says_so_and_changes_nothing(project):
    canvas_session(project, pages=('A', 'B'),
                   scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    project.scene.clearSelection()
    before = project.scene.undo_stack.count()
    project.window.statusBar().clearMessage()

    assert panel.move_selected_to_canvas('B') is False

    assert project.scene.undo_stack.count() == before
    message = project.window.statusBar().currentMessage()
    assert 'select' in message.lower()
    assert 'moved' not in message.lower()


def test_moving_to_the_canvas_the_assets_are_already_on_does_nothing(project):
    canvas_session(project, pages=('A', 'B'),
                   scene_pages=[page('A'), page('B')])
    panel = project.view.category_panel
    item = select(project.items[0])
    before = project.scene.undo_stack.count()
    project.window.statusBar().clearMessage()

    assert panel.move_selected_to_canvas('A') is False

    assert project.scene.undo_stack.count() == before
    assert item.canvas_id == 'A'
    assert 'moved' not in project.window.statusBar().currentMessage().lower()
