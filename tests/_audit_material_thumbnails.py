"""Measure the total cost of the material row previews of every real model.

Every row of the material list asks for a small sphere preview.  Each one of
them is a native render of the model's textures, so a model with a dozen
materials can take a dozen seconds to fill the list.  The eighth round moved
those renders off the interactive lane; this probe measures how much work is
left in them, model by model, on the real project.

The probe opens the real project, opens every model in the material preview,
asks for the row preview of every material and reports the measured cost per
row and in total.  It also writes the material specs to a JSON file so that
the worker side of the same code path can be measured in a second process
(``tests/_audit_material_thumbnail_worker.py``), because the desktop process
must never load the public VTK runtime next to F3D's own build.

    $env:AUDIT_SPEC_JSON='C:\\temp\\spec.json'
    $env:AUDIT_REPORT='C:\\temp\\report.json'
    $env:AUDIT_WARM_COVER='1'   # start the cover helper before measuring
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import pytest
from PyQt6 import QtWidgets

PROJECT = Path(os.environ.get(
    'PRISM_AUDIT_PROJECT',
    r'C:\Users\admin\Desktop\Prism测试\Prism-100k-v1-新版.prism')).resolve()


def _open_project(qtbot, window):
    from prism.storage.managed import database_path
    expected = Path(database_path(PROJECT)).resolve()
    window.view.open_from_file(str(PROJECT), readonly=True)

    def loading():
        worker = getattr(window.view, 'worker', None)
        return (getattr(window.view, '_project_opening', False)
                or (worker is not None and worker.isRunning()))

    def opened():
        return (not loading() and bool(window.view.filename)
                and Path(window.view.filename).resolve() == expected
                and not getattr(window.view, '_paged_preparing', False))

    qtbot.waitUntil(lambda: loading() or opened(), timeout=60000)
    qtbot.waitUntil(opened, timeout=300000)
    assert not window.view._paged_open_failed, window.view._paged_open_failed_reason


def _texture_report(spec):
    """The pictures behind one material, without decoding them."""
    from PIL import Image
    report = []
    for slot, image in sorted((spec.get('maps') or {}).items()):
        if not image or not image.get('path'):
            continue
        path = Path(image['path'])
        entry = dict(slot=slot, path=str(path), channels=image.get('channels'),
                     color_space=image.get('color_space'))
        try:
            entry['bytes'] = path.stat().st_size
            with Image.open(path) as probe:
                entry['pixels'] = list(probe.size)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            entry['error'] = str(error)[:80]
        report.append(entry)
    return report


def test_the_row_previews_of_every_real_model(main_window, qtbot, request):
    if not PROJECT.exists():
        pytest.skip(f'真实工程不存在：{PROJECT}')
    window = main_window
    window.resize(1440, 900)
    window.show()
    trace_directory = tempfile.TemporaryDirectory(prefix='prism-thumb-audit-')
    from prism.performance_trace import install
    logger = install(QtWidgets.QApplication.instance(), window, trace_directory.name)
    request.addfinalizer(trace_directory.cleanup)
    request.addfinalizer(logger.close)
    _open_project(qtbot, window)

    composition = window.model_composition
    composition.ensure()
    qtbot.waitUntil(lambda: composition.service is not None, timeout=180000)
    service = composition.service
    material = service.multimaterial

    if os.environ.get('AUDIT_WARM_COVER', '0') == '1':
        # The row previews are rendered by the cover helper.  Starting it
        # first measures the rows without the helper's own start-up.
        started = time.perf_counter()
        material.cover_controller.warm()
        print(f'\nAUDIT: 预热 cover helper 调用返回 '
              f'{(time.perf_counter()-started)*1000:.1f}ms', flush=True)
        time.sleep(float(os.environ.get('AUDIT_WARM_WAIT_S', '6')))

    from prism.model_library.contracts import ModelQuery
    from prism.model_library.multimaterial.catalog import catalog_from_payload
    from prism.model_library.multimaterial.presentation import material_spec
    records = list(service.query(ModelQuery()).items)
    assert records, '真实工程里没有模型'
    catalogue = []
    for summary in records:
        payload = service.repository.get_payload(summary.model_id) or {}
        block = payload.get('multimaterial') or {}
        reference = block.get('catalog_ref') or {}
        count = 0
        if reference:
            blob = service.repository.resources.read_all(int(reference['attachment_id']))
            count = len(catalog_from_payload(json.loads(blob)).definitions)
        catalogue.append((count, summary.model_id, service.get(summary.model_id)))
    catalogue.sort(key=lambda row: -row[0])
    print(f'\nAUDIT: 工程里 {len(catalogue)} 个模型，材质数 '
          f'{[row[0] for row in catalogue]}', flush=True)

    arrived = {}
    def on_event(event):
        if event.get('kind') == 'thumbnail' and event.get('key'):
            arrived.setdefault(event['key'], time.perf_counter())
    token = material.subscribe(on_event)

    if os.environ.get('AUDIT_COVER_LOAD', '0') == '1':
        # The model library renders one cover per model, and the covers and
        # the row pictures share the cover helper.  This is what the helper is
        # doing while the user picks a model.
        for _count, model_id, _full in catalogue:
            try:
                material.render_cover(model_id)
            except Exception as error:  # noqa: BLE001 - reported, not raised
                print(f'AUDIT: 封面请求失败 {error}', flush=True)
        print(f'\nAUDIT: 已请求 {len(catalogue)} 张封面（与行小预览共用 cover helper）', flush=True)

    summary_rows = []
    all_specs = []
    for count, model_id, full in catalogue:
        target = material.target_for(full)
        started = time.perf_counter()
        job = material.open_model(full)
        material._jobs[job].result(timeout=300)
        open_ms = (time.perf_counter() - started) * 1000
        item = material._models[model_id]
        state = item['state']
        specs = []
        for entry in state.materials:
            definition = material._resolved(item, entry.working_id, state)
            spec = material_spec(definition, item['paths'], material._root)
            # The same UV0 rule the thumbnail path uses.
            for image in spec['maps'].values():
                image['texcoord'] = 0
            specs.append(dict(working_id=entry.working_id, name=entry.display_name,
                              spec=spec, textures=_texture_report(spec)))
        pictures = {texture['path'] for entry in specs for texture in entry['textures']}
        biggest = 0
        for texture in (t for entry in specs for t in entry['textures']):
            width, height = texture.get('pixels') or (0, 0)
            biggest = max(biggest, width * height)
        # The cached sphere images would make the measurement a no-op.  They
        # are a cache: dropping them only costs the time this probe measures.
        # Every cached image of the cache folder goes, because the row
        # preview hashes its own spec and the probe cannot know that hash.
        for path in material._root.glob('*.sphere.png'):
            try:
                path.unlink()
            except OSError:
                pass
        if os.environ.get('AUDIT_CLEAR_SMALL', '0') == '1':
            # The small copies of the pictures are a cache too.  Dropping
            # them measures the first run, where they have to be made.
            for path in material._root.glob('*.small.png'):
                try:
                    path.unlink()
                except OSError:
                    pass
        print(f'AUDIT: 清空小预览缓存', flush=True)
        walls = []
        for entry in specs:
            key = 'working:' + entry['working_id']
            started = time.perf_counter()
            material.thumbnail(target, key)
            deadline = time.perf_counter() + 180
            while key not in arrived and time.perf_counter() < deadline:
                qtbot.wait(20)
            walls.append((entry['name'], (time.perf_counter() - started) * 1000))
        total = sum(row[1] for row in walls)
        summary_rows.append(dict(model_id=model_id, materials=len(specs),
                                 distinct_textures=len(pictures), biggest_texture=biggest,
                                 open_ms=round(open_ms, 1), total_ms=round(total, 1),
                                 rows=[[row[0], round(row[1], 1)] for row in walls]))
        print(f'\nAUDIT: 模型 {model_id} 材质 {len(specs)} 张贴图 {len(pictures)} '
              f'最大贴图 {biggest//1024}K 打开 {open_ms:.0f}ms '
              f'→ 行小预览合计 {total:.1f}ms', flush=True)
        for name, wall in walls:
            print(f'AUDIT:   {name[:34]:34s} {wall:8.1f}ms', flush=True)
        all_specs.append(dict(model_id=model_id, materials=specs))

    material.unsubscribe(token)
    print('\nAUDIT: 全部模型行小预览合计 '
          f'{sum(row["total_ms"] for row in summary_rows):.1f}ms', flush=True)

    logger.close()
    timings = {}
    for line in logger.path.read_text(encoding='utf-8').splitlines():
        row = json.loads(line)
        if row.get('event') == 'model_material.native.timing' and row.get('action') == 'thumbnail':
            timings.setdefault('thumbnail', []).append(
                dict(elapsed_ms=row.get('elapsed_ms'), timing=row.get('timing')))
    for row in timings.get('thumbnail', []):
        print(f"AUDIT: native thumbnail elapsed={row['elapsed_ms']:.1f}ms "
              f"timing={json.dumps(row['timing'], ensure_ascii=False)}", flush=True)

    spec_path = os.environ.get('AUDIT_SPEC_JSON')
    if spec_path:
        Path(spec_path).write_text(json.dumps(
            dict(model_id=catalogue[0][1], materials=all_specs[0]['materials']),
            ensure_ascii=False), encoding='utf-8')
    report = os.environ.get('AUDIT_REPORT')
    if report:
        Path(report).write_text(json.dumps(dict(
            models=summary_rows,
            native=timings.get('thumbnail', []),
        ), ensure_ascii=False, indent=1), encoding='utf-8')
        print(f'AUDIT: 报告已写入 {report}', flush=True)
