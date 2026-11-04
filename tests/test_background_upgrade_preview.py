"""Production upgrade route with a held converter and real GUI installation."""
import hashlib
import threading
import json
import sqlite3
import os

from PyQt6 import QtCore, QtGui, QtWidgets, QtTest
import pytest

from tests.test_partial_project import project, png
from prism.storage import entrypoint
from prism.actions import storage_workflow, clipboard_workflow


@pytest.fixture(autouse=True)
def valid_gui_fixture(project):
    # The partial-save fixture intentionally contains malformed resource nodes
    # and non-GUI stand-ins. Build valid private GUI pages before measuring.
    with sqlite3.connect(project) as connection:
        for table in ('workspace_nodes', 'material_records', 'workspace_pages', 'board_mindmap'):
            connection.execute('DELETE FROM ' + table)
        for order, canvas in enumerate(('A', 'B')):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                (canvas, 'canvas', canvas, json.dumps({'__prism_page_payload__': True,
                  'content': None, 'tags': [], 'parent': None, 'permanent': False}), order))


def select_upgrade(main_window, tmp_path, monkeypatch):
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    main_window.view.require_directory_upgrade = True
    folder = tmp_path / 'upgraded'
    folder.mkdir()
    monkeypatch.setattr(QtWidgets.QMessageBox, 'question', lambda *a, **kw:
                        QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory', lambda *a, **kw: str(folder))
    return folder


def test_old_canvas_is_readonly_before_background_conversion_then_handoff(
        main_window, project, qtbot, tmp_path, monkeypatch):
    folder = select_upgrade(main_window, tmp_path, monkeypatch)
    view = main_window.view
    original = hashlib.sha256(project.read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    convert = entrypoint.convert
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(8), 'test did not release its own converter'
        return convert(*args, **kwargs)
    monkeypatch.setattr(entrypoint, 'convert', held)
    try:
        view.open_from_file(str(project))
        qtbot.waitUntil(entered.is_set, timeout=10000)
        assert view.filename == str(project.resolve())
        assert not view._project_opening
        assert view.scene._item_count == 12
        assert view._upgrade_preview_readonly
        assert not view.isInteractive()
        assert view._storage_operation.isRunning()
        assert not view._upgrade_preview_session.status.isModal()
        epoch = view._trace_open_epoch
        view.open_from_file(str(tmp_path / 'must-not-replace.bee'))
        assert view._trace_open_epoch == epoch
        assert view.filename == str(project.resolve())
        target = tmp_path / 'forbidden.prism'
        view.do_save(str(target), create_new=True, silent=True)
        assert not target.exists()
        assert view.do_insert_images(['must-not-read.png']) is False
        before = view.undo_stack.count()
        command = QtGui.QUndoCommand('forbidden preview mutation')
        view.undo_stack.push(command)
        assert view.undo_stack.count() == before
        monkeypatch.setattr(QtWidgets.QApplication, 'clipboard', lambda:
                            pytest.fail('Preview paste must not read the global clipboard'))
        clipboard_workflow.paste(view)
    finally:
        release.set()
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is None, timeout=15000)
    target = folder / (project.stem + '-新版.prism')
    assert entrypoint.is_entry(target)
    assert view.filename == str(folder / (target.name + '.data') / 'metadata.sqlite')
    assert not view._upgrade_preview_readonly
    assert view.isInteractive()
    assert hashlib.sha256(project.read_bytes()).hexdigest() == original
    from prism import upgrade_mapping
    saved = upgrade_mapping.hint(view.settings, project)
    assert saved is not None
    assert upgrade_mapping.validate(project, saved) == str(target.resolve())


def test_failed_conversion_keeps_readonly_source_until_explicit_end(
        main_window, project, qtbot, tmp_path, monkeypatch):
    select_upgrade(main_window, tmp_path, monkeypatch)
    def failed(*args, **kw):
        raise OSError('injected target failure')
    monkeypatch.setattr(entrypoint, 'convert', failed)
    view = main_window.view
    view.open_from_file(str(project))
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is not None
                   and view._upgrade_preview_session.phase == 'conversion_failed', timeout=10000)
    session = view._upgrade_preview_session
    assert session.owns()
    assert view._upgrade_preview_readonly
    assert view.filename == str(project.resolve())
    session.end_preview()
    assert view._upgrade_preview_session is None
    assert not view._upgrade_preview_readonly
    assert view.filename is None


@pytest.mark.parametrize('leave', [True, False])
def test_cancel_waits_for_own_converter_before_clearing_preview(
        main_window, project, qtbot, tmp_path, monkeypatch, leave):
    folder = select_upgrade(main_window, tmp_path, monkeypatch)
    view = main_window.view
    entered, release = threading.Event(), threading.Event()
    convert = entrypoint.convert
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(8)
        return convert(*args, **kwargs)
    monkeypatch.setattr(entrypoint, 'convert', held)
    try:
        view.open_from_file(str(project))
        qtbot.waitUntil(entered.is_set, timeout=10000)
        if leave:
            view._upgrade_preview_session.end_preview()
        else:
            view._storage_operation.requestInterruption()
        assert view._upgrade_preview_readonly
        assert view.filename == str(project.resolve())
        assert view._storage_operation.isInterruptionRequested()
    finally:
        release.set()
    if not leave:
        qtbot.waitUntil(lambda: view._upgrade_preview_session.phase == 'conversion_failed', timeout=10000)
        assert view._upgrade_preview_readonly
        assert view.filename == str(project.resolve())
        main_window.open_workspace_page('canvas', 'B')
        assert view.current_canvas_id == 'B'
        assert view.category_panel.resource_tree.isEnabled()
        assert not view.isInteractive()
        view._upgrade_preview_session.end_preview()
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is None, timeout=10000)
    assert view.filename is None
    assert not (folder / (project.stem + '-新版.prism')).exists()


def test_missing_target_during_handoff_keeps_old_source_readonly(
        main_window, project, qtbot, tmp_path, monkeypatch):
    select_upgrade(main_window, tmp_path, monkeypatch)
    monkeypatch.setattr(entrypoint, 'convert', lambda *args, **kw: tmp_path / 'missing.prism')
    view = main_window.view
    view.open_from_file(str(project))
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is not None
                   and view._upgrade_preview_session.phase == 'conversion_failed', timeout=10000)
    session = view._upgrade_preview_session
    assert view.filename == str(project.resolve())
    assert session.owns() and view._upgrade_preview_readonly
    # A late terminal from another epoch must not release the read-only guard.
    from prism.opening_lifecycle import OpenCompletion
    session.open_finished(OpenCompletion('unrelated', session.epoch - 1, 'other', 'ready'))
    assert view._upgrade_preview_session is session
    session.end_preview()
    assert view.filename is None


def test_preview_browses_other_resources_without_editor_writeback_and_restores_page(
        main_window, project, qtbot, tmp_path, monkeypatch):
    select_upgrade(main_window, tmp_path, monkeypatch)
    view = main_window.view
    note = '<p>Previously unopened document, preserved verbatim.</p>'
    tree_body = {'data': {'text': 'Previously unopened mindmap'}, 'children': [
        {'data': {'text': 'Nested child'}}]}
    with sqlite3.connect(project) as connection:
        for page_id, kind, body in (('D', 'document', note), ('M', 'mindmap', tree_body)):
            connection.execute('INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                (page_id, kind, page_id, json.dumps({'__prism_page_payload__': True,
                 'content': body, 'tags': [], 'parent': None, 'deletedAt': None,
                 'permanent': False}), 10 if kind == 'document' else 11))
        for page_id, kind in (('B', 'canvas'), ('D', 'document'), ('M', 'mindmap')):
            node = dict(id='node:'+page_id, projectId='stable-project', section=kind, nodeType='page',
                        pageId=page_id, title=page_id, parentId=None, order=0,
                        deletedAt=None, permanent=False)
            connection.execute('INSERT INTO workspace_nodes VALUES(?,?)', (node['id'], json.dumps(node)))
    original = hashlib.sha256(project.read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    convert = entrypoint.convert
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(8), 'test did not release its own converter'
        return convert(*args, **kwargs)
    monkeypatch.setattr(entrypoint, 'convert', held)
    try:
        view.open_from_file(str(project))
        qtbot.waitUntil(entered.is_set, timeout=10000)
        tree = view.category_panel.resource_tree
        assert tree.isEnabled()
        index = tree.model().index_for_id('node:B')
        assert index.isValid()
        tree.expandAll()
        tree.scrollTo(index)
        QtTest.QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               pos=tree.visualRect(index).center())
        assert view.current_canvas_id == 'B'
        assert not view.isInteractive()
        main_window.open_workspace_page('document', 'default-document')
        panel = main_window.document_panel
        assert main_window.tabs.currentWidget() is panel
        assert panel.isEnabled()
        qtbot.waitUntil(lambda: panel._ready and not panel._pending_push, timeout=5000)
        document_mode = []
        panel._page.runJavaScript('window.__quill.isEnabled()', document_mode.append)
        qtbot.waitUntil(lambda: bool(document_mode), timeout=3000)
        assert document_mode == [False]
        before = panel._scene_html()
        panel._on_content_changed('<p>forbidden editor callback</p>')
        panel._set_scene_html('<p>forbidden synchronous writeback</p>')
        assert panel._scene_html() == before
        main_window.open_workspace_page('mindmap', 'default-mindmap')
        mindmap = main_window.mindmap_panel
        assert main_window.tabs.currentWidget() is mindmap
        try:
            qtbot.waitUntil(lambda: mindmap._ready and not getattr(mindmap, '_tree_loading', False), timeout=5000)
        except Exception:
            pytest.fail(f'mindmap ready={mindmap._ready}, loading={getattr(mindmap, "_tree_loading", None)}, status={mindmap._status.text()}')
        map_mode = []
        mindmap._page.runJavaScript('!!window.__mindMap && window.__mindMap.opt.readonly', map_mode.append)
        qtbot.waitUntil(lambda: bool(map_mode), timeout=3000)
        assert map_mode == [True]
        before_tree = mindmap._scene_tree()
        mindmap._on_tree_changed(json.dumps({'tree': {'data': {'text': 'forbidden'}}}))
        assert mindmap._scene_tree() == before_tree
        index = tree.model().index_for_id('node:D')
        tree.scrollTo(index)
        QtTest.QTest.mouseClick(tree.viewport(), QtCore.Qt.MouseButton.LeftButton,
                               pos=tree.visualRect(index).center())
        unopened_document = main_window._page_panels['D']
        assert main_window.tabs.currentWidget() is unopened_document
        assert unopened_document.isEnabled()
        assert unopened_document._scene_html() == note
        main_window.open_workspace_page('mindmap', 'M')
        unopened_map = main_window._page_panels['M']
        assert main_window.tabs.currentWidget() is unopened_map
        assert unopened_map._scene_tree() == tree_body
        main_window.open_workspace_page('document', 'default-document')
        assert not getattr(main_window, '_page_store_in_progress', False)
        assert view._upgrade_preview_readonly
        assert hashlib.sha256(project.read_bytes()).hexdigest() == original
    finally:
        release.set()
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is None, timeout=15000)
    assert main_window.tabs.currentWidget() is main_window.document_panel
    assert not view._upgrade_preview_readonly
    assert tree.isEnabled()
    assert hashlib.sha256(project.read_bytes()).hexdigest() == original
    panel = main_window.document_panel
    qtbot.waitUntil(lambda: panel._ready and not panel._pending_push, timeout=5000)
    unlocked = []
    panel._page.runJavaScript('window.__quill.isEnabled()', unlocked.append)
    qtbot.waitUntil(lambda: bool(unlocked), timeout=3000)
    assert unlocked == [True]


def test_changed_source_does_not_switch_to_a_different_conversion_snapshot(
        main_window, project, qtbot, tmp_path, monkeypatch):
    select_upgrade(main_window, tmp_path, monkeypatch)
    stamp = project.stat()
    def changed(*args, **kwargs):
        os.utime(project, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + 1000000000))
        return tmp_path / 'untrusted.prism'
    monkeypatch.setattr(entrypoint, 'convert', changed)
    view = main_window.view
    view.open_from_file(str(project))
    qtbot.waitUntil(lambda: getattr(view, '_upgrade_preview_session', None) is not None
                   and view._upgrade_preview_session.phase == 'conversion_failed', timeout=10000)
    session = view._upgrade_preview_session
    assert 'Source project changed' in session.label.text()
    assert view.filename == str(project.resolve())
    assert view._upgrade_preview_readonly
    session.end_preview()
