"""Explicit offline native GPU acceptance: 300 different, real CC0 PBR materials.

First run tools/fetch_material_benchmark.py. This is intentionally not CI data.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import statistics
import tempfile
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets
from prism import performance_trace
from prism.fileio.sql import SQLiteIO
from prism.material_textures import prepare_texture, prepare_render, export_originals, render_signature
from prism.widgets.material_cards import material_thumbnail
from prism.widgets.material_render import MaterialRender
from tests.test_material_cards import javascript

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'artifacts/material-tests/stress-300'
MANIFEST = ROOT / 'artifacts/material-tests/polyhaven-300/manifest.json'


def summary(values):
    ordered = sorted(values)
    return dict(count=len(values), median_ms=round(statistics.median(values), 3),
                p95_ms=round(ordered[min(len(ordered)-1, int(len(ordered)*.95))], 3),
                max_ms=round(max(values), 3))


def test_300_real_materials(main_window, qtbot, qapp):
    assert qapp.platformName() == 'windows', 'Use native Windows + production GPU settings'
    manifest = json.loads(MANIFEST.read_text(encoding='utf-8'))
    materials = manifest['materials']
    assert len(materials) == 300 and len({m['id'] for m in materials}) == 300
    OUT.mkdir(parents=True, exist_ok=True)
    window = main_window
    page = window.material_library
    service = page.records
    scene = window.view.scene
    records = []
    start = time.perf_counter()
    # Fixture assembly is not an interactive UI operation. Exercise the actual
    # service so original ownership/validation is identical to user imports.
    with QtCore.QSignalBlocker(service):
        for asset in materials:
            material = service.create(name=asset['name'])
            for slot, info in asset['maps'].items():
                source = Path(info['path'])
                assert hashlib.md5(source.read_bytes()).hexdigest() == info['md5']
                service.configure(material['id'], slot=slot, prepared=prepare_texture(source),
                                  channel='R' if slot == 'roughness' else 'RGB')
            service.configure(material['id'], normal_convention=asset['maps']['normal']['convention'],
                              tags=['Poly Haven', *asset['tags'][:3]])
            records.append(service.get(material['id']))
    report = dict(provider='Powered by Poly Haven', distinct_materials=300, originals=900,
                  fixture_assembly_seconds=round(time.perf_counter()-start, 3))
    window.resize(1600, 1000)
    window.show()
    trace = performance_trace.install(qapp, window, OUT / 'logs')
    report['log'] = str(trace.path)
    try:
        window.open_material_library()
        qtbot.waitUntil(lambda: page.materials.ready, timeout=30000)
        viewer = MaterialRender()
        qtbot.addWidget(viewer)
        viewer.resize(512, 512)
        viewer.show()
        viewer._capture_timer.setInterval(1)  # Bulk thumbnail job; no human drag debounce.
        results, errors = [], []
        viewer.completed.connect(lambda signature, blob: results.append((signature, blob)))
        viewer.failed.connect(errors.append)
        durations = []
        trace.record('benchmark_phase', name='render_all_300')
        with ThreadPoolExecutor(max_workers=1) as workers:
            for index, material in enumerate(records):
                started = time.perf_counter()
                with tempfile.TemporaryDirectory(prefix='prism-benchmark-') as directory:
                    work = workers.submit(prepare_render, material, scene.attachments, directory)
                    qtbot.waitUntil(work.done, timeout=30000)
                    before = len(results)
                    viewer.load_material(work.result(), str(index))
                    qtbot.waitUntil(lambda: len(results) > before or bool(errors), timeout=45000)
                    assert not errors, (material['name'], errors)
                    signature, blob = results[-1]
                    assert service.save_preview(material['id'], signature, blob)
                durations.append((time.perf_counter()-started)*1000)
                if (index+1) % 25 == 0:
                    print(f'GPU spheres: {index+1}/300', flush=True)
        report['bulk_material_prepare_render_capture'] = summary(durations)
        viewer.close()
        page.refresh()
        qtbot.wait(500)
        assert page.materials.count() == 300
        # Measure the former selection algorithm on the exact same 300 sphere
        # thumbnails: flush + full refresh + uncached image encode per card.
        trace.record('benchmark_phase', name='legacy_selection_300')
        baseline = []
        def uncached(record):
            # Preserve the exact former resize/re-encode algorithm, so future
            # improvements to material_thumbnail cannot make the baseline fake.
            from prism.fileio.attachments import encode_data_url
            entry = scene.attachments.get(record.get('preview', {}).get('thumbnailAttachmentId'))
            if not entry:
                return None
            image = QtGui.QImage.fromData(entry[2]).scaled(480,480,
                QtCore.Qt.AspectRatioMode.KeepAspectRatio, QtCore.Qt.TransformationMode.SmoothTransformation)
            buffer = QtCore.QBuffer()
            buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, 'PNG')
            return encode_data_url('image/png', bytes(buffer.data()))
        with patch.object(page, '_thumbnail', side_effect=uncached):
            for material in records[::100]:
                started = time.perf_counter()
                page.flush_details()
                page.selected_id = material['id']
                page.refresh()
                baseline.append((time.perf_counter()-started)*1000)
                page._auto_timer.stop()
                qtbot.wait(50)
        report['legacy_select'] = summary(baseline)
        trace.record('benchmark_phase', name='optimized_selection_300')
        select = []
        with patch.object(page.materials, 'set_records', wraps=page.materials.set_records) as rebuild:
            for material in records[::3]:
                started = time.perf_counter()
                page._selection_changed(material['id'])
                select.append((time.perf_counter()-started)*1000)
                page._auto_timer.stop()
                qtbot.wait(12)
            report['selection_catalogue_rebuilds'] = rebuild.call_count
            assert rebuild.call_count == 0
        report['select'] = summary(select)
        qtbot.waitUntil(lambda: not page._jobs, timeout=40000)
        # Full rebuilds reuse encoded thumbnails; real searches/layout/scroll
        # cross the WebChannel, rather than merely benchmarking a Python dict.
        trace.record('benchmark_phase', name='search_scroll_layout')
        searches = []
        for query in ('wood', 'stone', 'brick', 'fabric', ''):
            started = time.perf_counter()
            page.search.setText(query)
            searches.append((time.perf_counter()-started)*1000)
            qtbot.wait(120)
        report['search'] = summary(searches)
        for button in (page.list_button, page.grid_button):
            button.click()
            qtbot.wait(100)
        qtbot.waitUntil(lambda: javascript(page.materials, 'filtered.length===300&&!applying', qtbot), timeout=20000)
        for _ in range(12):
            javascript(page.materials, 'window.scrollTo(0,document.documentElement.scrollHeight)', qtbot)
            qtbot.wait(150)
        assert javascript(page.materials, "document.querySelectorAll('.asset-card').length", qtbot) == 300
        javascript(page.materials, 'window.scrollTo(0,0)', qtbot)
        page._selection_changed(records[0]['id'])
        qtbot.waitUntil(lambda: not page._jobs, timeout=40000)
        page.update_preview()
        qtbot.waitUntil(lambda: page.records.get(page.selected_id)['preview'].get('signature') == render_signature(page.records.get(page.selected_id)), timeout=40000)
        qtbot.waitUntil(lambda: page.details.inline_render.ready, timeout=20000)
        trace.record('benchmark_phase', name='live_drag_300')
        slider = page.details.slots['roughness'].slider
        drag = []
        for value in range(100, 901, 8):
            started = time.perf_counter()
            slider.setValue(value)
            drag.append((time.perf_counter()-started)*1000)
            qtbot.wait(16)
        report['drag_event'] = summary(drag)
        qtbot.waitUntil(lambda: not page.records.get(page.selected_id)['preview'].get('stale'), timeout=30000)
        # Batch export all 900 source files and compare hashes, without changing
        # names, resolution, format or channels of any original.
        trace.record('benchmark_phase', name='export_all_300')
        def export_all():
            count = 0
            for material in records:
                paths = export_originals(material, scene.attachments, OUT/'exports'/material['id'])
                assert {hashlib.sha256(Path(path).read_bytes()).hexdigest() for path in paths} == {f['sha256'] for f in material['files']}
                count += len(paths)
            return count
        with ThreadPoolExecutor(max_workers=1) as worker:
            future = worker.submit(export_all)
            qtbot.waitUntil(future.done, timeout=120000)
            report['exported_exact_originals'] = future.result()
        trace.record('benchmark_phase', name='switch_and_idle')
        for _ in range(10):
            window.tabs.setCurrentIndex(0)
            window.open_material_library()
            qtbot.wait(80)
        qtbot.wait(3500)
        window.grab().save(str(OUT/'library-300.png'))
        report['cache'] = dict(thumbnail_bytes=page._thumbnail_cache.cache.bytes,
                               icons_bytes=page._icon_cache.bytes, render_entries=len(page._render_cache))
        filename = OUT/'PolyHaven-300.prism'
        # Persist the complete test library so the user can reproduce it.
        page.store_to_scene()
        SQLiteIO(str(filename), scene, create_new=True).write()
        report['project'] = str(filename)
    finally:
        trace.close()
        (OUT/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
