"""Diagnostic: who overwrites current_page_id during _reload_workspace_pages?"""
import json
import traceback
from pathlib import Path

import pytest

from tests.test_zz_deleg_b1_open_scope import fixture_q1  # reuse the fixture builder

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")


def test_diag_who_sets_current_page(main_window, qtbot, monkeypatch):
    fx = fixture_q1('first_deleted')
    window = main_window
    view = window.view

    calls = []
    real_open = window.open_workspace_page

    def spy(kind, page_id, *a, **kw):
        calls.append({'kind': kind, 'page_id': page_id,
                      'stack': [f'{f.filename}:{f.lineno} {f.name}'
                                for f in traceback.extract_stack()[:-1][-8:]]})
        return real_open(kind, page_id, *a, **kw)

    monkeypatch.setattr(window, 'open_workspace_page', spy)

    from prism import fileio
    fileio.load_prism(fx['path'], view.scene, scope=None)
    calls.clear()          # ignore the load itself
    view._finalize_loaded_project(fx['path'], errors=[])
    qtbot.wait(200)

    out = {
        'final_current_page_id': window.current_page_id,
        'final_view_canvas_id': view.current_canvas_id,
        'first_canvas_id': window._first_canvas_id(),
        'open_workspace_page_calls': calls,
    }
    (EVIDENCE / 'diag-who-sets-page.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n=== WHO SETS THE PAGE ===')
    print(json.dumps(out, ensure_ascii=False, indent=2)[:3000])