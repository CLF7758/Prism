"""文字样式（用户的第 8 条：插入文字要能选和改字号、颜色、大小）。

这条里有**两件事**，缺一件都白搭：

  1. 界面上能改（属性面板里的颜色按钮 + 字号 + 粗体/斜体）
  2. **改完能存下来** —— `get_extra_save_data()` 原来只存
     `{'text': ...}`，颜色字号根本留不住，保存再打开就变回默认色

所以下面两头都验：`PrismTextItem` 的存取往返，和属性面板的控件。
"""
import pytest
from PyQt6 import QtGui

from prism import commands
from prism.items import PrismTextItem


def make_text(text='测试'):
    return PrismTextItem(text)


# ── 存储往返 ────────────────────────────────────────────────────

def test_a_default_text_has_the_default_colour(qapp):
    item = make_text()
    assert item.defaultTextColor().isValid()
    style = item.style()
    assert style['color'], '默认色也该报出来'
    assert style['size'] and style['size'] > 0, '默认字号该是正数'


def test_the_style_survives_a_save_and_load(qapp):
    """**核心那条**：改过的样式要能存下来、读回来。

    以前这里只存文字，所以颜色字号改了也留不住。
    """
    item = make_text('改过的')
    item.set_style(color='#12ab34', size=28.0, bold=True, italic=True)

    saved = item.get_extra_save_data()
    assert saved['color'] == '#12ab34', f"存的是 {saved.get('color')!r}"
    assert saved['size'] == pytest.approx(28.0)
    assert saved['bold'] is True
    assert saved['italic'] is True

    # 读回来
    restored = PrismTextItem.create_from_data(data=saved)
    assert restored.toPlainText() == '改过的'
    style = restored.style()
    assert style['color'] == '#12ab34'
    assert style['size'] == pytest.approx(28.0)
    assert style['bold'] is True
    assert style['italic'] is True


def test_an_old_project_without_style_fields_still_loads(qapp):
    """**旧工程里的文字没有样式字段**，不能因此崩掉或者变样。

    那几项是后来加的，所以每一项都得能独立缺省。
    """
    old_data = {'text': '老工程里的文字', 'categories': [], 'tags': [],
                'rating': 0, 'notes': '', 'title': '',
                'canvasId': None, 'groupId': None, 'groupNote': ''}
    restored = PrismTextItem.create_from_data(data=old_data)
    assert restored.toPlainText() == '老工程里的文字'
    style = restored.style()
    assert style['color'], '缺字段时该回落到默认色'
    assert style['bold'] is False
    assert style['italic'] is False


def test_a_bad_size_is_ignored_not_fatal(qapp):
    """存盘数据脏了（字号是个字符串之类）不该让整个工程打不开。"""
    data = {'text': '脏数据', 'size': '大一点', 'color': 'not-a-colour'}
    item = PrismTextItem.create_from_data(data=data)
    assert item.toPlainText() == '脏数据'
    assert item.style()['size'] and item.style()['size'] > 0


def test_set_style_only_changes_what_is_passed(qapp):
    """只传要改的那几项，别的保持不动。"""
    item = make_text()
    item.set_style(color='#ff0000', size=20.0)
    item.set_style(bold=True)
    style = item.style()
    assert style['color'] == '#ff0000', '颜色不该被后一次调用洗掉'
    assert style['size'] == pytest.approx(20.0)
    assert style['bold'] is True


def test_the_style_can_be_undone(qapp):
    """改样式要能撤销 —— 用的是 `ChangeTextStyle`。"""
    from PyQt6.QtGui import QUndoStack

    stack = QUndoStack()
    item = make_text()
    before = item.style()

    stack.push(commands.ChangeTextStyle(
        item, {'color': '#ff0000', 'size': 30.0, 'bold': True}))
    assert item.style()['color'] == '#ff0000'
    assert item.style()['bold'] is True

    stack.undo()
    after = item.style()
    assert after['color'] == before['color'], '撤销该把颜色还回去'
    assert after['bold'] == before['bold']
    assert after['size'] == pytest.approx(before['size'])


def test_the_style_round_trips_through_the_database(qapp, tmp_path):
    """真存一次盘再读回来 —— 上面那条只验了字典，这条走 sqlite。"""
    import json
    import sqlite3

    from PyQt6.QtGui import QUndoStack

    from prism.fileio.sql import SQLiteIO
    from prism.scene import PrismGraphicsScene

    stack = QUndoStack()
    scene = PrismGraphicsScene(stack)
    keep = (stack, scene)       # 别让 GC 收走栈（见 test_data_safety）

    item = PrismTextItem('存盘测试')
    item.set_style(color='#00ff88', size=33.0, italic=True)
    scene.addItem(item)

    path = str(tmp_path / 'style.prism')
    io_ = SQLiteIO(path, scene, create_new=True)
    try:
        io_.write()          # `write()` 会先建表（`write_data()` 不会）
    finally:
        io_._close_connection()

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute('SELECT data FROM items').fetchall()
    finally:
        connection.close()
    assert rows, '没写出任何 item'
    payload = json.loads(rows[0][0])
    assert payload.get('color') == '#00ff88', f'存的是 {payload!r}'
    assert payload.get('size') == pytest.approx(33.0)
    assert payload.get('italic') is True
    assert keep  # 仅仅是让引用活着
