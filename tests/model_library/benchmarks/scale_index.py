"""D6: the index set at 100, 1,000, 10,000 and 100,000 records.

This measures the **index set**: many small records, the list, the query,
the sort and the save. It uses the product's own table, its own indexes and
its own query path (``ModelRepositoryAdapter.query``) against a throw-away
database -- the user's project is never touched.

What this does **not** measure, and must not be read as measured:

* the real mixed set (FBX 30 % / OBJ 20 % / GLB 30 % / glTF 20 %, at least
  70 % independent content) -- there is no 100,000 file real library;
* native loading, rendering, thumbnails, VRAM;
* the cover cache and the card DOM budget of the real UI.

Run it with::

    .venv/Scripts/python.exe tests/model_library/benchmarks/scale_index.py

The raw result is written to ``artifacts/model-library-acceptance/``.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..')))

from prism.model_library.contracts import ModelQuery  # noqa: E402
from prism.model_library.project_storage import (  # noqa: E402
    MODEL_TABLE, ModelProjectContext, connect, ensure_model_tables,
)
from prism.model_library.repository import ModelRepositoryAdapter  # noqa: E402

TIERS = (100, 1000, 10000, 100000)
FORMATS = ('fbx', 'obj', 'glb', 'gltf')
FOLDER_COUNT = 40
#: The fictional dependency set of one record: the model plus its files.
DEPENDENCY_FILES = 3


def rss_bytes():
    from prism.performance_trace import process_memory_bytes
    value = process_memory_bytes()
    return int(value) if value else None


def percentile(values, fraction):
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(fraction * (len(ordered) - 1)))))
    return round(ordered[index], 3)


def summarize(samples):
    if not samples:
        return {}
    return {
        'samples': len(samples),
        'p50_ms': percentile(samples, 0.50),
        'p95_ms': percentile(samples, 0.95),
        'p99_ms': percentile(samples, 0.99),
        'max_ms': round(max(samples), 3),
        'mean_ms': round(statistics.fmean(samples), 3),
    }


def payload_for(index):
    """One record shaped like the product's own payload."""
    model_id = 'bench-%08d' % index
    name = '模型 %08d %s' % (index, FORMATS[index % 4].upper())
    files = [{
        'file_id': 'file-%08d-%d' % (index, number),
        'relative_path': 'models/%s/%08d/file-%d.bin' % (name[:4], index, number),
        'role': 'main' if number == 0 else 'dependency',
        'size_bytes': 4096 * (number + 1),
        'sha256': '%064x' % index,
        'source_ref': 'sha256:%064x' % index,
        'missing': False,
    } for number in range(DEPENDENCY_FILES)]
    return {
        'id': model_id,
        'model_id': model_id,
        'project_id': 'benchmark-project',
        'name': name,
        'format': FORMATS[index % 4],
        'main_file_id': files[0]['file_id'],
        'source_revision': 1,
        'record_revision': 1,
        'folder_id': 'folder-%02d' % (index % FOLDER_COUNT),
        'tags': ['tag-%d' % (index % 7)],
        'notes': '基准记录 %d，标签与备注分布用于查询压力。' % index,
        'files': files,
        'appearance': {'revision': 0, 'bindings': {}, 'properties': {}},
        'statistics': {'vertices': 1000 + index % 500, 'triangles': 2000 + index % 1000,
                       'polygons': 2000 + index % 1000, 'instances': 1,
                       'basis': 'file', 'backend_version': 'bench'},
        'created_at': '2026-10-26T00:00:00',
        'updated_at': '2026-10-26T00:00:00',
        'deleted_at': None,
        'sort_order': index,
        'unknown_fields': {},
    }


def build_database(directory, count):
    """Fill a throw-away database with ``count`` records."""
    metadata = os.path.join(directory, 'bench.sqlite')
    connection = connect(metadata)
    try:
        ensure_model_tables(connection)
        connection.execute('BEGIN IMMEDIATE')
        for index in range(count):
            payload = payload_for(index)
            connection.execute(
                'INSERT INTO %s (id, payload) VALUES (?, ?)' % MODEL_TABLE,
                (payload['id'], json.dumps(payload, ensure_ascii=False)))
        connection.execute('COMMIT')
        connection.execute('ANALYZE')
    finally:
        connection.close()
    return metadata


def context_for(metadata):
    return ModelProjectContext(
        project_path=os.path.dirname(metadata),
        metadata_path=metadata,
        project_id='benchmark-project',
        storage_instance_id='benchmark-instance',
        open_epoch=1,
        session_token='benchmark-session',
        backend='legacy_sqlite',
        source_revision='bench',
        managed_root=None)


def time_calls(function, iterations):
    samples = []
    for _ in range(iterations):
        started = time.perf_counter()
        function()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def measure_tier(count, iterations, switch_iterations):
    directory = tempfile.mkdtemp(prefix='ai4-scale-')
    try:
        insert_started = time.perf_counter()
        metadata = build_database(directory, count)
        insert_ms = (time.perf_counter() - insert_started) * 1000
        size_bytes = os.path.getsize(metadata)
        repository = ModelRepositoryAdapter(context_for(metadata))
        baseline_rss = rss_bytes()
        text_query = lambda: repository.query(ModelQuery(text='模型', limit=128))
        folder_query = lambda: repository.query(
            ModelQuery(folder_id='folder-%02d' % (count % FOLDER_COUNT), limit=128))
        sort_query = lambda: repository.query(ModelQuery(sort='-name', limit=128))
        page_query = lambda: repository.query(ModelQuery(offset=count // 2, limit=64))
        result = {
            'records': count,
            'dependency_files_per_record': DEPENDENCY_FILES,
            'dependency_files_total': count * DEPENDENCY_FILES,
            'fictional_model_bytes_total': sum(
                4096 * (number + 1) for _ in range(count) for number in range(DEPENDENCY_FILES)),
            'insert_ms_total': round(insert_ms, 1),
            'insert_ms_per_record': round(insert_ms / max(1, count), 4),
            'database_bytes': size_bytes,
            'rss_baseline_bytes': baseline_rss,
            'text_query': summarize(time_calls(text_query, iterations)),
            'folder_filter': summarize(time_calls(folder_query, iterations)),
            'sort_desc': summarize(time_calls(sort_query, iterations)),
            'page_offset': summarize(time_calls(page_query, iterations)),
        }
        # The long-term trend: many switches, then the memory that is left.
        switch_texts = ['模型 %d' % (number % 1000) for number in range(switch_iterations)]
        switch_calls = iter(switch_texts)

        def switch_once():
            text = next(switch_calls)
            return repository.query(ModelQuery(text=text, limit=32))

        switches = time_calls(switch_once, switch_iterations)
        after_rss = rss_bytes()
        growth = None
        if baseline_rss and after_rss:
            growth = after_rss - baseline_rss
        result['switching'] = summarize(switches)
        result['rss_after_bytes'] = after_rss
        result['rss_growth_bytes'] = growth
        result['rss_growth_mib'] = round(growth / (1024 * 1024), 2) if growth is not None else None
        result['rss_growth_within_budget'] = (
            None if growth is None else growth <= max(baseline_rss * 0.10, 64 * 1024 * 1024))
        return result
    finally:
        shutil.rmtree(directory, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tiers', default=','.join(str(tier) for tier in TIERS))
    parser.add_argument('--iterations', type=int, default=200)
    parser.add_argument('--switch-iterations', type=int, default=1000)
    parser.add_argument('--out', default=None)
    arguments = parser.parse_args()
    tiers = [int(item) for item in arguments.tiers.split(',') if item.strip()]
    started = datetime.now(timezone.utc)
    results = []
    for count in tiers:
        print('tier %d ...' % count, flush=True)
        results.append(measure_tier(count, arguments.iterations, arguments.switch_iterations))
    payload = {
        'schema': 'model_library_scale/1',
        'generated_utc': started.isoformat(),
        'finished_utc': datetime.now(timezone.utc).isoformat(),
        'generator': 'tests/model_library/benchmarks/scale_index.py',
        'load': 'index set (many small records; no real model bytes on disk)',
        'not_measured': [
            'real mixed set at 100k records (no real library of that size exists)',
            'native loading, rendering, thumbnails, VRAM',
            'cover cache and card DOM budget in the real UI',
        ],
        'environment': {
            'python': platform.python_version(),
            'platform': platform.platform(),
            'cpu_count': os.cpu_count(),
        },
        'targets': {
            'index_warm_p95_ms': 1000,
            'index_cold_p95_ms': 3000,
            'query_p95_ms': 200,
            'query_p99_ms': 500,
            'switch_p95_ms': 100,
            'switch_p99_ms': 200,
            'rss_growth_budget': 'max(10% of baseline, 64 MiB)',
        },
        'tiers': results,
    }
    target = arguments.out
    if target is None:
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
        target = os.path.join(root, 'artifacts', 'model-library-acceptance', 'scale-index.json')
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    for tier in results:
        print('records=%-7d text_query p95=%8s ms  p99=%8s ms  switch p95=%8s ms  db=%.1f MiB  rss+%.1f MiB' % (
            tier['records'], tier['text_query'].get('p95_ms'), tier['text_query'].get('p99_ms'),
            tier['switching'].get('p95_ms'), tier['database_bytes'] / 1048576,
            tier['rss_growth_mib'] or 0.0))
    print('written:', target)
    return 0


if __name__ == '__main__':
    sys.exit(main())
