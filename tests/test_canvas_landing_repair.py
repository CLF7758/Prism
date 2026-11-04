from PyQt6 import QtCore
from tests.test_paged_canvas_loader import pages
from tests.test_partial_project_gui import project as gui_project
from prism import camera_policy


def test_click_canvas_waits_for_geometry_then_shows_all_pictures(pages,qtbot,monkeypatch):
    view,scope,session=pages;window=view.parent
    monkeypatch.setattr(window,'_navigation_is_committed',lambda:True)
    monkeypatch.setattr(window,'_store_current_page',lambda *_:True)
    view.scale(0.02,0.02);view.centerOn(-50000,-50000)
    window.open_workspace_page('canvas','B')
    qtbot.waitUntil(lambda:not view._paged_canvas_loader.active,timeout=10000)
    assert camera_policy.visible_asset_bounds(view) is not None, [(i.save_id,i.isVisible(),getattr(i,'_canvas_id',None),i.sceneBoundingRect()) for i in view.scene.iter_assets()][:3]
    bounds=camera_policy.visible_asset_bounds(view)
    assert len([i for i in view.scene.iter_assets() if i.isVisible()])==40
    visible=view.mapToScene(view.viewport().rect()).boundingRect()
    assert visible.adjusted(-2,-2,2,2).contains(bounds),(visible,bounds)
    view.centerOn(-50000,-50000)
    window.open_workspace_page('canvas','B')
    visible=view.mapToScene(view.viewport().rect()).boundingRect()
    assert visible.adjusted(-2,-2,2,2).contains(bounds)


def test_default_canvas_does_not_open_aggregate_browser(main_window,monkeypatch):
    window=main_window
    calls=[]
    monkeypatch.setattr(window,'open_workspace_page',lambda *args:calls.append(args))
    monkeypatch.setattr(window.asset_browser,'open',lambda *args:(_ for _ in ()).throw(AssertionError('wrong page')))
    window._open_resource_page('canvas','default-canvas')
    assert calls==[('canvas','default-canvas')]


def test_convert_current_project_uses_captured_save(main_window,monkeypatch,tmp_path):
    from PyQt6 import QtWidgets
    from prism.actions.storage_workflow import convert_project
    main_window.view.filename=str(tmp_path/'old.prism')
    calls=[]
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(tmp_path/'new'),'Prism'))
    monkeypatch.setattr(main_window.view,'do_save',lambda *a,**k:calls.append((a,k)))
    convert_project(main_window.view)
    assert calls==[((str(tmp_path/'new.prism.data'),),{'create_new':True})]


def test_forced_upgrade_opens_only_verified_new_copy(gui_project,qtbot,monkeypatch,tmp_path):
    from PyQt6 import QtWidgets
    import hashlib
    from prism.actions import storage_workflow
    from prism.storage.managed import database_path
    from contextlib import closing
    import sqlite3
    p=gui_project;view=p.view;view.require_directory_upgrade=True
    before=hashlib.sha256(p.source.read_bytes()).hexdigest()
    destination=tmp_path/'upgraded.prism';opened=[]
    monkeypatch.setattr(QtWidgets.QMessageBox,'question',lambda *a,**k:QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(destination),'Prism'))
    monkeypatch.setattr(storage_workflow,'_open_result',lambda v,path,**k:opened.append(path))
    assert view.open_from_file(str(p.source)) is False
    assert getattr(view,'_storage_operation',None) is not None
    qtbot.waitUntil(lambda:getattr(view,'_storage_operation',None) is None,timeout=20000)
    assert opened==[str(destination)]
    assert hashlib.sha256(p.source.read_bytes()).hexdigest()==before
    with closing(sqlite3.connect(database_path(destination))) as conn:
        assert conn.execute('SELECT COUNT(*) FROM items').fetchone()[0]==len(p.items)
        assert conn.execute('SELECT original FROM future_extension').fetchone()[0]==b'not-a-known-table\x00\xff'


def test_cancel_forced_upgrade_does_not_open_legacy(gui_project,monkeypatch):
    from PyQt6 import QtWidgets
    p=gui_project;view=p.view;view.require_directory_upgrade=True
    monkeypatch.setattr(QtWidgets.QMessageBox,'question',lambda *a,**k:QtWidgets.QMessageBox.StandardButton.Cancel)
    assert view.open_from_file(str(p.source)) is False
    assert getattr(view,'_storage_operation',None) is None
    assert getattr(view,'_opening_operation',None) is None


def test_reset_action_remains_available_in_aggregate_canvas(main_window):
    from prism.actions.actions import actions
    view=main_window.view
    view.set_material_navigation_active(True)
    assert actions['fit_scene'].qaction.isEnabled()
    view.set_material_navigation_active(False)


def test_failed_forced_upgrade_keeps_old_file_and_never_opens_copy(gui_project,qtbot,monkeypatch,tmp_path):
    import sqlite3,hashlib
    from PyQt6 import QtWidgets
    from prism.actions import storage_workflow
    p=gui_project;view=p.view;view.require_directory_upgrade=True
    with sqlite3.connect(p.source) as conn:
        conn.execute('CREATE TRIGGER unknown_extension AFTER INSERT ON items BEGIN SELECT 1; END')
    before=hashlib.sha256(p.source.read_bytes()).hexdigest()
    destination=tmp_path/'refused.prism';opened=[];errors=[]
    monkeypatch.setattr(QtWidgets.QMessageBox,'question',lambda *a,**k:QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QtWidgets.QMessageBox,'warning',lambda *a,**k:errors.append(a[2]))
    monkeypatch.setattr(QtWidgets.QFileDialog,'getSaveFileName',lambda *a,**k:(str(destination),'Prism'))
    monkeypatch.setattr(storage_workflow,'_open_result',lambda *a,**k:opened.append(a))
    view.open_from_file(str(p.source))
    qtbot.waitUntil(lambda:getattr(view,'_storage_operation',None) is None,timeout=20000)
    assert errors and not opened
    assert not destination.exists()
    assert hashlib.sha256(p.source.read_bytes()).hexdigest()==before


def test_reset_while_aggregate_layout_is_pending_lands_on_new_geometry(main_window,qtbot):
    from prism.asset_records import ImageRecord
    browser=main_window.asset_browser;canvas=browser.list
    main_window.resize(1200,800);main_window.tabs.setCurrentWidget(browser)
    from PyQt6 import QtWidgets
    QtWidgets.QApplication.processEvents()
    browser.timer.stop()
    items=[ImageRecord({'data':{'imageWidth':100,'imageHeight':80}},('unused',i)) for i in range(2)]
    browser.model.replace(items);browser.timer.stop()
    canvas._start_staged_rebuild(keep_layout=False)
    assert canvas._build_state is not None
    camera_policy.request_reset(canvas,camera_policy.aggregate_content_bounds)
    assert canvas._camera_pending_reset[3]
    qtbot.waitUntil(lambda:canvas._build_state is None,timeout=10000)
    bounds=camera_policy.aggregate_content_bounds(canvas)
    assert bounds is not None
    assert not canvas._camera_pending_reset[3]
    visible=canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    assert visible.adjusted(-2,-2,2,2).contains(bounds)
