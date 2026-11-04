"""How much disk does one real model leave behind when the process ends?

The small probe in ``_audit_model_lease_leak.py`` shows that a working copy
survives the process.  This one runs the same A/B against the real 100k
project, so the leftover is measured in megabytes and not in kilobytes of a
test triangle.

The child opens the real project read-only, leases the largest model into a
throwaway cache folder, closes the service and ends the process.  Mode ``0``
ends it right away, mode ``1`` waits for the close thread first.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REAL_PROJECT = Path(r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')

CHILD = textwrap.dedent('''
    import os, sys, time
    from pathlib import Path
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService

    project = Path(sys.argv[1])
    cache = Path(sys.argv[2])
    wait = sys.argv[3] == '1'
    context = open_model_context(project, open_epoch=1)
    repo = ModelRepositoryAdapter(context)
    payloads = repo.list_payloads()
    if not payloads:
        print('NO-MODEL')
        sys.stdout.flush()
        os._exit(1)
    def total(payload):
        return sum(int(item.get('size_bytes', 0) or 0)
                   for item in (payload.get('files') or []))
    payload = max(payloads, key=total)
    model_id = str(payload.get('model_id'))
    bundles = ModelBundleStore(context, repo, root=str(cache))
    service = ModelLibraryService(repo, bundles=bundles,
                                  on_commit=lambda before, after: None)
    started = time.monotonic()
    bundle = bundles.prepare(model_id, int(payload.get('source_revision', 0) or 0))
    prepared = time.monotonic() - started
    size = sum(f.stat().st_size for f in Path(bundle.package_root).rglob('*')
               if f.is_file())
    print('LEASED %.1f MB prepared=%.0fms' % (size/1048576, prepared*1000))
    service.close()
    if wait:
        finished = service.wait_closed(timeout=20)
        print('WAITED ' + ('yes' if finished else 'timeout'))
    else:
        print('NO-WAIT')
    sys.stdout.flush()
    os._exit(0)
''')


def _run_child(project, cache, mode):
    path = ROOT / 'tests' / '_audit_lease_real_child.py'
    path.write_text(CHILD, encoding='utf-8')
    try:
        return subprocess.run([sys.executable, str(path), str(project), str(cache), mode],
                              capture_output=True, text=True, timeout=300)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _measure(cache):
    entries = [entry for entry in cache.iterdir() if entry.is_dir()]
    size = sum(sum(f.stat().st_size for f in entry.rglob('*') if f.is_file())
               for entry in entries)
    return len(entries), size


def test_the_real_model_leaves_a_working_copy_behind(tmp_path, qapp):
    if not REAL_PROJECT.exists():
        print(f'AUDIT: 真实工程不存在 {REAL_PROJECT}', flush=True)
        return
    report = {}
    for mode, label in (('0', 'without-wait'), ('1', 'with-wait')):
        cache = tmp_path / ('real-' + label)
        cache.mkdir()
        result = _run_child(REAL_PROJECT, cache, mode)
        print(f'\nAUDIT[{label}]: {result.stdout.strip()[:300]}', flush=True)
        if result.stderr.strip():
            print(f'AUDIT[{label}]: stderr -> {result.stderr.strip()[:400]}', flush=True)
        count, size = _measure(cache)
        report[label] = (count, size)
        print(f'AUDIT[{label}]: 残留租约目录 {count} 个，共 {size/1048576:.1f} MB', flush=True)
    before, before_size = report['without-wait']
    after, after_size = report['with-wait']
    print(f'\nAUDIT: 不等待残留 {before} 个/{before_size/1048576:.1f} MB -> '
          f'等待后残留 {after} 个/{after_size/1048576:.1f} MB', flush=True)
