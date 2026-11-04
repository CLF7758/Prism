"""
Delegation round 2026-10-23 -- P2: the whole-project aggregation must return the
WHOLE project, not the first batch, not the current page, not the visible count.

This module tests the layer where the owner's symptom can actually come from:
the read-only catalog query.  It builds a REAL project on disk, builds a REAL
catalog cache for it with the product's own builder, and then queries it through
the product's own scope API.

Nothing is injected and no result model is patched: the ids come from the
catalog, the ground truth comes from plain SQL over the same file.

The ground truth is computed with plain SQL over the fixture, so it is
independent of the product.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_zz_deleg_b1_open_scope import expected_sets, fixture_q2

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\fix-20261023\evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)

SOURCE_STATE = None


def _build_catalog(path, cache):
    """Build a real catalog cache with the product's own builder."""
    from prism.project_catalog_index import ProjectCatalogIndex
    index = ProjectCatalogIndex(str(path), str(cache))
    result = index.build()
    return index, result


def _scope(path, cache):
    from prism.project_open_scope import ProjectOpenScope
    return ProjectOpenScope(str(path), str(cache))


def _all_ids(scope, **kwargs):
    """Every id the scope's own query yields, read through its public API."""
    out = []
    for row in scope.iter_query(max_rows=None, **kwargs):
        out.append(int(row['id']))
    return out


def test_p2_catalog_query_returns_the_whole_project(tmp_path):
    """'all' must cover the whole project -- not one batch, not one page."""
    fx = fixture_q2()
    truth = expected_sets(fx['path'])
    cache = tmp_path / 'catalog.sqlite'

    index, result = _build_catalog(fx['path'], cache)
    assert result.complete, 'the catalog build must be complete for this test to mean anything'

    scope = _scope(fx['path'], cache)
    assert scope.ready, 'the freshly built catalog must be ready'

    ids = _all_ids(scope)
    live = set(truth['all_live_ids'])

    report = {
        'fixture': fx['path'],
        'catalog_rows_built': result.total_records,
        'query_returned': len(ids),
        'ground_truth_live': len(live),
        'first_batch_would_be': 100,
    }
    assert len(ids) == len(set(ids)), 'the query must not repeat ids'
    missing = live - set(ids)
    extra = set(ids) - live
    report['missing_from_query'] = sorted(missing)[:20]
    report['extra_in_query'] = sorted(extra)[:20]
    report['missing_count'] = len(missing)
    report['extra_count'] = len(extra)
    (EVIDENCE / 'p2-whole-project-query.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    assert len(ids) == len(live), (
        f'the whole-project query returned {len(ids)} rows but the project holds '
        f'{len(live)} live assets -- the first batch ({report["first_batch_would_be"]}) '
        'or the visible count is being taken for the final total')
    assert not missing, f'{len(missing)} live assets are missing from the query'
    assert not extra, f'{len(extra)} rows in the query are not live assets'


def test_p2_scope_none_is_not_the_same_as_an_empty_scope(tmp_path):
    """scope_pages=None means "no page restriction"; an empty set means "nothing".

    The two must not be conflated: an empty scope that is treated as "no
    restriction" would answer "everything" for a query that asked for nothing.
    """
    fx = fixture_q2()
    truth = expected_sets(fx['path'])
    cache = tmp_path / 'catalog.sqlite'
    _build_catalog(fx['path'], cache)
    scope = _scope(fx['path'], cache)
    assert scope.ready

    all_ids = set(_all_ids(scope))
    empty_ids = set(_all_ids(scope, canvas_ids=set()))
    one_page = set(_all_ids(scope, canvas_ids={'cv-0'}))

    report = {
        'no_scope_count': len(all_ids),
        'empty_scope_count': len(empty_ids),
        'one_canvas_count': len(one_page),
        'ground_truth_live': truth['all_live_count'],
        'cv0_ground_truth': sum(1 for i in truth['all_live_ids']
                                if i in _canvas_of(fx['path'])),
    }
    (EVIDENCE / 'p2-scope-semantics.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    assert len(empty_ids) == 0, (
        'an explicitly empty page scope must answer "no rows", not "everything"')
    assert len(one_page) < len(all_ids), (
        'a one-canvas scope must be a strict subset of the whole project')
    assert len(all_ids) == truth['all_live_count']


def _canvas_of(path):
    """canvas id per item id, read straight from the file."""
    con = sqlite3.connect(path)
    try:
        out = {}
        for iid, data in con.execute('SELECT id, data FROM items'):
            d = json.loads(data or '{}')
            if d.get('deletedAt'):
                continue
            out[iid] = d.get('canvasId')
        return out
    finally:
        con.close()


def test_p2_untagged_and_unclassified_use_their_own_predicates(tmp_path):
    """'untagged' and 'unclassified' are DIFFERENT predicates.

    They must not be the same set, and neither may be silently replaced by
    "categories is empty".
    """
    fx = fixture_q2()
    truth = expected_sets(fx['path'])
    cache = tmp_path / 'catalog.sqlite'
    _build_catalog(fx['path'], cache)
    scope = _scope(fx['path'], cache)
    assert scope.ready

    untagged = set(_all_ids(scope, untagged_only=True))
    unclassified_truth = set(truth['unclassified_ids'])
    untagged_truth = set(truth['untagged_ids'])

    report = {
        'untagged_from_catalog': len(untagged),
        'untagged_ground_truth': len(untagged_truth),
        'unclassified_ground_truth': len(unclassified_truth),
        'same_set': untagged == unclassified_truth,
        'note': ('untagged comes from the tags field, unclassified from the '
                 'canvas bucket (default-canvas / no owner). They are different '
                 'predicates by design.'),
    }
    (EVIDENCE / 'p2-predicate-semantics.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    assert untagged == untagged_truth, (
        "the 'untagged' predicate must match the tags field exactly")
    assert untagged != unclassified_truth, (
        "'untagged' and 'unclassified' collapsed into the same set -- one of "
        'them is using the wrong predicate')
