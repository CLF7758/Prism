import json
from dataclasses import replace

import pytest
from PyQt6 import QtGui

from prism.material_parameters import RANGES, slot_options, validate_slot_patch
from prism.widgets.material_slots import MaterialSlot
from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import MaterialEditRequest, MaterialOperation
from tests.model_library.multimaterial.conftest import chair_catalog, make_target


@pytest.mark.parametrize('key', RANGES)
def test_all_sliders_end_at_one_and_allow_typed_overdrive(qtbot, key):
    slot = MaterialSlot(key, key)
    qtbot.addWidget(slot)
    slot.show_binding({'name': 'map.png', 'width': 8, 'height': 8})
    assert slot.amount.value() == 1
    assert slot.slider.value() == slot.slider.maximum()
    slot.slider.setValue(250)
    assert slot.amount.value() == .25
    slot.slider.setValue(slot.slider.maximum())
    assert slot.amount.value() == 1
    patches = []
    slot.options_requested.connect(lambda _, patch: patches.append(patch))
    slot.amount.setValue(2.75)
    assert slot.amount.value() == 2.75 and slot.slider.value() == slot.slider.maximum()
    assert next(iter(patches[-1].values())) == 2.75
    validate_slot_patch(key, patches[-1])
    slot.show_binding({'name': 'map.png', 'width': 8, 'height': 8},
                      {'slots': {key: patches[-1]}})
    assert slot.amount.value() == 2.75
    slot.show()
    qtbot.wait(1)
    for size in (9, 14, 20):
        font = QtGui.QFont(slot.amount.font())
        font.setPointSize(size)
        slot.amount.setFont(font)
        text_width = slot.amount.lineEdit().fontMetrics().horizontalAdvance(slot.amount.text())
        assert slot.amount.lineEdit().contentsRect().width() >= text_width


def test_model_reset_keeps_bindings_names_and_unrelated_switches():
    service = ProjectMaterialService.__new__(ProjectMaterialService)
    catalog, state = chair_catalog()
    working = state.materials[0]
    edited = replace(working, display_name='保持名字', property_patch_json=json.dumps({
        'slots': {'basecolor': {'color': [.2,.3,.4], 'value': .2, 'enabled': False},
                  'normal': {'value': 3, 'invert': True}}}))
    state = replace(state, materials=(edited,) + state.materials[1:])
    item = dict(state=state, catalog=catalog, snapshots={}, paths={}, resources={})
    request = MaterialEditRequest('reset', 'selection-reset', state.target, MaterialOperation.PATCH,
        (working.working_id,), parameters_json=json.dumps({'patch': {'reset_adjustments': True}}))
    restored = service._edit(item, request)
    result = restored.materials[0]
    assert restored.assignments == state.assignments
    assert result.texture_patches == edited.texture_patches and result.display_name == '保持名字'
    props = json.loads(result.property_patch_json)
    options = slot_options(props, 'basecolor', True)
    assert options['amount'] == 1 and options['color'] == [1,1,1] and options['enabled'] is False
    assert props['slots']['normal']['invert'] is True


def test_model_switch_requests_one_reset_without_touching_texture_edits(loaded, port):
    resets = []
    port.reset_adjustments = lambda target, ids: resets.append((target, ids)) or 'reset-job'
    loaded.select_material('w-wood')
    loaded._option_committed('normal', {'value': 2}, 'gesture', 1)
    loaded.select_material('w-metal')
    loaded._option_committed('basecolor', {'color': [.1,.2,.3]}, 'gesture2', 2)
    assert not resets
    loaded.reset(replace(make_target(), model_id='next-model'))
    assert len(resets) == 1 and resets[0][0].model_id == make_target().model_id
    assert resets[0][1] == ()  # Reset all edited parts when leaving the model.
    assert not loaded._adjusted_working_ids


def test_initial_selection_resets_old_parameters_before_inspection_once(loaded, port):
    order = []
    port.reset_adjustments = lambda target: order.append('reset') or 'reset-job'
    previous = port.inspect
    port.inspect = lambda target: order.append('inspect') or previous(target)
    loaded._reset_on_inspect = True
    loaded.inspect()
    loaded.inspect()
    assert order == ['reset', 'inspect', 'inspect']


def test_selection_reset_waits_for_commit_preserves_originals_and_reopens(tmp_path, qapp, qtbot):
    import hashlib
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace
    from PIL import Image
    from tests.model_library.test_storage import make_scene, create_project
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.importer import ModelImporter
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService
    from prism.material_parameters import resolved_controls

    source = tmp_path / 'triangle.obj'
    source.write_text('mtllib triangle.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\n'
                      'vt 0 0\nvt 1 0\nvt 0 1\nusemtl Wood\nf 1/1 2/2 3/3\n')
    source.with_suffix('.mtl').write_text('newmtl Wood\nKd .4 .2 .1\nmap_Kd texture.png\n')
    Image.new('RGB', (8,8), (120,80,40)).save(tmp_path / 'texture.png')
    project = create_project(tmp_path / 'model.prism', make_scene())
    context = open_model_context(project, open_epoch=1)
    repo = ModelRepositoryAdapter(context)
    importer = ModelImporter(context, repo)
    handle = importer.import_paths([source])
    qtbot.waitUntil(lambda: importer.job_state(handle) not in ('queued','running'), timeout=15000)
    assert importer.job_state(handle) == 'completed'
    with closing(sqlite3.connect(context.metadata_path)) as db:
        model_id = db.execute('select id from model_records').fetchone()[0]
    owner = ModelLibraryService(repo, bundles=ModelBundleStore(context,repo,root=str(tmp_path/'bundles')),
                                on_commit=lambda before, after: None)
    service = ProjectMaterialService(owner, SimpleNamespace(shutdown=lambda: None))
    service._render = lambda *args, **kwargs: None  # Persistence test, no window or GPU.
    events = []
    service.subscribe(events.append)
    try:
        target = service.target_for(repo.get(model_id))
        state = service._load(target)['state']
        working = state.materials[0].working_id
        files = repo.get_payload(model_id)['files']
        request = MaterialEditRequest('numeric', 'gesture', target, MaterialOperation.PATCH,
            (working,), parameters_json=json.dumps({'patch': {'slots': {
                'basecolor': {'color': [.1,.2,.3], 'value': 2.75}}}}))
        committed = service.commit(request)
        # Supply the old GUI revision while its commit may still be queued.
        reset = service.reset_adjustments(target)
        service._jobs[committed].result(timeout=15)
        service._jobs[reset].result(timeout=15)
        assert not any(e.get('kind') == 'commit_failed' for e in events), events
        service._models.clear()
        item = service._load(service.target_for(repo.get(model_id)))
        definition = service._resolved(item, working)
        controls = resolved_controls(json.loads(definition.properties_json), [t.slot for t in definition.textures])
        assert controls['basecolor']['amount'] == 1
        assert controls['basecolor']['color'] == [1,1,1]
        assert len(definition.textures) == 1
        for file in files:
            assert hashlib.sha256(repo.resources.read_all(file['attachment_id'])).hexdigest() == file['sha256']
    finally:
        importer.close_project(context.session_token)
        service.close()
