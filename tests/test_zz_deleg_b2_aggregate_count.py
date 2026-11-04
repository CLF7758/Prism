"""
Delegation round 2026-10-23 -- Batch 2: locate where the aggregate count first
deviates from the whole-project truth.

The owner's report is "the browser only has 100 items although the project has
more".  This test does NOT assume a cause.  It builds a project with more
assets than one loaded page holds, opens it the way the product opens it, and
records the count at EVERY stage:

    A  the catalog scope state (None / not ready / ready)
    B  the catalog's own count snapshot (None means "unknown", not 0)
    C  the number of rows the browser's aggregate query received
    D  the number of rows the browser's model actually holds
    E  the sidebar's "all" counter text
    F  what the browser shows when there is nothing to show

The first stage whose value disagrees with the offline ground truth is the
deviation layer.  Nothing is injected, no result model is patched, and no
high-resolution decode is forced: the counts come from the product's own
paths.
"""
import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_zz_deleg_b1_open_scope import expected_sets, fixture_q2

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)


def _sidebar_text(panel):
    out = {}
    for key, widget in getattr(panel, '_nav_counts', {}).items():
        out[key] = widget.text() if hasattr(widget, 'text') else str(widget)
    return out


def _browser_state(browser):
    return {
        'mode': getattr(browser, 'mode', None),
        'scope_pages': (sorted(getattr(browser, 'scope_pages', None) or [])
                        if getattr(browser, 'scope_pages', None) is not None else None),
        'model_items': len(getattr(getattr(browser, 'model', None), 'items', []) or []),
        'breadcrumb': getattr(browser, 'breadcrumb', None) and browser.breadcrumb.text(),
        'summary_visible': bool(getattr(browser, 'summary', None)
                                and browser.summary.isVisible()),
        'summary_text': getattr(browser, 'summary', None) and browser.summary.text(),
        'empty_visible': bool(getattr(browser, 'empty', None)
                              and browser.empty.isVisible()),
        'empty_text': getattr(browser, 'empty', None) and browser.empty.text(),
        'list_enabled': bool(getattr(browser, 'list', None) and browser.list.isEnabled()),
    }


def _catalog_state(panel):
    catalog = getattr(panel, 'catalog', None)
    if catalog is None:
        return {'catalog': None}
    state = {'present': True}
    try:
        state['scope'] = 'available' if catalog._catalog_scope() is not None else 'unavailable'
    except Exception as exc:
        state['scope'] = f'error: {exc!r}'
    try:
        snapshot = catalog.count_snapshot()
        state['total'] = snapshot.get('total')
        state['unclassified'] = snapshot.get('unclassified')
        state['untagged'] = snapshot.get('untagged')
        state['canvases'] = snapshot.get('canvases')
        state['complete'] = snapshot.get('complete')
    except Exception as exc:
        state['snapshot_error'] = repr(exc)
    try:
        from prism.project_catalog import catalog_scope_counters
        state['scope_counters'] = catalog_scope_counters()
    except Exception as exc:
        state['counters_error'] = repr(exc)
    return state


def test_b2_locate_aggregate_count_deviation(main_window, qtbot):
    fx = fixture_q2()
    ground = expected_sets(fx['path'])
    window = main_window
    view = window.view
    report = {
        'fixture': fx['path'],
        'ground_truth': {
            'all_live_count': ground['all_live_count'],
            'untagged_count': ground['untagged_count'],
            'unclassified_count': ground['unclassified_count'],
            'deleted_count': len(ground['deleted_ids']),
        },
        'stages': [],
    }

    def record(label):
        panel = view.category_panel
        browser = window.asset_browser
        stage = {
            'stage': label,
            'partial_source': getattr(view.scene, '_partial_project_source', None),
            'loaded_items': len(list(view.scene.items())),
            'catalog': _catalog_state(panel),
            'sidebar': _sidebar_text(panel),
            'browser': _browser_state(browser),
        }
        report['stages'].append(stage)
        return stage

    # --- open the project the way the product opens it ---------------------
    from prism import fileio
    fileio.load_prism(fx['path'], view.scene, scope=None)
    view._finalize_loaded_project(fx['path'], errors=[])
    record('after open (catalog build has just been kicked off)')

    # The catalog build is asynchronous.  Give it a bounded chance to finish so
    # the READY stage is a real observation, not an assumption.
    try:
        qtbot.waitUntil(
            lambda: view.category_panel.catalog is not None
            and view.category_panel.catalog._catalog_scope() is not None,
            timeout=30000)
    except Exception as exc:
        report['catalog_never_became_ready'] = repr(exc)
    record('after catalog became ready')

    # --- the three entries the owner names ---------------------------------
    panel = view.category_panel
    for mode in ('all', 'unclassified', 'untagged'):
        try:
            panel._navigate(mode)
            qtbot.wait(400)
        except Exception as exc:
            report.setdefault('navigate_errors', {})[mode] = repr(exc)
        record(f'after navigating to {mode}')

    report['deviation'] = _first_deviation(report)
    (EVIDENCE / 'b2-aggregate-deviation.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')

    print('\n=== B2 AGGREGATE DEVIATION ===')
    print('ground truth: all=%s untagged=%s unclassified=%s deleted=%s' % (
        ground['all_live_count'], ground['untagged_count'],
        ground['unclassified_count'], len(ground['deleted_ids'])))
    for stage in report['stages']:
        cat = stage['catalog']
        br = stage['browser']
        print('%-34s scope=%-12s cat_total=%-8s model=%-5s sidebar_all=%-8s empty=%s' % (
            stage['stage'], cat.get('scope'), cat.get('total'),
            br['model_items'], stage['sidebar'].get('all'),
            (br['empty_text'] or '')[:40]))
    print('\nfirst deviation:', json.dumps(report['deviation'], ensure_ascii=False))


def _first_deviation(report):
    """Where does the count stop agreeing with the whole-project truth?"""
    truth = report['ground_truth']['all_live_count']
    for stage in report['stages']:
        cat = stage['catalog']
        br = stage['browser']
        if cat.get('scope') == 'unavailable':
            # The catalog is not ready: "unknown" is the correct answer here,
            # so a divergence at this stage is not a count bug.
            continue
        cat_total = cat.get('total')
        if cat_total is not None and cat_total != truth:
            return {'stage': stage['stage'], 'layer': 'catalog count_snapshot',
                    'value': cat_total, 'truth': truth}
        if br['model_items'] != truth and cat_total == truth:
            return {'stage': stage['stage'], 'layer': 'browser model',
                    'value': br['model_items'], 'truth': truth}
    return {'stage': None, 'layer': None, 'value': None, 'truth': truth,
            'note': 'no divergence observed in the recorded stages'}