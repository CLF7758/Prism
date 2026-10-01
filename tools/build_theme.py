"""从 ui_tokens 生成 prism/assets/theme.qss。

`tools/theme_template.qss` 是模板（含 `{token}` 占位），本脚本用
`prism.ui_tokens.qss_variables()` 填色，写出运行时要读的 theme.qss。

**为什么要有这一步**：规范 §15.3 要求"主题 Token 统一源导出为 Qt 样式和
Web CSS 变量"。QSS 没有变量，所以只能生成。运行时零改动 —— 读的还是
theme.qss，只是它现在由 Token 生成，不再手工维护。

改颜色请改 `prism/ui_tokens.py`；改结构请改 `tools/theme_template.qss`，
然后重跑本脚本。

用法::

    .\\.venv\\Scripts\\python tools\\build_theme.py            # 生成
    .\\.venv\\Scripts\\python tools\\build_theme.py --check     # 只校验，不写
    .\\.venv\\Scripts\\python tools\\build_theme.py --mode light
"""
import argparse
import io
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from prism.ui_tokens import (PLACEHOLDER, css_variables,  # noqa: E402
                             missing_placeholders, qss_variables)

TEMPLATE = os.path.join(REPO, 'tools', 'theme_template.qss')
TARGET = os.path.join(REPO, 'prism', 'assets', 'theme.qss')
WEB_TARGET = os.path.join(REPO, 'prism', 'assets', 'theme_tokens.css')

HEADER = """/*
 * Prism — 由 prism/ui_tokens.py 生成，请不要直接编辑这个文件。
 *
 * 改颜色  → prism/ui_tokens.py
 * 改结构  → tools/theme_template.qss
 * 然后跑 → .\\\\.venv\\\\Scripts\\\\python tools\\\\build_theme.py
 *
 * 依据：桌面/Prism-全套UI与交互开发规范-v1.md §4 / §5 / §6
 * 模式：%(mode)s
 */
"""


def render(mode='dark'):
    template = io.open(TEMPLATE, encoding='utf-8').read()
    missing = missing_placeholders(template, mode)
    if missing:
        raise SystemExit(
            '模板里有 %d 个 Token 不存在：%s\n'
            'Token 名不许自己发明（规范 §5.1），请先改 ui_tokens.py。'
            % (len(missing), ', '.join(missing)))
    variables = qss_variables(mode)
    body = PLACEHOLDER.sub(
        lambda m: variables[m.group(1)], template)
    leftovers = PLACEHOLDER.findall(body)
    if leftovers:
        raise SystemExit('还有占位没被替换：%s' % ', '.join(sorted(set(leftovers))))
    return HEADER % {'mode': mode} + body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true',
                        help='只校验，不写文件')
    parser.add_argument('--mode', default='dark',
                        choices=('dark', 'light'))
    parser.add_argument('--write-web', action='store_true',
                        help='顺带输出 WebEngine 用的 CSS 变量文件')
    args = parser.parse_args()

    qss = render(args.mode)
    print('模板  : %s' % os.path.relpath(TEMPLATE, REPO))
    print('模式  : %s' % args.mode)
    print('行数  : %d' % len(qss.splitlines()))

    if args.check:
        current = io.open(TARGET, encoding='utf-8').read()
        if current == qss:
            print('已是最新 —— 无需重新生成')
            return 0
        print('!! theme.qss 与 Token 不一致，需要重跑 build_theme.py')
        return 1

    io.open(TARGET, 'w', encoding='utf-8', newline='\n').write(qss)
    print('写出  : %s' % os.path.relpath(TARGET, REPO))

    if args.write_web:
        css = css_variables(args.mode) + '\n'
        io.open(WEB_TARGET, 'w', encoding='utf-8', newline='\n').write(css)
        print('写出  : %s' % os.path.relpath(WEB_TARGET, REPO))

    # 自检：生成结果里不该再有裸的旧配色
    for legacy in ('#0a84ff', '#1c1c1e', '#2c2c2e', 'rgba(245,245,247'):
        if legacy.lower() in qss.lower():
            print('!! 生成结果里还有旧配色 %s' % legacy)
            return 1
    print('自检  : 没有残留的旧配色')
    return 0


if __name__ == '__main__':
    sys.exit(main())
