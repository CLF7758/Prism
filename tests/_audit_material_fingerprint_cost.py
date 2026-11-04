"""取证：材质指纹到底贵在哪，能不能便宜地做对。

实测：一次项目打开里 `material_validation_fingerprint` 在 GUI 线程上跑了
两次，每次约 50 ms（trace 里 208 ms 冻结的一部分）。它做的是
``json.dumps((project_id, records), sort_keys=True)`` 再 sha256 —— 5 001 条
记录先拼成一整串 JSON，再哈希。

这个探针量三件事：
  * 现在这版（整串 JSON + sha256）多少钱
  * 逐条 JSON 后折进 sha256 多少钱（不拼大串）
  * 只走一遍结构、不分配字节的遍历要多少钱

用法：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_material_fingerprint_cost.py
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def real_records():
    from prism.storage.managed import database_path
    path = Path(database_path(PROJECT))
    connection = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        rows = connection.execute(
            'SELECT payload FROM material_records ORDER BY rowid').fetchall()
    finally:
        connection.close()
    return [json.loads(payload) for (payload,) in rows]


def current(records, project_id):
    payload = json.dumps((project_id, records), sort_keys=True,
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).digest()


def per_record(records, project_id):
    digest = hashlib.sha256()
    digest.update(project_id.encode('utf-8'))
    for record in records:
        digest.update(json.dumps(record, sort_keys=True,
                                 ensure_ascii=False,
                                 separators=(',', ':')).encode('utf-8'))
    return digest.digest()


def timed(function, records, project_id, repeat=3):
    best = None
    for _ in range(repeat):
        started = time.perf_counter()
        function(records, project_id)
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def main():
    if not PROJECT.exists():
        print(f'真实工程不存在：{PROJECT}')
        return
    records = real_records()
    print(f'真实材质记录：{len(records)} 条\n', flush=True)
    current_ms = timed(current, records, 'p')
    per_record_ms = timed(per_record, records, 'p')
    print(f'  整串 JSON + sha256（现在这版）: {current_ms:8.2f} ms', flush=True)
    print(f'  逐条 JSON 折进 sha256        : {per_record_ms:8.2f} ms', flush=True)
    print(f'  快约 {current_ms/max(per_record_ms, .01):.1f} 倍', flush=True)
    print(f'  两种算法结果一致: '
          f'{current(records, "p") == per_record(records, "p")}', flush=True)


if __name__ == '__main__':
    main()
