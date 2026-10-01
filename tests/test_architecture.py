"""架构约束 —— 把任务书里那几句话变成能跑的测试。

任务书 §7 T10 的目标写得是**行为约束**而不是行数：

    目标不是强行把文件压到 400 行，而是让 View 中不再出现
    **数据库访问、文件格式细节和长业务流程**。

§2 又补了一句：

    领域服务不得依赖 Qt 控件。需要 Qt 信号、线程和进度时，使用单独的
    Qt 适配器包装纯业务服务。

这些话如果只写在文档里，下次有人顺手在界面文件里加一行 `import
sqlite3` 没人会拦。写成测试就能拦 —— 而且失败信息能直接告诉他该去哪找
服务，不用去翻文档。

**为什么这些约束值得钉住**：它们描述的是"这次重构要达到的状态"。状态会
被后来的改动慢慢侵蚀（一次一行，每次都有理由），而侵蚀完之后再想恢复
就没人知道原来的边界在哪了。
"""
import ast
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: 界面层不该出现的导入。左边是禁止的模块，右边是"该去哪找"。
FORBIDDEN_IN_UI = {
    'sqlite3': 'prism/fileio/sql.py（SQLiteIO）',
    'sqlar': 'prism/fileio/sql.py 里的附件读写',
}


def imports_of(path):
    """这个文件 import 了哪些顶层模块。"""
    with open(path, encoding='utf-8') as handle:
        tree = ast.parse(handle.read(), filename=path)
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                names.add(node.module.split('.')[0])
    return names


def modules_below(relative_dir):
    base = os.path.join(ROOT, relative_dir)
    for folder, _dirs, files in os.walk(base):
        if '__pycache__' in folder:
            continue
        for name in files:
            if name.endswith('.py'):
                yield os.path.join(folder, name)


# ── 界面层不碰数据库 ────────────────────────────────────────────

@pytest.mark.parametrize('forbidden,where_it_belongs',
                         sorted(FORBIDDEN_IN_UI.items()))
def test_the_view_does_not_touch_the_database(forbidden, where_it_belongs):
    """`view.py` 里不许出现数据库访问。

    任务书 §7 T10 明确列了这一条。真要读写数据库，该走
    `prism/fileio/sql.py`，不是在视图里开连接。
    """
    imports = imports_of(os.path.join(ROOT, 'prism', 'view.py'))
    assert forbidden not in imports, (
        f'prism/view.py 不该 import {forbidden!r} —— '
        f'数据库访问应该放在 {where_it_belongs}')


@pytest.mark.parametrize('forbidden', sorted(FORBIDDEN_IN_UI))
def test_the_widgets_do_not_touch_the_database(forbidden):
    """面板类也一样。"""
    offenders = []
    for path in modules_below(os.path.join('prism', 'widgets')):
        if forbidden in imports_of(path):
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, (
        f'这些界面文件 import 了 {forbidden!r}：{offenders}\n'
        f'（该走 prism/fileio/sql.py）')


def test_widgets_do_not_import_the_prism_sqlite_module():
    """更直接的一条：界面不许 import `prism.fileio.sql`。"""
    offenders = []
    for path in modules_below(os.path.join('prism', 'widgets')):
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        if 'from prism.fileio.sql' in source or \
                'prism.fileio import sql' in source:
            offenders.append(os.path.relpath(path, ROOT))
    assert not offenders, (
        f'这些界面文件直接用了 SQLiteIO：{offenders}\n'
        f'（文件读写该经过服务层）')


def test_the_view_does_not_import_the_file_format_modules():
    """视图不该知道文件格式的细节。

    导出/导入的具体格式（DOCX、XMind、SVG…）由 `prism/fileio/` 和
    `prism/actions/*_workflow.py` 负责。
    """
    offenders = []
    for path in modules_below('prism'):
        relative = os.path.relpath(path, ROOT).replace('\\', '/')
        if not relative.endswith('prism/view.py'):
            continue
        with open(path, encoding='utf-8') as handle:
            source = handle.read()
        for module in ('import_docx', 'import_xmind', 'document_export',
                       'mindmap_export'):
            if f'import {module}' in source:
                offenders.append(f'{relative}: {module}')
    assert not offenders, (
        f'view.py 里出现了文件格式模块：{offenders}\n'
        f'（该由 prism/actions/ 下的 workflow 负责）')


# ── 服务层不依赖界面控件 ────────────────────────────────────────

#: 任务书 §7 T9 列的六个服务，以及它们各自的文件。
SERVICES = {
    'prism/workspace_service.py': 'WorkspaceService',
    'prism/export_service.py': 'ExportService',
    'prism/project_load_service.py': 'ProjectLoadService',
    'prism/duplicate_scan.py': 'DuplicateScanService',
    'prism/fileio/video_service.py': 'VideoFrameService',
    'prism/asset_import_service.py': None,   # 目前是函数模块
}

#: 界面控件模块。服务里出现这些就是越界。
WIDGET_MODULES = ('prism.view', 'prism.widgets', 'prism.actions')


@pytest.mark.parametrize('relative', sorted(SERVICES))
def test_services_do_not_import_the_ui(relative):
    """任务书：**服务不得持有整个 `PrismGraphicsView`**，依赖必须显式传入。

    Qt 信号和线程是允许的（服务要报进度和取消），但**界面控件**不行。
    """
    path = os.path.join(ROOT, relative)
    with open(path, encoding='utf-8') as handle:
        source = handle.read()
    offenders = [module for module in WIDGET_MODULES
                 if f'import {module}' in source or f'from {module}' in source]
    assert not offenders, (
        f'{relative} 里出现了界面模块：{offenders}\n'
        f'（服务要靠信号和返回值与界面通信，不直接持有控件）')


@pytest.mark.parametrize('relative', sorted(SERVICES))
def test_services_are_importable_without_a_view(relative):
    """服务模块要能在**没有界面**的情况下 import 成功。

    这条抓的是"模块级偷偷建了一个控件"这类问题 —— 那种代码在测试里
    会因为缺 QApplication 而炸，在真机上则会在 import 时就建出窗口。
    """
    import importlib

    module_name = relative[:-3].replace('/', '.').replace('\\', '.')
    module = importlib.import_module(module_name)
    assert module is not None


# ── 六个服务都在（T9 的清单）────────────────────────────────────

def test_all_six_services_from_the_task_book_exist():
    """任务书 §7 T9 列了六个要抽的服务，逐个确认文件在。"""
    for relative in SERVICES:
        assert os.path.exists(os.path.join(ROOT, relative)), (
            f'任务书要求的服务不见了：{relative}')


def test_the_view_is_not_where_the_services_live():
    """服务的实现在自己文件里，不是在 view.py 里。

    这条防的是"抽出服务之后又慢慢挪回视图里"。
    """
    with open(os.path.join(ROOT, 'prism', 'view.py'),
              encoding='utf-8') as handle:
        source = handle.read()
    for name in ('class WorkspaceService', 'class ExportService',
                 'class DuplicateScanService', 'class ProjectLoadService',
                 'class VideoFrameService'):
        assert name not in source, (
            f'{name} 被定义在 view.py 里了 —— 它该在自己的文件里')
