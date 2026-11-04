"""Folder-first upgrade and true installation-terminal contract."""
from dataclasses import FrozenInstanceError
from pathlib import Path
import time

import pytest
from PyQt6 import QtTest, QtWidgets

from prism.actions import storage_workflow
from prism.opening_lifecycle import OpenOperation


@pytest.mark.parametrize('case', ['accept', 'cancel_prompt', 'cancel_folder', 'collision'])
def test_upgrade_chooses_folder_without_overwriting_source(qapp, tmp_path, monkeypatch, case):
    widget = QtWidgets.QWidget()
    source = tmp_path / 'old.prism'
    original = b'untouched-source'
    source.write_bytes(original)
    folder = tmp_path / 'chosen'
    folder.mkdir()
    target = folder / 'old-新版.prism'
    if case == 'collision':
        target.write_bytes(b'existing-output')
    monkeypatch.setattr(QtWidgets.QMessageBox, 'question', lambda *a, **kw:
        QtWidgets.QMessageBox.StandardButton.Cancel if case == 'cancel_prompt'
        else QtWidgets.QMessageBox.StandardButton.Ok)
    monkeypatch.setattr(QtWidgets.QMessageBox, 'warning', lambda *a, **kw: None)
    selected = []
    def choose(*args):
        selected.append(args)
        return '' if case == 'cancel_folder' else str(folder)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory', choose)
    def forbidden(*args, **kwargs):
        pytest.fail('Upgrade must select a folder, not a save-file dialog')
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', forbidden)
    calls = []
    from prism import upgrade_preview
    class Session:
        def __init__(self, view, src, dst, **kw):
            calls.append((view, src, Path(dst), kw))
        def start(self):
            calls.append('started')
    monkeypatch.setattr(upgrade_preview, 'UpgradePreview', Session)
    try:
        accepted = storage_workflow.upgrade_on_open(widget, source, readonly=True)
        assert accepted is (case == 'accept')
        if case == 'accept':
            assert calls == [(widget, source.resolve(), target, {'readonly': True}), 'started']
        else:
            assert calls == []
        assert source.read_bytes() == original
        if case == 'collision':
            assert target.read_bytes() == b'existing-output'
    finally:
        widget.close()


@pytest.mark.parametrize('outcome', ['ready', 'cancelled', 'failed', 'baseline_failed'])
def test_gui_open_terminal_is_single_frozen_epoch_bound_event(main_window, monkeypatch, outcome):
    view = main_window.view
    monkeypatch.setattr(view.category_panel, 'update_counts', lambda: None)
    epoch = getattr(view, '_trace_open_epoch', 0) + 1
    view._trace_open_epoch = epoch
    operation = OpenOperation('example.prism', view.scene, epoch, None, time.perf_counter(), 0, 0,
                              project_id='project-one')
    view._opening_operation = operation
    view.filename = operation.filename
    view._project_opening = True
    observed = QtTest.QSignalSpy(view.project_open_finished)
    view._end_open(operation, outcome)
    view._end_open(operation, outcome)
    assert len(observed) == 1
    event = observed[0][0]
    assert (event.filename, event.epoch, event.project_id, event.outcome) == (
        operation.filename, epoch,
        view.scene.project_id if outcome in ('ready', 'baseline_failed') else 'project-one', outcome)
    assert not view._project_opening
    with pytest.raises(FrozenInstanceError):
        event.epoch = epoch + 1
    stale = OpenOperation('obsolete.prism', view.scene, epoch - 1, None, time.perf_counter(), 0, 0)
    view._end_open(stale, 'ready')
    assert len(observed) == 1
