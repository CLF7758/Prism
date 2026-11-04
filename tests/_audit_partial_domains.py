"""Explicit opt-in real-source audit for complete material/tree metadata saves."""
from contextlib import closing
import json
import sqlite3
import time

from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
from prism.fileio.partial_save import write_partial_snapshot
from prism.partial_project import PartialProjectSession
from tests._audit_partial_project import (SOURCE, OUT, file_hash, original_hashes,
                                         attachment_hashes, _AuditScene)


def test_real_domains_keep_originals_and_reopen(main_window, qtbot):
    target = OUT / 'Partial-domains-real-20261021.prism'
    report_path = OUT / 'partial-domains-real-20261021.json'
    report = {'assertions_completed': False, 'source': str(SOURCE),
              'target': str(target), 'stages': {}}

    def stage(name, call):
        started = time.perf_counter()
        value = call()
        report['stages'][name] = round((time.perf_counter()-started)*1000, 3)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        return value

    try:
        source_hash = stage('source_hash_before', lambda: file_hash(SOURCE))
        originals = stage('original_hashes_before', lambda: original_hashes(SOURCE, 'sqlar', 'item_id'))
        attachments = stage('attachment_hashes_before', lambda: attachment_hashes(SOURCE))
        session = stage('open_complete_metadata_scope', lambda: PartialProjectSession.open(
            SOURCE, include_boards=False, include_domains=True))
        scene = _AuditScene([])
        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as db:
            scene.material_records = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM material_records ORDER BY id')]
            scene.workspace_nodes = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM workspace_nodes ORDER BY id')]
            scene.attachments = StoredAttachments(sqlite_attachment_loader(SOURCE))
            scene.attachments._meta = {key: (name, mime) for key, name, mime in db.execute(
                'SELECT id,name,mime FROM attachments WHERE board_id=0')}
        scene.material_library_state = {}
        assert session.capture(scene).changes == ()
        record = scene.material_records[0]
        record['notes'] = record.get('notes', '') + '\nPartial domain audit marker'
        snapshot = stage('capture_one_material', lambda: session.capture(scene))
        assert len(snapshot.changes) == 1 and snapshot.changes[0].table == 'material_records'
        receipt = stage('save_as', lambda: write_partial_snapshot(snapshot, target))
        session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
        assert session.invalid is None and session.capture(scene).changes == ()
        assert stage('original_hashes_after', lambda: original_hashes(target, 'sqlar', 'item_id')) == originals
        assert stage('attachment_hashes_after', lambda: attachment_hashes(target)) == attachments
        assert stage('source_hash_after', lambda: file_hash(SOURCE)) == source_hash
        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as before, \
                closing(sqlite3.connect(target.as_uri()+'?mode=ro', uri=True)) as after:
            for table in ('items', 'workspace_nodes', 'workspace_pages', 'prism_metadata',
                          'board_notes', 'board_mindmap'):
                assert before.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() == \
                       after.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall(), table
            stored = after.execute('SELECT payload FROM material_records WHERE id=?',
                                   (record['id'],)).fetchone()[0]
            assert json.loads(stored)['notes'] == record['notes']
            assert after.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        report.update(materials=len(scene.material_records), nodes=len(scene.workspace_nodes),
                      original_entries=len(originals), attachment_entries=len(attachments),
                      source_sha256=source_hash)
        window = main_window
        window.view.open_from_file(str(target))
        qtbot.waitUntil(lambda: not window.view._project_opening and
                       not window.view.worker.isRunning(), timeout=60000)
        reopened = next(row for row in window.view.scene.material_records if row['id'] == record['id'])
        assert reopened['notes'] == record['notes']
        window.view.undo_stack.setClean()
        report['assertions_completed'] = True
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
