"""Save As owns its sources; edits during autosave remain visibly unsaved."""
from unittest.mock import Mock
import sqlite3
import pytest

from PyQt6 import QtGui

from prism.fileio.sql import SQLiteIO
from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
from prism.fileio.errors import PrismFileIOError


def test_save_as_rebinds_clean_lazy_sources_after_old_file_is_removed(view, tmp_path):
    original, destination = tmp_path/'original.prism', tmp_path/'copy.prism'
    view.scene.attachments = {1:('source.png','image/png',b'exact original bytes')}
    io = SQLiteIO(str(original),view.scene,create_new=True)
    io.write()
    io._close_connection()
    store = StoredAttachments(loader=sqlite_attachment_loader(str(original)))
    store._meta[1] = ('source.png','image/png')
    view.scene.attachments = store
    io = SQLiteIO(str(destination),view.scene,create_new=True)
    io.write()
    io._close_connection()
    assert store.loaded_count() == 0
    original.unlink()
    assert store[1][2] == b'exact original bytes'


def test_save_acknowledgement_keeps_newer_attachment_revision(tmp_path):
    store = StoredAttachments()
    store[1] = ('source.png','image/png',b'first')
    saved = store[1]
    store[1] = ('source.png','image/png',b'new edit during save')
    store.saved_to(tmp_path/'project.prism',[(1,saved)])
    assert store.read_uncached(1)[2] == b'new edit during save'
    assert 1 in store._dirty


def test_content_edited_during_save_is_not_marked_clean(view, tmp_path):
    view.mark_content_dirty()
    view._save_revision = view._content_revision
    view._save_undo_index = view.undo_stack.index()
    view.mark_content_dirty()
    view.on_saving_finished(str(tmp_path/'project.prism'),[],silent=True)
    assert view.is_dirty()


def test_canvas_edited_during_save_is_not_marked_clean(view, tmp_path):
    view._save_revision = getattr(view,'_content_revision',0)
    view._save_undo_index = view.undo_stack.index()
    view.undo_stack.push(QtGui.QUndoCommand('new edit'))
    view.on_saving_finished(str(tmp_path/'project.prism'),[],silent=True)
    assert not view.undo_stack.isClean()


def test_undo_then_replace_at_same_index_during_save_stays_dirty(view, tmp_path):
    view.undo_stack.push(QtGui.QUndoCommand('first edit'))
    view._save_undo_index = view.undo_stack.index()
    view._save_undo_revision = view._undo_revision
    view.undo_stack.undo()
    view.undo_stack.push(QtGui.QUndoCommand('different edit at same index'))
    assert view.undo_stack.index() == view._save_undo_index
    view.on_saving_finished(str(tmp_path/'project.prism'), [], silent=True)
    assert not view.undo_stack.isClean()


def test_a_second_save_cannot_replace_the_active_worker(view, tmp_path, monkeypatch):
    worker = Mock()
    worker.isRunning.return_value = True
    view.worker = worker
    factory = Mock(side_effect=AssertionError('overlapping save'))
    monkeypatch.setattr('prism.view.fileio.ThreadedIO', factory)
    view.do_save(str(tmp_path/'project.prism'),create_new=False)
    assert view.worker is worker
    factory.assert_not_called()


def test_unchanged_successful_save_clears_dirty_state(view, tmp_path):
    view.mark_content_dirty()
    view._save_revision = view._content_revision
    view._save_undo_index = view.undo_stack.index()
    view.on_saving_finished(str(tmp_path/'project.prism'),[])
    assert not view.is_dirty()


def test_locked_project_is_not_rebuilt_or_replaced(view, tmp_path, monkeypatch):
    path = tmp_path/'project.prism'
    io = SQLiteIO(str(path),view.scene,create_new=True)
    io.write()
    io._close_connection()
    original = path.read_bytes()
    writer = SQLiteIO(str(path),view.scene)
    monkeypatch.setattr(writer,'_migrate',Mock(side_effect=sqlite3.OperationalError('database is locked')))
    rebuild = Mock(side_effect=AssertionError('healthy locked file must not be replaced'))
    monkeypatch.setattr(writer,'_backup_before_overwrite',rebuild)
    with pytest.raises(PrismFileIOError,match='database is locked'):
        writer.write()
    rebuild.assert_not_called()
    assert path.read_bytes() == original
