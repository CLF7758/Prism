"""B: real opening ownership, cancellation, installed-ID bases and A readiness."""
import hashlib
import threading
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore

from prism import fileio, paged_open_preparation as preparation
from prism.project_catalog_index import ProjectCatalogIndex
from tests.test_paged_canvas_loader import pages  # noqa: F401
from tests.test_paged_open_preparation import configure_open


@pytest.fixture(autouse=True)
def settle_background_directory_presentation(view, qtbot):
    """Do not destroy a fixture window mid unrelated catalog presentation."""
    yield
    qtbot.waitUntil(lambda: getattr(view, '_catalog_build_active', None) is None,
                    timeout=15000)
    browser = getattr(view.parent, 'asset_browser', None)
    if browser is not None:
        qtbot.waitUntil(lambda: (getattr(browser, '_catalog_pending', None) is None
                                and getattr(browser, '_catalog_query_job', None) is None),
                        timeout=15000)
    qtbot.wait(20)


def fake_full(view, monkeypatch, reader):
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    monkeypatch.setattr(view, '_catalog_scope_instance', lambda *args: None)
    monkeypatch.setattr(fileio, 'load_prism', reader)


def enqueue(scene, count):
    for key in range(count):
        scene.add_item_later({'type': 'text', 'data': {'text': str(key)}})


def test_custom_result_is_not_thread_or_install_completion(view, qtbot, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def reader(filename, scene, worker=None):
        enqueue(scene, 401)
        worker.finished.emit(filename, [])
        entered.set()
        assert release.wait(8)
    fake_full(view, monkeypatch, reader)
    try:
        assert view.open_from_file('controlled.prism') is False
        op = view._opening_operation
        qtbot.waitUntil(entered.is_set)
        qtbot.waitUntil(lambda: op.reader_result is not None)
        assert view.filename is None
        assert not op.reader_stopped and view._file_operation_pending()
        assert view.open_from_file('must-not-replace.prism', readonly=True) is False
        assert view._opening_operation is op
    finally:
        release.set()
    qtbot.waitUntil(lambda: op.terminal, timeout=10000)
    assert view.filename == 'controlled.prism'
    assert view.scene._item_count == 401
    assert not view._file_operation_pending()


def test_real_unedited_full_open_is_clean(view, qtbot, monkeypatch):
    import os
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    source = os.path.join(os.path.dirname(__file__), 'assets', 'test1item.bee')
    view.open_from_file(source)
    operation = view._opening_operation
    qtbot.waitUntil(lambda: operation.terminal, timeout=10000)
    assert view.filename == source
    assert not view.is_dirty()


def test_reader_stopped_but_gui_queue_still_protects_save_and_replace(view, qtbot, monkeypatch, tmp_path):
    def reader(filename, scene, worker=None):
        enqueue(scene, 401)
        worker.finished.emit(filename, [])
    fake_full(view, monkeypatch, reader)
    original = view._load_scheduler.finish
    held = []
    monkeypatch.setattr(view._load_scheduler, 'finish', lambda callback: held.append(callback))
    view.open_from_file('controlled.prism')
    op = view._opening_operation
    qtbot.waitUntil(lambda: bool(held))
    assert op.reader_stopped and not op.reader.isRunning()
    assert view._file_operation_pending()
    assert not view._allow_project_replacement()
    target = tmp_path / 'not-yet.prism'
    view.do_save(str(target), create_new=True)
    assert not target.exists() and view.worker is op.reader
    original(held[0])
    qtbot.waitUntil(lambda: op.terminal)
    assert view.scene._item_count == 401


def test_clear_waits_for_a_cancelled_reader_that_still_writes(view, qtbot, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    def reader(filename, scene, worker=None):
        enqueue(scene, 1)
        entered.set()
        assert release.wait(8)
        # Models a read already past its last cancellation checkpoint.
        enqueue(scene, 1)
        worker.finished.emit('', [])
    fake_full(view, monkeypatch, reader)
    try:
        view.open_from_file('controlled.prism')
        op = view._opening_operation
        qtbot.waitUntil(entered.is_set)
        project = view.scene.project_id
        assert view.clear_scene() is False
        assert view.scene.project_id == project
        assert not op.terminal and view._file_operation_pending()
    finally:
        release.set()
    qtbot.waitUntil(lambda: op.terminal)
    assert view.filename is None and view.scene._item_count == 0
    assert view.scene.items_to_add.empty()
    assert view.scene.project_id != project
    assert not view._file_operation_pending()


@pytest.mark.parametrize('mode', ['blank', 'exception'])
def test_reader_without_success_never_adopts_filename(view, qtbot, monkeypatch, mode):
    def reader(filename, scene, worker=None):
        if mode == 'exception':
            raise RuntimeError('read exploded')
        worker.finished.emit('', [])
    fake_full(view, monkeypatch, reader)
    view.open_from_file('controlled.prism')
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal)
    assert view.filename is None
    assert view._paged_open_failed and not view._file_operation_pending()


def test_old_progress_result_native_and_preparation_do_not_touch_new_open(view, qtbot, monkeypatch):
    def reader(filename, scene, worker=None):
        worker.finished.emit(filename, [])
    fake_full(view, monkeypatch, reader)
    view.open_from_file('old.prism')
    old = view._opening_operation
    bridge = old.bridge
    qtbot.waitUntil(lambda: old.terminal)
    view.open_from_file('new.prism')
    new = view._opening_operation
    qtbot.waitUntil(lambda: new.terminal)
    before = (view.filename, view.scene.project_id, view._content_dirty, view.worker)
    bridge.on_progress(999)
    bridge.on_result('old.prism', ['bad'])
    bridge.on_stopped()
    view._paged_operation_completed(old, 'scope', None, RuntimeError('old'), 1)
    assert before == (view.filename, view.scene.project_id, view._content_dirty, view.worker)
    assert view._opening_operation is new and not view._load_scheduler.timer.isActive()


def test_reentrant_clear_during_finalization_cannot_start_baseline_or_build(view, qtbot, monkeypatch):
    def reader(filename, scene, worker=None):
        worker.finished.emit(filename, [])
    fake_full(view, monkeypatch, reader)
    original = view.category_panel.force_rebuild
    fired = []
    def rebuild():
        original()
        if getattr(view, '_opening_operation', None) is not None and not fired:
            fired.append(True)
            assert view.clear_scene() is False
    monkeypatch.setattr(view.category_panel, 'force_rebuild', rebuild)
    baseline, build = Mock(), Mock()
    monkeypatch.setattr(view, '_register_paged_session', baseline)
    monkeypatch.setattr(view, '_maybe_start_catalog_build', build)
    view.open_from_file('controlled.prism')
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal)
    assert view.filename is None and view.scene.items_to_add.empty()
    baseline.assert_not_called()
    build.assert_not_called()


def test_baseline_failure_keeps_browsing_and_blocks_save_as(pages, qtbot, monkeypatch, tmp_path):  # noqa: F811
    view, scope, _ = pages
    configure_open(view, scope, monkeypatch)
    def fail(*args):
        raise RuntimeError('baseline unavailable')
    monkeypatch.setattr(preparation, 'prepare_session', fail)
    digest = hashlib.sha256(scope.project_path.read_bytes()).digest()
    view.open_from_file(str(scope.project_path))
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal, timeout=15000)
    assert op.phase == 'baseline_failed'
    assert view.filename == str(scope.project_path)
    assert view.scene._item_count > 0 and view.scene._partial_project_source
    assert view.scene._partial_project_session is None
    assert not view._file_operation_pending()
    target = tmp_path / 'refused.prism'
    view.do_save(str(target), create_new=True)
    assert not target.exists()
    assert 'browsing is available' in view.parent.statusBar().currentMessage().lower()
    assert hashlib.sha256(scope.project_path.read_bytes()).digest() == digest


def test_delete_during_install_keeps_source_baseline_and_real_dirty(pages, qtbot, monkeypatch):  # noqa: F811
    view, scope, _ = pages
    configure_open(view, scope, monkeypatch)
    original = view._register_paged_session
    def register(filename):
        item = next(item for item in view.scene.iter_assets() if item.save_id == 1)
        view.scene.removeItem(item)
        view.mark_content_dirty()
        original(filename)
    monkeypatch.setattr(view, '_register_paged_session', register)
    view.open_from_file(str(scope.project_path))
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal, timeout=15000)
    session = view.scene._partial_project_session
    assert 1 in op.installed_ids and 1 in session.loaded()
    assert view.is_dirty()
    assert not any(item.save_id == 1 for item in view.scene.iter_assets())
    from prism.partial_project import PartialSaveConflict
    # Raw disappearance is not authorized trash. The retained baseline makes
    # the existing safety protocol refuse it instead of silently forgetting it.
    with pytest.raises(PartialSaveConflict, match='explicit trash command'):
        session.capture(view.scene)


def test_trash_during_install_remains_dirty_and_saveable(pages, qtbot, monkeypatch):  # noqa: F811
    view, scope, _ = pages
    configure_open(view, scope, monkeypatch)
    from prism import commands
    original = view._register_paged_session
    def register(filename):
        item = next(item for item in view.scene.iter_assets() if item.save_id == 1)
        view.undo_stack.push(commands.TrashItems(view.scene, [item]))
        original(filename)
    monkeypatch.setattr(view, '_register_paged_session', register)
    view.open_from_file(str(scope.project_path))
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal, timeout=15000)
    assert view.is_dirty()
    session = view.scene._partial_project_session
    assert 1 in session.loaded()
    snapshot = session.capture(view.scene)
    import json
    assert any(change.table == 'items' and change.key == 1
               and json.loads(change.after[-1]).get('deletedAt')
               for change in snapshot.changes)


def test_source_first_screen_and_baseline_do_not_wait_for_A_catalog(pages, qtbot, monkeypatch):  # noqa: F811
    view, scope, _ = pages
    view.filename = None
    from prism import thumbnail_cache
    monkeypatch.setattr(thumbnail_cache, 'cache_directory', lambda: str(scope.cache_path.parent / 'thumbs'))
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    entered, release = threading.Event(), threading.Event()
    original = ProjectCatalogIndex.build
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(12)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ProjectCatalogIndex, 'build', delayed)
    try:
        view.open_from_file(str(scope.project_path))
        op = view._opening_operation
        qtbot.waitUntil(entered.is_set, timeout=15000)
        qtbot.waitUntil(lambda: op.terminal, timeout=15000)
        assert view.filename == str(scope.project_path)
        assert view.scene._item_count == 256
        assert not view._open_scope.ready and not view._file_operation_pending()
        assert view.scene._partial_project_session is not None
        assert view.request_canvas_assets('B') is False
        assert view.request_canvas_assets('A') is False
        assert view._pending_canvas_demand == 'A'
        assert not hasattr(view, '_paged_canvas_loader') or not view._paged_canvas_loader.active
    finally:
        release.set()
    qtbot.waitUntil(lambda: view._catalog_build_active is None, timeout=15000)
    qtbot.waitUntil(lambda: view.scene._item_count == 320, timeout=15000)
    assert view._open_scope.ready


def test_real_unedited_scoped_open_is_clean(pages, qtbot, monkeypatch):  # noqa: F811
    view, scope, _ = pages
    configure_open(view, scope, monkeypatch)
    view.open_from_file(str(scope.project_path))
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal, timeout=15000)
    assert not view.is_dirty()


def test_open_terminal_publishes_whole_project_sidebar_counts(pages, qtbot, monkeypatch):
    view, scope, _ = pages
    configure_open(view, scope, monkeypatch)
    panel = view.category_panel
    view.clear_scene()
    assert panel._nav_counts['all'].text() == '0'
    view.open_from_file(str(scope.project_path))
    qtbot.waitUntil(lambda: view._opening_operation.terminal, timeout=15000)
    # No user edit, metadata signal or manual recount may be needed to replace
    # the empty-scene counters left behind by the opening suppression gate.
    assert panel._nav_counts['all'].text() == '362'  # 360 images + two canvas resources.
    assert panel._implicit_default_canvases['A'] == 320
    assert panel._implicit_default_canvases['B'] == 40


def test_clear_during_scope_waits_for_current_cancellation_terminal(view, qtbot, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    def resolve(*args):
        entered.set()
        assert release.wait(8)
        raise preparation.PreparationCancelled('cancel')
    monkeypatch.setattr(preparation, 'resolve_scope', resolve)
    try:
        view.open_from_file('controlled.prism')
        op = view._opening_operation
        qtbot.waitUntil(entered.is_set)
        assert view.clear_scene() is False
        assert not op.terminal and view._file_operation_pending()
    finally:
        release.set()
    qtbot.waitUntil(lambda: op.terminal)
    assert not view._file_operation_pending() and view.filename is None


def test_clear_drops_old_deferred_bodies_before_new_save(view, qtbot, tmp_path):
    import sqlite3
    view.scene._deferred_board_extras = {'note_html', 'mindmap_tree', 'workspace_pages'}
    view.scene._deferred_page_ids = {'old-page'}
    view.scene._board_extras_source = str(tmp_path / 'nonexistent-old.prism')
    view.clear_scene()
    assert view.scene._deferred_board_extras is None
    assert view.scene._deferred_page_ids == set()
    assert view.scene._board_extras_source is None
    target = tmp_path / 'new.prism'
    view.do_save(str(target), create_new=True)
    qtbot.waitUntil(lambda: target.exists() and getattr(view, '_save_operation', None) is None,
                    timeout=15000)
    with sqlite3.connect(target) as db:
        assert db.execute('SELECT count(*) FROM workspace_pages').fetchone()[0] == 0


def test_error_placeholder_gets_source_baseline_and_preserves_row(pages, qtbot, monkeypatch):  # noqa: F811
    import sqlite3
    from prism.items import PrismErrorItem
    view, scope, _ = pages
    with sqlite3.connect(scope.project_path) as db:
        db.execute("UPDATE sqlar SET data=X'',sz=0 WHERE item_id=1")
        before = db.execute('SELECT * FROM items WHERE id=1').fetchone()
    configure_open(view, scope, monkeypatch)
    view.open_from_file(str(scope.project_path))
    op = view._opening_operation
    qtbot.waitUntil(lambda: op.terminal, timeout=15000)
    assert 1 in op.installed_ids
    assert any(isinstance(item, PrismErrorItem) and item.original_save_id == 1
               for item in view.scene.iter_assets())
    session = view.scene._partial_project_session
    assert 1 in session.loaded()
    snapshot = session.capture(view.scene)
    assert not any(change.table == 'items' and change.key == 1 for change in snapshot.changes)
    with sqlite3.connect(scope.project_path) as db:
        assert db.execute('SELECT * FROM items WHERE id=1').fetchone() == before


@pytest.mark.parametrize('save_as', [False, True])
def test_waiting_canvas_survives_new_source_revision_after_save(pages, qtbot, monkeypatch, tmp_path, save_as):  # noqa: F811
    view, scope, _ = pages
    view.filename = None
    from prism import thumbnail_cache
    monkeypatch.setattr(thumbnail_cache, 'cache_directory', lambda: str(scope.cache_path.parent / 'thumbs'))
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    entered, release = threading.Event(), threading.Event()
    original = ProjectCatalogIndex.build
    def delayed(self, *args, **kwargs):
        entered.set()
        assert release.wait(20)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(ProjectCatalogIndex, 'build', delayed)
    target = tmp_path / 'saved-copy.prism' if save_as else scope.project_path
    try:
        view.open_from_file(str(scope.project_path))
        op = view._opening_operation
        qtbot.waitUntil(lambda: op.terminal and entered.is_set(), timeout=15000)
        assert view.request_canvas_assets('B') is False
        view.mark_content_dirty()
        view.do_save(str(target), create_new=save_as)
        qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None
                        and view.filename == str(target), timeout=15000)
        assert str(view.scene._partial_project_session.source) == str(target)
    finally:
        release.set()
    qtbot.waitUntil(lambda: view._catalog_build_active is None, timeout=15000)
    qtbot.waitUntil(lambda: set(range(321,361)).issubset(view.scene._partial_project_session.loaded()),
                    timeout=15000)
    assert view._open_scope.project_path == target


def test_import_entrypoints_do_not_mutate_nonterminal_open(view):
    from types import SimpleNamespace
    operation = SimpleNamespace(terminal=False, phase='baseline')
    view._opening_operation = operation
    worker = getattr(view, 'worker', None)
    count = view.scene._item_count
    undo = view.undo_stack.index()
    assert view.do_insert_images(['anything.png']) is False
    assert view._on_inbox_import([object()]) is False
    assert getattr(view, 'worker', None) is worker
    assert view.scene._item_count == count
    assert view.undo_stack.index() == undo
    assert not getattr(view, '_import_in_progress', False)
    view._opening_operation = None


def test_project_replacement_waits_for_import_gui_ack(view):
    view._import_in_progress = True
    try:
        assert not view._allow_project_replacement()
    finally:
        view._import_in_progress = False


def test_close_during_open_does_not_store_old_editor(main_window, monkeypatch):
    from types import SimpleNamespace
    from PyQt6 import QtGui
    view = main_window.view
    view._opening_operation = SimpleNamespace(terminal=False)
    store = Mock()
    monkeypatch.setattr(main_window, '_store_current_page', store)
    event = QtGui.QCloseEvent()
    main_window.closeEvent(event)
    assert not event.isAccepted()
    store.assert_not_called()
    view._opening_operation = None


def test_finalize_exception_clears_adopted_filename_and_installed_scene(view, qtbot, monkeypatch):
    def reader(filename, scene, worker=None):
        enqueue(scene, 1)
        worker.finished.emit(filename, [])
    fake_full(view, monkeypatch, reader)
    original = view.category_panel.force_rebuild
    def rebuild():
        operation = getattr(view, '_opening_operation', None)
        if operation is not None and not operation.terminal:
            raise RuntimeError('finalization exploded')
        original()
    monkeypatch.setattr(view.category_panel, 'force_rebuild', rebuild)
    view.open_from_file('controlled.prism')
    operation = view._opening_operation
    qtbot.waitUntil(lambda: operation.terminal)
    assert operation.phase == 'failed'
    assert view.filename is None
    assert view.scene._item_count == 0 and view.scene.items_to_add.empty()
    assert not view._file_operation_pending()


def test_capture_start_is_blocked_before_reading_unstable_scene(view, monkeypatch):
    from types import SimpleNamespace
    from prism.actions import capture_workflow
    view._opening_operation = SimpleNamespace(terminal=False)
    service = Mock()
    monkeypatch.setattr(capture_workflow, 'service', service)
    try:
        capture_workflow.capture(view)
        service.assert_not_called()
    finally:
        view._opening_operation = None


@pytest.mark.parametrize('during_dialog', [False, True])
def test_xmind_import_checks_opening_before_and_after_dialog(view, monkeypatch, during_dialog):
    from types import SimpleNamespace
    from PyQt6 import QtWidgets
    from prism import navigation
    monkeypatch.setattr(navigation, 'defer_navigation', lambda *a, **k: False)
    panel = Mock()
    monkeypatch.setattr(view, '_import_target_panel', panel)
    def dialog(**kwargs):
        view._opening_operation = SimpleNamespace(terminal=False)
        return ('chosen.xmind', '')
    choose = Mock(side_effect=dialog)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', choose)
    if not during_dialog:
        view._opening_operation = SimpleNamespace(terminal=False)
    try:
        view.on_action_import_mindmap()
        panel.assert_not_called()
        assert choose.call_count == int(during_dialog)
    finally:
        view._opening_operation = None
