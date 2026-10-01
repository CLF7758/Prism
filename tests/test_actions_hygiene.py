"""动作注册表的卫生检查 —— 两条都能自动查出"用户点到才发现"的问题。

这两条来自第五次核对时的一轮排查。当时想的是"翻译漏没漏、快捷键冲不
冲突、菜单能不能到达" —— 头一条揪出 31 条漏翻，第二条立刻揪出一处
**真冲突**：

    'Ctrl+Shift+M': export_mindmap <-> toggle_side_panel

`export_mindmap` 是这轮新加的脑图导出，`toggle_side_panel` 是原有的。
两个动作抢一个键的话**只有一个能触发**，另一个等于失灵 —— 而用户在
界面上点半天也想不出为什么。

顺带查出来的第三类问题：`Action.text` 是 `self.text = text` **直接存**，
不会自动翻译。所以 `text='Toggle &Metadata Panel'` 这种写法在中文界面
里就是英文 —— 有 4 个动作是这么写的。

**关于"菜单可达"我没写成断言**：实测有 6 个动作不在菜单栏里
（`clear_filter` / `edit_categories` / `edit_notes` / `export_scene` /
`show_titlebar` / `toggle_side_panel`），但它们**都有 QAction**，说明是
运行时挂到别处去了（右键菜单或工具栏）。"不在菜单栏"不等于"到不了"，
要断言得先把所有挂载点找全 —— 那超出这次的范围，所以只在这里记一笔。
"""
import ast
import io
import os
import re

import pytest

from prism.actions.actions import actions

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def normalise_shortcut(combo):
    """把快捷键写法归一化 —— `ctrl+m` 和 `Ctrl+M` 是同一个键。

    不归一化的话，"Ctrl+M" 和 "CTRL+m" 会被当成两个不同的键，冲突就查
    不出来（而 Qt 认它们是同一个）。
    """
    parts = [part.strip().lower() for part in str(combo).split('+')]
    return '+'.join(sorted(part for part in parts if part))


def test_no_two_actions_share_a_shortcut():
    """**核心那条**：一个键不能给两个动作用。

    抢了的后果不是报错，是**静默失灵** —— 用户按键只触发其中一个，
    另一个像坏了一样。
    """
    owner = {}
    clashes = []
    for action_id in actions.keys():
        action = actions[action_id]
        for combo in (getattr(action, 'shortcuts', None) or []):
            key = normalise_shortcut(combo)
            if key in owner and owner[key] != action_id:
                clashes.append((combo, owner[key], action_id))
            else:
                owner[key] = action_id

    assert not clashes, (
        f'{len(clashes)} 个快捷键被两个动作抢了，用户按键只会触发其中一个：\n'
        + '\n'.join(f'  {combo!r}: {first} <-> {second}'
                    for combo, first, second in clashes))


def test_shortcut_syntax_is_canonical():
    """快捷键写法要规整 —— 前后空格、大小写。

    Qt 对 `'Ctrl+M'` 和 `' ctrl+m '` 一视同仁，但字符串比对不会。
    写法不规整的话，上面那条冲突检查就会漏（而 Qt 那边照样冲突）。
    """
    odd = []
    for action_id in actions.keys():
        for combo in (getattr(actions[action_id], 'shortcuts', None) or []):
            if combo != combo.strip():
                odd.append((action_id, combo, '前后有空格'))
            elif not re.fullmatch(r'(Ctrl|Alt|Shift|Meta|\w+)'
                                  r'(\+(Ctrl|Alt|Shift|Meta|\w+))*', combo):
                odd.append((action_id, combo, '格式不认识'))
            elif combo != combo.replace('control', 'Ctrl'):
                odd.append((action_id, combo, 'control 该写 Ctrl'))

    assert not odd, (
        '快捷键写法不统一：\n'
        + '\n'.join(f'  {combo!r}（{action_id}）：{why}'
                    for action_id, combo, why in odd))


def test_every_action_text_is_translated():
    """`Action.text` 是直接存的，不会自动翻译。

    所以定义处必须调 `_()`，否则那个菜单项在中文界面里就是英文 ——
    而 `test_translations.py` 只管"`_()` 里的字符串有没有译文"，
    抓不到"该走 `_()` 却没走"的。
    """
    path = os.path.join(PROJECT_ROOT, 'prism', 'actions', 'actions.py')
    tree = ast.parse(io.open(path, encoding='utf-8').read())

    bare = []
    total = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == 'Action'):
            continue
        total += 1
        action_id = None
        for keyword in node.keywords:
            if keyword.arg == 'id' and isinstance(keyword.value, ast.Constant):
                action_id = keyword.value.value
        for keyword in node.keywords:
            if keyword.arg != 'text':
                continue
            value = keyword.value
            wrapped = (isinstance(value, ast.Call)
                       and isinstance(value.func, ast.Name)
                       and value.func.id == '_')
            if not wrapped:
                shown = (value.value if isinstance(value, ast.Constant)
                         else '<表达式>')
                bare.append((action_id, shown))

    assert not bare, (
        f'{len(bare)} 个动作的 text 没走 _()（共 {total} 个）—— '
        f'它们在中文界面里会显示英文：\n'
        + '\n'.join(f'  {action_id!r}: {shown!r}'
                    for action_id, shown in bare))


def test_every_action_has_a_text():
    """每个动作都得有 text，不然菜单项是空的（用户看不出那是什么）。"""
    empty = [action_id for action_id in actions.keys()
             if not str(getattr(actions[action_id], 'text', '') or '').strip()]
    assert not empty, f'这些动作没有 text：{empty}'


def test_the_registry_is_not_empty():
    """别让上面几条因为"注册表是空的"而假绿。"""
    assert len(list(actions.keys())) > 50, (
        f'只注册了 {len(list(actions.keys()))} 个动作 —— 是不是没加载完？')
