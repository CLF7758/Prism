"""
Delegation round 2026-10-23 -- P2: the BROWSER must publish the whole project.

The catalog query layer is already proven whole-project
(tests/test_zz_deleg_p2_whole_project_query.py).  This module covers the next
layer: the asset browser's own publish step.

It gives the browser a REAL catalog for a REAL project and lets the product's
own code path run:

    browser.refresh()
      -> _refresh_catalog_assets()
        -> CatalogQuery(scope, ...)          (background thread)
          -> _catalog_rows_ready()           (GUI thread)
            -> _install_catalog_rows()       (sliced, 4 ms per slice)

and then checks how many rows the browser actually holds.

Nothing is injected: the scope is a real ProjectOpenScope over a real catalog
cache built by the product's own builder.
"""
import json
from pathlib import Path

from tests.test_zz_deleg_b1_open_scope import expected_sets, fixture_q2

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\fix-20261023\evidence")
EVIDENCE.mkdir(parents=True, exist_ok=True)


def _prepare(tmp_path):
    """A real project, a real catalog cache and a real scope."""
    from prism.project_catalog_index import ProjectCatalogIndex
    from prism.project_open_scope import ProjectOpenScope

    fx = fixture_q2()
    cache = tmp_path / 'catalog.sqlite'
    index = ProjectCatalogIndex(str(fx['path']), str(cache))
    result = index.build()
    assert result.complete
    scope = ProjectOpenScope(str(fx['path']), str(cache))
    assert scope.ready
    return fx, scope, result


def test_p2_browser_publishes_every_row_of_the_whole_project(main_window,
                                                             qtbot, tmp_path):
    """The browser must end up holding the whole project, not one batch."""
    fx, scope, result = _prepare(tmp_path)
    truth = expected_sets(fx['path'])
    expected = truth['all_live_count']

    window = main_window
    view = window.view
    browser = window.asset_browser

    # The browser takes its whole-project scope from the view.  The project is
    # a PARTIAL one (only one page is loaded), which is the case the owner
    # reports: the loaded page holds 100 rows, the project holds far more.
    view._open_scope = scope
    view.scene._partial_project_source = str(fx['path'])

    browser.mode = 'all'
    browser.scope_pages = None          # "all" means the WHOLE project
    browser.saved_rule = None
    browser.refresh()

    # The query runs on a worker thread; the rows are installed in slices.
    qtbot.waitUntil(lambda: browser._catalog_pending is None
                    and browser._catalog_query_job is None, timeout=60000)
    qtbot.wait(300)

    published = len(browser.model.items)
    report = {
        'fixture': fx['path'],
        'catalog_rows_built': result.total_records,
        'ground_truth_live': expected,
        'browser_published': published,
        'breadcrumb': browser.breadcrumb.text(),
        'scope_pages': browser.scope_pages,
        'mode': browser.mode,
    }
    (EVIDENCE / 'p2-browser-publish.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    assert published == expected, (
        f'the browser published {published} rows but the project holds {expected} '
        'live assets -- a batch or the visible count is being taken for the total')
    assert browser.scope_pages is None, (
        "'all' must not keep a current-page restriction")
