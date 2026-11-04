import hashlib
import json
import sqlite3
from pathlib import Path
from contextlib import closing

from tests.test_partial_project import project, png, FakeScene, loaded_items, pages_of
from tests.test_partial_project_gui import project as gui_project
from prism.storage.migration import convert_legacy, read_manifest
from prism.storage.managed import database_path, revision
from prism.partial_project import PartialProjectSession
from prism.fileio.partial_save import write_partial_snapshot
from prism.source_store import read_archive


def test_directory_normal_save_changes_metadata_not_old_originals(project,tmp_path,png):
    folder=tmp_path/'working.prismproj'
    convert_legacy(project,folder,experimental=True,managed=True)
    metadata=database_path(folder)
    assert read_manifest(folder)['version']==2
    assert read_archive((metadata,1))==png
    original_files={str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in (folder/'objects/loose').rglob('*') if p.is_file()}
    session=PartialProjectSession.open(metadata,item_ids=(1,2,3,4),page_ids=(),include_boards=True)
    scene=FakeScene(loaded_items(project),[])
    scene._items[0].data['notes']='directory edit'
    snapshot=session.capture(scene)
    receipt=write_partial_snapshot(snapshot)
    session.acknowledge(snapshot,receipt,scene=scene)
    assert session.capture(scene).changes==()
    assert revision(metadata)[1]==1
    assert {str(p.relative_to(folder)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (folder/'objects/loose').rglob('*') if p.is_file()}==original_files
    with closing(sqlite3.connect(metadata)) as conn:
        assert conn.execute('SELECT COUNT(*) FROM sqlar WHERE data IS NOT NULL').fetchone()[0]==0
        assert json.loads(conn.execute('SELECT data FROM items WHERE id=1').fetchone()[0])['notes']=='directory edit'
    from prism.fileio.sql import read_attachment
    assert read_attachment(metadata,1).blob==png
    from prism.paged_canvas_loader import _Archive
    with _Archive(metadata) as archive:
        assert archive.embedded(1)
        assert archive.embedded_ids((1,2,999))=={1,2}
        assert archive.blob(1)==png


def test_directory_save_as_legacy_keeps_all_originals(project,tmp_path,png):
    folder=tmp_path/'source.prismproj';convert_legacy(project,folder,experimental=True,managed=True)
    metadata=database_path(folder)
    session=PartialProjectSession.open(metadata,item_ids=(1,2,3,4),page_ids=())
    scene=FakeScene(loaded_items(project),[]);scene._items[0].data['notes']='compat edit'
    target=tmp_path/'compatible.prism'
    receipt=write_partial_snapshot(session.capture(scene),target)
    assert receipt.filename==str(target)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute('SELECT data FROM sqlar WHERE item_id=12').fetchone()[0]==png
        assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]==12
        assert json.loads(conn.execute('SELECT data FROM items WHERE id=1').fetchone()[0])['notes']=='compat edit'


def test_first_gui_directory_save_then_incremental_save(main_window,qtbot,tmp_path,monkeypatch):
    from prism.items import PrismTextItem
    item=PrismTextItem('first save');main_window.view.scene.addItem(item)
    view=main_window.view
    monkeypatch.setattr(main_window,'_store_current_page',lambda *_:True)
    target=tmp_path/'new.prismproj'
    view.do_save(str(target),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert target.is_dir(), (view.filename,str(getattr(view,"last_save_outcome",None)),list(tmp_path.iterdir()))
    assert view.filename==str(target/'metadata.sqlite')
    assert view.scene._partial_project_session is not None
    item.notes='second save';view.mark_content_dirty()
    view.do_save(view.filename,create_new=False,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed
    assert revision(view.filename)[1]==1
    qtbot.waitUntil(lambda:not view.worker.isRunning(),timeout=20000)
    qtbot.waitUntil(lambda:not view._file_operation_pending(),timeout=20000)
    from PyQt6 import QtWidgets
    messages=[]
    monkeypatch.setattr(QtWidgets.QMessageBox,"warning",lambda *args:messages.append(args[2]))
    view.open_from_file(str(target))
    assert view._opening_operation is not None
    qtbot.waitUntil(lambda:not view._project_opening and not getattr(view,'_paged_preparing',False),timeout=20000)
    assert view.filename==str(target/'metadata.sqlite')
    assert view.scene._partial_project_session is not None


def test_existing_gui_project_save_as_directory_preserves_unloaded_rows(gui_project,qtbot,monkeypatch):
    p=gui_project;view=p.view
    view.filename=str(p.source)
    monkeypatch.setattr(p.window,'_store_current_page',lambda *_:True)
    p.items[0].notes='changed while converting'
    target=p.source.with_name('converted.prismproj')
    view.do_save(str(target),create_new=True,silent=True)
    operation=view._save_operation
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    import traceback
    assert view.last_save_outcome.confirmed, (view.last_save_outcome,traceback.format_exception(operation.error))
    assert view.filename==str(target/'metadata.sqlite')
    with closing(sqlite3.connect(view.filename)) as connection:
        assert connection.execute('SELECT COUNT(*) FROM items').fetchone()[0]==len(p.items)
        assert connection.execute('SELECT original FROM future_extension').fetchone()[0]==b'not-a-known-table\x00\xff'


def test_first_image_save_failure_retains_source_then_retry(main_window,qtbot,tmp_path,monkeypatch):
    from PyQt6 import QtGui
    from prism.items import PrismPixmapItem
    from prism.storage import managed_io
    image_path=Path(__file__).parent/'assets/test3x3.png'
    item=PrismPixmapItem(QtGui.QImage(str(image_path)),str(image_path))
    item._source_path=str(image_path)  # Same original-source contract as the importer.
    view=main_window.view;view.scene.addItem(item)
    monkeypatch.setattr(main_window,'_store_current_page',lambda *_:True)
    original=managed_io.convert_legacy
    def fail(*args,**kwargs):raise OSError('injected conversion failure')
    target=tmp_path/'image.prismproj'
    with monkeypatch.context() as patch:
        patch.setattr(managed_io,'convert_legacy',fail)
        view.do_save(str(target),create_new=True,silent=True)
        qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert not view.last_save_outcome.confirmed
    assert not target.exists()
    assert item.save_id is None
    assert item._prism_source is None
    assert item.filename==str(image_path)
    view.do_save(str(target),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert item.save_id is not None
    assert read_archive((view.filename,item.save_id))==image_path.read_bytes()
    second=PrismPixmapItem(QtGui.QImage(str(image_path)),str(image_path));second._source_path=str(image_path)
    view.scene.addItem(second)
    view.mark_content_dirty()
    view.do_save(view.filename,create_new=False,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert second.save_id is not None
    assert read_archive((view.filename,second.save_id))==image_path.read_bytes()


def test_gui_directory_save_as_rebinds_to_independent_copy(main_window,qtbot,tmp_path,monkeypatch):
    from prism.items import PrismTextItem
    view=main_window.view;item=PrismTextItem('independent');view.scene.addItem(item)
    monkeypatch.setattr(main_window,'_store_current_page',lambda *_:True)
    original=tmp_path/'original.prismproj';copy=tmp_path/'copy.prismproj'
    view.do_save(str(original),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed
    view.do_save(str(copy),create_new=True,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed,view.last_save_outcome
    assert view.scene._partial_project_session.source==str(copy/'metadata.sqlite')
    assert revision(original/'metadata.sqlite')[0]!=revision(copy/'metadata.sqlite')[0]
    item.notes='copy only';view.mark_content_dirty()
    view.do_save(view.filename,create_new=False,silent=True)
    qtbot.waitUntil(lambda:getattr(view,'_save_operation',None) is None,timeout=20000)
    assert view.last_save_outcome.confirmed
    with closing(sqlite3.connect(copy/'metadata.sqlite')) as conn:
        assert json.loads(conn.execute('SELECT data FROM items').fetchone()[0])['notes']=='copy only'
    with closing(sqlite3.connect(original/'metadata.sqlite')) as conn:
        assert json.loads(conn.execute('SELECT data FROM items').fetchone()[0]).get('notes')!='copy only'
