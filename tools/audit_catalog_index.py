"""Bounded/resumable read-only catalog experiment, never a scene/save loader.

Run from the repository root. Source opens mode=ro, cache/report destinations
are explicit. Repeated runs resume construction instead of rescanning the head.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import sqlite3
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from prism.project_catalog_index import ProjectCatalogIndex


def identity(path):
    stat = path.stat()
    return dict(bytes=stat.st_size, modified_ns=stat.st_mtime_ns)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--cache', required=True, type=Path)
    parser.add_argument('--report', required=True, type=Path)
    parser.add_argument('--budget', type=float, default=40)
    args = parser.parse_args()
    index = ProjectCatalogIndex(args.source, args.cache)
    report_path = args.report.resolve()
    if report_path in (index.source, index.cache, Path(str(index.cache)+'.build-lock')):
        raise ValueError('Report must not overwrite source or catalog')
    before = identity(index.source)
    report = dict(source=str(index.source), source_before=before,
                  cache=str(index.cache), experiment='read_only_catalog_not_product_open',
                  precondition='OS caches not cleared; repeated runs and post-build queries may be warm')
    history = []
    if report_path.exists():
        previous = json.loads(report_path.read_text(encoding='utf-8'))
        if previous.get('source') == str(index.source):
            history = previous.get('builds', [])
    try:
        built = index.build(max_seconds=args.budget)
        history.append(asdict(built))
        report.update(builds=history, complete=built.complete)
        if built.complete:
            with sqlite3.connect(index.cache.as_uri()+'?mode=ro', uri=True) as cache:
                scopes = cache.execute('SELECT scope,total FROM counts ORDER BY scope').fetchall()
                canvas, kind = json.loads(scopes[0][0]) if scopes else (None, None)
                plan = cache.execute('EXPLAIN QUERY PLAN SELECT * FROM assets '
                                     'WHERE canvas_id IS ? AND kind=? AND id>? '
                                     'ORDER BY id LIMIT ?', (canvas, kind, 0, 128)).fetchall()
            page_times, detail_times, parse_times = [], [], []
            for _ in range(5):
                start = time.perf_counter()
                page = index.page(canvas, kind=kind, limit=128)
                page_times.append((time.perf_counter()-start)*1000)
                start = time.perf_counter()
                details = index.details([row['id'] for row in page])
                detail_times.append((time.perf_counter()-start)*1000)
                start = time.perf_counter()
                parsed = [json.loads(row['data']) for row in details]
                parse_times.append((time.perf_counter()-start)*1000)
            report.update(scope_count=len(scopes), selected_scope=[canvas, kind],
                          selected_scope_total=scopes[0][1] if scopes else 0,
                          page_count=len(page), page_payload_bytes=len(json.dumps(page).encode()),
                          detail_count=len(details),
                          detail_json_characters=sum(len(row['data'] or '') for row in details),
                          page_ms=page_times, detail_ms=detail_times,
                          detail_parse_ms=parse_times, parsed_count=len(parsed),
                          page_median_ms=statistics.median(page_times),
                          detail_median_ms=statistics.median(detail_times), query_plan=plan,
                          cache_bytes=index.cache.stat().st_size)
            # Different pages expose disk access that one repeatedly hot page
            # hides. This is not a cold-cache test and does not render images.
            sampled = []
            positions = sorted({round(i*(len(scopes)-1)/7) for i in range(8)}) if scopes else []
            for position in positions:
                selected_canvas, selected_kind = json.loads(scopes[position][0])
                start = time.perf_counter()
                selected = index.page(selected_canvas, kind=selected_kind, limit=128)
                page_ms = (time.perf_counter()-start)*1000
                start = time.perf_counter()
                selected_details = index.details([row['id'] for row in selected])
                detail_ms = (time.perf_counter()-start)*1000
                sampled.append(dict(canvas_id=selected_canvas, records=len(selected_details),
                                    page_ms=page_ms, detail_ms=detail_ms))
            report.update(sampled_scopes=sampled,
                          excludes=['Qt scene construction', 'image decoding', 'painting', 'saving'])
    except Exception as error:
        report.update(complete=False, error=repr(error), builds=history)
        raise
    finally:
        report['source_after'] = identity(index.source)
        report['source_stat_unchanged'] = report['source_after'] == before
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({key: report.get(key) for key in (
            'complete', 'error', 'source_stat_unchanged', 'cache_bytes',
            'page_median_ms', 'detail_median_ms')}, ensure_ascii=True))


if __name__ == '__main__':
    main()
