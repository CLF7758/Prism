"""Real new media retains originals and GUI ownership through preparation."""
import sqlite3
import json

import pytest

from PyQt6 import QtCore, QtGui

from prism.items import PrismPathItem, PrismPixmapItem, PrismTextItem
from tests.test_partial_project_gui import PNG, open_canvas_a, project, rows  # noqa: F401


@pytest.mark.parametrize('managed', [False, True], ids=['legacy', 'directory'])
def test_new_text_and_path_save_with_captured_sources_and_reopen(
        main_window, tmp_path, qtbot, monkeypatch, managed):
    from prism.fileio.partial_save import item_save_columns
    from prism.source_store import read_archive

    view = main_window.view
    monkeypatch.setattr(main_window, '_store_current_page', lambda *_: True)
    original = PNG.read_bytes() + b'keep-imported-original-exactly'
    imported = PrismPixmapItem(QtGui.QImage.fromData(original))
    imported.set_source_blob(original, 'png')
    view.scene.addItem(imported)
    view.scene.addItem(PrismTextItem('existing annotation'))
    target = tmp_path / ('annotations.prismproj' if managed else 'annotations.prism')
    view.do_save(str(target), create_new=True, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=20000)
    assert view.last_save_outcome.confirmed, view.last_save_outcome
    source = view.filename
    before_items = rows(source, 'items')
    if not managed:
        from prism.partial_project import PartialProjectSession
        PartialProjectSession.open(source, item_ids=tuple(row[0] for row in before_items),
                                   page_ids=(), include_boards=True).attach(view.scene)

    text = PrismTextItem('new annotation — 新文字')
    path = PrismPathItem(points=[(0, 0), (40, 25), (70, 10)],
                         color='#42a5f5', width=3)
    generated = PrismPixmapItem(QtGui.QImage(str(PNG)))
    for item in (text, path, generated):
        view.scene.addItem(item)
    expected = [item_save_columns(item) for item in (text, path)]
    view.mark_content_dirty()
    view.do_save(view.filename, create_new=False, silent=True)
    worker = view.worker
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=20000)
    assert worker.wait(5000)
    assert view.last_save_outcome.confirmed, view.last_save_outcome
    assert all(item.save_id is not None for item in (text, path, generated))
    ids = {text.save_id, path.save_id, generated.save_id}
    assert len(ids) == 3
    assert read_archive((source, imported.save_id)) == original
    saved = {row[0]: row for row in rows(source, 'items')}
    assert len(saved) == len(before_items) + 3
    for previous in before_items:
        assert saved[previous[0]][:8] == previous[:8]
        if previous[1] == 'text':
            assert json.loads(saved[previous[0]][-1]) == json.loads(previous[-1])
    for item, columns in zip((text, path), expected):
        assert saved[item.save_id][1:-1] == columns[:-1]
        assert json.loads(saved[item.save_id][-1]) == json.loads(columns[-1])
    with sqlite3.connect(source) as connection:
        assert connection.execute('SELECT COUNT(*) FROM sqlar WHERE item_id IN (?, ?)',
                                  (text.save_id, path.save_id)).fetchone()[0] == 0
    assert not QtGui.QImage.fromData(read_archive((source, generated.save_id))).isNull()

    # Use the real project opening path, not just a writer-private receipt.
    qtbot.waitUntil(lambda: not view._file_operation_pending(), timeout=20000)
    view.open_from_file(source)
    qtbot.waitUntil(lambda: not view._project_opening and not getattr(view, '_paged_preparing', False),
                    timeout=20000)
    reopened = {item.save_id: item for item in view.scene.items_for_save()}
    for item, columns in zip((text, path), expected):
        restored = reopened[item.save_id]
        assert restored.TYPE == item.TYPE
        assert json.loads(item_save_columns(restored)[-1]) == json.loads(columns[-1])


def test_two_new_images_keep_original_bytes_and_acknowledge_real_items(project, qtbot, monkeypatch):
    session = open_canvas_a(project)
    original = PNG.read_bytes() + b'original-source-tail-not-a-reencoding'
    image = QtGui.QImage.fromData(original)
    items = [PrismPixmapItem(image), PrismPixmapItem(image)]
    for item in items:
        item.set_source_blob(original, 'png')
        item.canvas_id = 'A'
        project.scene.addItem(item)
        pos = item.pos
        def gui_pos(pos=pos):
            assert QtCore.QThread.currentThread() is project.view.thread()
            return pos()
        monkeypatch.setattr(item, 'pos', gui_pos)
        monkeypatch.setattr(item, 'original_bytes', lambda: (_ for _ in ()).throw(
            AssertionError('worker must not read the live item source')))
    before = project.source.read_bytes()
    target = project.source.parent / 'new-originals.prism'
    project.view.do_save(str(target), create_new=True, silent=True)
    worker = project.view.worker
    qtbot.waitUntil(lambda: not project.view._save_preparing, timeout=15000)
    assert worker.wait(5000)
    assert session.invalid is None
    assert project.view.filename == str(target)
    assert project.source.read_bytes() == before
    assert all(item.save_id is not None for item in items)
    assert len({item.save_id for item in items}) == 2
    with sqlite3.connect(target) as connection:
        for item in items:
            blob = connection.execute('SELECT data FROM sqlar WHERE item_id=?',
                                      (item.save_id,)).fetchone()[0]
            assert blob == original
        names = connection.execute('SELECT name FROM sqlar').fetchall()
        assert len(names) == len(set(names))
    assert all(session._residency.residents.get(item.save_id) is not None
               or item in project.scene.items_for_save() for item in items)


def test_file_backed_original_is_read_without_calling_live_item(project, qtbot, monkeypatch):
    session = open_canvas_a(project)
    original = PNG.read_bytes() + b'exact-imported-byte-tail'
    source = project.source.parent / 'retained-source.png'
    source.write_bytes(original)
    item = PrismPixmapItem(QtGui.QImage.fromData(original), 'original.png')
    item._source_blob = None
    item._source_path = str(source)
    item.canvas_id = 'A'
    project.scene.addItem(item)
    monkeypatch.setattr(item, 'original_bytes', lambda: (_ for _ in ()).throw(
        AssertionError('the private source descriptor must be used')))
    target = project.source.parent / 'source-copy.prism'
    project.view.do_save(str(target), create_new=True, silent=True)
    worker = project.view.worker
    qtbot.waitUntil(lambda: not project.view._save_preparing, timeout=15000)
    assert worker.wait(5000)
    assert session.invalid is None
    assert project.view.filename == str(target)
    assert item._source_blob is None
    with sqlite3.connect(target) as connection:
        assert connection.execute('SELECT data FROM sqlar WHERE item_id=?',
                                  (item.save_id,)).fetchone()[0] == original


def test_opening_deferred_document_during_capture_cannot_clear_saved_original(project, qtbot, monkeypatch):
    from prism.fileio.save_snapshot import BoardSaveSnapshot
    from prism.fileio.sql import load_deferred_board_extras
    original = '<p>unopened default document must survive</p>'
    with sqlite3.connect(project.source) as connection:
        connection.execute('UPDATE board_notes SET html=?', (original,))
    project.scene.note_html = ''
    project.scene._deferred_board_extras = {'note_html'}
    project.scene._board_extras_source = str(project.source)
    session = open_canvas_a(project)
    real_steps = BoardSaveSnapshot.capture_steps

    def open_document_after_body_copy(cls, scene, **kwargs):
        snapshot = yield from real_steps(scene, **kwargs)
        # Navigation loads content but makes no edit, so content revision stays
        # unchanged. The captured body and its deferred marker must stay paired.
        load_deferred_board_extras(scene, names=('note_html',))
        return snapshot

    monkeypatch.setattr(BoardSaveSnapshot, 'capture_steps',
                        classmethod(open_document_after_body_copy))
    target = project.source.parent / 'opened-during-capture.prism'
    project.view.do_save(str(target), create_new=True, silent=True)
    worker = project.view.worker
    qtbot.waitUntil(lambda: not project.view._save_preparing, timeout=15000)
    assert worker.wait(5000)
    assert session.invalid is None
    assert project.view.filename == str(target)
    with sqlite3.connect(target) as connection:
        assert connection.execute('SELECT html FROM board_notes').fetchone()[0] == original


def test_measured_large_board_capture_yields_to_gui(project, qtbot, monkeypatch, tmp_path):
    """Record measured slice/heartbeat times, not a claimed hard 4 ms ceiling."""
    import json
    import os
    import time
    from pathlib import Path
    from prism.fileio.save_preparation import SavePreparation
    from tests.test_save_preparation import large_body

    monkeypatch.setattr(project.window, '_store_current_page', lambda _panel=None: True)
    project.scene.mindmap_tree = large_body()
    slices, heartbeats = [], []
    real_drain = SavePreparation._drain

    def drain(operation):
        started = time.perf_counter()
        real_drain(operation)
        slices.append((time.perf_counter() - started) * 1000)

    monkeypatch.setattr(SavePreparation, '_drain', drain)
    timer = QtCore.QTimer(project.view)
    timer.setInterval(1)
    timer.timeout.connect(lambda: heartbeats.append(time.perf_counter()))
    target = tmp_path / 'measured-large-board.prism'
    started = time.perf_counter()
    timer.start()
    try:
        project.view.do_save(str(target), create_new=True, silent=True)
        operation, worker = project.view._save_operation, project.view.worker
        qtbot.waitUntil(lambda: project.view._save_operation is None, timeout=20000)
        assert worker.wait(5000)
    finally:
        timer.stop()
    assert operation.error is None
    assert len(slices) > 10 and len(heartbeats) > 5
    report = dict(scope='synthetic offscreen Qt; 6000 nested mind-map nodes; not user project',
        configured_slice_ms=4, configured_step_limit=256, capture_slices=len(slices),
        measured_max_capture_slice_ms=round(max(slices), 3), gui_heartbeats=len(heartbeats),
        measured_max_heartbeat_gap_ms=round(max(
            (b-a)*1000 for a, b in zip(heartbeats, heartbeats[1:])), 3),
        total_save_ms=round((time.perf_counter()-started)*1000, 3))
    output = Path(os.environ.get('PRISM_SAVE_BENCHMARK_OUTPUT', str(tmp_path / 'capture-measurements.json')))
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
