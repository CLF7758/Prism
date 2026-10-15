"""UI routing and source/cache contracts; real ML is verified separately."""
import sqlite3
import sys
from unittest.mock import Mock
import numpy as np
from PyQt6 import QtGui
from prism import fileio
from prism.project_search import item_key
from prism.components.visual_search_worker import Engine, source_identity, source_bytes
from prism.visual_search_service import stage_request


def add_image(view, tmp_path, name):
    image = QtGui.QImage(160, 90, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    path = tmp_path/name
    assert image.save(str(path))
    item = fileio._load_image_item(str(path))
    view.scene.addItem(item)
    return item


def test_image_search_icon_is_in_existing_input_without_loading_ml(main_window):
    controller = main_window.search_controller
    assert controller.visual_search.action in controller.input.actions()
    assert not controller.visual_search.action.icon().isNull()
    assert controller.visual_search.action.toolTip().startswith('以图搜图')
    assert 'torch' not in sys.modules and 'PyQt5' not in sys.modules


def test_image_results_use_normal_asset_canvas_and_filter_deleted(main_window, tmp_path, monkeypatch):
    view = main_window.view
    a, b = add_image(view, tmp_path, 'a.png'), add_image(view, tmp_path, 'b.png')
    controller = main_window.search_controller.visual_search
    monkeypatch.setattr(controller.service, 'search', Mock(return_value='query'))
    monkeypatch.setattr(controller.service, 'stop', Mock())
    controller.start(str(tmp_path/'a.png'))
    controller.timer.stop()
    controller.submit()
    controller.finished('query', dict(results=[dict(key=item_key(b), score=.9), dict(key=item_key(a), score=.8)],
        indexed=2, total=2, device='cpu', cached=2, failed=0))
    browser = main_window.asset_browser
    assert browser.mode == 'visual' and browser.model.items == [b, a]
    assert sorted(browser.list.cards, key=lambda c: (c.pos().y(), c.pos().x())) == browser.list.cards
    assert view._material_canvas() is browser.list
    b._deleted_at = '2026-10-15T00:00:00Z'
    browser.refresh()
    assert browser.model.items == [a]
    controller.exit()
    assert not controller.active


def test_staging_preserves_sources_and_counts_missing_assets(tmp_path, qapp):
    image = QtGui.QImage(90, 60, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('red'))
    source = tmp_path/'query.png'
    assert image.save(str(source))
    before = source.read_bytes()
    result = stage_request(tmp_path/'stage', str(source), [('a', (before, None, None)), ('missing', None)])
    import json
    from pathlib import Path
    manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
    assert len(manifest['assets']) == 2
    assert manifest['assets'][1]['unavailable']
    assert source.read_bytes() == before
    assert max(result['preview'].width(), result['preview'].height()) <= 64


def test_cached_embedding_bypasses_model_and_source_decode(tmp_path):
    path = tmp_path/'original'
    path.write_bytes(b'original source')
    engine = Engine(tmp_path/'index')
    vector = np.full(512, 1/np.sqrt(512), dtype=np.float32)
    identity = source_identity(dict(path=str(path)))
    engine.connection.execute('INSERT INTO vectors VALUES (?,?,?,?)', ('digest', vector.tobytes(), 512, 0))
    engine.connection.execute('INSERT INTO aliases VALUES (?,?)', (identity, 'digest'))
    cached, hit = engine.feature(dict(path=str(path)))
    assert hit and np.array_equal(cached, vector) and engine.model is None
    path.write_bytes(b'a changed original source')
    assert source_identity(dict(path=str(path))) != identity
    engine.connection.close()


def test_worker_reads_embedded_original_bytes_without_qt(tmp_path):
    database = tmp_path/'archive.prism'
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE sqlar(item_id INTEGER, data BLOB)')
        connection.execute('INSERT INTO sqlar VALUES (?,?)', (7, b'original bytes'))
    assert source_bytes(dict(archive=[str(database), 7])) == b'original bytes'


def test_actual_model_service_returns_results_without_ui_ml(main_window, tmp_path, qtbot, monkeypatch):
    import pytest
    from prism.visual_search_service import component_directory, source_snapshot
    if not (component_directory()/'index/models/ViT-B-32.pt').exists():
        pytest.skip('Optional local model is not installed')
    item = add_image(main_window.view, tmp_path, 'actual-model.png')
    controller = main_window.search_controller.visual_search
    from PyQt6 import QtWidgets
    monkeypatch.setattr(QtWidgets.QMessageBox, 'warning', Mock())
    results, failures = [], []
    controller.service.results.connect(lambda key, data: results.append(data))
    controller.service.failed.connect(failures.append)
    try:
        controller.start(str(tmp_path/'actual-model.png'))
        qtbot.waitUntil(lambda: bool(results or failures), timeout=90000)
        assert not failures, failures
        assert results[0]['results'][0]['key'] == item_key(item)
        assert main_window.asset_browser.mode == 'visual'
        assert item in main_window.asset_browser.model.items
        assert 'torch' not in sys.modules and 'PyQt5' not in sys.modules
    finally:
        controller.exit()
        controller.service.close()
