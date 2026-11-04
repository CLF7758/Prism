"""Two real pages: unload dirty A, load B, Save As, edit A during save, reopen."""
from contextlib import closing
import hashlib
import json
import sqlite3

from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
from prism.fileio.partial_save import write_partial_snapshot
from prism.partial_project import PartialProjectSession, read_baseline
from prism.project_open_scope import ProjectOpenScope
from prism.source_store import read_archive
from tests._audit_partial_project import (SOURCE, OUT, file_hash, original_hashes,
                                         attachment_hashes, _AuditItem, _AuditScene)


def test_real_two_page_dirty_unload_save_as_and_reopen(main_window, qtbot):
    target = OUT / 'Partial-multipage-real-20261021.prism'
    report_path = OUT / 'partial-multipage-real-20261021.json'
    report = {'assertions_completed': False, 'source': str(SOURCE), 'target': str(target),
              'scope': 'retained logical objects, two page metadata scopes; not automatic partial UI'}
    try:
        source_hash = file_hash(SOURCE)
        originals = original_hashes(SOURCE, 'sqlar', 'item_id')
        attachments = attachment_hashes(SOURCE)
        scope = ProjectOpenScope(SOURCE, OUT / 'partial-session-catalog.sqlite')
        if not scope.ready:
            assert scope.build(max_seconds=30).complete
        candidates = [(canvas, count) for (canvas, kind), count in scope.counts().items()
                      if kind == 'pixmap' and 0 < count <= 128]
        assert len(candidates) >= 2
        pages = []
        for canvas, count in candidates[:2]:
            summaries = scope.page(canvas, kind='pixmap', limit=128)
            assert len(summaries) == count
            ids = [row['id'] for row in summaries]
            items = [_AuditItem(row) for row in scope.details(ids)]
            for item in items:
                item._prism_source = (str(SOURCE), item.save_id)
            pages.append((ids, items))
        a_ids, a_items = pages[0]
        b_ids, b_items = pages[1]
        session = PartialProjectSession.open(SOURCE, item_ids=a_ids,
            include_boards=False, include_domains=True)
        scene = _AuditScene(a_items)
        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as db:
            scene.material_records = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM material_records ORDER BY id')]
            scene.workspace_nodes = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM workspace_nodes ORDER BY id')]
            scene.attachments = StoredAttachments(sqlite_attachment_loader(SOURCE))
            scene.attachments._meta = {key: (name, mime) for key, name, mime in db.execute(
                'SELECT id,name,mime FROM attachments WHERE board_id=0')}
        scene.material_library_state = {}
        session.attach(scene)
        a_items[0].data['notes'] = 'multipage A before snapshot'
        token = session.prepare_unload(a_items, scene)
        scene._items.clear()
        session.commit_unload(token, scene)
        baseline_b = read_baseline(SOURCE, [('items', key) for key in b_ids])
        scene._items.extend(b_items)
        session.register_loaded(baseline_b, b_items, scene)
        b_items[0].data['notes'] = 'multipage B edit'
        snapshot = session.capture(scene)
        assert {c.key for c in snapshot.changes} == {a_ids[0], b_ids[0]}
        a_items[0].data['notes'] = 'multipage A edited while saving'
        receipt = write_partial_snapshot(snapshot, target)
        session.acknowledge(snapshot, receipt, scene=scene, filename=str(target))
        assert all(item._prism_source[0] == str(target.resolve()) for item in a_items + b_items)
        follow_up = session.capture(scene)
        assert len(follow_up.changes) == 1 and follow_up.changes[0].key == a_ids[0]
        receipt = write_partial_snapshot(follow_up)
        session.acknowledge(follow_up, receipt, scene=scene)
        assert session.capture(scene).changes == ()
        assert hashlib.sha256(read_archive(a_items[0]._prism_source)).hexdigest() == originals[a_ids[0]]
        assert original_hashes(target, 'sqlar', 'item_id') == originals
        assert attachment_hashes(target) == attachments
        assert file_hash(SOURCE) == source_hash
        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as before, \
                closing(sqlite3.connect(target.as_uri()+'?mode=ro', uri=True)) as after:
            original_rows = {row[0]: row for row in before.execute('SELECT * FROM items')}
            saved_rows = {row[0]: row for row in after.execute('SELECT * FROM items')}
            assert original_rows.keys() == saved_rows.keys()
            changed = {key for key in saved_rows if original_rows[key] != saved_rows[key]}
            assert changed == {a_ids[0], b_ids[0]}
            for table in ('workspace_pages', 'workspace_nodes', 'material_records',
                          'board_notes', 'board_mindmap', 'prism_metadata'):
                assert before.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall() == \
                       after.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall(), table
            assert after.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        window = main_window
        window.view.open_from_file(str(target))
        qtbot.waitUntil(lambda: not window.view._project_opening and
                       not window.view.worker.isRunning(), timeout=60000)
        reopened = {item.save_id: item for item in window.view.scene.iter_assets()}
        assert reopened[a_ids[0]].notes == a_items[0].data['notes']
        assert reopened[b_ids[0]].notes == b_items[0].data['notes']
        window.view.undo_stack.setClean()
        report.update(assertions_completed=True, a_rows=len(a_ids), b_rows=len(b_ids),
            stored_items=len(saved_rows), changed_item_ids=sorted(changed),
            original_entries=len(originals), attachment_entries=len(attachments), source_sha256=source_hash)
    finally:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
