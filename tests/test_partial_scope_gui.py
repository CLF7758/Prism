"""User move commands and complete metadata saves through the real GUI worker."""
import json
import sqlite3
import pytest

from prism.partial_project import PartialProjectSession
from tests.test_partial_project_gui import project, CANVAS_A, rows
from tests.test_partial_domains import material, node


def canvas_session(project):
    with sqlite3.connect(project.source) as db:
        for key in ('A', 'B'):
            db.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                       (key, 'canvas', key, '{}', 3))
    project.scene.workspace_pages = [{'id': 'A', 'kind': 'canvas', 'title': 'A'}]
    session = PartialProjectSession.open(project.source, item_ids=CANVAS_A,
        page_ids=('A',), include_boards=True)
    session.attach(project.scene)
    return session


def test_sidebar_move_to_unloaded_canvas_saves_and_undo_saves(project, qtbot):
    session = canvas_session(project)
    item = project.items[0]
    assert project.view.category_panel.move_items_to_canvas([item], 'B')
    assert item.canvas_id == 'B'
    target = project.source.parent / 'moved.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)
    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'B'
    assert len(saved) == 9
    project.scene.undo_stack.undo()
    assert item.canvas_id == 'A'
    project.view.do_save(str(target), create_new=False)
    qtbot.waitUntil(lambda: session.revision == 2, timeout=15000)
    saved = dict((r[0], r) for r in rows(target, 'items'))
    assert json.loads(saved[item.save_id][-1])['canvasId'] == 'A'
    assert rows(target, 'sqlar') == rows(project.source, 'sqlar')


def test_bad_sidebar_target_is_refused_without_mutation_or_undo_entry(project):
    canvas_session(project)
    panel = project.view.category_panel
    item = project.items[0]
    index = project.scene.undo_stack.index()
    assert not panel.move_items_to_canvas([item], 'no-such-canvas')
    assert item.canvas_id == 'A'
    assert project.scene.undo_stack.index() == index
    assert 'destination' in project.window.statusBar().currentMessage().lower()


def test_raw_canvas_edit_refuses_save_before_starting_worker(project):
    session = canvas_session(project)
    project.items[0].canvas_id = 'B'
    worker = getattr(project.view, 'worker', None)
    target = project.source.parent / 'must-not-exist.prism'
    project.view.do_save(str(target), create_new=True)
    assert not target.exists()
    assert session.acknowledged is None
    assert getattr(project.view, 'worker', None) is worker
    assert 'explicit move' in project.window.statusBar().currentMessage()


def test_material_and_tree_changes_save_through_gui_without_full_scene(project, qtbot):
    obj, folder = material('m1'), node('n1')
    obj['projectId'] = folder['projectId'] = project.scene.project_id
    obj['folderId'] = 'n1'
    obj['future'] = {'keep': True}
    with sqlite3.connect(project.source) as db:
        db.execute('UPDATE material_records SET payload=?', (json.dumps(obj),))
        db.execute('UPDATE workspace_nodes SET payload=?', (json.dumps(folder),))
    scene = project.scene
    scene.material_records, scene.workspace_nodes = [obj], [folder]
    session = PartialProjectSession.open(project.source, item_ids=CANVAS_A,
        page_ids=(), include_domains=True)
    session.attach(scene)
    obj['notes'], folder['title'] = 'GUI note', 'GUI folder'
    scene.material_library_state = {'viewMode': 'list'}
    target = project.source.parent / 'domains-gui.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: session.revision == 1, timeout=15000)
    assert session.invalid is None
    assert json.loads(rows(target, 'material_records')[0][1])['notes'] == 'GUI note'
    assert json.loads(rows(target, 'material_records')[0][1])['future'] == {'keep': True}
    assert json.loads(rows(target, 'workspace_nodes')[0][1])['title'] == 'GUI folder'
    assert len(rows(target, 'items')) == 9
    assert rows(target, 'sqlar') == rows(project.source, 'sqlar')


@pytest.mark.parametrize('failure_mode', ['raise', 'invalidate'])
def test_failed_receipt_does_not_clear_dirty_or_report_save_success(
        project, qtbot, monkeypatch, failure_mode):
    session = canvas_session(project)
    project.items[0].notes = 'must stay dirty'
    project.view._content_dirty = True
    def fail_ack(*args, **kwargs):
        if failure_mode == 'invalidate':
            session.invalid = 'injected rebind failure'
            return
        raise RuntimeError('injected receipt failure')
    monkeypatch.setattr(session, 'acknowledge', fail_ack)
    target = project.source.parent / 'written-but-unconfirmed.prism'
    project.view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: bool(session.invalid), timeout=15000)
    assert target.exists()
    assert session.acknowledged is None
    assert project.view._content_dirty is True
    assert 'could not be confirmed' in project.window.statusBar().currentMessage()
