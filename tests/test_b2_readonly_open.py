"""B-2 返工 ④ 与 ② 的验收测试。

④ **只读打开不得触发任何写事务**。判据是三样一起看：目录里不出现
   ``-journal`` / ``-wal`` / ``-shm``，源文件的 size / mtime_ns / 内容
   hash 前后完全一致。只看一次快照不算数 —— 自动保存是延迟触发的，
   打开本身也要一条写事务都不许发生。

② **读 ``project_id`` 失败要说真话**。三种结局必须分开：表/键不存在
   （老工程，正常）、读到了（就是它）、读失败了（异常，必须报出来）。
   把读失败吞成随机 uuid 再报"材质库数据损坏"，是在拿假原因盖真原因。

所有工程都在 ``tmp_path`` 里现造，不碰任何真实工程文件。
"""
import hashlib
import json
import os
import sqlite3

import pytest

from prism.fileio.errors import PrismFileIOError
from prism.fileio.schema import SCHEMA, USER_VERSION
from prism.fileio.sql import (SQLiteIO, is_readonly_project,
                               request_readonly_project)


PROJECT_ID = 'b2-readonly-open'


def _fingerprint(path):
    """size, mtime_ns and the content hash of one file."""
    stat = os.stat(path)
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return stat.st_size, stat.st_mtime_ns, digest.hexdigest()


def _write_traces(directory):
    """The write traces SQLite leaves next to a project."""
    return sorted(name for name in os.listdir(directory)
                  if name.endswith(('-journal', '-wal', '-shm')))


def _create_schema(connection):
    """Create the real schema, statement by statement.

    ``SCHEMA`` is a list of statements without trailing semicolons, so
    they cannot simply be concatenated into one script.
    """
    for statement in SCHEMA:
        connection.execute(statement)


def _make_project(path, project_id=PROJECT_ID, *, with_material=True,
                  image_bytes=b''):
    """A small but complete project: items, archive bytes and metadata.

    Every table the reader touches is there, so the read really walks the
    whole path instead of stopping at the first missing table.
    """
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=%s' % USER_VERSION)
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS workspace_nodes '
                           '(id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS material_records '
                           '(id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('project_id', project_id))
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('categories', '[]'))
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('tags', '[]'))
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('tag_system', '{}'))
        connection.execute(
            'INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
            "VALUES ('text',1,2,3,1,0,1,?)",
            (json.dumps({'text': 'hello'}),))
        connection.execute(
            'INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
            "VALUES ('pixmap',4,5,6,1,0,1,?)",
            (json.dumps({'filename': 'pic.png', 'imageWidth': 3,
                         'imageHeight': 3}),))
        connection.execute(
            'INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) '
            'VALUES (?,?,?,?,?,?)',
            ('pic.png', 2, 420, 0, len(image_bytes), image_bytes))
        connection.execute('INSERT INTO board_notes (board_id, html, updated_at)'
                           ' VALUES (0, ?, 0)', ('<p>note</p>',))
        connection.execute('INSERT INTO board_mindmap (board_id, tree) '
                           'VALUES (0, ?)',
                           (json.dumps({'data': {'text': 'root'}}),))
        connection.execute('INSERT INTO workspace_pages '
                           '(id, kind, title, data, sort_order) '
                           'VALUES (?,?,?,?,?)',
                           ('page-1', 'document', 'Doc',
                            json.dumps({'__prism_page_payload__': True,
                                        'content': '<p>body</p>', 'tags': [],
                                        'parent': None, 'permanent': False}),
                            0))
        connection.execute('INSERT INTO workspace_nodes VALUES (?,?)',
                           ('node-1', json.dumps({'id': 'node-1',
                                                  'type': 'folder',
                                                  'name': 'folder',
                                                  'parentId': None})))
        if with_material:
            record = dict(id='mat-1', projectId=project_id, schemaVersion=1,
                          name='Material', notes='', folderId=None, tags=[],
                          textures={}, preview={}, nativePackages=[],
                          status='draft', createdAt='2026-10-22',
                          updatedAt='2026-10-22', deletedAt=None)
            connection.execute('INSERT INTO material_records VALUES (?,?)',
                               ('mat-1', json.dumps(record)))
        connection.commit()
    finally:
        connection.close()
    return path


# ── ④ 只读打开不得触发任何写事务 ────────────────────────────────

def test_readonly_open_leaves_no_write_traces(view, tmp_path, imgdata3x3):
    """只读打开一个工程：没有 journal，源文件一个字节都没变。"""
    path = _make_project(str(tmp_path / 'project.prism'), image_bytes=imgdata3x3)
    before = _fingerprint(path)
    assert _write_traces(str(tmp_path)) == []

    io = SQLiteIO(path, view.scene, readonly=True)
    io.read()
    io._close_connection()

    assert _write_traces(str(tmp_path)) == [], (
        '只读打开留下了写痕迹')
    assert _fingerprint(path) == before, (
        '只读打开改动了源工程')


def test_readonly_open_through_load_prism_leaves_no_write_traces(
        view, tmp_path, imgdata3x3):
    """走真正的打开入口（``fileio.load_prism``）也是一样的结果。"""
    from prism import fileio

    path = _make_project(str(tmp_path / 'project.prism'), image_bytes=imgdata3x3)
    before = _fingerprint(path)

    fileio.load_prism(path, view.scene)

    assert _write_traces(str(tmp_path)) == []
    assert _fingerprint(path) == before


def test_readonly_open_reads_the_content(view, tmp_path, imgdata3x3):
    """只读打开仍然要读得到东西 —— 不许为了不写而少读。"""
    path = _make_project(str(tmp_path / 'project.prism'), image_bytes=imgdata3x3)

    SQLiteIO(path, view.scene, readonly=True).read()

    assert view.scene.project_id == PROJECT_ID
    assert view.scene.note_html == '<p>note</p>'
    assert [page['id'] for page in view.scene.workspace_pages] == ['page-1']
    assert [node['id'] for node in view.scene.workspace_nodes] == ['node-1']
    assert [record['id'] for record in view.scene.material_records] == ['mat-1']


def test_the_readonly_connection_cannot_write(view, tmp_path):
    """结构性保证：只读连接**物理上**写不了，不是"我们不写"。

    旧实现用 ``mode=rw``：连接本身仍可写，于是读路径上一句 pragma
    就能开出一个写事务。现在是 ``mode=ro``，SQLite 自己拒绝。
    """
    path = _make_project(str(tmp_path / 'project.prism'))

    io = SQLiteIO(path, view.scene, readonly=True)
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            io.ex("INSERT INTO items(type,x,y,z,scale,rotation,flip,data) "
                  "VALUES ('text',0,0,0,1,0,1,'{}')")
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            io.ex('PRAGMA user_version=1')
    finally:
        io._close_connection()


def test_readonly_open_does_not_migrate_the_file_in_place(view, tmp_path):
    """旧工程只读打开：原文件**不升级**，也不留下 journal。

    旧实现是"先试着写一下，写得动就地升级"—— 那正是"只读打开却改了
    源文件"。管理员权限下那次试写还会成功，于是工程被静默升级。
    """
    path = str(tmp_path / 'old.prism')
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=2')
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('project_id', 'old-project'))
        connection.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                           "VALUES ('text',1,2,3,1,0,1,?)",
                           (json.dumps({'text': 'hello'}),))
        connection.commit()
    finally:
        connection.close()
    before = _fingerprint(path)

    SQLiteIO(path, view.scene, readonly=True).read()

    assert _write_traces(str(tmp_path)) == []
    assert _fingerprint(path) == before, '只读打开把旧工程就地升级了'
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as check:
        version = check.execute('PRAGMA user_version').fetchone()[0]
    assert version == 2, f'原文件的版本被改了：{version}'


def test_readonly_open_of_an_old_project_still_reads(view, tmp_path):
    """旧工程仍然读得到 —— 迁移在临时副本上做，源文件不动。"""
    path = str(tmp_path / 'old.prism')
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=2')
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO prism_metadata VALUES (?,?)',
                           ('project_id', 'old-project'))
        connection.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                           "VALUES ('text',1,2,3,1,0,1,?)",
                           (json.dumps({'text': 'hello'}),))
        connection.commit()
    finally:
        connection.close()

    SQLiteIO(path, view.scene, readonly=True).read()

    assert view.scene.project_id == 'old-project'
    assert _write_traces(str(tmp_path)) == []


# ── ④ 的另一半：只读工程不许被写回 ──────────────────────────────

def test_a_readonly_project_is_not_written_back(view, tmp_path):
    """被标成只读的工程：保存被拒绝，文件一个字节都没变。

    2026-10-22 的事故就是这么发生的 —— GUI 打开了一个 51 GB 的工程，
    45 分钟后自动保存把它改写了，还留下一个 719 MB 的回滚日志。
    """
    path = _make_project(str(tmp_path / 'project.prism'))
    before = _fingerprint(path)

    request_readonly_project(view.scene, path)
    SQLiteIO(path, view.scene, readonly=True).read()
    assert is_readonly_project(view.scene, path) is True

    with pytest.raises(PrismFileIOError, match='只读'):
        SQLiteIO(path, view.scene).write()

    assert _fingerprint(path) == before
    assert _write_traces(str(tmp_path)) == []


def test_a_normal_open_drops_the_readonly_mark(view, tmp_path):
    """只读标记不能粘住：正常打开的工程照常能保存。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    other = _make_project(str(tmp_path / 'other.prism'))

    request_readonly_project(view.scene, path)
    SQLiteIO(path, view.scene, readonly=True).read()
    assert is_readonly_project(view.scene, path) is True

    # A normal open of another project: the mark is stale and goes away.
    SQLiteIO(other, view.scene, readonly=True).read()
    assert is_readonly_project(view.scene, other) is False
    assert is_readonly_project(view.scene) is False


def test_a_readonly_project_can_still_be_saved_as_another_file(
        view, tmp_path):
    """只读只针对那个工程：另存为到别的文件是允许的。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    target = str(tmp_path / 'copy.prism')

    request_readonly_project(view.scene, path)
    SQLiteIO(path, view.scene, readonly=True).read()

    SQLiteIO(target, view.scene, create_new=True).write()
    assert os.path.isfile(target)


# ── ② 读 project_id 失败要说真话 ────────────────────────────────

def test_the_project_id_comes_from_the_file(view, tmp_path):
    path = _make_project(str(tmp_path / 'project.prism'))

    SQLiteIO(path, view.scene, readonly=True).read()

    assert view.scene.project_id == PROJECT_ID


def test_a_project_id_failure_is_reported_with_its_real_reason(
        view, tmp_path, monkeypatch):
    """读 project_id 失败：报真原因，不报"材质库数据损坏"。

    旧实现把异常吞掉、换成一个随机 uuid；那个随机 uuid 随后让材质库
    校验失败，用户看到的是"材质库数据损坏"—— 真原因被彻底盖住了。
    """
    path = _make_project(str(tmp_path / 'project.prism'))
    original_fetchone = SQLiteIO.fetchone

    def failing_fetchone(self, *args, **kwargs):
        statement = args[0] if args else ''
        if 'project_id' in str(statement):
            raise sqlite3.OperationalError('database is locked')
        return original_fetchone(self, *args, **kwargs)

    monkeypatch.setattr(SQLiteIO, 'fetchone', failing_fetchone)

    with pytest.raises(PrismFileIOError) as error:
        SQLiteIO(path, view.scene, readonly=True).read()

    message = str(error.value)
    assert 'database is locked' in message, f'真原因没被说出来：{message}'
    assert '材质库数据损坏' not in message, (
        f'还是报成了材质库数据损坏：{message}')


def test_a_project_without_a_project_id_is_not_killed(view, tmp_path):
    """老工程没有 project_id 这一项：正常打开，不许判死。"""
    path = str(tmp_path / 'old.prism')
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=%s' % USER_VERSION)
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                           "VALUES ('text',1,2,3,1,0,1,?)",
                           (json.dumps({'text': 'hello'}),))
        connection.commit()
    finally:
        connection.close()
    before = view.scene.project_id

    SQLiteIO(path, view.scene, readonly=True).read()

    assert isinstance(view.scene.project_id, str)
    assert view.scene.project_id
    # 兜底 id 必须是稳定的：同一个工程读两次不能换来两个 id，
    # 否则同一批材质记录在第二次读时就成了"外来的"。
    assert view.scene.project_id == before


def test_a_project_without_a_metadata_table_is_not_killed(view, tmp_path):
    """连 prism_metadata 都没有的老工程：正常打开，不许判死。"""
    path = str(tmp_path / 'ancient.prism')
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=%s' % USER_VERSION)
        connection.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                           "VALUES ('text',1,2,3,1,0,1,?)",
                           (json.dumps({'text': 'hello'}),))
        connection.commit()
    finally:
        connection.close()

    SQLiteIO(path, view.scene, readonly=True).read()

    assert isinstance(view.scene.project_id, str)
    assert view.scene.project_id


def test_a_missing_project_id_is_not_reported_as_corrupt_data(
        view, tmp_path):
    """没有 project_id 却有材质记录：说的是缺 id，不是记录坏了。"""
    path = str(tmp_path / 'old-with-materials.prism')
    connection = sqlite3.connect(path)
    try:
        _create_schema(connection)
        connection.execute('PRAGMA user_version=%s' % USER_VERSION)
        connection.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                           '(key TEXT PRIMARY KEY, value TEXT)')
        connection.execute('CREATE TABLE IF NOT EXISTS material_records '
                           '(id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        record = dict(id='mat-1', projectId='some-other-project',
                      schemaVersion=1, name='Material', notes='',
                      folderId=None, tags=[], textures={}, preview={},
                      nativePackages=[], status='draft',
                      createdAt='2026-10-22', updatedAt='2026-10-22',
                      deletedAt=None)
        connection.execute('INSERT INTO material_records VALUES (?,?)',
                           ('mat-1', json.dumps(record)))
        connection.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                           "VALUES ('text',1,2,3,1,0,1,?)",
                           (json.dumps({'text': 'hello'}),))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(PrismFileIOError) as error:
        SQLiteIO(path, view.scene, readonly=True).read()

    message = str(error.value)
    assert 'project_id' in message, f'没有说是缺 project_id：{message}'
    assert '材质库数据损坏' not in message, (
        f'还是报成了材质库数据损坏：{message}')


def test_really_corrupt_material_data_still_says_so(view, tmp_path):
    """真的坏了的材质记录：还是报"材质库数据损坏"（但带上真原因）。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    connection = sqlite3.connect(path)
    try:
        connection.execute("UPDATE material_records SET payload='{broken json'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(PrismFileIOError) as error:
        SQLiteIO(path, view.scene, readonly=True).read()

    message = str(error.value)
    assert '材质库数据损坏' in message
    assert 'Expecting' in message or 'JSON' in message, (
        f'没有带上真正的原因：{message}')
