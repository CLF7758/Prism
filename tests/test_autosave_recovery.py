import hashlib
import json
import os
import sqlite3

import pytest

from prism.fileio.save_preparation import AutosavePolicy, AutosaveSchedule
from prism.recovery import RecoveryStore, project_key, recover_copy


def test_idle_and_continuous_edit_deadlines():
    schedule = AutosaveSchedule()
    assert not schedule.observe(1, True, 0)
    assert not schedule.observe(1, True, 29)
    assert schedule.observe(1, True, 30)
    schedule.reset()
    for second in range(0, 120, 5):
        assert not schedule.observe(second, True, second)
    assert schedule.observe(120, True, 120)


def test_retry_and_new_revision_do_not_accumulate_requests():
    schedule = AutosaveSchedule()
    schedule.observe(1, True, 0)
    schedule.attempted(30, 60)
    assert not schedule.observe(2, True, 89)
    assert not schedule.observe(2, True, 90)  # edited again one second ago
    assert schedule.observe(2, True, 119)
    assert not schedule.observe(2, False, 120)
    assert not schedule.observe(3, True, 121)


def test_first_failure_is_visible_once():
    policy = AutosavePolicy()
    policy.record_failure('disk full')
    assert policy.should_announce()
    assert not policy.should_announce()
    assert policy.record_success() == 1


def test_old_save_completion_cannot_change_new_project(main_window, tmp_path):
    from tests.test_catalog_aggregate_consistency import FakeSaveWorker
    view = main_window.view
    view.filename = str(tmp_path/'one.prism')
    view.mark_content_dirty()
    worker = FakeSaveWorker()
    def save(*args, **kwargs):
        view.worker = worker
    view.do_save = save
    assert view.autosave_tick() == 'started'
    view._autosave_project_changed()
    before = main_window.statusBar().currentMessage()
    worker.finished.emit(str(view.filename), ['old failure'])
    assert view.autosave_policy.failures == 0
    assert main_window.statusBar().currentMessage() == before


def test_normal_exit_does_not_offer_discarded_edits_but_keeps_drafts(tmp_path):
    store = RecoveryStore(tmp_path)
    store.write(dict(project_id='one', filename='a.prism'), session='closed')
    store.write(dict(project_id='two', filename='draft.prismproj', full_project=True), session='closed')
    store.finish_session('closed')
    records = store.records()
    assert next(r for r in records if r['payload']['project_id'] == 'one')['status'] == 'discarded'
    assert next(r for r in records if r['payload']['project_id'] == 'two')['status'] == 'pending'


def test_projects_and_unsaved_records_are_isolated(tmp_path):
    store = RecoveryStore(tmp_path/'recovery')
    for filename in ('a.prism', 'b.prism', None):
        store.write(dict(project_id='same-id', filename=filename), session='one')
    assert len(store.records()) == 3
    store.resolve(project_key('same-id', 'a.prism'), session='one')
    assert sum(value['status'] == 'pending' for value in store.records()) == 2


def test_checkpoint_history_is_bounded_but_other_sessions_survive(tmp_path):
    store = RecoveryStore(tmp_path)
    payload = dict(project_id='one', filename='a.prism', note_html='old crash')
    store.write(payload, session='crashed')
    for i in range(5):
        store.write(dict(payload, note_html=str(i)), session='current')
    assert len(store.records()) == 4  # three current checkpoints + earlier crash
    store.resolve(project_key('one', 'a.prism'), session='current')
    store.cleanup(now=10**12)
    assert len(store.records()) == 1
    assert store.records()[0]['payload']['note_html'] == 'old crash'


def test_capacity_failure_keeps_pending_evidence(tmp_path, monkeypatch):
    from prism import recovery
    store = RecoveryStore(tmp_path)
    payload = dict(project_id='one', filename='a.prism')
    path = store.write(payload, session='old')
    original = open(path, 'rb').read()
    monkeypatch.setattr(recovery, 'MAX_TOTAL_BYTES', len(original))
    with pytest.raises(OSError, match='256 MiB'):
        store.write(dict(payload, filename='b.prism'), session='new')
    assert open(path, 'rb').read() == original


def test_attachment_bytes_roundtrip_without_logging_content(tmp_path):
    store = RecoveryStore(tmp_path)
    payload = dict(project_id='one', filename='a.prism',
                   attachments=[(1, 'new.png', 'image/png', b'\x00\xfforiginal')])
    store.write(payload, session='one')
    assert store.records()[0]['payload']['attachments'][0][3] == b'\x00\xfforiginal'


def test_recover_copy_keeps_original_and_restores_page_attachment(view, tmp_path):
    from prism.fileio.sql import SQLiteIO
    from prism.fileio.attachments_store import sqlite_attachment_loader
    source = tmp_path/'original.prism'
    view.scene.workspace_pages = [dict(id='doc', kind='document', title='Document', content='before')]
    io = SQLiteIO(str(source), view.scene, create_new=True)
    io.write(); io._close_connection()
    before = hashlib.sha256(source.read_bytes()).digest()
    record = dict(payload=dict(project_id=view.scene.project_id, filename=str(source),
        workspace_pages=[dict(id='doc', kind='document', title='Recovered', content='after')],
        attachments=[(7, 'new.png', 'image/png', b'new attachment')]))
    output = recover_copy(record, tmp_path/'recovered.prismproj')
    assert hashlib.sha256(source.read_bytes()).digest() == before
    with sqlite3.connect(output) as db:
        title, data = db.execute("SELECT title,data FROM workspace_pages WHERE id='doc'").fetchone()
        assert title == 'Recovered'
        assert json.loads(data)['content'] == 'after'
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
    assert sqlite_attachment_loader(output)(7) == b'new attachment'


def test_recovery_refuses_wrong_project_identity(view, tmp_path):
    from prism.fileio.sql import SQLiteIO
    source = tmp_path/'original.prism'
    io = SQLiteIO(str(source), view.scene, create_new=True)
    io.write(); io._close_connection()
    before = source.read_bytes()
    with pytest.raises(ValueError, match='身份'):
        recover_copy(dict(payload=dict(project_id='wrong', filename=str(source))), tmp_path/'copy.prismproj')
    assert source.read_bytes() == before


def test_untitled_autosave_uses_complete_existing_project_writer(main_window, qtbot):
    from PyQt6 import QtGui
    from prism.items import PrismTextItem, PrismPixmapItem
    from prism.source_store import read_archive
    from tests.test_partial_project_gui import PNG
    window = main_window
    qtbot.waitUntil(lambda: window._recovery_worker is None, timeout=10000)
    window.view.scene.addItem(PrismTextItem('unsaved canvas text'))
    original = PNG.read_bytes() + b'preserve-exact-original'
    image = PrismPixmapItem(QtGui.QImage.fromData(original))
    image.set_source_blob(original, 'png')
    window.view.scene.addItem(image)
    window.view.mark_content_dirty()
    assert window._perform_autosave() == 'started'
    qtbot.waitUntil(lambda: getattr(window.view, '_save_operation', None) is None, timeout=20000)
    assert window.view.last_save_outcome.confirmed
    assert window._is_recovery_project(window.view.filename)
    assert os.path.isfile(window.view.filename)
    assert read_archive((window.view.filename, image.save_id)) == original
    records = window._recovery_store.records()
    assert any(value['payload'].get('full_project') and value['status'] == 'pending' for value in records)
    with sqlite3.connect(window.view.filename) as db:
        assert db.execute('SELECT COUNT(*) FROM items').fetchone()[0] >= 1
