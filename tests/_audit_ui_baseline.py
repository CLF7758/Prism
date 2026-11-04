"""当前版本三流程基线：打开工程 / 全部图片 / 跨模块切换。

这是**取证脚本**，不改产品代码。它在一个能打开的真实审计副本上跑固定顺序，
把每一步的总耗时、阶段耗时（来自 `performance_trace`）、GUI 停顿和实际扫描数量
写进 JSON，供主线按"函数 + 线程 + 次数 + 耗时"定位。

固定条件（保证可重复）：
* 工程：`artifacts/full-audit/Test-project-audit.prism`（2.6 GB 审计副本，只读打开，
  所有保存类动作一律不做）；
* 顺序：打开 → 全部图片 → 画布 → 文档 → 脑图 → 材质库 → 画布；
* 每步记录源码 sha256 与工程 size+mtime_ns（**不是整文件哈希**）；
* trace 原始 jsonl 落在 `artifacts/full-audit/ui-baseline-trace/`。

运行：
    .\\.venv\\Scripts\\python.exe -m pytest tests/_audit_ui_baseline.py -q -s
"""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

import pytest

from prism import performance_trace


OUT = Path('artifacts/full-audit').resolve()
PROJECT = OUT / 'Test-project-audit.prism'
REPORT = OUT / 'ui-baseline-20261021.json'
TRACE_DIR = OUT / 'ui-baseline-trace'

SOURCE_FILES = ('prism/view.py', 'prism/window_workspace.py', 'prism/__main__.py',
                'prism/fileio/sql.py', 'prism/scene_loading.py',
                'prism/widgets/asset_browser.py', 'prism/widgets/asset_canvas.py',
                'prism/widgets/material_library.py', 'prism/canvas_tiles.py',
                'prism/performance_trace.py')


def write(payload):
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(str(REPORT) + '.tmp')
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                         encoding='utf-8')
    temporary.replace(REPORT)


def fingerprints():
    root = Path(__file__).resolve().parents[1]
    hashes = {}
    for name in SOURCE_FILES:
        try:
            hashes[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()[:16]
        except OSError as problem:
            hashes[name] = f'unreadable: {problem}'
    stat = PROJECT.stat()
    return dict(sources=hashes,
                project=dict(bytes=stat.st_size, modified_ns=stat.st_mtime_ns,
                             note='size+mtime_ns，不是整文件哈希'))


def scene_counts(scene):
    """实际有多少对象：图元 / 页面 / 材质 / 资源树节点 / 附件。"""
    try:
        assets = sum(1 for _item in scene.iter_assets())
    except BaseException:                                # noqa: BLE001 - evidence
        assets = None
    attachments = getattr(scene, 'attachments', None)
    return dict(assets=assets,
                pages=len(getattr(scene, 'workspace_pages', []) or []),
                materials=len(getattr(scene, 'material_records', []) or []),
                nodes=len(getattr(scene, 'workspace_nodes', []) or []),
                attachments=(len(attachments) if hasattr(attachments, '__len__') else None),
                item_count=getattr(scene, '_item_count', None))


def trace_operations(directory):
    """把 trace jsonl 里的 operation 事件按名字汇总（次数/中位/最大）。"""
    import statistics
    events = []
    for path in sorted(Path(directory).glob('Prism-operations-*.jsonl')):
        with path.open(encoding='utf-8', errors='replace') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    operations, stalls, rss = {}, [], []
    for event in events:
        if event.get('event') == 'operation' and event.get('duration_ms') is not None:
            operations.setdefault(event.get('name'), []).append(event['duration_ms'])
        elif event.get('event') == 'gui_stall':
            stalls.append(event.get('duration_ms'))
        elif event.get('event') == 'process' and event.get('rss_bytes'):
            rss.append(event['rss_bytes'])
    summary = {}
    for name, values in operations.items():
        values = sorted(values)
        summary[name] = dict(count=len(values),
                             median_ms=round(statistics.median(values), 1),
                             max_ms=round(max(values), 1),
                             total_ms=round(sum(values), 1))
    return dict(operations=summary, events=len(events),
                gui_stall=dict(count=len(stalls),
                               median_ms=round(statistics.median(stalls), 1) if stalls else None,
                               max_ms=round(max(stalls), 1) if stalls else None),
                rss=dict(first_mb=round(rss[0] / 1e6, 1) if rss else None,
                         last_mb=round(rss[-1] / 1e6, 1) if rss else None,
                         peak_mb=round(max(rss) / 1e6, 1) if rss else None))


def test_ui_baseline(main_window, qtbot, qapp):
    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    report = dict(generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'),
                  project=str(PROJECT), steps={},
                  identity=fingerprints(),
                  ordering=['open', 'all-assets', 'canvas', 'document', 'mindmap',
                            'material-library', 'canvas-again'],
                  note=('GUI 基线；只读打开审计副本，不做任何保存。'
                        '阶段耗时来自 performance_trace 的 operation 事件'))
    write(report)

    window, view = main_window, main_window.view
    trace = performance_trace.install(qapp, window, TRACE_DIR)
    report['trace'] = str(trace.path)

    def step(name, call, *, settle_ms=0):
        entry = dict(started=time.strftime('%H:%M:%S'))
        report['steps'][name] = entry
        write(report)
        before = scene_counts(view.scene) if view.scene is not None else {}
        started = time.perf_counter()
        try:
            detail = call() or {}
            entry['status'] = 'ok'
            entry['error'] = None
        except BaseException as problem:                  # noqa: BLE001 - evidence
            detail, entry['status'] = {}, 'error'
            entry['error'] = f'{type(problem).__name__}: {problem}'
        if settle_ms:
            qtbot.wait(settle_ms)
        entry['ms'] = round((time.perf_counter() - started) * 1000, 1)
        after = scene_counts(view.scene) if view.scene is not None else {}
        entry['scene_before'] = before
        entry['scene_after'] = after
        if isinstance(detail, dict):
            entry.update(detail)
        write(report)
        print(f'[{entry["status"]}] {name} {entry["ms"]}ms {entry.get("error") or ""}',
              flush=True)
        return entry

    def open_project():
        started = time.perf_counter()
        view.open_from_file(str(PROJECT))

        def ready():
            worker = getattr(view, 'worker', None)
            return (not getattr(view, '_project_opening', False)
                    and (worker is None or not worker.isRunning()))

        qtbot.waitUntil(ready, timeout=180000)
        interactive_ms = round((time.perf_counter() - started) * 1000, 1)
        # 再等一轮事件循环，让 fit/目录收尾也跑完
        qtbot.wait(200)
        finished_ms = round((time.perf_counter() - started) * 1000, 1)
        return dict(first_interactive_ms=interactive_ms,
                    settled_ms=finished_ms,
                    page=view.filename)

    def all_assets():
        browser = getattr(window, 'asset_browser', None)
        assert browser is not None, '窗口没有 asset_browser'
        started = time.perf_counter()
        browser.open('all')
        opened_ms = round((time.perf_counter() - started) * 1000, 1)
        cards = getattr(getattr(browser, 'list', None), 'cards', None)
        return dict(open_ms=opened_ms,
                    cards=(len(cards) if cards is not None else None),
                    model_items=len(getattr(getattr(browser, 'model', None),
                                            'items', []) or []))

    pages = {}

    def collect_pages():
        for page in (view.scene.workspace_pages or []):
            pages.setdefault(page.get('kind'), []).append(page.get('id'))
        return dict(kinds={kind: len(ids) for kind, ids in pages.items()},
                    first={kind: ids[0] for kind, ids in pages.items() if ids})

    def switch(kind, page_id):
        started = time.perf_counter()
        window.open_workspace_page(kind, page_id)
        qtbot.wait(150)
        return dict(page=page_id, switch_ms=round((time.perf_counter() - started) * 1000, 1))

    def material_library():
        library = getattr(window, 'material_library', None)
        assert library is not None, '窗口没有 material_library'
        started = time.perf_counter()
        opener = getattr(window, 'open_material_library', None)
        if callable(opener):
            opener()
        else:
            window.tabs.setCurrentWidget(library)
        qtbot.wait(300)
        return dict(first_enter_ms=round((time.perf_counter() - started) * 1000, 1),
                    records=(library.materials.count() if hasattr(library, 'materials') else None))

    step('open', open_project)
    step('page-catalog', collect_pages)
    step('all-assets', all_assets)
    if pages.get('canvas'):
        step('canvas', lambda: switch('canvas', pages['canvas'][0]))
    if pages.get('document'):
        step('document', lambda: switch('document', pages['document'][0]))
    if pages.get('mindmap'):
        step('mindmap', lambda: switch('mindmap', pages['mindmap'][0]))
    step('material-library', material_library)
    if pages.get('canvas'):
        step('canvas-again', lambda: switch('canvas', pages['canvas'][0]))

    # 让 trace 落盘后再汇总
    qtbot.wait(300)
    report['trace_summary'] = trace_operations(TRACE_DIR)
    report['finished'] = True
    report['exit_code'] = 0
    write(report)
    print(f'[DONE] {REPORT}', flush=True)
