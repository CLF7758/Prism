#!/usr/bin/env python
"""E-5 验收脚本：T1 接线后的「口径一致性」比对（目录口径 vs scene 口径）。

硬约束（组内红线）
------------------
* 真实工程 **只读** 打开：一律 ``sqlite3.connect(file:...?mode=ro, uri=True)``。
  生产 loader（``prism.fileio.load_prism``）对 ``readonly=True`` 用的��是
  ``mode=rw``（``prism/fileio/sql.py:325-328``），所以 **不许** 直接拿它打开
  ``E:\\Prism-stress-100k\\`` 下的源文件。
* 口径比对需要「真的把工程装进 scene」，因此先把源文件 **字节级复制** 到本机
  临时目录，再用生产 loader 打开 **副本**。副本与源的双流 sha256 相同才算数。
* 目录口径用生产同款 ``prism.project_open_scope.ProjectOpenScope``；
  缓存写到自己的目录，绝不写回源工程。

三个子命令
----------
``copy``    字节级复制 + 源/副本双流 sha256（证明副本与源逐字节相同）
``facts``   源工程的三项规模口径（记录数 / 唯一源图数 / 实际解码字节数）
``compare`` 口径一致性比对：计数（总数 / 按画布 / 未分类 / 回收站）、
            检索（``wood`` / ``a``）、素材列表（条数 + 顺序）

只依赖项目原有依赖（Pillow / PyQt6 / SQLite），不引入新第三方包。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import platform
import shutil
import sqlite3
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

CHUNK = 32 * 1024 * 1024


# ── 小工具 ────────────────────────────────────────────────────────────────

def log(*args) -> None:
    print('[e5]', *args, flush=True)


def write_json(path, payload) -> pathlib.Path:
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2,
                               default=str), encoding='utf-8')
    log('wrote %s' % path)
    return path


def stat_of(path) -> dict:
    path = pathlib.Path(path)
    try:
        info = path.stat()
    except OSError as error:
        return dict(path=str(path), exists=False, error=str(error))
    return dict(path=str(path), exists=True, size_bytes=info.st_size,
                mtime_ns=info.st_mtime_ns, mtime=time.strftime(
                    '%Y-%m-%dT%H:%M:%S', time.localtime(info.st_mtime)))


def ro_uri(path) -> str:
    """只读 URI。真实工程只许这样打开。"""
    return pathlib.Path(path).resolve().as_uri() + '?mode=ro'


def _evidence():
    sys.path.insert(0, str(REPO / 'tools' / 'scale'))
    import evidence  # noqa: E402  (tools/scale/evidence.py, 只读复用)
    return evidence


def _env_snapshot(evidence) -> dict:
    environment = evidence.capture_environment()
    environment['python_processes'] = evidence.python_processes()
    return environment


# ── copy ──────────────────────────────────────────────────────────────────

def cmd_copy(args) -> int:
    source = pathlib.Path(args.source)
    dest = pathlib.Path(args.dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    before = stat_of(source)
    source_hash = hashlib.sha256()
    dest_hash = hashlib.sha256()
    copied = 0
    started = time.perf_counter()
    with source.open('rb') as src, dest.open('wb') as dst:
        while True:
            block = src.read(CHUNK)
            if not block:
                break
            source_hash.update(block)
            dest_hash.update(block)
            dst.write(block)
            copied += len(block)
            if copied % (4 * 1024 * 1024 * 1024) == 0:
                log('copied %.1f GiB' % (copied / 1073741824))
    elapsed = time.perf_counter() - started
    after = stat_of(source)

    report = dict(
        source=before, source_after_copy=after,
        source_unchanged=(before.get('size_bytes') == after.get('size_bytes')
                          and before.get('mtime_ns') == after.get('mtime_ns')),
        dest=stat_of(dest), bytes_copied=copied,
        source_sha256=source_hash.hexdigest(),
        dest_sha256=dest_hash.hexdigest(),
        identical=source_hash.hexdigest() == dest_hash.hexdigest(),
        seconds=round(elapsed, 3),
        mib_per_second=round(copied / 1048576 / max(elapsed, 1e-9), 2),
        finished=time.strftime('%Y-%m-%dT%H:%M:%S'),
    )
    if args.out:
        write_json(args.out, report)
    log(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['identical'] and report['source_unchanged'] else 1


# ── facts ─────────────────────────────────────────────────────────────────

def cmd_facts(args) -> int:
    """三项规模口径。分列，不合并。

    扫描次数说明：``E:`` 是 16 TB 机械盘（`Get-PhysicalDisk` 实测 HDD/SATA），
    ``sqlar`` 表的 10 万行各自带 ~518 KB 的 BLOB，**一次全表扫描 = 10 万次
    随机读**（≈13 分钟，且与其他组员的并发扫描抢盘）。所以这里的
    ``sqlar`` 统计**合并成一条语句**（一次表扫描），``items`` 侧的扫描也
    尽量合并。
    """
    project = pathlib.Path(args.project)
    evidence = _evidence()
    connection = sqlite3.connect(ro_uri(project), uri=True)
    connection.execute('PRAGMA cache_size=-262144')      # 256 MiB 页缓存
    connection.execute('PRAGMA mmap_size=1073741824')    # 1 GiB 映射窗口
    report = dict(project=str(project), uri=ro_uri(project),
                  environment=_env_snapshot(evidence), measured={})

    def timed(label, call):
        started = time.perf_counter()
        value = call()
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        log('%s -> %s (%s ms)' % (label, value, elapsed))
        return value, elapsed

    # ── items 表（小表，几 MB，扫描便宜） ─────────────────────
    value, elapsed = timed('items rows + json bytes', lambda: connection.execute(
        'SELECT count(*), COALESCE(sum(length(data)),0) FROM items').fetchone())
    report['measured']['records_total'] = dict(value=value[0], milliseconds=elapsed)
    report['measured']['items_json_bytes'] = dict(value=value[1],
                                                  milliseconds=elapsed)
    report['measured']['records_by_type'] = dict(value=[tuple(row) for row in
                                                        connection.execute(
        'SELECT type, count(*) FROM items GROUP BY type ORDER BY type').fetchall()],
        milliseconds=None)

    value, elapsed = timed('items distinct filename (json)', lambda: connection.execute(
            "SELECT count(DISTINCT json_extract(data,'$.filename')) "
            "FROM items WHERE type='pixmap'").fetchone()[0])
    report['measured']['unique_source_images_from_items_json'] = dict(
        value=value, milliseconds=elapsed,
        note='items.data 里 distinct 的 filename 数（主口径）')

    value, elapsed = timed(
        'items lacking imageWidth/imageHeight (would force a decode)',
        lambda: connection.execute(
            "SELECT count(*) FROM items WHERE type='pixmap' AND "
            "(json_extract(data,'$.imageWidth') IS NULL OR "
            " json_extract(data,'$.imageHeight') IS NULL)").fetchone()[0])
    report['measured']['pixmaps_without_size_in_json'] = dict(
        value=value, milliseconds=elapsed,
        note='生产 loader 对这些行会 read_archive()+解码头（prism/fileio/sql.py:598-606）')

    # ── sqlar 归档表（一次全表扫描，机械盘很贵） ───────────────
    if args.archive:
        value, elapsed = timed(
            'sqlar rows + sum(sz) + distinct name (one table scan)',
            lambda: connection.execute(
                'SELECT count(*), COALESCE(sum(sz),0), count(DISTINCT name) '
                'FROM sqlar').fetchone())
        report['measured']['archive_rows'] = dict(value=value[0], milliseconds=elapsed)
        report['measured']['archive_stored_bytes'] = dict(
            value=value[1], milliseconds=elapsed,
            note='归档内保存的字节总量（含重复副本），不是「实际解码字节数」')
        report['measured']['unique_source_images'] = dict(
            value=value[2], milliseconds=elapsed,
            note='归档里 distinct 的原始文件名数 = 唯一源图数（交叉核对口径）')
    else:
        report['measured']['archive_rows'] = dict(value='无实测', milliseconds=None,
                                                   note='--archive 未开')

    connection.close()
    if args.out:
        write_json(args.out, report)
    log(json.dumps(report['measured'], ensure_ascii=False, indent=2))
    return 0


# ── compare ───────────────────────────────────────────────────────────────

def _force_readonly_loader():
    """把生产 loader 的连接强制成 ``mode=ro``。

    生产 loader 的 ``readonly=True`` 用的**不是**只读 URI：
    ``prism/fileio/sql.py:325-328`` 拼的是 ``?mode=rw``。本组对真实工程的
    红线是「一律 ``mode=ro``」，所以这里把连接 URI 换掉。

    只读连接上 ``_migrate()`` 永远不会走到写分支（实测真实工程
    ``user_version=4`` == 产品 ``USER_VERSION=4``，它会直接 return），
    但仍然显式跳过它：schema 万一落后，``_migrate()`` 会把整个 51.8 GiB
    源文件复制到临时目录（``prism/fileio/sql.py:367-373``），那是本组
    不能接受的磁盘开销。

    返回原函数，调用方负责还原。
    """
    from prism.fileio import sql as sql_module

    original = sql_module.SQLiteIO._establish_connection

    def patched(self):
        self._close_connection()
        if self.create_new:
            original(self)
            return
        uri = pathlib.Path(self.filename).resolve().as_uri() + '?mode=ro'
        self._connection = sqlite3.connect(uri, uri=True)
        self._cursor = self.connection.cursor()
        version = self.fetchone('PRAGMA user_version')[0]
        if version < sql_module.USER_VERSION:
            raise RuntimeError(
                'source schema user_version=%s < product USER_VERSION=%s; '
                'refusing to migrate through a read-only connection'
                % (version, sql_module.USER_VERSION))

    sql_module.SQLiteIO._establish_connection = patched
    return original


def _restore_loader(original) -> None:
    from prism.fileio import sql as sql_module
    sql_module.SQLiteIO._establish_connection = original


def _install_archive_counter():
    """量「实际解码字节数」：包住 source_store.read_archive，数真正读出来的字节。"""
    import prism.source_store as source_store
    counter = dict(calls=0, bytes=0)
    original = source_store.read_archive

    def counting(source):
        data = original(source)
        counter['calls'] += 1
        try:
            counter['bytes'] += len(data)
        except TypeError:
            pass
        return data

    source_store.read_archive = counting
    return counter, original


def _restore_archive_counter(original) -> None:
    import prism.source_store as source_store
    source_store.read_archive = original


def _load_scene(project_path, app, evidence):
    """用生产 loader 把工程装进 scene（生产同款的分批安装节奏）。

    连接被强制成 ``mode=ro``（见 ``_force_readonly_loader``），所以真实工程
    在这一步也是**只读**打开：不需要任何副本，也不会有任何写回。
    """
    from PyQt6 import QtGui, QtWidgets
    from prism.fileio import load_prism
    from prism.scene import PrismGraphicsScene

    scene = PrismGraphicsScene(QtGui.QUndoStack())
    counter, original = _install_archive_counter()
    loader_original = _force_readonly_loader()
    started = time.perf_counter()
    try:
        load_prism(str(project_path), scene)
    finally:
        _restore_loader(loader_original)
        _restore_archive_counter(original)
    load_ms = (time.perf_counter() - started) * 1000

    # 生产用 SceneLoadScheduler（prism/scene_loading.py:29）：
    # 每轮 128 条 / 6 ms 预算，16 ms 一轮。这里用同样的调用把队列排空。
    pending_before = scene.items_to_add.qsize()
    flush_started = time.perf_counter()
    rounds = 0
    while not scene.items_to_add.empty():
        scene.add_queued_items(max_items=128, time_budget_ms=6)
        rounds += 1
        app.processEvents()
    flush_ms = (time.perf_counter() - flush_started) * 1000
    return scene, dict(load_ms=round(load_ms, 2), flush_ms=round(flush_ms, 2),
                       flush_rounds=rounds,
                       queued_items_before_flush=pending_before,
                       queued_items_left_after_flush=scene.items_to_add.qsize(),
                       archive_bytes_read=counter['bytes'],
                       archive_reads=counter['calls'])


def _resource_service(scene):
    from prism.workspace_service import ResourceTreeService, legacy_page_nodes
    nodes = list(getattr(scene, 'workspace_nodes', []) or [])
    if not nodes:
        nodes = legacy_page_nodes(getattr(scene, 'workspace_pages', []) or [],
                                  scene.project_id)
    return ResourceTreeService(scene.project_id, nodes, None)


def _ids(results):
    out = []
    for result in results:
        item = getattr(result, 'item', None)
        key = getattr(item, 'save_id', None)
        out.append(int(key) if key is not None else None)
    return out


def _sequence_diff(left, right, limit=20):
    """两个有序 id 序列的差异（首个不同位置 + 各自片段）。"""
    first = None
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            first = index
            break
    if first is None and len(left) != len(right):
        first = min(len(left), len(right))
    return dict(equal=(first is None and len(left) == len(right)),
                first_difference_index=first,
                left_length=len(left), right_length=len(right),
                left_slice=left[first:first + limit] if first is not None else [],
                right_slice=right[first:first + limit] if first is not None else [],
                left_only_ids=sorted(set(left) - set(right))[:limit],
                right_only_ids=sorted(set(right) - set(left))[:limit],
                left_only_count=len(set(left) - set(right)),
                right_only_count=len(set(right) - set(left)))


def _scene_population(scene) -> dict:
    """scene 侧的条数：**怎么数出来的**要写清楚，不能拿 items 表行数当答案。

    * ``installed_virtual_records`` = ``len(scene._viewport_items.records)``
      —— 生产装载调度真正 ``addItem`` 进 scene 的虚拟记录数。
    * ``iter_assets`` = ``scene.iter_assets()`` 的产出数（``ProjectAssetCatalog``
      的 ``asset_items()`` 用的就是它）。
    * ``items_for_save`` = ``scene.items_for_save()`` 的产出数（带排序，代价高）。
    """
    records = getattr(getattr(scene, '_viewport_items', None), 'records', None) or {}
    return dict(
        installed_virtual_records=len(records),
        iter_assets=sum(1 for _ in scene.iter_assets()),
        items_for_save=sum(1 for _ in scene.items_for_save()),
        note='installed_virtual_records=len(scene._viewport_items.records); '
             'iter_assets=scene.iter_assets() 产出数（catalog.asset_items 用它）; '
             'items_for_save=scene.items_for_save() 产出数')


def _catalog_descriptions(scope, ids, limit=512) -> dict:
    """目录侧 id → 可读描述（filename / title / canvas），用于把差异说清楚。"""
    out = {}
    if not ids:
        return out
    wanted = list(ids[:limit])
    for start in range(0, len(wanted), 1024):
        try:
            rows = scope.summaries(wanted[start:start + 1024])
        except Exception:
            return out
        for row in rows:
            try:
                key = int(row['id'])
            except (KeyError, TypeError, ValueError):
                continue
            out[key] = dict(filename=row.get('filename'), title=row.get('title'),
                            canvas_id=row.get('canvas_id'), kind=row.get('kind'),
                            deleted_at=row.get('deleted_at'))
    return out


def _scene_descriptions(scene, ids, limit=512) -> dict:
    """scene 侧 id → 可读描述（filename / title / canvas）。"""
    out = {}
    if not ids:
        return out
    wanted = set()
    for value in ids[:limit]:
        try:
            wanted.add(int(value))
        except (TypeError, ValueError):
            continue
    if not wanted:
        return out
    for item in scene.iter_assets():
        key = getattr(item, 'save_id', None)
        if key is None:
            continue
        try:
            key = int(key)
        except (TypeError, ValueError):
            continue
        if key in wanted:
            out[key] = dict(
                filename=getattr(item, 'filename', None),
                title=getattr(item, '_title', None),
                canvas_id=getattr(item, '_canvas_id', None),
                type=getattr(item, 'TYPE', None))
    return out


def _cache_rows(path):
    """只读读一份目录缓存的 assets 行（id → 标量元组）。"""
    connection = sqlite3.connect(ro_uri(path), uri=True)
    try:
        rows = connection.execute(
            'SELECT id,kind,canvas_id,title,filename,tags,rating,deleted_at '
            'FROM assets ORDER BY id').fetchall()
    finally:
        connection.close()
    return {int(row[0]): tuple(row[1:]) for row in rows}


def _compare_catalog_caches(left_path, right_path) -> dict:
    """两份独立建好的目录缓存逐行对比（只读）。"""
    left = _cache_rows(left_path)
    right = _cache_rows(right_path)
    only_left = sorted(set(left) - set(right))
    only_right = sorted(set(right) - set(left))
    differing = [key for key in sorted(set(left) & set(right))
                 if left[key] != right[key]]
    return dict(left=str(left_path), right=str(right_path),
                left_rows=len(left), right_rows=len(right),
                equal=(left == right),
                only_left_count=len(only_left), only_right_count=len(only_right),
                differing_count=len(differing),
                only_left_ids=only_left[:50], only_right_ids=only_right[:50],
                differing_ids=differing[:50],
                differing_examples=[dict(id=key, left=left[key], right=right[key])
                                   for key in differing[:10]])


def _sort_key_divergence(scope) -> dict:
    """两侧的**排序键**是不是同一个？

    * 目录侧未装载行：``_directory_rows`` 用 ``row.get('filename')`` 排序
      （prism/project_catalog.py:1094）；
    * scene 侧（以及被 overlay 顶替的目录行）：``_scene_entry`` 用
      ``_title or filename`` 排序（prism/project_catalog.py:1047）。

    两个键不同的画布有多少个 —— 这是「顺序一致」这个结论的**脆弱点**：
    本工程全量打开时 overlay 顶替了全部磁盘行，所以顺序实测一致；
    但只要有没装载的行，两个键就会分叉。

    这里直接从目录缓存的 assets 表算（只读）。
    """
    rows = None
    connection = sqlite3.connect(ro_uri(scope.cache_path), uri=True)
    try:
        rows = connection.execute(
            'SELECT canvas_id,id,filename,title FROM assets').fetchall()
    finally:
        connection.close()
    by_canvas = {}
    for canvas_id, key, filename, title in rows:
        by_canvas.setdefault(canvas_id, []).append(
            (int(key), filename or '', title or ''))
    diverging, examples = 0, []
    for canvas_id, group in by_canvas.items():
        by_filename = [row[0] for row in sorted(
            group, key=lambda row: (row[1].casefold(), row[0]))]
        by_title = [row[0] for row in sorted(
            group, key=lambda row: (row[2].casefold(), row[0]))]
        if by_filename != by_title:
            diverging += 1
            if len(examples) < 10:
                examples.append(dict(canvas_id=canvas_id,
                                     first_by_filename=by_filename[:6],
                                     first_by_title=by_title[:6]))
    return dict(canvases=len(by_canvas), diverging_canvases=diverging,
                examples=examples,
                note='目录侧未装载行按 filename 排序，scene 侧按 title 排序；'
                     'diverging_canvases = 两种键会给出不同顺序的画布数')


def cmd_compare(args) -> int:
    os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
    from PyQt6 import QtWidgets

    evidence = _evidence()
    real_project = pathlib.Path(args.project)
    # scene 口径默认也用真实工程：loader 的连接被强制成 mode=ro，
    # 所以「装 scene」不需要任何副本，也不需要写权限。
    scene_project = pathlib.Path(args.scene_project) if args.scene_project \
        else real_project
    out_dir = pathlib.Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    report = dict(
        label=args.label,
        real_project=str(real_project),
        scene_project=str(scene_project),
        started=time.strftime('%Y-%m-%dT%H:%M:%S'),
        environment_before=_env_snapshot(evidence),
        real_project_before=stat_of(real_project),
        measurements={},
        comparisons={},
    )

    # ── 目录口径的 scope（生产同款，源文件只读） ─────────────────────
    # 两种状态必须分开量（组长口径）：
    #   warm = 目录缓存已就绪 → 目录口径生效
    #   cold = 目录缓存缺失 → `ProjectOpenScope` 建了实例但 not ready，
    #          `ProjectAssetCatalog._catalog_scope()` 记 miss('not_ready')，
    #          计数/列表**退回 scene 口径**（此时界面上只有一个口径）
    from prism.project_open_scope import ProjectOpenScope
    if args.cache:
        cache_path = pathlib.Path(args.cache)
    else:
        # 生产真实路径：%APPDATA%\Prism\catalog\<工程名>.sqlite
        # （prism/view.py:1313-1314 同一算法）；本组改用系统临时目录，
        # 避免在别的组员的生产缓存目录里留下东西。
        cache_path = pathlib.Path(tempfile.gettempdir()) / 'e5-consistency' \
            / (scene_project.name + '.sqlite')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    scope = ProjectOpenScope(str(scene_project), str(cache_path))
    build_report = dict(cache=str(cache_path), ready_before=bool(scope.ready))
    if not scope.ready and not args.no_build:
        started = time.perf_counter()
        result = scope.build(batch_size=128)
        build_report['build'] = dict(
            complete=bool(result.complete), total=int(result.total_records),
            processed=int(result.processed_records), resumed=bool(result.resumed),
            json_characters=int(result.json_characters),
            seconds=round(result.seconds, 3),
            milliseconds=round((time.perf_counter() - started) * 1000, 2))
    build_report['ready_after'] = bool(scope.ready)
    report['measurements']['catalog_scope_warm'] = build_report
    if not scope.ready:
        report['error'] = 'catalog scope is not ready; comparison aborted'
        write_json(out_dir / 'consistency.json', report)
        log(report['error'])
        return 2

    # 冷缓存状态：生产同款实例（ProjectOpenScope 建成、缓存文件不存在），
    # ready=False → 目录口径完全不可用，界面退回 scene 口径。
    cold_cache = pathlib.Path(tempfile.gettempdir()) / 'e5-consistency' \
        / ('cold-' + scene_project.name + '.sqlite')
    if cold_cache.exists():
        cold_cache.unlink()
    cold_scope = ProjectOpenScope(str(scene_project), str(cold_cache))
    report['measurements']['catalog_scope_cold'] = dict(
        cache=str(cold_cache), cache_exists=cold_cache.exists(),
        ready=bool(cold_scope.ready),
        note='生产同款实例 + 缓存文件缺失 = view._catalog_scope_instance() 的 '
             'catalog_missing 分支；ready=False → miss(not_ready) → 退回 scene 口径')

    # 另一份独立建好的目录缓存（只读复用，不建不写）：交叉验证目录口径。
    production_cache = None
    if args.production_cache and pathlib.Path(args.production_cache).exists():
        prod_scope = ProjectOpenScope(str(scene_project), str(args.production_cache))
        production_cache = dict(path=str(args.production_cache),
                                ready=bool(prod_scope.ready),
                                total=prod_scope.total() if prod_scope.ready else None)
        if prod_scope.ready:
            groups = prod_scope.grouped_counts()
            production_cache['grouped_counts_total'] = sum(
                amount for _cid, _deleted, _untagged, amount in groups)
            production_cache['grouped_canvases'] = len({
                cid for cid, _d, _u, _a in groups})
            production_cache['source_state'] = prod_scope.source_state()
        else:
            production_cache['source_state'] = prod_scope.source_state()
            production_cache['note'] = ('这份缓存的 source 指纹与本次的工程不匹配 '
                                        '（它是为另一个副本建的）→ ready=False，'
                                        '只能作为「指纹不匹配就判废」的证据')
        report['measurements']['production_catalog_scope'] = production_cache
        # 逐行对比两份独立建好的目录缓存（指纹不同也没关系，内容一样就行）。
        try:
            report['measurements']['catalog_cache_cross_check'] = \
                _compare_catalog_caches(str(cache_path),
                                        str(args.production_cache))
        except Exception as error:
            report['measurements']['catalog_cache_cross_check'] = \
                dict(error=repr(error))

    # ── scene 口径：真的把工程装进 scene ──────────────────────────
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(
        [sys.argv[0]])
    scene, load_report = _load_scene(scene_project, app, evidence)
    report['measurements']['scene_load'] = load_report
    log('scene load: %s' % json.dumps(load_report, ensure_ascii=False))

    from prism.project_catalog import ProjectAssetCatalog
    service = _resource_service(scene)
    scene.workspace_nodes = service.nodes
    catalog_dir = ProjectAssetCatalog(scene, service,
                                      scope_provider=lambda: scope)
    catalog_scene = ProjectAssetCatalog(scene, service,
                                        scope_provider=lambda: None)
    catalog_cold = ProjectAssetCatalog(scene, service,
                                       scope_provider=lambda: cold_scope)

    report['measurements']['scene_shape'] = dict(
        scene_population=_scene_population(scene),
        workspace_pages=len(getattr(scene, 'workspace_pages', []) or []),
        resource_nodes=len(service.nodes),
        material_records=len(getattr(scene, 'material_records', []) or []),
        partial_source=getattr(scene, '_partial_project_source', None))

    # overlay 语义：全量打开时，本次会话的活对象会**逐条替换**磁盘行
    # （prism/project_catalog.py:1076-1084）。这决定了「列表顺序」这一项
    # 在热档下到底是同一口径还是两套口径 —— 必须把它数出来。
    overlay, fresh = catalog_dir._session_overlay()
    report['measurements']['session_overlay'] = dict(
        loaded_overlay_items=len(overlay),
        fresh_unsaved_items=len(fresh),
        note='目录侧的 _session_overlay()：loaded_overlay_items = 会话里带 save_id 的活对象数；'
             '磁盘行逐条被它顶替（所以未装载行的 filename 排序键在热档下用不上）')
    try:
        report['measurements']['sort_key_divergence'] = _sort_key_divergence(scope)
    except Exception as error:
        report['measurements']['sort_key_divergence'] = dict(error=repr(error))

    # ── 1) 计数 ───────────────────────────────────────────────────
    def snapshot(catalog):
        started = time.perf_counter()
        value = dict(
            total=catalog.total_count(),
            unclassified=catalog.unclassified_count(),
            untagged=catalog.untagged_count(),
            canvases=catalog.canvas_counts(),
            trash_top_level=catalog.trash_top_level_count(),
            trash_assets=catalog.trash_asset_count(),
            snapshot=catalog.count_snapshot(),
        )
        value['milliseconds'] = round((time.perf_counter() - started) * 1000, 2)
        return value

    counts_dir = snapshot(catalog_dir)
    counts_scene = snapshot(catalog_scene)
    counts_cold = snapshot(catalog_cold)
    report['measurements']['counts_catalog'] = counts_dir
    report['measurements']['counts_scene'] = counts_scene
    report['measurements']['counts_cold'] = counts_cold
    # C3 口径证据：目录侧应当全是 hit，scene 侧应当全是 miss(no_scope)，
    # 冷缓存侧应当全是 miss(not_ready)。
    try:
        from prism.project_catalog import catalog_scope_counters
        report['measurements']['catalog_scope_counters_after_counts'] = \
            catalog_scope_counters()
    except Exception as error:
        report['measurements']['catalog_scope_counters_after_counts'] = \
            dict(error=repr(error))

    def canvas_diff(left, right):
        left = dict(left or {})
        right = dict(right or {})
        keys = sorted(set(left) | set(right))
        rows = []
        for key in keys:
            a, b = left.get(key), right.get(key)
            if a != b:
                rows.append(dict(canvas_id=key, catalog=a, scene=b,
                                 delta=(None if a is None or b is None
                                        else b - a)))
        return dict(equal=not rows, differing_canvases=len(rows),
                    total_catalog=sum(left.values()) if left else 0,
                    total_scene=sum(right.values()) if right else 0,
                    canvas_count_catalog=len(left), canvas_count_scene=len(right),
                    differences=rows[:50])

    report['comparisons']['count_total'] = dict(
        catalog=counts_dir['total'], scene=counts_scene['total'],
        cold=counts_cold['total'],
        equal=counts_dir['total'] == counts_scene['total'],
        delta=(None if counts_dir['total'] is None or counts_scene['total'] is None
               else counts_scene['total'] - counts_dir['total']),
        cold_equals_scene=counts_cold['total'] == counts_scene['total'],
        cold_equals_catalog=counts_cold['total'] == counts_dir['total'])
    report['comparisons']['count_canvas'] = canvas_diff(
        counts_dir['canvases'], counts_scene['canvases'])
    report['comparisons']['count_canvas']['cold'] = canvas_diff(
        counts_cold['canvases'], counts_scene['canvases'])
    report['comparisons']['count_unclassified'] = dict(
        catalog=counts_dir['unclassified'], scene=counts_scene['unclassified'],
        cold=counts_cold['unclassified'],
        equal=counts_dir['unclassified'] == counts_scene['unclassified'],
        cold_equals_scene=(counts_cold['unclassified']
                           == counts_scene['unclassified']))
    report['comparisons']['count_trash'] = dict(
        catalog_top_level=counts_dir['trash_top_level'],
        scene_top_level=counts_scene['trash_top_level'],
        cold_top_level=counts_cold['trash_top_level'],
        equal_top_level=(counts_dir['trash_top_level']
                         == counts_scene['trash_top_level']),
        catalog_assets=counts_dir['trash_assets'],
        scene_assets=counts_scene['trash_assets'],
        cold_assets=counts_cold['trash_assets'],
        equal_assets=counts_dir['trash_assets'] == counts_scene['trash_assets'],
        cold_equals_scene=(counts_cold['trash_top_level']
                           == counts_scene['trash_top_level']
                           and counts_cold['trash_assets']
                           == counts_scene['trash_assets']))
    report['comparisons']['count_untagged'] = dict(
        catalog=counts_dir['untagged'], scene=counts_scene['untagged'],
        cold=counts_cold['untagged'],
        equal=counts_dir['untagged'] == counts_scene['untagged'])

    # ── 2) 检索 ───────────────────────────────────────────────────
    # 目录口径 = `scope.iter_query(text=…)`（生产同款目录检索）
    # scene 口径 = `search_project()`（顶部搜索框的内存扫描那一半）
    # 生产顶部搜索框 = `search_project()` **并上** `search_catalog_assets()`
    #   （prism/search_controller.py:299-313）；后者按 `loaded_keys` 跳过
    #   已装载的素材 —— 全量打开时 10 万条全在 scene 里，所以目录那一半
    #   贡献 0 条，界面上的素材命中集合 = scene 内存集合。
    from prism.project_search import search_project, search_catalog_assets
    from prism.project_search import item_key, searchable_items
    search_results = {}
    loaded_keys = {item_key(item) for item in searchable_items(scene)}
    report['measurements']['loaded_keys'] = len(loaded_keys)
    for word in args.words:
        started = time.perf_counter()
        catalog_ids = []
        for row in scope.iter_query(text=word, limit=1024):
            catalog_ids.append(int(row['id']))
        catalog_ms = (time.perf_counter() - started) * 1000

        started = time.perf_counter()
        hits = search_project(scene, service, word, panels=())
        scene_ms = (time.perf_counter() - started) * 1000
        asset_hits = [hit for hit in hits if hit.get('kind') == 'asset']
        scene_ids = []
        for hit in asset_hits:
            key = hit.get('item_id')
            if key is None:
                key = str(hit.get('result_id', '')).split(':', 1)[-1]
            try:
                scene_ids.append(int(key))
            except (TypeError, ValueError):
                scene_ids.append(None)
        kinds = {}
        for hit in hits:
            kinds[hit.get('kind')] = kinds.get(hit.get('kind'), 0) + 1

        # 生产顶部搜索框的另一半：目录侧候选（跳过已装载的素材）。
        started = time.perf_counter()
        try:
            catalog_hits = search_catalog_assets(scope, scene, service, word,
                                                 loaded_keys=loaded_keys)
        except Exception as error:
            catalog_hits = []
            log('search_catalog_assets failed for %r: %r' % (word, error))
        catalog_hit_ms = (time.perf_counter() - started) * 1000
        catalog_hit_ids = []
        for hit in catalog_hits:
            try:
                catalog_hit_ids.append(int(hit.get('item_id')))
            except (TypeError, ValueError):
                catalog_hit_ids.append(None)

        catalog_set, scene_set = set(catalog_ids), set(scene_ids)
        only_catalog = sorted(catalog_set - scene_set)
        only_scene = sorted(scene_set - catalog_set)
        search_results[word] = dict(
            catalog=dict(milliseconds=round(catalog_ms, 2), hits=len(catalog_ids),
                         unique=len(catalog_set)),
            scene=dict(milliseconds=round(scene_ms, 2), total_results=len(hits),
                       asset_hits=len(scene_ids), unique=len(scene_set),
                       by_kind=kinds),
            production_top_search_box=dict(
                milliseconds=round(catalog_hit_ms, 2),
                catalog_side_hits=len(catalog_hits),
                catalog_side_ids=catalog_hit_ids,
                note='search_project + search_catalog_assets(loaded_keys=...)；'
                     '全量打开时目录那一半贡献 0 条'),
            intersection=sorted(catalog_set & scene_set),
            intersection_count=len(catalog_set & scene_set),
            catalog_only=only_catalog,
            catalog_only_count=len(only_catalog),
            scene_only=only_scene,
            scene_only_count=len(only_scene),
            equal=(catalog_set == scene_set),
            catalog_only_descriptions=_catalog_descriptions(scope, only_catalog),
            scene_only_descriptions=_scene_descriptions(scene, only_scene),
            sample_catalog_only=catalog_ids[:10],
            sample_scene_only=scene_ids[:10])
        log('search %r: catalog %d / scene %d assets, only-catalog %d, only-scene %d, '
            'catalog-side-of-top-box %d'
            % (word, len(catalog_ids), len(scene_ids),
               len(catalog_set - scene_set), len(scene_set - catalog_set),
               len(catalog_hits)))
        del hits, asset_hits, scene_ids, catalog_ids, catalog_hits, catalog_hit_ids
    report['comparisons']['search'] = search_results

    # ── 3) 素材列表：条数 + 顺序 ─────────────────────────────────
    list_results = {}
    # PowerShell 会吞掉空字符串参数，所以用 <empty> 代表「无过滤」。
    for text in ['' if value == '<empty>' else value
                 for value in args.list_texts]:
        started = time.perf_counter()
        dir_rows = catalog_dir.query(text=text, limit=None)
        dir_ms = (time.perf_counter() - started) * 1000
        dir_ids = _ids(dir_rows)
        del dir_rows

        started = time.perf_counter()
        scene_rows = catalog_scene.query(text=text, limit=None)
        scene_ms = (time.perf_counter() - started) * 1000
        scene_ids = _ids(scene_rows)
        del scene_rows

        started = time.perf_counter()
        cold_rows = catalog_cold.query(text=text, limit=None)
        cold_ms = (time.perf_counter() - started) * 1000
        cold_ids = _ids(cold_rows)
        del cold_rows

        diff = _sequence_diff(dir_ids, scene_ids)
        diff['catalog_milliseconds'] = round(dir_ms, 2)
        diff['scene_milliseconds'] = round(scene_ms, 2)
        diff['cold_milliseconds'] = round(cold_ms, 2)
        diff['catalog_count'] = len(dir_ids)
        diff['scene_count'] = len(scene_ids)
        diff['cold_count'] = len(cold_ids)
        diff['cold_equals_scene'] = cold_ids == scene_ids
        diff['first_20_catalog'] = dir_ids[:20]
        diff['first_20_scene'] = scene_ids[:20]
        diff['first_20_cold'] = cold_ids[:20]
        diff['first_1000_equal'] = dir_ids[:1000] == scene_ids[:1000]
        diff['first_1000_catalog'] = dir_ids[:1000]
        diff['first_1000_scene'] = scene_ids[:1000]
        list_results[text] = diff
        log('list %r: catalog %d / scene %d / cold %d rows, equal=%s'
            % (text, len(dir_ids), len(scene_ids), len(cold_ids), diff['equal']))
        del dir_ids, scene_ids, cold_ids
    report['comparisons']['list'] = list_results

    report['environment_after'] = _env_snapshot(evidence)
    report['real_project_after'] = stat_of(real_project)
    report['real_project_unchanged'] = (
        report['real_project_before'].get('size_bytes')
        == report['real_project_after'].get('size_bytes')
        and report['real_project_before'].get('mtime_ns')
        == report['real_project_after'].get('mtime_ns'))
    report['finished'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    write_json(out_dir / 'consistency.json', report)
    log('source unchanged: %s' % report['real_project_unchanged'])
    return 0


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)

    copy_parser = sub.add_parser('copy', help='byte copy with dual sha256')
    copy_parser.add_argument('--source', required=True)
    copy_parser.add_argument('--dest', required=True)
    copy_parser.add_argument('--out', default=None)
    copy_parser.set_defaults(func=cmd_copy)

    facts_parser = sub.add_parser('facts', help='scale facts (read-only URI)')
    facts_parser.add_argument('--project', required=True)
    facts_parser.add_argument('--out', default=None)
    facts_parser.add_argument('--archive', action='store_true',
                              help='also scan the sqlar archive table (one full '
                                   'table scan over 51.8 GiB on a spinning disk)')
    facts_parser.set_defaults(func=cmd_facts)

    compare_parser = sub.add_parser('compare', help='catalog vs scene caliber')
    compare_parser.add_argument('--project', required=True,
                                help='real project (read-only reference)')
    compare_parser.add_argument('--scene-project', default=None,
                                help='file loaded into the scene; default = the '
                                     'real project (loader is forced onto mode=ro, '
                                     'so no copy and no write access is needed)')
    compare_parser.add_argument('--cache', default=None,
                                help='catalog cache path; default = the production '
                                     'path (%%APPDATA%%\\Prism\\catalog\\<name>.sqlite)')
    compare_parser.add_argument('--production-cache', default=None,
                                help='the production catalog cache of the real project')
    compare_parser.add_argument('--out', required=True)
    compare_parser.add_argument('--label', default='e5-consistency')
    compare_parser.add_argument('--words', nargs='+', default=['wood', 'a'])
    compare_parser.add_argument('--list-texts', nargs='+',
                                default=['<empty>', 'wood', 'a'],
                                help='<empty> = 无过滤（全部素材）；PowerShell 会吞掉'
                                     '空字符串参数，所以用 <empty> 代表')
    compare_parser.add_argument('--no-build', action='store_true',
                                help='do not build the catalog; fail if not ready')
    compare_parser.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == '__main__':
    raise SystemExit(main())
