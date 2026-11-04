"""D组真实浏览器、真实delegate、版本化绘制与像素采集联合验收。"""
import base64
import importlib.util
import itertools
import json
import os
from pathlib import Path
import threading
import time

from PyQt6 import QtCore, QtGui

from prism.presentation_evidence import add_presentation_observer, remove_presentation_observer
from tests.test_catalog_browser_partial import prepare, wait_complete

ROOT = Path(__file__).resolve().parents[1]


def thumbnail():
    image = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#ed2020'))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return [32, 32, base64.b64encode(bytes(buffer.data())).decode('ascii')]


def test_real_browser_five_layers_with_actual_pixmap_and_capture(main_window, tmp_path, qtbot, monkeypatch):
    monkeypatch.setenv('PRISM_PRESENTATION_DIAGNOSTIC', '1')
    monkeypatch.setenv('PRISM_PRESENTATION_ID_BUDGET', '150000')
    events, counter, lock = [], itertools.count(1), threading.RLock()

    def observe(kind, **fields):
        with lock:
            events.append(dict(fields, event=kind, session=fields['session_id'],
                               sequence=next(counter), monotonic=time.monotonic()))

    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', title='D-real-red', imageWidth=32,
                           imageHeight=32, displayThumbnail=thumbnail(), opacity=1.0))])
    source_before = Path(scope.project_path).read_bytes()
    native = QtGui.QGuiApplication.platformName() == 'windows'
    if native:
        main_window.setAttribute(QtCore.Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        main_window.setWindowFlag(QtCore.Qt.WindowType.WindowDoesNotAcceptFocus, True)
        main_window.resize(860, 640)
        main_window.show()
        qtbot.waitUntil(lambda: main_window.windowHandle().isExposed(), timeout=10000)
    add_presentation_observer(observe)
    try:
        browser.open('all')
        wait_complete(qtbot, browser, 1)
        qtbot.waitUntil(lambda: any(value['event'] == 'asset_paint_frame'
                                   and value.get('viewport_drawn', 0) > 0
                                   for value in events), timeout=15000)
        proxy = browser.model.items[0]
        assert proxy._catalog_display_ready
        assert proxy not in list(scene.iter_assets())
        browser.list.viewport().update()
        qtbot.wait(100)
        pixels = browser.list.viewport().grab().toImage()
        capture_id = browser.list._asset_paint_capture_id
        assert capture_id
        assert not pixels.isNull()
        red_pixels = sum(pixels.pixelColor(x, y).red() > 180
                         and pixels.pixelColor(x, y).green() < 80
                         for y in range(0, pixels.height(), 4)
                         for x in range(0, pixels.width(), 4))
        assert red_pixels > 10
        query = browser.model.presentation_evidence.query_id
        # Keep the full observer sequence: filtering another query out would
        # create input sequence gaps and cannot be called a lossless capture.
        relevant = list(events)
        module_path = ROOT / 'tools/analyze_prism_trace.py'
        spec = importlib.util.spec_from_file_location('d_integrated_analyzer', module_path)
        analyzer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(analyzer)
        log = tmp_path / 'd-five-layers.jsonl'
        log.write_text(''.join(json.dumps(value, ensure_ascii=False) + '\n'
                               for value in relevant), encoding='utf-8')
        reports, _ = analyzer.analyse([str(log)])
        row = next(row for report in reports
                   for row in report['presentation_evidence']['queries'] if row['query_id'] == query)
        assert row['all_layers_observed'], row['missing_or_invalid']
        assert row['model_rows'] == 1
        assert row['viewport_drawn'] > 0
        assert row['layers']['visibility']['frame_id'] == row['layers']['paint']['frame_id'] == capture_id
        assert row['layers']['paint']['capture_id'] == capture_id
        assert row['display_confirmed'] is None
        assert Path(scope.project_path).read_bytes() == source_before
        captures = os.environ.get('PRISM_D_CAPTURE_DIR')
        if captures:
            output = Path(captures)
            assert output.is_dir()
            stem = 'D-real-browser-' + str(QtGui.QGuiApplication.platformName()) + '-' + capture_id
            image_path = output / (stem + '.png')
            assert not image_path.exists()
            assert pixels.save(str(image_path), 'PNG')
            payload = dict(qt_platform=QtGui.QGuiApplication.platformName(),
                           capture_id=capture_id, capture_boundary='QWidget.grab completed',
                           native_window_exposed=bool(native and main_window.windowHandle().isExposed()),
                           viewport_visible=browser.list.viewport().isVisible(),
                           device_pixel_ratio=browser.list.viewport().devicePixelRatioF(),
                           capture_pixels=[pixels.width(), pixels.height()],
                           physical_display_confirmed=False,
                           red_sampled_pixels=red_pixels, source_original_bytes_unchanged=True,
                           model_rows=1, query_id=query, layered_report=row)
            proof = output / (stem + '.json')
            assert not proof.exists()
            proof.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            raw = output / (stem + '.jsonl')
            assert not raw.exists()
            raw.write_bytes(log.read_bytes())
    finally:
        remove_presentation_observer(observe)
        if native:
            main_window.hide()
