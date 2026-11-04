"""取证：模型每次加载都要把 8K 贴图重新写一遍吗？

`ProjectMaterialService._load_serial` 把"工程里存着的材质贴图"解包到
``material-work`` 目录，文件名带内容摘要（``sha256_<hex>.PNG``）。但它每次
都无条件地 ``read_all`` + ``write_bytes``，于是同一张 100 MB 的 8K 贴图在
每一次模型加载时都被完整读出、完整重写。

这个探针在真实工程上量三件事：

  * 一张贴图附件读出来要多久（读的时候还会顺带校验 sha256）
  * 把它写成缓存文件要多久
  * "文件已经在了就不动"这条路径要多久

用法：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_material_resource_extract.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def timed(function, repeat=1):
    started = time.perf_counter()
    value = function()
    return (time.perf_counter() - started) * 1000, value


def main():
    if not PROJECT.exists():
        print(f'真实工程不存在：{PROJECT}')
        return
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter

    context = open_model_context(str(PROJECT), writable=False)
    repository = ModelRepositoryAdapter(context)
    print(f'工程：{PROJECT.name}  backend={context.backend}\n', flush=True)

    models = repository.list_records() if hasattr(repository, 'list_records') else []
    if not models:
        from prism.model_library.contracts import ModelQuery
        models = repository.query(ModelQuery()).items
    print(f'模型记录：{len(models)} 条', flush=True)

    def attachment_size(entry):
        size = int(entry.get('size_bytes') or 0)
        if size:
            return size
        try:
            with repository.resources.open(int(entry['attachment_id'])) as reader:
                return len(reader.read())
        except Exception:
            return 0

    # 找出资源最大的模型：按真实字节量排，不信 payload 里可能为 0 的 size。
    rows = []
    for record in models:
        payload = repository.get_payload(record.model_id)
        if payload is None:
            continue
        resources = (payload.get('multimaterial') or {}).get('resources') or {}
        files = [item for item in (payload.get('files') or []) if isinstance(item, dict)]
        resource_bytes = sum(attachment_size(entry) for entry in resources.values())
        file_bytes = sum(int(item.get('size_bytes') or 0) for item in files)
        rows.append((resource_bytes, file_bytes, record.model_id, resources, payload))
    rows.sort(key=lambda row: -(row[0] + row[1]))
    print('\n各模型体量（资源 / 原件包）：', flush=True)
    for resource_bytes, file_bytes, model_id, _resources, _payload in rows[:8]:
        print(f'   {model_id}  资源 {resource_bytes/1024/1024:8.1f} MB   '
              f'原件包 {file_bytes/1024/1024:8.1f} MB')

    resource_bytes, file_bytes, model_id, resources, payload = rows[0]
    print(f'\n最大的模型：{model_id}  资源 {resource_bytes/1024/1024:.1f} MB   '
          f'原件包 {file_bytes/1024/1024:.1f} MB', flush=True)
    for rid, entry in sorted(resources.items(),
                             key=lambda row: -attachment_size(row[1])):
        print(f"   {rid[:24]:<26} attachment={entry.get('attachment_id')} "
              f"{attachment_size(entry)/1024/1024:8.1f} MB "
              f"locator={entry.get('locator')}")

    # source_catalog_read 包裹的就是 _load_serial：三段开销分开量。
    get_ms, _ = timed(lambda: repository.get_payload(model_id))
    print(f'\nget_payload（读工程里的模型记录）：{get_ms:.1f} ms', flush=True)

    from prism.model_library.bundles import ModelBundleStore
    store = ModelBundleStore(context, repository)
    revision = int(payload.get('source_revision', 0) or 0)
    prepare_ms, bundle = timed(lambda: store.prepare(model_id, revision))
    print(f'bundles.prepare（把原件包复制进租约目录）：{prepare_ms:.1f} ms', flush=True)
    try:
        copied = sum(item.stat().st_size for item in
                     Path(bundle.package_root).rglob('*') if item.is_file())
        print(f'   复制出来的字节：{copied/1024/1024:.1f} MB')
    finally:
        try:
            store.release(bundle.lease_id)
        except Exception:
            pass

    biggest = max(resources.items(),
                  key=lambda row: attachment_size(row[1]))
    rid, entry = biggest
    attachment_id = int(entry['attachment_id'])

    read_ms, blob = timed(lambda: repository.resources.read_all(attachment_id))
    print(f'\nread_all(附件 {attachment_id})：{read_ms:.1f} ms  '
          f'{len(blob)/1024/1024:.1f} MB', flush=True)

    with tempfile.TemporaryDirectory(prefix='prism-extract-') as folder:
        target = Path(folder) / 'texture.PNG'
        write_ms, _ = timed(lambda: target.write_bytes(blob))
        print(f'write_bytes 到缓存：        {write_ms:.1f} ms', flush=True)
        stat_ms, _ = timed(lambda: target.stat().st_size)
        print(f'文件已存在时的 stat：      {stat_ms:.3f} ms', flush=True)

    print(f'\n每次模型加载，这一张贴图要多花 {read_ms+write_ms:.0f} ms；'
          f'跳过它只要 {stat_ms:.3f} ms', flush=True)

    # _load_serial 的其余几段：工程里存了什么，就量什么。
    block = payload.get('multimaterial') or {}
    print('\n--- _load_serial 的其余几段 ---', flush=True)
    catalog_ref = block.get('catalog_ref') or {}
    print(f'catalog_ref: {bool(catalog_ref)}   state: {bool(block.get("state"))}   '
          f'snapshots: {len(block.get("snapshots") or {})} 项', flush=True)

    if catalog_ref:
        cat_ms, cat_blob = timed(lambda: repository.resources.read_all(
            int(catalog_ref['attachment_id'])))
        print(f'读目录附件：{cat_ms:.1f} ms  {len(cat_blob)/1024/1024:.1f} MB')
        import hashlib
        import json
        hash_ms, _ = timed(lambda: hashlib.sha256(cat_blob).hexdigest())
        print(f'校验目录摘要：{hash_ms:.1f} ms')
        parse_ms, _ = timed(lambda: json.loads(cat_blob))
        print(f'解析目录 JSON：{parse_ms:.1f} ms')

    import copy
    deep_ms, _ = timed(lambda: copy.deepcopy(block.get('snapshots') or {}))
    print(f'deepcopy(snapshots)：{deep_ms:.1f} ms')
    deep_ms, _ = timed(lambda: copy.deepcopy(block.get('resources') or {}))
    print(f'deepcopy(resources)：{deep_ms:.1f} ms')
    if block.get('state'):
        state_bytes = len(json.dumps(block['state'], ensure_ascii=False))
        print(f'state 大小：{state_bytes/1024/1024:.1f} MB')
        decode_ms, _ = timed(lambda: json.dumps(block['state'], ensure_ascii=False))
        print(f'序列化 state（decode_state 的输入）：{decode_ms:.1f} ms')

    # 全部资源解包一遍的真实成本。
    def extract_all():
        total = 0
        for resource_id, resource in resources.items():
            if resource.get('attachment_id') is None:
                continue
            payload_bytes = repository.resources.read_all(int(resource['attachment_id']))
            total += len(payload_bytes)
        return total

    extract_ms, total_bytes = timed(extract_all)
    print(f'\n解包全部 {len(resources)} 张资源（只读不写）：'
          f'{extract_ms:.1f} ms  {total_bytes/1024/1024:.1f} MB')

    # 改法之后：文件已经在稳定缓存里时，只做一次存在性检查。
    # 应用用的是 QStandardPaths.CacheLocation/model-library/material-work。
    cache_base = (Path(os.environ['LOCALAPPDATA']) / 'Prism' / 'Prism' /
                  'cache' / 'model-library')
    work_root = cache_base / 'material-work'
    work_root.mkdir(parents=True, exist_ok=True)
    print(f'\n稳定缓存目录：{work_root}', flush=True)

    def extract_cached():
        total = 0
        for resource_id, resource in resources.items():
            output = work_root / (resource_id.replace(':', '_') +
                                  Path(resource.get('locator', '.png')).suffix)
            if output.exists():
                total += output.stat().st_size
                continue
            payload_bytes = repository.resources.read_all(int(resource['attachment_id']))
            output.write_bytes(payload_bytes)
            total += len(payload_bytes)
        return total

    first_ms, first_bytes = timed(extract_cached)
    print(f'第一次（要解包）：{first_ms:.1f} ms  {first_bytes/1024/1024:.1f} MB')
    second_ms, second_bytes = timed(extract_cached)
    print(f'第二次（已在缓存）：{second_ms:.1f} ms  {second_bytes/1024/1024:.1f} MB')
    print(f'\n同一个模型、同一个工程：{extract_ms:.0f} ms → {second_ms:.0f} ms'
          f'（省下 {extract_ms-second_ms:.0f} ms）', flush=True)


if __name__ == '__main__':
    main()
