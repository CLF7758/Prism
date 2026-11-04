"""Explicit native UI navigation audit using the isolated 100k project."""
import json
import os
from pathlib import Path
import time
from unittest.mock import patch

from PyQt6 import QtCore

from prism import performance_trace


def test_real_scale_module_navigation(main_window, qtbot, qapp):
    output = Path('artifacts/full-audit').resolve()
    project = Path('E:/Prism-stress-100k/Prism-100k-v1.prism')
    window = main_window
    view = window.view
    window.resize(1440, 900)
    window.show()
    report = {'completed': False, 'project': str(project), 'stages': []}
    trace = performance_trace.install(qapp, window, output/'scale-navigation-trace')
    def persist():
        (output/'scale-module-navigation-20261021.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    def stage(name, function):
        report['current_stage'] = name
        persist()
        trace.record('navigation_audit_stage', name=name)
        start = time.perf_counter()
        result = function()
        report['stages'].append({'name': name, 'ms': round((time.perf_counter()-start)*1000, 3)})
        persist()
        return result
    try:
        stage('open_request', lambda: view.open_from_file(str(project)))
        stage('open_ready', lambda: qtbot.waitUntil(
            lambda: view.filename == str(project) and not view.worker.isRunning()
            and not getattr(view, '_project_opening', False) and view.scene.items_to_add.empty(),
            timeout=int(os.environ.get('PRISM_NAV_OPEN_TIMEOUT_MS', '120000'))))
        scene = view.scene
        assert sum(1 for _ in scene.iter_assets()) == 100000
        assert len(scene.material_records) == 5000
        docs = [p for p in scene.workspace_pages if p['kind'] == 'document']
        maps = [p for p in scene.workspace_pages if p['kind'] == 'mindmap']
        assert len(docs) == len(maps) == 500
        page = window.material_library
        with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as cards:
            stage('material_first', window._open_material_library_entry)
            assert page.materials.count() == 5000
            stage('material_browser_ready', lambda: qtbot.waitUntil(lambda: page.materials.ready, timeout=20000))
            first = cards.call_count
            stage('canvas_return', lambda: window.tabs.setCurrentWidget(window.canvas_page))
            stage('material_return_unchanged', window._open_material_library_entry)
            assert cards.call_count == first
            report['unchanged_return_rebuilds'] = cards.call_count-first
        def open_editor(kind, source):
            window.open_workspace_page(kind, source['id'])
            panel = window._page_panels[source['id']]
            qtbot.waitUntil(lambda: getattr(panel, '_ready', False)
                           and not getattr(panel, '_pending_push', False)
                           and not getattr(panel, '_tree_loading', False), timeout=30000)
            return panel
        stage('document_first', lambda: open_editor('document', docs[0]))
        stage('mindmap_first', lambda: open_editor('mindmap', maps[0]))
        stage('material_after_editors', window._open_material_library_entry)
        for number in range(3):
            stage(f'document_warm_{number}', lambda: open_editor('document', docs[0]))
            stage(f'mindmap_warm_{number}', lambda: open_editor('mindmap', maps[0]))
            stage(f'material_warm_{number}', window._open_material_library_entry)
        # Exercise all-assets query/packing too; do not substitute a small model.
        stage('all_assets', lambda: window.asset_browser.open('all'))
        assert window.asset_browser.model.rowCount() == 100000
        qtbot.wait(300)
        stage('material_after_all_assets', window._open_material_library_entry)
        report['rss_bytes'] = performance_trace.process_memory_bytes()
        report['counts'] = {'images': 100000, 'materials': 5000, 'documents': 500, 'mindmaps': 500}
        report['completed'] = True
        persist()
    finally:
        trace.close()
        # This audit never saves back to the fixture or the user's original.
        view.undo_stack.setClean()
