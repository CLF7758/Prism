"""Re-open the full 300-material project and verify final transport/search UI."""
import json
from pathlib import Path
import time
from unittest.mock import patch
from PyQt6 import QtGui, QtWidgets
from prism.fileio.sql import SQLiteIO
from prism import constants, performance_trace
from prism.__main__ import create_palette_from_dict
from tests.test_material_cards import javascript

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'artifacts/material-tests/stress-300/final-ui'


def test_final_300_transport_tags_search_jump(main_window, qtbot, qapp):
    assert qapp.platformName() == 'windows'
    OUT.mkdir(parents=True, exist_ok=True)
    qapp.setFont(QtGui.QFont('Microsoft YaHei',11))
    qapp.setPalette(create_palette_from_dict(constants.COLORS))
    assets = ROOT/'prism/assets'
    qss = (assets/'theme.qss').read_text(encoding='utf-8')
    qapp.setStyleSheet(qss.replace('url(assets/icons/', f'url({assets.as_posix()}/icons/'))
    window = main_window
    SQLiteIO(str(ROOT/'artifacts/material-tests/stress-300/PolyHaven-300.prism'), window.view.scene, readonly=True).read()
    window.view.category_panel._install_resource_service()
    window.search_controller.reset_project()
    window.resize(1600,1050)
    window.show()
    trace = performance_trace.install(qapp, window, OUT/'logs')
    report = {'materials':300, 'log':str(trace.path)}
    try:
        trace.record('benchmark_phase', name='cold_open_final_transport')
        start = time.perf_counter()
        window.open_material_library()
        page = window.material_library
        qtbot.waitUntil(lambda: page.materials.ready, timeout=30000)
        qtbot.waitUntil(lambda: javascript(page.materials, "document.querySelector('.card-image img')?.src.startsWith('data:')", qtbot), timeout=15000)
        report['open_until_first_real_thumbnail_ms'] = (time.perf_counter()-start)*1000
        assert page.materials.count() == 300
        records = page.records.list_records()
        trace.record('benchmark_phase', name='selection_and_switch_final')
        timings = []
        with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as rebuild:
            for material in records[::3]:
                start = time.perf_counter()
                page._selection_changed(material['id'])
                timings.append((time.perf_counter()-start)*1000)
                page._auto_timer.stop()
                qtbot.wait(10)
            assert rebuild.call_count == 0
        report['selection_median_ms'] = sorted(timings)[len(timings)//2]
        qtbot.waitUntil(lambda: not page._jobs, timeout=30000)
        for _ in range(6):
            window.tabs.setCurrentIndex(0)
            window.open_material_library()
            page._auto_timer.stop()
            qtbot.wait(120)
        trace.record('benchmark_phase', name='global_search_300_materials')
        controller = window.search_controller
        controller.input.setText('Poly Haven')
        controller.submit()
        hits = [r for r in controller.results if r['kind']=='material']
        assert len(hits) == 300
        report['global_material_hits'] = len(hits)
        qtbot.wait(350)
        window.grab().save(str(OUT/'global-search-300.png'))
        target = next(r for r in hits if r['material_id']==records[-1]['id'])
        controller.open_result(target)
        assert window.tabs.currentWidget() is page
        assert page.selected_id == records[-1]['id']
        qtbot.waitUntil(lambda: javascript(page.materials, "(()=>{const c=document.querySelector('.asset-card.selected');if(!c)return false;const r=c.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight})()", qtbot), timeout=15000)
        qtbot.waitUntil(lambda: javascript(page.materials, "document.querySelector('.asset-card.selected img').src.startsWith('data:')", qtbot), timeout=10000)
        report['jump_to_300th_visible'] = True
        # Right-side tags are editable and join the existing global tag system.
        old = page.details.tag_edit.text()
        page.details.tag_edit.setText(old+', 验收标签')
        page.flush_details()
        assert '验收标签' in page.records.get(page.selected_id)['tags']
        window.view.category_panel._browse_tag('验收标签')
        assert any(r.get('material_id')==records[-1]['id'] for r in controller.results)
        report['material_tag_search'] = True
        controller.exit()
        window.open_material_library()
        page._selection_changed(records[0]['id'])
        page.materials.set_scroll_position(0)
        qtbot.waitUntil(lambda: not page._jobs, timeout=30000)
        qtbot.wait(1500)
        window.grab().save(str(OUT/'library-300-final.png'))
    finally:
        trace.close()
        (OUT/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
