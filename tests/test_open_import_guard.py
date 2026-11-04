"""Opening preparation must refuse imports without disturbing shared state."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from PyQt6 import QtWidgets

from prism.actions import capture_workflow, import_workflow
from prism.clipboard_capture import ClipboardCaptureService, ClipboardSnapshot
from prism.fileio.resource_import import ImportNode, ImportPlan
from prism.widgets.clipboard_inbox import ClipboardInboxDialog
from prism.widgets.import_preview import ImportPreviewDialog


def canvas_plan(paths=(), count=1):
    return ImportPlan('canvas', 'Incoming', tuple(
        ImportNode(str(index), None, 'page', f'Incoming {index}', 'canvas',
                   assets=tuple(paths) if index == 0 else ())
        for index in range(count)))


def capture_target(view, **kwargs):
    return dict(project_id=view.scene.project_id, canvas_id='default-canvas',
                x=0, y=0, **kwargs)


@pytest.fixture
def guarded(view, monkeypatch):
    """Use the real view guard, with an idle baseline worker rather than busy IO."""
    operation = SimpleNamespace(terminal=False)
    monkeypatch.setattr(view, '_opening_operation', operation, raising=False)
    status = Mock()
    monkeypatch.setattr(view, '_catalog_status', status)
    worker = Mock()
    worker.isRunning.return_value = False
    monkeypatch.setattr(view, 'worker', worker, raising=False)
    scheduler = view._load_scheduler
    progress = getattr(view, 'progress', None)
    import_in_progress = getattr(view, '_import_in_progress', False)
    scene = view.scene
    items = tuple(scene.items_for_save())
    pages = deepcopy(scene.workspace_pages)
    service = view.category_panel.resource_service
    roots = {section: deepcopy(service.listChildren(section))
             for section in ('canvas', 'document', 'mindmap')}
    undo = (view.undo_stack.count(), view.undo_stack.index(), view.undo_stack.isClean())
    current = view.current_canvas_id
    window = view.window()
    page_id = window.current_page_id
    tab = window.tabs.currentWidget()
    blocked = []
    for obj, name in ((view, 'do_insert_images'), (view, '_ensure_current_canvas'),
                      (view, '_import_target_panel'), (view, 'mark_content_dirty'),
                      (window, 'open_workspace_page'),
                      (window, 'create_workspace_page_quietly'),
                      (service, 'createFolder'), (service, 'createPage'),
                      (import_workflow.fileio, 'ThreadedIO')):
        stub = Mock(side_effect=AssertionError(f'Unexpected mutation: {name}'))
        monkeypatch.setattr(obj, name, stub)
        blocked.append(stub)

    def unchanged():
        assert view.scene is scene
        assert tuple(scene.items_for_save()) == items
        assert scene.workspace_pages == pages
        assert {section: service.listChildren(section) for section in roots} == roots
        assert (view.undo_stack.count(), view.undo_stack.index(),
                view.undo_stack.isClean()) == undo
        assert view.worker is worker
        assert view._load_scheduler is scheduler
        assert getattr(view, 'progress', None) is progress
        assert getattr(view, '_import_in_progress', False) == import_in_progress
        assert view.current_canvas_id == current
        assert window.current_page_id == page_id
        assert window.tabs.currentWidget() is tab
        worker.assert_not_called()
        worker.isRunning.assert_not_called()
        worker.start.assert_not_called()
        for stub in blocked:
            stub.assert_not_called()

    return SimpleNamespace(view=view, operation=operation, status=status,
                           service=service, unchanged=unchanged)


@pytest.mark.parametrize('route', ['document', 'subtree', 'inbox', 'capture',
                                   'plan', 'assets', 'empty-assets'])
def test_nonterminal_open_refuses_before_any_boundary(guarded, monkeypatch, tmp_path, route):
    view = guarded.view
    chooser = Mock(side_effect=AssertionError('No dialog during opening'))
    navigation = Mock(side_effect=AssertionError('No navigation during opening'))
    monkeypatch.setattr('prism.navigation.defer_navigation', navigation)
    monkeypatch.setattr(import_workflow, 'ask_import_source', chooser)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', chooser)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames', chooser)
    original = tmp_path / 'retained.png'
    original.write_bytes(b'original capture bytes')
    plan = canvas_plan([str(original)])
    if route == 'document':
        import_workflow.import_document(view)
    elif route == 'subtree':
        import_workflow.import_resource_subtree(view, 'canvas')
    elif route == 'inbox':
        capture_workflow.inbox(view)
    elif route == 'capture':
        capture_workflow.import_capture(view, capture_target(view), str(original))
    elif route == 'plan':
        outcome = import_workflow._apply_plan(view, guarded.service, plan, None)
        assert outcome.canceled and not outcome.nodes
    else:
        if route == 'empty-assets':
            plan = canvas_plan()
        staging = tmp_path / 'staging'
        staging.mkdir()
        (staging / 'unused').write_bytes(b'staging')
        import_workflow._import_assets(view, plan, import_workflow.ImportOutcome('canvas'),
                                       str(staging))
        assert not staging.exists()
    assert original.read_bytes() == b'original capture bytes'
    chooser.assert_not_called()
    navigation.assert_not_called()
    guarded.status.assert_called()
    guarded.unchanged()


def start_open_on_return(guarded, result):
    def callback(*args, **kwargs):
        guarded.operation.terminal = False
        return result
    return callback


def test_document_rechecks_after_file_dialog(guarded, monkeypatch):
    guarded.operation.terminal = True
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName',
                        start_open_on_return(guarded, ('incoming.docx', '')))
    import_workflow.import_document(guarded.view)
    guarded.status.assert_called_once()
    guarded.unchanged()


@pytest.mark.parametrize('shape', ['folder', 'archive', 'files'])
def test_subtree_rechecks_after_source_dialog(guarded, monkeypatch, shape):
    guarded.operation.terminal = True
    monkeypatch.setattr(import_workflow, 'ask_import_source',
                        start_open_on_return(guarded, shape))
    next_dialog = Mock(side_effect=AssertionError('No second dialog after refusal'))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory', next_dialog)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', next_dialog)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames', next_dialog)
    import_workflow.import_resource_subtree(guarded.view, 'canvas')
    next_dialog.assert_not_called()
    guarded.unchanged()


@pytest.mark.parametrize('shape,section,chooser,result', [
    ('folder', 'canvas', 'getExistingDirectory', 'incoming'),
    ('archive', 'canvas', 'getOpenFileName', ('incoming.zip', '')),
    ('files', 'canvas', 'getOpenFileNames', (['incoming.png'], '')),
    ('files', 'document', 'getOpenFileNames', (['incoming.docx'], '')),
    ('files', 'mindmap', 'getOpenFileNames', (['incoming.xmind'], '')),
])
def test_subtree_rechecks_after_file_chooser(
        guarded, monkeypatch, shape, section, chooser, result):
    guarded.operation.terminal = True
    monkeypatch.setattr(import_workflow, 'ask_import_source', lambda *args: shape)
    monkeypatch.setattr(QtWidgets.QFileDialog, chooser,
                        start_open_on_return(guarded, result))
    import_workflow.import_resource_subtree(guarded.view, section)
    guarded.status.assert_called_once()
    guarded.unchanged()


@pytest.mark.parametrize('loose', [False, True])
def test_preview_rechecks_before_applying_nodes(guarded, monkeypatch, tmp_path, loose):
    guarded.operation.terminal = True
    monkeypatch.setattr(ImportPreviewDialog, 'exec', start_open_on_return(
        guarded, QtWidgets.QDialog.DialogCode.Accepted))
    if loose:
        from prism.fileio import resource_import
        plan = ImportPlan('document', 'Incoming', (
            ImportNode('note', None, 'page', 'Note', 'document'),))
        monkeypatch.setattr(resource_import, 'scan_files', lambda *args: plan)
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames',
                            lambda *args: (['incoming.docx'], ''))
        import_workflow._import_loose_files(guarded.view, guarded.service,
                                            'document', None, '')
    else:
        source = tmp_path / 'staging'
        source.mkdir()
        (source / 'incoming.png').write_bytes(b'asset')
        import_workflow._scan_and_apply(guarded.view, guarded.service, 'canvas',
                                        None, str(source), staging=str(source))
        assert not source.exists()
    guarded.status.assert_called_once()
    guarded.unchanged()


def test_apply_plan_rechecks_immediately_before_first_node(guarded, monkeypatch):
    guard = Mock(side_effect=[True, False])
    monkeypatch.setattr(guarded.view, '_allow_import_during_open', guard)
    outcome = import_workflow._apply_plan(guarded.view, guarded.service, canvas_plan(), None)
    assert guard.call_count == 2
    assert outcome.canceled and not outcome.nodes
    guarded.unchanged()


def test_apply_plan_rechecks_after_progress_events(guarded, monkeypatch):
    guarded.operation.terminal = True
    created = []
    monkeypatch.setattr(import_workflow, '_create_node',
                        lambda *args: created.append(args[4]))
    progress = Mock()
    progress.wasCanceled.return_value = False
    monkeypatch.setattr(QtWidgets, 'QProgressDialog', lambda *args, **kwargs: progress)
    monkeypatch.setattr(QtWidgets.QApplication, 'processEvents',
                        start_open_on_return(guarded, None))
    plan = canvas_plan(count=import_workflow.PROGRESS_AFTER + 1)
    outcome = import_workflow._apply_plan(guarded.view, guarded.service, plan, None)
    assert created == [plan.nodes[0]]
    assert outcome.canceled
    progress.close.assert_called_once()
    progress.deleteLater.assert_called_once()
    guarded.unchanged()


@pytest.mark.parametrize('route', ['document', 'inbox', 'capture'])
def test_deferred_navigation_rechecks_opening_before_resuming(guarded, monkeypatch, route):
    guarded.operation.terminal = True
    queued = []
    monkeypatch.setattr('prism.navigation.defer_navigation',
                        lambda window, callback, **kwargs: queued.append(callback) or True)
    if route == 'document':
        import_workflow.import_document(guarded.view)
    elif route == 'inbox':
        capture_workflow.inbox(guarded.view)
    else:
        capture_workflow.import_capture(guarded.view, capture_target(guarded.view), 'retained.png')
    assert len(queued) == 1
    guarded.operation.terminal = False
    queued.pop()()
    guarded.status.assert_called_once()
    guarded.unchanged()


@pytest.mark.parametrize('route', ['inbox-file', 'inbox-target', 'capture-target',
                                   'capture-choose-canvas'])
def test_capture_rechecks_dialogs_without_consuming_file(guarded, monkeypatch, tmp_path, route):
    guarded.operation.terminal = True
    original = tmp_path / 'capture.png'
    original.write_bytes(b'original capture bytes')
    monkeypatch.setattr('prism.capture_service.inbox_directory', lambda: tmp_path)
    target = capture_target(guarded.view)
    if route == 'inbox-file':
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames',
                            start_open_on_return(guarded, ([str(original)], '')))
        picker = Mock(side_effect=AssertionError('No target picker after refusal'))
        monkeypatch.setattr(capture_workflow, 'target', picker)
    else:
        monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames',
                            lambda *args: ([str(original)], ''))
        monkeypatch.setattr(capture_workflow, 'target', start_open_on_return(guarded, target))
    if route.startswith('inbox'):
        capture_workflow.inbox(guarded.view)
    else:
        requested = (capture_target(guarded.view, choose_canvas=True)
                     if route == 'capture-choose-canvas' else None)
        capture_workflow.import_capture(guarded.view, requested, str(original))
    assert original.read_bytes() == b'original capture bytes'
    guarded.status.assert_called_once()
    guarded.unchanged()


@pytest.mark.parametrize('route', ['inbox', 'capture'])
@pytest.mark.parametrize('with_guard', [False, True])
def test_capture_worker_busy_semantics_and_fake_view_compatibility(
        monkeypatch, tmp_path, route, with_guard):
    window = Mock()
    worker = Mock()
    worker.isRunning.return_value = True
    view = SimpleNamespace(window=lambda: window, worker=worker,
                           scene=SimpleNamespace(project_id='p', workspace_pages=[]),
                           do_insert_images=Mock())
    if with_guard:
        view._allow_import_during_open = lambda: True
    target = dict(project_id='p', canvas_id='default-canvas', x=0, y=0)
    notice = Mock()
    monkeypatch.setattr(QtWidgets.QMessageBox, 'information', notice)
    monkeypatch.setattr(capture_workflow, 'target', lambda *args: target)
    monkeypatch.setattr('prism.capture_service.inbox_directory', lambda: tmp_path)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames',
                        lambda *args: (['retained.png'], ''))
    if route == 'inbox':
        capture_workflow.inbox(view)
    else:
        capture_workflow.import_capture(view, target, 'retained.png')
    worker.isRunning.assert_called_once()
    notice.assert_called_once()
    assert '正在导入' in notice.call_args.args[2]
    window.open_workspace_page.assert_not_called()
    view.do_insert_images.assert_not_called()


def test_plan_accepts_fake_view_without_guard():
    from prism.workspace_service import ResourceTreeService
    service = ResourceTreeService('p')
    outcome = import_workflow._apply_plan(SimpleNamespace(), service, canvas_plan(), None)
    assert not outcome.canceled
    assert len(outcome.nodes) == 1
    assert len(service.listChildren('canvas')) == 1


def test_document_accepts_fake_view_without_guard(monkeypatch):
    view = SimpleNamespace(window=lambda: SimpleNamespace(), _import_directory=lambda: '')
    chooser = Mock(return_value=('', ''))
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileName', chooser)
    import_workflow.import_document(view)
    chooser.assert_called_once()


def inbox_dialog(qtbot, imgdata3x3):
    service = ClipboardCaptureService()
    snapshot = ClipboardSnapshot(imgdata3x3, 'png', 'image/png', False)
    service.accept(snapshot)
    dialog = ClipboardInboxDialog(service)
    qtbot.addWidget(dialog)
    dialog.list_widget.setCurrentRow(0)
    return service, dialog, service.items[0]


def test_clipboard_refusal_does_not_emit_or_take_original(guarded, qtbot, imgdata3x3, monkeypatch):
    service, dialog, item = inbox_dialog(qtbot, imgdata3x3)
    dialog.import_allowed = guarded.view._allow_import_during_open
    received = Mock()
    dialog.import_requested.connect(received)
    take = Mock(wraps=service.take)
    monkeypatch.setattr(service, 'take', take)
    dialog._import_selected()
    received.assert_not_called()
    take.assert_not_called()
    assert service.items == [item]
    assert item.data == imgdata3x3
    assert dialog.list_widget.count() == 1
    assert 'remain in the inbox' in dialog.feedback.text()
    guarded.status.assert_called_once()
    guarded.unchanged()


def test_clipboard_rechecks_before_take_if_signal_starts_opening(
        guarded, qtbot, imgdata3x3, monkeypatch):
    guarded.operation.terminal = True
    service, dialog, item = inbox_dialog(qtbot, imgdata3x3)
    dialog.import_allowed = guarded.view._allow_import_during_open
    dialog.import_requested.connect(start_open_on_return(guarded, None))
    take = Mock(wraps=service.take)
    monkeypatch.setattr(service, 'take', take)
    dialog._import_selected()
    take.assert_not_called()
    assert service.items == [item]
    assert item.data == imgdata3x3
    assert 'remain in the inbox' in dialog.feedback.text()
    guarded.unchanged()


@pytest.mark.parametrize('with_guard', [False, True])
def test_clipboard_allowed_or_optional_callback_keeps_existing_behavior(
        qtbot, imgdata3x3, with_guard):
    service, dialog, item = inbox_dialog(qtbot, imgdata3x3)
    if with_guard:
        dialog.import_allowed = lambda: True
    received = Mock()
    dialog.import_requested.connect(received)
    dialog._import_selected()
    received.assert_called_once_with([item], None)
    assert not service.items


@pytest.mark.parametrize('operation,allowed', [
    (None, True),
    (SimpleNamespace(terminal=True), True),
    (SimpleNamespace(terminal=False), False),
])
def test_real_view_guard_only_rejects_nonterminal_operation(
        view, monkeypatch, operation, allowed):
    monkeypatch.setattr(view, '_opening_operation', operation, raising=False)
    status = Mock()
    monkeypatch.setattr(view, '_catalog_status', status)
    assert view._allow_import_during_open() is allowed
    assert status.call_count == (0 if allowed else 1)
