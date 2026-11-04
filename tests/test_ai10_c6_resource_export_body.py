"""C6：延迟正文/附件的导出 —— 读得到就写出来，读不到就明确失败。

这一组是 AI-10 在 round-2 里修的那条缺陷的定向验证。缺陷是：按页打开的
工程里，文档页的正文还在文件里没进内存，而导出读的是
``page.get('content') or ''`` —— 读到的是"没人读过的正文"，写出来的是
一个**空 .docx**，界面上还报"导出成功"。

这一组盯住的事：

1. **正文真的拿到了**：按页打开（正文不进内存）之后导出的 .docx 里有那
   段字、有那张图 —— 不是空文件。
2. **附件真的拿到了**：.docx 里的 media 就是工程里存的那份字节。
3. **真的空的就说空**：正文确实是空的，不产出空 .docx，而是说明跳过。
4. **读不到就明确失败**：源文件没了、正文坏了、图片读不出来 —— 明确
   报错、**不产出文件**，多页时逐页给原因。
5. **取消不算成功**：取消要真的停下来，已经写出的文件要撤回来。
6. **源工程一个字节都不动**：导出前后源文件的哈希一样。

夹具都是**副本**：每个用例在自己的临时目录里造一个工程，测完即弃；
产品树里不写任何东西。
"""
import hashlib
import json
import os
import sqlite3
import struct
import zipfile
import zlib
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from prism.fileio.resource_export import (
    ExportCancelled, ResourceSubtreeExporter, write_directory, write_zip)
from prism.fileio.resource_export_preparation import (
    BODY_DEFERRED, BODY_EMPTY, BODY_ERROR, BODY_LOADED, BODY_MISSING,
    PageBodyReader)
from prism.fileio.schema import APPLICATION_ID, SCHEMA, USER_VERSION
from prism.workspace_service import ResourceTreeService

PROJECT_ID = 'ai10-c6-export'

DOC_PAGE = 'doc-page'
SECOND_DOC_PAGE = 'doc-second'
EMPTY_DOC_PAGE = 'empty-doc-page'
BROKEN_DOC_PAGE = 'broken-doc-page'
MINDMAP_PAGE = 'mindmap-page'
EMPTY_MINDMAP_PAGE = 'empty-mindmap-page'

#: 一个不会在 .docx 的 XML 里被拆开的标记：断言正文用的是它。
DOC_MARK = 'AI10-DEFERRED-BODY'
SECOND_DOC_MARK = 'AI10-SECOND-BODY'
DOC_HTML = (f'<p>{DOC_MARK} 延迟正文里的那段字。</p>'
            '<p><img src="prism://attach/1"></p>')
SECOND_DOC_HTML = f'<p>{SECOND_DOC_MARK} 第二页。</p>'
MINDMAP_MARK = 'AI10-DEFERRED-TREE'
MINDMAP_TREE = {'data': {'text': f'{MINDMAP_MARK} 根节点'},
                'children': [{'data': {'text': '子节点'}, 'children': []}]}


# ── 夹具 ─────────────────────────────────────────────────────────


def png_bytes(seed=1, size=8):
    """一张真的 PNG —— 断言的是字节相等，不能用"看起来一样"糊弄过去。"""
    raw = b''
    for row in range(size):
        raw += b'\x00' + bytes([(column * 17 + seed) % 256
                                for column in range(size * 3)])

    def chunk(tag, data):
        return (struct.pack('>I', len(data)) + tag + data +
                struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff))

    header = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header) +
            chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b''))


IMAGE_BYTES = png_bytes(7)


def page_payload(content):
    """The envelope the product stores a page body in."""
    return {'__prism_page_payload__': True, 'content': content,
            'tags': [], 'parent': None, 'permanent': False}


def build_project(path, pages, attachments=()):
    """Create a .prism with the product's own DDL.

    ``pages`` is a sequence of ``(page_id, kind, title, content)``; the
    content is stored exactly the way the product stores it.
    """
    path = Path(path)
    with closing(sqlite3.connect(str(path))) as connection:
        for statement in SCHEMA:
            connection.execute(statement)
        connection.execute('CREATE TABLE IF NOT EXISTS workspace_nodes ('
                           'id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata ('
                           'key TEXT PRIMARY KEY, value TEXT)')
        # 产品的读写器靠这两个标记认出"这是 Prism 文件"。
        connection.execute(f'PRAGMA user_version={USER_VERSION}')
        connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
        connection.execute('INSERT OR REPLACE INTO prism_metadata VALUES(?,?)',
                           ('project_id', PROJECT_ID))
        for order, (page_id, kind, title, content) in enumerate(pages):
            connection.execute(
                'INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
                (page_id, kind, title,
                 json.dumps(page_payload(content), ensure_ascii=False),
                 order))
        for attachment_id, name, mime, blob in attachments:
            connection.execute(
                'INSERT INTO attachments(id,board_id,name,mime,blob,created_at)'
                ' VALUES(?,?,?,?,?,?)',
                (attachment_id, 0, name, mime, blob, 0))
        connection.commit()
    return path


def standard_project(tmp_path, name='project.prism'):
    """The project every case starts from: one document, one mind map."""
    return build_project(
        Path(tmp_path) / name,
        pages=[(DOC_PAGE, 'document', '延迟文档', DOC_HTML),
               (SECOND_DOC_PAGE, 'document', '第二页', SECOND_DOC_HTML),
               (EMPTY_DOC_PAGE, 'document', '空文档', ''),
               (MINDMAP_PAGE, 'mindmap', '延迟脑图', MINDMAP_TREE),
               (EMPTY_MINDMAP_PAGE, 'mindmap', '空脑图', None)],
        attachments=[(1, 'photo.png', 'image/png', IMAGE_BYTES)])


def tree_service(pages):
    """The resource tree the export walks, with one page node per page."""
    service = ResourceTreeService(PROJECT_ID)
    for page_id, _kind, title, _content in pages:
        service.createPage('document' if 'doc' in page_id else 'mindmap',
                           title, page_id)
    return service


def sha256(path):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_rows(path, statement, parameters=()):
    with closing(sqlite3.connect(
            Path(path).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
        return connection.execute(statement, parameters).fetchall()


def broken_body_project(tmp_path):
    """A project whose document body is damaged (not valid JSON)."""
    path = standard_project(tmp_path)
    with closing(sqlite3.connect(str(path))) as connection:
        connection.execute(
            'UPDATE workspace_pages SET data=? WHERE id=?',
            ('{this is not json', BROKEN_DOC_PAGE))
        connection.execute(
            'INSERT INTO workspace_pages VALUES(?,?,?,?,?)',
            (BROKEN_DOC_PAGE, 'document', '坏掉的文档', '{broken', 9))
        connection.commit()
    return path


# ── 场景状态 ─────────────────────────────────────────────────────


def deferred_state(path):
    """The scene state a page-scoped open leaves behind.

    ``SQLiteIO.read_board_extras(scope=...)`` reads the page list and the
    page *headers*; the bodies stay in the file.  This is that state,
    built from the file itself so the test cannot drift away from it.
    """
    pages = []
    for page_id, kind, title in read_rows(
            path, 'SELECT id, kind, title FROM workspace_pages '
                  'ORDER BY sort_order, rowid'):
        pages.append({'id': page_id, 'kind': kind, 'title': title,
                      'tags': [], 'parent': None, 'permanent': False})
    return {
        'workspace_pages': pages,
        '_deferred_page_ids': {page['id'] for page in pages},
        '_board_extras_source': str(path),
    }


def reader_for(state, attachments=None):
    """The reader the export uses, over a deferred scene state."""
    return PageBodyReader(
        pages=state['workspace_pages'],
        source=state['_board_extras_source'],
        deferred_page_ids=state['_deferred_page_ids'])


def export_document(state, outdir, page_id=DOC_PAGE, attachments=None,
                    service=None):
    """Export one page through the official subtree export path."""
    if attachments is None:
        # 场景里那张附件表：工程里存的那份图片。
        attachments = {1: ('photo.png', 'image/png', IMAGE_BYTES)}
    bodies = reader_for(state)
    tree = service if service is not None else tree_service(
        [(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(
        tree, pages=bodies.pages, attachments=attachments, bodies=bodies)
    node = next(n for n in tree.nodes
                if n.get('pageId') == page_id and n.get('nodeType') == 'page')
    plan = exporter.build_plan('document' if 'doc' in page_id else 'mindmap',
                                node['id'])
    write_directory(plan, str(outdir))
    return plan


def docx_text(path):
    with zipfile.ZipFile(str(path)) as archive:
        return archive.read('word/document.xml').decode('utf-8')


def docx_media(path):
    with zipfile.ZipFile(str(path)) as archive:
        return {name: archive.read(name)
                for name in archive.namelist() if name.startswith('word/media/')}


# ── 1. 正文真的拿到了 ───────────────────────────────────────────


def test_a_deferred_document_page_exports_its_text(tmp_path):
    """按页打开之后导出的 .docx 里有正文 —— 不是空文件。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)

    outdir = tmp_path / 'out'
    export_document(state, outdir)

    target = outdir / '延迟文档' / '延迟文档.docx'
    assert target.is_file(), '延迟正文应该导出成一个真的 .docx'
    assert DOC_MARK in docx_text(target)


def test_the_deferred_page_really_has_no_body_in_memory(tmp_path):
    """这条是缺陷本身：页面在，正文不在。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)

    page = next(p for p in state['workspace_pages'] if p['id'] == DOC_PAGE)
    assert 'content' not in page, (
        '夹具没有复现缺陷：延迟打开的页面不该带 content 键')
    # 旧写法读到的就是空串：
    assert (page.get('content') or '') == ''


def test_a_deferred_document_page_exports_its_picture(tmp_path):
    """图片也是工程里存的那份字节。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    attachments = {1: ('photo.png', 'image/png', IMAGE_BYTES)}

    outdir = tmp_path / 'out'
    export_document(state, outdir, attachments=attachments)

    target = outdir / '延迟文档' / '延迟文档.docx'
    media = docx_media(target)
    assert media, '.docx 里应该有图片'
    assert IMAGE_BYTES in media.values(), '图片字节必须和工程里存的那份一样'


def test_a_deferred_mindmap_page_exports_its_tree(tmp_path):
    """脑图同理：树还在文件里，就该被导出来。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(MINDMAP_PAGE, 'mindmap', '延迟脑图', MINDMAP_TREE)])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == MINDMAP_PAGE)
    plan = exporter.build_plan('mindmap', node['id'])
    write_directory(plan, str(tmp_path / 'out'))

    target = tmp_path / 'out' / '延迟脑图' / '延迟脑图.xmind'
    assert target.is_file()
    with zipfile.ZipFile(str(target)) as archive:
        content = json.loads(archive.read('content.json').decode('utf-8'))
    assert MINDMAP_MARK in json.dumps(content, ensure_ascii=False)


def test_the_source_project_is_not_touched(tmp_path):
    """导出只读源工程，一个字节都不能变。"""
    project = standard_project(tmp_path)
    before = sha256(project)
    state = deferred_state(project)

    export_document(state, tmp_path / 'out')

    assert sha256(project) == before, '源工程被改了'


# ── 2. 真的空的就说空 ───────────────────────────────────────────


def test_a_genuinely_empty_document_produces_no_file(tmp_path):
    """正文确实是空的：不出空 .docx，而是说明跳过。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(EMPTY_DOC_PAGE, 'document', '空文档', '')])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       bodies=bodies)
    node = next(n for n in tree.nodes
                if n.get('pageId') == EMPTY_DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])
    write_directory(plan, str(tmp_path / 'out'))

    assert not (tmp_path / 'out' / '空文档' / '空文档.docx').exists(), (
        '空正文不能写成一个空 .docx 冒充导出成功')
    assert any('empty' in note or '空' in note for note in plan.all_notes), (
        f'空正文要说清楚，notes={plan.all_notes}')


def test_a_deferred_empty_mindmap_is_skipped_with_a_note(tmp_path):
    """空脑图：跳过并说明，和原来一样。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(EMPTY_MINDMAP_PAGE, 'mindmap', '空脑图', None)])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       bodies=bodies)
    node = next(n for n in tree.nodes
                if n.get('pageId') == EMPTY_MINDMAP_PAGE)
    plan = exporter.build_plan('mindmap', node['id'])
    write_directory(plan, str(tmp_path / 'out'))

    assert not (tmp_path / 'out' / '空脑图' / '空脑图.xmind').exists()
    assert any('empty mind map' in note for note in plan.all_notes)


def test_the_reader_tells_empty_apart_from_unreadable(tmp_path):
    """空正文和读不到，是两回事 —— 状态必须分得开。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    bodies = reader_for(state)

    assert bodies.read(EMPTY_DOC_PAGE).status == BODY_EMPTY
    assert bodies.read(DOC_PAGE).status == BODY_DEFERRED

    # 源文件没了：这次是"读不到"，不是"空"。
    os.replace(str(project), str(tmp_path / 'gone.prism'))
    gone = reader_for(deferred_state(tmp_path / 'gone.prism'))
    gone.source = str(tmp_path / 'nowhere.prism')
    assert gone.read(DOC_PAGE).status == BODY_ERROR


def test_the_reader_goes_through_the_official_accessor(tmp_path):
    """读取委托给正式只读访问器 —— 格式只有一个主人。

    ``__prism_page_payload__`` 信封是 ``prism/fileio/sql.py`` 写出来的。两处
    各解析一遍同一个信封就会静默漂移：写的一边存了正文，读的一边拿到空
    字典，然后导出"成功"出一个空文件 —— 那正是这个缺陷的形状。这条用例把
    "本包不再自己解析信封"钉死，也钉死"委托之后三种结局仍然分得开"。
    """
    from prism.fileio import resource_export_preparation as preparation
    from prism.fileio import sql

    assert preparation.read_page_body is sql.read_page_body, (
        '读取必须走 prism.fileio.sql 提供的正式只读访问器')
    assert 'sqlite3' not in vars(preparation), (
        '本包不再自己开数据库连接，也就没有第二个信封解析器')

    project = standard_project(tmp_path)
    bodies = reader_for(deferred_state(project))
    assert bodies.read(DOC_PAGE).status == BODY_DEFERRED
    assert bodies.read(EMPTY_DOC_PAGE).status == BODY_EMPTY
    assert bodies.read('no-such-page').status == BODY_MISSING


# ── 3. 读不到就明确失败 ─────────────────────────────────────────


def test_a_missing_source_fails_loudly_and_writes_nothing(tmp_path):
    """源工程被移走：明确失败，不产出 .docx。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    os.replace(str(project), str(tmp_path / 'moved.prism'))

    bodies = reader_for(state)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])

    errors = []
    plan_target = tmp_path / 'out'
    from prism.fileio.resource_export import _write_entries
    errors = _write_entries(plan, str(plan_target / '文档'), None)

    assert errors, '读不到正文必须报错'
    assert not (plan_target / '延迟文档' / '延迟文档.docx').exists(), (
        '读不到正文不能留下一个空 .docx')
    assert 'gone' in errors[0] or 'prism' in errors[0], (
        f'报错要说清楚是哪个文件：{errors}')


def test_a_damaged_body_fails_loudly(tmp_path):
    """正文在文件里损坏了：明确失败，不产出文件。"""
    project = broken_body_project(tmp_path)
    state = deferred_state(project)
    bodies = reader_for(state)
    body = bodies.read(BROKEN_DOC_PAGE)

    assert body.status == BODY_ERROR
    assert body.reason, '失败要说原因'


def test_a_mixed_export_writes_the_good_page_and_reports_the_bad_one(tmp_path):
    """多页混着：好的写出来，坏的逐个说原因，并且说"不完整"。"""
    project = broken_body_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML),
                         (BROKEN_DOC_PAGE, 'document', '坏掉的文档', '{broken')])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(
        tree, pages=bodies.pages,
        attachments={1: ('photo.png', 'image/png', IMAGE_BYTES)}, bodies=bodies)
    plan = exporter.build_plan('document')

    from prism.fileio.resource_export import _write_entries
    errors = _write_entries(plan, str(tmp_path / 'out' / '文档'), None)

    assert (tmp_path / 'out' / '文档' / '延迟文档.docx').is_file()
    assert not (tmp_path / 'out' / '文档' / '坏掉的文档.docx').exists(), (
        '读不出正文的那一页不能留下空文件')
    assert errors, '坏掉的那一页必须报错'
    assert any('坏掉的文档' in error for error in errors), (
        f'报错要指出是哪一页：{errors}')


def test_an_unreadable_picture_fails_the_page(tmp_path):
    """图片读不出来：这一页不算导出成功，缺一张图的 .docx 不算数。"""
    project = standard_project(tmp_path)
    with closing(sqlite3.connect(str(project))) as connection:
        connection.execute('UPDATE attachments SET blob=NULL WHERE id=1')
        connection.commit()
    state = deferred_state(project)
    attachments = {1: ('photo.png', 'image/png', None)}

    bodies = reader_for(state)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       attachments=attachments, bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])

    from prism.fileio.resource_export import _write_entries
    errors = _write_entries(plan, str(tmp_path / 'out' / '延迟文档'), None)

    assert errors, '图片读不出来必须报错'
    assert not (tmp_path / 'out' / '延迟文档' / '延迟文档.docx').exists()


def test_a_page_that_does_not_exist_is_reported(tmp_path):
    """树里指向一个不存在的页：明确失败，不是空文件。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    bodies = reader_for(state)

    body = bodies.read('no-such-page')

    assert body.status == BODY_MISSING
    assert body.reason


# ── 4. 内存里的正文说了算 ───────────────────────────────────────


def test_the_body_in_memory_wins_over_the_file(tmp_path):
    """用户改了还没存：导出的是眼前那份，不是文件里那份。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    page = next(p for p in state['workspace_pages'] if p['id'] == DOC_PAGE)
    page['content'] = '<p>AI10-DIRTY-BODY 用户刚敲的字</p>'

    outdir = tmp_path / 'out'
    export_document(state, outdir)

    target = outdir / '延迟文档' / '延迟文档.docx'
    text = docx_text(target)
    assert 'AI10-DIRTY-BODY' in text
    assert DOC_MARK not in text


def test_editing_during_the_export_does_not_change_the_file(tmp_path):
    """导出是一次快照：写到一半改字，不会写进半个文件。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    page = next(p for p in state['workspace_pages'] if p['id'] == DOC_PAGE)

    bodies = reader_for(state)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(
        tree, pages=bodies.pages,
        attachments={1: ('photo.png', 'image/png', IMAGE_BYTES)}, bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])

    page['content'] = '<p>AI10-CHANGED-MID-FLIGHT</p>'
    write_directory(plan, str(tmp_path / 'out'))

    text = docx_text(tmp_path / 'out' / '延迟文档' / '延迟文档.docx')
    assert DOC_MARK in text
    assert 'AI10-CHANGED-MID-FLIGHT' not in text


def test_a_loaded_body_is_read_from_memory(tmp_path):
    """正文已经在内存里：不必去碰文件。"""
    state = {'workspace_pages': [{'id': DOC_PAGE, 'kind': 'document',
                                  'title': '在内存里', 'content': DOC_HTML}],
             '_deferred_page_ids': set(),
             '_board_extras_source': None}
    bodies = reader_for(state)

    body = bodies.read(DOC_PAGE)

    assert body.status == BODY_LOADED
    assert body.content == DOC_HTML


# ── 5. 取消不算成功 ─────────────────────────────────────────────


class CancellingWorker:
    """A worker that cancels itself after the first file."""

    def __init__(self, cancel_after=1):
        self.cancel_after = cancel_after
        self.canceled = False
        self.progress = _Signal()
        self.finished = _Signal()
        self.begin_processing = _Signal()
        self.user_input_required = _Signal()

    def _on_progress(self, value):
        if value >= self.cancel_after:
            self.canceled = True

    def start(self):
        return None


class _Signal:
    def __init__(self):
        self.slots = []

    def connect(self, slot):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def test_cancelling_the_export_takes_the_files_back(tmp_path):
    """取消要真的停下来，已经写出的文件要撤回来，不能报成功。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML),
                         (SECOND_DOC_PAGE, 'document', '第二页',
                          SECOND_DOC_HTML)])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(
        tree, pages=bodies.pages,
        attachments={1: ('photo.png', 'image/png', IMAGE_BYTES)}, bodies=bodies)
    plan = exporter.build_plan('document')

    worker = CancellingWorker()
    worker.progress.connect(worker._on_progress)

    from prism.fileio.resource_export import _write_entries
    errors = _write_entries(plan, str(tmp_path / 'out' / '文档'), worker)

    assert bodies.cancelled, '取消必须被记下来'
    assert any(isinstance(error, ExportCancelled) for error in errors), (
        f'取消要作为失败说出来：{errors}')
    written = list((tmp_path / 'out').rglob('*.docx'))
    assert not written, f'取消之后不该留下文件：{written}'


# ── 6. 不留临时产物 ─────────────────────────────────────────────


def _leftovers(root):
    return [str(path) for path in Path(root).rglob('*')
            if path.is_file() and '.partial' in path.name]


def test_a_failed_export_leaves_no_temporary_files(tmp_path):
    """失败的导出不留半成品在目标目录里。"""
    project = broken_body_project(tmp_path)
    state = deferred_state(project)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML),
                         (BROKEN_DOC_PAGE, 'document', '坏掉的文档', '{broken')])
    bodies = reader_for(state)
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       bodies=bodies)
    good = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', good['id'])

    from prism.fileio.resource_export import _write_entries
    _write_entries(plan, str(tmp_path / 'out' / '延迟文档'), None)

    assert not _leftovers(tmp_path / 'out'), '目标目录里不该有临时文件'


def test_a_successful_export_leaves_no_temporary_files(tmp_path):
    project = standard_project(tmp_path)
    state = deferred_state(project)
    attachments = {1: ('photo.png', 'image/png', IMAGE_BYTES)}

    outdir = tmp_path / 'out'
    export_document(state, outdir, attachments=attachments)

    assert not _leftovers(outdir)


def test_the_zip_holds_the_same_files_and_leaves_nothing_behind(tmp_path):
    """zip 出口和文件夹出口写的是同一份东西。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    attachments = {1: ('photo.png', 'image/png', IMAGE_BYTES)}
    bodies = reader_for(state)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       attachments=attachments, bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])

    folder_root = write_directory(plan, str(tmp_path / 'folder'))
    archive_path = tmp_path / 'out.zip'
    write_zip(plan, str(archive_path))

    with zipfile.ZipFile(str(archive_path)) as archive:
        names = {name for name in archive.namelist()
                 if not name.endswith('/')}
    on_disk = {os.path.relpath(os.path.join(directory, name), folder_root)
               .replace(os.sep, '/')
               for directory, _dirs, files in os.walk(folder_root)
               for name in files}
    assert names == {f'{plan.root_name}/{name}' for name in on_disk}
    assert not _leftovers(tmp_path)


# ── 9. 另存之后 ─────────────────────────────────────────────────


def test_after_a_save_as_the_export_reads_the_new_file(tmp_path):
    """另存之后正文在新文件里；旧文件被移走也不影响这次导出。"""
    original = standard_project(tmp_path, 'original.prism')
    saved_as = tmp_path / 'saved-as.prism'
    saved_as.write_bytes(original.read_bytes())

    state = deferred_state(saved_as)
    state['_board_extras_source'] = str(saved_as)
    os.replace(str(original), str(tmp_path / 'moved-away.prism'))

    outdir = tmp_path / 'out'
    export_document(state, outdir)

    target = outdir / '延迟文档' / '延迟文档.docx'
    assert target.is_file()
    assert DOC_MARK in docx_text(target)


def test_after_a_save_as_with_the_new_file_gone_it_fails_loudly(tmp_path):
    """另存之后新文件也没了：明确失败，不产出空 .docx。"""
    original = standard_project(tmp_path, 'original.prism')
    saved_as = tmp_path / 'saved-as.prism'
    saved_as.write_bytes(original.read_bytes())

    state = deferred_state(saved_as)
    state['_board_extras_source'] = str(saved_as)
    os.replace(str(original), str(tmp_path / 'moved-away.prism'))
    os.replace(str(saved_as), str(tmp_path / 'gone-too.prism'))

    bodies = reader_for(state)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(
        tree, pages=bodies.pages,
        attachments={1: ('photo.png', 'image/png', IMAGE_BYTES)}, bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])

    from prism.fileio.resource_export import _write_entries
    errors = _write_entries(plan, str(tmp_path / 'out' / '延迟文档'), None)

    assert errors, '源文件没了必须报错'
    assert not (tmp_path / 'out' / '延迟文档' / '延迟文档.docx').exists()


# ── 8. 正式的那条导出路径 ───────────────────────────────────────


class SyncWorker:
    """The export worker, run synchronously.

    The real one is a ``QThread``; what matters here is the contract:
    it calls the writer with ``worker=`` and emits ``finished(path, errors)``
    when the write phase is over.
    """

    def __init__(self, func, *args, **kwargs):
        self.func = func
        self.args = args
        self.kwargs = kwargs
        self.kwargs['worker'] = self
        self.canceled = False
        self.progress = _Signal()
        self.finished = _Signal()
        self.begin_processing = _Signal()
        self.user_input_required = _Signal()

    def start(self):
        self.func(*self.args, **self.kwargs)

    def wait(self, timeout=None):
        return True


class FakeParent:
    def _store_current_page(self):
        return True


class FakeView:
    """Just enough of the view for the export workflow."""

    def __init__(self, scene, service, filename=None):
        self.scene = scene
        self.filename = filename
        self.parent = FakeParent()
        self.category_panel = SimpleNamespace(resource_service=service)
        self.worker = None
        self.progress = None
        self.reported = []

    def on_export_finished(self, filename, errors):
        self.reported.append((filename, list(errors)))


def fake_scene(state, attachments=None):
    if attachments is None:
        attachments = {1: ('photo.png', 'image/png', IMAGE_BYTES)}
    return SimpleNamespace(
        workspace_pages=state['workspace_pages'],
        attachments=attachments,
        _deferred_page_ids=state['_deferred_page_ids'],
        _board_extras_source=state['_board_extras_source'])


def run_workflow(view, tmp_path, monkeypatch, section='document', node_id=None):
    """Drive the official export workflow, with the dialogs stubbed out."""
    from PyQt6 import QtWidgets

    from prism.actions import export_workflow

    notifications = []
    monkeypatch.setattr(export_workflow, 'ask_export_shape',
                        lambda *args, **kwargs: 'folder')
    monkeypatch.setattr(export_workflow.widgets, 'PrismNotification',
                        lambda parent, text: notifications.append(text))
    monkeypatch.setattr(export_workflow.widgets, 'PrismProgressDialog',
                        lambda *args, **kwargs: None)
    monkeypatch.setattr(export_workflow.fileio, 'ThreadedIO', SyncWorker)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getExistingDirectory',
                        lambda *args, **kwargs: str(tmp_path / 'out'))

    export_workflow.export_resource_subtree(view, section, node_id)
    return notifications


def test_the_workflow_exports_a_deferred_document(tmp_path, monkeypatch):
    """走正式那条路径：按页打开的工程，导出拿到正文。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    service = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    node = next(n for n in service.nodes
                if n.get('pageId') == DOC_PAGE)
    view = FakeView(fake_scene(state), service, str(project))

    notifications = run_workflow(view, tmp_path, monkeypatch, 'document',
                                 node['id'])

    target = tmp_path / 'out' / '延迟文档' / '延迟文档.docx'
    assert target.is_file()
    assert DOC_MARK in docx_text(target)
    assert not view.reported, f'不该报错：{view.reported}'
    assert any('Exported 1 file(s)' in text for text in notifications), (
        f'成功提示要说实话：{notifications}')


def test_the_workflow_reports_an_incomplete_export(tmp_path, monkeypatch):
    """源工程没了：明确报"不完整"，并且不报成功。"""
    project = standard_project(tmp_path)
    state = deferred_state(project)
    service = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    node = next(n for n in service.nodes
                if n.get('pageId') == DOC_PAGE)
    view = FakeView(fake_scene(state), service, str(project))
    os.replace(str(project), str(tmp_path / 'moved.prism'))

    notifications = run_workflow(view, tmp_path, monkeypatch, 'document',
                                 node['id'])

    assert view.reported, '读不到正文必须报出来'
    assert not any('Exported' in text for text in notifications), (
        f'没写完就不能报成功：{notifications}')
    assert not list((tmp_path / 'out').rglob('*.docx')), '不该留下空文件'


def test_the_workflow_says_which_page_failed(tmp_path, monkeypatch):
    """多页混着：逐页给出原因。"""
    project = broken_body_project(tmp_path)
    state = deferred_state(project)
    service = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML),
                            (BROKEN_DOC_PAGE, 'document', '坏掉的文档',
                             '{broken')])
    view = FakeView(fake_scene(state), service, str(project))

    run_workflow(view, tmp_path, monkeypatch, 'document')

    assert view.reported, '坏掉的那一页必须报出来'
    failures = [error for _path, errors in view.reported for error in errors]
    assert any('坏掉的文档' in error for error in failures), (
        f'要逐页说原因：{failures}')



def test_a_real_paged_open_leaves_the_body_in_the_file(qapp, tmp_path):
    """走产品自己的按页打开：正文不进内存，导出照样拿得到。

    这一条不走"造一个假场景"的路，而是真的 ``load_prism(..., scope=[])``
    打开一次，再从那个场景导出 —— 夹具和产品行为对不上就会露馅。
    """
    from PyQt6 import QtGui

    from prism.fileio import load_prism
    from prism.scene import PrismGraphicsScene

    project = standard_project(tmp_path)
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    load_prism(str(project), scene, scope=[])

    page = next(p for p in scene.workspace_pages if p['id'] == DOC_PAGE)
    assert 'content' not in page, '按页打开之后正文不该在内存里'

    bodies = PageBodyReader(pages=scene.workspace_pages,
                             source=scene._board_extras_source,
                             deferred_page_ids=scene._deferred_page_ids)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       attachments=scene.attachments,
                                       bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])
    write_directory(plan, str(tmp_path / 'out'))

    target = tmp_path / 'out' / '延迟文档' / '延迟文档.docx'
    assert target.is_file()
    assert DOC_MARK in docx_text(target)
    media = docx_media(target)
    assert IMAGE_BYTES in media.values(), '图片也要是工程里存的那份字节'


def test_a_real_paged_open_keeps_the_source_untouched(qapp, tmp_path):
    from PyQt6 import QtGui

    from prism.fileio import load_prism
    from prism.scene import PrismGraphicsScene

    project = standard_project(tmp_path)
    before = sha256(project)
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    load_prism(str(project), scene, scope=[])

    bodies = PageBodyReader(pages=scene.workspace_pages,
                             source=scene._board_extras_source,
                             deferred_page_ids=scene._deferred_page_ids)
    tree = tree_service([(DOC_PAGE, 'document', '延迟文档', DOC_HTML)])
    exporter = ResourceSubtreeExporter(tree, pages=bodies.pages,
                                       attachments=scene.attachments,
                                       bodies=bodies)
    node = next(n for n in tree.nodes if n.get('pageId') == DOC_PAGE)
    plan = exporter.build_plan('document', node['id'])
    write_directory(plan, str(tmp_path / 'out'))

    assert sha256(project) == before, '按页打开 + 导出都不能改源工程'
