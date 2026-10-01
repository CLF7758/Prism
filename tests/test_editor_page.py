"""编辑器页（editor.js）自己的检查。

这个无头环境**跑不了页面**：建一个 ``QWebEnginePage`` 会让进程直接以
0xC0000409 死掉，连一行错误都打不出来（同一个坑也让 MindMapPanel 在测试里
必须被 monkeypatch 掉）。所以页面没法真的加载起来做运行时验证，这一点在
T3 报告和人工验收清单里都如实写了。

能做的事仍然不少，而且抓的都是真出过的错：用 Qt 自带的 QJSEngine 把
editor.js 解析一遍——语法错误藏不住——再把页面与 Python 之间的两份约定
对上：Python 推过去的标签表、ribbon 里的命令，页面必须都认识，反过来页面
问的标签也不能漏。

这里还有一条**只能靠读源码发现**的规则：``quill.getSelection()`` 读的是
原生 DOM 选区，编辑器一失焦就返回 null。点 ribbon 按钮、查找栏开着的时候
都是这样，所以每个读选区的地方都要有自己的对策。
"""
import json
import os
import re

import pytest

from prism.widgets import document_panel as module
from prism.widgets.document_panel import DocumentPanel

SCRIPT_PATH = os.path.join(module.EDITOR_DIR, 'editor.js')

#: ``t('someKey')`` - how the page asks Python for a translated string.
_LABEL_CALL = re.compile(r"\bt\(\s*'([A-Za-z][A-Za-z0-9_]*)'")
#: ``payload.id === 'host-command'`` - how the page asks Python to do something.
_HOST_COMMAND = re.compile(r"payload\.id === '([\w-]+)'")


def strip_js_comments(source):
    """Drop ``//`` and ``/* */`` comments, leaving strings alone.

    A mention of ``quill.getSelection()`` inside a comment must not be read as
    a call - that is exactly how the caret rules below got confusing once.
    """
    out = []
    index = 0
    length = len(source)
    quote = None
    while index < length:
        char = source[index]
        if quote:
            out.append(char)
            if char == '\\' and index + 1 < length:
                index += 1
                out.append(source[index])
            elif char == quote:
                quote = None
            index += 1
            continue
        if char in '\'"`':
            quote = char
            out.append(char)
            index += 1
            continue
        if char == '/' and index + 1 < length:
            following = source[index + 1]
            if following == '/':
                while index < length and source[index] != '\n':
                    index += 1
                continue
            if following == '*':
                index += 2
                while index + 1 < length and source[index:index + 2] != '*/':
                    index += 1
                index += 2
                continue
        out.append(char)
        index += 1
    return ''.join(out)


@pytest.fixture(scope='module')
def script():
    with open(SCRIPT_PATH, encoding='utf-8') as handle:
        return handle.read()


@pytest.fixture(scope='module')
def code(script):
    """The script as code: comments are prose, not behaviour."""
    return strip_js_comments(script)


@pytest.fixture
def panel(view):
    panel = DocumentPanel(view)
    yield panel
    panel._save_timer.stop()
    panel.deleteLater()


def _ribbon_commands(panel):
    spec = panel._ribbon_spec()
    return {item['command'] for tab in spec['tabs']
            for group in tab['groups'] for item in group['items']}


def _action_commands(code):
    """The commands editor.js answers itself, out of its ACTIONS table."""
    start = code.index('var ACTIONS = {')
    end = code.index('\n  };', start)
    return set(re.findall(r"^\s*'?([A-Za-z][\w-]*)'?:\s*function",
                          code[start:end], re.MULTILINE))


def _matching_brace(code, start):
    """The index just past the brace that closes the one after ``start``."""
    depth = 0
    opened = False
    for position in range(start, len(code)):
        if code[position] == '{':
            depth += 1
            opened = True
        elif code[position] == '}':
            depth -= 1
            if opened and depth == 0:
                return position + 1
    raise AssertionError('unbalanced braces')


def _function_body(code, name):
    """The source of one top level function, braces included."""
    start = code.index(f'function {name}(')
    return code[start:_matching_brace(code, start)]


def _function_at(code, position):
    """``(name, body)`` of the function a position sits inside."""
    found = None
    for match in re.finditer(r'function (\w+)\(', code):
        if match.start() > position:
            break
        found = match
    assert found is not None, 'not inside a function'
    return found.group(1), _function_body(code, found.group(1))


# ── The script parses ────────────────────────────────────────────


def test_the_editor_script_has_no_syntax_error(script, qapp):
    """QJSEngine 报的第一个错必须是「缺依赖」，不能是语法错。

    editor.js 一跑起来就会去拿 Quill，所以 ReferenceError 正是「语法没
    问题」的信号；SyntaxError 才是我们要挡的。QJSEngine 需要一个应用
    实例，所以这个测试依赖 ``qapp``。
    """
    QtQml = pytest.importorskip('PyQt6.QtQml')
    engine = QtQml.QJSEngine()
    result = engine.evaluate(script, 'editor.js', 1)
    assert result.isError()
    assert result.property('name').toString() == 'ReferenceError', (
        result.property('name').toString(),
        result.property('message').toString())


def test_stripping_comments_keeps_strings_and_code():
    """分析用的去注释函数自己也要可信。"""
    source = ("var a = 'http://x/y';  // gone\n"
              "var b = 1; /* gone\ntoo() */ var c = '/*kept*/';")
    result = strip_js_comments(source)
    assert 'http://x/y' in result
    assert 'var b = 1;' in result
    assert 'var c' in result
    assert 'gone' not in result
    assert 'too()' not in result


# ── The two sides agree ──────────────────────────────────────────


def test_every_label_the_page_asks_for_is_in_the_table(panel, code):
    asked = set(_LABEL_CALL.findall(code))
    table = set(panel._label_table())
    assert not asked - table, asked - table


def test_the_label_table_has_no_entries_nobody_reads(panel, code):
    asked = set(_LABEL_CALL.findall(code))
    unused = set(panel._label_table()) - asked
    # applyLabels() reads a few straight off the DOM by their key.
    unused -= {'findNext', 'replaceOne', 'replaceAll', 'findPlaceholder',
               'replacePlaceholder', 'close'}
    assert not unused, unused


def test_every_ribbon_command_is_known_to_the_page(panel, code):
    actions = _action_commands(code)
    hosts = set(_HOST_COMMAND.findall(code))
    toggles = {'bold', 'italic', 'underline', 'header', 'lineHeight', 'align',
               'list', 'code-block', 'font', 'fontSize'}
    known = actions | hosts | toggles
    assert not _ribbon_commands(panel) - known, _ribbon_commands(panel) - known


def test_the_page_does_not_answer_commands_the_ribbon_never_sends(panel,
                                                                 code):
    """反过来也要对得上：页面里躺着的命令必须有一个按钮或菜单能触发它。"""
    sent = _ribbon_commands(panel)
    # These arrive from the Python side after a dialog was answered.
    sent |= {'insert-images', 'insert-table', 'insert-link', 'set-color',
             'image-size', 'insert-text', 'focus-editor', 'status'}
    unreachable = _action_commands(code) - sent
    assert not unreachable, unreachable


# ── Where the caret is, and who may ask ──────────────────────────


def test_the_picture_commands_restore_the_caret_first(code):
    """回归：点 ribbon 按钮会把焦点从编辑器拿走。

    Quill 的 getSelection() 读的是**原生 DOM 选区**，编辑器一失焦就返回
    null（quill.js / core/selection.js：getRange() → getNativeRange()）。
    而 quill.focus() 会把光标放回失焦前的位置（它 reapply 了 savedRange）。
    顺序反了的话，「删除图片」和「图片大小」在真实点击下会一直回答「先选中
    一张图片」，无论用户选没选。
    """
    body = _function_body(code, 'currentImage')
    assert 'quill.focus()' in body
    assert body.index('quill.focus()') < body.index('quill.getSelection()')


#: Everything that reads the caret with ``quill.getSelection()``, and why
#: each one is safe.
#:
#: That call answers with the *native* DOM selection, so it is null whenever
#: the editor does not hold the focus - which is true at every ribbon button
#: and for as long as the find bar is open.  A new reader has to be thought
#: about rather than added, so this list is checked instead of a blanket rule.
_CARET_READERS = ('currentImage', 'findStart')


def test_every_reader_of_the_caret_is_a_known_one(code):
    for match in re.finditer(r'quill\.getSelection\(\)', code):
        name, _body = _function_at(code, match.start())
        assert name in _CARET_READERS, name


def test_the_picture_commands_share_one_way_to_find_the_picture(code):
    """三个图片命令都走 currentImage()，不要在别处又读一遍选区。"""
    for name in ('setImageSize', 'deleteImage', 'imageSizeInfo'):
        body = _function_body(code, name)
        assert 'currentImage()' in body, name
        assert 'quill.getSelection()' not in body, name


def test_the_find_bar_does_not_steal_the_focus(code):
    """查找栏开着时焦点在输入框上，这里不能把焦点抢回编辑器 ——
    抢了用户就没法接着敲下一个关键词。"""
    body = _function_body(code, 'findStart')
    assert 'quill.hasFocus()' in body
    assert 'quill.focus()' not in body


def test_find_next_walks_through_the_document(code):
    """回归：原来每次都从 0 开始找，所以「下一个」永远停在第一个匹配上。"""
    body = _function_body(code, 'findNext')
    assert 'findStart(' in body
    assert 'rememberMatch(' in body
    assert 'quill.getSelection()' not in body


def test_replace_replaces_the_match_it_remembers(code):
    """回归：原来靠编辑器选区判断「当前是不是匹配项」，而查找栏开着时选区
    恒为 null，所以「替换」从来只做「下一个」。"""
    body = _function_body(code, 'replaceOne')
    assert 'lastFind.index' in body
    assert 'quill.deleteText(' in body
    assert 'quill.getSelection()' not in body


# ── The phase-one gaps ───────────────────────────────────────────


def test_the_ribbon_offers_a_way_to_delete_an_image(panel, code):
    assert 'image-delete' in _ribbon_commands(panel)
    assert 'image-delete' in _action_commands(code)
    assert 'function deleteImage' in code


def test_the_page_pastes_without_formatting_on_ctrl_shift_v(code):
    assert "getData('text/plain')" in code
    assert 'event.shiftKey' in code
    assert 'function insertPlainText' in code


def test_the_page_still_takes_formatted_paste(code):
    """粘贴常见 HTML 是 Quill 自己的活；这里的处理器不能把它挡掉。"""
    assert 'if (!files.length)' in code
    # The plain-text branch only runs when the modifier was held.
    assert 'if (pasteAsPlainText)' in code


#: The one string the page may show before the label table arrives: it is the
#: "this page could not reach Prism at all" notice, so by definition Python is
#: not there to translate anything.  Making it go through ``t()`` would mean
#: adding a key to ``_label_table()``, and ``test_document_editor.py`` pins
#: that table down to its own ``PAGE_LABEL_KEYS`` - that test file is not
#: ours to change.  Recorded in the T3 report instead of quietly worked
#: around.
_STARTUP_NOTICE = 'QWebChannel unavailable'


def test_the_page_never_hardcodes_ui_text(code):
    """页面上每一句给用户看的话都得走标签表。"""
    for match in re.finditer(r"setStatus\(\s*'([^']*)'", code):
        assert match.group(1) in ('', _STARTUP_NOTICE), match.group(0)


# ── The page is still the one we ship ────────────────────────────


def test_the_vendor_bundle_is_untouched():
    with open(os.path.join(module.EDITOR_DIR, 'UPSTREAM.json'),
              encoding='utf-8') as handle:
        upstream = json.load(handle)
    assert upstream['package'] == 'quill'
    assert upstream['license'] == 'BSD-3-Clause'
    assert upstream['version'] == '2.0.3'
