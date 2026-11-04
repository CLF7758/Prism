from contextlib import closing
from pathlib import Path
import hashlib,sqlite3
import pytest
from tests.test_partial_project import project,png
from tests.test_partial_project_gui import project as gui_project
from prism.storage.entrypoint import convert,is_entry,resolve
from prism.storage.managed import database_path
from prism.source_store import read_archive


def test_entry_publication_never_renames_data_directory(project,png,tmp_path,monkeypatch):
    entry=tmp_path/'new.prism';folder=tmp_path/'new.prism.data'
    before=hashlib.sha256(project.read_bytes()).hexdigest()
    rename=Path.rename
    def guard(self,*args,**kwargs):
        assert self!=folder, 'directory publication is forbidden'
        return rename(self,*args,**kwargs)
    monkeypatch.setattr(Path,'rename',guard)
    assert convert(project,entry)==entry
    assert is_entry(entry) and resolve(entry)==folder
    assert read_archive((database_path(entry),12))==png
    assert hashlib.sha256(project.read_bytes()).hexdigest()==before
    assert entry.stat().st_size<65536
    with closing(sqlite3.connect(entry)) as conn:
        assert conn.execute('PRAGMA user_version').fetchone()[0]>4


def test_entry_and_wrong_directory_identity_are_rejected(project,tmp_path):
    entry=tmp_path/'new.prism';convert(project,entry)
    with closing(sqlite3.connect(tmp_path/'new.prism.data/metadata.sqlite')) as conn:
        conn.execute("UPDATE _prism_storage_state SET instance='wrong'");conn.commit()
    from prism.storage.contracts import StorageError
    with pytest.raises(StorageError):database_path(entry)


def test_new_gui_save_creates_entry_then_reopens_it(main_window,qtbot,monkeypatch,tmp_path):
    from prism.items import PrismTextItem
    view=main_window.view;view.scene.addItem(PrismTextItem('entry first save'))
    monkeypatch.setattr(main_window,'_store_current_page',lambda *_:True)
    entry=tmp_path/'new.prism';folder=tmp_path/'new.prism.data'
    view.do_save(str(folder),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert is_entry(entry)
    qtbot.waitUntil(lambda:not view._file_operation_pending(),timeout=20000)
    view.require_directory_upgrade=True
    # Entry and metadata.sqlite identify the same open project; re-opening it
    # is intentionally a no-op. Close this test's scene before a real read-back.
    view.clear_scene()
    with qtbot.waitSignal(view.project_open_finished, timeout=20000):
        view.open_from_file(str(entry))
    assert view.filename==str(folder/'metadata.sqlite')


def test_missing_entry_can_be_recovered_without_renaming_data(project,png,tmp_path):
    from prism.storage.checkpoint import recover
    entry=tmp_path/'recovered.prism';folder=tmp_path/'recovered.prism.data'
    convert(project,entry);entry.unlink()
    assert recover(folder)==entry
    assert is_entry(entry)
    assert read_archive((database_path(entry),12))==png


def test_save_as_entry_keeps_current_edits_and_unknown_unloaded_data(gui_project,qtbot,monkeypatch,tmp_path):
    import json
    p=gui_project;view=p.view;view.filename=str(p.source)
    before=hashlib.sha256(p.source.read_bytes()).hexdigest()
    p.items[0].notes='captured current edit'
    monkeypatch.setattr(p.window,'_store_current_page',lambda *_:True)
    entry=tmp_path/'converted.prism';folder=tmp_path/'converted.prism.data'
    view.do_save(str(folder),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert is_entry(entry)
    assert hashlib.sha256(p.source.read_bytes()).hexdigest()==before
    with closing(sqlite3.connect(database_path(entry))) as conn:
        assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]==len(p.items)
        assert conn.execute('SELECT original FROM future_extension').fetchone()[0]==b'not-a-known-table\x00\xff'
        assert json.loads(conn.execute('SELECT data FROM items WHERE id=?',(p.items[0].save_id,)).fetchone()[0])['notes']=='captured current edit'


def test_save_as_dialog_publishes_modern_entry(main_window,qtbot,monkeypatch,tmp_path):
    from PyQt6 import QtWidgets
    from prism.items import PrismTextItem
    view=main_window.view;view.scene.addItem(PrismTextItem('save through menu'))
    monkeypatch.setattr(main_window,'_store_current_page',lambda *_:True)
    entry=tmp_path/'menu.prism'
    monkeypatch.setattr(QtWidgets.QFileDialog,'getExistingDirectory',lambda *a,**k:str(tmp_path))
    monkeypatch.setattr(QtWidgets.QInputDialog,'getText',lambda *a,**k:('menu',True))
    view.on_action_save_as()
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert is_entry(entry)
    assert view.filename==str(tmp_path/'menu.prism.data/metadata.sqlite')
    assert not (tmp_path/'menu.prism.data.prism').exists()


def test_project_location_dialog_cancels_and_rejects_existing_project(main_window,monkeypatch,tmp_path):
    from PyQt6 import QtWidgets
    view=main_window.view
    monkeypatch.setattr(QtWidgets.QFileDialog,'getExistingDirectory',lambda *a,**k:'')
    assert view._choose_project_destination('new project') is None
    monkeypatch.setattr(QtWidgets.QFileDialog,'getExistingDirectory',lambda *a,**k:str(tmp_path))
    monkeypatch.setattr(QtWidgets.QInputDialog,'getText',lambda *a,**k:('existing',True))
    existing=tmp_path/'existing.prism.data';existing.mkdir()
    warnings=[]
    monkeypatch.setattr(QtWidgets.QMessageBox,'warning',lambda *a,**k:warnings.append(a))
    assert view._choose_project_destination('save project as') is None
    assert warnings and existing.is_dir()
    monkeypatch.setattr(QtWidgets.QInputDialog,'getText',lambda *a,**k:('../escape',True))
    assert view._choose_project_destination('save project as') is None
