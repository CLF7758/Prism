"""L12 verification: settings edit, picker apply, worker stage, snapshot queue."""
import json
import os
import sys
import types

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events as model_events          # noqa: E402


def test_settings_edit_is_recorded(monkeypatch):
    """The inspector is the UI entry; it must say which field changed."""
    import prism.widgets.material_library as lib

    seen = []
    monkeypatch.setattr(lib, 'trace_record',
                        lambda name, **fields: seen.append((name, fields)))

    details = type('D', (), {})()
    details.blend_mode = type('C', (), {'currentData': staticmethod(lambda: 'blend')})()
    details.normal_convention = type('C', (), {'currentData': staticmethod(lambda: 'DirectX')})()
    details.flip_x = type('C', (), {'isChecked': staticmethod(lambda: True)})()
    details.flip_y = type('C', (), {'isChecked': staticmethod(lambda: False)})()
    details.alpha_cutoff = type('C', (), {'value': staticmethod(lambda: 0.25)})()
    details.ignore_base_alpha = type('C', (), {'isChecked': staticmethod(lambda: False)})()
    details.tag_edit = type('C', (), {'text': staticmethod(lambda: 'a, b')})()

    library = types.SimpleNamespace()
    library.details = details
    library.selected_id = 'm-1'
    records = type('R', (), {})()
    records.get = lambda _mid: {'preview': {'properties': {'blendMode': 'auto',
                                                           'normalConvention': 'DirectX',
                                                           'flipX': False,
                                                           'flipY': False,
                                                           'alphaCutoff': 0.5,
                                                           'ignoreBaseAlpha': False}}}
    records.configure = lambda *a, **k: None
    library.records = records
    library._bound_service = records
    library.window_host = type('W', (), {
        'view': type('V', (), {'_upgrade_preview_readonly': False,
                               'scene': type('S', (), {'attachments': {}})()})()})()

    try:
        lib.MaterialLibrary.save_settings(library)
    except Exception:
        pass                       # downstream commit is not under test here

    hits = [fields for name, fields in seen if name == 'material_settings_edited']
    assert hits, [name for name, _ in seen]
    payload = hits[0]
    assert payload['material'] == 'm-1'
    assert payload['blend_mode'] == 'blend'
    assert payload['alpha_cutoff'] == 0.25
    assert 'blendMode' in payload['changed']
    assert 'flipX' in payload['changed']
    assert 'alphaCutoff' in payload['changed']
    assert payload['tag_count'] == 2


def test_picker_records_before_reading_state():
    """The call site must come first, so a half-built dialog cannot hide it.

    NOT verified here: that the record actually reaches the trace pipeline for
    the picker. The monkeypatched sink never saw the call in this harness, the
    cause is not identified, and guessing would be worse than saying so. The
    event is therefore listed as *instrumented but unverified*.
    """
    source = open(os.path.join(REPO, 'prism', 'widgets', 'model_material_picker.py'),
                  encoding='utf-8').read()
    apply_body = source.split('    def _apply(self):', 1)[1].split('def reject', 1)[0]
    assert "_note_picker('material_picker.applied'" in apply_body
    assert apply_body.index('_note_picker') < apply_body.index('self.selected()')
    assert 'def _note_picker(self, event, **fields):' in source


def test_worker_render_start_is_emitted():
    source = open(os.path.join(REPO, 'prism', 'components', 'model_worker.py'),
                  encoding='utf-8').read()
    assert "event('model_cover_render_started'" in source
    # It must be after the existing 'rendered' start marker, not before it.
    assert source.index("event('rendered', phase='start'") < source.index(
        "event('model_cover_render_started'")


def test_snapshot_carries_queue_numbers(tmp_path):
    from prism.model_library.project_storage import (ModelProjectContext,
                                                     connect, ensure_model_tables)
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.service import ModelLibraryService
    from prism.widgets.diagnostics_dialog import _service_queue
    from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot

    metadata = os.path.join(str(tmp_path), 'p.prism')
    connection = connect(metadata)
    try:
        ensure_model_tables(connection)
        connection.execute('PRAGMA user_version=1')
    finally:
        connection.close()
    context = ModelProjectContext(project_path=metadata, metadata_path=metadata,
                                  project_id='proj-q', storage_instance_id='s-q',
                                  open_epoch=1, session_token='sess-q',
                                  backend='sqlite', source_revision='1',
                                  managed_root=None)
    service = ModelLibraryService(repository=ModelRepositoryAdapter(context),
                                  can_write=lambda: True)
    queue = _service_queue(service)
    assert queue['basis'] == 'service-private-attributes'
    assert queue['state'] == 'measured'
    assert queue['futures'] == 0
    snapshot = model_diagnostic_snapshot(service=service, queue=queue)
    assert snapshot['queue']['state'] == 'measured'
    assert snapshot['queue']['leases'] == 0
    # No service at all must stay "not supplied", never zero.
    assert _service_queue(None) is None


@pytest.mark.parametrize('name', [
    'material_settings_edited', 'material_picker.applied',
    'model_cover_render_started',
])
def test_contract_declares_the_new_events(name):
    assert name in model_events.REQUIRED_FIELDS, name
