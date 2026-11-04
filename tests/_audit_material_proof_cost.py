"""取证：材质"证明"机制现在还需要吗？

线上 trace 里，开工程时 GUI 线程上出现了 208 ms 的冻结，栈里有
``material_service.py:19 material_validation_fingerprint`` —— 它对 5 001 条
记录做一次 ``json.dumps`` 再哈希，就是这 96 ms 的来源。

但这个指纹的唯一作用是回答"**能不能跳过重新校验**"。如果重新校验本身
就很便宜（``validate_material_records`` 已经是零分配的遍历），那这份指纹
机制就是"校验还很贵"时代的遗留物，可以整条退场。

这个探针量三件事（都用真实工程的 5 001 条记录）：
  * 指纹（现在的机制）多少钱
  * 重新校验（零分配遍历）多少钱
  * 重新校验 + 深拷贝 多少钱

用法：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_material_proof_cost.py
"""
from __future__ import annotations

import copy
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


def timed(function, repeat=3):
    best = None
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def main():
    if not PROJECT.exists():
        print(f'真实工程不存在：{PROJECT}')
        return
    from prism.material_service import (material_validation_fingerprint,
                                        validate_material_records)
    records = real_records()
    # 校验器会核对 ``record['projectId']``，所以必须用记录里真实的 id，
    # 写死一个占位 id 只会换来一个 "材质库记录无效" 的假失败。
    project_id = records[0].get('projectId') if records else None
    if not project_id:
        print('真实材质记录缺少 projectId，无法取证')
        return
    print(f'真实材质记录：{len(records)} 条（projectId={project_id}）\n', flush=True)

    fingerprint_ms = timed(lambda: material_validation_fingerprint(records, project_id))
    validate_ms = timed(lambda: validate_material_records(records, project_id))
    copy_validate_ms = timed(lambda: validate_material_records(
        copy.deepcopy(records), project_id))

    print(f'  指纹（json.dumps + sha256）  : {fingerprint_ms:8.2f} ms', flush=True)
    print(f'  重新校验（零分配遍历）        : {validate_ms:8.2f} ms', flush=True)
    print(f'  深拷贝 + 重新校验             : {copy_validate_ms:8.2f} ms', flush=True)
    print(f'\n  指纹比重新校验贵 {fingerprint_ms/max(validate_ms, .01):.1f} 倍', flush=True)
    print('  → 指纹的唯一作用是"跳过重新校验"；'
          f'重新校验只要 {validate_ms:.1f}ms，'
          '比指纹便宜得多', flush=True)


if __name__ == '__main__':
    main()
