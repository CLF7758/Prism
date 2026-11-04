"""分片合并（E-3 组）：把并发的多个记录批次合成一份**全量**报告。

为什么需要它：5,000 条材质记录逐条走真实 Babylon.js WebGL 2 路径，单条约 0.90 s，
串行约 75 分钟，超出单轮窗口。所以按 ``--offset`` 切成若干片并发跑
（每片写到**独立的 out 目录**），本脚本再把各片合成一份全量结论。

**合并规则（关键：不许把各片数字相加）**：

1. **行是唯一事实来源**。各片 JSON 里的 ``record_batch.rows`` 是唯一被信任的输入；
   各片自己算出来的汇总数字（``records`` / ``unique_source_images`` /
   ``decoded_bytes`` / ``content_groups`` / ``group_fingerprint_conflicts`` /
   ``distinct_fingerprints``）**一律不采用**——它们只是"那片自己的口径"，
   相加是错的（例如"组内不一致的组数"必须跨片重算，同一个内容组可能横跨两片）。
2. **按记录 id 去重**，同一 id 在多片出现时保留**先读到的那一行**（输入顺序即优先级，
   结果确定）。去重数会被记进合并记录。
3. **按工程的 rowid 顺序重排**合并后的行（SQLite ``mode=ro`` 打开，绝不写回），
   使合并结果与串行跑出来的行序一致；出现工程里没有的 id 会被剔除并计数。
4. **全部统计量重算**（不引用任何分片的汇总）：
   - 记录数 = 合并后的行数
   - 唯一源图数 = 全部行 ``source_attachments`` 的 **attachmentId 并集**大小
   - 唯一源图字节数 = 同一并集上按 attachmentId 去重后的字节和
   - 实际解码字节数 = 全部行 ``read_bytes`` 之和
   - 实际解码槽位数 = 全部行 ``source_attachments`` 条目数之和
   - 内容组数 = 全部行 ``render_signature`` 的不同值个数
   - 组内指纹不一致的组数 = 同一 signature 下指纹集合大小 > 1 的组数
   - 组间撞图数 = 同一个指纹出现在 **> 1 个** signature 上的指纹个数
   - 不同内容组的指纹数 = 全部行的不同指纹个数
   - 渲染失败条数 = 带 ``error`` 的行数
   - 渲染退化条数 = ``analysis.degraded`` 为真的行数
5. **覆盖度**：报告缺失的记录序号（有缺口的批次就是"部分验收"，不能报全量）。
6. 计时统计（``prepare_ms`` / ``render_ms``）由合并后的行重算，
   并在报告里**标注并发数**——分片内的读数带并发争用，不是低负载读数。

用法::

    python tools/material/merge_batches.py --project E:/Prism-stress-100k/Prism-100k-v1.prism \\
        --base artifacts/scale-acceptance/2026-10-22/material-baseline.json \\
        --inputs artifacts/scale-acceptance/2026-10-22/material-batch/shard-1/material-baseline.json \\
                 artifacts/scale-acceptance/2026-10-22/material-batch/shard-2/material-baseline.json \\
        --out artifacts/scale-acceptance/2026-10-22
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import baseline  # noqa: E402  (同目录的基线脚本：口径必须共用一份实现)


def _project_record_ids(project_path):
    """The authoritative record order: SQLite rowid order, read-only.

    ``json_extract`` keeps the scan inside SQLite; the Python fallback parses
    the payload when the SQLite build has no JSON1.
    """
    connection = sqlite3.connect(f'file:{project_path}?mode=ro', uri=True)
    try:
        try:
            rows = connection.execute(
                "SELECT json_extract(payload, '$.id') FROM material_records "
                'ORDER BY rowid').fetchall()
            ids = [row[0] for row in rows]
            if ids and all(ids):
                return ids
        except sqlite3.OperationalError:
            pass
        ids = []
        for (payload,) in connection.execute(
                'SELECT payload FROM material_records ORDER BY rowid'):
            ids.append(json.loads(payload)['id'])
        return ids
    finally:
        connection.close()


def _collect_rows(inputs):
    """Read every shard's rows.  Shard summaries are deliberately ignored."""
    rows = []
    per_input = []
    seen = {}
    duplicates = 0
    for path in inputs:
        path = Path(path)
        if not path.is_file():
            per_input.append(dict(path=str(path), present=False, rows=0, unique=0,
                                  duplicates=0, range=None))
            continue
        payload = json.loads(path.read_text(encoding='utf-8'))
        shard_rows = (payload.get('record_batch') or {}).get('rows') or []
        added = 0
        for row in shard_rows:
            key = row.get('id')
            if key in seen:
                duplicates += 1
                continue
            seen[key] = True
            rows.append(row)
            added += 1
        ids = [row.get('id') for row in shard_rows if row.get('id')]
        per_input.append(dict(
            path=str(path), present=True, rows=len(shard_rows), unique=added,
            duplicates=len(shard_rows) - added,
            range=[ids[0], ids[-1]] if ids else None,
            shards=len(payload.get('merge', {}).get('shards') or []) or None,
            write_stats=payload.get('write_stats'),
            summary_ignored={
                'records': (payload.get('record_batch') or {}).get('records'),
                'unique_source_images': (payload.get('record_batch') or {}).get('unique_source_images'),
                'decoded_bytes': (payload.get('record_batch') or {}).get('decoded_bytes'),
                'content_groups': (payload.get('record_batch') or {}).get('content_groups'),
                'group_fingerprint_conflicts': (payload.get('record_batch') or {}).get('group_fingerprint_conflicts'),
                'distinct_fingerprints': (payload.get('record_batch') or {}).get('distinct_fingerprints'),
            }))
    return rows, per_input, duplicates


def _write_stats_total(per_input):
    """Aggregate the shards' write-retry counters plus the merge's own.

    The shards are the ones that actually render, so their counters are the
    interesting ones; the merge's own writes are added on top.  Shards that
    ran before the retry fix (no ``write_stats``) contribute nothing -- they
    are reported as ``None`` in ``by_shard`` instead of being silently
    counted as zero.
    """
    totals = {'writes': 0, 'retries': 0, 'retry_successes': 0, 'failures': 0}
    by_shard = {}
    for item in per_input:
        stats = item.get('write_stats')
        if not stats:
            by_shard[item.get('path')] = None
            continue
        by_shard[item.get('path')] = dict(stats)
        for key in totals:
            totals[key] += int(stats.get(key) or 0)
    own = baseline._write_stats
    for key in totals:
        totals[key] += int(own.get(key) or 0)
    totals['last_error'] = own.get('last_error')
    totals['by_shard'] = by_shard
    totals['merge_own'] = dict(own)
    return totals


def _recompute(rows):
    """Every determinism number, recomputed from the merged rows."""
    unique = {}
    decoded_bytes = 0
    decoded_slots = 0
    groups = {}
    fingerprint_groups = {}
    failures = []
    degraded = []
    rendered = 0
    for row in rows:
        decoded_bytes += row.get('read_bytes', 0) or 0
        decoded_slots += len(row.get('source_attachments') or {}) or 0
        for attachment_id, size in (row.get('source_attachments') or {}).items():
            unique[attachment_id] = size
        analysis = row.get('analysis') or {}
        fingerprint = analysis.get('fingerprint')
        signature = row.get('signature')
        if row.get('error'):
            failures.append(row.get('id'))
        if analysis.get('degraded'):
            degraded.append(row.get('id'))
        if fingerprint:
            rendered += 1
            groups.setdefault(signature, set()).add(fingerprint)
            fingerprint_groups.setdefault(fingerprint, set()).add(signature)
    conflicts = sorted(signature for signature, prints in groups.items()
                       if len(prints) > 1)
    collisions = sorted(fingerprint for fingerprint, signatures in fingerprint_groups.items()
                         if len(signatures) > 1)
    return dict(
        records=len(rows),
        rendered=rendered,
        unique_source_images=len(unique),
        decoded_bytes=decoded_bytes,
        decoded_slots=decoded_slots,
        unique_source_bytes=sum(unique.values()),
        content_groups=len(groups),
        group_fingerprint_conflicts=len(conflicts),
        conflicting_signatures=conflicts[:20],
        distinct_fingerprints=len(fingerprint_groups),
        group_fingerprint_collisions=len(collisions),
        colliding_fingerprints=collisions[:20],
        failures=len(failures),
        failed_ids=failures[:20],
        degraded=len(degraded),
        degraded_ids=degraded[:20],
    )


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path,
                        default=Path(r'E:\Prism-stress-100k\Prism-100k-v1.prism'))
    parser.add_argument('--base', type=Path, required=True,
                        help='提供能力层 / 回归 / 环境等非批次字段的基准报告')
    parser.add_argument('--inputs', type=Path, nargs='+', required=True,
                        help='参与合并的报告 JSON（各分片 + 已跑过的批次）')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=1,
                        help='分片并发数（写进报告，读数带争用）')
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'material-baseline.json'
    md_path = out_dir / 'material-baseline.md'

    base = json.loads(Path(args.base).read_text(encoding='utf-8'))
    rows, per_input, duplicates = _collect_rows(args.inputs)

    # 重排到工程的 rowid 顺序，并算覆盖度
    ids = _project_record_ids(args.project)
    index_of = {record_id: index for index, record_id in enumerate(ids)}
    unknown = [row.get('id') for row in rows if row.get('id') not in index_of]
    known = [row for row in rows if row.get('id') in index_of]
    known.sort(key=lambda row: index_of[row['id']])
    covered = {index_of[row['id']] for row in known}
    missing = [index for index in range(len(ids)) if index not in covered]

    computed = _recompute(known)

    report = dict(base)
    report['generated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    report['record_total'] = len(ids)
    report['record_batch'] = dict(rows=known, **computed)
    report['timing'] = {
        'prepare_ms': baseline.stats([row['prepare_ms'] for row in known
                                      if 'prepare_ms' in row]),
        'render_ms': baseline.stats([row['render_ms'] for row in known
                                     if 'render_ms' in row]),
    }
    report['merge'] = dict(
        tool='tools/material/merge_batches.py',
        merged_at=report['generated_at'],
        concurrency=args.concurrency,
        inputs=per_input,
        duplicate_rows=duplicates,
        unknown_ids=unknown,
        missing_count=len(missing),
        missing_sample=missing[:20],
        covered=len(known),
        record_total=len(ids),
        rule='行是唯一事实来源；分片自带汇总一律不采用，统计量全部由合并后的行重算；'
             '按 id 去重（先读到者优先）；按工程 rowid 顺序重排；'
             '组内不一致 / 组间撞图 / 唯一源图 / 解码字节 / 退化 / 失败全部重算',
        load_snapshot=baseline._load_snapshot(),
    )
    environment = report.setdefault('environment', {})
    environment['load_snapshot'] = baseline._load_snapshot()
    environment['merge_concurrency'] = args.concurrency
    report['write_stats'] = _write_stats_total(per_input)

    baseline._refresh_derived(report)
    report['scale']['rows'] = baseline._scale_rows(report)
    baseline.write_json(json_path, report)
    baseline._atomic_write(md_path, baseline.render_markdown(report))

    # 第二遍：把**合并自己**的落盘次数也算进统计里（第一遍写的时候还没写，
    # 统计不到自己）。代价是**最后这一遍的写入本身不计入** —— 这一点在
    # 报告里如实说明，不假装计数是精确的。
    report['write_stats'] = _write_stats_total(per_input)
    baseline._refresh_derived(report)
    report['scale']['rows'] = baseline._scale_rows(report)
    baseline.write_json(json_path, report)
    baseline._atomic_write(md_path, baseline.render_markdown(report))

    print(json.dumps({
        'json': str(json_path),
        'markdown': str(md_path),
        'covered': len(known),
        'record_total': len(ids),
        'missing': len(missing),
        'duplicates_dropped': duplicates,
        'computed': {key: value for key, value in computed.items()
                     if not isinstance(value, list)},
        'status': report.get('status'),
    }, ensure_ascii=False, indent=2))
    return 0 if not missing else 2


if __name__ == '__main__':
    sys.exit(main())
