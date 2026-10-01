"""检查代码里 import 的第三方包是否都登记在依赖清单里。

任务书 v5 §11 要求「每加入一个依赖，都必须同步检查 pyproject.toml 依赖」。
这个脚本把两件事对起来：

    代码实际 import 了什么          <->   pyproject.toml 声明了什么

两边不一致就报出来，退出码非零。加依赖时忘了改 pyproject.toml、或者删了
依赖但代码还在 import，都会在这里暴露，而不是等到打包时才炸。

用法::

    .\\.venv\\Scripts\\python -u tools\\check_dependencies.py

退出码：0 = 一致，1 = 有差异。
"""
import ast
import collections
import io
import os
import sys

try:
    import tomllib
except ModuleNotFoundError:                      # Python 3.10 及更早
    tomllib = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: import 用的名字 -> PyPI 发行包名。只列两者不一致的。
#: 比如代码写 ``import docx``，但包名是 ``python-docx``。
IMPORT_TO_DISTRIBUTION = {
    'OpenImageIO': 'openimageio',
    'PIL': 'pillow',
    'av': 'av',
    'docx': 'python-docx',
    'exif': 'exif',
    'lxml': 'lxml',
    'numpy': 'numpy',
    'plum': 'plum-py',
    'PyQt6': 'pyqt6',
    'rpack': 'rectangle-packer',
}

#: 只在这些目录里找 import。测试和工具允许用额外的东西。
SOURCE_DIRS = ('prism',)

#: 项目自己的包，不算第三方。
OWN_PACKAGES = {'prism'}


def normalise(name):
    """把依赖声明归一化成包名。

    要去掉三样东西，少去一样比对就会错（第一版就漏了版本约束，
    于是 ``'av>=19,<20'`` 被当成包名，全表比对失败）：

    * extras：``foo[bar]`` -> ``foo``
    * 版本约束：``av>=19,<20`` -> ``av``
    * 环境标记：``foo; python_version > '3.11'`` -> ``foo``

    最后统一小写，并把 ``-`` 和 ``_`` 视作相同（PyPI 的规则）。
    """
    name = name.split('[')[0]
    for separator in ('>=', '<=', '==', '!=', '~=', '===', '>', '<', ';'):
        name = name.split(separator)[0]
    return name.strip().lower().replace('_', '-')


#: 有些包不需要直接 import、或者 import 名和发行包名对不上脚本按顶层模块名
#: 做的推断。在这里说明清楚，两边（缺声明 / 多余声明）都放行。
INDIRECT = {
    'pyqt6-qt6': 'PyQt6 的 Qt 运行时，由 PyQt6 带入（代码不直接 import）',
    'pyqt6-webengine-qt6': 'PyQt6-WebEngine 的 Qt 运行时',
    'pyqt6-sip': 'PyQt6 的绑定层',
    'pyqt6-webengine': (
        '代码写的是 from PyQt6 import QtWebEngineWidgets —— '
        '顶层 import 名仍是 PyQt6，脚本按顶层名推断时认不出它'),
    'plum-py': (
        'exif 的解析后端，但 prism/fileio/image.py 直接 import plum '
        '来捕获 plum.exceptions.UnpackError（所以它会出现在"代码用了"里，'
        '却不在 pyproject.toml 里 —— 这是有意的）'),
}


def imported_modules():
    """扫描源码，返回 {import 名: [用到它的文件]}。"""
    found = collections.defaultdict(set)
    stdlib = set(getattr(sys, 'stdlib_module_names', ()))

    for source_dir in SOURCE_DIRS:
        base = os.path.join(ROOT, source_dir)
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                if not filename.endswith('.py'):
                    continue
                path = os.path.join(dirpath, filename)
                rel = os.path.relpath(path, ROOT)
                try:
                    tree = ast.parse(io.open(path, encoding='utf-8').read())
                except (SyntaxError, UnicodeDecodeError) as error:
                    print(f'  跳过 {rel}：{error}')
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            top = alias.name.split('.')[0]
                            if top in stdlib or top in OWN_PACKAGES:
                                continue
                            found[top].add(rel)
                    elif isinstance(node, ast.ImportFrom):
                        if node.level:
                            continue            # 相对导入，是 prism 自己
                        if not node.module:
                            continue
                        top = node.module.split('.')[0]
                        if top in stdlib or top in OWN_PACKAGES:
                            continue
                        found[top].add(rel)
    return found


def declared_distributions():
    """读 pyproject.toml，返回必需依赖和可选依赖的包名集合。"""
    if tomllib is None:
        raise SystemExit('需要 Python 3.11+ 才能读 TOML（当前 %s）'
                         % sys.version.split()[0])
    path = os.path.join(ROOT, 'pyproject.toml')
    with io.open(path, 'rb') as handle:
        data = tomllib.load(handle)
    project = data.get('project', {})
    required = {normalise(spec) for spec in project.get('dependencies', [])}
    optional = set()
    for specs in project.get('optional-dependencies', {}).values():
        optional.update(normalise(spec) for spec in specs)
    return required, optional


def main():
    found = imported_modules()
    required, optional = declared_distributions()
    declared = required | optional

    print(f'扫描 {SOURCE_DIRS[0]}/ 发现 {len(found)} 个第三方 import')
    print(f'pyproject.toml 声明 {len(required)} 个必需 + {len(optional)} 个可选')
    print()

    problems = []

    print('=== 代码用到，但 pyproject.toml 没声明 ===')
    missing = []
    for module in sorted(found):
        distribution = IMPORT_TO_DISTRIBUTION.get(module, normalise(module))
        if distribution in declared or distribution in INDIRECT:
            continue
        missing.append((module, distribution, found[module]))
    if missing:
        problems.append('缺声明')
        for module, distribution, files in missing:
            shown = ', '.join(sorted(files)[:3])
            more = f' 等 {len(files)} 个文件' if len(files) > 3 else ''
            print(f'  import {module:14s} -> 发行包 {distribution:18s} '
                  f'({shown}{more})')
        print()
        print('  修法：加进 pyproject.toml 的 dependencies，'
              '并在 DEPENDENCIES.md 里补一条记录')
    else:
        print('  （无）')
    print()

    print('=== pyproject.toml 声明了，但代码里找不到 import ===')
    unused = sorted(d for d in declared
                    if d not in INDIRECT
                    and not any(
                        IMPORT_TO_DISTRIBUTION.get(m, normalise(m)) == d
                        for m in found))
    if unused:
        for distribution in unused:
            problems.append('多余声明')
            print(f'  {distribution:22s} —— 代码里没有 import 它')
    else:
        print('  （无）')
    print()

    print('=== 间接依赖（已说明，不算问题）===')
    for distribution, note in sorted(INDIRECT.items()):
        if distribution in declared or distribution in (
                IMPORT_TO_DISTRIBUTION.get(m, normalise(m))
                for m in found):
            print(f'  {distribution:22s} {note}')
    print()

    if problems:
        print(f'[FAIL] 有 {len(problems)} 类问题，见上')
        return 1
    print('[PASS] 代码的 import 和 pyproject.toml 一致')
    print('       （DEPENDENCIES.md 是给人看的详细记录，'
          '改动依赖时记得同步）')
    return 0


if __name__ == '__main__':
    sys.exit(main())
