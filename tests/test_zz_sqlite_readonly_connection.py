"""A closed SQLiteIO connection stays closed, and no write path may touch a
read-only project.

事故背景（2026-10-22 18:36 那次改写工程之前就埋着）：

* ``SQLiteIO.connection`` 是 property。连接被显式关掉之后，下一次属性访问
  会**重开一个连接**——按 ``self.readonly`` 决定读写。写者
  （``readonly=False``）拿到的是**可写**句柄：于是"以只读方式打开的工程"
  在一次属性访问之后就重新变得可写；而且 Windows 上这个重开的连接还会把
  文件重新锁住（读工程时锁住文件，重建就会报"正被另一个程序占用"）。
* ``write_board_extras()`` 自己写表（笔记、附件、页面清单、素材记录、
  ``prism_metadata``），但它**不经过** ``write()`` 里的只读守卫——只读工程
  照样能被它改掉。

这两条都在这里钉死：关掉的连接不再自动复活，任何写路径都要先问许可。
"""
import os
import sqlite3

import pytest

from prism.fileio.errors import PrismFileIOError
from prism.fileio.sql import SQLiteIO, request_readonly_project
from tests.test_b2_readonly_open import (_fingerprint, _make_project,
                                         _write_traces)


# ── 关闭的连接不再自动复活 ────────────────────────────────────────

def test_a_closed_connection_is_not_reopened(view, tmp_path):
    """连接显式关掉之后，再访问 ``connection`` 必须报错，而不是重开一个。

    重开出来的连接按 ``self.readonly`` 决定读写：写者拿到的是可写句柄。
    """
    path = _make_project(str(tmp_path / 'project.prism'))
    io = SQLiteIO(path, view.scene, readonly=False)
    assert io.connection is not None          # 第一次访问：正常建立
    io._close_connection()

    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        io.connection


def test_a_closed_cursor_is_not_reopened(view, tmp_path):
    """``cursor`` 是另一条重开连接的入口，它也必须被堵住。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    io = SQLiteIO(path, view.scene, readonly=False)
    assert io.cursor is not None
    io._close_connection()

    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        io.cursor


def test_a_closed_writer_does_not_write_to_the_file(view, tmp_path):
    """关掉之后再写：写不进去，文件一个字节都不变。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    io = SQLiteIO(path, view.scene, readonly=False)
    io.ex('INSERT INTO items(type,x,y,z,scale,rotation,flip,data)'
          " VALUES ('text',0,0,0,1,0,1,'{}')")
    io.connection.commit()
    io._close_connection()

    # 快照取在合法写入**之后**：要证明的是"关掉之后写不进去"，
    # 不是"这个文件本来就不会变"。
    before = _fingerprint(path)
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        io.ex('INSERT INTO items(type,x,y,z,scale,rotation,flip,data)'
              " VALUES ('text',0,0,0,1,0,1,'{}')")

    assert _fingerprint(path) == before, '关掉的连接仍然改了源文件'
    assert _write_traces(str(tmp_path)) == [], '关掉的连接留下了写痕迹'


def test_a_closed_reader_stays_read_only(view, tmp_path):
    """只读读者关掉之后同样不再重开——重开的连接是按标记算的，不是按对象。"""
    path = _make_project(str(tmp_path / 'project.prism'))
    before = _fingerprint(path)
    io = SQLiteIO(path, view.scene, readonly=True)
    assert io.connection is not None
    io._close_connection()

    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        io.connection
    assert _fingerprint(path) == before


def test_an_explicit_reestablish_is_still_allowed(view, tmp_path):
    """需要新连接时**明说**：`_establish_connection()` 仍然能用。

    守卫只挡"偷偷复活"，不挡"我要一个新的"。
    """
    path = _make_project(str(tmp_path / 'project.prism'))
    io = SQLiteIO(path, view.scene, readonly=False)
    io.connection
    io._close_connection()

    io._establish_connection()
    assert io.connection is not None
    assert io.fetchone('SELECT COUNT(*) FROM board_notes')[0] == 1
    io._close_connection()


# ── 只读工程：任何写路径都要先问许可 ──────────────────────────────

def test_write_board_extras_refuses_a_readonly_project(view, tmp_path):
    """``write_board_extras()`` 自己写表，它必须和 ``write()`` 一样被守卫。

    事故背景：它绕过了 ``write()`` 里的只读守卫，笔记/附件/页面清单/
    素材记录都能被写进一个"只读打开"的工程。
    """
    path = _make_project(str(tmp_path / 'project.prism'))
    before = _fingerprint(path)

    # 走真实的只读打开流程：先请求只读，再读
    request_readonly_project(view.scene, path)
    reader = SQLiteIO(path, view.scene, readonly=True)
    reader.read()
    reader._close_connection()
    assert view.scene.readonly_project, '只读标记没有生效'

    writer = SQLiteIO(path, view.scene, readonly=False)
    with pytest.raises(PrismFileIOError, match='只读'):
        writer.write_board_extras()

    assert _fingerprint(path) == before, '只读工程被写回了'
    assert _write_traces(str(tmp_path)) == [], '只读工程留下了写痕迹'


def test_write_board_extras_still_writes_a_normal_project(view, tmp_path):
    """守卫不能把正常的保存也拦掉（这一条是给上一个测试兜底的）。"""
    path = str(tmp_path / 'normal.prism')
    writer = SQLiteIO(path, view.scene, create_new=True)
    writer.write()
    writer._close_connection()

    view.scene.note_html = '<p>hello</p>'
    writer = SQLiteIO(path, view.scene, readonly=False)
    writer.write_board_extras()
    assert writer.fetchone('SELECT html FROM board_notes WHERE board_id=0')[0] \
        == '<p>hello</p>'
    writer._close_connection()


def test_saving_as_a_new_file_is_not_blocked(view, tmp_path):
    """只读工程可以"另存为"到别的文件——许可只针对原来那个文件。"""
    source = _make_project(str(tmp_path / 'source.prism'))
    request_readonly_project(view.scene, source)
    reader = SQLiteIO(source, view.scene, readonly=True)
    reader.read()
    reader._close_connection()

    target = str(tmp_path / 'copy.prism')
    writer = SQLiteIO(target, view.scene, create_new=True)
    writer.write()
    writer._close_connection()
    assert os.path.exists(target)
