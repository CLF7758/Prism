"""Does the model library leave its lease directories behind when the process ends?

Every model that is open holds a lease: a working copy of the model in the
cache folder, about 25 MB for the 100k project.  ``ModelLibraryService.close``
releases the leases in a daemon thread, so a process that ends before that
thread finishes leaves the working copies on disk until the next library open
reclaims them.

This probe runs a child process that opens a real model, closes the service
and then ends the process right away - which is what a user closing the
window looks like - and reports the working copies that survived it.

The child takes the mode as its third argument:

``wait=0``
    end the process as soon as ``close()`` returns - the behaviour before the
    waiting was added.
``wait=1``
    wait for the close thread with ``service.wait_closed(timeout=10)`` before
    the process ends - the behaviour of ``prism/__main__.py`` now.

Both runs must use the same project, the same model and the same cache, so
the leftovers can be compared directly.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CHILD = textwrap.dedent('''
    import os, sqlite3, sys, time
    from pathlib import Path
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.importer import ModelImporter
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService

    project = Path(sys.argv[1])
    cache = Path(sys.argv[2])
    wait = sys.argv[3] == '1'
    context = open_model_context(project, open_epoch=1)
    repo = ModelRepositoryAdapter(context)
    importer = ModelImporter(context, repo)
    importer.import_paths([str(project.parent / 'one.obj')])
    model_id = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with sqlite3.connect(context.metadata_path) as db:
            row = db.execute('SELECT id FROM model_records').fetchone()
        if row:
            model_id = row[0]
            break
        time.sleep(.05)
    if model_id is None:
        print('NO-MODEL')
        sys.stdout.flush()
        os._exit(1)
    bundles = ModelBundleStore(context, repo, root=str(cache))
    service = ModelLibraryService(repo, bundles=bundles,
                                  on_commit=lambda before, after: None)
    payload = repo.get_payload(model_id)
    bundle = bundles.prepare(model_id, int(payload['source_revision']))
    print('LEASED ' + bundle.package_root)
    service.close()
    if wait:
        finished = service.wait_closed(timeout=10)
        print('WAITED ' + ('yes' if finished else 'timeout'))
    else:
        print('NO-WAIT')
    sys.stdout.flush()
    # The process ends here.  Without the waiting above, the close thread is
    # still running and the working copy stays on the disk.
    os._exit(0)
''')


def _run_child(project, cache, mode):
    path = ROOT / 'tests' / '_audit_lease_child.py'
    path.write_text(CHILD, encoding='utf-8')
    try:
        return subprocess.run([sys.executable, str(path), str(project), str(cache), mode],
                              capture_output=True, text=True, timeout=180)
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _leftovers(cache):
    entries = [entry for entry in cache.iterdir() if entry.is_dir()]
    size = sum(sum(f.stat().st_size for f in entry.rglob('*') if f.is_file())
               for entry in entries)
    return entries, size


def test_the_lease_working_copy_survives_the_process(tmp_path, qapp):
    from tests.model_library.test_storage import create_project, make_scene
    project = tmp_path / 'lease.prism'
    create_project(project, make_scene())
    source = tmp_path / 'one.obj'
    source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')

    report = {}
    for mode, label in (('0', 'without-wait'), ('1', 'with-wait')):
        cache = tmp_path / ('bundles-' + label)
        cache.mkdir()
        result = _run_child(project, cache, mode)
        print(f'\nAUDIT[{label}]: 子进程输出 -> {result.stdout.strip()[:300]}', flush=True)
        if result.stderr.strip():
            print(f'AUDIT[{label}]: stderr -> {result.stderr.strip()[:400]}', flush=True)
        entries, size = _leftovers(cache)
        report[label] = (len(entries), size)
        print(f'AUDIT[{label}]: 残留租约目录 {len(entries)} 个，共 {size/1048576:.1f} MB', flush=True)
        for entry in entries:
            print('   ', entry.name, flush=True)

    before, before_size = report['without-wait']
    after, after_size = report['with-wait']
    print(f'\nAUDIT: 不等待残留 {before} 个/{before_size/1048576:.1f} MB -> '
          f'等待后残留 {after} 个/{after_size/1048576:.1f} MB', flush=True)
