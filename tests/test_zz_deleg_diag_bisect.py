"""Diagnostic: bisect which step of _reload_workspace_pages moves the page."""
import json
import traceback
from pathlib import Path

from tests.test_zz_deleg_b1_open_scope import fixture_q1

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")


def test_diag_bisect(main_window, qtbot):
    fx = fixture_q1('first_deleted')
    window = main_window
    view = window.view
    from prism import fileio

    fileio.load_prism(fx['path'], view.scene, scope=None)

    steps = []

    def snap(label):
        steps.append({'step': label,
                      'current_page_id': window.current_page_id,
                      'view_canvas_id': view.current_canvas_id,
                      'first_canvas_id': window._first_canvas_id()})

    snap('after load_prism')
    window.tabs.setCurrentIndex(0)
    snap('after setCurrentIndex(0)')
    window._sync_page_panels()
    snap('after _sync_page_panels')
    canvas_id = window._first_canvas_id()
    window.current_page_kind = 'canvas'
    window.current_page_id = canvas_id
    view.current_canvas_id = canvas_id
    snap('after assigning canvas_id')
    view._overview_renderer.tiles.switch_context(canvas_id)
    snap('after switch_context')
    view.category_panel.rebuild_workspace_tree()
    snap('after rebuild_workspace_tree')
    view.category_panel._apply_filter()
    snap('after _apply_filter')
    window._set_widget_after_page_store(window.canvas_page, window.tabs.currentWidget())
    snap('after _set_widget_after_page_store')
    window._sync_canvas_scope(canvas_id)
    snap('after _sync_canvas_scope')
    window._select_resource_page(canvas_id)
    snap('after _select_resource_page')

    out = {'steps': steps, 'expected': canvas_id}
    (EVIDENCE / 'diag-bisect.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n=== BISECT ===')
    for s in steps:
        mark = '  ' if s['current_page_id'] == canvas_id else '<< CHANGED'
        print('%-34s page=%-10s view=%-10s %s' % (
            s['step'], s['current_page_id'], s['view_canvas_id'], mark))