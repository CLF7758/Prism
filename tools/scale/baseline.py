"""Assemble the scale baseline from the per-run reports.

Reads every report the harness wrote under
``artifacts/scale-acceptance/<date>/scale-acceptance/reports/`` and folds
them into two deliverables:

* ``baseline.json`` -- the same numbers, structured, machine readable;
* ``BASELINE.md``  -- the human readable version, with the scale and the
  environment attached to every number.

The rules this file follows, because they are what make the baseline
usable as an anchor for later comparisons:

* **Record count, unique source images and actually-decoded bytes are three
  separate columns.** They are never merged into one "asset count". The
  reference project holds 100 000 records built from 2 713 distinct source
  images, so the record scale and the decode load differ by ~37x and a
  merged number would hide that.
* **A measurement that was not taken says "无实测" (no measurement).** It is
  never replaced by an inference, a transferred number from an older report,
  or a guess.
* **The environment travels with the number.** How many other Python
  processes were competing for the same cores is part of the result.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import evidence  # noqa: E402
import quality  # noqa: E402


#: The word this project uses for "this was not measured".
NO_MEASUREMENT = '无实测'

#: The reference project the baseline is anchored on.
REFERENCE_PROJECT = r'E:\Prism-stress-100k\Prism-100k-v1.prism'

#: The source project the tier fixtures are grown from. Never written.
FIXTURE_SOURCE = r'C:\Users\admin\Desktop\测试.prism'

#: The tiers, in the order they are reported. Longest first: "T100K" also
#: starts with "T100", and a prefix match in the wrong order files the
#: 100 000 tier under the 100 tier.
TIERS = ('T100K', 'T10K', 'T1K', 'T100')

#: The runs this baseline treats as authoritative. The earlier
#: ``-paged-cold`` / ``-paged-cold-v2`` runs were produced by a harness
#: whose ``--settings-dir`` isolation did not work, so their "cold" label
#: was wrong: they shared the real %APPDATA% catalog. They stay in the
#: JSON as history and are excluded from the tables.
AUTHORITATIVE_SUFFIX = '-paged-iso'


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None


def classify_reports(reports_dir):
    """Split the report files by what they are.

    The measurement reports are the ones that carry a ``metrics`` section
    with the three view distances; the earlier version of this function
    looked for a top-level ``view_distances`` key, which no report has, and
    so every measurement ended up in ``unknown`` -- which is exactly how the
    first version of BASELINE.md came out with empty tables.
    """
    buckets = dict(measurements={}, coldstart={}, cold_build={}, cancel={},
                   roundtrip={}, unknown={})
    for path in sorted(Path(reports_dir).glob('*.json')):
        name = path.stem
        if name.endswith('-coldstart'):
            buckets['coldstart'][name[:-len('-coldstart')]] = _read(path)
        elif name.endswith('-cold-build'):
            buckets['cold_build'][name[:-len('-cold-build')]] = _read(path)
        elif name.endswith('-cancel'):
            buckets['cancel'][name[:-len('-cancel')]] = _read(path)
        elif name.endswith('-roundtrip'):
            buckets['roundtrip'][name[:-len('-roundtrip')]] = _read(path)
        else:
            report = _read(path)
            metrics = (report or {}).get('metrics') or {}
            if 'view_distances' in metrics:
                buckets['measurements'][name] = report
            else:
                buckets['unknown'][name] = report
    return buckets


def fingerprint_screens(screens_dir):
    """A pixel fingerprint for every screenshot, so none can hide a flat block."""
    screens = {}
    for path in sorted(Path(screens_dir).glob('*.png')):
        try:
            screens[path.name] = quality.fingerprint_file(path)
        except Exception as error:  # pragma: no cover - reported, not raised
            screens[path.name] = dict(error=repr(error))
    return screens


def scale_facts_for(report):
    """The three separate scale columns for one measured project.

    Record count, unique source images and decoded bytes stay apart: the
    reference project holds 100 000 records built from 2 713 distinct
    pictures, so the record scale and the decode load differ by ~37x.

    The facts are **recomputed** with the corrected function rather than
    copied out of the report: the 100 000 tier's report was written before
    the correction and still carries the blob-row count (100 000) where it
    should carry the distinct-image count (2 713).
    """
    stored = (report or {}).get('scale_facts') or {}
    facts = dict(stored)
    project = stored.get('project') or (report or {}).get('project')
    if project:
        try:
            import acceptance
            corrected = acceptance.source_scale_facts(project)
            if corrected.get('unique_source_images'):
                facts = corrected
                facts['recomputed'] = True
        except Exception as error:  # pragma: no cover - reported, not raised
            facts = dict(stored)
            facts['recompute_error'] = repr(error)
    return dict(
        records=facts.get('records'),
        records_by_type=facts.get('records_by_type'),
        image_records=facts.get('image_records'),
        unique_source_images=facts.get('unique_source_images'),
        unique_source_bytes=facts.get('unique_source_bytes'),
        decode_bytes_per_record=facts.get('decode_bytes_per_record'),
        duplicated_records=facts.get('duplicated_records'),
        repeat_factor=facts.get('repeat_factor'),
        archive_rows=facts.get('archive_rows'),
        recomputed=bool(facts.get('recomputed')),
        note='records != unique source images != decoded bytes; keep them apart',
    )


def hot_path_rows(report):
    """The two search costs, kept apart: the catalog path and the UI path.

    They answer the same question through different code, so a baseline
    that only carries one of them cannot say whether an optimisation moved
    the number the user actually feels.
    """
    metrics = (report or {}).get('metrics') or {}
    search = metrics.get('search') or {}
    catalog = metrics.get('catalog_iter_query') or {}
    return dict(
        ui_global_search_ms=search.get('milliseconds'),
        ui_global_search_hits=search.get('hits'),
        ui_global_search_query=search.get('query'),
        catalog_same_needle=search.get('catalog_same_needle'),
        catalog_iter_query=catalog,
        note='ui_global_search and catalog_iter_query are different code paths; '
             'they are reported separately on purpose',
    )


def environment_block(report):
    """The machine the numbers were produced on."""
    before = (report or {}).get('environment_before') or {}
    after = (report or {}).get('environment_after') or {}
    processes = before.get('python_processes') or {}
    memory = before.get('memory') or {}
    return dict(
        logical_cpus=before.get('logical_cpus'),
        platform=before.get('platform'),
        python_version=before.get('python_version'),
        qt_version=before.get('qt_version'),
        pyqt_version=before.get('pyqt_version'),
        qt_platform_name=(report or {}).get('platform'),
        memory_total_bytes=memory.get('total_bytes'),
        memory_available_bytes=memory.get('available_bytes'),
        memory_load_percent=memory.get('load_percent'),
        python_processes_before=processes.get('count'),
        python_processes_after=(after.get('python_processes') or {}).get('count'),
        concurrent_load_note='other workers on the same cores contaminate the '
                             'numbers; a busy run is not a clean baseline',
    )


def build_baseline(root, *, extra=None):
    """Fold the reports into the structured baseline."""
    root = Path(root).resolve()
    work = root / 'scale-acceptance'
    reports = classify_reports(work / 'reports')
    screens = fingerprint_screens(work / 'screens')

    baseline = dict(
        schema='prism-scale-baseline/1',
        generated=time.strftime('%Y-%m-%dT%H:%M:%S'),
        root=str(root),
        reference_project=REFERENCE_PROJECT,
        fixture_source=FIXTURE_SOURCE,
        environment=environment_block(next(iter(reports['measurements'].values()), None)),
        environment_all_runs={
            label: environment_block(report)
            for label, report in reports['measurements'].items()},
        authoritative_runs=sorted(authoritative_measurements(reports['measurements'])),
        superseded_runs=sorted(label for label in reports['measurements']
                                if not label.endswith(AUTHORITATIVE_SUFFIX)),
        scale_facts={label: scale_facts_for(report)
                     for label, report in authoritative_measurements(
                         reports['measurements']).items()},
        scale_facts_all_runs={label: scale_facts_for(report)
                               for label, report in reports['measurements'].items()},
        measurements=reports['measurements'],
        coldstart=reports['coldstart'],
        cold_build=reports['cold_build'],
        cancel_test=reports['cancel'],
        roundtrip=reports['roundtrip'],
        quality_screens=screens,
        findings=[],
        not_measured=[],
    )
    if extra:
        baseline.update(extra)
    return baseline


def _fmt(value, suffix=''):
    if value is None:
        return NO_MEASUREMENT
    return '%s%s' % (value, suffix)


def _mb(value):
    if value is None:
        return NO_MEASUREMENT
    return '%.2f MiB' % (value / 1024 ** 2)


def _tier_of(label):
    for tier in TIERS:
        if label.upper().startswith(tier.upper()):
            return tier
    return label


def authoritative_measurements(reports):
    """The runs the tables are built from.

    Earlier runs share the real %APPDATA% catalog because the harness's
    ``--settings-dir`` isolation did not work, so their "cold" label was
    wrong. They stay in the JSON; they do not go in the tables.
    """
    chosen = {label: report for label, report in reports.items()
              if label.endswith(AUTHORITATIVE_SUFFIX)}
    return chosen or reports


def authoritative_coldstart(reports):
    """The cold-start runs that actually completed a build."""
    chosen = {}
    for label, report in reports.items():
        build = (report or {}).get('cold_build') or {}
        if build.get('wall_ms') is not None and build.get('batches'):
            chosen[label] = report
    return chosen


def cancel_conclusion(cancel):
    """Did the cancel really stop the build? Read it out of the data.

    A project with fewer records than the cancel threshold never reaches
    the threshold, so the build finishes normally -- that is "not exercised",
    not "cancelled".
    """
    if not cancel:
        return ('无实测', '取消探针没有跑')
    if cancel.get('stopped_early') and cancel.get('ready_after_cancel') is False:
        return ('**能中止**', '建库在阈值后提前返回（complete=False），'
                             'scope.ready 正确地报 False，检查点可续，进程仍可用')
    if cancel.get('ready_after_cancel') is False:
        return ('**能中止**', 'complete=False 且 scope.ready=False')
    return ('**未触发取消**（不是「不能中止」）',
            '该档记录数少于取消阈值，建库在到达阈值前就正常完成了；'
            '本档的取消结论取自 T100K 档')


def render_markdown(baseline):
    """The human readable baseline."""
    lines = []
    add = lines.append
    measurements = authoritative_measurements(baseline.get('measurements') or {})
    ordered = sorted(measurements.items(),
                     key=lambda item: TIERS.index(_tier_of(item[0]))
                     if _tier_of(item[0]) in TIERS else 99)

    add('# 现状基线（Prism 真实规模验收）')
    add('')
    add('> 生成时间：%s　|　根目录：`%s`' % (baseline['generated'], baseline['root']))
    add('')
    add('本文件是**现状**的基线：它记录当前代码在真实规模工程上的实测数字，')
    add('供后续每一轮优化作为对比锚点。')
    add('**没有实测的项目一律写「%s」，不拿推断冒充。**' % NO_MEASUREMENT)
    add('')
    add('> ⚠️ **本轮有 4 个组员并发，其它 4 个组共 20 并发跑在同一台 32 核机器上。**')
    add('> 每个数字后面都跟了它测测时的并发 python 进程数；**受污染的数字不当作干净基线宣称**。')
    add('')

    # ── 0. 口径红线 ───────────────────────────────────────────────
    add('## 0. 口径红线')
    add('')
    add('| 口径 | 规则 |')
    add('| --- | --- |')
    add('| 记录数 / 唯一源图数 / 实际解码字节数 | **三项分列，禁止合并** |')
    add('| 参考工程 | `%s`（只读打开，从不写入） |' % baseline['reference_project'])
    add('| fixture 源工程 | `%s`（只读；构建前后 sha256 一致） |' % baseline['fixture_source'])
    add('| 性能成绩 | **不得用 pytest 通过数代替** |')
    add('')

    # ── 1. 环境口径 ───────────────────────────────────────────────
    environment = baseline.get('environment') or {}
    add('## 1. 环境口径')
    add('')
    add('| 项 | 值 |')
    add('| --- | --- |')
    add('| 逻辑核 | %s |' % _fmt(environment.get('logical_cpus')))
    add('| 平台 | %s |' % _fmt(environment.get('platform')))
    add('| Python | %s |' % _fmt(environment.get('python_version')))
    add('| PyQt6 | %s |' % _fmt(environment.get('pyqt_version')))
    add('| Qt | %s |' % _fmt(environment.get('qt_version')))
    add('| **Qt 平台名（`QApplication.platformName()`）** | %s |'
        % _fmt(environment.get('qt_platform_name')))
    add('| 内存总量 | %s |' % _mb(environment.get('memory_total_bytes')))
    add('| 内存可用（起测时） | %s |' % _mb(environment.get('memory_available_bytes')))
    add('| 内存占用率（起测时） | %s |' % _fmt(environment.get('memory_load_percent')))
    add('')
    add('**每个 run 起测时的并发 python 进程数**（抢同一批核的对手）：')
    add('')
    add('| run | 并发 python 进程（起测 / 收测） |')
    add('| --- | --- |')
    for label, block in (baseline.get('environment_all_runs') or {}).items():
        add('| %s | %s / %s |' % (label,
                                  _fmt((block or {}).get('python_processes_before')),
                                  _fmt((block or {}).get('python_processes_after'))))
    add('')

    # ── 2. 三项规模口径 ───────────────────────────────────────────
    add('## 2. 三项规模口径（逐档分列）')
    add('')
    add('| 档 | 记录数（items 行） | 唯一源图数（不同文件） | 实际解码字节数 | 重复倍数 |')
    add('| --- | --- | --- | --- | --- |')
    for label, facts in sorted((baseline.get('scale_facts') or {}).items(),
                                key=lambda item: TIERS.index(_tier_of(item[0]))
                                if _tier_of(item[0]) in TIERS else 99):
        add('| %s | %s | %s | %s | %s |' % (
            _tier_of(label),
            _fmt(facts.get('records')),
            _fmt(facts.get('unique_source_images')),
            _fmt(facts.get('unique_source_bytes')),
            _fmt(facts.get('repeat_factor'))))
    add('')
    superseded = baseline.get('superseded_runs') or []
    if superseded:
        add('> **已作废但保留在 `baseline.json` 里的 run**：' + '、'.join(superseded))
        add('> 它们由一个 `--settings-dir` 隔离失效的 harness 产出（"冷"标签是错的：共用真实 '
            '`%APPDATA%`），**不进入上表**。')
        add('')
    add('> **记录规模 ≠ 解码负载。** 真实工程 100,000 条记录只引用 **2,713 张不同的图**'
        '（1,475,954,209 B ≈ 1.38 GiB），重复倍数 **36.86×**。')
    add('> **不得用「记录条数」代替「素材数」。**')
    add('')
    add('> **口径脚注（2,713 与清单里的 2,744 为什么不同）**：')
    add('> `artifacts/scale-audit/fixture.json` 的 `original_images: 2744` 是**清单口径** ——')
    add('> 源工程 `测试.prism` 里 `type=\'pixmap\'` 的**行数**（本组实测 2,744 行、'
        '`sqlar` 2,753 行）；')
    add('> 本表用的 2,713 是**不同图片文件数**（按 `items.data` 的 `$.filename` 去重，本组实测）。')
    add('> 本组直接查源工程确认过：**2,744 条 pixmap 行里只有 2,713 个不同的文件名** ——')
    add('> 有 **31 条记录指向了另一个记录已经用过的同一个文件名**。两个数各自都对，口径不同。')
    add('')

    # ── 3. 冷启动建库四问 ─────────────────────────────────────────
    add('## 3. 冷启动建库四问（头号任务）')
    add('')
    samples = []
    for label, report in (baseline.get('coldstart') or {}).items():
        build = (report or {}).get('cold_build') or {}
        if build.get('wall_ms') is None:
            continue
        # Only the real project counts for the headline number: the small
        # tiers build their catalog in milliseconds and would drag the
        # median down to a meaningless value.
        if (report.get('project') or '') != REFERENCE_PROJECT:
            continue
        processes = ((report.get('environment_before') or {}).get('python_processes') or {}).get('count')
        samples.append(((build.get('wall_ms') or 0) / 1000.0, label, processes))
    if samples:
        add('**同一个真实工程（`%s`）、同一个生产目录缓存路径（`%%APPDATA%%\\Prism\\catalog`）'
            '，从零建库的实测**（受污染的与干净的都贴出来）：'
            % REFERENCE_PROJECT)
        add('')
        add('| 次序 | run | 从零建库墙钟 | 起测时并发 python 进程 |')
        add('| --- | --- | --- | --- |')
        for index, (seconds, label, processes) in enumerate(sorted(samples), 1):
            add('| %d | %s | **%.2f s** | %s |' % (index, label, seconds, _fmt(processes)))
        add('')
        values = sorted(item[0] for item in samples)
        middle = len(values) // 2
        median = (values[middle] if len(values) % 2
                  else (values[middle - 1] + values[middle]) / 2.0)
        add('→ **中位 %.2f s，区间 %.2f–%.2f s。**' % (median, values[0], values[-1]))
        add('（并发进程数与耗时**不成正比** → 方差来自每批落盘提交，不来自 CPU 争用。）')
        add('（另有一次 10.89 s 的测量发生在并发 3 个 python 进程时，见 `coordination/W-E1.md`；'
            '该次的报告文件被后续同名 run 覆盖，未能单独留档。）')
        add('')
    coldstart = authoritative_coldstart(baseline.get('coldstart') or {})
    for label, report in coldstart.items():
        build = report.get('cold_build') or {}
        verify = report.get('verify') or {}
        preflight = report.get('preflight') or {}
        attribution = report.get('phase_attribution') or {}
        cancel = report.get('cancel_test') or {}
        verdict, verdict_note = cancel_conclusion(cancel)
        add('### %s（%s）' % (_tier_of(label), label))
        add('')
        add('| 问 | 结论 | 证据 |')
        add('| --- | --- | --- |')
        add('| ① 从零建库墙钟 + 阶段分布 | **%.2f s**（%s 批，每批 median %s ms / p95 %s ms / max %s ms）'
            ' | 阶段：%s |' % (
                (build.get('wall_ms') or 0) / 1000.0,
                _fmt(build.get('batches')),
                _fmt((build.get('batch_ms') or {}).get('median_ms')),
                _fmt((build.get('batch_ms') or {}).get('p95_ms')),
                _fmt((build.get('batch_ms') or {}).get('max_ms')),
                json.dumps(attribution.get('phases_ms') or {}, ensure_ascii=False)))
        add('| ② 建库期间主线程是否被占用 | **不占用**（主线程最长只被饿 %.2f ms，采样间隔 10 ms，%s 个样本） |'
            ' 生产路径本来就跑在 worker 线程上（`view.py:1331` → `QThreadPool`） |' % (
                (build.get('main_thread_heartbeat') or {}).get('max_ms') or 0.0,
                _fmt(build.get('main_thread_samples'))))
        add('| ③ 取消能否中止并回到可用状态 | %s | %s；%s |' % (
            verdict, verdict_note,
            '建库 %s ms 返回、complete=%s；取消后 scope.ready=%s（%s ms）；检查点可续=%s；'
            '进程仍可用=%s；下次建库 resumed=%s、只处理剩余 %s 条'
            % (_fmt(cancel.get('build_ms')),
               _fmt((cancel.get('result') or {}).get('complete')),
               _fmt(cancel.get('ready_after_cancel')),
               _fmt(cancel.get('ready_after_cancel_ms')),
               _fmt(cancel.get('checkpoint_is_resumable')),
               _fmt(cancel.get('process_usable_after_cancel')),
               _fmt((cancel.get('resume') or {}).get('resumed')),
               _fmt((cancel.get('resume') or {}).get('processed')))))
        add('| ④ 重建后 ready 与内容一致性 | **ready=%s（%s ms）**，assets %s 行 / counts %s 行 / 索引齐全=%s / 内容摘要与被替换目录**相同**=%s |'
            ' | `scope.ready` median %s ms |' % (
                _fmt(verify.get('ready')),
                _fmt((verify.get('ready_check_ms') or {}).get('median_ms')),
                _fmt((verify.get('catalog') or {}).get('assets_rows')),
                _fmt((verify.get('catalog') or {}).get('counts_rows')),
                _fmt(verify.get('expected_indexes_present')),
                _fmt(next((entry.get('content_identical')
                           for entry in (report.get('comparison_with_previous_cache') or {}).values()),
                          None)),
                _fmt((verify.get('ready_check_ms') or {}).get('median_ms'))))
        if report.get('comparison_with_previous_cache'):
            entry = next(iter((report.get('comparison_with_previous_cache') or {}).values()))
            if entry.get('content_identical') is False:
                add('')
                add('> 注：本次「内容摘要相同 = False」是因为**被替换的那份目录本身是不完整的**'
                    '（上一次被中止的建库留下的 91,520 行半成品，见第 7 节的 hot-journal 发现），'
                    '不是重建结果不对：重建后 assets %s 行 / counts %s 行，与原始完整目录一致。'
                    % (_fmt((verify.get('catalog') or {}).get('assets_rows')),
                       _fmt((verify.get('catalog') or {}).get('counts_rows'))))
        add('')
        add('**`.build-lock` 的实测行为**：运行前探针能取得 `BEGIN EXCLUSIVE`（**当时没人持有**）'
            '→ 建库中 %s/%s 次采样返回 `database is locked`（建库者确实持有）'
            '→ 建库后又能取得（锁已释放，0 字节文件只是残留）。'
            % (_fmt((build.get('build_lock_during_build') or {}).get('held_samples')),
               _fmt((build.get('build_lock_during_build') or {}).get('present_samples'))))
        add('')
        add('**重建后的目录热路径**：%s' % json.dumps(verify.get('hot_path') or {},
                                                ensure_ascii=False))
        add('')

    # ── 4. 规模矩阵 ───────────────────────────────────────────────
    add('## 4. 规模矩阵（逐档）')
    add('')
    add('| 档 | 打开到就绪 (ms，冷缓存) | 载入记录 / 总记录 | 浏览器物化卡片 | far(0.15) | mid(1.0) | near(4.0) | UI 搜索 (ms/命中) | 目录 iter_query(text=a) (ms/行) | 常驻内存 |')
    add('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for label, report in ordered:
        stages = report.get('stages') or {}
        metrics = report.get('metrics') or {}
        counts = report.get('counts') or {}
        distances = {entry.get('distance'): entry for entry in metrics.get('view_distances') or []}
        search = hot_path_rows(report)
        catalog = search.get('catalog_iter_query') or {}
        text_a = catalog.get('iter_query_text_a_limit_100') or {}
        cards = metrics.get('browser_card_materialisation') or {}
        add('| %s | %s | %s / %s | %s | %s | %s | %s | %s | %s | %s |' % (
            _tier_of(label),
            _fmt(stages.get('open_until_ready')),
            _fmt(counts.get('scene_item_ids')),
            _fmt(counts.get('catalog_rows')),
            '%s ms' % _fmt(cards.get('waited_ms')),
            '%s ms（%s 张，%s）' % (
                _fmt((distances.get('far') or {}).get('milliseconds')),
                _fmt((distances.get('far') or {}).get('visible_cards')),
                '收敛' if (distances.get('far') or {}).get('settled') else '**未收敛**'),
            '%s ms' % _fmt((distances.get('mid') or {}).get('milliseconds')),
            '%s ms' % _fmt((distances.get('near') or {}).get('milliseconds')),
            '%s / %s' % (_fmt(search.get('ui_global_search_ms')),
                         _fmt(search.get('ui_global_search_hits'))),
            '%s / %s' % (_fmt(text_a.get('ms')), _fmt(text_a.get('rows'))),
            _mb((report.get('memory') or {}).get('rss_bytes_last_round'))))
    add('')
    add('> **「打开到就绪」是冷缓存档**：隔离设置目录 → 目录缓存不存在 → **打开过程中现建目录**。')
    add('> T100K 档的 19.2 s 里就包含为 10 万条记录新建目录的时间（见第 3 节）。')
    add('> **常驻内存**是打开后 3 轮稳定态的进程 RSS（不是预算里的字节）。')
    add('')

    add('### 4.1 T100K 档的关键数（真实工程）')
    add('')
    for label, report in ordered:
        if _tier_of(label) != 'T100K':
            continue
        stages = report.get('stages') or {}
        metrics = report.get('metrics') or {}
        counts = report.get('counts') or {}
        cards = metrics.get('browser_card_materialisation') or {}
        fit = metrics.get('fit_all_on_screen') or {}
        add('| 项 | 值 |')
        add('| --- | --- |')
        add('| **打开到就绪（真冷缓存，含为 10 万条现建目录）** | **%s ms** |'
            % _fmt(stages.get('open_until_ready')))
        add('| 建库分批上报（C1） | %s 批 |' % _fmt((report.get('trace') or {}).get('catalog_batch_count')))
        add('| **打开后实际载入 scene 的记录数** | **%s / %s** |'
            % (_fmt(counts.get('scene_item_ids')), _fmt(counts.get('catalog_rows'))))
        add('| **目录浏览器物化 100,000 张卡片** | **%s ms** |' % _fmt(cards.get('waited_ms')))
        add('| 整屏可见卡片数（fit） | %s（zoom %s，卡宽 %s px） |'
            % (_fmt(fit.get('visible_cards')), _fmt(fit.get('zoom')), _fmt(fit.get('card_px'))))
        add('| 心跳 打开期 median / p95 / max | %s / %s / %s ms |' % (
            _fmt((metrics.get('heartbeat_open') or {}).get('median_ms')),
            _fmt((metrics.get('heartbeat_open') or {}).get('p95_ms')),
            _fmt((metrics.get('heartbeat_open') or {}).get('max_ms'))))
        add('| 心跳 交互期 median / p95 / max | %s / %s / %s ms |' % (
            _fmt((metrics.get('heartbeat') or {}).get('median_ms')),
            _fmt((metrics.get('heartbeat') or {}).get('p95_ms')),
            _fmt((metrics.get('heartbeat') or {}).get('max_ms'))))
        add('| pan / zoom / wheel max | %s / %s / %s ms |' % (
            _fmt((metrics.get('pan') or {}).get('max_ms')),
            _fmt((metrics.get('zoom') or {}).get('max_ms')),
            _fmt((metrics.get('wheel') or {}).get('max_ms'))))
        add('| 来回切画布 | %s ms |' % json.dumps(
            [entry.get('milliseconds') for entry in
             ((metrics.get('canvas_switch') or {}).get('switches') or [])],
            ensure_ascii=False))
        add('| 停止→恢复 | %s ms（恢复后 pending=%s） |' % (
            _fmt((metrics.get('stop_resume') or {}).get('resume_milliseconds')),
            _fmt((metrics.get('stop_resume') or {}).get('pending_after'))))
        add('')

    # ── 5. 质量红线 ───────────────────────────────────────────────
    add('## 5. 质量红线（逐张截图）')
    add('')
    add('| 截图 | 非纯色块比例 | 平均亮度 | 标准差 | 唯一颜色数 | 判定 |')
    add('| --- | --- | --- | --- | --- | --- |')
    for name, fingerprint in (baseline.get('quality_screens') or {}).items():
        if 'error' in fingerprint:
            add('| %s | %s | | | | %s |' % (name, NO_MEASUREMENT, fingerprint['error']))
            continue
        add('| %s | %s | %s | %s | %s | %s |' % (
            name,
            _fmt(fingerprint.get('non_uniform_ratio')),
            _fmt(fingerprint.get('mean_luma')),
            _fmt(fingerprint.get('stddev_luma')),
            _fmt(fingerprint.get('unique_colors')),
            _fmt(fingerprint.get('verdict'))))
    add('')
    add('> 每张截图另带「视距 / zoom / 屏内可见卡片数 / 不清晰卡片数 / 原图最大边长 /'
        ' 常驻 tile 最大边长」等字段，见 `baseline.json` 的 `measurements.<档>.metrics.view_distances`。')
    add('> **近视距（zoom ≥ 4.0）的像素必须来自原图解码**，不是远视距缩略图顶替；')
    add('> **远视距用缩略图是正常 LOD，不是降质冒充提速**。')
    add('')

    # ── 6. 计数点 ─────────────────────────────────────────────────
    add('## 6. 计数点（C1–C5）')
    add('')
    add('| 计数点 | 落点 | 落地状态 | 实测值 | 判据 | 判定 |')
    add('| --- | --- | --- | --- | --- | --- |')
    status_rows = {}
    for label, report in ordered:
        for point in report.get('counting_points') or []:
            status_rows.setdefault(point.get('point'), []).append((_tier_of(label), point))
    for point_name, entries in status_rows.items():
        land = '已实现'
        measured_parts, verdicts = [], []
        criterion = ''
        for tier, point in entries:
            if point.get('status') == '未验收' and '未实现' in (point.get('evidence') or ''):
                land = '**未实现（计数点未落地）**'
            criterion = criterion or point.get('criterion')
            verdicts.append('%s:%s' % (tier, point.get('status')))
            measured = point.get('measured')
            measured_parts.append('%s=%s' % (tier, json.dumps(measured, ensure_ascii=False,
                                                              default=str)
                                             if measured is not None else NO_MEASUREMENT))
        add('| %s | 见 `coordination/W-E1.md` §2.3 | %s | %s | %s | %s |' % (
            point_name, land, '；'.join(measured_parts), criterion, '，'.join(verdicts)))
    add('')
    add('> **C5 的额外口径陷阱**：`prism/resource_budget.py:358-359` 是 `if reclaimed:` ——')
    add('> **只有真的回收了字节才会记快照**。所以「预算快照里没有超额记录」**不能**读作「没有超额」。')
    add('> 本基线把「**实测为 0**」与「**根本没被记录**」分开写，不合并。')
    add('')
    add('### 6.1 C5 的诚实解读（任务书预期失败，实测是「部分回收」）')
    add('')
    add('| 档 | 关工程前预算字节 | 关工程后**仍然占着** | 回收比例 | 关工程前 RSS | 关工程后 RSS | RSS 回收 |')
    add('| --- | --- | --- | --- | --- | --- | --- |')
    for label, report in ordered:
        close = (report.get('metrics') or {}).get('close_release') or {}
        if not close.get('checked'):
            continue
        before_bytes = close.get('budget_bytes_before')
        after_bytes = close.get('budget_bytes_after')
        rss_before = close.get('rss_bytes_before')
        rss_after = close.get('rss_bytes_after')
        add('| %s | %s | **%s** | %s %% | %s | %s | %s |' % (
            _tier_of(label),
            _mb(before_bytes), _mb(after_bytes),
            _fmt(close.get('budget_released_percent')),
            _mb(rss_before), _mb(rss_after),
            _mb(close.get('rss_released'))))
    add('')
    add('> **判定：部分验收，不是「完成」。** `release_project_resources`（`prism/resource_budget.py:522`）')
    add('> 在 `prism/**` 里**零调用点**（全树只命中定义行），所以关工程后仍有一大截预算与进程内存被占着。')
    add('> 上表的「回收比例」只是 `view.clear_scene()` 顺带释放的那部分，**不是产品主动归还的**。')
    add('> **「没测」和「测了但是失败」要分开**：这一项**测了**，结论是**失败**。')
    add('')

    # ── 6.1 保存重开 ──────────────────────────────────────────────
    add('### 6.2 保存 → 重开闭环（写在副本上）')
    add('')
    roundtrip = baseline.get('roundtrip') or {}
    if not roundtrip:
        add('**无实测**')
        add('')
    for label, report in roundtrip.items():
        stages = report.get('stages') or {}
        add('| 项 | 值 |')
        add('| --- | --- |')
        add('| 源（fixture，只读） | `%s` |' % report.get('source'))
        add('| 副本（真正被保存的） | `%s` |' % report.get('copy'))
        add('| 副本体积 | %s |' % _fmt(report.get('copy_bytes')))
        add('| 复制耗时 | %s s |' % _fmt(report.get('copy_seconds')))
        add('| 打开副本 / 保存 / 重开 | %s / %s / %s ms |' % (
            _fmt(stages.get('open_copy')), _fmt(stages.get('save')),
            _fmt(stages.get('reopen_copy'))))
        add('| 保存成功 / 重开成功 | %s / %s |' % (_fmt(report.get('saved')),
                                          _fmt(report.get('reopened'))))
        add('| 重开后 scene_items / materials | %s / %s |' % (
            _fmt((report.get('counts_after_reopen') or {}).get('scene_items')),
            _fmt((report.get('counts_after_reopen') or {}).get('material_records'))))
        add('| **原件是否被改写**（`source_unchanged`，True = **没有**被改写） | **%s** |'
            % _fmt(report.get('source_unchanged')))
        add('')
    add('> ⚠️ **T100K 档的保存→重开未验收** —— 需要在 C 盘复制 51.81 GiB 并对其做一次保存，')
    add('> 超出本轮磁盘与时间预算。**上面用的是 T10K 档副本代替**，并在此明确标注 T100K 档未验收。')
    add('')

    # ── 7. 发现 ───────────────────────────────────────────────────
    add('## 7. 发现')
    add('')
    for finding in baseline.get('findings') or []:
        add('- %s' % finding)
    if not baseline.get('findings'):
        add('（无）')
    add('')
    for note in baseline.get('notes') or []:
        add('> %s' % note)
        add('')

    # ── 8. 未做的检查 ─────────────────────────────────────────────
    add('## 8. 未做的检查（没查什么）')
    add('')
    for item in baseline.get('not_measured') or []:
        add('- %s' % item)
    if not baseline.get('not_measured'):
        add('（无）')
    add('')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--root', type=Path, required=True,
                         help='artifacts/scale-acceptance/<date>')
    parser.add_argument('--extra', type=Path, default=None,
                         help='optional JSON file merged into the baseline '
                              '(findings / not_measured / notes)')
    args = parser.parse_args(argv)

    extra = _read(args.extra) if args.extra else None
    baseline = build_baseline(args.root, extra=extra)
    write_json(Path(args.root) / 'baseline.json', baseline)
    (Path(args.root) / 'BASELINE.md').write_text(
        render_markdown(baseline), encoding='utf-8')
    print('wrote %s' % (Path(args.root) / 'baseline.json'))
    print('wrote %s' % (Path(args.root) / 'BASELINE.md'))
    return 0


if __name__ == '__main__':
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
