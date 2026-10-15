"""Capture integration must preserve sources, identities and the GUI process."""
import json
from pathlib import Path
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui
from prism import fileio
from prism.capture_service import CaptureService, prepare_pin
from prism.actions import capture_workflow


def make_image(tmp_path):
    image = QtGui.QImage(100, 80, QtGui.QImage.Format.Format_ARGB32)
    image.fill(QtGui.QColor(40, 80, 120))
    path = tmp_path/'original.png'
    assert image.save(str(path))
    return path


def test_pin_preparation_uses_original_and_preserves_file(tmp_path, qapp):
    original = make_image(tmp_path)
    contents = original.read_bytes()
    destination = tmp_path/'reference.png'
    prepare_pin((None, str(original), None), (10, 20, 50, 40), -1, True, .5, destination)
    result = QtGui.QImage(str(destination))
    assert result.size() == QtCore.QSize(50, 40)
    pixel = result.pixelColor(0, 0)
    assert abs(pixel.alpha()-128) <= 1
    assert abs(pixel.red()-pixel.green()) <= 1
    assert original.read_bytes() == contents


def test_overlay_ready_keeps_capture_target_and_aggregate_does_not_prompt(view, monkeypatch):
    from unittest.mock import Mock
    from prism.actions import capture_workflow
    adapter = CaptureService(view)
    target = dict(project_id=view.scene.project_id, canvas_id='default-canvas', kind='capture', started=0.1)
    adapter.jobs['pending'] = target
    adapter._message(dict(event='overlay_ready', request_id='pending'))
    assert adapter.jobs['pending'] is target
    monkeypatch.setattr(view, '_material_canvas', Mock(return_value=object()))
    capture = Mock()
    monkeypatch.setattr(capture_workflow, 'service', lambda _: capture)
    monkeypatch.setattr(capture_workflow, 'target', Mock(side_effect=AssertionError('must not open target picker before overlay')))
    capture_workflow.capture(view)
    assert capture.capture.call_args.args[0]['choose_canvas']
    adapter.close()


def test_capture_result_imports_once_and_rejects_external_paths(view, tmp_path, monkeypatch, qtbot):
    monkeypatch.setattr('prism.capture_service.inbox_directory', lambda: tmp_path/'inbox')
    adapter = CaptureService(view)
    adapter._write = Mock()
    imported = Mock()
    adapter.imported.connect(imported)
    request = 'capture-1'
    target = dict(project_id=view.scene.project_id, canvas_id='default-canvas')
    adapter.jobs[request] = target
    image = QtGui.QImage(str(make_image(tmp_path)))
    path = Path(adapter.directory.name)/'capture.png'
    assert image.save(str(path))
    value = dict(event='captured', request_id=request, path=str(path))
    adapter._message(value)
    adapter._message(value)
    qtbot.waitUntil(lambda: imported.call_count == 1)
    assert imported.call_count == 1
    assert imported.call_args.args[0] == target
    assert Path(imported.call_args.args[1]).read_bytes() == path.read_bytes()
    adapter.jobs['bad'] = target
    import pytest
    with pytest.raises(ValueError, match='路径'):
        adapter._message(dict(event='captured', request_id='bad', path=str(tmp_path/'outside.png')))
    adapter.close()


def test_old_project_result_does_not_insert(view, tmp_path, monkeypatch):
    notice = Mock()
    monkeypatch.setattr('PyQt6.QtWidgets.QMessageBox.information', notice)
    view.do_insert_images = Mock()
    capture_workflow.import_capture(view, dict(project_id='old', canvas_id='default-canvas'), str(make_image(tmp_path)))
    view.do_insert_images.assert_not_called()
    notice.assert_called_once()


def test_capture_toolbar_and_actions_are_present(main_window):
    from prism.actions.actions import actions
    assert main_window.canvas_toolbar.capture_button.objectName() == 'screenCaptureButton'
    assert main_window.canvas_toolbar.pin_button.objectName() == 'pinReferenceButton'
    assert actions['screen_capture'].qaction is not None
    assert actions['pin_reference'].qaction is not None


def test_handshake_flushes_bounded_queue_without_starting_helper(view):
    adapter = CaptureService(view)
    adapter._write = Mock()
    adapter.pending = [dict(command='capture', request_id='a')]
    adapter._message(dict(event='ready', protocol_version=1))
    assert adapter.ready and not adapter.pending
    adapter._write.assert_called_once_with(dict(command='capture', request_id='a'))
    adapter.close()


def test_real_qt5_helper_capture_roundtrip_inserts_and_undoes(view, tmp_path, monkeypatch, qtbot):
    import pytest
    from prism.capture_service import component_command
    try:
        component_command()
    except OSError:
        pytest.skip('Optional ScreenPinKit runtime is not installed on this machine')
    monkeypatch.setattr('prism.capture_service.inbox_directory', lambda: tmp_path/'inbox')
    adapter = capture_workflow.service(view)
    set_arguments = adapter.process.setArguments
    monkeypatch.setattr(adapter.process, 'setArguments', lambda values: set_arguments(values+['--self-test']))
    failures = []
    adapter.failed.disconnect()
    adapter.failed.connect(failures.append)
    adapter.capture(dict(project_id=view.scene.project_id, canvas_id='default-canvas', x=0, y=0))
    try:
        qtbot.waitUntil(lambda: bool(failures) or view.scene._item_count == 1, timeout=30000)
        assert not failures, (failures, adapter.diagnostics.decode('utf-8', errors='replace'))
        qtbot.waitUntil(lambda: not getattr(view, '_import_in_progress', False), timeout=10000)
        item = next(item for item in view.scene.items_for_save() if getattr(item, 'TYPE', '') == 'pixmap')
        assert item._image_size == QtCore.QSize(140, 80)
        assert item._canvas_id == 'default-canvas'
        source = QtGui.QImage.fromData(item.original_bytes())
        assert source.size() == QtCore.QSize(140, 80)
        view.undo_stack.undo()
        assert view.scene._item_count == 0
        view.undo_stack.redo()
        assert view.scene._item_count == 1
    finally:
        adapter.close()
