"""Real popup/input and same-file lazy-attachment save regressions."""
import sqlite3
import hashlib
import json
import shutil
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt6 import QtCore, QtTest

from prism.fileio.sql import SQLiteIO
from prism.fileio.attachments_store import StoredAttachments, sqlite_attachment_loader
from tests.test_material_cards import show_cards, javascript


def test_menu_is_nonblocking_from_material_library(main_window, qtbot):
    show_cards(main_window, qtbot)
    main_window._show_eagle_menu()
    assert main_window._active_eagle_menu.isVisible()
    main_window._active_eagle_menu.close()
    main_window._show_eagle_menu()
    assert main_window._active_eagle_menu.isVisible()
    main_window._active_eagle_menu.close()


def test_save_shortcut_when_canvas_hidden(main_window, qtbot, monkeypatch):
    page, _ = show_cards(main_window, qtbot)
    save = Mock()
    monkeypatch.setattr(main_window.view, 'do_save', save)
    main_window.view.filename = 'test.prism'
    page.search.setFocus()
    main_window.activateWindow()
    qtbot.wait(100)
    QtTest.QTest.keyClick(page.search, QtCore.Qt.Key.Key_S, QtCore.Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(lambda: save.called)
    assert save.call_count == 1


def test_real_right_click_opens_export_menu(main_window, qtbot):
    page, _ = show_cards(main_window, qtbot)
    grid = page.materials
    position = javascript(grid, "(()=>{const r=document.querySelector('.card-image').getBoundingClientRect();return {x:r.x+60,y:r.y+60}})()", qtbot)
    target = grid.web.focusProxy()
    point = QtCore.QPoint(round(position['x']), round(position['y']))
    QtTest.QTest.mouseClick(target, QtCore.Qt.MouseButton.RightButton, pos=point)
    qtbot.waitUntil(lambda: javascript(grid, "!!document.getElementById('material-menu')", qtbot), timeout=3000)


def test_same_file_save_keeps_clean_blobs_without_second_read_connection(main_window, tmp_path):
    path = tmp_path/'project.prism'
    scene = main_window.view.scene
    scene.attachments = {i: (f'{i}.png','image/png', bytes([i])*500_000) for i in range(1,9)}
    writer = SQLiteIO(str(path), scene, create_new=True)
    writer.write()
    writer._close_connection()
    store = StoredAttachments(loader=sqlite_attachment_loader(str(path)))
    store._meta = {i:(f'{i}.png','image/png') for i in range(1,9)}
    source = store._loader
    store._loader = Mock(side_effect=AssertionError('same-file lazy read during write'))
    store._loader.source_path = source.source_path
    store[9] = ('new.png','image/png',b'new source')
    scene.attachments = store
    writer = SQLiteIO(str(path), scene)
    writer.write()
    writer._close_connection()
    assert store.loaded_count() == 1
    with sqlite3.connect(path) as db:
        assert db.execute('select count(*) from attachments').fetchone()[0] == 9
        assert db.execute('select blob from attachments where id=8').fetchone()[0] == bytes([8])*500_000
        assert db.execute('select blob from attachments where id=9').fetchone()[0] == b'new source'


def test_save_as_streams_originals_and_rejects_missing_source(tmp_path):
    source, target = tmp_path/'source.db', tmp_path/'target.db'
    with sqlite3.connect(source) as db:
        db.execute('create table attachments (id integer primary key,blob blob)')
        db.execute('insert into attachments values (1,?)',(b'original',))
    store = StoredAttachments(loader=sqlite_attachment_loader(str(source)))
    store._meta[1] = ('color.png','image/png')
    with sqlite3.connect(target) as db:
        entries = list(store.entries_for_save(db,str(target)))
        assert entries == [(1,('color.png','image/png',b'original'))]
        assert store.loaded_count() == 0
        store._meta[2] = ('missing.png','image/png')
        with pytest.raises(ValueError,match='unreadable'):
            list(store.entries_for_save(db,str(target)))


def test_failed_read_does_not_hide_a_later_success():
    loader = Mock(side_effect=[None,b'original'])
    store = StoredAttachments(loader=loader)
    store._meta[1] = ('color.png','image/png')
    assert store[1][2] is None
    assert store[1][2] == b'original'


def test_all_300_saved_thumbnails_load_by_scrolling_without_selecting(main_window, qtbot):
    source = Path(__file__).resolve().parents[1]/'artifacts/material-tests/stress-300/PolyHaven-300.prism'
    if not source.exists():
        pytest.skip('Local 300-material fixture is not installed')
    SQLiteIO(str(source), main_window.view.scene, readonly=True).read()
    main_window.view.category_panel._install_resource_service()
    main_window.search_controller.reset_project()
    main_window.resize(1500,950)
    main_window.show()
    main_window.open_material_library()
    page = main_window.material_library
    qtbot.waitUntil(lambda: page.materials.ready, timeout=15000)
    initial = page.selected_id
    records = page.records.list_records()
    assert len(records) == 300
    for record in records:
        dom_id = page.materials._dom_id(record['id'])
        javascript(page.materials, f'prismCards.reveal("{dom_id}")', qtbot)
        expression = f'document.querySelector(\'[data-asset="{dom_id}"] img\')?.src.startsWith("data:image/png")'
        qtbot.waitUntil(lambda: javascript(page.materials, expression, qtbot), timeout=10000)
    assert page.selected_id == initial
    assert all(not a['thumbnailPending'] for a in page.materials.assets)


def test_actual_user_project_extras_save_without_loading_originals(main_window, tmp_path):
    report_path = Path(__file__).resolve().parents[1]/'artifacts/material-tests/stress-300/current-project-import.json'
    if not report_path.exists():
        pytest.skip('User project is not available')
    imported = json.loads(report_path.read_text(encoding='utf-8'))
    original = Path(imported['target'])
    target = tmp_path/'large-project-copy.prism'
    shutil.copyfile(original, target)
    scene = main_window.view.scene
    io = SQLiteIO(str(target), scene)
    io.read_board_extras()
    assert len(scene.material_records) == 332
    store = scene.attachments
    before = store.loaded_count()
    started = time.perf_counter()
    io.write_board_extras()
    elapsed = time.perf_counter()-started
    assert store.loaded_count() == before
    verified = 0
    for record in scene.material_records:
        if 'Poly Haven' not in record.get('tags', []):
            continue
        for file in record['files']:
            blob = io.fetchone('SELECT blob FROM attachments WHERE id=?', (file['attachmentId'],))[0]
            assert hashlib.sha256(blob).hexdigest() == file['sha256']
            verified += 1
    assert verified == 900
    assert io.fetchone('PRAGMA quick_check')[0] == 'ok'
    result = dict(project_bytes=target.stat().st_size,materials=332,originals_verified=verified,
                  save_extras_ms=round(elapsed*1000,2),source_blobs_loaded=store.loaded_count()-before)
    report_path.with_name('menu-save-regression.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    io._close_connection()
