"""Small detached fixtures for capture copying and work/wait attribution."""
import json
from types import SimpleNamespace

import pytest
from PyQt6 import QtCore

from prism.fileio import save_preparation as preparation
from prism.fileio.save_snapshot import BoardSaveSnapshot
from prism.fileio.sliced_copy import sliced_copy


def consume(steps):
    count = 0
    while True:
        try:
            next(steps)
            count += 1
        except StopIteration as done:
            return done.value, count


def test_shared_board_body_copied_once_without_losing_unknown_metadata():
    body = {'unknown_plugin': [{'custom': [n, {'preserve': True}]} for n in range(500)]}
    scene = SimpleNamespace(project_id='fixture', mindmap_tree=body,
                            workspace_pages=[{'id': 'page', 'body': body}],
                            material_library_state={'plugin': body})
    before_steps = sum(consume(sliced_copy(value))[1] for value in (
        body, scene.workspace_pages, scene.material_library_state))
    snapshot, after_steps = consume(BoardSaveSnapshot.capture_steps(scene, defer_tag_system=True))
    saved = snapshot.state
    assert saved.mindmap_tree == body and saved.mindmap_tree is not body
    assert saved.workspace_pages[0]['body'] is saved.mindmap_tree
    assert saved.material_library_state['plugin'] is saved.mindmap_tree
    body['unknown_plugin'][0]['custom'][1]['preserve'] = False
    assert saved.mindmap_tree['unknown_plugin'][0]['custom'][1]['preserve'] is True
    assert after_steps < before_steps / 2
    print(json.dumps({'fixture': '500 shared nested records',
                      'legacy_copy_checkpoints': before_steps,
                      'capture_checkpoints': after_steps}))


@pytest.fixture
def operation(qapp, tmp_path):
    view = QtCore.QObject()
    view.scene = SimpleNamespace(project_id='fixture')
    view.undo_stack = SimpleNamespace(index=lambda: 0)
    op = preparation.SavePreparation(view, str(tmp_path/'unused.prism'), False, True)
    op.worker = SimpleNamespace(canceled=False)
    view.worker = op.worker
    view._save_operation = op
    yield op
    op.timer.stop()


def test_slice_telemetry_distinguishes_work_from_event_loop_gaps(operation, monkeypatch):
    clock, events = [0.0], []
    monkeypatch.setattr(preparation, 'time', SimpleNamespace(
        monotonic=lambda: clock[0], perf_counter=lambda: clock[0]))
    monkeypatch.setattr(preparation, 'record', lambda name, **fields: events.append((name, fields)))
    operation._capture_clock = operation._capture_last_end = 0.0
    operation.SLICE_STEPS = 1

    def steps():
        clock[0] += .001
        yield

    operation._steps = steps()
    clock[0] = 5.0
    operation._drain()
    operation.timer.stop()
    clock[0] = 8.0
    operation._drain()
    assert operation.ready.is_set() and operation.error is None
    summary = events[-1][1]
    assert events[-1][0] == 'project_save_capture'
    assert summary['work_ms'] == pytest.approx(1)
    assert summary['wait_ms'] == pytest.approx(7999)
    assert summary['elapsed_ms'] == pytest.approx(8000)
    assert summary['slices'] == 2 and summary['steps'] == 1
    assert events[0][1]['work_ms'] == pytest.approx(1)
    assert events[0][1]['wait_ms'] == pytest.approx(5000)


def test_new_edit_still_refuses_capture_and_diagnostic_failure_is_isolated(operation, monkeypatch):
    def broken_record(*args, **kwargs):
        raise RuntimeError('diagnostic sink unavailable')

    monkeypatch.setattr(preparation, 'record', broken_record)
    operation.view._content_revision = 1
    operation._drain()
    assert operation.ready.is_set()
    assert isinstance(operation.error, preparation.PartialSaveConflict)
    assert operation.board is None


def test_session_indexes_share_detached_rows_and_preserve_unknown_fields(operation):
    row = ('id', {'future_metadata': [{'keep': True}]})
    session = SimpleNamespace(
        source='fixture', source_identity='identity', project_id='fixture', schema_version=1,
        loaded_pages=(), pages_complete=False, note_key=None, mindmap_key=None,
        attachments=False, _rows={('canvas', 'id'): row}, _canvas_rows={'id': row},
        _domain_deletions=set(), _library_state_before=None,
        _valid_domain_attachments=set(), _mindmap_clear_authorized=False, _page_bases={},
        _metadata_scope=False,
        _domains=SimpleNamespace(identity='domain-identity', _rows={('canvas', 'id'): row}),
        _residency=SimpleNamespace(assets_for_save=lambda assets: assets))
    operation.session = session
    consume(operation._collect())
    captured = operation.session_rows[('canvas', 'id')]
    assert captured is operation.session_fields['_canvas_rows']['id']
    assert captured is operation.domain_rows[('canvas', 'id')]
    assert captured is not row
    row[1]['future_metadata'][0]['keep'] = False
    assert captured[1]['future_metadata'][0]['keep'] is True
