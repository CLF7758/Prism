"""按需打开下的跨画布移动判定，与另存为之后的目录（catalog）重绑。

两件事在这里钉住：

  · ``PartialProjectSession._canvas_destination`` 的**目标判定**。工程文件里
    真实存在的画布就是合法目标 —— 哪怕这一页没有装它，也哪怕页面集是完整
    的（``pages_complete=True``）。反过来，根本不存在 / 在回收站 / 是文档页 /
    是脑图页的目标，一个都不能放行。

  · **另存为之后目录要重绑**。会话的 source 换了，目录还指着旧文件：旧文件
    的字节没变，旧指纹仍然成立，侧栏计数与项目搜索会继续读旧工程。会话侧的
    对外接口是 ``requires_catalog_rebuild()`` /
    ``install_source_rebind_hook()`` / ``catalog_rebuild_notice()`` /
    ``mark_catalog_rebuilt()``。
"""
import json
from pathlib import Path
import sqlite3

import pytest

from prism.fileio.partial_save import PartialSaveConflict
from prism.partial_project import PartialProjectSession

from tests.test_partial_project_gui import CANVAS_A, project


# ── 测试脚手架 ──────────────────────────────────────────────

def write_pages(project, pages, *, default_canvas=True):
    """把页面行写进工程文件。

    ``pages`` 是 ``(id, kind, data)`` 列表；``data`` 是页面 ``data`` 列里
    存的那段 JSON。
    """
    with sqlite3.connect(project.source) as db:
        for page_id, kind, data in pages:
            db.execute('INSERT OR REPLACE INTO workspace_pages VALUES(?,?,?,?,?)',
                       (page_id, kind, page_id, data, 3))
        # The domain tables are whole-table payloads: a session that covers
        # them needs every row's payload to carry its own id, and the shared
        # fixture's rows predate that rule.  Drop them so the domain
        # baseline is clean.
        db.execute('DELETE FROM material_records')
        db.execute('DELETE FROM workspace_nodes')
        if default_canvas:
            db.execute("UPDATE workspace_pages SET kind='canvas' "
                       "WHERE id='default-canvas'")
        else:
            db.execute("DELETE FROM workspace_pages WHERE id='default-canvas'")


def canvas_session(project, *, db_pages=(('A', 'canvas', '{}'),
                                         ('B', 'canvas', '{}')),
                   scene_pages=('A',), page_ids=None, pages_complete=False,
                   default_canvas=True):
    """写页面、把场景装到画布 'A' 上、开一个保存会话。

    ``db_pages``    -- **文件**里有的页面行。
    ``scene_pages`` -- **场景**装了的页面 id。
    ``page_ids``    -- **会话**加载的页面记录。``None`` 表示「场景的那些页」，
                       ``'all'`` 表示「工程里的全部页面」（页面集完整时该
                       这么写），元组表示就这些。
    """
    write_pages(project, db_pages, default_canvas=default_canvas)
    project.scene.workspace_pages = [
        {'id': key, 'kind': 'canvas', 'title': key, 'tags': []}
        for key in scene_pages]
    panel = project.view.category_panel
    panel.rebuild_workspace_tree()
    # 场景必须真的在画布 'A' 上：左栏按画布隔离素材（`_apply_filter`），
    # 而 Qt 不允许选中不可见的 item —— 场景停在 'default-canvas' 上的话，
    # 命令根本找不到选中项。
    project.window.open_workspace_page('canvas', 'A')
    if page_ids == 'all':
        with sqlite3.connect(project.source) as db:
            page_ids = tuple(row[0] for row in db.execute(
                'SELECT id FROM workspace_pages ORDER BY sort_order, rowid'))
    elif page_ids is None:
        page_ids = tuple(scene_pages)
    session = PartialProjectSession.open(
        project.source, item_ids=CANVAS_A, page_ids=tuple(page_ids),
        pages_complete=pages_complete, include_boards=True, include_domains=True)
    session.attach(project.scene)
    return session


def select(item):
    item.setSelected(True)
    return item


# ── 目标判定：文件里有的画布就是合法目标 ────────────────────

def test_a_canvas_the_file_knows_is_a_destination_with_a_complete_page_scope(project):
    """按需打开：文件里有 B，场景只装了 A —— 移动到 B 必须成立。

    这是 ``_canvas_destination`` 的判定：``_canvas_rows``（``open`` 读进来
    的**全部**画布行）里有 B，B 就是工程里的画布。页面集完整不完整
    （``pages_complete``）说的是"这一批页面是不是全部页面"，跟"工程里有没有
    这个画布"是两件事，不能拿它把一个真实存在的画布判成「不存在」。
    """
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'), ('B', 'canvas', '{}')),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)
    panel = project.view.category_panel
    item = select(project.items[0])

    assert panel.move_selected_to_canvas('B') is True

    assert item.canvas_id == 'B'
    assert project.scene.undo_stack.count() == 1
    assert session.invalid is None


def test_the_same_destination_is_admitted_by_the_session_itself(project):
    """会话层也放行：不抛 ``PartialSaveConflict``。"""
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'), ('B', 'canvas', '{}')),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)

    session.authorize_canvas_move([project.items[0]], 'B', project.scene)

    assert session.invalid is None


# ── 目标判定：非法目标一个都不能放行 ────────────────────────

def test_a_destination_the_file_does_not_have_is_refused(project):
    session = canvas_session(project, page_ids='all', pages_complete=True)

    with pytest.raises(PartialSaveConflict, match='does not exist'):
        session.authorize_canvas_move([project.items[0]], 'no-such-canvas',
                                      project.scene)


def test_a_document_page_is_refused(project):
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'), ('doc-1', 'document', '{}')),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)

    with pytest.raises(PartialSaveConflict):
        session.authorize_canvas_move([project.items[0]], 'doc-1',
                                      project.scene)


def test_a_document_page_in_the_scene_is_refused_by_name(project):
    """场景里装着的文档页：明确说「不是画布」。"""
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'), ('doc-1', 'document', '{}')),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)
    project.scene.workspace_pages.append(
        {'id': 'doc-1', 'kind': 'document', 'title': 'doc-1', 'tags': []})

    with pytest.raises(PartialSaveConflict, match='not an active canvas'):
        session.authorize_canvas_move([project.items[0]], 'doc-1',
                                      project.scene)


def test_a_mindmap_page_is_refused(project):
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'), ('map-1', 'mindmap', '{}')),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)

    with pytest.raises(PartialSaveConflict):
        session.authorize_canvas_move([project.items[0]], 'map-1',
                                      project.scene)


def test_a_canvas_in_the_recycle_bin_is_refused(project):
    session = canvas_session(
        project,
        db_pages=(('A', 'canvas', '{}'),
                  ('B', 'canvas', json.dumps({'deletedAt': '2026-01-01T00:00:00'}))),
        scene_pages=('A',),
        page_ids='all', pages_complete=True)

    with pytest.raises(PartialSaveConflict, match='not an active canvas'):
        session.authorize_canvas_move([project.items[0]], 'B',
                                      project.scene)


# ── 另存为之后：目录 scope 必须重建 ────────────────────────

def test_a_session_without_a_catalogue_never_asks_for_a_rebuild(project):
    """全量打开：没有目录 scope 的场景不能报错，也不需要重建。"""
    session = canvas_session(project)

    assert session.requires_catalog_rebuild() is False
    notice = session.catalog_rebuild_notice()
    assert notice['status'] == 'current'
    assert notice['counts'] == 'current'
    assert notice['search'] == 'catalogue'
    assert notice['message'] == ''


def test_saving_to_another_file_asks_for_a_catalogue_rebuild(project, qtbot):
    """另存为之后：会话的 source 换了，目录 scope 必须重建。"""
    session = canvas_session(project)
    calls = []
    session.install_source_rebind_hook(lambda old, new: calls.append((old, new)))
    target = project.source.parent / 'saved-as.prism'

    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    assert session.invalid is None
    assert Path(session.source).resolve() == target.resolve()
    assert session.requires_catalog_rebuild() is True
    assert len(calls) == 1
    old, new = calls[0]
    assert Path(old).resolve() == Path(project.source).resolve()
    assert Path(new).resolve() == target.resolve()

    # 重建完成前，计数与搜索该怎么办，答案是写死的，不能各说各话。
    notice = session.catalog_rebuild_notice()
    assert notice['status'] == 'stale'
    assert notice['counts'] == 'stale', '旧计数属于旧文件，必须标记过期'
    assert notice['search'] == 'fallback', '重建完成前搜索走内存回退'
    assert notice['message'], '要有一句给用户看的话'

    session.mark_catalog_rebuilt()
    assert session.requires_catalog_rebuild() is False
    assert session.catalog_rebuild_notice()['status'] == 'current'


def test_saving_to_the_same_file_does_not_ask_for_a_catalogue_rebuild(project, qtbot):
    """保存到同一个文件：source 没变，不该喊重建。"""
    session = canvas_session(project)
    calls = []
    session.install_source_rebind_hook(lambda old, new: calls.append((old, new)))

    project.view.do_save(str(project.source), create_new=False)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    assert session.invalid is None
    assert calls == [], '保存到同一个文件不构成 source 变化'
    assert session.requires_catalog_rebuild() is False


def test_a_failing_rebind_hook_does_not_invalidate_the_save(project, qtbot):
    """钩子失败不能让已经落盘的保存变成"会话已失效"。"""
    session = canvas_session(project)

    def boom(old, new):
        raise RuntimeError('the catalogue owner is on fire')

    session.install_source_rebind_hook(boom)
    target = project.source.parent / 'saved-as-boom.prism'

    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)

    assert session.invalid is None
    assert session.requires_catalog_rebuild() is True
