"""Real offscreen GUI saves exercise cooperative capture and private preparation."""
import json
import threading
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore

from prism import fileio
from prism.fileio import sql
from prism.fileio.save_preparation import CapturedItem, SavePreparation
from prism.fileio.save_snapshot import BoardSaveSnapshot
from prism.items import PrismTextItem
from prism.partial_project import PartialProjectSession, _assets

from tests.test_partial_project_gui import CANVAS_A, open_canvas_a, project, rows


TIMEOUT = 20000


def large_body():
    # Lots of nested elements, not one immutable string shared by sliced_copy.
    return {'root': {'title': 'large body', 'children': [
        {'id': index, 'body': {'paragraphs': [
            {'text': str(index), 'marks': ['bold', 'reviewed']},
            {'text': 'second paragraph', 'attrs': {'indent': index % 3}}]}}
        for index in range(6000)]}}


@pytest.fixture
def save_project(project, monkeypatch, qtbot):
    assert QtCore.QCoreApplication.instance().platformName() == 'offscreen'
    # No document panel/WebEngine is opened. Keep editor flushing from replacing
    # the plain board payload this test supplies. closeEvent calls it without a panel.
    monkeypatch.setattr(project.window, '_store_current_page', lambda _panel=None: True)
    project.scene.workspace_pages = []
    project.releases = []
    yield project
    for release in project.releases:
        release.set()
    worker = getattr(project.view, 'worker', None)
    if worker is not None and worker.isRunning():
        worker.on_canceled()
        assert worker.wait(TIMEOUT), 'test must not leave a running save thread'
    if getattr(project.view, '_save_operation', None) is not None:
        qtbot.waitUntil(lambda: project.view._save_operation is None, timeout=TIMEOUT)


def gate(project, monkeypatch, owner, name):
    entered, release = threading.Event(), threading.Event()
    project.releases.append(release)
    real = getattr(owner, name)

    def paused(*args, **kwargs):
        entered.set()
        if not release.wait(TIMEOUT / 1000):
            raise RuntimeError('test did not release background preparation')
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, paused)
    return entered, release


def begin(project, target, *, create_new=True):
    view = project.view
    before = getattr(view, 'worker', None)
    view.do_save(str(target), create_new=create_new, silent=True)
    operation = view._save_operation
    assert isinstance(operation, SavePreparation)
    assert view.worker is not before
    assert isinstance(view.worker, fileio.ThreadedIO)
    assert view.worker.func.__self__ is operation
    assert view.worker.func.__func__ is SavePreparation.run
    assert operation.worker is view.worker
    assert view._save_preparing
    assert not operation.ready.is_set(), 'do_save must return before GUI capture'
    return operation, view.worker


def settled(project, worker, qtbot):
    qtbot.waitUntil(lambda: not worker.isRunning(), timeout=TIMEOUT)
    assert worker.wait(TIMEOUT)
    qtbot.waitUntil(lambda: project.view._save_operation is None, timeout=TIMEOUT)
    assert project.view._save_preparing is False
    assert getattr(project.view, '_save_partial_snapshot', None) is None
    assert getattr(project.view, '_save_board_snapshot', None) is None


def mark_deferred(project):
    scene = project.scene
    scene.note_html = ''
    scene.mindmap_tree = None
    scene._deferred_board_extras = {'note_html', 'mindmap_tree', 'workspace_pages'}
    scene._deferred_page_ids = {'notes'}
    scene._board_extras_source = str(project.source)


def assert_refused(project, target, original, source_bytes, filename, session=None):
    assert target.read_bytes() == original
    assert project.source.read_bytes() == source_bytes
    assert project.view.filename == filename
    assert project.view.is_dirty()
    assert project.view._paged_save_refused[1]
    if session is not None:
        assert session.invalid is None
        assert session.acknowledged is None
        assert session.revision == 0


@pytest.mark.parametrize('partial', [False, True], ids=['full', 'partial'])
def test_large_nested_body_keeps_gui_timer_alive_and_capture_budgeted(
        save_project, qtbot, monkeypatch, partial):
    p = save_project
    session = open_canvas_a(p) if partial else None
    body = large_body()
    p.scene.mindmap_tree = body
    p.view.mark_content_dirty()
    yields, slices, ticks = [], [], []
    gui_thread = threading.get_ident()
    real_capture = BoardSaveSnapshot.capture_steps.__func__
    real_drain = SavePreparation._drain

    def capture_steps(cls, scene, **kwargs):
        steps = real_capture(cls, scene, **kwargs)
        while True:
            try:
                checkpoint = next(steps)
            except StopIteration as finished:
                return finished.value
            yields.append(threading.get_ident())
            yield checkpoint

    def drain(operation):
        before = len(yields)
        real_drain(operation)
        slices.append(len(yields) - before)

    monkeypatch.setattr(BoardSaveSnapshot, 'capture_steps', classmethod(capture_steps))
    monkeypatch.setattr(SavePreparation, '_drain', drain)
    heartbeat = QtCore.QTimer(p.view)
    heartbeat.setInterval(0)
    heartbeat.timeout.connect(lambda: ticks.append(
        not getattr(p.view, '_save_operation', None).ready.is_set()
        if getattr(p.view, '_save_operation', None) is not None else False))
    target = p.source.parent / 'large-body.prism'
    heartbeat.start()
    try:
        operation, worker = begin(p, target)
        settled(p, worker, qtbot)
    finally:
        heartbeat.stop()
    assert operation.error is None
    # The time budget is the real limit; the step count only has to be big
    # enough that it does not become the binding constraint (a 100k-material
    # project needs ~900 000 steps, and paying one event-loop round trip
    # per 256 of them cost 7.77 s of wall clock for 451 ms of copying).
    assert SavePreparation.SLICE_STEPS >= 4096
    assert SavePreparation.SLICE_SECONDS == .004
    assert len(yields) > 50000
    assert set(yields) == {gui_thread}
    assert len([count for count in slices if count]) > 10
    assert max(slices) <= SavePreparation.SLICE_STEPS
    assert sum(ticks) >= 5, 'the GUI heartbeat must run between capture slices'
    assert operation.board.state.mindmap_tree == body
    assert operation.board.state.mindmap_tree is not body
    assert operation.board.state.mindmap_tree['root']['children'] is not body['root']['children']
    assert json.loads(rows(target, 'board_mindmap')[0][1]) == body
    assert not p.view.is_dirty()
    if session is not None:
        assert session.acknowledged is not None and session.invalid is None


def test_gui_capture_slice_stops_at_four_milliseconds(
        save_project, qtbot, monkeypatch):
    from types import SimpleNamespace
    import prism.fileio.save_preparation as preparation_module

    p = save_project
    p.scene.mindmap_tree = {'children': [{'title': str(n)} for n in range(1000)]}
    writer = Mock(wraps=fileio.save_prism)
    monkeypatch.setattr(fileio, 'save_prism', writer)
    target = p.source.parent / 'budget-cancelled.prism'
    operation, worker = begin(p, target)
    real_steps = operation._steps
    checkpoints = []

    def counted_steps():
        while True:
            checkpoint = next(real_steps)
            checkpoints.append(checkpoint)
            yield checkpoint

    operation.timer.stop()
    operation._steps = counted_steps()
    clock = iter((0, .001, .002, .003, .004))
    # Replace only this module's clock reference, not the global time module
    # used by pytest, Qt polling, tracing or the concurrent worker.
    with monkeypatch.context() as local:
        local.setattr(preparation_module, 'time', SimpleNamespace(
            perf_counter=lambda: next(clock)))
        operation._drain()
    assert operation.error is None
    assert len(checkpoints) == 4, '4 ms must yield before the 256-step limit'
    assert not operation.ready.is_set() and operation.timer.isActive()
    worker.on_canceled()
    settled(p, worker, qtbot)
    writer.assert_not_called()
    assert not target.exists()


def test_partial_capture_is_worker_owned_and_receipt_uses_canonical_gui_items(
        save_project, qtbot, monkeypatch):
    p = save_project
    session = open_canvas_a(p)
    fresh = PrismTextItem('new captured text')
    fresh.canvas_id = 'A'
    p.scene.addItem(fresh)
    fresh.save_id = None
    p.view.mark_content_dirty()
    gui_thread = threading.get_ident()
    captures, writes, registrations = [], [], []
    real_capture = PartialProjectSession.capture
    real_write = fileio.save_prism
    real_accept = SavePreparation._accept

    def capture(private, state, **kwargs):
        items = tuple(_assets(state))
        captures.append((threading.get_ident(), private, state, items))
        assert threading.get_ident() != gui_thread
        assert private is not session and state is not p.scene
        assert items and all(isinstance(item, CapturedItem) for item in items)
        assert not isinstance(state, QtCore.QObject)
        assert all(not isinstance(item, QtCore.QObject) for item in items)
        return real_capture(private, state, **kwargs)

    def write(filename, scene, **kwargs):
        snapshot = kwargs['partial_snapshot']
        writes.append((threading.get_ident(), scene, snapshot))
        assert all(not isinstance(new.item, QtCore.QObject) for new in snapshot.new_items)
        return real_write(filename, scene, **kwargs)

    def accept(operation, owner):
        real_accept(operation, owner)
        snapshot = getattr(operation.view, '_save_partial_snapshot', None)
        registrations.append((threading.get_ident(), snapshot))

    monkeypatch.setattr(PartialProjectSession, 'capture', capture)
    monkeypatch.setattr(fileio, 'save_prism', write)
    monkeypatch.setattr(SavePreparation, '_accept', accept)
    target = p.source.parent / 'private-items.prism'
    operation, worker = begin(p, target)
    settled(p, worker, qtbot)
    assert operation.error is None
    assert len(captures) == len(writes) == len(registrations) == 1
    assert captures[0][0] == writes[0][0] != gui_thread
    assert writes[0][1] is operation.board.state
    assert captures[0][1] is operation.private_session
    assert registrations[0][0] == gui_thread
    assert len(registrations[0][1].new_items) == 1
    assert registrations[0][1].new_items[0].item is fresh
    assert writes[0][2].new_items[0].item is not fresh
    assert fresh.save_id is not None
    assert session.acknowledged is not None and session.invalid is None
    assert fresh.save_id in {row[0] for row in rows(target, 'items')}


def test_deferred_background_read_only_mutates_private_board_and_leaves_pages_lazy(
        save_project, qtbot, monkeypatch):
    p = save_project
    import sqlite3
    with sqlite3.connect(p.source) as db:
        db.execute('UPDATE board_notes SET html=?', ('<p>lazy note</p>',))
        db.execute('UPDATE board_mindmap SET tree=?', ('{"title":"lazy tree"}',))
    p.scene.workspace_pages = [{'id': 'notes', 'kind': 'document', 'title': 'notes'}]
    session = PartialProjectSession.open(p.source, item_ids=CANVAS_A,
                                        page_ids=('notes',), include_boards=True)
    session.attach(p.scene)
    mark_deferred(p)
    flags, pages = p.scene._deferred_board_extras, p.scene.workspace_pages
    source_bytes = p.source.read_bytes()
    before_pages = rows(p.source, 'workspace_pages')
    gui_thread = threading.get_ident()
    calls = []
    real_load = sql.load_deferred_board_extras
    materialize = Mock(side_effect=AssertionError('partial preparation loaded page bodies'))

    def load(state, names=None, **kwargs):
        calls.append((threading.get_ident(), state, names))
        assert threading.get_ident() != gui_thread and state is not p.scene
        assert state._deferred_board_extras is not flags
        assert names == ('note_html', 'mindmap_tree')
        return real_load(state, names=names, **kwargs)

    monkeypatch.setattr(sql, 'load_deferred_board_extras', load)
    monkeypatch.setattr(sql, '_materialize_workspace_pages', materialize)
    p.view.mark_content_dirty()
    target = p.source.parent / 'lazy-copy.prism'
    operation, worker = begin(p, target)
    settled(p, worker, qtbot)
    assert operation.error is None
    assert len(calls) == 1 and calls[0][1] is operation.board.state
    materialize.assert_not_called()
    assert p.scene.note_html == '' and p.scene.mindmap_tree is None
    assert p.scene._deferred_board_extras is flags
    assert flags == {'note_html', 'mindmap_tree', 'workspace_pages'}
    assert p.scene._deferred_page_ids == {'notes'}
    assert p.scene.workspace_pages is pages and 'content' not in pages[0]
    assert operation.board.state.note_html == '<p>lazy note</p>'
    assert operation.board.state.mindmap_tree == {'title': 'lazy tree'}
    assert operation.board.state._deferred_board_extras == {'workspace_pages'}
    assert 'content' not in operation.board.state.workspace_pages[0]
    assert p.scene._board_extras_source == str(target)
    assert rows(target, 'workspace_pages') == before_pages
    assert rows(target, 'board_notes') == rows(p.source, 'board_notes')
    assert rows(target, 'board_mindmap') == rows(p.source, 'board_mindmap')
    assert p.source.read_bytes() == source_bytes
    assert session.acknowledged is not None and session.invalid is None


@pytest.mark.parametrize('partial', [False, True], ids=['full', 'partial'])
def test_mark_content_dirty_between_gui_capture_slices_refuses_without_writing(
        save_project, qtbot, monkeypatch, partial):
    p = save_project
    session = open_canvas_a(p) if partial else None
    p.scene.mindmap_tree = large_body()
    mark = Mock(wraps=fileio.save_prism)
    monkeypatch.setattr(fileio, 'save_prism', mark)
    target = p.source.parent / 'capture-refused.prism'
    target.write_bytes(b'existing target\x00\xff')
    original, source_bytes, filename = target.read_bytes(), p.source.read_bytes(), p.view.filename
    p.view.mark_content_dirty()
    operation, worker = begin(p, target)
    # Advance one actual GUI slice, then edit while the remaining slices are
    # still queued. This exercises revision rejection, not just preflight.
    operation._drain()
    assert not operation.ready.is_set()
    p.view.mark_content_dirty()
    settled(p, worker, qtbot)
    mark.assert_not_called()
    assert 'changed during save capture' in str(operation.error)
    assert_refused(p, target, original, source_bytes, filename, session)


@pytest.mark.parametrize('stage', ['capture', 'background'])
def test_cancel_preparation_keeps_deferred_flags_dirty_and_existing_target(
        save_project, qtbot, monkeypatch, stage):
    p = save_project
    session = open_canvas_a(p)
    mark_deferred(p)
    flags = p.scene._deferred_board_extras
    target = p.source.parent / 'cancelled.prism'
    target.write_bytes(b'never replace on cancellation\x00\xff')
    original, source_bytes, filename = target.read_bytes(), p.source.read_bytes(), p.view.filename
    writer = Mock(wraps=fileio.save_prism)
    monkeypatch.setattr(fileio, 'save_prism', writer)
    if stage == 'background':
        entered, release = gate(p, monkeypatch, sql, 'load_deferred_board_extras')
    p.view.mark_content_dirty()
    operation, worker = begin(p, target)
    try:
        if stage == 'background':
            qtbot.waitUntil(entered.is_set, timeout=TIMEOUT)
            assert operation.ready.is_set() and not operation.accepted.is_set()
        worker.on_canceled()
    finally:
        if stage == 'background':
            release.set()
    settled(p, worker, qtbot)
    writer.assert_not_called()
    assert 'cancelled' in str(operation.error).lower()
    assert p.scene._deferred_board_extras is flags
    assert flags == {'note_html', 'mindmap_tree', 'workspace_pages'}
    assert p.scene._deferred_page_ids == {'notes'}
    assert p.scene._board_extras_source == str(p.source)
    assert_refused(p, target, original, source_bytes, filename, session)


@pytest.mark.parametrize('partial', [False, True], ids=['full', 'partial'])
def test_deferred_parse_failure_keeps_original_flags_dirty_and_target_bytes(
        save_project, qtbot, monkeypatch, partial):
    p = save_project
    import sqlite3
    with sqlite3.connect(p.source) as db:
        db.execute('UPDATE board_notes SET html=?', ('<p>read before failure</p>',))
        db.execute('UPDATE board_mindmap SET tree=?', ('not valid JSON',))
    session = open_canvas_a(p) if partial else None
    mark_deferred(p)
    flags = p.scene._deferred_board_extras
    target = p.source.parent / 'parse-failed.prism'
    target.write_bytes(b'original destination bytes\x00\xff')
    original, source_bytes, filename = target.read_bytes(), p.source.read_bytes(), p.view.filename
    writer = Mock(wraps=fileio.save_prism)
    monkeypatch.setattr(fileio, 'save_prism', writer)
    p.view.mark_content_dirty()
    operation, worker = begin(p, target)
    settled(p, worker, qtbot)
    assert isinstance(operation.error, json.JSONDecodeError)
    writer.assert_not_called()
    assert operation.board.state.note_html == '<p>read before failure</p>'
    assert p.scene.note_html == '' and p.scene.mindmap_tree is None
    assert p.scene._deferred_board_extras is flags
    assert flags == {'note_html', 'mindmap_tree', 'workspace_pages'}
    assert p.scene._deferred_page_ids == {'notes'}
    assert p.scene._board_extras_source == str(p.source)
    assert_refused(p, target, original, source_bytes, filename, session)


@pytest.mark.parametrize('partial', [False, True], ids=['full', 'partial'])
def test_edit_during_background_preparation_saves_old_snapshot_and_stays_dirty(
        save_project, qtbot, monkeypatch, partial):
    p = save_project
    session = open_canvas_a(p) if partial else None
    item = next(item for item in p.items if item.save_id == 2)
    p.scene.note_html = '<p>captured note</p>'
    p.scene.mindmap_tree = {'title': 'captured', 'children': [{'title': 'old child'}]}
    item.notes = 'captured item'
    p.view.mark_content_dirty()
    entered, release = gate(p, monkeypatch, sql, 'load_deferred_board_extras')
    target = p.source.parent / 'old-revision.prism'
    operation, worker = begin(p, target)
    try:
        qtbot.waitUntil(entered.is_set, timeout=TIMEOUT)
        assert operation.ready.is_set() and not operation.accepted.is_set()
        p.scene.note_html = '<p>newer note</p>'
        p.scene.mindmap_tree['children'][0]['title'] = 'new child'
        if partial:
            item.notes = 'newer item'
        p.view.mark_content_dirty()
    finally:
        release.set()
    settled(p, worker, qtbot)
    assert operation.error is None
    assert rows(target, 'board_notes')[0][2] == '<p>captured note</p>'
    assert json.loads(rows(target, 'board_mindmap')[0][1]) == {
        'title': 'captured', 'children': [{'title': 'old child'}]}
    assert p.scene.note_html == '<p>newer note</p>'
    assert p.scene.mindmap_tree['children'][0]['title'] == 'new child'
    assert p.view.is_dirty() and p.view.filename == str(target)
    if partial:
        saved_item = next(row for row in rows(target, 'items') if row[0] == 2)
        assert json.loads(saved_item[-1])['notes'] == 'captured item'
        assert item.notes == 'newer item'
        assert session.acknowledged is not None and session.invalid is None
        assert session.capture(p.scene).changes, 'later edits must survive receipt application'


@pytest.mark.parametrize('partial', [False, True], ids=['full', 'partial'])
def test_project_switch_and_second_save_refused_from_capture_through_reentrant_ack(
        save_project, qtbot, monkeypatch, partial):
    p = save_project
    session = open_canvas_a(p) if partial else None
    p.scene.note_html = '<p>one project throughout</p>'
    p.view.mark_content_dirty()
    target = p.source.parent / 'protected.prism'
    other = p.source.parent / 'never-opened.prism'
    overlap = p.source.parent / 'overlapping.prism'
    identity = p.scene.project_id
    phases = []
    real_accept = SavePreparation._accept
    real_finish = p.view.on_saving_finished
    entered_prepare, release_prepare = gate(p, monkeypatch, sql, 'load_deferred_board_extras')
    entered_write, release_write = gate(p, monkeypatch, fileio, 'save_prism')

    def observe(phase):
        view = p.view
        worker = view.worker
        operation = view._save_operation
        allowed = view._allow_project_replacement()
        phases.append((phase, allowed, view._save_preparing, operation, worker))
        # Only try real entry points once the guard says they are protected.
        # A broken guard must fail the assertion, not launch an unrelated read.
        if not allowed:
            assert view.open_from_file(str(other)) is False
            assert view.get_confirmation_unsaved_changes('discard?') is False
            view.on_action_new_scene()
            view.do_save(str(overlap), create_new=True, silent=True)
            assert view.scene is p.scene and view.scene.project_id == identity
            assert view.worker is worker and view._save_operation is operation

    def accept(operation, owner):
        observe('prepared_before_accept')
        real_accept(operation, owner)
        observe('accepted_before_write')

    def finishing(filename, errors, **kwargs):
        observe('finish_before_ack')
        real_finish(filename, errors, **kwargs)
        # on_saving_finished has already cleared the snapshots here; only the
        # operation preparation flag can still protect a completed worker.
        QtCore.QCoreApplication.processEvents()
        observe('finish_after_ack')

    if partial:
        real_ack = session.acknowledge

        def acknowledge(*args, **kwargs):
            assert not p.view.worker.isRunning()
            QtCore.QCoreApplication.processEvents()
            observe('inside_ack')
            return real_ack(*args, **kwargs)

        monkeypatch.setattr(session, 'acknowledge', acknowledge)
    else:
        real_ack = BoardSaveSnapshot.acknowledge

        def acknowledge(snapshot, *args, **kwargs):
            assert not p.view.worker.isRunning()
            QtCore.QCoreApplication.processEvents()
            observe('inside_ack')
            return real_ack(snapshot, *args, **kwargs)

        monkeypatch.setattr(BoardSaveSnapshot, 'acknowledge', acknowledge)
    monkeypatch.setattr(SavePreparation, '_accept', accept)
    monkeypatch.setattr(p.view, 'on_saving_finished', finishing)
    operation, worker = begin(p, target)
    try:
        observe('capturing')
        qtbot.waitUntil(entered_prepare.is_set, timeout=TIMEOUT)
        observe('background_preparing')
        release_prepare.set()
        qtbot.waitUntil(entered_write.is_set, timeout=TIMEOUT)
        assert operation.accepted.is_set()
        observe('writing')
        release_write.set()
        # Blocking wait on the GUI thread deliberately leaves the finished
        # signal queued, reproducing worker-complete/before-GUI-receipt.
        assert worker.wait(TIMEOUT)
        assert not worker.isRunning()
        observe('finished_before_queued_ack')
    finally:
        release_prepare.set()
        release_write.set()
    settled(p, worker, qtbot)
    assert operation.error is None
    assert [phase for phase, *_rest in phases] == [
        'capturing', 'background_preparing', 'prepared_before_accept',
        'accepted_before_write', 'writing', 'finished_before_queued_ack',
        'finish_before_ack', 'inside_ack', 'finish_after_ack']
    assert all(not allowed and preparing and owner is operation and thread is worker
               for _phase, allowed, preparing, owner, thread in phases)
    assert not overlap.exists() and not other.exists()
    assert p.view._allow_project_replacement() is True
    assert p.scene.project_id == identity and p.view.filename == str(target)
    assert not p.view.is_dirty()
    if session is not None:
        assert session.acknowledged is not None and session.invalid is None


def test_a_discarded_write_is_only_a_cancel_when_the_writer_says_so():
    """Nothing written is a disk fact; "the user cancelled" is another one.

    The terminal state decides what the user is told: a cancel gets a quiet
    status line, a failure gets the real reason in a dialog.  A write that
    died on an error must therefore not be reported as a cancel.
    """
    from prism.fileio.save_preparation import (SavePreparation, TERMINAL_CANCELLED,
                                               TERMINAL_FAILED)
    assert SavePreparation._terminal_for(
        'not_started', 'not_applicable', ['boom'], cancelled=False) == TERMINAL_FAILED
    assert SavePreparation._terminal_for(
        'not_started', 'not_applicable', ['boom'], cancelled=True) == TERMINAL_CANCELLED
    assert SavePreparation._terminal_for(
        'rolled_back', 'not_applicable', ['boom'], cancelled=False) == TERMINAL_FAILED
    assert SavePreparation._terminal_for(
        'rolled_back', 'not_applicable', ['boom'], cancelled=True) == TERMINAL_CANCELLED
