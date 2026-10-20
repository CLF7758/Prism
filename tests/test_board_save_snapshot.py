"""Background saves must not mix metadata/attachment revisions from later edits."""
import json
import sqlite3
import threading
from unittest.mock import Mock

from prism import fileio
from prism.fileio.attachments_store import StoredAttachments
from prism.fileio.save_snapshot import BoardSaveSnapshot
from prism.fileio.sql import SQLiteIO


def test_editing_board_while_worker_waits_keeps_one_save_revision(main_window, qtbot, tmp_path, monkeypatch):
    view = main_window.view
    scene = view.scene
    scene.note_html = '<p>before save</p>'
    scene.workspace_pages = [{'id': 'doc-snapshot', 'kind': 'document', 'title': 'Before',
                              'content': '<p>before page</p>', 'tags': ['before']}]
    material = view.category_panel.material_service.create(name='Before material')
    scene.attachments = StoredAttachments(entries={
        1: ('one.png', 'image/png', b'original one'),
        2: ('two.png', 'image/png', b'original two')})
    entered, release = threading.Event(), threading.Event()
    real_save = fileio.save_prism

    def delayed_save(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'save test was not released'
        return real_save(*args, **kwargs)

    monkeypatch.setattr(fileio, 'save_prism', delayed_save)
    target = tmp_path/'revision.prism'
    view.mark_content_dirty()
    view.do_save(str(target), create_new=True, silent=True)
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        scene.note_html = '<p>after save started</p>'
        scene.workspace_pages[0]['content'] = '<p>after page</p>'
        scene.workspace_pages[0]['tags'].append('after')
        material['name'] = 'After material'
        scene.attachments[1] = ('one.png', 'image/png', b'new one')
        del scene.attachments[2]
        scene.attachments[3] = ('three.png', 'image/png', b'new three')
        view.mark_content_dirty()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not view.worker.isRunning(), timeout=10000)
    assert view.worker.wait(5000)
    qtbot.waitUntil(lambda: view.filename == str(target), timeout=5000)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT html FROM board_notes').fetchone()[0] == '<p>before save</p>'
        payload = json.loads(db.execute('SELECT data FROM workspace_pages').fetchone()[0])
        assert payload['content'] == '<p>before page</p>'
        assert payload['tags'] == ['before']
        assert json.loads(db.execute('SELECT payload FROM material_records').fetchone()[0])['name'] == 'Before material'
        assert dict(db.execute('SELECT id,blob FROM attachments')) == {1: b'original one', 2: b'original two'}
    assert scene.attachments[1][2] == b'new one'
    assert scene.attachments[3][2] == b'new three'
    assert 2 not in scene.attachments
    assert {1, 3} <= scene.attachments._dirty
    assert view.is_dirty()


def test_lazy_save_as_snapshot_rebinds_only_after_gui_acknowledgement(view, tmp_path):
    source, target = tmp_path/'source.prism', tmp_path/'copy.prism'
    view.scene.attachments = {1: ('texture.exr', 'image/x-exr', b'exact original texture')}
    io = SQLiteIO(str(source), view.scene, create_new=True)
    io.write()
    io._close_connection()
    from prism.fileio.attachments_store import sqlite_attachment_loader
    store = StoredAttachments(loader=sqlite_attachment_loader(str(source)))
    store._meta[1] = ('texture.exr', 'image/x-exr')
    view.scene.attachments = store
    snapshot = BoardSaveSnapshot.capture(view.scene)
    assert store.loaded_count() == snapshot.state.attachments.loaded_count() == 0
    fileio.save_prism(str(target), view.scene, create_new=True, board_snapshot=snapshot.state)
    assert store._loader.source_path == str(source.resolve())
    assert store.loaded_count() == 0
    snapshot.acknowledge(view.scene, str(target))
    source.unlink()
    assert store[1][2] == b'exact original texture'


def test_failed_save_cannot_acknowledge_dirty_attachment_snapshot(view, tmp_path):
    store = StoredAttachments(entries={1: ('source.png', 'image/png', b'original')})
    view.scene.attachments = store
    view._save_board_snapshot = BoardSaveSnapshot.capture(view.scene)
    view.on_saving_finished(str(tmp_path/'failed.prism'), ['disk failure'], silent=True)
    assert store._dirty == {1}
    assert store._loader is None
    assert view._save_board_snapshot is None


def test_late_save_result_cannot_rename_or_clean_another_project(view, tmp_path):
    view._save_board_snapshot = BoardSaveSnapshot.capture(view.scene)
    view.scene.project_id = 'other-project'
    current = str(tmp_path/'currently-open.prism')
    view.filename = current
    view.mark_content_dirty()
    view.on_saving_finished(str(tmp_path/'old-project.prism'), [], silent=True)
    assert view.filename == current
    assert view.is_dirty()


def test_completed_worker_with_pending_gui_ack_cannot_start_an_overlapping_save(view, tmp_path, monkeypatch):
    snapshot = BoardSaveSnapshot.capture(view.scene)
    view._save_board_snapshot = snapshot
    view.worker = Mock()
    view.worker.isRunning.return_value = False
    factory = Mock(side_effect=AssertionError('previous save result has not been processed'))
    monkeypatch.setattr(fileio, 'ThreadedIO', factory)
    view.do_save(str(tmp_path/'second.prism'), create_new=False, silent=True)
    factory.assert_not_called()
    assert view._save_board_snapshot is snapshot
    view._save_board_snapshot = None


def test_project_replacement_waits_for_active_save(main_window, qtbot, tmp_path, monkeypatch):
    view = main_window.view
    view.scene.note_html = '<p>keep this project</p>'
    project_id = view.scene.project_id
    entered, release = threading.Event(), threading.Event()
    real_save = fileio.save_prism

    def delayed_save(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return real_save(*args, **kwargs)

    monkeypatch.setattr(fileio, 'save_prism', delayed_save)
    target = tmp_path/'active-save.prism'
    view.do_save(str(target), create_new=True, silent=True)
    worker = view.worker
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        # Opening via startup/drop can bypass the action's confirmation path.
        assert view.open_from_file(str(tmp_path/'other.prism')) is False
        assert view.get_confirmation_unsaved_changes('discard?') is False
        view.on_action_new_scene()
        assert view.scene.project_id == project_id
        assert view.scene.note_html == '<p>keep this project</p>'
        assert view.worker is worker
    finally:
        release.set()
        qtbot.waitUntil(lambda: not worker.isRunning(), timeout=10000)
        assert worker.wait(5000)
        qtbot.waitUntil(lambda: view._save_board_snapshot is None, timeout=5000)
    assert view.filename == str(target)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT html FROM board_notes').fetchone()[0] == '<p>keep this project</p>'


def test_reentrant_save_during_editor_readback_starts_only_one_writer(main_window, tmp_path, qtbot, monkeypatch):
    view = main_window.view
    panel = main_window.tabs.currentWidget()
    target, nested = tmp_path/'first.prism', tmp_path/'nested.prism'
    factory = Mock(wraps=fileio.ThreadedIO)
    monkeypatch.setattr(fileio, 'ThreadedIO', factory)
    def flush():
        view.do_save(str(nested), create_new=True, silent=True)
        return True
    monkeypatch.setattr(panel, 'store_to_scene', flush, raising=False)
    view.do_save(str(target), create_new=True, silent=True)
    qtbot.waitUntil(lambda: not view.worker.isRunning(), timeout=10000)
    qtbot.waitUntil(lambda: view.filename == str(target), timeout=5000)
    assert factory.call_count == 1
    assert not nested.exists()
    assert view._save_preparing is False


def test_attachment_deleted_mid_transaction_cannot_orphan_saved_document(view, qtbot, tmp_path, monkeypatch):
    scene = view.scene
    scene.note_html = '<p>saved image</p><img src="prism://attach/2">'
    scene.attachments = StoredAttachments(entries={2: ('two.png', 'image/png', b'exact source two')})
    entered, release = threading.Event(), threading.Event()
    original_ex = SQLiteIO.ex
    def pause_after_note(io, statement, *args):
        result = original_ex(io, statement, *args)
        if statement.startswith('INSERT OR REPLACE INTO board_notes '):
            entered.set()
            assert release.wait(5), 'save test was not released'
        return result
    monkeypatch.setattr(SQLiteIO, 'ex', pause_after_note)
    target = tmp_path/'consistent.prism'
    view.mark_content_dirty()
    view.do_save(str(target), create_new=True, silent=True)
    try:
        qtbot.waitUntil(entered.is_set, timeout=5000)
        scene.note_html = '<p>new edit</p><img src="prism://attach/3">'
        del scene.attachments[2]
        scene.attachments[3] = ('three.png', 'image/png', b'new source three')
        view.mark_content_dirty()
    finally:
        release.set()
    qtbot.waitUntil(lambda: not view.worker.isRunning(), timeout=10000)
    qtbot.waitUntil(lambda: view.filename == str(target), timeout=5000)
    with sqlite3.connect(target) as db:
        assert 'prism://attach/2' in db.execute('SELECT html FROM board_notes').fetchone()[0]
        assert dict(db.execute('SELECT id,blob FROM attachments')) == {2: b'exact source two'}
    assert scene.attachments[3][2] == b'new source three'
    assert 3 in scene.attachments._dirty
    assert view.is_dirty()
