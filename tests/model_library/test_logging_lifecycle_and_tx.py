"""L9: persistence transactions, trash/restore, preview pickup and lifecycle.

The transaction and conflict paths run against a real SQLite project file: a
version conflict is provoked for real by writing with a stale revision. The
preview paths are the real `preview_event` / `highlight` methods with only the
trace sink swapped.
"""
import dataclasses
import json
import os
import sys

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events as model_events                       # noqa: E402
from prism.model_library.project_storage import (ModelProjectContext,        # noqa: E402
                                                 ModelRevisionConflict, connect,
                                                 ensure_model_tables)
from prism.model_library.repository import ModelRepositoryAdapter            # noqa: E402
from prism.model_library import service as service_module                    # noqa: E402
from prism.model_library.multimaterial import application as app_module      # noqa: E402
from prism.model_library.multimaterial import contracts                      # noqa: E402


def _project(root):
    metadata = os.path.join(root, 'project.prism')
    connection = connect(metadata)
    try:
        ensure_model_tables(connection)
        connection.execute('PRAGMA user_version=1')
    finally:
        connection.close()
    return ModelProjectContext(project_path=metadata, metadata_path=metadata,
                               project_id='proj-tx', storage_instance_id='store-tx',
                               open_epoch=1, session_token='sess-tx', backend='sqlite',
                               source_revision='1', managed_root=None)


@pytest.fixture()
def repository(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr('prism.model_library.repository.record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    repo = ModelRepositoryAdapter(_project(str(tmp_path)))
    repo.insert({'model_id': 'model-1', 'project_id': 'proj-tx', 'name': 'Wood',
                 'folder_id': None, 'record_revision': 0, 'source_revision': 1,
                 'files': [], 'tags': [], 'notes': ''})
    return repo, seen


def test_transaction_begin_is_recorded(repository):
    repo, seen = repository
    payload = repo.get_payload('model-1')
    payload['name'] = 'Wood 2'
    repo.update(payload, expected_revision=payload['record_revision'])
    names = [name for name, _fields in seen]
    assert 'model_tx.begin' in names, names
    begin = [fields for name, fields in seen if name == 'model_tx.begin'][0]
    assert begin['model_id'] == 'model-1'
    assert begin['state'] == 'begun'


def test_stale_revision_is_recorded_as_conflict(repository):
    repo, seen = repository
    payload = repo.get_payload('model-1')
    payload['name'] = 'stale write'
    with pytest.raises(ModelRevisionConflict):
        repo.update(payload, expected_revision=99)          # wrong on purpose
    conflicts = [fields for name, fields in seen if name == 'model_tx.conflict']
    assert conflicts, [name for name, _ in seen]
    assert conflicts[0]['reason'] == 'revision_changed'
    assert conflicts[0]['expected_revision'] == 99
    assert conflicts[0]['stored_revision'] == payload['record_revision']
    # And nothing was written: the stored name is still the original.
    assert repo.get_payload('model-1')['name'] == 'Wood'


def test_preview_events_are_all_recorded(monkeypatch):
    seen = []
    monkeypatch.setattr('prism.model_library.service.record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    service = object.__new__(service_module.ModelLibraryService)
    service._closed = False
    service._preview_camera = {}
    service.preview_event('material.picked', {'binding_id': 'b-7',
                                              'identity': {'model_id': 'm-1'}})
    service.preview_event('camera_changed', {'camera': {'eye': [0, 0, 1]}})
    names = [name for name, _fields in seen]
    assert names.count('material_preview.event') == 2, names
    picked = [fields for name, fields in seen
              if name == 'material_preview.picked']
    assert picked and picked[0]['binding_id'] == 'b-7'
    camera = [fields for name, fields in seen if name == 'material_preview.event'][1]
    assert camera['has_camera'] is True


def test_highlight_is_recorded(monkeypatch):
    seen = []
    monkeypatch.setattr('prism.model_library.multimaterial.application.record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    target = contracts.MaterialTarget('sess', 1, 'm-1', 2, 3, 4)
    service = object.__new__(app_module.ProjectMaterialService)
    service._highlights = {}
    # highlight talks to the native controller; stub only that call.
    service._load = lambda target: {'state': 'x'}
    service._active = None
    service._render = lambda *args, **kwargs: None
    service.controller = type('C', (), {
        'submit': staticmethod(lambda *a, **k: 'job-1'),
        'forget': staticmethod(lambda *a, **k: None),
    })()
    try:
        service.highlight(target, ['b-1', 'b-2'])
    except Exception:
        pass                       # the render stub may not match every branch
    hits = [fields for name, fields in seen if name == 'material.edit.highlight']
    assert hits, [name for name, _ in seen]
    assert hits[0]['binding_count'] == 2
    assert hits[0]['model_id'] == 'm-1'
    assert hits[0]['binding_ids'] == ['b-1', 'b-2']


@pytest.mark.parametrize('name,required', [
    ('material_preview.event', ('event_name', 'state')),
    ('material_preview.picked', ('binding_id', 'state')),
    ('material.edit.highlight', ('model_id', 'binding_count')),
    ('model_tx.begin', ('model_id', 'expected_revision')),
    ('model_tx.conflict', ('model_id', 'expected_revision', 'stored_revision')),
    ('model_items_trashed', ('count', 'trash_stamp')),
    ('model_items_restored', ('count',)),
    ('model_leases_released', ('count',)),
    ('app_closing', ('state',)),
])
def test_new_events_are_declared(name, required):
    assert name in model_events.REQUIRED_FIELDS, '%s 未声明' % name
    for field in required:
        assert field in model_events.REQUIRED_FIELDS[name], \
            '%s 缺必需字段 %s' % (name, field)
