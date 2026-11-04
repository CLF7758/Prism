import json
import subprocess
import sys
import time
import threading
from pathlib import Path
from unittest.mock import Mock

from PyQt6 import QtCore, QtGui
from prism import image_decode, performance_trace
from prism.resource_budget import ProjectScope
from tests.test_asset_layout_contract import canvas


def test_scoped_reader_release_receives_real_path(qapp, tmp_path, monkeypatch, caplog):
    image = QtGui.QImage(16, 16, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    source = tmp_path/'source.png'
    assert image.save(str(source))
    scope = ProjectScope(str(tmp_path/'isolated'))
    removed = []
    original = image_decode._remove_file
    def remove(path):
        assert isinstance(path, str)
        removed.append((path, Path(path).exists()))
        original(path)
    monkeypatch.setattr(image_decode, '_remove_file', remove)
    with image_decode.native_reader(blob=source.read_bytes(), scope=scope) as reader:
        path = Path(reader.fileName())
        assert path.exists() and reader.read().size() == image.size()
    assert removed == [(str(path), True)]
    assert not path.exists()
    assert scope.release_leases() == 0
    assert 'Could not release decode_temp' not in caplog.text


def test_video_materialization_lease_releases_real_path(qapp, tmp_path, monkeypatch, caplog):
    from prism import source_store, video_thumbnails
    extracted = tmp_path/'disposable-video'
    extracted.write_bytes(b'test extraction only')
    monkeypatch.setattr(source_store, 'retain_archive_video', lambda *args: str(extracted))
    scope = ProjectScope(str(tmp_path/'scope'))
    job = video_thumbnails.MaterializeVideo('test', ('not-read.prism', 1), '.mp4',
                                           threading.Event(), scope=scope)
    job.run()
    assert extracted.exists()
    assert scope.release_leases() == 1
    assert not extracted.exists()
    assert 'Could not release video_extract' not in caplog.text


def test_large_relayout_keeps_active_index_and_latest_camera(qapp, qtbot, monkeypatch):
    owner, host, model = canvas(qapp, 5001)
    try:
        qtbot.waitUntil(lambda: host._build_state is None, timeout=10000)
        old_cards, old_index = host.cards, host._spatial
        card = host.cards[0]
        old_rect = card.sceneBoundingRect()
        monkeypatch.setattr(host, '_arrange_contents', Mock(side_effect=AssertionError('synchronous layout')))
        host.selectionModel().select(model.index(0), QtCore.QItemSelectionModel.SelectionFlag.Select)
        host.arrange(preserve_view=True)
        first = host._build_state
        assert first['keep_layout'] and host.cards is old_cards and host._spatial is old_index
        assert card in host.visible_cards(old_rect)
        host.arrange(preserve_view=True)
        assert first['cancel'].is_set()
        # Interaction during asynchronous preparation must survive commit.
        host.scale(1.3, 1.3)
        transform = host.transform()
        qtbot.waitUntil(lambda: host._build_state is None, timeout=10000)
        assert host.cards is not old_cards and len(host.cards) == 5001
        assert host.transform() == transform
        assert host.cards[0].isSelected()
        assert len(host._moved_rows) == 5001
    finally:
        if host._build_state:
            host._build_state['cancel'].set()
        host._build_state = None
        owner.close()


def test_metadata_change_invalidates_pending_layout(qapp, qtbot):
    owner, host, model = canvas(qapp, 5001)
    try:
        qtbot.waitUntil(lambda: host._build_state is None, timeout=10000)
        host.arrange(preserve_view=True)
        pending = host._build_state
        model.items[0].crop = QtCore.QRectF(0,0,32,128)
        host.catalog_rows_changed([0])
        assert pending['cancel'].is_set()
        qtbot.waitUntil(lambda: host._build_state is None, timeout=10000)
        assert host.cards[0]._size.height() > host.cards[0]._size.width()
    finally:
        if host._build_state:
            host._build_state['cancel'].set()
        host._build_state = None
        owner.close()


def test_manual_move_retires_pending_layout(qapp, qtbot):
    owner, host, model = canvas(qapp, 5001)
    try:
        qtbot.waitUntil(lambda: host._build_state is None, timeout=10000)
        host.arrange(preserve_view=True)
        pending = host._build_state
        host._metadata_layout_timer = QtCore.QTimer(host)
        host._metadata_layout_timer.start(1000)
        host.cards[0].setPos(123, 456)
        assert not host._metadata_layout_timer.isActive()
        assert pending['cancel'].is_set() and host._build_state is None
        host._layout_prepared((pending, ([(0,0)]*5001, (100,100)), None))
        assert host.cards[0].pos() == QtCore.QPointF(123,456)
    finally:
        if host._build_state:
            host._build_state['cancel'].set()
        host._build_state = None
        owner.close()


def test_gc_generation_is_not_project_generation(main_window, qapp, tmp_path, monkeypatch):
    trace = performance_trace.install(qapp, main_window, tmp_path)
    try:
        calls = []
        monkeypatch.setattr(trace, 'record', lambda event, **fields: calls.append((event, fields)))
        info = dict(generation=2, collected=7, uncollectable=0)
        # The watcher stores ``(started, context, trigger_thread)`` in
        # ``_gc_started`` and the stop phase unpacks that triple.  Poking a
        # bare number into the dict raised inside the callback, where the
        # guard swallowed it, and the collection was never recorded at all.
        trace._gc_callback('start', info)
        started, context, trigger = trace._gc_started[2]
        # A collection long enough to be worth recording.
        trace._gc_started[2] = (time.perf_counter() - .1, context, trigger)
        trace._gc_callback('stop', info)
        fields = next(fields for event, fields in calls if event == 'gc')
        assert fields['gc_generation'] == 2 and 'generation' not in fields
        assert len(fields['gc_counts_after']) == 3
        assert fields['collected'] == 7 and fields['uncollectable'] == 0
        assert fields['gc_collection_observed'] is True
        assert fields['duration_ms'] >= 100
    finally:
        trace.close()


def test_analyzer_uses_current_numbers_and_real_session_end(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = root/'artifacts/log-review-71336-20261024/inputs'
    result = tmp_path/'report.json'
    command = [sys.executable, str(root/'tools/analyze_prism_trace.py'),
               *map(str, sorted(source.glob('Prism-operations*'))), '--json', str(result)]
    subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=20)
    report = json.loads(result.read_text(encoding='utf-8'))['reports'][0]
    assert report['session_scope']['has_session_start'] is True
    assert report['session_scope']['has_session_end'] is True
    finding = next(f for f in report['findings'] if f['id']=='mindmap_payload_vs_gui_stall')
    assert '2.5 s' not in finding['note'] and '1.25 s' not in finding['note']
    assert str(finding['interval_union_ms']) in finding['note']
    assert finding['link_basis']=='session_trace_parent_span_id'
