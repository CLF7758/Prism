"""Explicit real-project partial-save proof; never modifies the source fixture."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import queue
import sqlite3
import time

from prism.fileio import save_prism
from prism.fileio.partial_save import RowPatch, read_partial_records, capture_partial_save
from prism.partial_project import PartialProjectSession
from prism.project_catalog_index import ProjectCatalogIndex
from prism.project_open_scope import ProjectOpenScope


OUT = Path('artifacts/full-audit').resolve()
SOURCE = OUT/'Test-project-audit.prism'
TARGET = OUT/'Partial-save-real-roundtrip-20261021.prism'
SESSION_TARGET = OUT/'Partial-save-contract-20261021.prism'
SESSION_REPORT = OUT/'partial-save-contract-20261021.json'


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(4*1024*1024), b''):
            digest.update(block)
    return digest.hexdigest()


def original_hashes(path, table, key):
    result = {}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as connection:
        for rowid, identity, size in connection.execute(f'SELECT rowid,{key},length(data) FROM {table}'):
            if size is None:
                result[identity] = None
                continue
            digest = hashlib.sha256()
            with connection.blobopen(table, 'data', rowid, readonly=True) as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
            result[identity] = digest.hexdigest()
    return result


def attachment_hashes(path):
    result = {}
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True)) as connection:
        for rowid, identity, size in connection.execute('SELECT rowid,id,length(blob) FROM attachments'):
            if size is None:
                result[identity] = None
                continue
            digest = hashlib.sha256()
            with connection.blobopen('attachments', 'blob', rowid, readonly=True) as stream:
                for block in iter(lambda: stream.read(1024*1024), b''):
                    digest.update(block)
            result[identity] = digest.hexdigest()
    return result


def test_partial_loaded_page_preserves_real_project_and_reopens(main_window, qtbot):
    assert SOURCE.exists()
    assert main_window.view.scene is not None
    report = dict(assertions_completed=False, source=str(SOURCE), target=str(TARGET),
                  scope='one page baseline and one item edit; source is audit copy', stages={})
    def stage(name, call):
        started = time.perf_counter()
        result = call()
        report['stages'][name] = round((time.perf_counter()-started)*1000, 3)
        return result
    try:
        source_hash = stage('hash_source_before', lambda: file_hash(SOURCE))
        sources = stage('hash_originals_before', lambda: original_hashes(SOURCE, 'sqlar', 'item_id'))
        attachment_hashes_before = stage('hash_attachments_before',
                                         lambda: attachment_hashes(SOURCE))
        catalog = ProjectCatalogIndex(SOURCE, OUT/'partial-real-catalog.sqlite')
        built = stage('build_read_only_catalog', lambda: catalog.build(max_seconds=20))
        assert built.complete
        with closing(sqlite3.connect(catalog.cache.as_uri()+'?mode=ro', uri=True)) as connection:
            scopes = [json.loads(row[0]) for row in connection.execute('SELECT scope FROM counts')]
        canvas_id, kind = next(scope for scope in scopes if scope[1] == 'pixmap')
        page = catalog.page(canvas_id, kind=kind, limit=128)
        baseline = stage('load_one_page_baseline', lambda: read_partial_records(
            SOURCE, [('items', row['id']) for row in page]))
        key = page[0]['id']
        marker = 'Partial-save audit: unloaded records must survive'
        snapshot = capture_partial_save(SOURCE, [RowPatch('items', key,
            json_updates=(('data', {'notes': marker}),))], baseline=baseline)
        receipt = stage('save_as_complete_project', lambda: save_prism(
            str(TARGET), None, create_new=True, partial_snapshot=snapshot))
        assert receipt.revision == snapshot.revision
        assert stage('hash_originals_after', lambda: original_hashes(TARGET, 'sqlar', 'item_id')) == sources
        assert stage('hash_attachments_after', lambda: attachment_hashes(TARGET)) == attachment_hashes_before
        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as before, \
                closing(sqlite3.connect(TARGET.as_uri()+'?mode=ro', uri=True)) as after:
            tables = [row[0] for row in before.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            report['preserved_tables'] = []
            for table in tables:
                if table in ('sqlar', 'attachments', 'items'):
                    continue
                assert before.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() == \
                    after.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall()
                report['preserved_tables'].append(table)
            original_rows = before.execute('SELECT * FROM items ORDER BY id').fetchall()
            saved_rows = after.execute('SELECT * FROM items ORDER BY id').fetchall()
            assert len(saved_rows) == len(original_rows)
            for old, new in zip(original_rows, saved_rows):
                if old[0] != key:
                    assert old == new
                else:
                    assert old[:-1] == new[:-1]
                    old_data, new_data = json.loads(old[-1]), json.loads(new[-1])
                    assert new_data['notes'] == marker
                    old_data['notes'] = marker
                    assert old_data == new_data
            assert after.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        assert stage('hash_source_after', lambda: file_hash(SOURCE)) == source_hash
        window = main_window
        window.resize(1440, 900)
        window.show()
        started = time.perf_counter()
        window.view.open_from_file(str(TARGET))
        qtbot.waitUntil(lambda: not window.view.worker.isRunning() and
                       not window.view._project_opening, timeout=60000)
        assert window.view.filename == str(TARGET)
        assert list(window.view.scene.iter_assets())
        report['stages']['native_complete_reopen'] = round((time.perf_counter()-started)*1000, 3)
        report.update(assertions_completed=True, loaded_page_records=len(page), stored_item_records=len(saved_rows),
                      original_entries=len(sources), attachment_entries=len(attachment_hashes_before),
                      source_sha256=source_hash, saved_revision=receipt.revision,
                      ui_activation='full existing loader; partial UI not enabled')
        window.view.undo_stack.setClean()
    finally:
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT/'partial-real-project-20261021.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


# ── 会话层审计：打开当前页 → 编辑 → 保存 → 重开 ──────────────────────
#
# 上面那条验证的是「显式 RowPatch + 只读基线」的底层写入。这一条验证
# `PartialProjectSession`：它自己从目录缓存拿当前页，自己决定改了什么，
# 自己拿回执。两者分开报告 —— 底层能写不等于会话层判断正确。

class _Point:
    __slots__ = ('_x', '_y')

    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class _AuditItem:
    """The save surface a loaded item exposes; deliberately no Qt widget.

    ``get_extra_save_data`` returns the stored dict verbatim so "unchanged"
    really means unchanged. A real ``PrismPixmapItem`` recomputes overview and
    thumbnail data, which is why the GUI path has its own test in
    ``tests/test_partial_project_gui.py`` instead of being asserted here.
    """

    TYPE = 'pixmap'

    def __init__(self, row):
        self.save_id = row['id']
        self._point = _Point(row['x'] or 0.0, row['y'] or 0.0)
        self._z = row['z'] or 0.0
        self._scale = 1.0 if row['scale'] is None else row['scale']
        self._rotation = row['rotation'] or 0.0
        self._flip = 1 if row['flip'] is None else row['flip']
        self.data = json.loads(row['data']) if row['data'] else {}

    def pos(self):
        return self._point

    def zValue(self):
        return self._z

    def scale(self):
        return self._scale

    def rotation(self):
        return self._rotation

    def flip(self):
        return self._flip

    def get_extra_save_data(self):
        return dict(self.data)


class _AuditScene:
    """Only what the session reads: assets and the (unloaded) board tables."""

    def __init__(self, items):
        self._items = list(items)
        self.workspace_pages = []
        self.note_html = ''
        self.mindmap_tree = None
        self.attachments = {}
        self.items_to_add = queue.Queue()

    def iter_assets(self):
        return iter(self._items)


def test_partial_session_saves_one_page_of_a_real_project(main_window, qtbot):
    """A page-scoped session on the real audit copy: one row changes, nothing else."""
    report = dict(assertions_completed=False, source=str(SOURCE),
                  target=str(SESSION_TARGET), scope='session layer, one canvas page',
                  ui_activation='full existing loader; partial UI not enabled',
                  skipped_reason=None, stages={})
    if not SOURCE.exists():
        report['skipped_reason'] = f'未测：审计副本不存在（{SOURCE}）'
        SESSION_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
        import pytest
        pytest.skip(report['skipped_reason'])

    def stage(name, call):
        started = time.perf_counter()
        result = call()
        report['stages'][name] = round((time.perf_counter()-started)*1000, 3)
        return result

    try:
        source_hash = stage('hash_source_before', lambda: file_hash(SOURCE))
        originals_before = stage('hash_originals_before',
                                 lambda: original_hashes(SOURCE, 'sqlar', 'item_id'))
        attachments_before = stage('hash_attachments_before',
                                   lambda: attachment_hashes(SOURCE))

        scope = ProjectOpenScope(SOURCE, OUT/'partial-session-catalog.sqlite')
        if not scope.ready:
            built = stage('build_catalog', lambda: scope.build(max_seconds=120))
            assert built.complete, '目录缓存没有建完，不能用它选页面'
        counts = stage('read_counts', scope.counts)
        assert counts, '目录统计应当可用（不可用时必须是 None，而不是空字典）'
        report['catalog_total'] = scope.total()
        report['counted_scopes'] = len(counts)

        pixmap_scopes = sorted(((count, canvas) for (canvas, kind), count in counts.items()
                                if kind == 'pixmap' and 1 <= count <= 128),
                               reverse=True)
        assert pixmap_scopes, '没有大小合适（≤128 条）的画布可用'
        page_count, canvas_id = pixmap_scopes[0]
        page = stage('read_page', lambda: scope.page(canvas_id, kind='pixmap', limit=128))
        ids = [row['id'] for row in page]
        assert len(ids) == page_count == len(set(ids))
        rows = stage('read_details', lambda: scope.details(ids))
        items = [_AuditItem(row) for row in rows]
        report.update(canvas_id=canvas_id, loaded_page_records=len(ids))

        session = PartialProjectSession.open(
            SOURCE, item_ids=ids, page_ids=(), pages_complete=False,
            include_boards=False)
        scene = _AuditScene(items)
        assert stage('capture_untouched', lambda: session.capture(scene)).changes == (), \
            '没有改动时不该产生任何行变更'

        marker = 'Session audit: one page edited, the rest of the project untouched'
        items[0].data['notes'] = marker
        edited = items[0].save_id
        snapshot = stage('capture_edit', lambda: session.capture(scene))
        assert len(snapshot.changes) == 1, [change.table for change in snapshot.changes]
        assert snapshot.changes[0].table == 'items'
        assert snapshot.changes[0].key == edited

        receipt = stage('save_as', lambda: save_prism(
            str(SESSION_TARGET), None, create_new=True, partial_snapshot=snapshot))
        assert receipt.revision == snapshot.revision
        session.acknowledge(snapshot, receipt, scene=scene, filename=str(SESSION_TARGET))
        assert session.invalid is None
        assert session.capture(scene).changes == (), '回执之后同一个页面不该还是 dirty'

        assert stage('hash_originals_after',
                     lambda: original_hashes(SESSION_TARGET, 'sqlar', 'item_id')) == originals_before
        assert stage('hash_attachments_after',
                     lambda: attachment_hashes(SESSION_TARGET)) == attachments_before
        assert stage('hash_source_after', lambda: file_hash(SOURCE)) == source_hash

        with closing(sqlite3.connect(SOURCE.as_uri()+'?mode=ro', uri=True)) as before, \
                closing(sqlite3.connect(SESSION_TARGET.as_uri()+'?mode=ro', uri=True)) as after:
            original_items = before.execute('SELECT * FROM items ORDER BY id').fetchall()
            saved_items = after.execute('SELECT * FROM items ORDER BY id').fetchall()
            assert len(saved_items) == len(original_items)
            changed = 0
            for old, new in zip(original_items, saved_items):
                if old == new:
                    continue
                assert old[0] == edited, f'未改动的行被重写了：{old[0]}'
                assert old[:-1] == new[:-1], '只该改 metadata，几何列不该动'
                old_data, new_data = json.loads(old[-1]), json.loads(new[-1])
                assert new_data['notes'] == marker
                old_data['notes'] = marker
                assert old_data == new_data, '未知字段必须在合并后原样保留'
                changed += 1
            assert changed == 1
            tables = [row[0] for row in before.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
            preserved = []
            for table in tables:
                if table in ('sqlar', 'attachments', 'items'):
                    continue
                assert before.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall() == \
                    after.execute(f'SELECT * FROM "{table}" ORDER BY 1').fetchall(), table
                preserved.append(table)
            assert after.execute('PRAGMA integrity_check').fetchone() == ('ok',)
        report['preserved_tables'] = preserved
        report['stored_item_records'] = len(saved_items)
        report['original_entries'] = len(originals_before)
        report['attachment_entries'] = len(attachments_before)
        report['source_sha256'] = source_hash
        report['saved_revision'] = receipt.revision

        window = main_window
        window.resize(1440, 900)
        window.show()
        started = time.perf_counter()
        window.view.open_from_file(str(SESSION_TARGET))
        qtbot.waitUntil(lambda: not window.view.worker.isRunning() and
                       not window.view._project_opening, timeout=60000)
        assert window.view.filename == str(SESSION_TARGET)
        assert list(window.view.scene.iter_assets())
        report['stages']['native_complete_reopen'] = round(
            (time.perf_counter()-started)*1000, 3)
        window.view.undo_stack.setClean()
        report['assertions_completed'] = True
    finally:
        SESSION_REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
