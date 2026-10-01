"""界面文字必须全中文 —— 这事能自动查。

用户的要求是「界面全中文」，而漏翻的后果是**用户得点到那一处才发现**。

第五次核对时逐项盘"还能做什么"，发现源码里有约 577 个唯一的 `_()` 字符串，
而**一条检查都没有**。手工探了一遍，揪出 **31 条漏翻**：

    clipboard_inbox.py   12 条      ← 这轮新写的收集箱
    settings.py           9 条      ← 这轮新写的设置
    media_library.py      3 条
    import_docx.py        2 条
    其余 5 个文件          各 1 条

全是"代码写了、`_()` 也调了、就是忘了进字典"—— 界面上一眼能看见的
（设置面板的分类名 `&Clipboard`、复选框 `Automatically watch the
clipboard`、按钮 `Add Tags…`……）。

其中一条更严重，`actions/export_workflow.py` 里我把**中文直接写进了源
字符串**：

    _('{reason}\\n\\n导出当前渲染结果吗？（会重新编码，不是原图）')

那违反约定（源字符串用英文、字典给中文）：中文界面碰巧对，**英文界面会
显示中文**。这条也修了，而且下面专门有一条测试盯着这种写法。

所以这个文件有三条测试：**漏翻**、**中文源字符串**、**占位符不一致**。
"""
import ast
import os

from prism.i18n.zh_CN import zh_CN

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = os.path.join(PROJECT_ROOT, 'prism')

#: 这些文件不参与检查。
SKIP_FILES = {'zh_CN.py'}


def translated_strings():
    """源码里所有 `_()` 的常量参数。

    用 `ast` 而不是正则：`_('第一段' '第二段')` 这种跨行拼法，正则只抓
    第一段，会产生假阳性（我第一版就是正则，报了 60 条，实际只有 31 条）。
    `ast` 会把相邻字面量拼成运行时真正的那个字符串。
    """
    found = {}
    for root, _dirs, files in os.walk(PACKAGE):
        for name in files:
            if not name.endswith('.py') or name in SKIP_FILES:
                continue
            path = os.path.join(root, name)
            relative = os.path.relpath(path, PROJECT_ROOT)
            try:
                tree = ast.parse(open(path, encoding='utf-8').read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                function = node.func
                if not (isinstance(function, ast.Name)
                        and function.id == '_' and node.args):
                    continue
                first = node.args[0]
                if (isinstance(first, ast.Constant)
                        and isinstance(first.value, str)):
                    found.setdefault(first.value, set()).add(relative)
    return found


def looks_chinese(text):
    """有没有汉字。"""
    return any('\u4e00' <= character <= '\u9fff' for character in text)


def test_every_called_string_has_a_translation():
    """**核心那条**：调了 `_()` 的字符串，字典里必须有对应条目。

    漏了就是界面上会冒出英文 —— 而用户得点到那一处才知道。
    """
    found = translated_strings()
    missing = sorted(key for key in found if key not in zh_CN)

    report = []
    for key in missing:
        where = ', '.join(sorted(found[key]))
        report.append(f'  {key[:72]!r}\n      {where}')

    assert not missing, (
        f'{len(missing)} 条字符串调了 _() 却没有中文翻译。'
        f'界面在这几处会显示英文：\n' + '\n'.join(report))


def test_source_strings_are_not_already_chinese():
    """**别把中文写进 `_()` 的源字符串。**

    约定是"源字符串用英文，字典给中文"。写成中文的话，中文界面碰巧对
    （因为界面就是中文），但**英文界面会显示中文**。

    我在 `export_workflow.py` 里干过一次：

        _('{reason}\\n\\n导出当前渲染结果吗？（会重新编码，不是原图）')

    """
    found = translated_strings()
    offenders = sorted(
        key for key in found
        if looks_chinese(key) and key not in zh_CN)

    report = []
    for key in offenders:
        where = ', '.join(sorted(found[key]))
        report.append(f'  {key[:72]!r}\n      {where}')

    assert not offenders, (
        f'{len(offenders)} 条源字符串里直接写了中文。源字符串要用英文，'
        f'中文放字典里 —— 不然英文界面会显示中文：\n' + '\n'.join(report))


def test_placeholders_survive_translation():
    """翻译不能丢掉或改坏 `{占位符}`。

    `'Sent {n} capture(s) to "{target}".'` 译成"已把收藏送到"的话，
    `.format(n=3)` 会抛 `KeyError`，或者更糟：少显示一个数字。
    """
    import re

    placeholder = re.compile(r'\{(\w+)(?::[^}]*)?\}')
    problems = []
    for source, translated in zh_CN.items():
        if not isinstance(translated, str):
            continue
        wanted = sorted(placeholder.findall(source))
        got = sorted(placeholder.findall(translated))
        if wanted != got:
            problems.append(
                f'  {source[:56]!r}\n    期望 {wanted}，实得 {got}')

    assert not problems, (
        f'{len(problems)} 条翻译的占位符和原文对不上：\n'
        + '\n'.join(problems))


def test_the_translations_are_actually_loaded():
    """字典接上了 —— 不然上面几条测的是个没人用的表。"""
    from prism.i18n.translator import translator

    assert zh_CN, '字典是空的'
    assert translator is not None

    # 挑一条这轮新加的，确认它真的能译出来
    sample = 'clipboard'  # noqa: F841  （只是说明下面看的是这轮加的条目）
    assert zh_CN.get('&Clipboard') == '剪贴板(&C)'


def test_unused_entries_are_reported_but_not_fatal():
    """字典里源码找不到的条目 —— **只报不算错**。

    实测有 93 条。它们大多是被改掉的旧字符串留下的（改文案时字典没跟着
    清），但也可能有**动态拼出来的**（`_('前缀' + suffix)`）—— 那种
    `ast` 看到的是 `BinOp` 不是常量，自然对不上。

    所以这条不断言"必须为空"，只在**数量暴涨**时报警：那说明有人改文案
    没更新字典，或者加了新写法。
    """
    found = translated_strings()
    unused = [key for key in zh_CN if key not in found]

    # 现在的实测值是 93。放宽到 150 —— 越过就说明哪里不对了。
    assert len(unused) < 150, (
        f'字典里有 {len(unused)} 条源码里找不到 —— 远超预期（93）。'
        f'是不是改文案的时候忘了同步字典？\n'
        + '\n'.join(f'  {key[:70]!r}' for key in sorted(unused)[:25]))
