"""L8: material edit actions leave one record each, and carry no content.

`_edit` is the single dispatch for every edit action. These tests drive the real
method with a real `MaterialState` and assert what it recorded -- patch slots,
the three-state texture actions, clone counts. The only stub is the trace sink.
"""
import json
import os
import sys

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library.multimaterial import application as app_module   # noqa: E402
from prism.model_library.multimaterial import contracts                   # noqa: E402
from prism.model_library import events as model_events                     # noqa: E402

TARGET = contracts.MaterialTarget(project_session='sess-1', open_epoch=2,
                                  model_id='model-9', source_revision=3,
                                  catalog_revision=4, state_revision=5)


def _state():
    material = contracts.WorkingMaterial(working_id='w-1', source_material_id='src-1',
                                         display_name='Wood')
    other = contracts.WorkingMaterial(working_id='w-2', source_material_id='src-2',
                                      display_name='Steel')
    return contracts.MaterialState(
        target=TARGET, materials=(material, other),
        assignments=(contracts.MaterialAssignment('binding-1', 'w-1'),
                     contracts.MaterialAssignment('binding-2', 'w-2')))


def _item():
    return {'state': _state(), 'catalog': None, 'snapshots': {}, 'paths': {},
            'resources': {}}


def _request(operation, *, working=('w-1',), bindings=(), parameters=None,
             request_id='req-1'):
    return contracts.MaterialEditRequest(
        request_id=request_id, gesture_id='g-1', target=TARGET,
        operation=operation, working_ids=tuple(working),
        binding_ids=tuple(bindings),
        parameters_json=json.dumps(parameters or {}))


@pytest.fixture()
def recorded(monkeypatch):
    seen = []
    monkeypatch.setattr(app_module, 'record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    service = object.__new__(app_module.ProjectMaterialService)
    service._clipboard = {}
    return service, seen


def test_patch_records_slots_and_texture_states(recorded):
    service, seen = recorded
    request = _request(contracts.MaterialOperation.PATCH, parameters={
        'patch': {'slots': {'basecolor': {'enabled': False}},
                  'texture': {'slot': 'normal', 'action': 'clear'}}})
    service._edit(_item(), request)
    hits = [fields for name, fields in seen if name == 'material.edit.patch']
    assert hits, [name for name, _ in seen]
    payload = hits[0]
    assert payload['slots'] == ['basecolor']
    assert payload['texture_actions'] == ['normal:clear']
    assert payload['material_count'] == 1
    assert payload['operation'] == 'patch'
    # Identity is on the record, so one action can be traced to its model.
    assert payload['model_id'] == 'model-9'
    assert payload['state_revision'] == 5
    assert payload['request_id'] == 'req-1'


def test_patch_channel_action_is_named(recorded):
    service, seen = recorded
    # The channel action re-uses the currently resolved texture, so the
    # resolved state has to contain one; that is the only stub here.
    resolved = contracts.MaterialDefinition(
        material_id='src-1', source_key='k-1', name='Wood', workflow='pbr',
        textures=(contracts.TextureUse('basecolor', 'sha256:abc', 'RGB', 0, 'srgb'),))
    service._resolved = lambda item, wid, state: resolved
    request = _request(contracts.MaterialOperation.PATCH, parameters={
        'patch': {'texture': {'slot': 'basecolor', 'action': 'channel',
                              'channel': 'A'}}})
    service._edit(_item(), request)
    payload = [fields for name, fields in seen
               if name == 'material.edit.patch'][0]
    assert payload['channels'] == ['basecolor:A']


def test_make_unique_records_clone_count(recorded):
    service, seen = recorded
    request = _request(contracts.MaterialOperation.MAKE_UNIQUE,
                       bindings=('binding-1',))
    result = service._edit(_item(), request)
    payload = [fields for name, fields in seen
               if name == 'material.edit.make_unique'][0]
    assert payload['clone_count'] == 1
    assert payload['rebound_bindings'] == 1
    # The new material really exists and the binding really moved.
    ids = {material.working_id for material in result.materials}
    assert len(ids) == 3
    moved = {a.binding_id: a.working_id for a in result.assignments}
    assert moved['binding-1'] != 'w-1'


def test_restore_material_is_recorded(recorded):
    service, seen = recorded
    request = _request(contracts.MaterialOperation.RESTORE_MATERIAL)
    service._edit(_item(), request)
    payload = [fields for name, fields in seen
               if name == 'material.edit.restore_material'][0]
    assert payload['material_count'] == 1


def test_restore_model_is_recorded_before_it_returns(recorded, monkeypatch):
    service, seen = recorded
    monkeypatch.setattr(app_module.ProjectMaterialService, '_initial',
                        lambda self, catalog, target: 'restored')
    request = _request(contracts.MaterialOperation.RESTORE_MODEL, working=(),
                       bindings=())
    assert service._edit(_item(), request) == 'restored'
    payload = [fields for name, fields in seen
               if name == 'material.edit.restore_model'][0]
    assert payload['model_id'] == 'model-9'


def test_no_content_is_recorded(recorded):
    """A patch body holds texture bytes and note text: none of it may leak."""
    service, seen = recorded
    marker = 'LEAK-MARKER-9c1f'
    request = _request(contracts.MaterialOperation.PATCH, parameters={
        'patch': {'marker': marker,
                  'texture': {'slot': 'basecolor', 'action': 'clear'}}})
    service._edit(_item(), request)
    blob = json.dumps([fields for _name, fields in seen], ensure_ascii=False)
    assert marker not in blob, '参数内容不该进日志'
    for forbidden in ('path', 'bytes', 'text', 'payload', 'note'):
        assert forbidden not in [key.lower() for _n, fields in seen
                                 for key in fields]


@pytest.mark.parametrize('name', [
    'material.edit.patch',
    'material.edit.replace_surface',
    'material.edit.replace_full',
    'material.edit.paste',
    'material.edit.make_unique',
    'material.edit.restore_material',
    'material.edit.restore_model',
])
def test_edit_family_is_declared(name):
    assert name in model_events.REQUIRED_FIELDS, '%s 未声明' % name
