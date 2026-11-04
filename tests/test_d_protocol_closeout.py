"""D protocol: immutable identity, explicit trace baselines, bounded pair sampling."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets

from prism import performance_trace
from prism.presentation_evidence import QueryEvidence
from tests.test_catalog_browser_partial import prepare, wait_complete
from tests.test_d_presentation_end_to_end import thumbnail


def completed(ev):
    ev.observe('read', [1, 2, 3])
    ev.read_pulled(3)
    ev.record('catalog_rows_read_evidence', **ev.read_fields(), complete=True, final=True)
    ev.observe('consumer_consumed', [1, 2, 3])
    ev.delivery('consumer_consumed', complete=True, final=True)
    ev.record('asset_model_published', model_rows=3, model_unique_ids=3,
              model_unique_ids_complete=True, complete=True, final=True)
    ev.final = True


def trace_stub(monkeypatch, session='S1'):
    events = []
    monkeypatch.setattr(performance_trace, '_active', SimpleNamespace(session=session))
    monkeypatch.setattr(performance_trace, 'record', lambda event, **fields: events.append((event, fields)))
    return events


def test_late_trace_and_switch_keep_query_identity_and_replay_not_new_work(monkeypatch):
    monkeypatch.setattr(performance_trace, '_active', None)
    ev = QueryEvidence({'mode': 'all'}, source_revision='r1', diagnostic=True)
    completed(ev)
    before = ev.fields()
    counts = ev.read_fields()
    events = trace_stub(monkeypatch)
    ev.attach_trace()
    assert [event for event, _ in events] == ['catalog_query_baseline',
        'catalog_rows_read_evidence', 'catalog_rows_delivered', 'asset_model_published']
    assert all(f['baseline_only'] and not f['baseline_rows_are_new_work'] for _, f in events)
    assert all(f['session_id'] == before['session_id'] and f['trace_session_id'] == 'S1' for _, f in events)
    assert ev.fields() == before and ev.read_fields() == counts
    events.clear()
    ev.attach_trace()
    assert not events
    monkeypatch.setattr(performance_trace, '_active', SimpleNamespace(session='S2'))
    ev.record('asset_paint_frame', frame_id='f2', final=False)
    assert [event for event, _ in events][:4] == ['catalog_query_baseline',
        'catalog_rows_read_evidence', 'catalog_rows_delivered', 'asset_model_published']
    assert events[-1][1]['trace_session_id'] == 'S2'
    assert ev.fields() == before and ev.read_fields() == counts


def test_paint_visibility_pair_sampling_retains_all_observer_facts(monkeypatch):
    events = trace_stub(monkeypatch)
    observed = []
    now = [10.0]
    monkeypatch.setattr('prism.presentation_evidence.time.monotonic', lambda: now[0])
    ev = QueryEvidence(source_revision='r1', observer=lambda event, **f: observed.append((event, f)))
    ev.final = True  # model installation is final, painting is not terminal
    for index in range(50):
        frame = str(index)
        ev.record('asset_paint_frame', frame_id=frame, final=False)
        ev.record('asset_visibility_snapshot', frame_id=frame, final=False)
        now[0] += .01
    assert len(observed) == 100
    paints = [f['frame_id'] for event, f in events if event == 'asset_paint_frame']
    visible = [f['frame_id'] for event, f in events if event == 'asset_visibility_snapshot']
    assert paints == visible and 1 <= len(paints) <= 3
    assert all(not f['final'] and f['priority'] == 'state' for _, f in events)
    assert not ev._frame_trace_decisions


def test_interleaved_frames_have_matching_bounded_sampling_decisions(monkeypatch):
    events = trace_stub(monkeypatch)
    now = [10.0]
    monkeypatch.setattr('prism.presentation_evidence.time.monotonic', lambda: now[0])
    ev = QueryEvidence(source_revision='r1')
    ev.record('asset_paint_frame', frame_id='f1', final=False)
    now[0] += .3
    ev.record('asset_paint_frame', frame_id='f2', final=False)
    ev.record('asset_visibility_snapshot', frame_id='f2', final=False)
    ev.record('asset_visibility_snapshot', frame_id='f1', final=False)
    assert [(event, f['frame_id']) for event, f in events] == [
        ('asset_paint_frame', 'f1'), ('asset_paint_frame', 'f2'),
        ('asset_visibility_snapshot', 'f2'), ('asset_visibility_snapshot', 'f1')]
    assert not ev._frame_trace_decisions
    for index in range(100):
        ev.record('asset_paint_frame', frame_id='open' + str(index), final=False)
    assert len(ev._frame_trace_decisions) <= 64


def test_transport_switch_inside_observer_does_not_rewrite_query_identity(monkeypatch):
    events = trace_stub(monkeypatch)
    ev = QueryEvidence(source_revision='r1')
    original = ev.session_id
    def switch(event, **fields):
        if event == 'asset_paint_frame':
            monkeypatch.setattr(performance_trace, '_active', SimpleNamespace(session='S2'))
    ev.observer = switch
    ev.record('asset_paint_frame', frame_id='midframe', final=False)
    paint = next(f for event, f in events if event == 'asset_paint_frame')
    assert ev.session_id == original == paint['session_id']
    assert paint['trace_session_id'] == 'S2'
    assert paint['trace_switch_during_observation']


def test_paint_wire_geometry_survives_existing_trace_field_caps(monkeypatch):
    events = trace_stub(monkeypatch)
    ev = QueryEvidence(source_revision='r1')
    clip = [[[0., 0.], [20., 0.], [20., 30.], [0., 30.], [0., 0.]]]
    ev.record('asset_paint_frame', frame_id='f1', final=False, effective_clip=clip)
    wire = events[0][1]
    assert wire['effective_clip_encoding'] == 'flat_xy_polygons'
    assert performance_trace._cap(wire['effective_clip']) == wire['effective_clip']
    assert wire['effective_clip_trace_complete']


def test_real_trace_writer_after_published_browser_preserves_baseline_and_pixels(
        main_window, tmp_path, qtbot, monkeypatch):
    monkeypatch.setattr(performance_trace, '_active', None)
    monkeypatch.setenv('PRISM_PRESENTATION_DIAGNOSTIC', '1')
    scope, scene, browser = prepare(main_window, tmp_path, [
        (1, 'pixmap', dict(canvasId='A', imageWidth=32, imageHeight=32,
                           displayThumbnail=thumbnail(), opacity=1.0))])
    before = scope.project_path.read_bytes()
    browser.open('all')
    wait_complete(qtbot, browser, 1)
    qtbot.waitUntil(lambda: browser.model.presentation_evidence is not None, timeout=15000)
    ev = browser.model.presentation_evidence
    identity = ev.fields()
    read_counts = ev.read_fields()
    trace_dir = tmp_path / 'writer'
    trace_dir.mkdir()
    trace = performance_trace.PerformanceTrace(QtWidgets.QApplication.instance(), main_window, trace_dir)
    monkeypatch.setattr(performance_trace, '_active', trace)
    try:
        # Opening the already-published query must attach its baseline itself.
        browser.open('all')
        assert browser.model.presentation_evidence is ev
        assert ev._trace_seen == trace.session
        assert ev.read_fields() == read_counts
        browser.list.viewport().update()
        qtbot.wait(350)
        # This diagnostic capture explicitly selects the next real frame for
        # trace retention; normal runtime sampling is not disabled.
        ev._last_emit.pop(('paint_pair', trace.session), None)
        image = browser.list.viewport().grab().toImage()
        assert not image.isNull()
        red_pixels = sum(image.pixelColor(x, y).red() > 180
                         and image.pixelColor(x, y).green() < 80
                         for y in range(0, image.height(), 4)
                         for x in range(0, image.width(), 4))
        assert red_pixels > 10
        capture = browser.list._asset_paint_capture_id
        qtbot.wait(300)
    finally:
        trace.close()
    assert not trace.writer.is_alive()
    facts = [json.loads(line) for line in trace.path.read_text(encoding='utf-8').splitlines()]
    query = [f for f in facts if f.get('query_id') == ev.query_id]
    assert ev.fields() == identity
    assert all(f['session_id'] == identity['session_id'] for f in query)
    assert all(f['trace_session_id'] == trace.session for f in query)
    baseline = [f for f in query if f.get('baseline_only')]
    assert {f['event'] for f in baseline} >= {'catalog_query_baseline',
        'catalog_rows_read_evidence', 'catalog_rows_delivered', 'asset_model_published'}
    assert all(not f['baseline_rows_are_new_work'] for f in baseline)
    paint = [f for f in query if f['event'] == 'asset_paint_frame']
    visible = [f for f in query if f['event'] == 'asset_visibility_snapshot']
    assert paint and visible
    assert {f['frame_id'] for f in paint} == {f['frame_id'] for f in visible}
    assert all(f['effective_clip_trace_complete'] for f in paint)
    assert all(f['effective_clip_encoding'] == 'flat_xy_polygons' for f in paint)
    assert all(f['effective_clip'][0] and isinstance(f['effective_clip'][0][0], (int, float)) for f in paint)
    assert capture in {f['frame_id'] for f in paint} == {f['frame_id'] for f in visible}
    assert before == scope.project_path.read_bytes()
    if os.environ.get('PRISM_D_CAPTURE_DIR'):
        output = Path(os.environ['PRISM_D_CAPTURE_DIR'])
        stem = 'D-real-trace-' + capture
        for suffix in ('.png', '.jsonl', '.json'):
            assert not (output / (stem + suffix)).exists()
        assert image.save(str(output / (stem + '.png')), 'PNG')
        (output / (stem + '.jsonl')).write_bytes(trace.path.read_bytes())
        (output / (stem + '.json')).write_text(json.dumps(dict(
            capture_id=capture, query_id=ev.query_id, session_id=ev.session_id,
            trace_session_id=trace.session, baseline_rows_are_new_work=False,
            red_sampled_pixels=red_pixels, physical_display_confirmed=False,
            original_source_bytes_unchanged=True,
            effective_clip_encoding='flat_xy_polygons'), ensure_ascii=False, indent=2), encoding='utf-8')
