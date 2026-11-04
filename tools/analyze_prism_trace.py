#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线分析 Prism JSONL 追踪日志，输出 JSON + 中文 Markdown。

设计前提（写死在这里，避免以后有人当成 bug 修掉）：

* **输入只读。** 本工具从不以写模式打开日志，也不会重命名/轮转它。
* **能读旧日志。** schema v2 新增字段（``trace_id``/``span_id``/
  ``parent_span_id``/``status``/``pid``/``thread_id``/``open_epoch`` …）在旧日志里
  一个都没有。缺哪个就标 ``legacy``/``unknown``，**绝不猜、不伪造父子链**。
* **不同 session 不混算。** 报告按 session 分段，跨 session 的时间轴、
  百分位、资源峰值都不合并。
* **重叠 span 不相加。** 阶段排名与关键路径一律按「区间并集」算独占时间；
  父子重叠会在 ``overlaps`` 里显式列出，避免把 1246+1250 当成 2496 ms。
* **ongoing 重复 stall 不算独立卡顿。** 只有 ``phase=end`` 才计入完整卡顿；
  ``ongoing`` 只在没有对应 end（进程中途死掉）时兜底计入并标注。
* **不完整不补成功。** 缺 end / 未知 span / 末行截断 / 掉队列 / 无 session_end
  一律记 ``incomplete``，异常行**计数**而不是悄悄跳过。
* **百分位必须带样本量与统计对象**，否则不给。
* **对比默认拒绝。** 同硬件 + 同版本 + 同场景三样不齐，就不出提升百分比。

用法::

    python tools/analyze_prism_trace.py LOG_OR_GLOB [...] \\
        --out-dir artifacts/logging-upgrade-20261023/analysis --label 64540
"""
from __future__ import annotations

import argparse
import collections
import glob
import json
import math
import os
import re
import sys
from pathlib import Path

TOOL_VERSION = '2.4.9'
SCHEMA_TARGET = 2
BYTE_CLASSIFICATION_VERSION = 2
BYTE_EVENT_CLASSES = collections.OrderedDict((
    ('stall_diagnostics', frozenset(('gui_stall', 'ui.stall_context',
                                     'gui_recovered', 'slow_dispatch',
                                     'nested_dispatch', 'gc'))),
    ('periodic_samples', frozenset(('performance', 'process',
                                    'material_performance', 'material_cache',
                                    'queue_rollup', 'resource_rollup'))),
    ('lifecycle', frozenset(('session_start', 'session_end', 'operation_start',
                             'operation', 'open_phase'))),
))
PERIODIC_BYTE_EVENTS = BYTE_EVENT_CLASSES['periodic_samples']
STALL_BYTE_EVENTS = BYTE_EVENT_CLASSES['stall_diagnostics']
LIFECYCLE_BYTE_EVENTS = BYTE_EVENT_CLASSES['lifecycle']

#: schema v2 计划新增的字段。缺任何一个都必须在报告里出现，
#: 否则「旧日志和新日志的差异」会被静默吃掉。
SCHEMA_V2_FIELDS = (
    'schema_version', 'pid', 'thread_id', 'thread_name', 'trace_id',
    'span_id', 'parent_span_id', 'project_token', 'open_epoch', 'generation',
    'source_revision', 'origin_action_id', 'priority', 'status',
)

#: 视为「有耗时」的���件：百分位与阶段排名只从这些里取。
SPAN_EVENTS = {
    'operation': 'end',
    'operation_start': 'start',
    'sql.execute': 'end',
    'sql.fetch': 'end',
    'gc': 'end',
    'hotpath_sample': 'end',
    'slow_dispatch': 'end',
    'nested_dispatch': 'end',
    'slow_input_to_paint': 'end',
    'project.sql_read': 'end',
    'project.sql_write': 'end',
}

#: 里程碑：自带 duration_ms，但语义是「阶段完成」，不当 span 参与并集。
MILESTONE_EVENTS = (
    'project_open_finished', 'project_open_ready', 'project_save_finished',
    'gui_recovered', 'project_partial_save_acknowledged',
)

#: GUI 「安装/装配」类工作的名字特征。用于把主线程耗时拆成
#: 安装 vs 其他 GUI 交互。命中不了就是未知，不假装是 0。
GUI_INSTALL_HINTS = (
    'set_widget', 'append_panel', 'canvas_ui', 'rebuild_tree',
    'switch_context', 'install', 'layout', 'set_records', 'cards.push',
    'rebuild', 'populate', 'bind',
)

#: 等待类名字特征。
WAIT_HINTS = ('wait', 'idle', 'await', 'join', 'queue', 'pending', 'block')

MAIN_THREADS = ('MainThread', 'main', 'Main')

#: 工作队列的计数口径（与生产端 ``prism.performance_trace.QUEUE_COUNTERS`` 一致）。
#: ``submitted``=投递了多少，``started``=真的开始了多少，``installed``=到了目的地
#: 多少；其余是「没有变成结果」的那些，必须如实报，不能并成一个数。
QUEUE_COUNTERS = ('submitted', 'started', 'completed', 'installed', 'cancelled',
                  'cancel_after_start', 'stale_discarded', 'rejected', 'retried',
                  'coalesced', 'failed')

#: 队列分类。前七个与生产端 ``QUEUE_CATEGORIES`` 一致，最后一个是缩略图队列。
QUEUE_CATEGORIES = ('image_decode', 'catalog', 'gui_install', 'layout',
                    'material_texture', 'material_batch', 'video_materialize',
                    'video_thumbnail')

#: 资源类：6 个有人测的 + 3 个没人测的。没人测的**不是 0**，是未知。
MEASURED_RESOURCE_CLASSES = ('cpu_decoded', 'encoded_thumbnail', 'tile_composite',
                              'material_texture', 'video_preview',
                              'editor_attachments')
UNMEASURED_RESOURCE_CLASSES = ('gpu', 'temp_disk', 'dirty_records')
RESOURCE_CLASSES = MEASURED_RESOURCE_CLASSES + UNMEASURED_RESOURCE_CLASSES

#: 呈现 marker 事件名 -> 语义。名字与生产端一致，便于检索。
PRESENTATION_MARKERS = {
    'assets.current_content_first_paint': 'content_first_paint',
    'assets.current_scope_interactive': 'scope_interactive',
    'assets.focus_quality_ready': 'focus_quality_ready',
    'assets.focus_quality_pending': 'focus_quality_pending',
    'assets.focus_quality_painted': 'focus_quality_painted',
    'assets.focus_quality_unknown': 'focus_quality_unknown',
    'assets.viewport_quality_ready': 'viewport_quality_ready',
    'assets.viewport_quality_pending': 'viewport_quality_pending',
    'assets.viewport_quality_painted': 'viewport_quality_painted',
    'assets.viewport_quality_unknown': 'viewport_quality_unknown',
}

#: ``assets.paint_frame`` 是**每帧计数器**（默认每秒一行），不是逐图日志。
PAINT_FRAME_EVENT = 'assets.paint_frame'

# D组分层证据：缓存生成/安装与实际视口绘制严格分开。
PRESENTATION_EVIDENCE_EVENTS = frozenset((
    'catalog_query_started', 'catalog_rows_read_evidence',
    'catalog_rows_delivered', 'asset_model_install_progress',
    'asset_model_published', 'catalog_query_cancelled', 'catalog_query_failed',
    'catalog_query_deferred',
    'asset_paint_frame', 'asset_visibility_snapshot',
    'asset_resource_generated', 'asset_cache_installed',
    'presentation_trace_baseline', 'catalog_query_baseline',
    'catalog_query_trace_baseline', 'asset_model_baseline',
))

#: 查询的终态事件：出现其中之一才算「查询已结束」。
#: 没有出现任何终态事件的查询必须显式记为 ``terminal_state='missing'``。
TERMINAL_QUERY_EVENTS = ('catalog_query_cancelled', 'catalog_query_failed',
                          'catalog_query_deferred')

#: stall 归类口径（与生产端 ``classify_stall`` 一致）。
STALL_CLASSIFICATIONS = ('heartbeat_starved', 'single_dispatch_long',
                         'background_stage_long', 'modal_nested_wait',
                         'unclassified')

#: 生产端用来表示「没有」的哨兵字符串。
UNKNOWN_SENTINEL = 'unknown'


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _num(value, default=None):
    """只接受真正的 int/float，bool 和脏值一律回退。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return default


def _count(value, default=None):
    """计数：整数保持整数，脏值回退 ``None``（**不是 0**）。"""
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    return default


def _is_unknown(value):
    """生产端的 ``PKG3_UNKNOWN`` 哨兵与等价的「没有」。"""
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() == UNKNOWN_SENTINEL:
        return True
    return False


def _shown(value, unknown='未知'):
    """给人看的值：未知就是未知，**绝不显示成 0**。"""
    if value is None:
        return unknown
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, (int, float)):
        return value
    return value


def percentile(values, pct):
    """最近秩百分位。样本不足时返回 None —— 没有样本就没有百分位。"""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def distribution(values, subject):
    """带样本量和统计对象的分位数。样本为空就只报 subject + n=0。"""
    payload = {
        'subject': subject,
        'n': len(values),
        'p50': percentile(values, 50),
        'p90': percentile(values, 90),
        'p99': percentile(values, 99),
        'max': max(values) if values else None,
        'min': min(values) if values else None,
    }
    if not values:
        payload['note'] = '样本量为 0，没有百分位；不做任何推断。'
    return {key: (round(val, 3) if isinstance(val, float) else val)
            for key, val in payload.items()}


def union_ms(intervals):
    """区间并集总长（毫秒）。入参是 monotonic **秒**，返回毫秒。

    旧日志的 monotonic 是秒，而调用方一律按毫秒消费，故在此统一换算。
    重叠部分只算一次 —— 这是不叠加重叠 span 的关键。
    """
    if not intervals:
        return 0.0
    ordered = sorted(intervals)
    total = 0.0
    cur_start, cur_end = ordered[0]
    for start, end in ordered[1:]:
        if start > cur_end:
            total += cur_end - cur_start
            cur_start, cur_end = start, end
        else:
            cur_end = max(cur_end, end)
    total += cur_end - cur_start
    return total * 1000.0


def overlaps_of(spans):
    """列出相互重叠的 span 对及其重叠毫秒数（start_mono/end_mono 是秒）。"""
    result = []
    ordered = sorted(spans, key=lambda s: (s['start_mono'], s['end_mono']))
    for i, left in enumerate(ordered):
        for right in ordered[i + 1:]:
            if right['start_mono'] >= left['end_mono']:
                break
            shared = (min(left['end_mono'], right['end_mono'])
                      - right['start_mono']) * 1000.0
            if shared <= 0:
                continue
            nested = (left['start_mono'] <= right['start_mono']
                      and left['end_mono'] >= right['end_mono'])
            result.append({
                'a_seq': left['seq'], 'a_name': left['name'],
                'a_ms': round(left['dur_ms'], 3),
                'b_seq': right['seq'], 'b_name': right['name'],
                'b_ms': round(right['dur_ms'], 3),
                'overlap_ms': round(shared, 3),
                'relation': 'nested(疑似父子)' if nested else 'parallel',
                'sum_would_be': round(left['dur_ms'] + right['dur_ms'], 3),
                'note': '父子/并行耗时不叠加，排名按并集独占时间算。',
            })
    return result


def fmt_ms(value):
    if value is None:
        return '未知'
    if abs(value) >= 1000:
        return '%.3f s (%.3f ms)' % (value / 1000.0, value)
    return '%.3f ms' % value


# --------------------------------------------------------------------------
# 读入
# --------------------------------------------------------------------------
_ROTATED_JSONL = re.compile(r'.+\.jsonl(?:\.\d+)?$', re.IGNORECASE)


def _rotation_sort_key(path):
    """Sort rotated logs by their rotation index, not lexicographically.

    ``.jsonl.10`` belongs after ``.jsonl.2``. A plain string sort puts it
    before, and the reported input list then names the files in the wrong
    order.
    """
    name = Path(path).name
    match = re.match(r'^(.*\.jsonl)(?:\.(\d+))?$', name, re.IGNORECASE)
    if not match:
        return (1, 0, name)
    return (0, int(match.group(2) or 0), name)


def catalog_publications_report(sess):
    """``catalog_result_published``: one row per publication, this attempt.

    The counters of one publication are **not** one "total work": they are
    four different quantities and they stay separate. The identity fields
    say which publication it is: a redelivery of the same
    ``publication_id`` is a duplicate, and a publication whose
    ``query_generation``/``publication_seq`` is older than one already
    counted is a **late old callback** that must not overwrite the newer
    count.

    The model marker (``asset_model_published``) describes the same
    publication from the other side. The two are reported side by side and
    **never summed**.
    """
    seen = set()
    duplicates = 0
    rows = []
    for event in sess.catalog_publications:
        identity = event.get('publication_id')
        if identity is not None:
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
        rows.append(event)

    # An older publication arriving later must not overwrite a newer one.
    newest = {}
    kept = []
    superseded = 0
    for event in rows:
        generation = event.get('query_generation')
        seq = event.get('publication_seq')
        if generation is None or seq is None:
            kept.append(event)
            continue
        key = (event.get('query_digest'), generation)
        previous = newest.get(key)
        if previous is not None:
            superseded += 1
            if previous > seq:
                continue
        newest[key] = seq
        kept.append(event)

    for event in kept:
        for name in ('evaluated_rows', 'read_rows', 'proxies_created',
                     'published_rows'):
            event[name + '_status'] = ('known' if event.get(name) is not None
                                       else 'unknown')

    violations = []
    for event in kept:
        for name in ('evaluated_rows_cumulative', 'proxies_created_cumulative',
                     'read_rows_cumulative'):
            if event.get(name):
                violations.append({'seq': event.get('seq'), 'field': name})

    return {
        'publications': kept,
        'publication_count': len(kept),
        'duplicate_publications': duplicates,
        'superseded_publications': superseded,
        'counter_basis': 'this_publication_attempt',
        'counts_are_separate': True,
        'model_marker_event_count': sess.event_counts.get('asset_model_published', 0),
        'double_count_warning': ('asset_model_published 与 catalog_result_published '
                                 '描述同一次发布，**不相加**。'),
        'cumulative_flag_violations': violations,
        'note': ('published_rows / evaluated_rows / proxies_created / read_rows '
                 '分别是本次发布的四个量，**不得相加成一个总工作量**；'
                 'read_rows 是行流取走的行数，不是 SQL 扫描行数，也不是物理读盘行数。'
                 '未知字段保持 unknown，不填 0。'),
    }


def rotation_chain_report(paths):
    """轮转链的完整性：哪一段缺失，以及各段覆盖的 sequence 范围。

    缺段不是「那段时间没有事件」，而是**证据不在**。两者必须分开说。
    """
    groups = collections.OrderedDict()
    for path in paths:
        name = Path(path).name
        match = re.match(r'^(.*\.jsonl)(?:\.(\d+))?$', name, re.IGNORECASE)
        if not match:
            continue
        base = match.group(1)
        index = int(match.group(2)) if match.group(2) is not None else None
        groups.setdefault(base, []).append({'path': path, 'index': index})
    chains = []
    for base, items in groups.items():
        indices = sorted(item['index'] for item in items
                         if item['index'] is not None)
        missing, missing_ranges = [], []
        expected = 1
        for index in sorted(set(indices)):
            if index > expected:
                missing_ranges.append([expected, index-1])
                # A malformed huge numeric suffix must not expand a billion
                # missing rows. Keep exact compact ranges plus a small sample.
                missing.extend(range(expected, min(index, expected+max(0, 64-len(missing)))))
            expected = max(expected, index+1)
        missing_base = not any(item['index'] is None for item in items)
        chains.append({
            'base': base,
            'segments': len(items),
            'indices': indices,
            'missing_indices': missing,
            'missing_index_ranges': missing_ranges,
            'missing_indices_truncated': sum(end-start+1 for start,end in missing_ranges) > len(missing),
            'complete': not missing and not missing_base,
            'missing_base': missing_base,
            'whole_session_complete': 'unknown; contiguous retained parts do not prove earlier parts survived',
            'paths': sorted(item['path'] for item in items),
            'note': ('轮转链缺段时，缺失的段不是「没有事件」，而是**证据不在**：'
                     '不能据此断言该时段没有发生任何事。'),
        })
    return chains


def resolve_inputs(raw_paths):
    """Expand paths while including only base JSONL and numeric rotations."""
    resolved, seen = [], set()
    for raw in raw_paths:
        target = Path(raw)
        if target.is_dir():
            candidates = sorted(
                (str(item) for item in target.iterdir()
                 if item.is_file() and _ROTATED_JSONL.fullmatch(item.name)),
                key=_rotation_sort_key)
        elif any(ch in raw for ch in '*?['):
            candidates = sorted((item for item in glob.glob(raw)
                                 if _ROTATED_JSONL.fullmatch(Path(item).name)),
                                key=_rotation_sort_key)
        else:
            # An explicit single file keeps its exact meaning. Siblings are
            # diagnosed later, never silently pulled into the analysis.
            candidates = [raw]
        for item in candidates:
            if not os.path.isfile(item):
                continue
            key = os.path.normcase(os.path.abspath(item))
            if key in seen:
                continue
            seen.add(key)
            resolved.append(os.path.abspath(item))
    return resolved


def _stat_identity(path):
    value = os.stat(path)
    return dict(device=value.st_dev, inode=value.st_ino, size=value.st_size,
                mtime_ns=value.st_mtime_ns)


def _newline_size(raw):
    if raw.endswith(b'\r\n'):
        return 2
    if raw.endswith((b'\n', b'\r')):
        return 1
    return 0


def read_physical_records(paths):
    """Read binary lines once and retain exact physical byte accounting."""
    records, malformed, files = [], [], []
    totals = collections.Counter()
    for path in paths:
        try:
            before = _stat_identity(path)
            with open(path, 'rb') as stream:
                lines = stream.readlines()
            after = _stat_identity(path)
        except OSError as error:
            malformed.append({'file': path, 'line': 0, 'error': str(error),
                              'note': '文件打不开。'})
            continue
        changed = before != after
        file_info = dict(path=path, before=before, after=after,
                         stable=not changed, physical_bytes=sum(map(len, lines)),
                         physical_lines=len(lines), event_lines=0,
                         event_line_bytes=0, newline_bytes=0,
                         first_sequence=None, last_sequence=None)
        last_nonempty = max((index for index, raw in enumerate(lines, 1)
                             if raw.strip()), default=0)
        for number, raw in enumerate(lines, 1):
            totals['physical_lines'] += 1
            totals['physical_bytes'] += len(raw)
            newline_bytes = _newline_size(raw)
            totals['newline_bytes'] += newline_bytes
            file_info['newline_bytes'] += newline_bytes
            payload = raw[:-newline_bytes] if newline_bytes else raw
            if not payload.strip():
                continue
            totals['event_lines'] += 1
            totals['event_line_bytes'] += len(payload)
            file_info['event_lines'] += 1
            file_info['event_line_bytes'] += len(payload)
            try:
                text = payload.decode('utf-8')
                entry = json.loads(text.strip())
            except (UnicodeDecodeError, ValueError) as error:
                item = {'file': path, 'line': number, 'bytes': len(payload),
                        'error': 'JSON 解析失败或 UTF-8 无效: %s' % error}
                if number == last_nonempty:
                    item['truncated_last_line'] = True
                malformed.append(item)
                continue
            if not isinstance(entry, dict):
                malformed.append({'file': path, 'line': number,
                                  'bytes': len(payload),
                                  'error': 'JSON 值不是对象'})
                continue
            canonical = json.dumps(entry, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode('utf-8')
            sequence = entry.get('sequence')
            if isinstance(sequence, int) and not isinstance(sequence, bool):
                values = [value for value in
                          (file_info['first_sequence'], file_info['last_sequence'])
                          if value is not None]
                file_info['first_sequence'] = min(values + [sequence])
                file_info['last_sequence'] = max(values + [sequence])
            records.append(dict(path=path, line=number, entry=entry,
                                event_line_bytes=len(payload),
                                newline_bytes=newline_bytes,
                                canonical=canonical))
        files.append(file_info)
    totals['file_bytes'] = totals['physical_bytes']
    totals['stat_size_after_bytes'] = sum(item['after']['size'] for item in files)
    return records, malformed, files, dict(totals)


def iter_entries(paths):
    """Compatibility iterator over the exact binary reader."""
    records, malformed, _files, _totals = read_physical_records(paths)
    by_location = {(item['path'], item['line']): item for item in malformed}
    records_by_location = {(item['path'], item['line']): item for item in records}
    for path in paths:
        locations = sorted((key for key in set(by_location) | set(records_by_location)
                            if key[0] == path), key=lambda key: key[1])
        if not locations and any(item['file'] == path and item['line'] == 0
                                 for item in malformed):
            item = next(item for item in malformed
                        if item['file'] == path and item['line'] == 0)
            yield path, 0, None, item['error']
        for key in locations:
            if key in records_by_location:
                yield path, key[1], records_by_location[key]['entry'], None
            else:
                item = by_location[key]
                yield path, key[1], None, item.get('bytes', item['error'])


# --------------------------------------------------------------------------
# 会话累积
# --------------------------------------------------------------------------
class Session(object):
    """单个 session 的全部分析状态。session 之间绝不互相污染。"""

    def __init__(self, session_id):
        self.session_id = session_id
        self.event_counts = collections.Counter()
        self.application_errors = {}
        self.diagnostic_archive_summaries = []
        self.maintenance = collections.deque(maxlen=64)
        self.application_error_overflow = 0
        self.spans = []
        self.milestones = []
        self.anomalies = collections.Counter()
        self.anomaly_samples = []
        self.schema_seen = collections.Counter()
        self.schema_values = {}
        self.schema_versions_seen = collections.Counter()
        self.total_events = 0
        self.first_time = None
        self.last_time = None
        self.first_mono = None
        self.last_mono = None
        self.dropped_events = 0
        self.dropped_seqs = []
        self.trace_incomplete = False
        self.seq_gap_count = 0
        self.dropped_by_kind = {}
        self.seq_gap_basis = None
        self.sequence_order_guaranteed = None
        self.has_session_start = False
        self.has_session_end = False
        self.pid = None
        self.runtime = None
        self.files = []
        self.samples = []
        self.sampler_names = []
        self.resource_peaks = {}
        self.count_layers = []
        self.paged_progress = []
        self._paged_progress_seen = set()
        self.queue_tasks = {}
        self.image_decode = []
        self.diagnostic_observations = []
        self.catalog = {}
        self.scope_events = []
        self.unmatched_starts = []
        self.time_base = 'monotonic'
        self.v2_events = 0
        # schema v2 新增事件族（原始数值 + 证据，不做任何加工）
        self.queue_rollups = []
        self.resource_rollups = []
        self.presentation_markers = []
        self.paint_frames = []
        self.presentation_evidence = []
        self.input_evidence_gaps = []
        self.lifecycle_evidence = []
        self.operation_sequences = set()
        self.observed_sequences = set()
        # 会话最后一条记录：用于「查询仍在飞行时会话就结束」的日志自证。
        self.last_record_sequence = None
        self.last_record_event = None
        self.operation_time_range = [None, None]
        self.dropped_events_observed = False
        self.retention_evidence = []
        self.stall_contexts = []
        self.stall_dispatch_batches = []
        self.span_by_id = {}
        # 采样策略与生命周期摘要（新增事件族）
        self.hotpath_rollups = []
        self.hotpath_samples = []
        self.manifests = []
        self.version_snapshots = []
        self.catalog_publications = []
        self.storage_workflow_events = collections.deque(maxlen=4096)
        self.storage_workflow_truncated = 0

    # -- 记录 ------------------------------------------------------------
    def note_schema(self, entry):
        for field in SCHEMA_V2_FIELDS:
            if field in entry:
                self.schema_seen[field] += 1
                if field not in self.schema_values:
                    self.schema_values[field] = entry.get(field)
        if _num(entry.get('schema_version')):
            self.v2_events += 1
            self.schema_versions_seen[_num(entry.get('schema_version'))] += 1

    def add_span(self, entry, path, line):
        duration = _num(entry.get('duration_ms'))
        if duration is None:
            duration = _num(entry.get('blocked_ms'))
        if duration is None:
            duration = _num(entry.get('latency_ms'))
        end_mono = _num(entry.get('monotonic'))
        start_mono = _num(entry.get('start_mono'))
        if entry.get('event') == 'gc':
            gc_start = _num(entry.get('gc_start_monotonic'))
            gc_end = _num(entry.get('gc_end_monotonic'))
            if gc_start is not None and gc_end is not None:
                if (math.isfinite(gc_start) and math.isfinite(gc_end) and gc_end >= gc_start
                        and duration is not None and abs((gc_end-gc_start)*1000-duration) <= 1):
                    start_mono, end_mono = gc_start, gc_end
                else:
                    self.note_anomaly('invalid_gc_interval', entry)
        if end_mono is None:
            end_mono = _num(entry.get('time'))
        lifecycle_start = (entry.get('role') == 'start'
                           or entry.get('status') == 'start'
                           or entry.get('phase') == 'start'
                           or SPAN_EVENTS.get(entry.get('event')) == 'start')
        if start_mono is None and lifecycle_start:
            # A start record's own monotonic timestamp is the interval start.
            # Keeping it only as end_mono would make strict reverse-time checks
            # and the linked interval silently fall back to a derived value.
            start_mono = end_mono
        elif start_mono is None and end_mono is not None and duration is not None:
            # 旧日志只有结束时间戳；开始时间 = 结束 - 耗时（已用真实
            # operation_start/operation 对验证过，误差 < 0.02 ms）。
            start_mono = end_mono - duration / 1000.0
        self.spans.append({
            'seq': entry.get('sequence'),
            'event': entry.get('event'),
            'name': entry.get('name') or entry.get('query') or entry.get('event'),
            'thread': (entry.get('thread_name') or entry.get('thread')
                       or entry.get('blocked_thread') or 'unknown'),
            'thread_id': entry.get('thread_id'),
            'dur_ms': duration,
            'start_mono': start_mono,
            'end_mono': end_mono,
            'status': (entry.get('status') or entry.get('phase')
                       or SPAN_EVENTS.get(entry.get('event')) or 'unknown'),
            'role': entry.get('role'),
            'observation': entry.get('observation'),
            'sampling_strategy': entry.get('sampling_strategy'),
            'cross_thread_handoff': entry.get('cross_thread_handoff'),
            'handoff_id': entry.get('handoff_id'),
            'dispatch_id': entry.get('dispatch_id'),
            'parent_dispatch_id': entry.get('parent_dispatch_id'),
            'root_dispatch_id': entry.get('root_dispatch_id'),
            'depth': entry.get('depth'),
            'dispatch_start_monotonic': _num(entry.get('dispatch_start_monotonic')),
            'dispatch_end_monotonic': _num(entry.get('dispatch_end_monotonic')),
            'rows': entry.get('rows'),
            'failed': entry.get('failed'),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'project_token': entry.get('project_token'),
            'open_epoch': entry.get('open_epoch'),
            'generation': entry.get('generation'),
            'source_revision': entry.get('source_revision'),
            'schema_version': entry.get('schema_version'),
            'stage': entry.get('stage'),
            'duration_source': ('duration_ms' if _num(entry.get('duration_ms')) is not None
                                else ('blocked_ms' if _num(entry.get('blocked_ms')) is not None
                                      else ('latency_ms' if _num(entry.get('latency_ms')) is not None
                                            else 'none'))),
            'stall_phase': entry.get('phase', '(legacy/no-phase)')
                           if entry.get('event') == 'gui_stall' else None,
            'stall_id': entry.get('stall_id'),
            'classification': entry.get('classification'),
            'classification_reason': entry.get('classification_reason'),
            'impact_level': entry.get('impact_level'),
            'impact_policy_version': entry.get('impact_policy_version'),
            'stack_histogram': (entry.get('stack_histogram') or []
                                if entry.get('event') == 'gui_stall' else []),
            'severity': entry.get('severity'),
            'samples': entry.get('samples'),
            'truncated': entry.get('truncated'),
            'evidence': {'file': path, 'line': line,
                         'sequence': entry.get('sequence')},
        })

    def note_anomaly(self, kind, entry=None, detail=''):
        self.anomalies[kind] += 1
        if len(self.anomaly_samples) < 25:
            evidence = (entry or {}).get('evidence') or {}
            self.anomaly_samples.append({
                'kind': kind, 'detail': detail,
                'sequence': (entry or {}).get('sequence', (entry or {}).get('seq')),
                'event': (entry or {}).get('event'),
                'file': (entry or {}).get('_file') or evidence.get('file'),
                'line': (entry or {}).get('_line') or evidence.get('line'),
            })


# --------------------------------------------------------------------------
# 主分析
# --------------------------------------------------------------------------
def _deduplicate_records(records, session_filter=None):
    """Deduplicate after the complete read, independent of input enumeration."""
    keyed = collections.defaultdict(list)
    sequence_less = []
    for record in records:
        entry = record['entry']
        sid = entry.get('session') or 'unknown-session'
        if session_filter and sid != session_filter:
            continue
        sequence = entry.get('sequence')
        if sequence is None:
            sequence_less.append(record)
        else:
            keyed[(sid, sequence)].append(record)

    selected = []
    identical_duplicates = 0
    conflicts = []
    for key in sorted(keyed, key=lambda value: (str(value[0]), str(value[1]))):
        group = keyed[key]
        variants = collections.defaultdict(list)
        for record in group:
            variants[record['canonical']].append(record)
        identical_duplicates += sum(max(0, len(values)-1)
                                    for values in variants.values())
        if len(variants) > 1:
            conflicts.append({
                'session': key[0], 'sequence': key[1],
                'variant_count': len(variants), 'record_count': len(group),
                'variants': [
                    {'event': values[0]['entry'].get('event'),
                     'locations': [{'file': item['path'], 'line': item['line']}
                                   for item in sorted(values, key=lambda item: (
                                       os.path.normcase(item['path']), item['line']))]}
                    for _canonical, values in sorted(variants.items(),
                                                     key=lambda item: item[0])],
            })
        # A conflict cannot be silently resolved by input order. Select the
        # lexicographically smallest canonical record solely to keep downstream
        # reporting total and deterministic, while reporting the conflict above.
        chosen = min(group, key=lambda item: (
            item['canonical'], os.path.normcase(item['path']), item['line']))
        selected.append(chosen)

    selected.extend(sequence_less)
    selected.sort(key=lambda item: (
        str(item['entry'].get('session') or 'unknown-session'),
        item['entry'].get('sequence') is None,
        item['entry'].get('sequence') if isinstance(
            item['entry'].get('sequence'), (int, float)) else 0,
        _num(item['entry'].get('monotonic'), float('inf')),
        item['canonical'], os.path.normcase(item['path']), item['line']))
    return selected, {
        'identical_duplicates': identical_duplicates,
        'conflict_keys': len(conflicts),
        'conflicting_records': sum(item['record_count'] for item in conflicts),
        'conflicts': conflicts,
        'sequence_less_records': len(sequence_less),
        'sequence_less_dedup_reliable': False,
    }


def _single_input_rotation_warnings(paths):
    """显式选中的文件如果还有同族轮转段**没有**被选中，要说出来。

    之前只在「恰好选中一个文件」时才检查：显式选中 base + .1 时，
    存在的 .2 段不会被报告，缺口就静默过去了。
    """
    selected_set = {os.path.normcase(os.path.abspath(path)) for path in paths}
    warnings = []
    for raw in paths:
        selected = Path(raw)
        match = re.match(r'^(.*\.jsonl)(?:\.\d+)?$', selected.name, re.IGNORECASE)
        if not match:
            continue
        base = match.group(1)
        siblings = []
        for item in selected.parent.iterdir():
            if not item.is_file():
                continue
            if not re.fullmatch(re.escape(base) + r'(?:\.\d+)?', item.name,
                                re.IGNORECASE):
                continue
            if os.path.normcase(os.path.abspath(str(item))) in selected_set:
                continue
            siblings.append(str(item.resolve()))
        if not siblings:
            continue
        warnings.append({
            'kind': 'single_input_has_rotation_siblings', 'file': str(selected),
            'siblings': sorted(siblings),
            'note': ('显式单文件保持单文件语义；发现同族轮转段，但未自动扩展。'
                     '未选中的段属于**证据缺口**，不能当作该时段没有事件。'),
        })
    return warnings


def _byte_accounting(records, unique_records, totals):
    def summarize(source):
        by_kind = collections.Counter()
        purpose = collections.Counter()
        sampling = collections.Counter()
        diagnostic_mode = collections.Counter()
        retention_priority = collections.Counter()
        for record in source:
            entry = record['entry']
            event = entry.get('event') or '(missing)'
            size = record['event_line_bytes']
            by_kind[event] += size
            if event in STALL_BYTE_EVENTS:
                purpose['stall_diagnosis'] += size
            elif event in LIFECYCLE_BYTE_EVENTS:
                purpose['lifecycle_observability'] += size
            elif event in PERIODIC_BYTE_EVENTS:
                purpose['runtime_baseline'] += size
            else:
                purpose['other_or_unclassified'] += size
            sampling['periodic_sample' if event in PERIODIC_BYTE_EVENTS
                     else 'event_driven'] += size
            diagnostic_mode[str(entry.get('trace_mode') or entry.get('mode')
                                or '(unspecified)')] += size
            retention_priority[str(entry.get('priority')
                                   or '(unspecified)')] += size
        return {
            'event_bytes_by_kind': dict(sorted(by_kind.items())),
            'event_bytes_by_class': _legacy_byte_classes(by_kind),
            'dimensions': {
                'diagnostic_purpose': dict(sorted(purpose.items())),
                'sampling_semantics': dict(sorted(sampling.items())),
                'diagnostic_mode': dict(sorted(diagnostic_mode.items())),
                'retention_priority': dict(sorted(retention_priority.items())),
            },
        }

    physical_summary = summarize(records)
    unique_summary = summarize(unique_records)
    return {
        'classification_version': BYTE_CLASSIFICATION_VERSION,
        'classification_members': dict(
            (name, sorted(members)) for name, members in BYTE_EVENT_CLASSES.items()),
        'classification_dimensions': (
            'diagnostic_purpose, sampling_semantics, diagnostic_mode and '
            'retention_priority are independent; no byte class is a value judgement'),
        'newline_policy': ('event_line_bytes excludes the exact trailing CRLF/LF/CR; '
                           'physical_bytes includes it'),
        'physical': dict(totals, **physical_summary),
        'unique_after_dedup': dict(
            events=len(unique_records),
            event_line_bytes=sum(item['event_line_bytes'] for item in unique_records),
            **unique_summary),
        'value_judgement': None,
    }


def _legacy_byte_classes(counter):
    """Keep the v1 grouped table for report compatibility, without calling it value."""
    result = collections.Counter()
    for event, size in counter.items():
        category = 'other'
        for name, members in BYTE_EVENT_CLASSES.items():
            if event in members:
                category = name
                break
        result[category] += size
    return dict(sorted(result.items()))


def _is_startup_archive(path):
    return re.fullmatch(r'Prism-startup-\d+\.jsonl', Path(path).name,
                        re.IGNORECASE) is not None


def with_startup_archives(paths):
    """Add only the matching PID identity archive, not unselected rotations.

    The archive contains sparse session/retention evidence, not operation
    history. Session identities are checked after reading, including PID reuse.
    """
    result = list(paths)
    seen = {os.path.normcase(os.path.abspath(path)) for path in result}
    for path in paths:
        match = re.fullmatch(r'Prism-operations-(\d+)\.jsonl(?:\.\d+)?',
                             Path(path).name, re.IGNORECASE)
        if not match:
            continue
        archive = Path(path).parent / ('Prism-startup-%s.jsonl' % match.group(1))
        key = os.path.normcase(os.path.abspath(str(archive)))
        if archive.is_file() and key not in seen:
            result.append(str(archive.resolve()))
            seen.add(key)
    return result


def matching_crash_reports(paths, session):
    """Read PID siblings only when their recorded session matches exactly.

    Sidecars are separate evidence, never synthetic trace events or extra
    independently counted failures. Old recordings did not emit crash events.
    """
    result, seen = [], set()
    for path in paths:
        match = re.fullmatch(r'Prism-(?:operations|startup)-(\d+)\.jsonl(?:\.\d+)?',
                             Path(path).name, re.IGNORECASE)
        if not match:
            continue
        sibling = Path(path).parent / ('Prism-crash-%s.json' % match.group(1))
        key = os.path.normcase(os.path.abspath(sibling))
        if key in seen:
            continue
        seen.add(key)
        try:
            with sibling.open('rb') as stream:
                data = stream.read(2*1024*1024 + 1)
            if len(data) > 2*1024*1024:
                continue
            crash = json.loads(data)
        except (OSError, ValueError):
            continue
        if (not isinstance(crash, dict) or crash.get('session') != session
                or not isinstance(crash.get('error'), str)):
            continue
        result.append(dict(file=str(sibling), session=session,
                           time=crash.get('time'), error=crash['error'][:2000],
                           evidence_kind='matching_session_crash_sidecar'))
    return result


def analyse(paths, session_filter=None):
    paths = with_startup_archives(paths)
    sessions = collections.OrderedDict()
    records, malformed, file_reads, physical_totals = read_physical_records(paths)
    operation_sessions = {
        item['entry'].get('session') or 'unknown-session'
        for item in records
        if re.fullmatch(r'Prism-operations-\d+\.jsonl(?:\.\d+)?',
                        Path(item['path']).name, re.IGNORECASE)}
    analysis_records = [item for item in records
                        if not _is_startup_archive(item['path'])
                        or not operation_sessions
                        or (item['entry'].get('session') or 'unknown-session')
                        in operation_sessions]
    unique_records, deduplication = _deduplicate_records(analysis_records, session_filter)
    truncated_last_line = [
        {'file': item['file'], 'line': item['line'],
         'note': '最后一行解析失败；文件尾部证据不完整，不能当作正常结束。'}
        for item in malformed if item.get('truncated_last_line')]

    for record in unique_records:
        path, number, entry = record['path'], record['line'], record['entry']
        sid = entry.get('session') or 'unknown-session'
        sess = sessions.get(sid)
        if sess is None:
            sess = sessions[sid] = Session(sid)
        if path not in sess.files:
            sess.files.append(path)

        seq = entry.get('sequence')
        kind = entry.get('event', '')
        # All statistics start here, after deterministic deduplication.
        sess.total_events += 1
        sess.event_counts[kind] += 1
        if kind in ('trace_log_cleanup', 'capture.inbox.cleanup', 'capture.inbox.cleanup_failed'):
            sess.maintenance.append(dict(entry=entry, evidence=dict(file=path, line=number, sequence=seq)))
        if kind == 'trace_diagnostic_archive_summary':
            sess.diagnostic_archive_summaries.append(dict(entry=entry,
                evidence=dict(file=path, line=number, sequence=seq)))
        error = entry.get('error') or entry.get('error_message')
        if isinstance(error, str) and error.strip():
            key = (str(kind), error[:500])
            group = sess.application_errors.get(key)
            if group is None and len(sess.application_errors) < 256:
                group = sess.application_errors[key] = dict(event=key[0], error=key[1],
                    count=0, first_sequence=seq, last_sequence=seq,
                    evidence=dict(file=path, line=number, sequence=seq))
            if group is not None:
                group['count'] += 1
                group['last_sequence'] = seq
            else:
                sess.application_error_overflow += 1
        sess.note_schema(entry)

        stamp = _num(entry.get('time'))
        mono = _num(entry.get('monotonic'))
        if stamp is not None:
            sess.first_time = stamp if sess.first_time is None else min(sess.first_time, stamp)
            sess.last_time = stamp if sess.last_time is None else max(sess.last_time, stamp)
        if mono is not None:
            sess.first_mono = mono if sess.first_mono is None else min(sess.first_mono, mono)
            sess.last_mono = mono if sess.last_mono is None else max(sess.last_mono, mono)

        if isinstance(seq, int) and not isinstance(seq, bool):
            sess.observed_sequences.add(seq)
            if sess.last_record_sequence is None or seq > sess.last_record_sequence:
                sess.last_record_sequence = seq
                sess.last_record_event = kind
        if not _is_startup_archive(path):
            if isinstance(seq, int) and not isinstance(seq, bool):
                sess.operation_sequences.add(seq)
            if stamp is not None:
                low, high = sess.operation_time_range
                sess.operation_time_range = [stamp if low is None else min(low, stamp),
                                             stamp if high is None else max(high, stamp)]
        if sess.pid is None and entry.get('pid') is not None:
            sess.pid = entry['pid']
        if kind in ('session_start', 'session_end', 'trace_shutdown_pending',
                    'trace_writer_failed', 'trace_writer_drained', 'trace_writer_checkpoint'):
            sess.lifecycle_evidence.append(dict(entry=entry,
                evidence={'file': path, 'line': number, 'sequence': seq}))
        if kind == 'trace_rotation':
            sess.retention_evidence.append(dict(entry=entry,
                evidence={'file': path, 'line': number, 'sequence': seq}))
        dropped = _num(entry.get('dropped_events'))
        sess.dropped_events_observed |= dropped is not None
        if dropped:
            sess.dropped_events = max(sess.dropped_events, int(dropped))
            if len(sess.dropped_seqs) < 25:
                sess.dropped_seqs.append({'sequence': seq, 'dropped_events': int(dropped)})
        # Schema 2 explicitly reports missing observations even when the older
        # dropped_events field is absent. These are cumulative snapshots, not
        # quantities to sum across repeated rollups.
        sess.trace_incomplete = sess.trace_incomplete or entry.get('trace_incomplete') is True
        gap_count = _count(entry.get('seq_gap_count'))
        if gap_count is not None:
            sess.seq_gap_count = max(sess.seq_gap_count, gap_count)
        if entry.get('seq_gap_basis') is not None:
            sess.seq_gap_basis = entry['seq_gap_basis']
        if isinstance(entry.get('sequence_order_guaranteed'), bool):
            sess.sequence_order_guaranteed = entry['sequence_order_guaranteed']
        by_kind = entry.get('dropped_by_kind')
        if isinstance(by_kind, dict):
            for dropped_kind, value in by_kind.items():
                count = _count(value)
                if count is not None and count >= 0:
                    sess.dropped_by_kind[dropped_kind] = max(
                        sess.dropped_by_kind.get(dropped_kind, 0), count)

        if kind == 'session_start':
            sess.has_session_start = True
            sess.pid = entry.get('pid')
        elif kind == 'session_end':
            sess.has_session_end = True
            if entry.get('pid'):
                sess.pid = entry.get('pid')
        elif kind == 'runtime':
            sess.runtime = {k: entry.get(k) for k in
                            ('python', 'platform', 'qt', 'qt_platform', 'webengine_flags',
                             'gc_min_duration_ms', 'stack_capture_scope',
                             'native_stack_capture_available', 'process_io_scope')
                            if k in entry}

        if kind in SPAN_EVENTS or kind in ('gui_stall', 'open_phase'):
            sess.add_span(entry, path, number)

        if kind in MILESTONE_EVENTS:
            sess.milestones.append({
                'seq': seq, 'event': kind,
                'duration_ms': _num(entry.get('duration_ms')) or _num(entry.get('blocked_ms')),
                'time': stamp, 'monotonic': mono,
                'phase': entry.get('phase'),
                'failed': entry.get('failed'),
                'filename': entry.get('filename'),
                'evidence': {'file': path, 'line': line_of(path, number)},
            })

        sess.sample_event(kind, entry, path, number)
        sess.v2_event(kind, entry, path, number)

    # Surface sequence conflicts inside their affected sessions as anomalies.
    for conflict in deduplication['conflicts']:
        sess = sessions.get(conflict['session'])
        if sess is not None:
            sess.note_anomaly('sequence_content_conflict',
                              {'sequence': conflict['sequence']},
                              '%d 个不同内容共享同一 sequence' %
                              conflict['variant_count'])

    # Input damage is not a transport loss counter. Attribute unreadable lines
    # conservatively to every session observed in that file; an unreadable file
    # with no usable identity can affect any session in this input selection.
    input_gaps = []
    file_sessions = collections.defaultdict(set)
    sequences = collections.defaultdict(set)
    for record in records:
        sid = record['entry'].get('session') or 'unknown-session'
        file_sessions[record['path']].add(sid)
    for record in unique_records:
        sid = record['entry'].get('session') or 'unknown-session'
        sequence = record['entry'].get('sequence')
        if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0:
            sequences[sid].add(sequence)
        elif record['entry'].get('event') in PRESENTATION_EVIDENCE_EVENTS:
            input_gaps.append(dict(kind='sequence_order_unknown', sessions=[sid],
                                   file=record['path'], line=record['line']))
    for item in malformed:
        affected = file_sessions.get(item['file']) or set(sessions)
        input_gaps.append(dict(kind='io_or_parse_error',
                               sessions=sorted(affected, key=str), detail=item))
    for item in file_reads:
        if not item['stable']:
            input_gaps.append(dict(kind='input_changed_during_read', file=item['path'],
                                   sessions=sorted(file_sessions[item['path']], key=str)))
    for conflict in deduplication['conflicts']:
        input_gaps.append(dict(kind='sequence_content_conflict',
                               sessions=[conflict['session']], detail=conflict))
    for sid, values in sequences.items():
        ordered = sorted(values)
        ranges = [[left + 1, right - 1] for left, right in zip(ordered, ordered[1:])
                  if right > left + 1]
        if ranges:
            input_gaps.append(dict(kind='sequence_coverage_gap', sessions=[sid],
                                   missing_sequence_ranges=ranges,
                                   basis='sorted_deduplicated_observed_sequence_coverage'))
    for gap in input_gaps:
        for sid in gap['sessions']:
            if sid in sessions:
                sess = sessions[sid]
                if gap['kind'] == 'sequence_coverage_gap':
                    pruned = retention_report(sess)['verified_missing_sequence_ranges']
                    proven = []
                    for left, right in gap['missing_sequence_ranges']:
                        for low, high in pruned:
                            if max(left, low) <= min(right, high):
                                proven.append([max(left, low), min(right, high)])
                    gap['verified_retention_missing_ranges'] = proven
                    gap['cause'] = ('retention_explains_all' if
                        sum(right-left+1 for left, right in proven) ==
                        sum(right-left+1 for left, right in gap['missing_sequence_ranges'])
                        else 'partly_retention_other_causes_unconfirmed' if proven
                        else 'unconfirmed_not_transport_loss_count')
                sess.input_evidence_gaps.append(gap)

    results = []
    for sid, sess in sessions.items():
        # Priority lanes can write terminal events before older normal events.
        # A lower sequence in the file is not evidence of loss. Facts with an
        # explicit sequence follow emission order, never physical line order.
        for facts in (sess.spans, sess.queue_rollups, sess.resource_rollups,
                      sess.presentation_markers, sess.paint_frames,
                      sess.stall_contexts, sess.milestones):
            facts.sort(key=lambda fact: (not isinstance(fact.get('seq'), int),
                                         fact.get('seq') if isinstance(fact.get('seq'), int) else 0))
        link_spans(sess)
        report = build_report(sess, truncated_last_line)
        report['crash_reports'] = matching_crash_reports(paths, sid)
        results.append(report)
    byte_accounting = _byte_accounting(records, unique_records, physical_totals)
    unstable = [item for item in file_reads if not item['stable']]
    warnings = _single_input_rotation_warnings(paths)
    if unstable:
        warnings.append({
            'kind': 'input_changed_during_read',
            'files': [item['path'] for item in unstable],
            'note': '读取前后文件身份/大小/mtime 不一致；证据标为不稳定，未补造。',
        })
    return results, dict(
        malformed=malformed,
        duplicate_events=deduplication['identical_duplicates'],
        identical_duplicates=deduplication['identical_duplicates'],
        conflict_keys=deduplication['conflict_keys'],
        conflicting_records=deduplication['conflicting_records'],
        conflicts=deduplication['conflicts'],
        sequence_less_records=deduplication['sequence_less_records'],
        sequence_less_dedup_reliable=False,
        files=paths, file_reads=file_reads, unstable_files=unstable,
        startup_archives=[path for path in paths if _is_startup_archive(path)],
        excluded_other_session_archive_records=len(records)-len(analysis_records),
        rotation_chains=rotation_chain_report(paths),
        input_evidence_gap=bool(input_gaps), input_evidence_gaps=input_gaps,
        warnings=warnings + ([{'kind': 'input_evidence_gap',
                               'note': '输入证据有缺口；不能推断队列丢失数量。'}]
                             if input_gaps else []),
        total_lines=physical_totals.get('event_lines', 0),
        physical_records=len(records), unique_events=len(unique_records),
        byte_accounting=byte_accounting)


def line_of(path, number):
    return number


def link_spans(sess):
    """把旧日志里的 ``operation_start`` 与 ``operation`` 配对，得到真实区间。

    这一步**不是伪造父子链**：配对依据是「同名 + 同线程 + start 的 seq 在 end 之前
    且中间没有同名同线程的其它未闭合 start」。旧日志没有 ``span_id``，所以配对结果
    一律标 ``link_confidence='legacy_inferred'``；有 ``trace_id`` 的事件直接采信，
    标 ``'trace_id'``。没有证据的关联一律不连。

    配对成功还能把区间起点从「结束时间 − 耗时」换成**真实的 start 时间戳**，
    区间更准，关键路径与并集统计更可信。
    """
    def lifecycle_role(span):
        role = span.get('role')
        if role in ('start', 'end', 'attachment'):
            return role
        if span.get('status') == 'start':
            return 'start'
        if (span.get('span_id') and span.get('event')
                not in ('operation', 'operation_start', 'open_phase')):
            # Older schema-2 SQL/dispatch diagnostics inherited their enclosing
            # span identity but did not declare an attachment role.
            span['role'] = 'attachment'
            return 'attachment'
        if span.get('dur_ms') is not None:
            return 'end'
        return None

    by_name_thread = collections.defaultdict(lambda: {'starts': [], 'ends': []})
    for span in sess.spans:
        role = lifecycle_role(span)
        if span.get('span_id') or role == 'attachment':
            continue
        cell = by_name_thread[(span['name'], span['thread'])]
        if role == 'start':
            cell['starts'].append(span)
        elif role == 'end':
            cell['ends'].append(span)

    paired, unpaired = {}, []
    prepaired_starts, prepaired_ends = set(), set()
    identified_starts = {id(span) for span in sess.spans
                         if span.get('span_id') and lifecycle_role(span) == 'start'}
    identified_ends = {id(span) for span in sess.spans
                       if span.get('span_id') and lifecycle_role(span) == 'end'}
    by_identity = collections.defaultdict(list)
    for span in sess.spans:
        if span.get('span_id') and span.get('trace_id'):
            by_identity[(span['trace_id'], span['span_id'])].append(span)

    for identity, group in by_identity.items():
        starts = [span for span in group if lifecycle_role(span) == 'start']
        ends = [span for span in group if lifecycle_role(span) == 'end']
        attachments = [span for span in group
                       if lifecycle_role(span) == 'attachment']
        if len(starts) != 1 or len(ends) != 1:
            # A missing half is incomplete evidence, not identity reuse.
            if len(starts) > 1 or len(ends) > 1:
                sess.note_anomaly(
                    'span_identity_conflict',
                    starts[0] if starts else ends[0],
                    'trace/span %s/%s 有 %d start、%d end、%d attachment' %
                    (identity[0], identity[1], len(starts), len(ends),
                     len(attachments)))
            continue
        start, end = starts[0], ends[0]
        reasons = []
        if start.get('name') != end.get('name'):
            reasons.append('operation_name_mismatch')
        if start.get('stage') != end.get('stage'):
            reasons.append('operation_stage_mismatch')
        if (isinstance(start.get('seq'), int) and isinstance(end.get('seq'), int)
                and end['seq'] <= start['seq']):
            reasons.append('reverse_sequence')
        if (start.get('start_mono') is not None and end.get('end_mono') is not None
                and end['end_mono'] < start['start_mono']):
            reasons.append('reverse_monotonic_time')
        if start.get('thread_id') is not None and end.get('thread_id') is not None:
            same_thread = start['thread_id'] == end['thread_id']
        else:
            same_thread = start.get('thread') == end.get('thread')
        explicit_handoff = (
            bool(end.get('cross_thread_handoff'))
            or (start.get('handoff_id') is not None
                and start.get('handoff_id') == end.get('handoff_id')))
        if not same_thread and not explicit_handoff:
            reasons.append('thread_mismatch_without_handoff')
        if reasons:
            sess.note_anomaly('span_pair_rejected', start, ','.join(reasons))
            continue
        prepaired_starts.add(id(start))
        prepaired_ends.add(id(end))
        paired[id(start)] = end
        end['start_seq'] = start['seq']
        end['linked_start_mono'] = start['start_mono']
        end['link_basis'] = 'session_trace_span_role'
        end['link_confidence'] = 'strict'
        start['link_basis'] = 'session_trace_span_role'
        start['link_confidence'] = 'strict'

    # A span_id without trace_id is not a complete schema-v2 identity and must
    # never fall back to name-based pairing.
    for span in sess.spans:
        if span.get('span_id') and not span.get('trace_id'):
            sess.note_anomaly('span_identity_missing_trace_id', span,
                              'span_id 存在但 trace_id 缺失，未配对')

    for cell in by_name_thread.values():
        starts = sorted(cell['starts'], key=lambda s: (s['seq'] is None, s['seq']))
        ends = sorted(cell['ends'], key=lambda s: (s['seq'] is None, s['seq']))
        used = set()
        for start in starts:
            if id(start) in prepaired_starts:
                continue
            if start['seq'] is None:
                unpaired.append(start)
                continue
            for end in ends:
                if id(end) in used or id(end) in prepaired_ends:
                    continue
                if end['seq'] is None or end['seq'] <= start['seq']:
                    continue
                # 中间不能还有同名同线程的未闭合 start，否则配对不可信。
                clash = [s for s in starts
                         if s['seq'] is not None and start['seq'] < s['seq'] < end['seq']
                         and id(s) not in paired]
                if clash:
                    continue
                used.add(id(end))
                paired[id(start)] = end
                end['start_seq'] = start['seq']
                end['linked_start_mono'] = start['start_mono']
                basis = 'trace_id' if end.get('trace_id') else 'legacy_inferred'
                end['link_basis'] = basis
                end['link_confidence'] = (
                    'trace_id' if end.get('trace_id') else 'legacy_inferred')
                start['link_basis'] = basis
                start['link_confidence'] = (
                    'trace_id' if end.get('trace_id') else 'legacy_inferred')
                break
            else:
                unpaired.append(start)
        for end in ends:
            if id(end) in prepaired_ends:
                continue
            if id(end) not in used and end.get('start_seq') is None:
                end['unterminated'] = True
    unpaired.extend(span for span in sess.spans
                    if id(span) in identified_starts
                    and id(span) not in prepaired_starts)
    for span in sess.spans:
        if id(span) in identified_ends and id(span) not in prepaired_ends:
            span['unterminated'] = True
    sess.unmatched_starts = unpaired
    sess.start_pairs = paired

    # 用真实 start 时间戳覆盖派生的 start_mono（更准）。
    for span in sess.spans:
        if span.get('linked_start_mono') is not None:
            span['start_mono_derived'] = span['start_mono']
            span['start_mono'] = span['linked_start_mono']

    # 包含关系 → 父子。仍然是「推断」，但证据是真实 start/end 时间戳。
    timed = [s for s in sess.spans
             if s.get('start_seq') is not None and s['dur_ms'] is not None
             and s['start_mono'] is not None and s['end_mono'] is not None
             and not s.get('trace_id') and not s.get('span_id')]
    for outer in timed:
        for inner in timed:
            if outer is inner:
                continue
            if (outer['start_mono'] <= inner['start_mono']
                    and outer['end_mono'] >= inner['end_mono']
                    and outer['dur_ms'] > inner['dur_ms']):
                inner['inferred_parent_seq'] = outer['seq']
                inner['inferred_parent_basis'] = 'start/end 区间包含（legacy 推断）'
                outer.setdefault('inferred_children', []).append(inner['seq'])
                break
    resolve_parent_chain(sess)
    return paired, unpaired


def resolve_parent_chain(sess):
    """用日志自己声明的 ``span_id`` / ``parent_span_id`` 还原真实父子链。

    * 有 ``span_id`` 的：父子关系**直接采信**日志，标 ``parent_span_id``。
    * 没有 ``span_id`` 的旧日志：一律 ``None``，**绝不**编造父子链。
    """
    by_identity = {}
    for span in sess.spans:
        span_id, trace_id = span.get('span_id'), span.get('trace_id')
        if (not span_id or not trace_id
                or span.get('link_confidence') != 'strict'):
            continue
        key = (trace_id, span_id)
        # A conflicted/reversed/orphan identity is not a real parent. Only the
        # terminal half of a lifecycle already accepted by strict pairing can
        # satisfy another span's parent reference.
        current = by_identity.get(key)
        if current is None:
            by_identity[key] = span
        elif current.get('dur_ms') is None and span.get('dur_ms') is not None:
            by_identity[key] = span

    for span in sess.spans:
        parent_span_id = span.get('parent_span_id')
        if not parent_span_id:
            span['parent_seq'] = None
            span['parent_found'] = None
            span['chain_source'] = ('legacy_inferred'
                                    if span.get('inferred_parent_seq') is not None
                                    else 'none')
            continue
        parent = by_identity.get((span.get('trace_id'), parent_span_id))
        span['parent_seq'] = parent.get('seq') if parent else None
        span['parent_found'] = parent is not None
        span['chain_source'] = 'session_trace_parent_span_id'

    # 深度：只沿同一 trace 的父链往上数，带环保护。
    def depth_of(span):
        seen = set()
        depth = 0
        current = span
        while True:
            parent_span_id = current.get('parent_span_id')
            key = (current.get('trace_id'), parent_span_id)
            if not parent_span_id or key in seen:
                break
            seen.add(key)
            parent = by_identity.get(key)
            if parent is None:
                break
            depth += 1
            current = parent
        return depth

    for span in sess.spans:
        span['chain_depth'] = depth_of(span) if span.get('span_id') else None
    sess.span_by_id = by_identity
    return by_identity


# 这些采样/资源/计数事件的抽取挂在 Session 上，避免主循环过长。
def _sample_event(self, kind, entry, path, number):
    file = path
    if kind in ('gc', 'catalog_install_slice_overrun', 'catalog_install_work',
                'asset_model_delta_published', 'asset_model_delta_declined', 'process'):
        self.diagnostic_observations.append(dict(entry=entry,
            evidence={'file': path, 'line': number}))
    if kind in ('performance', 'process', 'material_performance', 'material_cache'):
        self.samples.append({
            'event': kind, 'seq': entry.get('sequence'),
            'time': _num(entry.get('time')), 'monotonic': _num(entry.get('monotonic')),
        })
        if kind not in self.sampler_names:
            self.sampler_names.append(kind)
        peak(self.resource_peaks, 'rss_bytes', _num(entry.get('rss_bytes')), entry, file)
        peak(self.resource_peaks, 'cpu_one_core_percent',
             _num(entry.get('cpu_one_core_percent')), entry, file)
        peak(self.resource_peaks, 'image_cache_bytes',
             _num(entry.get('image_cache_bytes')), entry, file)
        peak(self.resource_peaks, 'thumbnail_bytes', _num(entry.get('thumbnail_bytes')),
             entry, file)
        peak(self.resource_peaks, 'asset_tile_bytes', _num(entry.get('asset_tile_bytes')),
             entry, file)
        peak(self.resource_peaks, 'slot_icon_bytes', _num(entry.get('slot_icon_bytes')),
             entry, file)
        peak(self.resource_peaks, 'heap_bytes',
             _num((entry.get('metrics') or {}).get('heap_bytes')), entry, file)
        peak(self.resource_peaks, 'material_jobs', _num(entry.get('jobs')), entry, file)
        if kind == 'performance':
            self.count_layers.append({
                'layer': 'painted_cards', 'metric': 'asset_cards_painted',
                'value': entry.get('asset_cards_painted'),
                'seq': entry.get('sequence'),
            })
            self.count_layers.append({
                'layer': 'live_assets', 'metric': 'assets',
                'value': entry.get('assets'), 'seq': entry.get('sequence'),
            })
    elif kind == 'catalog_browser_rows_read':
        rows = _num(entry.get('rows'), 0.0)
        pages = _num(entry.get('pages'), 0.0)
        cell = self.catalog.setdefault('browser_batches', {
            'batches': 0, 'rows': 0.0, 'pages': 0.0, 'first_seq': entry.get('sequence'),
            'last_seq': entry.get('sequence'), 'scopes': {}, 'batch_rows': []})
        cell['batches'] += 1
        cell['rows'] += rows
        cell['pages'] += pages
        cell['last_seq'] = entry.get('sequence')
        scope = entry.get('scope') or '(none)'
        cell['scopes'][scope] = cell['scopes'].get(scope, 0) + 1
        if len(cell['batch_rows']) < 400:
            cell['batch_rows'].append([entry.get('sequence'), rows])
    elif kind == 'catalog_rows_read':
        self.catalog.setdefault('reads', []).append({
            'seq': entry.get('sequence'), 'rows': entry.get('rows'),
            'pages': entry.get('pages'), 'limit': entry.get('limit'),
            'complete': entry.get('complete'), 'cancelled': entry.get('cancelled'),
            'truncated': entry.get('truncated'),
            'limit_respected': entry.get('limit_respected'),
            'max_page': entry.get('max_page'),
            'page_limit_respected': entry.get('page_limit_respected'),
            'result_limit_respected': entry.get('result_limit_respected'),
            'max_rows': entry.get('max_rows'),
            'yielded': entry.get('yielded'), 'returned': entry.get('returned'),
        })
    elif kind in ('catalog_scope_hit', 'catalog_scope_miss'):
        self.scope_events.append({
            'seq': entry.get('sequence'), 'result': 'hit' if kind.endswith('hit') else 'miss',
            'reason': entry.get('reason'), 'partial': entry.get('partial'),
            'monotonic': _num(entry.get('monotonic')),
        })
    elif kind.startswith('material_task_'):
        task = entry.get('task')
        if task:
            slot = self.queue_tasks.setdefault(task, {})
            slot[kind] = {'seq': entry.get('sequence'),
                          'monotonic': _num(entry.get('monotonic')),
                          'kind': entry.get('task_kind'),
                          'material': entry.get('material'),
                          'current': entry.get('current')}
    elif kind == 'image_decode_pending':
        self.image_decode.append({
            'seq': entry.get('sequence'),
            'pending': _num(entry.get('pending')),
            'max_pending': _num(entry.get('max_pending')),
            'max_pending_seen': _num(entry.get('max_pending_seen')),
            'waiting_owners': _num(entry.get('waiting_owners')),
        })
    elif kind in ('paged_catalog_build_progress', 'paged_scope_prepared',
                  'paged_prepare_processed'):
        phase = ('catalog_build' if kind == 'paged_catalog_build_progress'
                 else 'scope' if kind == 'paged_scope_prepared'
                 else entry.get('phase'))
        if phase == 'catalog_build':
            operation_id = entry.get('build_id')
            semantic = (phase, operation_id, entry.get('processed'),
                        entry.get('processed_this_run'), entry.get('total_records'),
                        entry.get('total_known'), entry.get('final'))
            layer = 'catalog_build_progress'
        elif phase == 'scope':
            operation_id = entry.get('open_id')
            semantic = (phase, operation_id, entry.get('scope_ids'),
                        entry.get('pixmap_count'), entry.get('non_pixmap_count'),
                        tuple(entry.get('kinds') or ()), entry.get('canvas'))
            layer = 'scope_prepared'
        else:
            operation_id = None
            semantic = ('ambiguous', entry.get('sequence'))
            layer = 'ambiguous_paged_progress'
        if semantic not in self._paged_progress_seen:
            self._paged_progress_seen.add(semantic)
            fact = {
                'layer': layer, 'phase': phase or 'ambiguous',
                'operation_id': operation_id, 'event': kind,
                'legacy': bool(entry.get('legacy')),
                'value': entry.get('processed'), 'seq': entry.get('sequence'),
                'processed_this_run': entry.get('processed_this_run'),
                'total_records': entry.get('total_records'),
                'total_known': entry.get('total_known'),
                'final': entry.get('final'),
                'scope_ids': entry.get('scope_ids'),
                'pixmap_count': entry.get('pixmap_count'),
                'non_pixmap_count': entry.get('non_pixmap_count'),
                'canvas': entry.get('canvas'), 'kinds': entry.get('kinds'),
            }
            self.paged_progress.append(fact)
            self.count_layers.append(dict(fact, metric='processed'))
    elif kind == 'project_partial_session_attached':
        self.count_layers.append({
            'layer': 'partial_baseline', 'metric': 'items',
            'value': entry.get('items'), 'pages': entry.get('pages'),
            'seq': entry.get('sequence')})
    elif kind == 'material_cards_payload':
        self.count_layers.append({
            'layer': 'cards_model', 'metric': 'records',
            'value': entry.get('records'), 'bytes': entry.get('bytes'),
            'seq': entry.get('sequence')})
    elif kind == 'material_cards_patch':
        self.count_layers.append({
            'layer': 'cards_patched', 'metric': 'distinct_material',
            'value': entry.get('material'), 'seq': entry.get('sequence')})
    elif kind == 'sql.execute' or kind == 'sql.fetch':
        rows = entry.get('rows')
        if rows is not None:
            self.count_layers.append({
                'layer': 'sql_result_rows', 'metric': 'rows',
                'value': rows, 'seq': entry.get('sequence'),
                'query': (entry.get('query') or '')[:80]})
    elif kind == 'material_browser':
        metrics = entry.get('metrics') or {}
        self.count_layers.append({
            'layer': 'browser_metrics', 'metric': 'counts',
            'value': metrics.get('counts'), 'seq': entry.get('sequence'),
            'longest_task_ms': metrics.get('longest_task_ms')})
    elif kind == 'project_open_finished':
        self.count_layers.append({
            'layer': 'open_milestone', 'metric': 'finalization_complete',
            'value': entry.get('duration_ms'), 'seq': entry.get('sequence'),
            'phase': entry.get('phase')})


Session.sample_event = _sample_event


# --------------------------------------------------------------------------
# schema v2 新增事件族（原始数值原样收下，加工只发生在报告层）
# --------------------------------------------------------------------------
def _scope_key_of(entry):
    """呈现 marker 的范围身份：project + generation + scope 指纹。

    拿不到的字段就是 ``None``（未知），不拿别的字段顶替。
    """
    return '|'.join([
        str(entry.get('project_id')),
        str(entry.get('generation')),
        str(entry.get('scope_pages_fingerprint')),
    ])


def _v2_event(self, kind, entry, path, number):
    """收下新事件族：队列汇总、资源汇总、呈现 marker、stall 归类。

    这里只做「取值 + 记证据」，不做任何推断；结论留给报告层。
    """
    evidence = {'file': path, 'line': number, 'sequence': entry.get('sequence')}
    if kind in PRESENTATION_EVIDENCE_EVENTS:
        fact = dict(entry)
        fact['seq'] = entry.get('sequence')
        fact['evidence'] = evidence
        self.presentation_evidence.append(fact)
    if kind == 'catalog_result_published':
        # AI-06's publication event. Every counter is **this attempt**;
        # the identity fields (``publication_id`` / ``query_generation`` /
        # ``publication_seq``) say which publication it is, so a redelivery
        # can be recognised and an older publication must not overwrite a
        # newer one.
        self.catalog_publications.append({
            'seq': entry.get('sequence'),
            'publication_id': entry.get('publication_id'),
            'query_digest': entry.get('query_digest'),
            'query_generation': entry.get('query_generation'),
            'publication_seq': entry.get('publication_seq'),
            'open_epoch': entry.get('open_epoch'),
            'session_id': entry.get('session_id'),
            'project': entry.get('project'),
            'source_revision': entry.get('source_revision'),
            'overlay_revision': entry.get('overlay_revision'),
            'mode': entry.get('mode'),
            'published_rows': _count(entry.get('published_rows')),
            'evaluated_rows': _count(entry.get('evaluated_rows')),
            'proxies_created': _count(entry.get('proxies_created')),
            'read_rows': _count(entry.get('read_rows')),
            'candidate_rows': _count(entry.get('candidate_rows')),
            'result_reused': entry.get('result_reused'),
            'read_reused': entry.get('read_reused'),
            'complete': entry.get('complete'),
            'final': entry.get('final'),
            'reason': entry.get('reason'),
            'counter_basis': entry.get('counter_basis'),
            'evaluated_rows_cumulative': entry.get('evaluated_rows_cumulative'),
            'proxies_created_cumulative': entry.get('proxies_created_cumulative'),
            'read_rows_cumulative': entry.get('read_rows_cumulative'),
            'evidence': evidence,
        })
    if kind == 'storage.workflow':
        if len(self.storage_workflow_events) >= 4096:
            self.storage_workflow_truncated += 1
        self.storage_workflow_events.append(dict(entry))
    if kind == 'hotpath_rollup':
        self.hotpath_rollups.append({
            'seq': entry.get('sequence'),
            'name': entry.get('name'),
            'calls': _count(entry.get('calls')),
            'total_ms': _num(entry.get('total_ms')),
            'max_ms': _num(entry.get('max_ms')),
            'failed_calls': _count(entry.get('failed_calls')),
            'window_calls': _count(entry.get('window_calls')),
            'window_total_ms': _num(entry.get('window_total_ms')),
            'window_max_ms': _num(entry.get('window_max_ms')),
            'window_failed_calls': _count(entry.get('window_failed_calls')),
            'counters_cumulative': entry.get('counters_cumulative'),
            'cumulative_scope': entry.get('cumulative_scope'),
            'reset_reason': entry.get('reset_reason'),
            'flush_reason': entry.get('flush_reason'),
            'owner_project_token': entry.get('owner_project_token'),
            'owner_open_epoch': entry.get('owner_open_epoch'),
            'owner_generation': entry.get('owner_generation'),
            'minimum_sample_ms': _num(entry.get('minimum_sample_ms')),
            'sampling_strategy': entry.get('sampling_strategy'),
            'trace_mode': entry.get('trace_mode'),
            'recording_contract_version': entry.get('recording_contract_version'),
            'monotonic': _num(entry.get('monotonic')),
            'evidence': evidence,
        })
    elif kind == 'hotpath_sample':
        self.hotpath_samples.append({
            'seq': entry.get('sequence'),
            'name': entry.get('name'),
            'duration_ms': _num(entry.get('duration_ms')),
            'failed': entry.get('failed'),
            'observation': entry.get('observation'),
            'sampling_strategy': entry.get('sampling_strategy'),
            'minimum_sample_ms': _num(entry.get('minimum_sample_ms')),
            'trace_mode': entry.get('trace_mode'),
            'monotonic': _num(entry.get('monotonic')),
            'evidence': evidence,
        })
    elif kind == 'trace_manifest':
        self.manifests.append({
            'seq': entry.get('sequence'),
            'phase': entry.get('phase'),
            'schema_version': entry.get('schema_version'),
            'recording_contract_version': entry.get('recording_contract_version'),
            'mode': entry.get('mode'),
            'log': entry.get('log'),
            'rotation_max_bytes': _num(entry.get('rotation_max_bytes')),
            'rotation_backup_count': _count(entry.get('rotation_backup_count')),
            'retained_log_files': _count(entry.get('retained_log_files')),
            'retention_policy': entry.get('retention_policy'),
            'evidence_retention': entry.get('evidence_retention'),
            'hotpath_rollup_interval_s': _num(entry.get('hotpath_rollup_interval_s')),
            'periodic_sampling_policy': {key: entry.get(key) for key in (
                'normal_process_sample_interval_s', 'diagnostic_process_sample_interval_s',
                'normal_budget_sample_interval_s', 'diagnostic_budget_sample_interval_s',
                'periodic_sampling_note')},
            'gc_min_duration_ms': _num(entry.get('gc_min_duration_ms')),
            'hotpath_paths': entry.get('hotpath_paths'),
            'events_written': _count(entry.get('events_written')),
            'bytes_written': _count(entry.get('bytes_written')),
            'writer_ms_total': _num(entry.get('writer_ms_total')),
            'writer_ms_max': _num(entry.get('writer_ms_max')),
            'record_self_ms_total': _num(entry.get('record_self_ms_total')),
            'record_self_ms_max': _num(entry.get('record_self_ms_max')),
            'gc_collection_errors': _count(entry.get('gc_collection_errors')),
            'duration_s': _num(entry.get('duration_s')),
            'monotonic': _num(entry.get('monotonic')),
            'evidence': evidence,
        })
    elif kind == 'trace_version':
        self.version_snapshots.append({
            'seq': entry.get('sequence'),
            'schema_version': entry.get('schema_version'),
            'source_mode': entry.get('source_mode'),
            'entry_point': entry.get('entry_point'),
            'python': entry.get('python'),
            'executable': entry.get('executable'),
            'cwd': entry.get('cwd'),
            'version_snapshot_inconsistent': entry.get('version_snapshot_inconsistent'),
            'files': entry.get('files'),
            'error': entry.get('error'),
            'monotonic': _num(entry.get('monotonic')),
            'evidence': evidence,
        })
    if kind == 'queue_rollup':
        counters = entry.get('counters')
        if not isinstance(counters, dict):
            counters = {}
        self.queue_rollups.append({
            'seq': entry.get('sequence'),
            'category': entry.get('category'),
            'counters': dict((name, _count(counters.get(name)))
                             for name in QUEUE_COUNTERS),
            'pending': _count(entry.get('pending')),
            'running': _count(entry.get('running')),
            'ready': _count(entry.get('ready')),
            'limit': _count(entry.get('limit')),
            'peak_pending': _count(entry.get('peak_pending')),
            'peak_running': _count(entry.get('peak_running')),
            'oldest_age_ms': _num(entry.get('oldest_age_ms')),
            'oldest_age_status': entry.get('oldest_age_status'),
            'oldest_task_id': entry.get('oldest_task_id'),
            'busy_period_ms': _num(entry.get('busy_period_ms')),
            'busy_period_status': entry.get('busy_period_status'),
            'peak_scope': entry.get('peak_scope'),
            'counters_cumulative': entry.get('counters_cumulative'),
            'cumulative_scope': entry.get('cumulative_scope'),
            'reset_reason': entry.get('reset_reason'),
            'flush_reason': entry.get('flush_reason'),
            'owner_project_token': entry.get('owner_project_token'),
            'owner_open_epoch': entry.get('owner_open_epoch'),
            'owner_generation': entry.get('owner_generation'),
            'sampling_strategy': entry.get('sampling_strategy'),
            'recording_contract_version': entry.get('recording_contract_version'),
            'wait_ms_total': _num(entry.get('wait_ms_total')),
            'wait_ms_max': _num(entry.get('wait_ms_max')),
            'wait_samples': _count(entry.get('wait_samples')),
            'service_ms_total': _num(entry.get('service_ms_total')),
            'service_ms_max': _num(entry.get('service_ms_max')),
            'service_samples': _count(entry.get('service_samples')),
            'shared_tasks': _count(entry.get('shared_tasks')),
            'atomic_in_progress': _count(entry.get('atomic_in_progress')),
            'cancelled_remaining_work': _count(entry.get('cancelled_remaining_work')),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'evidence': evidence,
        })
    elif kind == 'resource_rollup':
        classes = entry.get('classes')
        if not isinstance(classes, dict):
            classes = {}
        self.resource_rollups.append({
            'seq': entry.get('sequence'),
            'reason': entry.get('reason'),
            'classes': classes,
            'limit': _num(entry.get('limit')),
            'bytes': _num(entry.get('bytes')),
            'over_budget': _num(entry.get('over_budget')),
            'pin_over_budget': _num(entry.get('pin_over_budget')),
            'resident_bytes': _num(entry.get('resident_bytes')),
            'budget_kind': entry.get('budget_kind'),
            'budget_is_rss_cap': entry.get('budget_is_rss_cap'),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'evidence': evidence,
        })
    elif kind in PRESENTATION_MARKERS:
        self.presentation_markers.append({
            'seq': entry.get('sequence'),
            'event': kind,
            'kind': PRESENTATION_MARKERS[kind],
            'status': entry.get('status'),
            'painted': entry.get('painted'),
            'reason': entry.get('reason'),
            'scope_key': _scope_key_of(entry),
            'project_id': entry.get('project_id'),
            'generation': entry.get('generation'),
            'scope_mode': entry.get('scope_mode'),
            'commit_barrier': entry.get('commit_barrier'),
            'scope_pages_fingerprint': entry.get('scope_pages_fingerprint'),
            'quality_covered': _count(entry.get('quality_covered')),
            'quality_total': _count(entry.get('quality_total')),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'evidence': evidence,
        })
    elif kind == PAINT_FRAME_EVENT:
        self.paint_frames.append({
            'seq': entry.get('sequence'),
            'frames': _count(entry.get('frames')),
            'cards': _count(entry.get('cards')),
            'content_paints': _count(entry.get('content_paints')),
            'text_paints': _count(entry.get('text_paints')),
            'quality_samples': _count(entry.get('quality_samples')),
            'quality_ready_samples': _count(entry.get('quality_ready_samples')),
            'quality_unknown_samples': _count(entry.get('quality_unknown_samples')),
            'first_paint_ms': _num(entry.get('first_paint_ms')),
            'flushes': _count(entry.get('flushes')),
            'window_ms': _num(entry.get('window_ms')),
            'scope_key': _scope_key_of(entry),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'evidence': evidence,
        })
    elif kind == 'ui.stall_dispatch_batch':
        self.stall_dispatch_batches.append(dict(entry=entry, evidence=evidence))
    elif kind == 'ui.stall_context':
        self.stall_contexts.append({
            'seq': entry.get('sequence'),
            'stall_id': entry.get('stall_id'),
            'duration_ms': _num(entry.get('duration_ms')),
            'window_start_monotonic': _num(entry.get('window_start_monotonic')),
            'window_end_monotonic': _num(entry.get('window_end_monotonic')),
            'classification': entry.get('classification'),
            'classification_reason': entry.get('classification_reason'),
            'dispatch_evidence_version': entry.get('dispatch_evidence_version'),
            'dispatch_span_ms': _num(entry.get('dispatch_span_ms')),
            'dispatches_completed': _count(entry.get('dispatches_completed')),
            'dispatch_batch_version': entry.get('dispatch_batch_version'),
            'dispatch_batch_count': entry.get('dispatch_batch_count'),
            'dispatch_interval_count': entry.get('dispatch_interval_count'),
            'dispatch_intervals_retained': entry.get('dispatch_intervals_retained'),
            'dispatch_intervals_omitted': entry.get('dispatch_intervals_omitted'),
            'dispatch_intervals': (entry.get('dispatch_intervals')
                                   if isinstance(entry.get('dispatch_intervals'), list)
                                   else []),
            'covering_dispatches': (entry.get('covering_dispatches')
                                    if isinstance(entry.get('covering_dispatches'), list)
                                    else []),
            'heartbeat_delay_ms': _num(entry.get('heartbeat_delay_ms')),
            'timer_starved': entry.get('timer_starved'),
            'sample_count': _count(entry.get('sample_count')),
            'stack_depth': _count(entry.get('stack_depth')),
            'stack_truncated': entry.get('stack_truncated'),
            'stack_is_sampled_not_blame': entry.get('stack_is_sampled_not_blame'),
            'stack_caveat': entry.get('stack_caveat'),
            'nested_loop': entry.get('nested_loop'),
            'modal': entry.get('modal'),
            'observed_gui_gc_ms': _num(entry.get('observed_gui_gc_ms')),
            'observed_gui_gc_union_ms': _num(entry.get('observed_gui_gc_union_ms')),
            'observed_gui_gc_naive_sum_ms': _num(entry.get('observed_gui_gc_naive_sum_ms')),
            'observed_gui_gc_fraction': _num(entry.get('observed_gui_gc_fraction')),
            'observed_gui_gc_interval_count': _count(
                entry.get('observed_gui_gc_interval_count')),
            'observed_gui_gc_merged_interval_count': _count(
                entry.get('observed_gui_gc_merged_interval_count')),
            'gc_attribution': entry.get('gc_attribution'),
            'gc_classification_applied': entry.get('gc_classification_applied'),
            'gc_classification_skip_reason': entry.get(
                'gc_classification_skip_reason'),
            'background_busy': entry.get('background_busy'),
            'background_is_causal_evidence': entry.get(
                'background_is_causal_evidence'),
            'project': entry.get('project'),
            'mode': entry.get('mode'),
            'effective_mode': entry.get('effective_mode'),
            'scope_fingerprint': entry.get('scope_fingerprint'),
            'stack_sample': entry.get('stack_sample'),
            'stage': entry.get('stage'),
            'trace_id': entry.get('trace_id'),
            'span_id': entry.get('span_id'),
            'parent_span_id': entry.get('parent_span_id'),
            'origin_action_id': entry.get('origin_action_id'),
            'evidence': evidence,
        })


Session.v2_event = _v2_event


def peak(store, key, value, entry, path):
    if value is None:
        return
    cell = store.get(key)
    if cell is None or value > cell['value']:
        store[key] = {'value': value, 'seq': entry.get('sequence'), 'file': path}


# --------------------------------------------------------------------------
# 完整性
# --------------------------------------------------------------------------
def lifecycle_report(sess):
    facts = sess.lifecycle_evidence
    failures = [fact for fact in facts if fact['entry']['event'] == 'trace_writer_failed']
    drained = [fact for fact in facts if fact['entry']['event'] == 'trace_writer_drained']
    end_facts = [fact for fact in facts if fact['entry']['event'] == 'session_end']
    protocol = any(type(fact['entry'].get('writer_lifecycle_version')) is int
                   and fact['entry']['writer_lifecycle_version'] == 1
                   for fact in end_facts)
    drained_after_end = bool(end_facts and drained and any(
        type(end['entry'].get('sequence')) is int
        and type(drain['entry'].get('sequence')) is int
        and drain['entry']['sequence'] > end['entry']['sequence']
        and type(drain['entry'].get('pending_unwritten')) is int
        and drain['entry']['pending_unwritten'] == 0
        for end in end_facts for drain in drained))
    state = ('writer_failed' if failures else
             'shutdown_drained' if drained_after_end else
             'shutdown_drain_unconfirmed' if sess.has_session_end and protocol else
             'end_recorded_legacy_drain_unmeasured' if sess.has_session_end else
             'end_not_observed')
    return dict(state=state, writer_lifecycle_version=1 if protocol else None,
                drain_confirmed=drained_after_end, evidence=facts,
                process_exit='not_observed_by_offline_trace',
                note='日志排空不等于进程已退出；无 end 不证明崩溃、强杀或写入器死亡。')


def retention_report(sess):
    values = sorted(sess.operation_sequences)
    rotations = sess.retention_evidence
    counts = [fact['entry'].get('rotation_count') for fact in rotations]
    latest = max((count for count in counts
                  if type(count) is int and count >= 0), default=0)
    pruned_ranges = []
    for fact in rotations:
        entry = fact['entry']
        index = entry.get('closed_segment_index')
        backups = entry.get('rotation_backup_count')
        if (type(index) is int and type(backups) is int and index >= 0 and backups >= 0
                and index < latest - backups):
            ranges = entry.get('closed_segment_sequence_ranges')
            for pair in ranges if isinstance(ranges, list) else []:
                if (isinstance(pair, list) and len(pair) == 2
                        and all(isinstance(n, int) and not isinstance(n, bool) for n in pair)
                        and 0 <= pair[0] <= pair[1]):
                    pruned_ranges.append(pair)
    merged = []
    for left, right in sorted(pruned_ranges):
        if merged and left <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], right)
        else:
            merged.append([left, right])
    # An externally saved old segment can recover a physically pruned record.
    # Do not call recovered evidence missing merely because rollover occurred.
    observed = sorted(sess.observed_sequences)
    cursor, missing_ranges = 0, []
    for left, right in merged:
        while cursor < len(observed) and observed[cursor] < left:
            cursor += 1
        next_missing = left
        while cursor < len(observed) and observed[cursor] <= right:
            if observed[cursor] > next_missing:
                missing_ranges.append([next_missing, observed[cursor]-1])
            next_missing = observed[cursor]+1
            cursor += 1
        if next_missing <= right:
            missing_ranges.append([next_missing, right])
    return dict(operation_sequence_range=[values[0], values[-1]] if values else None,
                operation_time_range=sess.operation_time_range,
                rotation_count_observed=latest if rotations else None,
                verified_pruned_sequence_ranges=merged,
                verified_missing_sequence_ranges=missing_ranges,
                evidence=rotations,
                note='轮转保留窗口不是全量历史；优先级重排可在边界留下非连续序号。'
                     '只有成功轮转检查点能证明具体序号被裁剪；其余缺口不推断原因。')


def completeness(sess, truncated_last_line):
    reasons, incomplete = [], False
    lifecycle = lifecycle_report(sess)
    retention = retention_report(sess)
    if sess.input_evidence_gaps:
        incomplete = True
        reasons.append('input_evidence_gap：IO/解析、序号覆盖或内容冲突影响输入证据；'
                       '不能据此推断队列丢失数量。')
    if not sess.has_session_start:
        reasons.append('缺少 session_start：日志可能从中途开始。')
        incomplete = True
    if not sess.has_session_end:
        reasons.append('缺少 session_end：结束未观察到；可能仍在运行、未执行关闭、'
                       '被强杀或通道失败。仅凭文件尾部不能区分，也不能当作正常结束。')
        incomplete = True
    if lifecycle['state'] in ('writer_failed', 'shutdown_drain_unconfirmed'):
        reasons.append('写入生命周期状态=%s；未确认日志排空。' % lifecycle['state'])
        incomplete = True
    if retention['verified_missing_sequence_ranges']:
        reasons.append('成功轮转检查点证明部分历史已裁剪且不在输入中；不等同于队列丢失。')
        incomplete = True
    if sess.dropped_events:
        reasons.append('dropped_events=%d：队列有丢弃，事件序列有洞，'
                       '耗时排行只反映采样到的部分。' % sess.dropped_events)
        incomplete = True
    if sess.trace_incomplete or sess.seq_gap_count or any(sess.dropped_by_kind.values()):
        incomplete = True
        reasons.append('schema_v2 trace_incomplete=%s; seq_gap_count=%s; dropped_by_kind=%s'
                       % (sess.trace_incomplete, sess.seq_gap_count, sess.dropped_by_kind))
    for item in truncated_last_line:
        if item['file'] in sess.files:
            reasons.append('末行截断（%s 第 %d 行）：尾部数据不完整。'
                           % (item['file'], item['line']))
            incomplete = True

    orphan_starts = sess.unmatched_starts or []
    if orphan_starts:
        incomplete = True
        reasons.append('%d 个 operation_start 没有配对的 operation（缺 end）'
                       '（seq: %s）：这些工作**不自动算成功**，也不补时长。'
                       % (len(orphan_starts),
                          ', '.join(str(s['seq']) for s in orphan_starts[:12])))
    sess.unmatched_starts = orphan_starts
    orphan_ends = [span for span in sess.spans
                   if span.get('event') in ('operation', 'open_phase')
                   and span.get('role') == 'end' and span.get('unterminated')]
    if orphan_ends:
        incomplete = True
        reasons.append('%d 条显式生命周期 end 没有可靠 start 配对（seq: %s）；'
                       '可能被左侧裁剪或关联被拒绝，不补造 start。' %
                       (len(orphan_ends), s_seq(orphan_ends)))
    if sess.anomalies.get('span_identity_conflict'):
        incomplete = True
        reasons.append('存在真正重复的 span 身份，关联不可信。')

    unknown_span = [s for s in sess.spans
                    if s['dur_ms'] is None and s['status'] != 'start']
    if unknown_span:
        incomplete = True
        reasons.append('%d 条 span 缺 duration/时间戳（未知 span）：无法计入耗时统计。'
                       % len(unknown_span))

    if sess.parent_span_refs_missing():
        incomplete = True

    return {
        'incomplete': incomplete,
        'reasons': reasons,
        'input_evidence_gap': bool(sess.input_evidence_gaps),
        'input_evidence_gaps': sess.input_evidence_gaps,
        'dropped_events': sess.dropped_events,
        'dropped_events_observed': sess.dropped_events_observed,
        'lifecycle': lifecycle,
        'retention': retention,
        'counter_scope_note': '事件逐条计数仅覆盖输入窗口；队列累计快照按生产者口径，'
                              '不因输入缺口一概降为下界，不跨快照相加。',
        'trace_incomplete': sess.trace_incomplete,
        'seq_gap_count': sess.seq_gap_count,
        'dropped_by_kind': sess.dropped_by_kind,
        'seq_gap_basis': sess.seq_gap_basis,
        'sequence_order_guaranteed': sess.sequence_order_guaranteed,
        'dropped_event_samples': sess.dropped_seqs,
        'unterminated_operation_starts': [s['seq'] for s in orphan_starts],
        'unpaired_explicit_operation_ends': [s['seq'] for s in orphan_ends],
        'unknown_span_count': len(unknown_span),
        'anomalous_line_counts': dict(sess.anomalies),
        'anomalous_line_samples': sess.anomaly_samples,
        'policy': 'incomplete 只做标注，绝不自动补成「成功」或「已完成」。',
    }


def _parent_span_refs_missing(self):
    known = {(s.get('trace_id'), s['span_id']) for s in self.spans
             if s.get('span_id')}
    refs = [s for s in self.spans if s.get('parent_span_id')]
    missing = [s for s in refs
               if (s.get('trace_id'), s['parent_span_id']) not in known]
    if missing:
        self.note_anomaly('unknown_parent_span', missing[0],
                          '同 trace 的 parent_span_id 未出现，seq=%s' % s_seq(missing))
    return len(missing)


def s_seq(spans):
    return ', '.join(str(s['seq']) for s in spans[:12]) or '(none)'


Session.parent_span_refs_missing = _parent_span_refs_missing


# --------------------------------------------------------------------------
# stall 处理
# --------------------------------------------------------------------------
def stall_report(sess):
    complete, ongoing, legacy = [], [], []
    for entry in _raw_stalls(sess):
        phase = entry['phase']
        if phase == 'end':
            complete.append(entry)
        elif phase == 'ongoing':
            ongoing.append(entry)
        else:
            legacy.append(entry)

    paired = set()
    for end in complete:
        same_id = [item for item in ongoing
                   if end.get('stall_id') is not None
                   and item.get('stall_id') == end.get('stall_id')]
        if same_id:
            for item in same_id:
                paired.add(id(item))
            end['also_seen_ongoing'] = [item['seq'] for item in same_id]
            end['association_basis'] = 'session+stall_id'
            continue
        if end.get('stall_id') is not None:
            end['association_basis'] = 'session+stall_id_no_ongoing'
            continue
        # Old logs have no identity. Keep the former adjacency guess, but label
        # it as heuristic and consume each ongoing at most once.
        for ong in ongoing:
            if (ong.get('stall_id') is None
                    and abs(end['dur_ms'] - ong['dur_ms']) < 0.51
                    and end['seq'] is not None and ong['seq'] is not None
                    and 0 <= end['seq'] - ong['seq'] <= 50
                    and id(ong) not in paired):
                paired.add(id(ong))
                end['also_seen_ongoing'] = [ong['seq']]
                end['association_basis'] = 'legacy_adjacency_heuristic'
                break

    unmatched = [item for item in ongoing if id(item) not in paired]
    unterminated = []
    unresolved_by_id = {}
    for item in unmatched:
        stall_id = item.get('stall_id')
        if stall_id is None:
            # Legacy samples cannot be proven to describe the same window.
            unterminated.append(item)
            continue
        previous = unresolved_by_id.get(stall_id)
        if previous is None or item['dur_ms'] > previous['dur_ms']:
            unresolved_by_id[stall_id] = item
    unterminated.extend(unresolved_by_id.values())
    # 关键：只有 end 计入完整卡顿。unterminated 是「可能死在中途」，
    # 单列出来，绝不混进完整卡顿分母。
    counted = complete + legacy
    values = [e['dur_ms'] for e in counted]
    # The GC evidence lives on ``ui.stall_context``; it is joined to the
    # terminal stall by ``stall_id``. A classification of "gc_pause" without
    # the measured milliseconds is not evidence.
    gc_by_stall = {}
    for context in sess.stall_contexts:
        stall_id = context.get('stall_id')
        if stall_id is not None:
            gc_by_stall.setdefault(stall_id, context)

    def gc_evidence(entry):
        context = gc_by_stall.get(entry.get('stall_id'))
        if context is None:
            return None
        return {
            'observed_gui_gc_ms': context.get('observed_gui_gc_ms'),
            'observed_gui_gc_union_ms': context.get('observed_gui_gc_union_ms'),
            'observed_gui_gc_naive_sum_ms': context.get('observed_gui_gc_naive_sum_ms'),
            'observed_gui_gc_fraction': context.get('observed_gui_gc_fraction'),
            'observed_gui_gc_interval_count': context.get('observed_gui_gc_interval_count'),
            'observed_gui_gc_merged_interval_count': context.get(
                'observed_gui_gc_merged_interval_count'),
            'gc_attribution': context.get('gc_attribution'),
            'gc_classification_applied': context.get('gc_classification_applied'),
            'gc_classification_skip_reason': context.get('gc_classification_skip_reason'),
            'modal': context.get('modal'),
            'evidence': context.get('evidence'),
        }
    out = {
        'policy': ('只有 phase=end（或无 phase 的旧版单发事件）计入完整卡顿；'
                   'phase=ongoing 不作为独立卡顿重复计数，只用来兜底进程中途死掉的场景。'),
        'complete_stalls': len(complete),
        'legacy_single_shot_stalls': len(legacy),
        'ongoing_repeats_ignored': len(ongoing) - len(unterminated),
        'unterminated_stalls_no_end': len(unterminated),
        'duration_distribution': distribution(
            values, '本 session 全部完整卡顿 duration_ms（n=complete+legacy 单发）'),
        'impact_level_counts': dict(collections.Counter(
            entry.get('impact_level') or '(legacy/unknown)' for entry in counted)),
        'impact_policy_versions': sorted(set(
            entry.get('impact_policy_version') for entry in counted
            if entry.get('impact_policy_version') is not None)),
        'top_stalls': sorted(
            [{'seq': e['seq'], 'duration_ms': e['dur_ms'], 'severity': e['severity'],
              'thread': e['thread'], 'samples': e['samples'],
              'truncated': e['truncated'],
              'top_stack': (e['stacks'][0][1] if e['stacks'] else None),
              'top_stack_share': top_share(e),
              'stall_id': e.get('stall_id'),
              'impact_level': e.get('impact_level'),
              'impact_policy_version': e.get('impact_policy_version'),
              'classification': e.get('classification'),
              'classification_reason': e.get('classification_reason'),
              'gc_evidence': gc_evidence(e),
              'also_seen_ongoing_at_seq': e.get('also_seen_ongoing'),
              'association_basis': e.get('association_basis'),
              'evidence': e['evidence']}
             for e in counted], key=lambda x: -x['duration_ms'])[:12],
        'unterminated_detail': [
            {'seq': e['seq'], 'duration_ms_so_far': e['dur_ms'],
             'note': '只有 ongoing、没有 end —— 该次卡顿的最终时长未知，不补全。',
             'evidence': e['evidence']} for e in unterminated],
    }
    return out


def top_share(stall):
    """主导栈占采样数的百分比 —— 采样为空时给 None，不给 0。"""
    stacks = stall.get('stacks') or []
    samples = stall.get('samples') or 0
    if not stacks or not samples:
        return None
    return round(100.0 * stacks[0][0] / samples, 1)


def _raw_stalls(sess):
    out = []
    for span in sess.spans:
        if span['event'] != 'gui_stall':
            continue
        out.append({
            'seq': span['seq'], 'phase': span.get('stall_phase'),
            'dur_ms': span['dur_ms'] or 0.0,
            'thread': span['thread'], 'stacks': span.get('stack_histogram') or [],
            'severity': span.get('severity'), 'samples': span.get('samples'),
            'truncated': span.get('truncated'),
            'stall_id': span.get('stall_id'),
            'impact_level': span.get('impact_level'),
            'impact_policy_version': span.get('impact_policy_version'),
            'classification': span.get('classification'),
            'classification_reason': span.get('classification_reason'),
            'evidence': span['evidence'],
        })
    return out


# --------------------------------------------------------------------------
# 窗口（= 旧日志里最接近 trace 的东西）
# --------------------------------------------------------------------------
def build_windows(sess, raw_by_kind):
    windows = []

    starts = raw_by_kind.get('project_open_start', [])
    readies = raw_by_kind.get('project_open_ready', [])
    finishes = raw_by_kind.get('project_open_finished', [])
    if starts:
        start = starts[0]
        end = readies[0] if readies else (finishes[0] if finishes else None)
        if end is not None:
            windows.append({
                'name': 'project_open',
                'start_seq': start.get('sequence'), 'end_seq': end.get('sequence'),
                'start_mono': _num(start.get('monotonic')),
                'end_mono': _num(end.get('monotonic')),
                'wall_ms': _num(end.get('duration_ms')),
                'note': '打开窗口。finalization_complete 与 partial_baseline_attached '
                        '是两个不同里程碑，分别列出，不合并。',
                'milestones': [
                    {'seq': f.get('sequence'), 'event': f.get('event'),
                     'phase': f.get('phase'), 'duration_ms': _num(f.get('duration_ms'))}
                    for f in finishes + readies],
            })

    save_starts = raw_by_kind.get('project_save_start', [])
    save_ends = raw_by_kind.get('project_save_finished', [])
    for index, (start, end) in enumerate(zip(save_starts, save_ends), 1):
        windows.append({
            'name': 'project_save#%d' % index,
            'start_seq': start.get('sequence'), 'end_seq': end.get('sequence'),
            'start_mono': _num(start.get('monotonic')),
            'end_mono': _num(end.get('monotonic')),
            'wall_ms': _num(end.get('duration_ms')),
            'failed': end.get('failed'),
            'note': '保存窗口（含 partial_project 快照编码与写盘）。',
            'milestones': [],
        })

    if sess.first_mono is not None and sess.last_mono is not None:
        windows.append({
            'name': 'whole_session',
            'start_seq': None, 'end_seq': None,
            'start_mono': sess.first_mono, 'end_mono': sess.last_mono,
            'wall_ms': round((sess.last_mono - sess.first_mono) * 1000.0, 3),
            'note': '整段会话，仅作为采样覆盖率与资源峰值的分母。',
            'milestones': [],
        })

    # 未配对的 open_phase start：标记阶段未闭合。
    for window in windows:
        window['timeline'] = window_report(sess, window)
    return windows


def _is_work_span(span):
    """Is this span a piece of **work**, or an observation *about* work?

    Two kinds of records are observations, not work:

    * a **threshold sample** -- its opening half was never recorded, so it
      cannot be unioned into a window without counting the same work twice.
    * a **stall record** -- the watchdog thread writes it to say the GUI made
      no progress for a while.  It measures a *blocked* main thread; it is
      not something the main thread did, and it is not background work
      either.  Counting it as a work span would put blocked time into the
      background bucket and inflate the "unattributed work" total with the
      very stalls it describes.
    """
    if span.get('observation') == 'threshold_sample_not_complete_span':
        return False
    if span.get('event') == 'gui_stall':
        return False
    return True


def window_report(sess, window):
    wall = window['wall_ms']
    lo, hi = window['start_mono'], window['end_mono']
    candidates = [s for s in sess.spans
                  if s['start_mono'] is not None and s['end_mono'] is not None
                  and s['end_mono'] >= lo and s['start_mono'] <= hi
                  and s['dur_ms'] is not None
                  and s['status'] != 'start']
    # A threshold sample is an **observation**, not a stage: its opening
    # half was never recorded, so it cannot be unioned into the window (it
    # would double count the very work it measured) and it must not
    # out-rank a real parent stage.
    observations = [s for s in candidates
                    if s.get('observation') == 'threshold_sample_not_complete_span']
    inside = [s for s in candidates if _is_work_span(s)]
    gui = [s for s in inside if s['thread'] in MAIN_THREADS]
    service = [s for s in inside if s['thread'] not in MAIN_THREADS]
    install = [s for s in gui if is_install(s['name'])]
    waitlike = [s for s in inside if is_wait(s['name'])]

    def union(items):
        return union_ms([(s['start_mono'], s['end_mono']) for s in items])

    all_union = union(inside)
    wall_derived = (round((hi - lo) * 1000.0, 3)
                     if (lo is not None and hi is not None) else wall)
    # 用墙钟做分母更诚实：即使 span 漏记，剩余量会落到 unaccounted。
    denominator = wall_derived if wall_derived else (wall or 0.0)

    buckets = [
        {'bucket': 'service_io（后台/工作线程）',
         'ms': round(union(service), 3), 'spans': len(service)},
        {'bucket': 'gui_install（主线程装配类）',
         'ms': round(union(install), 3), 'spans': len(install)},
        {'bucket': 'gui_other（主线程其它工作）',
         'ms': round(union([s for s in gui if s not in install]), 3),
         'spans': len(gui) - len(install)},
        {'bucket': 'wait_idle（名字含等待语义）',
         'ms': round(union(waitlike), 3), 'spans': len(waitlike)},
    ]
    for bucket in buckets:
        bucket['share_of_wall_pct'] = (
            round(100.0 * bucket['ms'] / denominator, 2) if denominator else None)

    unaccounted = max(0.0, denominator - all_union)

    stages = []
    for span in inside:
        stages.append({
            'rank_key': span['dur_ms'],
            'seq': span['seq'], 'name': span['name'], 'event': span['event'],
            'thread': span['thread'], 'ms': round(span['dur_ms'], 3),
            'rows': span['rows'], 'status': span['status'],
            'trace_id': span['trace_id'], 'span_id': span['span_id'],
            'parent_span_id': span['parent_span_id'],
            'parent_seq': span.get('parent_seq'),
            'chain_source': span.get('chain_source'),
            'origin_action_id': span.get('origin_action_id'),
            'link_confidence': span.get('link_confidence', 'unknown'),
            'evidence': span['evidence'],
        })
    stages.sort(key=lambda s: -s['rank_key'])
    for index, stage in enumerate(stages, 1):
        stage['rank'] = index
        stage.pop('rank_key')

    return {
        'window_ms': wall_derived,
        'window_ms_reported_by_log': wall,
        'span_count': len(inside),
        'threshold_observations': {
            'count': len(observations),
            'samples': [{'seq': s['seq'], 'name': s['name'],
                         'ms': round(s['dur_ms'], 3), 'event': s['event'],
                         'failed': s.get('failed'),
                         'evidence': s['evidence']}
                        for s in sorted(observations,
                                        key=lambda s: -(s['dur_ms'] or 0))[:15]],
            'note': ('阈值采样是独立观测事件：没有 opening，不是完整 span，'
                     '因此不并入窗口用时，也不与父阶段竞争排名。'),
        },
        'parent_child_chain': {
            'source': ('schema_v2（span_id / parent_span_id，日志声明）'
                       if any(s.get('span_id') for s in inside)
                       else 'legacy_inferred（旧日志无 span_id，父子由时间区间推断）'),
            'note': ('阶段排名用单条 span 耗时；有真实父子链时用 parent_span_id，'
                     '没有才退回 legacy 推断并标出来。'),
        },
        'critical_path_note': ('阶段排名用单条 span 耗时；分桶与占比用区间并集，'
                               '因此父子/并行 span 不会重复计入。'),
        'buckets': buckets,
        'union_all_spans_ms': round(all_union, 3),
        'unaccounted_ms': round(unaccounted, 3),
        'unaccounted_note': ('未被任何 span 覆盖的墙钟时间；可能是未记录的阶段、'
                             '事件循环空转、或日志缺口 —— 不臆测。'),
        'sum_of_span_ms': round(sum(s['dur_ms'] for s in inside), 3),
        'sum_vs_union_note': 'sum_of_span_ms 是朴素相加（含重叠），仅作对照，不用于结论。',
        'stage_ranking': stages[:20],
        'overlaps': overlaps_of(inside)[:15],
    }


def is_install(name):
    text = (name or '').lower()
    return any(hint in text for hint in GUI_INSTALL_HINTS)


def is_wait(name):
    text = (name or '').lower()
    return any(hint in text for hint in WAIT_HINTS)


# --------------------------------------------------------------------------
# 各专项结论
# --------------------------------------------------------------------------
def slow_sql_report(sess, raw_by_kind):
    rows = []
    for span in sess.spans:
        if span['event'] not in ('sql.execute', 'sql.fetch'):
            continue
        if (span['dur_ms'] or 0) <= 0:
            continue
        rows.append({
            'seq': span['seq'], 'event': span['event'], 'name': span['name'],
            'ms': round(span['dur_ms'], 3), 'rows': span['rows'],
            'thread': span['thread'], 'failed': span['failed'],
            'evidence': span['evidence'],
        })
    rows.sort(key=lambda r: -r['ms'])
    sql_ops = [{'seq': s['seq'], 'name': s['name'], 'ms': round(s['dur_ms'], 3),
                'rows': s['rows'], 'thread': s['thread'], 'status': s['status'],
                'start_seq': s.get('start_seq'),
                'inferred_parent_seq': s.get('inferred_parent_seq'),
                'link_confidence': s.get('link_confidence', 'unknown'),
                'evidence': s['evidence']}
               for s in sess.spans
               if s['dur_ms'] is not None
               and (s['name'] in ('project.sql_read', 'project.sql_write')
                    or (s['event'] == 'operation'
                        and str(s['name']).startswith('project.sql')))]
    sql_ops.sort(key=lambda r: -r['ms'])
    values = [r['ms'] for r in rows]
    return {
        'sql_events': rows[:15],
        'project_sql_operations': sql_ops[:15],
        'duration_distribution': distribution(
            values, '本 session 全部 sql.execute/sql.fetch 事件的 duration_ms'),
        'nesting_note': ('project.sql_read 与其内部的 project.sql.execute 存在包含关系'
                         '（例如 project.sql_read ≈ 269,835 ms 内含 embedded_glb '
                         'execute ≈ 262,103 ms），父子耗时不叠加。'),
    }


def catalog_report(sess):
    cell = sess.catalog.get('browser_batches', {})
    batch_rows = cell.get('batch_rows', [])
    reasons = collections.Counter()
    partial_true = 0
    not_ready_seq = []
    timeline = []
    last = None
    for event in sess.scope_events:
        key = '%s/%s/partial=%s' % (event['result'], event['reason'], event['partial'])
        reasons[key] += 1
        if event['result'] == 'miss' and event['reason'] == 'not_ready' and event['partial'] is True:
            partial_true += 1
            not_ready_seq.append(event['seq'])
        state = (event['result'], event['reason'], event['partial'])
        if state != last:
            timeline.append({'seq': event['seq'], **{
                'result': event['result'], 'reason': event['reason'],
                'partial': event['partial'], 'change': True}})
            last = state
        else:
            timeline.append({'seq': event['seq'], 'change': False})
    return {
        'browser_read_batches': cell.get('batches', 0),
        'browser_rows_total': int(cell.get('rows', 0)),
        'browser_pages_total': int(cell.get('pages', 0)),
        'batch_seq_range': [cell.get('first_seq'), cell.get('last_seq')],
        'batch_scopes': cell.get('scopes', {}),
        'batch_rows_distribution': distribution(
            [r[1] for r in batch_rows], '单批 catalog_browser_rows_read 的 rows'),
        'catalog_rows_read': sess.catalog.get('reads', []),
        'scope_event_counts': dict(reasons),
        'scope_not_ready_partial_true_count': partial_true,
        'scope_not_ready_partial_true_seqs': not_ready_seq,
        'scope_state_timeline': timeline,
        'interpretation_guard': (
            '出现过 196 批 / 100,000 行的 scope=all 全库摘要，'
            '所以**不能**把任何单次查询行数简单归因为「查询永远只有 100」；'
            '日志里同时存在 limit=512、rows=100000 的读取记录。'),
    }


def queue_report(sess):
    waits, unfinished, superseded = [], [], 0
    for task, slot in sess.queue_tasks.items():
        queued = slot.get('material_task_queued')
        started = slot.get('material_task_start')
        result = slot.get('material_task_result')
        finished = slot.get('material_task_finished')
        if queued and started and queued['monotonic'] is not None and started['monotonic'] is not None:
            waits.append((started['monotonic'] - queued['monotonic']) * 1000.0)
        if queued and not finished:
            unfinished.append({'task': task, 'seq': queued['seq'],
                               'note': 'queued 但没有 finished：生命周期未闭合，不算成功。'})
        if result and result.get('current') is False:
            superseded += 1
    pending = [d['pending'] for d in sess.image_decode if d['pending'] is not None]
    owners = [d['waiting_owners'] for d in sess.image_decode if d['waiting_owners'] is not None]
    cancelled_reads = [r for r in sess.catalog.get('reads', []) if r.get('cancelled')]
    return {
        'material_tasks_total': len(sess.queue_tasks),
        'material_task_kinds': collections.Counter(
            slot['material_task_queued']['kind']
            for slot in sess.queue_tasks.values() if slot.get('material_task_queued')),
        'queue_wait_distribution': distribution(
            waits, 'material_task 从 queued 到 start 的等待时间（毫秒），n=可配对任务数'),
        'queue_wait_max_ms': round(max(waits), 3) if waits else None,
        'tasks_without_finished': unfinished,
        'tasks_superseded_current_false': superseded,
        'image_decode_pending_max': max(pending) if pending else None,
        'image_decode_max_pending_reported': max(
            [d['max_pending'] for d in sess.image_decode if d['max_pending'] is not None],
            default=None),
        'image_decode_waiting_owners_max': max(owners) if owners else None,
        'cancelled_catalog_reads': cancelled_reads,
        'saturation_verdict': (
            '任务等待及解码队列峰值见上述实测分布；这些字段本身不足以判定持续饱和。'
            '无样本时不得推断队列健康或没有排队。'),
        'cancellation_waste_note': (
            '「取消浪费了多少字节/CPU」日志里没有字段，只能给出取消/失效的**计数**；'
            '不做字节级估算。'),
    }


def _d_count(value):
    """D counts are JSON nonnegative integers, never booleans/coerced floats."""
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _d_before(left, right):
    if left is None or right is None:
        return False
    a, b = _d_count(left.get('seq')), _d_count(right.get('seq'))
    if a is None or b is None or a >= b:
        return False
    ma, mb = _num(left.get('monotonic')), _num(right.get('monotonic'))
    return ma is None or mb is None or (math.isfinite(ma) and math.isfinite(mb) and ma <= mb)


def _d_baseline(fact):
    return ('baseline' in fact.get('event', '') or fact.get('baseline') is True
            or fact.get('baseline_only') is True
            or fact.get('evidence_origin') == 'trace_baseline')


def _d_counts(fact, layer, errors, warnings):
    """Validate lifetime logical counts without requiring filtered model equality."""
    if fact is None:
        errors.append(layer)
        return False
    if (fact.get('complete') is not True or fact.get('final') is not True
            or fact.get('cancelled') is not False or fact.get('superseded') is True):
        errors.append(layer + '_not_complete_terminal')
    logical_field = ('rows_read' if layer == 'read' and 'rows_read' in fact else 'logical_rows')
    logical = _d_count(fact.get(logical_field))
    if logical is None:
        errors.append(layer + '_logical_count_unknown_or_invalid')
    if layer == 'read' and 'rows_read' in fact and 'logical_rows' in fact:
        if _d_count(fact['logical_rows']) is None or fact['logical_rows'] != fact['rows_read']:
            errors.append('read_logical_alias_mismatch')
    unique = _d_count(fact.get('unique_rows'))
    if unique is None:
        if fact.get('unique_rows') is not None:
            errors.append(layer + '_unique_count_invalid')
        elif fact.get('unique_rows_complete') is not False:
            errors.append(layer + '_unique_count_unknown')
        warnings.append(layer + '_unique_count_unknown')
    if unique is not None and logical is not None and unique > logical:
        errors.append(layer + '_unique_exceeds_logical')
    complete = unique is not None and fact.get('unique_rows_complete') is True
    if fact.get('unique_rows_basis') in ('lower_bound', 'not_collected', 'unknown'):
        complete = False
    if fact.get('id_budget_exhausted') is True:
        warnings.append(layer + '_id_budget_exhausted')
        complete = False
    if not complete:
        warnings.append(layer + '_unique_count_incomplete')
    return complete


def _d_projection(fact, scope):
    fact = fact or {}
    return dict(viewport_drawn=_d_count(fact.get('viewport_drawn')),
                current_revision_coverage=_d_count(fact.get('current_revision_coverage')),
                content_ready=_d_count(fact.get('content_ready')),
                count_units=fact.get('count_units'),
                coverage_scope=fact.get('coverage_scope', scope),
                counts_are_lower_bounds=fact.get('counts_are_lower_bounds'),
                incomplete=fact.get('incomplete'), evidence=fact.get('evidence'))


def presentation_evidence_report(sess):
    """Query provenance and view/frame evidence; never infer full presentation."""
    if not sess.presentation_evidence:
        return {
            'protocol': 'legacy', 'query_count': 0, 'queries': [],
            'unlinked_events': [], 'verdict': 'evidence_insufficient',
            'display_confirmed': None,
            'input_evidence_gap': bool(sess.input_evidence_gaps),
            'policy': '只有旧协议：读取量及跨帧模型规模属于间接证据，不能证明完整呈现。',
        }
    queries, unlinked = collections.OrderedDict(), []
    facts = sorted(sess.presentation_evidence, key=lambda item: (
        _d_count(item.get('seq')) is None, _d_count(item.get('seq')) or 0))
    provenance_fields = ('read_id', 'source_query_id', 'read_source_revision')
    for fact in facts:
        identity_missing = [key for key in (
            'query_id', 'session_id', 'scope', 'source_revision',
            'presentation_schema_version') if _is_unknown(fact.get(key)) or fact.get(key) == '']
        if identity_missing or type(fact.get('presentation_schema_version')) is not int or fact['presentation_schema_version'] != 1:
            unlinked.append({'query_id': fact.get('query_id'), 'event': fact.get('event'),
                             'missing_fields': identity_missing,
                             'reason': 'missing_identity_or_unsupported_protocol',
                             'evidence': fact['evidence']})
            continue
        query_id = fact['query_id']
        cell = queries.setdefault(query_id, {
            'query_id': query_id, 'session_id': fact['session_id'],
            'scope': fact['scope'], 'source_revision': fact['source_revision'],
            'events': collections.defaultdict(list), 'identity_conflicts': [],
            'post_cancel_events': [], 'cancelled': False, 'failed': False,
            'deferred': False, 'terminal_event': None, 'terminal_reason': None,
            'terminal_seq': None,
            'provenance': {}, 'accepted': [], 'baselines': [],
        })
        mismatch = [key for key in ('session_id', 'scope', 'source_revision') if fact.get(key) != cell[key]]
        # Query protocol identity is frozen independently of transport identity.
        if 'trace_session_id' in fact and fact['trace_session_id'] != sess.session_id:
            mismatch.append('trace_session_id')
        for key in provenance_fields:
            if key in fact:
                if _is_unknown(fact[key]) or fact[key] == '':
                    mismatch.append(key + '_unknown')
                elif key in cell['provenance'] and fact[key] != cell['provenance'][key]:
                    mismatch.append(key)
                else:
                    cell['provenance'][key] = fact[key]
        if mismatch:
            cell['identity_conflicts'].append({'fields': mismatch, 'evidence': fact['evidence']})
            continue
        kind = fact['event']
        if (cell['cancelled'] or fact.get('cancelled') is True) and kind != 'catalog_query_cancelled':
            cell['post_cancel_events'].append(fact['evidence'])
            cell['cancelled'] = True
            continue
        cell['accepted'].append(fact)
        if _d_baseline(fact):
            cell['baselines'].append(fact)
        else:
            cell['events'][kind].append(fact)
        if kind == 'catalog_query_cancelled':
            cell['cancelled'] = True
        if kind == 'catalog_query_failed':
            cell['failed'] = True
        if kind == 'catalog_query_deferred':
            cell['deferred'] = True
        if kind in TERMINAL_QUERY_EVENTS:
            # 终态只认一次：同一查询若有多个终态事件，取最后一个（按序号）。
            cell['terminal_event'] = kind
            cell['terminal_reason'] = fact.get('reason')
            cell['terminal_seq'] = fact.get('seq')
    rows = []
    for cell in queries.values():
        events, missing, warnings = cell['events'], [], []
        last = lambda kind: (events[kind][-1:] or [None])[0]
        read, model = last('catalog_rows_read_evidence'), last('asset_model_published')
        delivered = events['catalog_rows_delivered']
        consumers = [event for event in delivered if
                     (event.get('delivery_stage') or event.get('layer') or event.get('stage')) == 'consumer_consumed']
        last_consumer = (consumers[-1:] or [None])[0]
        # 被取消的查询，最后一次交付尝试本身就是被取消截断的（complete=False），
        # 它不能反过来否定消费者**已经完整消费**的那一次交付。权威的交付证据
        # 是消费者真正完成的那一次；没有完整交付时仍退回最后一次，让缺失可见。
        complete_consumers = [event for event in consumers
                               if event.get('complete') is True
                               and event.get('final') is True
                               and event.get('cancelled') is not True
                               and event.get('superseded') is not True]
        consumer = (complete_consumers[-1:] or consumers[-1:] or [None])[0]
        read_unique = _d_counts(read, 'read', missing, warnings)
        consumer_unique = _d_counts(consumer, 'delivered', missing, warnings)
        if model is None:
            missing.append('model')
        elif (model.get('final') is not True or model.get('complete') is not True
              or model.get('cancelled') is not False or model.get('superseded') is True):
            missing.append('model_not_complete_terminal')
        if model is not None and _d_count(model.get('model_rows')) is None:
            missing.append('model_rows_unknown_or_invalid')
        model_unique = False
        if model is not None:
            unique_fields = [key for key in ('model_unique_ids', 'model_unique_rows')
                             if key in model or key + '_complete' in model]
            if not unique_fields:
                warnings.append('model_unique_count_unknown')
            for key in unique_fields:
                value = _d_count(model.get(key))
                complete = value is not None and model.get(key + '_complete') is True
                model_unique = complete if key == unique_fields[0] else model_unique and complete
                if model.get(key) is not None and value is None:
                    missing.append('model_unique_count_invalid')
                if value is not None and _d_count(model.get('model_rows')) is not None and value > model['model_rows']:
                    missing.append('model_unique_exceeds_rows')
            if len(unique_fields) == 2 and model.get(unique_fields[0]) != model.get(unique_fields[1]):
                missing.append('model_unique_alias_mismatch')
            if model.get('id_budget_exhausted') is True:
                model_unique = False
                warnings.append('model_id_budget_exhausted')
            if not model_unique:
                warnings.append('model_unique_count_incomplete')
        if not cell['provenance']:
            warnings.append('legacy_read_provenance_unavailable')
        else:
            for fact in cell['accepted']:
                if fact['event'] in TERMINAL_QUERY_EVENTS:
                    continue
                absent = [key for key in provenance_fields if key not in fact]
                if absent:
                    missing.append('read_provenance_missing')
                    cell['identity_conflicts'].append({'fields': absent, 'evidence': fact['evidence']})
        baseline_models = [fact for fact in cell['baselines']
                           if fact.get('complete') is True and fact.get('cancelled') is False
                           and fact.get('superseded') is not True
                           and (fact.get('model_published') is True
                                or (fact['event'] == 'asset_model_published'
                                    and fact.get('final') is True
                                    and fact.get('baseline_rows_are_new_work') is False))
                           and _d_count(fact.get('model_rows')) is not None
                           and all(not _is_unknown(fact.get(key)) and fact.get(key) != ''
                                   for key in provenance_fields)]
        if cell['baselines']:
            warnings.append('trace_baseline_linked_not_actual_read')
        # 「该次发布的模型对本次查询是完整的」：存在该 query_id 的
        # asset_model_published 且 complete=True（未被取消/取代）。
        # 这条判定不再受「之后又发起了新搜索」的 cancel_or_switch 影响。
        complete_publications = [fact for fact in events['asset_model_published']
                                 if fact.get('complete') is True
                                 and fact.get('cancelled') is not True
                                 and fact.get('superseded') is not True]
        published_complete = bool(complete_publications)
        publication_seq = None
        if complete_publications:
            seqs = [_d_count(fact.get('seq')) for fact in complete_publications]
            seqs = [seq for seq in seqs if seq is not None]
            publication_seq = max(seqs) if seqs else None
        cancel_fact = (events['catalog_query_cancelled'][-1:] or [None])[0]
        cancel_seq = _d_count(cancel_fact.get('seq')) if cancel_fact else None
        cancel_reason = cancel_fact.get('reason') if cancel_fact else None
        # 取消发生在完整发布**之后** ⇒ 用户已经看到结果，之后的取消只是
        # 「用户又输入了新搜索词」，属于取代而不是「什么都没呈现」。
        superseded = bool(cell['cancelled'] and published_complete
                          and publication_seq is not None and cancel_seq is not None
                          and cancel_seq > publication_seq)
        superseded_reason = cancel_reason if superseded else None
        superseded_seq = cancel_seq if superseded else None
        # Latest snapshots are selected within a stable (view, frame) pair.
        frames = collections.OrderedDict()
        for fact in cell['accepted']:
            if fact['event'] not in ('asset_paint_frame', 'asset_visibility_snapshot'):
                continue
            view = fact.get('view_id', 'defaultview')
            if _is_unknown(view) or view == '':
                missing.append('view_id_unknown')
                continue
            frame_id = fact.get('frame_id')
            if _is_unknown(frame_id) or frame_id == '':
                missing.append('paint_frame_id_unknown')
                continue
            frame = frames.setdefault((view, frame_id), dict(view_id=view, frame_id=frame_id, paint=None, visibility=None))
            frame['paint' if fact['event'] == 'asset_paint_frame' else 'visibility'] = fact
        frame_rows = []
        for frame in frames.values():
            paint, visibility = frame['paint'], frame['visibility']
            errors, frame_warnings = [], []
            if paint is None:
                errors.append('paint')
            if visibility is None:
                errors.append('visibility')
            closed = _d_before(paint, visibility)
            if paint is not None and visibility is not None and not closed:
                errors.append('visibility_before_paint_or_order_unknown')
            publications = [fact for fact in events['asset_model_published'] + baseline_models if _d_before(fact, paint)]
            publication = max(publications, key=lambda fact: fact['seq']) if publications else None
            if publication is None:
                errors.append('paint_before_model_publish_or_baseline')
            elif (publication.get('complete') is not True or publication.get('cancelled') is not False
                  or publication.get('superseded') is True
                  or (not _d_baseline(publication) and publication.get('final') is not True)):
                errors.append('frame_model_not_complete_terminal')
            for label, fact in (('paint', paint), ('visibility', visibility)):
                if fact is None:
                    continue
                if fact.get('incomplete') is True or fact.get('complete') is not True:
                    frame_warnings.append(label + '_evidence_incomplete')
                if fact.get('id_budget_exhausted') is True:
                    frame_warnings.append(label + '_id_budget_exhausted')
                if fact.get('ever_drawn_unique_ids_complete') is False:
                    frame_warnings.append(label + '_query_lifetime_unique_count_incomplete')
                if _is_unknown(fact.get('count_units')):
                    frame_warnings.append(label + '_count_units_unknown')
                if _is_unknown(fact.get('coverage_scope')):
                    frame_warnings.append(label + '_coverage_scope_unknown')
                if fact.get('unknown') is None and 'unknown' in fact:
                    frame_warnings.append(label + '_unknown_coverage')
                elif _d_count(fact.get('unknown')) not in (None, 0):
                    frame_warnings.append(label + '_unknown_coverage')
                for field in ('viewport_drawn', 'current_revision_coverage', 'content_ready',
                              'placeholder', 'fallback', 'unknown', 'model_rows'):
                    if field in fact and fact[field] is not None and _d_count(fact[field]) is None:
                        errors.append(label + '_' + field + '_invalid')
                if label == 'paint' and any(_d_count(fact.get(field)) is None for field in ('model_rows', 'viewport_drawn')):
                    errors.append('paint_counts_unknown_or_invalid')
                if label == 'visibility' and any(_d_count(fact.get(field)) is None for field in ('viewport_drawn', 'current_revision_coverage', 'content_ready')):
                    frame_warnings.append('visibility_current_counts_unknown')
                if publication is not None and fact.get('model_rows') != publication.get('model_rows'):
                    errors.append(label + '_model_rows_mismatch')
                if label == 'visibility':
                    if fact.get('coverage_scope', 'current_viewport') != 'current_viewport':
                        errors.append('visibility_not_current_viewport')
                    if fact.get('current_viewport_exact') is False or fact.get('counts_are_lower_bounds') is True:
                        frame_warnings.append('visibility_current_viewport_not_exact')
                    units = fact.get('count_units')
                    if units is not None and units not in ('unique_model_rows', 'unique_asset_ids', 'asset_ids'):
                        errors.append('visibility_not_unique_count_units')
            frame_rows.append(dict(frame, closed=closed, model_publication=publication,
                                   evidence_valid=not errors,
                                   evidence_complete=closed and not errors and not frame_warnings,
                                   missing_or_invalid=errors, warnings=frame_warnings,
                                   paint_clip=_d_projection(paint, 'paint_clip'),
                                   current_viewport=_d_projection(visibility, 'current_viewport')))
        frame_order = lambda frame: _d_count((frame['paint'] or {}).get('seq')) or -1
        started = [frame for frame in frame_rows if frame['paint'] is not None]
        latest = max(started, key=frame_order) if started else None
        # 「最近一次真正画出内容的帧」：最后一帧 viewport_drawn > 0 的帧。
        # 「最近一次开始的帧」可能是空帧（最后一帧什么都没画），用它当依据
        # 会让同一批查询互相矛盾（0 / 1189 / 127）。两者都保留，口径可见。
        content_frames = [frame for frame in started
                          if (_d_count((frame['paint'] or {}).get('viewport_drawn')) or 0) > 0]
        latest_content = max(content_frames, key=frame_order) if content_frames else None
        complete_frames = [frame for frame in started if frame['evidence_complete']]
        recent = max(complete_frames, key=frame_order) if complete_frames else None
        views = []
        for view in dict.fromkeys(frame['view_id'] for frame in frame_rows):
            candidates = [frame for frame in started if frame['view_id'] == view]
            view_latest = max(candidates, key=frame_order) if candidates else None
            completed = [frame for frame in complete_frames if frame['view_id'] == view]
            views.append(dict(view_id=view, latest_started_frame=view_latest,
                              latest_started_frame_closed=bool(view_latest and view_latest['closed']),
                              most_recent_complete_frame=max(completed, key=frame_order) if completed else None))
            if view_latest is None or not view_latest['closed']:
                missing.append('latest_frame_pending:' + str(view))
            if view_latest:
                missing.extend(view_latest['missing_or_invalid'])
                warnings.extend(view_latest['warnings'])
        if latest_content is not None:
            missing.extend(latest_content['missing_or_invalid'])
            warnings.extend(latest_content['warnings'])
        paint = latest_content['paint'] if latest_content else None
        visibility = latest_content['visibility'] if latest_content else None
        observed = dict(read=read is not None, delivered=consumer is not None,
                        model=model is not None, paint=paint is not None, visibility=visibility is not None)
        if paint is None:
            missing.append('paint' if not started else 'paint_content_frame_missing')
        if visibility is None:
            missing.append('visibility' if not started else 'visibility_content_frame_missing')
        if any(frame['paint'] is None for frame in frame_rows):
            missing.append('visibility_frame_identity_mismatch')
        if cell['identity_conflicts']:
            missing.append('identity_conflict')
        if any(item['query_id'] == cell['query_id'] for item in unlinked):
            missing.append('unlinked_query_evidence')
        if cell['post_cancel_events']:
            missing.append('post_cancel_evidence_rejected')
        # 输入缺口只影响**落在缺口区间内**的查询证据：缺口在别处（早于/晚于
        # 本查询的记录区间）不能反过来否定本查询已经观测到的五层证据。
        # 没有区间信息的缺口（IO/解析错误、序号顺序未知、内容冲突）保守处理。
        query_seqs = [_d_count(fact.get('seq')) for fact in cell['accepted']]
        query_seqs = [seq for seq in query_seqs if seq is not None]
        query_seq_range = [min(query_seqs), max(query_seqs)] if query_seqs else None

        def _gap_touches_query(gap):
            ranges = gap.get('missing_sequence_ranges')
            if not ranges:
                return True
            if query_seq_range is None:
                return True
            low, high = query_seq_range
            for item in ranges:
                if (isinstance(item, list) and len(item) == 2
                        and isinstance(item[0], int) and isinstance(item[1], int)
                        and item[0] <= high and item[1] >= low):
                    return True
            return False

        overlapping_gaps = [gap for gap in sess.input_evidence_gaps if _gap_touches_query(gap)]
        if overlapping_gaps:
            missing.append('input_evidence_gap')
        elif sess.input_evidence_gaps:
            missing.append('input_evidence_gap_outside_query_evidence')
        if sess.dropped_events or sess.trace_incomplete or sess.seq_gap_count or any(sess.dropped_by_kind.values()):
            missing.append('trace_loss_reported')
        # 终态缺失是查询生命周期的事实，不是呈现证据本身的缺陷：它必须被
        # 显式记录，但不能反过来否定已经观测到的五层证据。
        terminal_state = {'catalog_query_cancelled': 'cancelled',
                          'catalog_query_failed': 'failed',
                          'catalog_query_deferred': 'deferred'}.get(
                              cell['terminal_event'], 'missing')
        if terminal_state == 'missing':
            missing.append('terminal_state_missing')
        # 呈现证据有效 = 五层都被观测到 且 模型已发布且 complete=True 且 查询未失败。
        # 之后的 cancel_or_switch 不再抹掉呈现证据：它被单独记为 superseded。
        blocking = [item for item in missing
                    if item not in ('input_evidence_gap_outside_query_evidence',
                                    'terminal_state_missing')]
        evidence_valid = (not blocking and published_complete
                          and not cell['failed'] and all(observed.values()))
        viewport_unique = bool(views) and all(
            view['latest_started_frame'] is not None
            and view['latest_started_frame']['closed']
            and view['latest_started_frame']['visibility'].get('current_viewport_exact') is True
            and view['latest_started_frame']['visibility'].get('count_units') in ('unique_model_rows', 'unique_asset_ids', 'asset_ids')
            and view['latest_started_frame']['visibility'].get('coverage_scope') == 'current_viewport'
            and view['latest_started_frame']['visibility'].get('incomplete') is not True
            and view['latest_started_frame']['visibility'].get('id_budget_exhausted') is not True
            and all(_d_count(view['latest_started_frame']['visibility'].get(field)) is not None
                    for field in ('viewport_drawn', 'current_revision_coverage', 'content_ready'))
            for view in views)
        # 分层唯一计数是否完整（既有口径，逐层如实记录）。
        census_complete = bool(read_unique and consumer_unique and model_unique
                               and viewport_unique)
        # 顶层口径与 evidence_valid 自洽：
        # 取消会截断读取/交付链，被取消的查询其分层唯一计数必然不完整，
        # 不能据此否定「已经完整发布的模型」；所以被取消的查询按发布规则
        # 判定（已发布且 complete ⇒ 该次发布的模型对该查询是完整的；
        # 被取消且未发布 ⇒ 不完整），未被取消的查询沿用分层普查口径。
        if cell['cancelled']:
            unique_complete = bool(published_complete and evidence_valid)
            unique_basis = 'published_model_complete_after_cancel'
        else:
            unique_complete = bool(census_complete and evidence_valid)
            unique_basis = 'per_layer_unique_census'
        all_layers = all(observed.values()) and evidence_valid
        # This compatibility flag describes valid observations, not complete ID
        # census or complete presentation. Those are independent below.
        current = _d_projection(visibility, 'current_viewport')
        layers = dict(read=[read], delivered=delivered, consumer_final=[consumer],
                      install_progress=[last('asset_model_install_progress')], model=model,
                      paint=paint, visibility=visibility, baselines=cell['baselines'],
                      delivered_last_consumer=last_consumer)

        def _frame_summary(frame):
            if not frame:
                return None
            paint_fact = frame.get('paint') or {}
            visibility_fact = frame.get('visibility') or {}
            return {
                'frame_id': frame.get('frame_id'), 'view_id': frame.get('view_id'),
                'paint_sequence': paint_fact.get('seq'),
                'visibility_sequence': visibility_fact.get('seq'),
                'capture_id': paint_fact.get('capture_id') or visibility_fact.get('capture_id'),
                'paint_viewport_drawn': _d_count(paint_fact.get('viewport_drawn')),
                'visibility_viewport_drawn': _d_count(visibility_fact.get('viewport_drawn')),
                'closed': frame.get('closed'),
            }

        frame_selection = {
            'rule': ('latest_content_frame=最近一次真正画出内容的帧（paint.viewport_drawn>0）；'
                     'latest_started_frame=最近一次开始的帧（可能是空帧）'),
            'latest_started_frame': _frame_summary(latest),
            'latest_content_frame': _frame_summary(latest_content),
            'content_frame_found': latest_content is not None,
            'content_frame_count': len(content_frames),
            'started_frame_count': len(started),
        }
        terminal_state_evidence = None
        if terminal_state == 'missing':
            query_last = cell['accepted'][-1] if cell['accepted'] else None
            query_last_seq = _d_count(query_last.get('seq')) if query_last else None
            session_last_seq = sess.last_record_sequence
            terminal_state_evidence = {
                'query_last_sequence': query_last_seq,
                'query_last_event': query_last.get('event') if query_last else None,
                'session_last_sequence': session_last_seq,
                'session_last_event': sess.last_record_event,
                'query_last_is_session_tail': bool(
                    query_last_seq is not None and session_last_seq is not None
                    and 0 <= session_last_seq - query_last_seq <= 1),
                'note': ('该查询没有任何终态事件（cancelled/failed/deferred）；'
                         '它的最后一条记录紧邻会话最后一条记录，'
                         '说明会话在查询仍在飞行时结束。'),
            }
        rows.append(dict(
            query_id=cell['query_id'], session_id=cell['session_id'], trace_session_id=sess.session_id,
            scope=cell['scope'], source_revision=cell['source_revision'], read_provenance=cell['provenance'],
            cancelled=cell['cancelled'], failed=cell['failed'], deferred=cell['deferred'],
            terminal_state=terminal_state, terminal_event=cell['terminal_event'],
            terminal_reason=cell['terminal_reason'], terminal_seq=cell['terminal_seq'],
            terminal_state_evidence=terminal_state_evidence,
            superseded=superseded, superseded_reason=superseded_reason,
            superseded_seq=superseded_seq, cancel_reason=cancel_reason,
            presented_before_supersede=bool(superseded and evidence_valid),
            model_publication_complete=published_complete,
            layers=layers,
            layers_observed=observed, evidence_valid=evidence_valid,
            unique_count_complete=unique_complete,
            unique_count_complete_basis=unique_basis,
            unique_count_complete_census_complete=census_complete,
            unique_count_complete_by_layer=dict(read=read_unique, delivered=consumer_unique,
                                                model=model_unique, current_viewport=viewport_unique),
            all_layers_observed=all_layers,
            missing_or_invalid=list(dict.fromkeys(missing)), warnings=list(dict.fromkeys(warnings)),
            identity_conflicts=cell['identity_conflicts'], post_cancel_events=cell['post_cancel_events'],
            input_evidence_gap=bool(sess.input_evidence_gaps), input_evidence_gaps=sess.input_evidence_gaps,
            baseline_linked=bool(baseline_models), frames=frame_rows, views=views,
            latest_started_frame=latest, latest_started_frame_closed=bool(latest and latest['closed']),
            latest_content_frame=latest_content, frame_selection=frame_selection,
            most_recent_complete_frame=recent,
            model_rows=_d_count((model or {}).get('model_rows')),
            viewport_drawn=current['viewport_drawn'], current_revision_coverage=current['current_revision_coverage'],
            content_ready=current['content_ready'], current_viewport=current,
            paint_clip=_d_projection(paint, 'paint_clip'),
            generated=events['asset_resource_generated'][-1:] or [None],
            cache_installed=events['asset_cache_installed'][-1:] or [None], display_confirmed=None,
            verdict=('failed' if cell['failed'] else
                     'presented_then_superseded' if superseded and evidence_valid else
                     'published_then_cancelled_before_paint' if superseded else
                     'cancelled' if cell['cancelled'] else
                     'evidence_insufficient' if not all_layers else
                     'layers_observed_evidence_degraded' if warnings or not unique_complete else
                     'layers_observed_not_full_presentation')))
    return {
        'protocol': 'presentation-v1', 'query_count': len(rows), 'queries': rows,
        'unlinked_events': unlinked, 'display_confirmed': None, 'verdict': 'layered_evidence',
        'input_evidence_gap': bool(sess.input_evidence_gaps),
        'policy': ('查询session身份冻结；trace_session_id若提供必须匹配物理trace session。'
                   '同query/scope/source_revision/read provenance逐层关联，按view_id/frame_id配对。'
                   'layers_observed、evidence_valid、unique_count_complete各自独立；预算/未知明确降级。'
                   '呈现证据有效=五层都被观测到且模型已发布(complete=True)且查询未失败；'
                   '发布之后的cancel_or_switch不再抹掉呈现证据，单独记为superseded/superseded_reason；'
                   '发布之前就被取消的查询仍然无效，原始reason见cancel_reason/terminal_reason。'
                   '顶层计数仅来自**最近一次真正画出内容的帧**的current visibility；'
                   'paint_clip分栏保留单位/范围；latest_started_frame与latest_content_frame并列可见。'
                   'unique_count_complete对被取消的查询按发布规则判定（已发布且complete⇒完整，'
                   '被取消且未发布⇒不完整），未被取消的查询沿用分层普查口径。'
                   'terminal_state显式区分cancelled/failed/deferred/missing；missing表示会话在查询仍在飞行时结束。'
                   '最新未闭合不退回旧帧；baseline链接不是新实际读取；累计快照不相加。'
                   '任何记录都不能证明全部素材完整呈现或显示器最终呈现。'),
    }


def count_layers_report(sess):
    logical = None
    for layer in sess.count_layers:
        if layer['layer'] == 'browser_metrics':
            continue
    batches = sess.catalog.get('browser_batches', {})
    logical = batches.get('rows')
    prepared = [l for l in sess.count_layers if l['layer'] == 'model_prepared']
    partial = [l for l in sess.count_layers if l['layer'] == 'partial_baseline']
    cards_model = [l for l in sess.count_layers if l['layer'] == 'cards_model']
    painted = [l['value'] for l in sess.count_layers
               if l['layer'] == 'painted_cards' and l['value'] is not None]
    live = [l['value'] for l in sess.count_layers
            if l['layer'] == 'live_assets' and l['value'] is not None]
    sql_rows = [l for l in sess.count_layers if l['layer'] == 'sql_result_rows']
    return {
        'logical_rows_read': int(logical) if logical is not None else None,
        'logical_evidence_seq_range': batches.get('first_seq') and [
            batches.get('first_seq'), batches.get('last_seq')],
        'model_prepared': prepared[-3:],
        'partial_baseline_items': partial[-1:],
        'cards_model_records': cards_model[-2:],
        'painted_cards_max': max(painted) if painted else None,
        'painted_cards_distribution': distribution(
            painted, 'performance 采样点的 asset_cards_painted 数值'),
        'live_assets_distribution': distribution(
            live, 'performance 采样点的 assets 数值（存活素材模型数）'),
        'sql_rows_returned': [{'seq': l['seq'], 'rows': l['value'], 'query': l['query']}
                              for l in sql_rows],
        'interpretation': (
            '旧读取量、模型数和paint汇总不能互相替代。asset_cards_painted属于'
            '旧采样计数，assets.paint_frame.cards是跨帧模型行数之和，不是唯一实际'
            '绘制素材数；差异不能证明漏图，也不能证明呈现完整。新协议的直接'
            '交付、模型、视口绘制与可见性请查presentation_evidence。'),
    }


def sampling_coverage(sess):
    by_event = collections.defaultdict(list)
    for sample in sess.samples:
        by_event[sample['event']].append(sample['monotonic'])
    out = {}
    for event, monos in by_event.items():
        monos = [m for m in monos if m is not None]
        gaps = [round((b - a) * 1000.0, 3) for a, b in zip(monos, monos[1:])]
        span_ms = round((monos[-1] - monos[0]) * 1000.0, 3) if len(monos) > 1 else 0.0
        out[event] = {
            'samples': len(monos),
            'first_seq': None,
            'interval_ms': distribution(gaps, '%s 采样间隔（毫秒）' % event),
            'covered_span_ms': span_ms,
            'coverage_of_session_pct': (
                round(100.0 * span_ms / ((sess.last_mono - sess.first_mono) * 1000.0), 2)
                if sess.first_mono is not None and sess.last_mono is not None
                and sess.last_mono > sess.first_mono else None),
        }
    return {
        'per_sampler': out,
        'caveat': ('RSS/CPU 是**周期性采样**读到的瞬时值，真实峰值若恰好落在两个'
                   '采样点之间不会被记录；报告里的「峰值」只能读作「采样到的最大值」。'),
    }


# --------------------------------------------------------------------------
# schema v2 新事件族
# --------------------------------------------------------------------------
QUEUE_DEPTH_FIELDS = ('pending', 'running', 'ready', 'limit', 'atomic_in_progress',
                      'cancelled_remaining_work', 'shared_tasks')


def _cancel_after_start_note(count):
    """What the ``cancel_after_start`` counter really says.

    The counter is the number of tasks that were cancelled **after** they had
    started.  The story it tells depends on the number: a zero means no work
    was ever caught mid-flight, and a missing counter means the log never
    says.  Printing the "it had already started, the cancel cannot stop it"
    story for a zero would claim work that never happened.
    """
    if isinstance(count, (int, float)) and not isinstance(count, bool):
        if count > 0:
            return ('cancel_after_start=%s：**这部分工作已经开始跑了，取消打不断它**，'
                    '只能把结果丢掉。所以它不是「即时取消」，代价是那段 CPU/IO 照付。'
                    % count)
        return ('cancel_after_start=0：**没有**工作在开始之后才被取消 —— '
                '取消都发生在开始之前，没有已经开跑又停不下来的工作。')
    return ('cancel_after_start 未记录：日志里没有这个计数，'
            '**无法判断**有没有工作在开始之后被取消。')


def queue_rollups_report(sess):
    """``queue_rollup``：每个工作队列一行，报「给了多少 / 起了多少 / 装上多少 /
    **扔掉多少**」，外加等待 vs 服务时间的拆分、峰值深度与饱和证据。

    队列计数是**累计**的，所以每个分类取最后一次快照（并对累计量取最大值，
    防止某个字段在生产端不是单调的）。
    """
    by_category = collections.OrderedDict()
    for rollup in sess.queue_rollups:
        category = rollup.get('category') or '(unknown)'
        cell = by_category.get(category)
        if cell is None:
            cell = by_category[category] = {
                'category': category, 'rollups': 0,
                'counters': {}, 'first_seq': rollup['seq'],
                'last_seq': rollup['seq'], 'evidence': rollup['evidence'],
                'gauge': {}, 'cumulative': {}, 'owner': {}, 'reset_reasons': [],
            }
        cell['rollups'] += 1
        cell['last_seq'] = rollup['seq']
        cell['evidence'] = rollup['evidence']
        for name in QUEUE_COUNTERS:
            value = rollup['counters'].get(name)
            if value is None:
                continue
            seen = cell['counters'].get(name)
            cell['counters'][name] = value if seen is None else max(seen, value)
        for name in QUEUE_DEPTH_FIELDS:
            if rollup.get(name) is not None:
                cell['gauge'][name] = rollup[name]
        cell['gauge']['oldest_age_ms'] = rollup.get('oldest_age_ms')
        for name in ('oldest_age_status', 'oldest_task_id', 'busy_period_ms',
                     'busy_period_status', 'peak_scope'):
            cell['gauge'][name] = rollup.get(name)
        if rollup.get('owner_project_token') is not None:
            cell['owner'] = {
                'project_token': rollup.get('owner_project_token'),
                'open_epoch': rollup.get('owner_open_epoch'),
                'generation': rollup.get('owner_generation'),
            }
        reason = rollup.get('reset_reason')
        if reason and reason not in cell['reset_reasons']:
            cell['reset_reasons'].append(reason)
        for name in ('peak_pending', 'peak_running',
                     'wait_ms_total', 'wait_ms_max', 'wait_samples',
                     'service_ms_total', 'service_ms_max', 'service_samples'):
            value = rollup.get(name)
            if value is None:
                continue
            seen = cell['cumulative'].get(name)
            cell['cumulative'][name] = value if seen is None else max(seen, value)

    rows = []
    for category, cell in by_category.items():
        counters = cell['counters']
        gauge = cell['gauge']
        cumulative = cell['cumulative']
        # ``cancelled`` already *contains* ``cancel_after_start`` (the
        # producer counts every cancel and marks the subset that had
        # already started). Summing the two counts one cancel twice.
        # ``coalesced`` and ``retried`` are **not** drops: the work ran, or
        # will run, under another request. Grouping them with the drops
        # overstates how much was lost.
        cancelled_total = counters.get('cancelled')
        cancel_after_start = counters.get('cancel_after_start')
        stale_discarded = counters.get('stale_discarded')
        rejected = counters.get('rejected')
        coalesced = counters.get('coalesced')
        retried = counters.get('retried')
        cancel_never_ran = (
            cancelled_total - cancel_after_start
            if isinstance(cancelled_total, (int, float))
            and isinstance(cancel_after_start, (int, float)) else None)

        def _known_sum(*parts):
            """Sum only when every part is known; otherwise stay unknown."""
            if not all(isinstance(part, (int, float)) for part in parts):
                return None
            return sum(parts)

        discarded = {
            'cancelled': cancelled_total,
            'cancel_after_start': cancel_after_start,
            'stale_discarded': stale_discarded,
            'rejected': rejected,
            'coalesced': coalesced,
            'retried': retried,
        }
        offered = counters.get('submitted')
        started = counters.get('started')
        installed = counters.get('installed')
        limit = gauge.get('limit')
        peak_pending = cumulative.get('peak_pending')
        at_limit = None
        if limit is not None and peak_pending is not None:
            at_limit = peak_pending >= limit
        wait_total = cumulative.get('wait_ms_total')
        service_total = cumulative.get('service_ms_total')
        wait_share = None
        if (wait_total is not None and service_total is not None
                and (wait_total + service_total) > 0):
            wait_share = round(100.0 * wait_total / (wait_total + service_total), 2)
        cancel_after_start = counters.get('cancel_after_start')
        atomic_in_progress = gauge.get('atomic_in_progress')
        cancelled_remaining = gauge.get('cancelled_remaining_work')

        if at_limit is True:
            saturation = ('**曾触顶**：peak_pending=%s 触到 limit=%s —— 历史上到过上限。'
                          '这只证明**曾触顶**，不能证明**持续饱和**：'
                          '持续饱和需要同一段时间里反复处在上限，'
                          '而 peak_pending 只是历史最大值这一个数。'
                          % (peak_pending, limit))
        elif at_limit is False:
            saturation = ('未见触顶：peak_pending=%s，limit=%s（用到 %s%%）。'
                          '峰值没有到上限，说明**没有观测到**排队触顶。'
                          % (peak_pending, limit,
                             round(100.0 * peak_pending / limit, 1) if limit else '—'))
        else:
            saturation = ('无法判断是否触顶：日志里没有 limit 或 peak_pending，'
                          '**不能**断言队列没饱和。')
        if atomic_in_progress:
            saturation += (' 另有 %s 个任务正在执行且无法被打断（atomic_in_progress）。'
                           % atomic_in_progress)
        if cancelled_remaining:
            saturation += (' 取消时还剩 %s 份工作没跑（cancelled_remaining_work）。'
                           % cancelled_remaining)

        rows.append({
            'category': category,
            'rollups': cell['rollups'],
            'first_seq': cell['first_seq'],
            'last_seq': cell['last_seq'],
            'counters': counters,
            'offered': offered,
            'started': started,
            'completed': counters.get('completed'),
            'installed': installed,
            'discarded': discarded,
            'discarded_total': _known_sum(cancelled_total, stale_discarded),
            'discarded_breakdown': {
                'never_ran': {'cancelled_never_ran': cancel_never_ran,
                              'rejected': rejected},
                'ran_but_result_dropped': {'cancel_after_start': cancel_after_start,
                                           'stale_discarded': stale_discarded},
                'not_a_drop': {'coalesced': coalesced, 'retried': retried},
                'note': ('cancelled 已经包含 cancel_after_start，不能再相加；'
                         'coalesced/retried 是「并入/重试」，不是丢图；'
                         'rejected 是队列没接，不是丢图。'),
            },
            'failed': counters.get('failed'),
            'queue_wait': {
                'total_ms': wait_total, 'max_ms': cumulative.get('wait_ms_max'),
                'samples': cumulative.get('wait_samples'),
                'note': ('wait_ms_total 是**每个任务排队等待时间之和**（样本数 = '
                         'wait_samples），不是墙钟；并发任务各记一次，'
                         '不能当成这段时间的墙钟长度。'),
            },
            'service': {
                'total_ms': service_total,
                'max_ms': cumulative.get('service_ms_max'),
                'samples': cumulative.get('service_samples'),
                'note': 'service_ms_total 是每个任务服务时间之和（样本数 = service_samples）。',
            },
            'wait_vs_service': {
                'wait_total_ms': wait_total,
                'service_total_ms': service_total,
                'wait_share_pct': wait_share,
                'note': ('排队等待占「等待+服务」的比例。两边都是累计值，'
                         '只说明时间花在哪儿，不说明墙钟被占了多久。'),
            },
            'depth': {
                'pending': gauge.get('pending'),
                'running': gauge.get('running'),
                'ready': gauge.get('ready'),
                'limit': limit,
                'oldest_age_ms': gauge.get('oldest_age_ms'),
                'oldest_age_status': gauge.get('oldest_age_status'),
                'oldest_task_id': gauge.get('oldest_task_id'),
                'busy_period_ms': gauge.get('busy_period_ms'),
                'busy_period_status': gauge.get('busy_period_status'),
                'note': ('pending/running/ready 是最后一次快照的瞬时值。'
                         'oldest_age_ms 是最老任务的真实年龄：没有逐任务身份时是'
                         '**未知**（null），不会用首次提交时间伪造。'
                         'busy_period_ms 是当前忙碌周期时长，与最老任务年龄'
                         '不是一回事，也不能互相替代。'),
            },
            'peak': {
                'pending': peak_pending,
                'running': cumulative.get('peak_running'),
                'scope': gauge.get('peak_scope'),
                'note': ('峰值来自队列自己的计数器，是**采样到的历史最大值**，'
                         '不代表当前深度，也不能填进「最后深度」。'),
            },
            'owner': {
                'project_token': cell['owner'].get('project_token'),
                'open_epoch': cell['owner'].get('open_epoch'),
                'generation': cell['owner'].get('generation'),
                'reset_reasons': cell['reset_reasons'],
                'note': ('累计计数属于该 owner。换工程或重开时，旧 owner 的尾数'
                         '会先落盘再重置，所以旧工程的工作量不会被算成新工程的。'),
            },
            'saturation': {
                'peak_pending': peak_pending,
                'limit': limit,
                'pending_at_or_over_limit': at_limit,
                'atomic_in_progress': atomic_in_progress,
                'cancelled_remaining_work': cancelled_remaining,
                'shared_tasks': gauge.get('shared_tasks'),
                'verdict': saturation,
            },
            'cancel_after_start_note': _cancel_after_start_note(cancel_after_start),
            'evidence': cell['evidence'],
        })

    return {
        'rollup_count': len(sess.queue_rollups),
        'categories': rows,
        'known_categories': list(QUEUE_CATEGORIES),
        'missing_categories': [name for name in QUEUE_CATEGORIES
                                if name not in by_category],
        'missing_categories_basis': 'not_observed_in_this_session',
        'policy': ('队列计数是累计值，报告取最后一次快照；未出现的分类是'
                   '**本次会话没有观测到这类队列**，不是「计数为 0」，'
                   '也不能据此推断它不存在。'),
    }


def hotpath_report(sess):
    """快路径采样：累计计数、窗口增量、阈值样本与采样策略。

    ``calls``/``total_ms``/``max_ms`` 是**自上次重置以来**的累计值，必须
    连同 owner 和 ``reset_reason`` 一起读；``window_*`` 是上一次 rollup
    以来的增量，跨窗口求和才是实际观察到的调用数。慢样本是独立观测事件，
    不是完整 span。
    """
    by_name = collections.OrderedDict()
    for rollup in sess.hotpath_rollups:
        name = rollup.get('name') or '(unknown)'
        cell = by_name.get(name)
        if cell is None:
            cell = by_name[name] = {
                'name': name, 'rollups': 0,
                'cumulative_calls_last': None,
                'cumulative_total_ms_last': None,
                'cumulative_max_ms_last': None,
                'window_calls_total': 0,
                'window_total_ms_total': 0.0,
                'window_max_ms_max': None,
                'failed_calls_last': None,
                'owners': [], 'reset_reasons': [], 'flush_reasons': [],
                'minimum_sample_ms': rollup.get('minimum_sample_ms'),
                'sampling_strategy': rollup.get('sampling_strategy'),
                'trace_modes': [],
                'first_seq': rollup['seq'], 'last_seq': rollup['seq'],
                'evidence': rollup['evidence'],
            }
        cell['rollups'] += 1
        cell['last_seq'] = rollup['seq']
        cell['evidence'] = rollup['evidence']
        cell['cumulative_calls_last'] = rollup.get('calls')
        cell['cumulative_total_ms_last'] = rollup.get('total_ms')
        cell['cumulative_max_ms_last'] = rollup.get('max_ms')
        cell['failed_calls_last'] = rollup.get('failed_calls')
        if isinstance(rollup.get('window_calls'), (int, float)):
            cell['window_calls_total'] += rollup['window_calls']
        if isinstance(rollup.get('window_total_ms'), (int, float)):
            cell['window_total_ms_total'] += rollup['window_total_ms']
        if isinstance(rollup.get('window_max_ms'), (int, float)):
            if (cell['window_max_ms_max'] is None
                    or rollup['window_max_ms'] > cell['window_max_ms_max']):
                cell['window_max_ms_max'] = rollup['window_max_ms']
        owner = rollup.get('owner_project_token')
        if owner is not None and owner not in cell['owners']:
            cell['owners'].append(owner)
        for key, bucket in (('reset_reason', 'reset_reasons'),
                            ('flush_reason', 'flush_reasons'),
                            ('trace_mode', 'trace_modes')):
            value = rollup.get(key)
            if value is not None and value not in cell[bucket]:
                cell[bucket].append(value)

    samples_by_name = collections.OrderedDict()
    for sample in sess.hotpath_samples:
        name = sample.get('name') or '(unknown)'
        cell = samples_by_name.get(name)
        if cell is None:
            cell = samples_by_name[name] = {
                'name': name, 'count': 0, 'failed': 0,
                'durations': [], 'observations': [],
            }
        cell['count'] += 1
        if sample.get('failed'):
            cell['failed'] += 1
        if isinstance(sample.get('duration_ms'), (int, float)):
            cell['durations'].append(sample['duration_ms'])
        if len(cell['observations']) < 15:
            cell['observations'].append({
                'seq': sample['seq'], 'duration_ms': sample.get('duration_ms'),
                'failed': sample.get('failed'),
                'observation': sample.get('observation'),
                'evidence': sample['evidence'],
            })
    for cell in samples_by_name.values():
        cell['duration_distribution'] = distribution(
            cell.pop('durations'), '%s 的阈值采样耗时' % cell['name'])

    rows = []
    for name, cell in by_name.items():
        samples = samples_by_name.get(name, {'count': 0, 'failed': 0})
        rows.append(dict(cell, threshold_sample_count=samples.get('count', 0),
                         threshold_failed_count=samples.get('failed', 0)))
    for name, cell in samples_by_name.items():
        if name in by_name:
            continue
        rows.append({
            'name': name, 'rollups': 0,
            'cumulative_calls_last': None, 'cumulative_total_ms_last': None,
            'cumulative_max_ms_last': None,
            'window_calls_total': 0, 'window_total_ms_total': 0.0,
            'window_max_ms_max': None, 'failed_calls_last': None,
            'owners': [], 'reset_reasons': [], 'flush_reasons': [],
            'minimum_sample_ms': None, 'sampling_strategy': None,
            'trace_modes': [], 'first_seq': None, 'last_seq': None,
            'threshold_sample_count': cell['count'],
            'threshold_failed_count': cell['failed'],
            'evidence': cell['observations'][0]['evidence'] if cell['observations'] else None,
        })

    return {
        'rollup_count': len(sess.hotpath_rollups),
        'threshold_sample_count': len(sess.hotpath_samples),
        'paths': rows,
        'threshold_observations': list(samples_by_name.values()),
        'policy': ('普通模式不为每次快调用写完整生命周期：只有超过阈值或失败的调用'
                   '才有独立观测样本，其余进入按间隔的累计 rollup。'
                   '累计值带 owner 与 reset_reason，换工程不会把旧工程的工作量'
                   '算成新工程的。'),
        'caveat': ('阈值样本**没有 opening**，不是完整 span：它不并入窗口用时，'
                   '也不与父阶段竞争排名。'),
    }


def recording_contract_report(sess):
    """记录契约：schema/契约版本、manifest、版本快照与证据保留策略。"""
    start = next((m for m in sess.manifests if m.get('phase') == 'start'), None)
    end = next((m for m in sess.manifests if m.get('phase') == 'end'), None)
    versions = sess.schema_versions_seen
    mismatch = sorted(version for version in versions
                      if version != SCHEMA_TARGET)
    contract = None
    for item in (end, start):
        if item and item.get('recording_contract_version') is not None:
            contract = item['recording_contract_version']
            break
    if contract is None:
        for snapshot in sess.version_snapshots:
            if snapshot.get('schema_version') is not None:
                contract = snapshot['schema_version']
                break
    if not sess.manifests:
        verdict = ('**缺少 trace_manifest**：本日志没有记录契约与保留策略的'
                   '自述，无法确认它由哪个契约版本写出、证据保留多久。')
    elif mismatch:
        verdict = ('schema 版本 %s 与目标 v%d 不一致：字段含义可能不同，'
                   '跨版本比较必须先对齐。' % (mismatch, SCHEMA_TARGET))
    else:
        verdict = ('记录契约一致：manifest 在场，schema 版本与目标一致。')
    return {
        'schema_target': SCHEMA_TARGET,
        'schema_versions_seen': dict(versions),
        'schema_version_mismatch': mismatch or None,
        'recording_contract_version': contract,
        'manifest_present': bool(sess.manifests),
        'manifest_start': start,
        'manifest_end': end,
        'retention_policy': (start or {}).get('retention_policy'),
        'evidence_retention': (start or {}).get('evidence_retention'),
        'rotation': {
            'rotation_max_bytes': (start or {}).get('rotation_max_bytes'),
            'rotation_backup_count': (start or {}).get('rotation_backup_count'),
            'retained_log_files': (start or {}).get('retained_log_files'),
        },
        'version_snapshots': sess.version_snapshots,
        'writer_cost': {
            'events_written': (end or {}).get('events_written'),
            'bytes_written': (end or {}).get('bytes_written'),
            'writer_ms_total': (end or {}).get('writer_ms_total'),
            'writer_ms_max': (end or {}).get('writer_ms_max'),
            'record_self_ms_total': (end or {}).get('record_self_ms_total'),
            'record_self_ms_max': (end or {}).get('record_self_ms_max'),
            'gc_collection_errors': (end or {}).get('gc_collection_errors'),
            'note': ('写入耗时由 writer 线程自己测量，record 耗时是生产事件的'
                     '自耗时；两者都不是「全应用开销」。'),
        },
        'verdict': verdict,
    }


def resource_rollups_report(sess):
    """``resource_rollup``：每个资源类一行。

    * ``source='measured'``：current/peak/evicted 有数。
    * ``source='not_accounted'`` / ``'unknown'``：**current=None，打印为未知**，
      绝不写成 0，也绝不丢掉这个类。
    * budget 是**共享缓存上限**，不是进程 RSS 红线；``pin_over_budget`` 在各
      class 里是同一个共享数字，**不能跨 class 求和**。
    """
    if not sess.resource_rollups:
        return {
            'rollup_count': 0,
            'classes': [],
            'note': '本 session 没有 resource_rollup 事件（旧日志没有这个族）。',
            'budget_note': ('budget 是**共享缓存上限**（shared_cache_limit），'
                            '不是进程 RSS 上限。'),
            'pin_over_budget_note': ('pin_over_budget 是共享数字，'
                                      '**不要跨 class 求和**。'),
        }

    last = sess.resource_rollups[-1]
    raw_classes = last.get('classes') or {}
    ordered = [name for name in RESOURCE_CLASSES if name in raw_classes]
    ordered += [name for name in sorted(raw_classes) if name not in ordered]

    rows = []
    for name in RESOURCE_CLASSES:
        payload = raw_classes.get(name)
        if not isinstance(payload, dict):
            rows.append({
                'class': name,
                'present_in_rollup': False,
                'source': '(absent)',
                'owner': None, 'budget': None, 'unit': None,
                'current': None, 'peak': None, 'evicted': None,
                'pin_over_budget': None,
                'current_display': '未知（本 rollup 里没有这个 class）',
                'reason': '该 rollup 没有这个 class —— **缺失，不是 0**。',
                'note': '没有这个 class 不代表它占 0 字节，只代表这次没报。',
            })
            continue
        source = payload.get('source') or 'unknown'
        measured = (source == 'measured')
        current = payload.get('current') if measured else None
        peak = payload.get('peak') if measured else None
        evicted = payload.get('evicted') if measured else None
        pin = payload.get('pin_over_budget') if measured else None
        if current is None:
            current_display = '未知'
        else:
            current_display = current
        rows.append({
            'class': name,
            'present_in_rollup': True,
            'source': source,
            'owner': payload.get('owner'),
            'budget': payload.get('budget'),
            'unit': payload.get('unit'),
            'budget_kind': payload.get('budget_kind'),
            'budget_is_rss_cap': payload.get('budget_is_rss_cap'),
            'categories': payload.get('categories'),
            'current': current,
            'peak': peak,
            'evicted': evicted,
            'pin_over_budget': pin,
            'current_display': current_display,
            'reason': payload.get('reason'),
            'pin_over_budget_scope': payload.get('pin_over_budget_scope'),
            'note': ('measured：数值来自实测计数。' if measured else
                     'source=%s：**数值未知**（%s）。未知就是未知，'
                     '不是 0，也不能当成「没占资源」。'
                     % (source, payload.get('reason') or '日志未说明原因')),
        })

    unknown_names = [name for name in raw_classes if name not in RESOURCE_CLASSES]
    for name in unknown_names:
        payload = raw_classes[name]
        rows.append({
            'class': name,
            'present_in_rollup': True,
            'source': payload.get('source') or 'unknown',
            'owner': payload.get('owner'),
            'budget': payload.get('budget'),
            'unit': payload.get('unit'),
            'current': None, 'peak': None, 'evicted': None,
            'pin_over_budget': None,
            'current_display': '未知',
            'reason': payload.get('reason'),
            'note': ('类名不在本工具的已知清单里，按未知处理 —— '
                     '**不丢、不当成 0**。'),
        })

    return {
        'rollup_count': len(sess.resource_rollups),
        'last_seq': last.get('seq'),
        'evidence': last.get('evidence'),
        'reason': last.get('reason'),
        'classes': rows,
        'shared_limit': last.get('limit'),
        'shared_pin_over_budget': last.get('pin_over_budget'),
        'over_budget': last.get('over_budget'),
        'bytes': last.get('bytes'),
        'resident_bytes': last.get('resident_bytes'),
        'budget_kind': last.get('budget_kind'),
        'budget_is_rss_cap': last.get('budget_is_rss_cap'),
        'budget_note': ('budget 是**共享缓存上限**（shared_cache_limit）：'
                        '它约束的是「可重建的 CPU 缓存」这一族，'
                        '**不是进程 RSS 上限**，不能当成内存红线。'),
        'pin_over_budget_note': ('pin_over_budget 在各个 class 里重复的是'
                                  '**同一个共享数字**（pin_over_budget_scope='
                                  'shared_budget）；**不要跨 class 求和**。'
                                  '共享值见 shared_pin_over_budget=%s。'
                                  % last.get('pin_over_budget')),
        'missing_classes': [name for name in RESOURCE_CLASSES
                            if name not in raw_classes],
        'policy': ('source 不是 measured 的类，current/peak/evicted 一律是 None，'
                   '报告里打印为「未知」——**不写成 0，也不丢掉这个类**。'),
    }


def presentation_markers_report(sess):
    """呈现 marker：按范围报告「首绘 / 交互就绪 / 清晰度状态 / 是否画出来」。

    * ``unknown`` 就是 unknown，附上原因；**缺 marker 不等于「已经画出来」**。
    * ``assets.paint_frame`` 是**每帧计数器**（默认每秒一行），不是逐图日志。
    """
    scopes = collections.OrderedDict()
    for marker in sess.presentation_markers:
        key = marker['scope_key']
        cell = scopes.get(key)
        if cell is None:
            cell = scopes[key] = {
                'scope_key': key,
                'project_id': marker.get('project_id'),
                'generation': marker.get('generation'),
                'scope_pages_fingerprint': marker.get('scope_pages_fingerprint'),
                'scope_mode': marker.get('scope_mode'),
                'markers': [],
                'content_first_paint': None,
                'scope_interactive': None,
                'focus_quality': None,
                'viewport_quality': None,
            }
        cell['markers'].append({
            'seq': marker['seq'], 'event': marker['event'], 'kind': marker['kind'],
            'status': marker.get('status'), 'painted': marker.get('painted'),
            'reason': marker.get('reason'),
            'quality_covered': marker.get('quality_covered'),
            'quality_total': marker.get('quality_total'),
            'evidence': marker['evidence'],
        })
        kind = marker['kind']
        if kind == 'content_first_paint':
            cell['content_first_paint'] = {
                'seen': True, 'seq': marker['seq'], 'status': marker.get('status'),
                'painted': marker.get('painted'), 'evidence': marker['evidence'],
            }
        elif kind == 'scope_interactive':
            cell['scope_interactive'] = {
                'seen': True, 'seq': marker['seq'], 'status': marker.get('status'),
                'evidence': marker['evidence'],
            }
        elif kind in ('focus_quality_ready', 'focus_quality_pending',
                      'focus_quality_unknown'):
            cell['focus_quality'] = _quality_state(cell.get('focus_quality'),
                                                    marker, 'focus')
        elif kind == 'focus_quality_painted':
            state = cell.get('focus_quality') or {'state': None, 'reason': None,
                                                  'painted': None, 'seq': None,
                                                  'painted_marker_seq': None,
                                                  'evidence': None}
            state['painted_marker_seq'] = marker['seq']
            state['painted_marker_evidence'] = marker['evidence']
            cell['focus_quality'] = state
        elif kind in ('viewport_quality_ready', 'viewport_quality_pending',
                      'viewport_quality_unknown'):
            cell['viewport_quality'] = _quality_state(cell.get('viewport_quality'),
                                                      marker, 'viewport')
        elif kind == 'viewport_quality_painted':
            state = cell.get('viewport_quality') or {'state': None, 'reason': None,
                                                    'painted': None, 'seq': None,
                                                    'painted_marker_seq': None,
                                                    'evidence': None}
            state['painted_marker_seq'] = marker['seq']
            state['painted_marker_evidence'] = marker['evidence']
            cell['viewport_quality'] = state

    scope_rows = []
    for key, cell in scopes.items():
        focus = cell.get('focus_quality')
        viewport = cell.get('viewport_quality')
        scope_rows.append({
            'scope_key': key,
            'project_id': cell['project_id'],
            'generation': cell['generation'],
            'scope_pages_fingerprint': cell['scope_pages_fingerprint'],
            'scope_mode': cell['scope_mode'],
            'content_first_paint': cell['content_first_paint'] or {
                'seen': False, 'seq': None, 'status': None, 'painted': None,
                'note': '**没有这个 marker** —— 不等于「已经画出来」，只能记为未见。',
            },
            'scope_interactive': cell['scope_interactive'] or {
                'seen': False, 'seq': None, 'status': None,
                'note': '**没有这个 marker** —— 交互就绪状态未知。',
            },
            'focus_quality': _quality_row(focus, 'focus'),
            'viewport_quality': _quality_row(viewport, 'viewport'),
            'marker_count': len(cell['markers']),
            'markers': cell['markers'],
        })

    frames = []
    totals = {'frames': 0, 'cards': 0, 'content_paints': 0, 'text_paints': 0,
              'quality_samples': 0, 'quality_ready_samples': 0,
              'quality_unknown_samples': 0}
    for frame in sess.paint_frames:
        for name in totals:
            value = frame.get(name)
            if isinstance(value, (int, float)):
                totals[name] += value
        frames.append(frame)

    return {
        'scope_count': len(scope_rows),
        'scopes': scope_rows,
        'frames': {
            'rollup_count': len(sess.paint_frames),
            'totals': totals,
            'per_window': frames,
            'note': ('assets.paint_frame 是**每帧计数器**（生产端默认每秒 flush 一行，'
                     '每次 flush 后计数清零）：frames=帧数，cards=跨帧模型行数之和，'
                     'content_paints/text_paints是旧内容/文本分类帧计数。'
                     '这些不是唯一实际绘制ID或可见内容就绪证据，不能据此声明完整呈现。'),
        },
        'policy': ('缺 marker 只说明「没观察到」，**不是**「已经画出来」；'
                   'quality=unknown 保留 unknown 及其原因，不折算成 ready 或 pending。'),
    }


def _quality_state(previous, marker, family):
    """记录一次清晰度状态翻转。state 永远是 ready/pending/unknown 三选一。"""
    kind = marker['kind']
    if kind.endswith('_ready'):
        state = 'ready'
    elif kind.endswith('_pending'):
        state = 'pending'
    else:
        state = 'unknown'
    return {
        'state': state,
        'state_known': True,
        'reason': marker.get('reason'),
        'painted': marker.get('painted'),
        'seq': marker['seq'],
        'evidence': marker['evidence'],
        # A new quality target (zoom/pan/invalidation) needs fresh paint proof.
        # Retaining an old ready frame here would claim the new pending target
        # was already presented, even though no frame has painted it yet.
        'painted_marker_seq': None,
        'painted_marker_evidence': None,
    }


def _quality_row(state, family):
    """清晰度的最终状态。unknown 保留 unknown 与原因，缺 marker 是「未见」。"""
    if not state:
        return {
            'family': family,
            'state': '(无 marker)',
            'state_known': False,
            'reason': None,
            'painted': None,
            'painted_marker_seen': False,
            'seq': None,
            'note': ('**没有 %s 清晰度 marker** —— 状态未知；'
                     '缺 marker **不是**「已经画出来」的证据。' % family),
        }
    painted_marker = state.get('painted_marker_seq') is not None
    if state.get('state') == 'unknown':
        note = ('状态是 **unknown**（原因：%s）：判据不足，不是「模糊」也不是「清晰」。'
                % (state.get('reason') or '日志未给出原因'))
    elif state.get('state') == 'ready':
        note = ('状态 **ready**：这一范围的清晰度判据成立（质量覆盖 %s/%s）。'
                % ('—', '—'))
    else:
        note = ('状态 **pending**：要求的清晰度还没装上（原因：%s）。'
                % (state.get('reason') or '日志未给出原因'))
    if not painted_marker:
        note += ' **没有**对应的 `*_quality_painted` marker —— 「ready」不等于「已画出」。'
    return {
        'family': family,
        'state': state.get('state'),
        'state_known': True,
        'reason': state.get('reason'),
        'painted': state.get('painted'),
        'painted_marker_seen': painted_marker,
        'seq': state.get('seq'),
        'evidence': state.get('evidence'),
        'note': note,
    }


def _stall_dispatch_evidence(sess, row):
    """Reassemble only recorder-frozen batches with matching stall/window.

    Completeness is for the recorded dispatch snapshot, never native CPU
    attribution or proof that the finite recorder history saw every dispatch.
    """
    original = row.get('dispatch_intervals') or []
    result = dict(complete=False, reason='legacy_unmeasured', batch_evidence=[],
                  scope='recorded_frozen_dispatch_history', root_cause_proven=False)
    if type(row.get('dispatch_batch_version')) is not int or row['dispatch_batch_version'] != 1:
        if any(not isinstance(item, dict) for item in original):
            result['reason'] = 'legacy_capped_or_invalid'
        return original, result
    count, retained, omitted, batches = (row.get(key) for key in (
        'dispatch_interval_count', 'dispatch_intervals_retained',
        'dispatch_intervals_omitted', 'dispatch_batch_count'))
    if (any(type(value) is not int or value < 0 for value in (count, retained, omitted, batches))
            or retained > 512 or count != retained+omitted
            or batches != ((retained+23)//24 if retained > 24 else 0)):
        result['reason'] = 'invalid_batch_manifest'
        return original, result
    candidates = [fact for fact in sess.stall_dispatch_batches
                  if fact['entry'].get('stall_id') == row.get('stall_id')]
    parts = {}
    for fact in candidates:
        entry = fact['entry']
        index = entry.get('batch_index')
        if (type(entry.get('dispatch_batch_version')) is not int or entry['dispatch_batch_version'] != 1
                or type(index) is not int or not 0 <= index < batches or index in parts
                or type(entry.get('batch_count')) is not int or entry['batch_count'] != batches
                or type(entry.get('dispatch_interval_count')) is not int or entry['dispatch_interval_count'] != count
                or entry.get('window_start_monotonic') != row.get('window_start_monotonic')
                or entry.get('window_end_monotonic') != row.get('window_end_monotonic')
                or not isinstance(entry.get('dispatch_intervals'), list)
                or len(entry['dispatch_intervals']) != min(24, retained-index*24)):
            result['reason'] = 'conflicting_or_invalid_batch'
            return original, result
        parts[index] = entry['dispatch_intervals']
        result['batch_evidence'].append(fact['evidence'])
    if len(parts) != batches:
        result['reason'] = 'missing_dispatch_batches'
        return original, result
    intervals = ([item for index in range(batches) for item in parts[index]]
                 if batches else original)
    if len(intervals) != retained or (batches and original != intervals[:24]):
        result['reason'] = 'dispatch_count_or_prefix_mismatch'
        return original, result
    window_start, window_end = row.get('window_start_monotonic'), row.get('window_end_monotonic')
    if (type(window_start) not in (int, float) or type(window_end) not in (int, float)
            or not math.isfinite(window_start) or not math.isfinite(window_end)
            or window_end < window_start):
        result['reason'] = 'invalid_frozen_window'
        return original, result
    ids = set()
    for item in intervals:
        if not isinstance(item, dict):
            result['reason'] = 'invalid_dispatch_interval'
            return original, result
        ident = item.get('dispatch_id')
        start, end = item.get('start_monotonic'), item.get('end_monotonic')
        if (not isinstance(ident, str) or not ident or ident in ids
                or type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end) or end < start
                or end < window_start or start > window_end):
            result['reason'] = 'invalid_dispatch_interval'
            return original, result
        ids.add(ident)
    result.update(complete=omitted == 0,
                  reason='complete_batches' if omitted == 0 else 'recorder_retention_limit',
                  recorded_intervals=retained, omitted_intervals=omitted)
    return intervals, result


def stall_contexts_report(sess):
    """``ui.stall_context`` grouped by classification and terminal stall identity."""
    counts = collections.Counter()
    association_counts = collections.Counter()
    by_class = collections.OrderedDict()
    events = []
    terminals = [item for item in _raw_stalls(sess) if item['phase'] == 'end']

    def usable_stall_id(value):
        return value not in (None, '', 'unknown')

    terminals_by_id = collections.defaultdict(list)
    for terminal in terminals:
        if usable_stall_id(terminal.get('stall_id')):
            terminals_by_id[terminal['stall_id']].append(terminal)
    consumed_terminals = set()

    for item in sess.stall_contexts:
        row = dict(item)
        row['dispatch_intervals'], row['dispatch_evidence'] = _stall_dispatch_evidence(sess, row)
        stall_id = row.get('stall_id')
        candidates = (terminals_by_id.get(stall_id, [])
                      if usable_stall_id(stall_id) else [])
        terminal = None
        if len(candidates) == 1 and id(candidates[0]) not in consumed_terminals:
            terminal = candidates[0]
            row['association_basis'] = 'session+stall_id'
        elif len(candidates) > 1:
            row['association_basis'] = 'duplicate_terminal_stall_id'
            row['terminal_candidate_seqs'] = [value['seq'] for value in candidates]
        elif len(candidates) == 1:
            row['association_basis'] = 'duplicate_context_for_stall_id'
            row['terminal_candidate_seqs'] = [candidates[0]['seq']]
        elif usable_stall_id(stall_id):
            row['association_basis'] = 'session+stall_id_no_terminal'
        else:
            # Identity-free logs can only use the old adjacency guess. A terminal
            # is consumed once so separate contexts never claim the same window.
            legacy_candidates = [
                value for value in terminals
                if not usable_stall_id(value.get('stall_id'))
                and id(value) not in consumed_terminals
                and row.get('duration_ms') is not None
                and abs(value['dur_ms']-row['duration_ms']) < 0.51
                and value.get('seq') is not None and row.get('seq') is not None
                and 0 <= value['seq']-row['seq'] <= 50]
            if legacy_candidates:
                terminal = min(legacy_candidates,
                               key=lambda value: value['seq']-row['seq'])
                row['association_basis'] = 'legacy_adjacency_heuristic'
            else:
                row['association_basis'] = 'legacy_no_terminal_match'

        if terminal is not None:
            consumed_terminals.add(id(terminal))
            row.update(
                terminal_seq=terminal.get('seq'),
                terminal_duration_ms=terminal.get('dur_ms'),
                terminal_impact_level=terminal.get('impact_level'),
                terminal_impact_policy_version=terminal.get('impact_policy_version'),
                terminal_evidence=terminal.get('evidence'))
        association_counts[row['association_basis']] += 1

        classification = row.get('classification') or '(missing)'
        counts[classification] += 1
        cell = by_class.get(classification)
        if cell is None:
            cell = by_class[classification] = {
                'classification': classification,
                'count': 0,
                'reasons': collections.Counter(),
                'durations': [],
            }
        cell['count'] += 1
        if row.get('classification_reason'):
            cell['reasons'][row['classification_reason']] += 1
        if row.get('duration_ms') is not None:
            cell['durations'].append(row['duration_ms'])
        events.append(row)

    groups = []
    for classification, cell in by_class.items():
        groups.append({
            'classification': classification,
            'count': cell['count'],
            'reasons': dict(cell['reasons']),
            'duration_distribution': distribution(
                cell['durations'],
                'classification=%s 的 stall duration_ms' % classification),
        })

    return {
        'count': len(sess.stall_contexts),
        'classification_counts': dict(counts),
        'association_counts': dict(association_counts),
        'unclaimed_terminal_stalls': [
            {'seq': item.get('seq'), 'stall_id': item.get('stall_id'),
             'evidence': item.get('evidence')}
            for item in terminals if id(item) not in consumed_terminals],
        'by_classification': groups,
        'events': events,
        'known_classifications': list(STALL_CLASSIFICATIONS),
        'caveats': [
            '`stack_is_sampled_not_blame=True`：栈是**采样**不是归因。采样落在 '
            '`app.exec`/`processEvents` **不是**「某个具体业务函数慢」的证据，'
            '只证明采样那一刻人在那里。',
            '`unclassified` = **证据不足**，不是「什么都没发生」。它表示归类器'
            '拒绝猜测，原因写在 `classification_reason` 里。',
            '新协议只按同 session 的 `stall_id` 一对一关联终态；只有没有身份的旧日志'
            '才允许使用明确标注的邻接启发式，且一个终态最多消费一次。',
            '`background_is_causal_evidence=False`：后台活跃是同时发生的观察，'
            '不能据此声称它导致 GUI 卡顿。',
            '`modal` / `nested_loop` 为 `unknown` 时是**真的观测不到**，'
            '不能反推成「没有弹窗」。',
        ],
    }


def dispatch_intervals_report(sess):
    """Report nested dispatch identities without double-counting wall time."""
    rows = []
    ids = collections.Counter()
    for span in sess.spans:
        if span.get('event') not in ('slow_dispatch', 'nested_dispatch'):
            continue
        dispatch_id = span.get('dispatch_id')
        start = span.get('dispatch_start_monotonic')
        end = span.get('dispatch_end_monotonic')
        if not dispatch_id or start is None or end is None or end < start:
            continue
        ids[dispatch_id] += 1
        rows.append({
            'dispatch_id': dispatch_id,
            'parent_dispatch_id': span.get('parent_dispatch_id'),
            'root_dispatch_id': span.get('root_dispatch_id'),
            'depth': span.get('depth'), 'start': start, 'end': end,
            'inclusive_ms': round((end-start)*1000, 3),
            'seq': span.get('seq'), 'name': span.get('name'),
        })
    by_id = {row['dispatch_id']: row for row in rows if ids[row['dispatch_id']] == 1}
    for row in rows:
        children = [child for child in rows
                    if child.get('parent_dispatch_id') == row['dispatch_id']
                    and child['start'] >= row['start'] and child['end'] <= row['end']]
        child_union = union_ms([(child['start'], child['end']) for child in children])
        row['direct_child_union_ms'] = round(child_union, 3)
        row['exclusive_ms'] = round(max(0.0, row['inclusive_ms']-child_union), 3)
    return {
        'dispatches': rows,
        'unique_ids': len(by_id),
        'reused_ids': sorted(key for key, count in ids.items() if count > 1),
        'inclusive_sum_ms': round(sum(row['inclusive_ms'] for row in rows), 3),
        'interval_union_ms': round(union_ms([(row['start'], row['end'])
                                             for row in rows]), 3),
        'exclusive_sum_ms': round(sum(row['exclusive_ms'] for row in rows), 3),
        'note': ('inclusive 是每层 notify 耗时；interval_union 是墙钟忙碌并集；'
                 'exclusive 扣除直接子区间。三者不互相冒充。'),
    }


def span_chain_report(sess):
    """schema v2 的 span 链与归因。

    * 父子关系只采信日志里的 ``parent_span_id``；旧日志没有就留空。
    * ``origin_action_id`` 是「哪个用户动作引发的」的**权威**字段；
      没有它就是「未归因」，**绝不**按「最后一次点击」推断。
    """
    spans = [span for span in sess.spans
             if span.get('role') != 'attachment' and _is_work_span(span)]
    with_ids = [s for s in spans if s.get('span_id')]
    links = []
    for span in spans:
        if not span.get('parent_span_id'):
            continue
        links.append({
            'child_seq': span.get('seq'),
            'child_name': span.get('name'),
            'child_ms': round(span['dur_ms'], 3) if span.get('dur_ms') is not None else None,
            'child_span_id': span.get('span_id'),
            'parent_span_id': span.get('parent_span_id'),
            'parent_seq': span.get('parent_seq'),
            'parent_found': span.get('parent_found'),
            'depth': span.get('chain_depth'),
            'chain_source': span.get('chain_source'),
            'evidence': span['evidence'],
        })

    def group(key):
        buckets = collections.OrderedDict()
        for span in spans:
            if span.get('dur_ms') is None or span.get('status') == 'start':
                continue
            value = span.get(key)
            label = '(none)' if value is None else str(value)
            cell = buckets.setdefault(label, {'key': value, 'spans': [],
                                              'seqs': [], 'names': []})
            cell['spans'].append(span)
            if len(cell['seqs']) < 40:
                cell['seqs'].append(span.get('seq'))
            if len(cell['names']) < 12 and span.get('name') not in cell['names']:
                cell['names'].append(span.get('name'))
        return buckets

    attribution = []
    for label, cell in group('origin_action_id').items():
        intervals = [(s['start_mono'], s['end_mono']) for s in cell['spans']
                     if s.get('start_mono') is not None and s.get('end_mono') is not None]
        attribution.append({
            'origin_action_id': cell['key'],
            'spans': len(cell['spans']),
            'interval_union_ms': round(union_ms(intervals), 3),
            'naive_sum_ms': round(sum(s['dur_ms'] for s in cell['spans']
                                      if s.get('dur_ms') is not None), 3),
            'seqs': cell['seqs'],
            'names': cell['names'],
            'basis': 'origin_action_id（日志声明的用户动作 id）',
        })
    attribution.sort(key=lambda row: -(row['interval_union_ms'] or 0))

    traces = []
    for label, cell in group('trace_id').items():
        intervals = [(s['start_mono'], s['end_mono']) for s in cell['spans']
                     if s.get('start_mono') is not None and s.get('end_mono') is not None]
        traces.append({
            'trace_id': cell['key'],
            'spans': len(cell['spans']),
            'interval_union_ms': round(union_ms(intervals), 3),
            'seqs': cell['seqs'],
            'names': cell['names'],
        })
    traces.sort(key=lambda row: -(row['interval_union_ms'] or 0))

    unattributed = sum(1 for s in spans
                       if s.get('dur_ms') is not None and s.get('status') != 'start'
                       and s.get('origin_action_id') is None)

    return {
        'has_real_chain': bool(with_ids),
        'spans_total': len(spans),
        'spans_with_span_id': len(with_ids),
        'links': links,
        'parent_refs_missing': [link for link in links
                                if link['parent_found'] is False],
        'traces': traces,
        'attribution': attribution,
        'unattributed_spans': unattributed,
        'terminal_status_counts': dict(collections.Counter(
            s['status'] for s in spans
            if s.get('dur_ms') is not None and s.get('status') != 'start')),
        'chain_source': ('schema_v2 span_id/parent_span_id（日志声明）'
                         if with_ids else 'legacy（无 span_id，不做父子推断）'),
        'note': ('父子关系**只**来自日志里的 `parent_span_id`；旧日志没有这个字段，'
                 '本节留空——**不编造父子链**。'
                 '`origin_action_id` 是「哪个用户动作引发的」的权威字段；'
                 '没有它的 span 一律算「未归因」，**绝不**按「最后一次点击」推断。'
                 '区间并集才是这条动作真正占用的时间，朴素相加会重复计重叠部分。'),
    }


# --------------------------------------------------------------------------
# 组装报告
# --------------------------------------------------------------------------
def diagnostic_work_report(sess):
    """Explicit observation units; never sum overlapping GC/installation time."""
    events = sess.diagnostic_observations
    def selected(kind):
        return [fact for fact in events if fact['entry'].get('event') == kind]
    def timing(facts, field):
        values = [_num(f['entry'].get(field)) for f in facts]
        values = [v for v in values if v is not None and math.isfinite(v) and v >= 0]
        return dict(observed_events=len(facts), timed_events=len(values),
                    p50_ms=percentile(values, 50), p95_ms=percentile(values, 95),
                    max_ms=max(values) if values else None)
    gc_events = selected('gc')
    gc_groups = collections.Counter()
    for fact in gc_events:
        entry = fact['entry']
        # Legacy `generation` belongs to the project lifecycle, not Python GC.
        generation = entry.get('gc_generation')
        gc_groups[str(generation) if type(generation) is int else 'unknown'] += 1
    overruns = selected('catalog_install_slice_overrun')
    declined = selected('asset_model_delta_declined')
    phases = collections.Counter(f['entry'].get('phase_at_yield') or 'unknown' for f in overruns)
    reasons = collections.Counter(f['entry'].get('reason') or 'unknown' for f in declined)
    io_intervals = []
    previous = None
    for fact in sorted(selected('process'), key=lambda f: (
            _num(f['entry'].get('monotonic'), float('inf')), f['entry'].get('sequence', 0))):
        entry = fact['entry']
        io = entry.get('io') if isinstance(entry.get('io'), dict) else {}
        current = (entry.get('pid'), _num(entry.get('monotonic')), io)
        delta = None
        reason = 'no_previous_compatible_sample'
        if previous is not None:
            pid, stamp, old = previous
            elapsed = None if stamp is None or current[1] is None else current[1] - stamp
            compatible = (pid is not None and pid == current[0] and elapsed is not None and elapsed > 0
                          and io.get('source') == old.get('source')
                          and io.get('source') not in (None, 'unknown')
                          and io.get('scope') == old.get('scope') == 'current_process_lifetime'
                          and io.get('includes_child_processes') is False
                          and old.get('includes_child_processes') is False)
            values = {key: (_count(io.get(key)), _count(old.get(key)))
                      for key in ('read_bytes', 'write_bytes')}
            if compatible and all(a is not None and b is not None and math.isfinite(a)
                                  and math.isfinite(b) and a >= b >= 0 for a, b in values.values()):
                delta = {key: int(a-b) for key, (a,b) in values.items()}
                delta['interval_seconds'] = elapsed
                reason = None
            else:
                reason = 'counter_reset_missing_or_incompatible_sample'
        io_intervals.append(dict(seq=entry.get('sequence'), delta=delta, unknown_reason=reason,
                                 evidence=fact['evidence']))
        previous = current
    return dict(
        gc=dict(**timing(gc_events, 'duration_ms'), generations=dict(gc_groups),
                events=gc_events, observation_thresholds_ms=sorted(set(
                    f['entry']['gc_min_duration_ms'] for f in gc_events
                    if type(f['entry'].get('gc_min_duration_ms')) in (int, float))),
                threshold_unknown_events=sum('gc_min_duration_ms' not in f['entry'] for f in gc_events),
                policy='Only logged collections; legacy threshold may be unknown. Trigger context is not allocation root cause. Do not add to enclosing spans.'),
        installation=dict(**timing(overruns, 'elapsed_ms'), phases=dict(phases),
                          work=selected('catalog_install_work'), overruns=overruns,
                          policy='Cooperative budget, not preemptive. inherited/evaluated/published rows are distinct; lifetime read counters are not per-install scans.'),
        save_projection=dict(published=selected('asset_model_delta_published'),
                             declined=declined, decline_reasons=dict(reasons),
                             policy='A decline requests requery; it does not prove a full scan ran. No event means unobserved, not a cache hit or success.'),
        process_io=dict(intervals=io_intervals, physical_disk_bytes=False,
                        per_operation_attribution=False, includes_child_processes=False,
                        policy='Differences of compatible process lifetime transfer counters; not physical disk bytes or project bytes.'))


def build_report(sess, truncated_last_line):
    raw_by_kind = collections.defaultdict(list)
    for span in sess.spans:
        raw_by_kind[span['event']].append(span)
    return _assemble(sess, raw_by_kind, truncated_last_line)


def storage_workflow_report(sess):
    operations = collections.OrderedDict()
    for entry in sess.storage_workflow_events:
        identity = entry.get('operation_id')
        if not identity:
            continue
        operation = operations.setdefault(identity, dict(operation_id=identity,
            operation=entry.get('operation'), project_id=entry.get('project_id'),
            open_epoch=entry.get('open_epoch'), start=None, terminal=None,
            stage_ends=[], progress=[], latest_progress=None))
        phase = entry.get('phase')
        if phase == 'start':
            operation['start'] = entry
        elif phase == 'end':
            operation['terminal'] = entry
        elif phase == 'stage_end':
            operation['stage_ends'].append(entry)
        elif phase == 'progress':
            operation['progress'].append(entry)
            operation['latest_progress'] = entry
    for operation in operations.values():
        operation['terminal_observed'] = operation['terminal'] is not None
        operation['start_observed'] = operation['start'] is not None
    return dict(operations=list(operations.values()),
        truncated_events=sess.storage_workflow_truncated,
        timing_basis='background wall time, not GUI blocking; do not add parent/child durations',
        byte_basis='logical original payload counters; physical disk throughput unknown',
        missing_terminal='incomplete evidence, not proof of success or failure')


def _assemble(sess, raw_by_kind, truncated_last_line):
    windows = build_windows(sess, raw_by_kind)
    report = {
        'tool': 'analyze_prism_trace.py',
        'tool_version': TOOL_VERSION,
        'schema_target': SCHEMA_TARGET,
        'session': sess.session_id,
        'input_files': sess.files,
        'read_only': True,
        'session_scope': {
            'note': '本节所有数字仅来自 session %s，未与其他 session 混合。' % sess.session_id,
            'total_events': sess.total_events,
            'event_counts': dict(sess.event_counts.most_common()),
            'has_session_start': sess.has_session_start,
            'has_session_end': sess.has_session_end,
            'time_range': {
                'first_time': sess.first_time, 'last_time': sess.last_time,
                'wall_span_s': (round(sess.last_time - sess.first_time, 3)
                                if sess.first_time is not None and sess.last_time is not None
                                else None),
                'first_monotonic': sess.first_mono, 'last_monotonic': sess.last_mono,
                'time_base_used': sess.time_base,
            },
            'pid': sess.pid,
            'runtime': sess.runtime,
        },
        'completeness': completeness(sess, truncated_last_line),
        'schema_coverage': {
            'schema_v2_events': sess.v2_events,
            'is_legacy_log': sess.v2_events == 0,
            'fields': {
                field: {
                    'events_carrying': sess.schema_seen.get(field, 0),
                    'coverage_pct': (round(100.0 * sess.schema_seen.get(field, 0)
                                           / sess.total_events, 2)
                                     if sess.total_events else 0.0),
                    'sample_value': sess.schema_values.get(field),
                    'state': ('present' if sess.schema_seen.get(field) else
                              'legacy_missing'),
                } for field in SCHEMA_V2_FIELDS},
            'legacy_note': (
                '当前日志包含关联字段；父子链仅使用实际记录的 parent_span_id，缺失部分保持未知。'
                if any(sess.schema_seen.get(field, 0) for field in
                       ('trace_id', 'span_id', 'parent_span_id')) else
                '未记录关联字段，无法建立真实父子链；不得按时间邻近补造关联。'),
        },
        'sampling_coverage': sampling_coverage(sess),
        'recording_contract': recording_contract_report(sess),
        'diagnostic_archive': dict(summaries=sess.diagnostic_archive_summaries,
            coverage='significant events only; not a complete operation history'),
        'maintenance': dict(events=list(sess.maintenance), coverage='latest 64 observed cleanup records'),
        'storage_workflows': storage_workflow_report(sess),
        'catalog_publications': catalog_publications_report(sess),
        'hotpath': hotpath_report(sess),
        'traces': windows,
        'gui_stalls': stall_report(sess),
        'slow_sql': slow_sql_report(sess, raw_by_kind),
        'catalog': catalog_report(sess),
        'queues': queue_report(sess),
        'queue_rollups': queue_rollups_report(sess),
        'resource_rollups': resource_rollups_report(sess),
        'presentation_markers': presentation_markers_report(sess),
        'presentation_evidence': presentation_evidence_report(sess),
        'stall_contexts': stall_contexts_report(sess),
        'dispatch_intervals': dispatch_intervals_report(sess),
        'span_chain': span_chain_report(sess),
        'paged_progress': {
            'events': sess.paged_progress,
            'catalog_build': [item for item in sess.paged_progress
                              if item['phase'] == 'catalog_build'],
            'scope_prepared': [item for item in sess.paged_progress
                               if item['phase'] == 'scope'],
            'ambiguous': [item for item in sess.paged_progress
                          if item['phase'] == 'ambiguous'],
            'policy': ('新事件优先；带 phase 的旧事件按阶段分流；新旧双写按操作身份和'
                       '语义字段只计一次；无 phase 不归 model_prepared。'),
        },
        'count_layers': count_layers_report(sess),
        'diagnostic_work': diagnostic_work_report(sess),
        'resource_peaks': {
            'peaks': sess.resource_peaks,
            'caveat': ('峰值均为采样可见值；日志不含磁盘吞吐、网络、分页内存细节，'
                       '这些属于**未知项**。'),
            'unknown_items': [
                '实际磁盘 I/O 字节数 / 读放大倍数',
                '分页文件与提交内存峰值',
                '当时视口尺寸与可见卡片判定依据',
                '被丢弃事件的具体内容',
                'CPU 总占用（只有单核百分比，没有多核/整机口径）',
            ],
        },
        'application_errors': dict(groups=sorted(sess.application_errors.values(),
            key=lambda row:row['count'], reverse=True),
            overflow_records=sess.application_error_overflow,
            note='Counts are error records, not independent failed operations; paired lifecycle events may describe one failure.'),
        'findings': findings(sess, windows),
    }
    report['milestones'] = sess.milestones
    return report


def findings(sess, windows):
    """把 6 条硬指标做成可核对的结论，每条带原始 sequence 证据。"""
    out = []
    idx = {w['name']: w for w in windows}

    # 1. 打开墙钟
    for milestone in sess.milestones:
        if milestone['event'] == 'project_open_finished':
            out.append({
                'id': 'open_finalization_complete',
                'title': '打开 finalization_complete 墙钟',
                'value_ms': milestone['duration_ms'],
                'seq': milestone['seq'],
                'phase': milestone['phase'],
                'file': milestone['evidence']['file'],
                'line': milestone['evidence']['line'],
            })

    # 2. embedded_glb 慢 SQL + 占比 + 诚实性声明
    heavy = [s for s in sess.spans
             if s['event'] == 'operation' and s['name'] == 'project.sql.execute'
             and s['thread'] == 'Dummy-1'
             and s['name'] and (s['dur_ms'] or 0) > 60000]
    for span in sorted(heavy, key=lambda s: -(s['dur_ms'] or 0)):
        query = span.get('name')
        wall = None
        for milestone in sess.milestones:
            if milestone['event'] == 'project_open_finished':
                wall = milestone['duration_ms']
        ratio = (span['dur_ms'] / wall) if (wall and span['dur_ms']) else None
        out.append({
            'id': 'slow_sql_embedded_glb',
            'title': '%s 慢 SQL（后台线程）' % (span.get('evidence') and 'project.sql.execute'),
            'duration_ms': round(span['dur_ms'], 3),
            'rows': span['rows'],
            'thread': span['thread'],
            'share_of_open_wall_pct': round(100.0 * ratio, 2) if ratio else None,
            'open_wall_ms': wall,
            'seq': span['seq'],
            'evidence': span['evidence'],
            'honesty_note': (
                '**rows=0 不等于没有 I/O，也不等于读了 55 GB。**'
                '日志只记录了 duration_ms 与返回行数；0 行说明这条语句没有产出结果集'
                '（可能被取消/条件不满足/结果被丢弃），但**耗时 262 s 本身证明这段时间里'
                '线程确实被占住了**。要把这 262 s 归因到具体读了多少字节，需要 '
                'I/O 计数或 page fault / read_bytes 指标 —— 本日志没有，**未知**。'
                '任何「读了 55 GB」「0 行所以 0 开销」的说法都是推断，不是本日志的证据。'),
        })

    # 3. 目录批次
    batches = sess.catalog.get('browser_batches')
    if batches:
        out.append({
            'id': 'catalog_browser_read_batches',
            'title': '目录浏览分页读取累计（不等于全库扫描）',
            'batches': batches['batches'],
            'rows_total': int(batches['rows']),
            'pages_total': int(batches['pages']),
            'seq_range': [batches['first_seq'], batches['last_seq']],
            'guard': ('累计读取 %d 行，可能跨查询、重复或被取消；没有唯一行数与'
                      '项目总量的对应证据，不能判定全库扫描。'
                      % int(batches['rows'])),
        })

    # 4. 保存后 scope miss
    miss_seqs = [e['seq'] for e in sess.scope_events
                 if e['result'] == 'miss' and e['reason'] == 'not_ready'
                 and e['partial'] is True]
    if miss_seqs:
        episodes, pending = [], None
        for event in sess.scope_events:
            if event['result'] == 'miss' and event['reason'] == 'not_ready' and event['partial'] is True:
                if pending is None:
                    pending = dict(first_seq=event['seq'], count=0, start=event['monotonic'])
                pending['count'] += 1
            elif event['result'] == 'hit' and pending is not None:
                start = pending.pop('start')
                pending.update(recovered=True, recovery_seq=event['seq'],
                    duration_ms=(round((event['monotonic']-start)*1000, 3)
                                 if start is not None and event['monotonic'] is not None else None))
                episodes.append(pending)
                pending = None
        if pending is not None:
            pending.pop('start')
            pending.update(recovered=False, recovery_seq=None, duration_ms=None)
            episodes.append(pending)
        out.append({
            'id': 'catalog_scope_miss_not_ready',
            'title': '部分加载工程目录暂未就绪（按恢复区间区分）',
            'count': len(miss_seqs),
            'seqs': miss_seqs,
            'episodes': episodes,
            'interpretation': ('partial=True 表示工程部分加载，不代表保存失败。'
                               'miss 是尚未就绪的调用次数，不是独立故障数；'
                               '后续 hit 表示已观察到恢复，未恢复区间需要继续排查。'
                               '不能只按次数判定持续故障或断言发生在保存后。'),
        })

    # 5. 脑图 payload + 主线程 stall，父子不叠加
    mm = [s for s in sess.spans
          if str(s['name']).startswith('mindmap.') and s['dur_ms'] is not None]
    if mm:
        inter = [(s['start_mono'], s['end_mono']) for s in mm
                 if s['start_mono'] is not None]
        uni = union_ms(inter)
        # 只认与脑图窗口真实重叠的主线程 stall；无法重叠则不臆测（留空并说明）。
        stall = None
        lo = min(a for a, _ in inter) if inter else None
        hi = max(b for _, b in inter) if inter else None
        if lo is not None:
            overlaps = [
                s for s in sess.spans
                if s['event'] == 'gui_stall' and s['thread'] in MAIN_THREADS
                and s['dur_ms'] and s['start_mono'] is not None
                and s['start_mono'] < hi and s['end_mono'] > lo]
            if overlaps:
                stall = max(overlaps, key=lambda s: s['dur_ms'])
        out.append({
            'id': 'mindmap_payload_vs_gui_stall',
            'title': '脑图 payload 构建与主线程 stall（父子重叠，不可相加）',
            'spans': [{'seq': s['seq'], 'name': s['name'],
                       'ms': round(s['dur_ms'], 3), 'thread': s['thread'],
                       'start_seq': s.get('start_seq'),
                       'inferred_parent_seq': s.get('inferred_parent_seq'),
                       'link_confidence': s.get('link_confidence', 'unknown'),
                       'evidence': s['evidence']} for s in mm],
            'naive_sum_ms': round(sum(s['dur_ms'] for s in mm), 3),
            'interval_union_ms': round(uni, 3),
            'gui_stall_ms': round(stall['dur_ms'], 3) if stall else None,
            'gui_stall_seq': stall['seq'] if stall else None,
            'gui_stall_evidence': stall['evidence'] if stall else None,
            'gui_stall_match': ('按时间区间与脑图 span 重叠筛选，'
                                '未取「最后一次长 stall」，避免错挂证据。'),
            'link_basis': ('session_trace_parent_span_id'
                           if all(s.get('span_id') for s in mm)
                           else 'legacy_inferred_or_mixed; inspect individual span evidence'),
            'note': ('Naive sum %.3f ms; interval union %.3f ms; '
                     'overlapping GUI stall %s ms. Nested intervals must not be added.' % (
                         sum(s['dur_ms'] for s in mm), uni,
                         round(stall['dur_ms'], 3) if stall else 'not_observed')),
        })

    # 6. 保存前 JSON 处理停顿
    json_stalls = []
    for s in sess.spans:
        if s['event'] != 'gui_stall' or s['thread'] not in MAIN_THREADS:
            continue
        stacks = s.get('stack_histogram') or []
        top = stacks[0][1] if stacks else ''
        if '_page_patches' in top or 'partial_project' in top:
            json_stalls.append({'seq': s['seq'], 'ms': round(s['dur_ms'], 3),
                                'phase': s.get('stall_phase'),
                                'top_stack': top[:120]})
    if json_stalls:
        json_stalls.sort(key=lambda x: -x['ms'])
        out.append({
            'id': 'save_page_patches_json_stall',
            'title': '保存前 partial_project._page_patches JSON 处理主线程停顿',
            'stalls': json_stalls[:6],
            'note': ('主线程在 _page_patches 的 loads/dumps 上阻塞，'
                     '与保存窗口重叠；这是**前台卡顿**，不是后台可忽略成本。'),
        })

    # 恢复快照 JSON 编码/写盘
    snap = []
    for s in sess.spans:
        if s['event'] != 'gui_stall':
            continue
        stacks = s.get('stack_histogram') or []
        top = stacks[0][1] if stacks else ''
        if '_write_recovery_snapshot' in top:
            snap.append({'seq': s['seq'], 'ms': round(s['dur_ms'], 3),
                         'phase': s.get('stall_phase'),
                         'samples': s.get('samples')})
    if snap:
        out.append({
            'id': 'recovery_snapshot_stall',
            'title': '恢复快照 JSON 编码/写盘造成的主线程停顿',
            'events': sorted(snap, key=lambda x: -x['ms']),
            'note': ('集中在 __main__._write_recovery_snapshot，'
                     '含 json encoder 与写盘；属于周期性后台写但跑在主线程。'),
        })
    return out


# --------------------------------------------------------------------------
# 对比（默认拒绝出提升百分比）
# --------------------------------------------------------------------------
REQUIRED_COMPARE_KEYS = ('hardware', 'app_version', 'scenario')


def compare_reports(current, baseline, context=None):
    declared = context or {}
    result = {
        'required_conditions': list(REQUIRED_COMPARE_KEYS),
        'declared_context': declared,
        'verdict': 'refused',
        'improvement_pct': None,
        'reasons': [],
    }
    missing = [k for k in REQUIRED_COMPARE_KEYS if not declared.get(k)]
    if missing:
        result['reasons'].append(
            '调用方未声明 %s：**默认不输出任何提升百分比**，因为不同硬件/版本/'
            '场景之间的数字不可比。' % ', '.join(missing))
        return result
    base_context = baseline.get('comparison_context') or {}
    mismatched = [k for k in REQUIRED_COMPARE_KEYS
                  if base_context.get(k) != declared.get(k)]
    if mismatched:
        result['reasons'].append(
            '基线声明的 %s 与当前不一致，跨条件对比没有意义。' % ', '.join(mismatched))
        return result
    result['verdict'] = 'allowed'
    return result


# --------------------------------------------------------------------------
# Markdown
# --------------------------------------------------------------------------
def render_markdown(reports, io_info, comparison):
    lines = []
    add = lines.append
    add('# Prism 离线 trace 分析报告')
    add('')
    add('- 工具：`tools/analyze_prism_trace.py` v%s' % TOOL_VERSION)
    add('- 目标 schema：v%d（字段缺失一律标 `legacy_missing`，不猜）' % SCHEMA_TARGET)
    add('- 输入（**只读**，未改写/重命名）：')
    for path in io_info['files']:
        add('  - `%s`' % path)
    add('- 物理行：%d；有效物理记录：%d；去重后事件：%d；异常行：%d'
        % (io_info['total_lines'], io_info['physical_records'],
           io_info['unique_events'], len(io_info['malformed'])))
    add('- 相同内容重复：%d；同 `(session, sequence)` 内容冲突键：%d（涉及记录 %d）；'
        '无 sequence 记录：%d（不能可靠去重）'
        % (io_info['identical_duplicates'], io_info['conflict_keys'],
           io_info['conflicting_records'], io_info['sequence_less_records']))
    add('')
    add('### 输入稳定性与精确字节口径')
    add('')
    add('| 文件 | 稳定 | 物理字节 | 物理行 | 事件正文字节 | 换行字节 | sequence 范围 |')
    add('| --- | --- | --- | --- | --- | --- | --- |')
    for item in io_info['file_reads']:
        add('| `%s` | %s | %s | %s | %s | %s | %s–%s |' % (
            item['path'], item['stable'], item['physical_bytes'],
            item['physical_lines'], item['event_line_bytes'], item['newline_bytes'],
            _shown(item['first_sequence']), _shown(item['last_sequence'])))
    byte_info = io_info['byte_accounting']
    physical = byte_info['physical']
    unique = byte_info['unique_after_dedup']
    add('- 口径：%s' % byte_info['newline_policy'])
    add('- 物理总字节=%s，其中事件正文=%s、换行=%s；去重后唯一事件正文=%s。'
        % (physical.get('physical_bytes', 0), physical.get('event_line_bytes', 0),
           physical.get('newline_bytes', 0), unique['event_line_bytes']))
    add('- 字节分类版本：v%s；物理事件正文按类=%s；去重后按类=%s。'
        % (byte_info['classification_version'], physical['event_bytes_by_class'],
           unique['event_bytes_by_class']))
    add('- 分类成员：`%s`；`value_judgement=None`，只计字节，不替用户判断价值。'
        % byte_info['classification_members'])
    if io_info['warnings']:
        add('- 输入警告：')
        for warning in io_info['warnings']:
            add('  - `%s`：%s %s' % (
                warning['kind'], warning['note'],
                ('同族文件=' + str(warning.get('siblings'))
                 if warning.get('siblings') else '')))
    if io_info['conflicts']:
        add('')
        add('### Sequence 内容冲突（确定性选择代表项，不静默覆盖）')
        for item in io_info['conflicts'][:10]:
            add('- session=`%s` sequence=%s：%s 个内容版本 / %s 条记录；位置=%s'
                % (item['session'], item['sequence'], item['variant_count'],
                   item['record_count'],
                   [variant['locations'] for variant in item['variants']]))
    if io_info['malformed']:
        add('')
        add('### 异常行（计数，不静默忽略）')
        for item in io_info['malformed'][:10]:
            add('- `%s` 第 %s 行：%s' % (item['file'], item['line'], item['error']))
    add('')
    add('> **只读声明**：本工具仅以读模式打开日志，不写入、不轮转、不重命名原始日志。')
    add('> **不混合声明**：不同 session 分节报告，跨 session 不合并时间轴/百分位/峰值。')
    add('')

    for report in reports:
        sid = report['session']
        scope = report['session_scope']
        comp = report['completeness']
        add('---')
        add('')
        add('## Session `%s`' % sid)
        add('')
        work = report['diagnostic_work']
        add('### GC、安装与保存复用观测')
        add('')
        add('- GC 已记录 %s 次，P95=%s ms，代际=%s；仅代表记录到的集合，不与父阶段相加。' % (
            work['gc']['observed_events'], work['gc']['p95_ms'], work['gc']['generations']))
        add('- 安装超预算 %s 次，P95=%s ms；阶段=%s。协作预算不是单次调用的硬上限。' % (
            work['installation']['observed_events'], work['installation']['p95_ms'], work['installation']['phases']))
        add('- 保存结果复用 %s 次；回退原因=%s。无事件不能当作命中。' % (
            len(work['save_projection']['published']), work['save_projection']['decline_reasons']))
        add('- 进程 I/O 只计算相邻兼容累计计数的差值，不代表物理磁盘读写或单个阶段成本。')
        add('')

        add('### 基本信息')
        add('')
        add('| 项 | 值 |')
        add('| --- | --- |')
        add('| pid | %s |' % (scope['pid'] if scope['pid'] is not None else '未知'))
        add('| runtime | %s |' % (
            ' / '.join('%s=%s' % (k, v) for k, v in (scope['runtime'] or {}).items())
            or '日志未记录'))
        add('| 事件总数 | %d |' % scope['total_events'])
        add('| 时间范围 | %s → %s（跨度 %s s） |' % (
            scope['time_range']['first_time'], scope['time_range']['last_time'],
            scope['time_range']['wall_span_s']))
        add('| dropped_events | %s |' % (comp['dropped_events']
            if comp['dropped_events_observed'] else '未知（未观察到计数，默认 0 不是实测）'))
        add('| session_start / session_end | %s / %s |' % (
            scope.get('has_session_start', 'unknown'),
            scope.get('has_session_end', 'unknown')))
        add('| 写入生命周期 | %s |' % comp['lifecycle']['state'])
        add('')
        add('- %s' % comp['lifecycle']['note'])
        add('- %s' % comp['counter_scope_note'])
        add('- operations 保留窗口：seq=%s；时间=%s。startup 仅补身份/边界，不补历史。' % (
            comp['retention']['operation_sequence_range'], comp['retention']['operation_time_range']))
        add('- %s' % comp['retention']['note'])
        add('')

        add('### 完整性')
        add('')
        add('- 状态：**%s**' % ('INCOMPLETE（数据不完整，不自动补成功）'
                                if comp['incomplete'] else 'COMPLETE'))
        if comp['reasons']:
            for reason in comp['reasons']:
                add('  - %s' % reason)
        else:
            add('- 未发现缺 end / 截断 / 掉队列 / 崩溃特征。')
        add('- %s' % comp['policy'])
        if comp['unterminated_operation_starts']:
            add('- 未配对 operation_start 的 seq：%s'
                % ', '.join(str(s) for s in comp['unterminated_operation_starts'][:20]))
        if comp['anomalous_line_counts']:
            add('- 异常行计数：%s' % comp['anomalous_line_counts'])
        add('')

        add('### Schema 覆盖（新增字段）')
        add('')
        legacy = report['schema_coverage']['is_legacy_log']
        add('- 日志类型：**%s**' % ('旧日志（legacy）' if legacy else 'schema v2'))
        add('- %s' % report['schema_coverage']['legacy_note'])
        add('')
        add('| 字段 | 携带事件数 | 覆盖率 | 样例值 | 状态 |')
        add('| --- | --- | --- | --- | --- |')
        for field, info in report['schema_coverage']['fields'].items():
            add('| `%s` | %d | %s%% | %s | %s |' % (
                field, info['events_carrying'], info['coverage_pct'],
                info['sample_value'] if info['sample_value'] is not None else '—',
                info['state']))
        add('')

        add('### 采样覆盖')
        add('')
        for name, info in report['sampling_coverage']['per_sampler'].items():
            dist = info['interval_ms']
            add('- `%s`：样本 %d，覆盖会话 %s%%，采样间隔 p50=%s ms / p90=%s ms（n=%d）'
                % (name, info['samples'], info['coverage_of_session_pct'],
                   dist['p50'], dist['p90'], dist['n']))
        add('- ⚠️ %s' % report['sampling_coverage']['caveat'])
        add('')

        add('### 关键结论（附原始 sequence 证据）')
        add('')
        add('| 结论 | 实测值 | 证据 seq | 说明 |')
        add('| --- | --- | --- | --- |')
        for item in report['findings']:
            if item['id'] == 'open_finalization_complete':
                add('| 打开 finalization_complete | %s ms | seq %s（phase=%s） | 墙钟 |' % (
                    item['value_ms'], item['seq'], item['phase']))
            elif item['id'] == 'slow_sql_embedded_glb':
                add('| embedded_glb 慢 SQL | %s ms，rows=%s，线程 %s | seq %s | 占打开墙钟 **%s%%** |' % (
                    item['duration_ms'], item['rows'], item['thread'], item['seq'],
                    item['share_of_open_wall_pct']))
            elif item['id'] == 'catalog_browser_read_batches':
                add('| 目录分批读取 | %d 批 / %d 行 / %d 页 | seq %s–%s | %s |' % (
                    item['batches'], item['rows_total'], item['pages_total'],
                    item['seq_range'][0], item['seq_range'][1], item['guard']))
            elif item['id'] == 'catalog_scope_miss_not_ready':
                add('| scope miss(not_ready, partial) | %d 次调用 / %d 个区间，%d 个已恢复 | seq %s 等 | %s |' % (
                    item['count'], len(item['episodes']), sum(e['recovered'] for e in item['episodes']),
                    ', '.join(str(s) for s in item['seqs'][:8]), item['interpretation']))
            elif item['id'] == 'mindmap_payload_vs_gui_stall':
                add('| 脑图 payload / 主线程 stall | payload %s；并集 %s ms（朴素相加 %s ms），stall %s ms | seq %s；stall seq %s | 父子不叠加 |' % (
                    ' + '.join('%s=%s ms' % (s['name'], s['ms'])
                                for s in item['spans']),
                    item['interval_union_ms'], item['naive_sum_ms'],
                    item['gui_stall_ms'],
                    ', '.join(str(s['seq']) for s in item['spans']),
                    item['gui_stall_seq']))
            elif item['id'] == 'save_page_patches_json_stall':
                add('| 保存前 _page_patches JSON 停顿 | %s | seq %s | 主线程前台卡顿 |' % (
                    ', '.join('%s ms' % s['ms'] for s in item['stalls'][:3]),
                    ', '.join(str(s['seq']) for s in item['stalls'][:3])))
            elif item['id'] == 'recovery_snapshot_stall':
                add('| 恢复快照 JSON 编码/写盘 | %s | seq %s | 周期性写盘落在主线程 |' % (
                    ', '.join('%s ms' % s['ms'] for s in item['events'][:3]),
                    ', '.join(str(s['seq']) for s in item['events'][:3])))
        add('')
        for item in report['findings']:
            if item.get('honesty_note'):
                add('> **诚实性声明（%s）**：%s' % (item['id'], item['honesty_note']))
                add('')
            if item.get('interpretation'):
                add('> %s：%s' % (item['id'], item['interpretation']))
                add('')
            if item.get('note'):
                add('> %s：%s' % (item['id'], item['note']))
                add('')

        errors = report.get('application_errors', {})
        for maintenance in report.get('maintenance', {}).get('events', []):
            entry = maintenance['entry']
            add('Background cleanup `%s`, seq %s: removed=%s; protected=%s; failures=%s; over_budget=%s.' % (
                entry.get('event'), entry.get('sequence'), entry.get('removed_files'),
                entry.get('protected_processes', entry.get('protected_files')),
                entry.get('failed_files', entry.get('error_type')), entry.get('over_budget')))
            add('')
        for summary in report.get('diagnostic_archive', {}).get('summaries', []):
            entry = summary['entry']
            add('Diagnostic archive: retained %s bytes; budget %s bytes; omitted %s. '
                'Significant events only, not full history.' % (
                    entry.get('retained_bytes'), entry.get('budget_bytes'), entry.get('skipped_by_kind')))
            add('')
        for crash in report.get('crash_reports', []):
            message = crash['error'].replace('|', '\\|').replace('\n', ' ')
            add('**Crash sidecar (same session):** %s — `%s`' % (message, crash['file']))
            add('')
        add('### 应用错误记录')
        add('')
        add('记录条数不等于独立失败操作数；同一次故障可能同时记录服务与界面事件。')
        add('')
        add('| 事件 | 错误 | 条数 | 首次 / 最后 sequence |')
        add('| --- | --- | --- | --- |')
        for row in errors.get('groups', []):
            message = row['error'].replace('|', '\\|').replace('\n', ' ')
            add('| `%s` | %s | %s | %s / %s |' % (
                row['event'], message, row['count'], row['first_sequence'], row['last_sequence']))
        if errors.get('overflow_records'):
            add('- 超出分组上限的错误记录：%s' % errors['overflow_records'])
        add('')
        add('### Trace / 窗口')
        add('')
        for trace in report['traces']:
            add('#### `%s`（%s）' % (trace['name'], trace['note']))
            add('')
            add('- 窗口：%s ms（seq %s → %s）'
                % (trace['timeline']['window_ms'], trace['start_seq'], trace['end_seq']))
            add('- 覆盖 span：%d 条；并集 %s ms；朴素相加 %s ms；未覆盖 %s ms' % (
                trace['timeline']['span_count'], trace['timeline']['union_all_spans_ms'],
                trace['timeline']['sum_of_span_ms'], trace['timeline']['unaccounted_ms']))
            add('')
            add('| 分桶（并集） | 耗时 | 占墙钟 | span 数 |')
            add('| --- | --- | --- | --- |')
            for bucket in trace['timeline']['buckets']:
                add('| %s | %s ms | %s%% | %d |' % (
                    bucket['bucket'], bucket['ms'], bucket['share_of_wall_pct'],
                    bucket['spans']))
            add('')
            add('阶段排名（关键路径，单条耗时；重叠见下表，**不可相加**）：')
            add('')
            add('| # | span | 名称 | ms | 线程 | 证据 seq |')
            add('| --- | --- | --- | --- | --- | --- |')
            for stage in trace['timeline']['stage_ranking'][:10]:
                add('| %d | %s | %s | %s | %s | %s |' % (
                    stage['rank'], stage['event'], stage['name'], stage['ms'],
                    stage['thread'], stage['seq']))
            add('')
            if trace['timeline']['overlaps']:
                add('重叠 span（相加会重复计时）：')
                add('')
                add('| a | b | 重叠 ms | 关系 |')
                add('| --- | --- | --- | --- |')
                for pair in trace['timeline']['overlaps'][:8]:
                    add('| %s %s (%s ms) | %s %s (%s ms) | %s | %s |' % (
                        pair['a_seq'], pair['a_name'], pair['a_ms'],
                        pair['b_seq'], pair['b_name'], pair['b_ms'],
                        pair['overlap_ms'], pair['relation']))
                add('')

        add('### 主线程 stall')
        add('')
        stalls = report['gui_stalls']
        add('- %s' % stalls['policy'])
        add('- 完整卡顿 %d 次（旧版单发 %d 次）；忽略的 ongoing 重复 %d 次；'
            '无 end 的未闭合 %d 次'
            % (stalls['complete_stalls'], stalls['legacy_single_shot_stalls'],
               stalls['ongoing_repeats_ignored'], stalls['unterminated_stalls_no_end']))
        dist = stalls['duration_distribution']
        add('- 卡顿时长分位：p50=%s / p90=%s / p99=%s ms（样本 n=%d）'
            % (dist['p50'], dist['p90'], dist['p99'], dist['n']))
        add('- 四级影响分布：%s；策略版本：%s'
            % (stalls['impact_level_counts'], stalls['impact_policy_versions']))
        add('')
        add('| 排序 | stall_id | ms | 影响 | 分类 | 关联依据 | 线程 | 采样数 | 证据 seq | 主导栈 |')
        add('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
        for index, stall in enumerate(stalls['top_stalls'][:10], 1):
            add('| %d | %s | %s | %s | %s | %s | %s | %s | %s | %s |' % (
                index, _shown(stall['stall_id']), stall['duration_ms'],
                _shown(stall['impact_level']), _shown(stall['classification']),
                _shown(stall['association_basis']), stall['thread'],
                stall['samples'], stall['seq'], (stall['top_stack'] or '')[:70]))
        add('')

        add('### 慢 SQL')
        add('')
        sql = report['slow_sql']
        add('- %s' % sql['nesting_note'])
        add('')
        add('| 事件 | 名称 | ms | rows | 线程 | 证据 seq |')
        add('| --- | --- | --- | --- | --- | --- |')
        for row in sql['sql_events'][:8]:
            add('| %s | %s | %s | %s | %s | %s |' % (
                row['event'], (row['name'] or '')[:60], row['ms'], row['rows'],
                row['thread'], row['seq']))
        add('')
        add('| project.sql.* 操作 | ms | rows | 线程 | 状态 | 证据 seq |')
        add('| --- | --- | --- | --- | --- | --- |')
        for row in sql['project_sql_operations'][:8]:
            add('| %s | %s | %s | %s | %s | %s |' % (
                row['name'], row['ms'], row['rows'], row['thread'],
                row['status'], row['seq']))
        add('')

        add('### 目录状态与计数层级')
        add('')
        catalog = report['catalog']
        add('- 目录浏览读取：%d 批 / %d 行 / %d 页，seq %s–%s'
            % (catalog['browser_read_batches'], catalog['browser_rows_total'],
               catalog['browser_pages_total'], catalog['batch_seq_range'][0],
               catalog['batch_seq_range'][1]))
        add('- ⚠️ %s' % catalog['interpretation_guard'])
        add('- scope 事件计数：%s' % catalog['scope_event_counts'])
        add('- `not_ready` + `partial=True` 次数：%d，seq %s'
            % (catalog['scope_not_ready_partial_true_count'],
               ', '.join(str(s) for s in catalog['scope_not_ready_partial_true_seqs'][:10])))
        add('')
        layers = report['count_layers']
        add('逻辑数 → 模型数 → 卡片数：')
        add('')
        add('- 逻辑读取行（可能含重复，非唯一ID）：%s' % _shown(layers['logical_rows_read']))
        add('- 卡片模型记录：%s' % (layers['cards_model_records'] or '未知'))
        add('- 旧卡片采样计数峰值（不能证明实际绘制）：%s' % _shown(layers['painted_cards_max']))
        add('- 存活素材模型数分布：p50=%s / p90=%s / p99=%s（n=%s）' % (
            layers['live_assets_distribution']['p50'],
            layers['live_assets_distribution']['p90'],
            layers['live_assets_distribution']['p99'],
            layers['live_assets_distribution']['n']))
        add('- %s' % layers['interpretation'])
        add('')

        add('### 队列与取消')
        add('')
        queue = report['queues']
        add('- material 任务：%d 个；等待 p50=%s / p90=%s ms（n=%s），最大 %s ms' % (
            queue['material_tasks_total'], queue['queue_wait_distribution']['p50'],
            queue['queue_wait_distribution']['p90'], queue['queue_wait_distribution']['n'],
            queue['queue_wait_max_ms']))
        add('- image_decode pending 峰值：%s；等待 owner 峰值：%s'
            % (queue['image_decode_pending_max'], queue['image_decode_waiting_owners_max']))
        add('- 未闭合任务：%d；被判失效（current=false）：%d；被取消的目录读取：%d'
            % (len(queue['tasks_without_finished']),
               queue['tasks_superseded_current_false'], len(queue['cancelled_catalog_reads'])))
        add('- %s' % queue['saturation_verdict'])
        add('- %s' % queue['cancellation_waste_note'])
        add('')

        add('### 资源峰值与未知项')
        add('')
        for key, info in sorted(report['resource_peaks']['peaks'].items()):
            add('- `%s` 峰值：%s（seq %s）' % (key, info['value'], info['seq']))
        add('- ⚠️ %s' % report['resource_peaks']['caveat'])
        add('- **未知项**：')
        for unknown in report['resource_peaks']['unknown_items']:
            add('  - %s' % unknown)
        add('')
        lines.extend(render_schema2_sections(report))

    if comparison is not None:
        add('---')
        add('')
        add('## 与基线对比')
        add('')
        add('- 判定：**%s**；提升百分比：%s'
            % (comparison['verdict'], comparison['improvement_pct']))
        for reason in comparison['reasons']:
            add('  - %s' % reason)
        add('- 默认规则：同硬件 + 同版本 + 同场景三样齐全才允许给出提升百分比。')
        add('')
    return '\n'.join(lines) + '\n'


def render_schema2_sections(report):
    """Human-readable evidence for new families; absent families stay absent."""
    lines = []
    add = lines.append
    chain = report['span_chain']
    if chain['has_real_chain']:
        add('### Schema 2 关联与状态')
        add('')
        add('- terminal_status_counts: `%s`' % chain['terminal_status_counts'])
        for row in chain['attribution']:
            add('- action `%s`: interval_union_ms=%s; naive_sum_ms=%s; spans=%s'
                % (row['origin_action_id'], row['interval_union_ms'],
                   row['naive_sum_ms'], row['spans']))
        add('- %s' % chain['note'])
        add('')
    dispatch = report['dispatch_intervals']
    if dispatch['dispatches']:
        add('### GUI dispatch 单调区间')
        add('')
        add('- dispatch=%s；唯一身份=%s；复用身份=%s'
            % (len(dispatch['dispatches']), dispatch['unique_ids'],
               dispatch['reused_ids']))
        add('- inclusive_sum_ms=%s；interval_union_ms=%s；exclusive_sum_ms=%s'
            % (dispatch['inclusive_sum_ms'], dispatch['interval_union_ms'],
               dispatch['exclusive_sum_ms']))
        add('- %s' % dispatch['note'])
        for row in dispatch['dispatches'][:12]:
            add('  - `%s` parent=`%s` root=`%s` depth=%s：inclusive=%s ms，'
                'direct_child_union=%s ms，exclusive=%s ms（seq %s）'
                % (row['dispatch_id'], row['parent_dispatch_id'],
                   row['root_dispatch_id'], row['depth'], row['inclusive_ms'],
                   row['direct_child_union_ms'], row['exclusive_ms'], row['seq']))
        add('')
    stall_contexts = report['stall_contexts']
    if stall_contexts['count']:
        add('### Stall 冻结窗口与终态关联')
        add('')
        add('- 分类=%s；关联=%s；未被 context 认领的终态=%s'
            % (stall_contexts['classification_counts'],
               stall_contexts['association_counts'],
               len(stall_contexts['unclaimed_terminal_stalls'])))
        for row in stall_contexts['events'][:12]:
            add('- stall_id=`%s`，seq=%s → terminal_seq=%s，依据=`%s`，'
                'classification=`%s`（%s），window=%s→%s，dispatch证据=%s，覆盖=%s'
                % (_shown(row.get('stall_id')), row.get('seq'),
                   _shown(row.get('terminal_seq')), row.get('association_basis'),
                   row.get('classification'), row.get('classification_reason'),
                   _shown(row.get('window_start_monotonic')),
                   _shown(row.get('window_end_monotonic')),
                   len(row.get('dispatch_intervals') or []),
                   len(row.get('covering_dispatches') or [])))
            evidence = row['dispatch_evidence']
            add('  - 派发快照完整=%s；状态=%s；批次证据=%s。'
                '完整仅指记录到的冻结快照，不是原生调用根因。' % (
                    evidence['complete'], evidence['reason'], len(evidence['batch_evidence'])))
        for caveat in stall_contexts['caveats']:
            add('- %s' % caveat)
        add('')
    paged = report['paged_progress']
    if paged['events']:
        add('### 分阶段分页进度')
        add('')
        add('- catalog_build=%s；scope_prepared=%s；ambiguous=%s'
            % (len(paged['catalog_build']), len(paged['scope_prepared']),
               len(paged['ambiguous'])))
        add('- %s' % paged['policy'])
        for row in paged['events'][:20]:
            add('- phase=`%s` operation_id=`%s` event=`%s` legacy=%s：'
                'processed=%s，processed_this_run=%s，total_records=%s，'
                'scope_ids=%s，pixmap/non_pixmap=%s/%s（seq %s）'
                % (row['phase'], row['operation_id'], row['event'], row['legacy'],
                   _shown(row['value']), _shown(row['processed_this_run']),
                   _shown(row['total_records']), _shown(row['scope_ids']),
                   _shown(row['pixmap_count']), _shown(row['non_pixmap_count']),
                   row['seq']))
        add('')
    queues = report['queue_rollups']
    if queues['rollup_count']:
        add('### 队列汇总：计数、等待与取消')
        add('')
        for row in queues['categories']:
            add('- `%s`: counters=%s; pending=%s; running=%s; ready=%s'
                % (row['category'], row['counters'], _shown(row['depth']['pending']),
                   _shown(row['depth']['running']), _shown(row['depth']['ready'])))
            add('  - wait_total_ms=%s; service_total_ms=%s; atomic_in_progress=%s; '
                'cancelled_remaining_work=%s; shared_tasks=%s'
                % (_shown(row['queue_wait']['total_ms']),
                   _shown(row['service']['total_ms']),
                   _shown(row['saturation']['atomic_in_progress']),
                   _shown(row['saturation']['cancelled_remaining_work']),
                   _shown(row['saturation']['shared_tasks'])))
            add('  - %s' % row['saturation']['verdict'])
        add('- 取消、开始后取消、重试等计数可能重叠，不代表不同任务数。')
        add('')
    resources = report['resource_rollups']
    if resources['rollup_count']:
        add('### 资源汇总：实测与未覆盖')
        add('')
        for row in resources['classes']:
            add('- `%s`: source=%s; current=%s; peak=%s; reason=%s'
                % (row['class'], row['source'], _shown(row['current']),
                   _shown(row['peak']), row['reason'] or '未提供'))
        add('- %s' % resources['budget_note'])
        add('- %s' % resources['pin_over_budget_note'])
        add('')
    presentation = report['presentation_markers']
    if presentation['scope_count']:
        add('### 呈现：可操作、准备完成与实际绘制')
        add('')
        for row in presentation['scopes']:
            add('- scope `%s`: first_paint_seen=%s; interactive_seen=%s'
                % (row['scope_key'], row['content_first_paint']['seen'],
                   row['scope_interactive']['seen']))
            for family in ('focus_quality', 'viewport_quality'):
                state = row[family]
                add('  - %s: state=%s; painted_marker_seen=%s; reason=%s'
                    % (family, state['state'], state['painted_marker_seen'],
                       state['reason'] or '未提供'))
        add('- %s' % presentation['policy'])
        add('')
    layered = report['presentation_evidence']
    add('### 素材呈现五层证据（D组9＋10）')
    add('')
    add(layered['policy'])
    add('')
    for row in layered['queries']:
        add('查询 `%s`：model_rows=%s，viewport_drawn=%s，'
            'current_revision_coverage=%s，content_ready=%s。判定为 `%s`。'
            '缺失或无效证据：%s。显示器最终呈现未确认。'
            % (row['query_id'], _shown(row['model_rows']),
               _shown(row['viewport_drawn']), _shown(row['current_revision_coverage']),
               _shown(row['content_ready']), row['verdict'],
               '、'.join(row['missing_or_invalid']) or '五层均有直接记录'))
        add('- layers_observed=%s；evidence_valid=%s；unique_count_complete=%s；'
            '最新开始帧闭合=%s；最近完整帧=%s。'
            % (row['layers_observed'], row['evidence_valid'], row['unique_count_complete'],
               row['latest_started_frame_closed'],
               (row['most_recent_complete_frame'] or {}).get('frame_id', '未知')))
        selection = row.get('frame_selection') or {}
        content_frame = selection.get('latest_content_frame') or {}
        started_frame = selection.get('latest_started_frame') or {}
        add('- 终态=%s；终态原因=%s；被取代=%s；取代原因=%s；'
            '发布后被取消且已呈现=%s；模型已完整发布=%s。'
            % (row.get('terminal_state'), row.get('terminal_reason') or '无',
               row.get('superseded'), row.get('superseded_reason') or '无',
               row.get('presented_before_supersede'),
               row.get('model_publication_complete')))
        add('- 取帧口径：内容帧（viewport_drawn>0）seq=%s；最新开始帧 seq=%s；'
            '内容帧 paint_clip.viewport_drawn=%s；最新开始帧 paint_clip.viewport_drawn=%s。'
            % (content_frame.get('paint_sequence') or '无',
               started_frame.get('paint_sequence') or '无',
               _shown(content_frame.get('paint_viewport_drawn')),
               _shown(started_frame.get('paint_viewport_drawn'))))
        terminal_evidence = row.get('terminal_state_evidence')
        if terminal_evidence:
            add('- 终态缺失自证：查询最后一条记录 seq=%s（%s）；会话最后一条记录 seq=%s（%s）；'
                '查询仍在飞行时会话就结束=%s。'
                % (terminal_evidence.get('query_last_sequence'),
                   terminal_evidence.get('query_last_event'),
                   terminal_evidence.get('session_last_sequence'),
                   terminal_evidence.get('session_last_event'),
                   terminal_evidence.get('query_last_is_session_tail')))
        for name in ('paint_clip', 'current_viewport'):
            counts = row[name]
            add('- %s：viewport_drawn=%s；current_revision_coverage=%s；content_ready=%s；'
                'units=%s；scope=%s；incomplete=%s。'
                % (name, _shown(counts['viewport_drawn']),
                   _shown(counts['current_revision_coverage']), _shown(counts['content_ready']),
                   _shown(counts['count_units']), counts['coverage_scope'], _shown(counts['incomplete'])))
        if row['warnings']:
            add('- 证据降级警告：%s。' % '、'.join(row['warnings']))
        for view in row['views']:
            add('- view=%s：最新开始frame=%s；闭合=%s；最近完整frame=%s。'
                % (view['view_id'], (view['latest_started_frame'] or {}).get('frame_id', '未知'),
                   view['latest_started_frame_closed'],
                   (view['most_recent_complete_frame'] or {}).get('frame_id', '未知')))
        add('')
    if layered['unlinked_events']:
        add('另有 %s 条新协议记录缺身份或版本不支持，未参与有效关联。'
            % len(layered['unlinked_events']))
        add('')
    return lines


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def guard_outputs(inputs, outputs):
    """绝不允许把产物写回输入日志。"""
    ins = set(os.path.normcase(os.path.abspath(p)) for p in inputs)
    for out in outputs:
        if not out:
            continue
        key = os.path.normcase(os.path.abspath(out))
        if key in ins:
            raise SystemExit('拒绝执行：输出路径 %s 与输入日志相同，'
                             '本工具只读输入。' % out)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='离线分析 Prism JSONL 追踪日志，输出 JSON + 中文 Markdown（只读输入）。')
    parser.add_argument('logs', nargs='+',
                        help='日志路径 / 通配符 / 目录（目录内取 *.jsonl，支持轮转文件）')
    parser.add_argument('--session', help='只分析指定 session（默认全部分节报告）')
    parser.add_argument('--json', dest='json_out', help='JSON 产物路径')
    parser.add_argument('--md', dest='md_out', help='Markdown 产物路径')
    parser.add_argument('--out-dir', help='产物目录（配合 --label 生成文件名）')
    parser.add_argument('--label', default='report', help='产物文件名标签（默认 report）')
    parser.add_argument('--baseline', help='基线 JSON，用于对比（条件不足则拒绝出百分比）')
    parser.add_argument('--compare-context',
                        help='JSON 文件，声明 hardware / app_version / scenario 三元组')
    args = parser.parse_args(argv)

    inputs = with_startup_archives(resolve_inputs(args.logs))
    if not inputs:
        parser.error('没有找到可读的日志文件：%s' % ', '.join(args.logs))

    json_out, md_out = args.json_out, args.md_out
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        json_out = json_out or str(out_dir / ('%s.json' % args.label))
        md_out = md_out or str(out_dir / ('%s.md' % args.label))
    guard_outputs(inputs, [json_out, md_out])

    reports, io_info = analyse(inputs, session_filter=args.session)

    comparison = None
    if args.baseline:
        baseline = json.loads(Path(args.baseline).read_text(encoding='utf-8'))
        context = (json.loads(Path(args.compare_context).read_text(encoding='utf-8'))
                   if args.compare_context else None)
        comparison = compare_reports(reports[0] if reports else {}, baseline, context)
        if comparison['verdict'] != 'allowed':
            sys.stderr.write(
                '对比被拒绝：%s（不输出提升百分比）\n'
                % '；'.join(comparison['reasons']))

    if json_out:
        Path(json_out).write_text(
            json.dumps({'reports': reports, 'io': io_info,
                        'comparison': comparison},
                       ensure_ascii=False, indent=2),
            encoding='utf-8')
        sys.stderr.write('已写 JSON：%s\n' % json_out)
    if md_out:
        Path(md_out).write_text(render_markdown(reports, io_info, comparison),
                                encoding='utf-8')
        sys.stderr.write('已写 Markdown：%s\n' % md_out)
    if not json_out and not md_out:
        sys.stdout.write(render_markdown(reports, io_info, comparison))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
