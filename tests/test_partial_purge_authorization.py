"""Permanent deletes in a partly loaded project: either saved, or refused first.

Three recycle-bin paths used to leave a project that could never be saved again,
with nothing said to the user: purging a resource-tree node (its row vanished
while the domain tracker had no authorized deletion), purging an asset (a partial
snapshot may never drop an ``items`` row), and clearing a saved mind map (the
board singleton needs an explicit clear command).

The rule under test: after a click, the operation either completes *and* saves,
or is refused before anything is mutated -- with the reason shown.
"""
import copy
import json
import sqlite3
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui

from prism.fileio.partial_save import PartialSaveConflict, write_partial_snapshot
from prism.partial_project import PartialProjectSession
from tests.test_partial_domains import material, node
from tests.test_partial_project import (FakeScene, loaded_items, pages_of, png,
                                       project, rows)

TRASHED = '2026-10-01T09:30:00+00:00'


def _page_node(key, page_id, title='Trashed page', deleted_at=TRASHED,
               project_id='stable-project'):
    entry = node(key)
    entry.update(projectId=project_id, section='document', nodeType='page',
                 title=title, pageId=page_id, deletedAt=deleted_at)
    return entry


@pytest.fixture
def purge_scope(project):
    """A whole-domain session whose recycle bin holds several page nodes.

    ``n1`` is a live material folder and ``n6`` a trashed one; both still hold a
    material. ``p1``..``p5`` are page nodes for a document, for the loaded canvas,
    for a page still in use, for the default canvas and for the mind map -- the
    shapes a purge is asked about.
    """
    folder, trashed_folder = node('n1'), node('n6')
    for entry in (folder, trashed_folder):
        entry['projectId'] = 'stable-project'
    trashed_folder['title'] = 'Trashed folder'
    trashed_folder['deletedAt'] = TRASHED
    kept = material('m1')
    kept.update(projectId='stable-project', folderId='n1')
    orphan = material('m2')
    orphan.update(projectId='stable-project', folderId='n6')
    pages = [_page_node('p1', 'd1', 'Trashed document'),
             _page_node('p2', 'A', 'Trashed canvas'),
             _page_node('p3', 'd2', 'Still in use', deleted_at=None),
             _page_node('p4', 'default-canvas', 'Default canvas'),
             _page_node('p5', 'default-mindmap', 'Trashed mind map')]
    with sqlite3.connect(project) as db:
        db.execute('UPDATE material_records SET payload=?', (json.dumps(kept),))
        db.execute('INSERT INTO material_records VALUES(?,?)', ('m2', json.dumps(orphan)))
        db.execute('UPDATE workspace_nodes SET payload=?', (json.dumps(folder),))
        for entry in (trashed_folder, *pages):
            db.execute('INSERT INTO workspace_nodes VALUES(?,?)',
                       (entry['id'], json.dumps(entry)))
    session = PartialProjectSession.open(
        project, item_ids=(1, 2, 3, 4),
        page_ids=[page['id'] for page in pages_of(project)],
        pages_complete=True, include_domains=True)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.material_records = [copy.deepcopy(kept), copy.deepcopy(orphan)]
    scene.workspace_nodes = [copy.deepcopy(folder), copy.deepcopy(trashed_folder)] + \
        copy.deepcopy(pages)
    scene.material_library_state = {}
    return session, scene


def _purge_node(session, scene, key):
    """What the tree does in place, plus what the window does to the page body."""
    session.authorize_resource_purge([key], [], scene)
    scene.workspace_nodes[:] = [n for n in scene.workspace_nodes if n['id'] != key]


# ── 资源树永久删除：授权之后能存下来 ────────────────────────────────

def test_authorized_tree_purge_saves_and_leaves_every_other_row(purge_scope):
    session, scene = purge_scope
    _purge_node(session, scene, 'p1')
    scene.workspace_pages[:] = [p for p in scene.workspace_pages if p['id'] != 'd1']

    snapshot = session.capture(scene)
    receipt = write_partial_snapshot(snapshot)
    session.acknowledge(snapshot, receipt, scene=scene)

    assert [row[0] for row in rows(session.source, 'workspace_nodes')] == \
        ['n1', 'n6', 'p2', 'p3', 'p4', 'p5']
    assert 'd1' not in [row[0] for row in rows(session.source, 'workspace_pages')]
    # Untouched domains and unloaded pages are exactly as they were.
    assert len(rows(session.source, 'material_records')) == 2
    assert len(rows(session.source, 'items')) == 12
    assert len(rows(session.source, 'board_mindmap')) == 1
    assert session.capture(scene).changes == (), 'a saved purge is not dirty'


def test_unauthorized_tree_purge_is_still_refused(purge_scope):
    """The guard that caught this bug must keep catching it."""
    session, scene = purge_scope
    scene.workspace_nodes[:] = [n for n in scene.workspace_nodes if n['id'] != 'p1']
    with pytest.raises(PartialSaveConflict, match='disappeared'):
        session.capture(scene)


# ── 资源树永久删除：授权之前就拒绝，一步都不改 ──────────────────────

def test_material_folder_holding_materials_is_refused_before_anything_changes(purge_scope):
    session, scene = purge_scope
    with pytest.raises(PartialSaveConflict, match='Materials still live'):
        session.authorize_resource_purge(['n6'], [], scene)
    assert [n['id'] for n in scene.workspace_nodes] == \
        ['n1', 'n6', 'p1', 'p2', 'p3', 'p4', 'p5']
    assert not session._domain_deletions, '拒绝就不能留下任何授权'
    # The refused purge leaves the project saveable.
    assert session.capture(scene).changes == ()


def test_page_holding_loaded_assets_is_refused_before_anything_changes(purge_scope):
    session, scene = purge_scope
    with pytest.raises(PartialSaveConflict, match='belong to the pages'):
        session.authorize_resource_purge(['p2'], ['A'], scene)
    assert not session._domain_deletions
    assert session.capture(scene).changes == ()


def test_parked_asset_page_is_refused_too(purge_scope):
    """A page that was loaded and then unloaded still owns its assets."""
    session, scene = purge_scope
    item = scene._items[0]
    session.attach(scene)
    token = session.prepare_unload([item], scene)
    scene._items[:] = [i for i in scene._items if i is not item]
    session.commit_unload(token, scene)
    with pytest.raises(PartialSaveConflict, match='belong to the pages'):
        session.authorize_resource_purge(['p2'], ['A'], scene)


def test_unloaded_node_live_node_and_default_canvas_are_refused(purge_scope):
    session, scene = purge_scope
    with pytest.raises(PartialSaveConflict, match='was not loaded'):
        session.authorize_resource_purge(['ghost'], [], scene)
    with pytest.raises(PartialSaveConflict, match='recycle bin'):
        session.authorize_resource_purge(['p3'], ['d2'], scene)
    with pytest.raises(PartialSaveConflict, match='default canvas'):
        session.authorize_resource_purge(['p4'], [], scene)
    assert not session._domain_deletions


def test_session_without_the_domain_scope_refuses_the_purge(project):
    """No baseline for the tree means no way to verify the delete."""
    session = PartialProjectSession.open(project, item_ids=(1, 2, 3, 4),
                                         page_ids=['d1'], pages_complete=False,
                                         include_domains=False)
    scene = FakeScene(loaded_items(project), pages_of(project))
    scene.workspace_nodes = [_page_node('p1', 'd1')]
    with pytest.raises(PartialSaveConflict, match='outside this save scope'):
        session.authorize_resource_purge(['p1'], ['d1'], scene)


# ── 脑图页永久删除：清空正文要显式授权 ────────────────────────────

def test_purged_mind_map_page_saves_once_its_clear_is_authorized(purge_scope):
    """The recycle-bin purge is the explicit clear command."""
    session, scene = purge_scope
    assert scene.mindmap_tree == {'root': {}}
    session.authorize_resource_purge(['p5'], ['default-mindmap'], scene)
    scene.workspace_nodes[:] = [n for n in scene.workspace_nodes if n['id'] != 'p5']
    # What WindowWorkspaceMixin._clear_purged_mindmap does before emptying it.
    assert session.mindmap_clear_needed() is True
    session.authorize_mindmap_clear()
    scene.mindmap_tree = None

    write_partial_snapshot(session.capture(scene))
    assert rows(session.source, 'board_mindmap')[0][1] is None
    assert [row[0] for row in rows(session.source, 'workspace_nodes')] == \
        ['n1', 'n6', 'p1', 'p2', 'p3', 'p4']


def test_mind_map_clear_without_the_page_purge_is_still_refused(purge_scope):
    session, scene = purge_scope
    session.authorize_resource_purge(['p5'], [], scene)
    scene.mindmap_tree = None
    with pytest.raises(PartialSaveConflict, match='explicit clear command'):
        session.capture(scene)


def test_mind_map_clear_needed_only_when_the_board_row_is_loaded(project):
    without = PartialProjectSession.open(project, item_ids=(1, 2), page_ids=(),
                                         include_boards=False)
    assert without.mindmap_clear_needed() is False
    with_board = PartialProjectSession.open(project, item_ids=(1, 2), page_ids=(),
                                            include_boards=True)
    assert with_board.mindmap_clear_needed() is True


# ── 素材永久删除：部分加载下按钮禁用，点了也只给原因 ────────────────

def _trashed_asset(view):
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#336699'))
    from prism.items import PrismPixmapItem
    item = PrismPixmapItem(image, filename='purge-me.png')
    item.init_metadata()
    view.scene.addItem(item)
    item.setSelected(True)
    view.on_action_delete_items()
    return item


def _asset_panel(qtbot, view):
    from prism.widgets.trash_panel import TrashPanel
    panel = TrashPanel(view)
    qtbot.addWidget(panel)
    return panel


def test_asset_purge_button_is_off_for_a_partial_project(view, qtbot):
    item = _trashed_asset(view)
    view.scene._partial_project_source = str(view.scene.project_id or 'partial')
    panel = _asset_panel(qtbot, view)
    panel.asset_list.selectionModel().select(
        panel.asset_model.index(0, 0),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)

    assert panel.asset_purge_button.isEnabled() is False
    assert panel.asset_purge_button.toolTip(), '禁用要说清楚为什么'

    emitted = []
    panel.assets_purged.connect(emitted.extend)
    with patch('PyQt6.QtWidgets.QMessageBox.information') as told:
        assert panel.purge_selected_assets() == []
    assert emitted == [], '被拒绝时不能发出永久删除信号'
    assert told.called, '必须告诉用户原因，不能静默失败'
    assert item in view.scene.items(), '被拒绝就不能动图元'


def test_asset_purge_button_stays_on_for_a_whole_project(view, qtbot):
    _trashed_asset(view)
    panel = _asset_panel(qtbot, view)
    panel.asset_list.selectionModel().select(
        panel.asset_model.index(0, 0),
        QtCore.QItemSelectionModel.SelectionFlag.ClearAndSelect)
    assert panel.asset_purge_button.isEnabled() is True


def test_window_refuses_to_purge_assets_for_a_partial_project(view):
    item = _trashed_asset(view)
    view.scene._partial_project_source = str(view.scene.project_id or 'partial')
    with patch('PyQt6.QtWidgets.QMessageBox.warning') as told:
        view.window()._on_assets_purged([item])
    assert told.called
    assert item in view.scene.items()


# ── 面板接线：先授权，再动手；拿不到授权就一步都不改 ────────────────

class _Session:
    def __init__(self, reason=None):
        self.reason = reason
        self.calls = []

    def authorize_resource_purge(self, node_ids, page_ids, scene):
        self.calls.append((tuple(node_ids), tuple(page_ids)))
        if self.reason:
            raise PartialSaveConflict(self.reason)


def _tree_panel(qtbot, window):
    from prism.widgets.trash_panel import TrashPanel
    service = window.view.category_panel.resource_service
    folder = service.createFolder('document', '夹里的页')
    page = service.createPage('document', '正文', 'purge-page', folder['id'])
    service.trash([folder['id']], TRASHED)
    panel = TrashPanel(window.view, window)
    qtbot.addWidget(panel)
    for row in range(panel.list_widget.count()):
        panel.list_widget.item(row).setSelected(True)
    return panel, service, folder, page


def test_panel_purge_asks_the_session_before_touching_the_tree(main_window, qtbot):
    panel, service, folder, page = _tree_panel(qtbot, main_window)
    session = _Session(reason='a partial save cannot write this delete')
    main_window.view.scene._partial_project_source = str(main_window.view.scene.project_id or 'partial')
    main_window.view.scene._partial_project_session = session
    purged = []
    panel.purged.connect(purged.extend)

    with patch('PyQt6.QtWidgets.QMessageBox.information') as told:
        assert panel.purge_selected() == []
    assert told.called
    assert session.calls == [((folder['id'], page['id']), ('purge-page',))]
    assert service._by_id(folder['id']) is not None, '拒绝就不能删节点'
    assert purged == [], '拒绝就不能清页面正文'


def test_panel_purge_purges_once_the_session_admits_it(main_window, qtbot):
    panel, service, folder, page = _tree_panel(qtbot, main_window)
    session = _Session()
    main_window.view.scene._partial_project_source = str(main_window.view.scene.project_id or 'partial')
    main_window.view.scene._partial_project_session = session
    purged = []
    panel.purged.connect(purged.extend)

    assert set(panel.purge_selected()) == {folder['id'], page['id']}
    assert service._by_id(folder['id']) is None
    assert purged == ['purge-page']


def test_panel_purge_waits_for_a_ready_partial_session(main_window, qtbot):
    panel, service, folder, _page = _tree_panel(qtbot, main_window)
    main_window.view.scene._partial_project_source = 'preparing.prism'
    main_window.view.scene._partial_project_session = None
    with patch('PyQt6.QtWidgets.QMessageBox.information') as told:
        assert panel.purge_selected() == []
    assert told.called
    assert service._by_id(folder['id']) is not None


class _BoardSession:
    """Records what the window asks the session before clearing the board."""

    def __init__(self, loaded=True, reason=None):
        self.loaded = loaded
        self.reason = reason
        self.authorized = 0

    def mindmap_clear_needed(self):
        return self.loaded

    def authorize_mindmap_clear(self):
        self.authorized += 1
        if self.reason:
            raise PartialSaveConflict(self.reason)


def test_window_authorizes_the_clear_before_emptying_a_purged_mind_map(main_window):
    """Purging the mind-map page is the explicit clear command it was waiting for."""
    scene = main_window.view.scene
    session = _BoardSession()
    scene._partial_project_session = session
    scene.mindmap_tree = {'root': {'text': 'gone with the page'}}

    main_window._on_trash_purged(['default-mindmap'])

    assert session.authorized == 1, '清空之前必须先取得授权，否则这次保存会被拒绝'
    assert scene.mindmap_tree is None


def test_window_needs_no_authorization_for_an_unloaded_board(main_window):
    scene = main_window.view.scene
    session = _BoardSession(loaded=False)
    scene._partial_project_session = session
    scene.mindmap_tree = {'root': {'text': 'never loaded, so nothing to write'}}

    main_window._on_trash_purged(['default-mindmap'])

    assert session.authorized == 0
    assert scene.mindmap_tree is None


def test_window_keeps_the_board_when_the_clear_is_refused(main_window):
    """宁可正文留着，也不能把工程变成再也存不了的样子。"""
    scene = main_window.view.scene
    session = _BoardSession(reason='this project must be reopened first')
    scene._partial_project_session = session
    scene.mindmap_tree = {'root': {'text': 'stays put'}}

    with patch('PyQt6.QtWidgets.QMessageBox.warning') as told:
        main_window._on_trash_purged(['default-mindmap'])

    assert told.called, '拒绝要照实说，不能静默'
    assert scene.mindmap_tree == {'root': {'text': 'stays put'}}


def test_whole_project_purge_behaviour_is_unchanged(view, qtbot):
    """Without a partial session the panel must not gain a new refusal."""
    panel, service, folder, page = _tree_panel(qtbot, view.window())
    purged = []
    panel.purged.connect(purged.extend)
    assert set(panel.purge_selected()) == {folder['id'], page['id']}
    assert purged == ['purge-page']


def test_partial_save_never_falls_back_to_a_full_rewrite(purge_scope):
    """The purge writes diffs; unloaded rows keep their bytes and rows."""
    session, scene = purge_scope
    before = {table: rows(session.source, table) for table in (
        'sqlar', 'attachments', 'items', 'future_extension', 'material_records')}
    _purge_node(session, scene, 'p1')
    snapshot = session.capture(scene)
    assert all(change.table in ('workspace_nodes',) for change in snapshot.changes)
    write_partial_snapshot(snapshot)
    for table, expected in before.items():
        assert rows(session.source, table) == expected, table