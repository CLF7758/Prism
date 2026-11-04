"""P1 验证：按页打开只读一页、保存被明确拒绝、源文件不变、GUI 开关生效。

对应交接书 P1/3C：
* 正式读取函数 `load_prism` 接受 `scope`，只读这些 id；
* 按页打开在 scene 上登记 `_loaded_scope_ids` 与 `_partial_project_source`；
* 未接部分保存基线时，保存必须**明确拒绝**（不能拿全量 writer 覆盖未加载行）；
* 源工程字节不变；
* `PRISM_PAGED_OPEN=1` 时真实 GUI 入口 `open_from_file` 走按页路径。

运行：
    .\.venv\Scripts\python.exe -m pytest tests/_verify_paged_open.py -q -s
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time

import pytest


OUT = Path('artifacts/full-audit').resolve()
PROJECT = OUT / 'Test-project-audit.prism'
RESULT = OUT / 'paged-open-20261021-run1.json'


def digest(path):
    value = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def catalog_path(project):
    from prism.thumbnail_cache import cache_directory
    return Path(os.path.dirname(cache_directory())) / 'catalog' / (Path(project).name + '.sqlite')


def ensure_catalog(project, budget=180):
    from prism.project_open_scope import ProjectOpenScope
    cache = catalog_path(project)
    scope = ProjectOpenScope(project, cache)
    built = None
    if not scope.ready:
        built = scope.build(max_seconds=budget)
        assert built.complete, f'目录未建完：{built}'
    return scope, (built.seconds if built else 0.0)


def drain(scene):
    while not scene.items_to_add.empty():
        scene.add_queued_items()


def new_scene():
    from PyQt6 import QtGui
    from prism.scene import PrismGraphicsScene
    return PrismGraphicsScene(QtGui.QUndoStack())


def test_paged_read_is_bounded_and_saving_is_refused(qapp, tmp_path):
    from prism.fileio import load_prism, save_prism

    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    source_before = digest(PROJECT)

    scope_index, build_seconds = ensure_catalog(PROJECT)
    counts = scope_index.counts() or {}
    canvas = next(canvas_id for (canvas_id, kind), count in counts.items()
                  if kind == 'pixmap' and 1 <= count <= 128)
    page = scope_index.page(canvas, kind='pixmap', limit=100)
    ids = [row['id'] for row in page]
    assert ids, '目录页为空'

    # 全量（旧路径）作为对照
    full_scene = new_scene()
    started = time.perf_counter()
    load_prism(str(PROJECT), full_scene)
    full_ms = round((time.perf_counter() - started) * 1000, 1)
    drain(full_scene)
    full_assets = sum(1 for _item in full_scene.iter_assets())

    # 按页
    paged_scene = new_scene()
    started = time.perf_counter()
    load_prism(str(PROJECT), paged_scene, scope=ids)
    paged_ms = round((time.perf_counter() - started) * 1000, 1)
    drain(paged_scene)
    paged_assets = sum(1 for _item in paged_scene.iter_assets())

    assert paged_assets == len(ids), f'按页只该装 {len(ids)} 条，实得 {paged_assets}'
    assert paged_assets < full_assets
    assert tuple(getattr(paged_scene, '_loaded_scope_ids', ())) == tuple(ids)
    assert getattr(paged_scene, '_partial_project_source', None)

    # 未接部分保存基线 → 保存必须拒绝，且不产生目标文件
    target = tmp_path / 'paged-save.prism'
    refused = None
    try:
        save_prism(str(target), paged_scene, create_new=True)
    except Exception as problem:                        # noqa: BLE001 - 期望被拒绝
        refused = f'{type(problem).__name__}: {problem}'
    assert refused is not None and 'partial' in refused.lower(), refused
    assert not target.exists()
    assert digest(PROJECT) == source_before

    # 未加载行仍在文件里（数量与全量一致）
    with closing(sqlite3.connect(PROJECT.as_uri() + '?mode=ro', uri=True)) as connection:
        stored = connection.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    assert stored == full_assets or stored >= full_assets

    RESULT.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(
        generated_at=time.strftime('%Y-%m-%dT%H:%M:%S'), project=str(PROJECT),
        catalog=str(catalog_path(PROJECT)), catalog_build_seconds=build_seconds,
        canvas=canvas, scope_ids=len(ids),
        full_ms=full_ms, full_assets=full_assets,
        paged_ms=paged_ms, paged_assets=paged_assets,
        speedup=(round(full_ms / paged_ms, 2) if paged_ms else None),
        save_refused=refused, source_unchanged=digest(PROJECT) == source_before,
        stored_items=stored,
        note=('对照：同一工程、同一次运行；全量走旧路径，按页只读目录给的一页。'
              '不含 GUI 安装外的解码与绘制。'))
    RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'[P1] full={full_ms}ms/{full_assets}条 paged={paged_ms}ms/{paged_assets}条 '
          f'speedup={payload["speedup"]}x refused={refused}', flush=True)


def test_gui_paged_open_uses_scope(main_window, qtbot, monkeypatch):
    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    ensure_catalog(PROJECT)
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    view = main_window.view
    started = time.perf_counter()
    view.open_from_file(str(PROJECT))
    qtbot.waitUntil(lambda: (not getattr(view, '_project_opening', False)
                             and (getattr(view, 'worker', None) is None
                                  or not view.worker.isRunning())), timeout=180000)
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    drain(view.scene)
    loaded = sum(1 for _item in view.scene.iter_assets())
    scope_ids = tuple(getattr(view.scene, '_loaded_scope_ids', ()))
    assert scope_ids, 'GUI 打开没有走按页路径（未登记范围）'
    assert loaded == len(scope_ids), f'装了 {loaded} 条，范围是 {len(scope_ids)}'
    assert view.filename == str(PROJECT)
    print(f'[P1-GUI] open={elapsed}ms loaded={loaded} scope={len(scope_ids)}', flush=True)


def test_gui_paged_open_falls_back_without_catalog(main_window, qtbot, monkeypatch, tmp_path):
    """目录不可用时必须退回全量，而不是假装打开了一个空页。"""
    if not PROJECT.exists():
        pytest.skip(f'审计副本不存在：{PROJECT}')
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    monkeypatch.setattr('prism.thumbnail_cache.cache_directory',
                        lambda: str(tmp_path / 'empty' / 'previews'))
    view = main_window.view
    view.open_from_file(str(PROJECT))
    qtbot.waitUntil(lambda: (not getattr(view, '_project_opening', False)
                             and (getattr(view, 'worker', None) is None
                                  or not view.worker.isRunning())), timeout=180000)
    drain(view.scene)
    loaded = sum(1 for _item in view.scene.iter_assets())
    assert loaded > 1000, f'退回全量后应加载全部资产，实得 {loaded}'
    assert not getattr(view.scene, '_loaded_scope_ids', None)
    assert not getattr(view.scene, '_partial_project_source', None)
    print(f'[P1-GUI-fallback] loaded={loaded}', flush=True)
