"""Opt-in real-project sidebar publication after the formal open completes."""
import json
import os
from pathlib import Path

import pytest


@pytest.mark.skipif(not os.environ.get('PRISM_COUNTS_SOURCE'),
                    reason='Explicit real-project acceptance only')
def test_ready_project_sidebar_counts_without_manual_recount(main_window, qtbot, monkeypatch):
    from prism import view as view_module
    cache = os.environ.get('PRISM_COUNTS_CACHE')
    if cache:
        monkeypatch.setattr(view_module, '_catalog_cache_path', lambda filename: cache)
    source = Path(os.environ['PRISM_COUNTS_SOURCE']).resolve(strict=True)
    output = Path(os.environ['PRISM_COUNTS_OUTPUT'])
    output.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    view = main_window.view
    main_window.show()
    view.open_from_file(str(source), readonly=True)
    qtbot.waitUntil(lambda: view._opening_operation.terminal, timeout=15000)
    assert view._opening_operation.phase == 'ready'
    panel = view.category_panel
    result = dict(total=panel._nav_counts['all'].text(),
                  untagged=panel._nav_counts['untagged'].text(),
                  unclassified=panel._nav_counts['unclassified'].text(),
                  first_canvas_count=(panel._implicit_default_canvases or {}).get('stress-canvas-0000'),
                  second_canvas_count=(panel._implicit_default_canvases or {}).get('stress-canvas-0001'),
                  catalog_ready=view._open_scope.ready,
                  current_canvas=view.current_canvas_id,
                  installed_rows=view.scene._item_count)
    after = source.stat()
    result['source_size_mtime_unchanged'] = ((before.st_size, before.st_mtime_ns)
                                           == (after.st_size, after.st_mtime_ns))
    (output / 'counts.json').write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                       encoding='utf-8')
    main_window.grab().save(str(output / 'sidebar.png'))
    assert result['catalog_ready']
    assert result['total'] == '100,000'
    assert result['first_canvas_count'] == 100
    assert result['second_canvas_count'] == 100
    assert result['source_size_mtime_unchanged']
