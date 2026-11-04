"""A2: the working-material state, its three-state resolution and its validation.

These are the rules the UI and the storage both depend on, so they are tested
without Qt, without a renderer and without a database: a wrong answer here
would be a wrong answer everywhere.
"""
import json

import pytest

from prism.model_library.multimaterial.catalog import (
    CatalogBuilder,
    MaterialDefinition,
    TextureUse,
)
from prism.model_library.multimaterial.contracts import (
    BindingSite,
    MaterialAssignment,
    MaterialEditRequest,
    MaterialOperation,
    MaterialState,
    MaterialTarget,
    PatchAction,
    TexturePatch,
    WorkingMaterial,
)
from prism.model_library.multimaterial.state_store import (
    SOURCE_CLEARED,
    SOURCE_DISABLED,
    SOURCE_NONE,
    SOURCE_SNAPSHOT,
    SOURCE_SOURCE,
    SOURCE_WORKING,
    collect_references,
    decode_state,
    editable_slot_report,
    effective_properties,
    effective_texture,
    encode_state,
    leftover_materials,
    orphaned_resources,
    state_signature,
)
from prism.model_library.multimaterial.validation import (
    E_ACTION_INVALID,
    E_PARAM_INVALID,
    E_REQUEST_CONFLICT,
    E_RESOURCE_MISSING,
    E_SLOT_READ_ONLY,
    E_TARGET_MISSING,
    E_UNKNOWN_BINDING,
    E_UNKNOWN_MATERIAL,
    SURFACE_REPLACEMENT_KEEPS,
    MaterialValidationError,
    check_idempotent,
    parse_parameters,
    plan_make_unique,
    request_signature,
    resolve_targets,
    validate_catalog,
    validate_state,
)


# ---------------------------------------------------------------------------
# fixtures: one chair, four legs sharing wood
# ---------------------------------------------------------------------------
def make_target(state_revision=1):
    return MaterialTarget('session', 1, 'model-1', 2, 3, state_revision)


def make_catalog(*, dangling=False):
    builder = CatalogBuilder('test-parser', main_path='<memory>')
    builder.add_definition(MaterialDefinition(
        material_id='src:wood', source_key='0', name='wood',
        workflow='pbr',
        textures=(
            TextureUse('basecolor', 'sha256:wood-colour', 'RGB', 0, 'srgb',
                       source_locator='wood.png'),
            TextureUse('roughness', 'sha256:wood-rough', 'G', 0, 'linear',
                       source_locator='rough.png'),
            TextureUse('bump', 'sha256:wood-bump', 'R', 0, 'linear',
                       source_locator='bump.png'),
        ),
        properties_json=json.dumps({'roughness_factor': 0.5,
                                    'metallic_factor': 0.0,
                                    'present': ['metallic_factor',
                                                'roughness_factor']})))
    for name in ('leg-fl', 'leg-fr', 'leg-bl', 'leg-br'):
        builder.add_binding(BindingSite(f'bind-{name}', f'src-{name}', name,
                                        'geo-chair', 'prim-0', 'src:wood'))
        builder.note_binding_resolution(f'bind-{name}', 'assigned')
    builder.add_binding(BindingSite('bind-seat', 'src-seat', 'seat', 'geo-chair',
                                    'prim-1', None))
    builder.note_binding_resolution('bind-seat', 'unassigned')
    if dangling:
        builder.add_binding(BindingSite('bind-broken', 'src-broken', 'broken',
                                        'geo-chair', 'prim-2', None))
        builder.note_binding_resolution('bind-broken', 'dangling')
        builder.note_unknown('dangling_bindings', {
            'bind-broken': {'material_name': 'missing', 'node_path': 'broken'}})
    return builder.build()


def make_state(*, with_override=False, snapshot=None):
    wood = WorkingMaterial(
        working_id='w-wood', source_material_id='src:wood', display_name='wood',
        base_snapshot_ref=snapshot,
        texture_patches=(
            (TexturePatch('basecolor', PatchAction.SET,
                          TextureUse('basecolor', 'sha256:my-wood', 'RGB', 0,
                                     'srgb', source_locator='my.png')),
             TexturePatch('roughness', PatchAction.CLEAR))
            if with_override else ()),
        property_patch_json=json.dumps(
            {'roughness_factor': {'value': 0.0},
             'metallic_factor': {'disabled': True}} if with_override else {}))
    return MaterialState(
        target=make_target(),
        materials=(wood,),
        assignments=tuple(MaterialAssignment(f'bind-{name}', 'w-wood')
                          for name in ('leg-fl', 'leg-fr', 'leg-bl', 'leg-br')))


# ---------------------------------------------------------------------------
# three-state resolution
# ---------------------------------------------------------------------------
def test_inherited_values_come_from_the_source_definition():
    catalog = make_catalog()
    state = make_state()
    wood = state.materials[0]

    base = effective_texture('basecolor', wood, catalog)
    assert base.source == SOURCE_SOURCE
    assert base.use.resource_id == 'sha256:wood-colour'
    # A source role resolves too -- it is inherited, not dropped.
    assert effective_texture('bump', wood, catalog).source == SOURCE_SOURCE
    assert effective_texture('emissive', wood, catalog).source == SOURCE_NONE
    assert effective_texture('emissive', wood, catalog).use is None


def test_an_override_wins_and_a_clear_does_not_fall_back():
    catalog = make_catalog()
    state = make_state(with_override=True)
    wood = state.materials[0]

    basecolor = effective_texture('basecolor', wood, catalog)
    assert basecolor.source == SOURCE_WORKING
    assert basecolor.use.resource_id == 'sha256:my-wood'
    # roughness was cleared: it must not silently inherit the source texture.
    roughness = effective_texture('roughness', wood, catalog)
    assert roughness.source == SOURCE_CLEARED
    assert roughness.use is None
    assert not roughness.has_texture


def test_a_library_snapshot_is_the_inheritance_base_when_present():
    catalog = make_catalog()
    snapshot = MaterialDefinition(
        material_id='lib:walnut', source_key='lib-1', name='walnut', workflow='pbr',
        textures=(TextureUse('basecolor', 'sha256:walnut', 'RGB', 0, 'srgb',
                             source_locator='walnut.png'),),
        properties_json=json.dumps({'roughness_factor': 0.2}))
    state = make_state(snapshot='sha256:snapshot-1')
    wood = state.materials[0]

    base = effective_texture('basecolor', wood, catalog,
                            snapshots={'sha256:snapshot-1': snapshot})
    assert base.source == SOURCE_SNAPSHOT
    assert base.use.resource_id == 'sha256:walnut'
    # A slot the snapshot does not state falls back to the source definition.
    assert effective_texture('roughness', wood, catalog,
                             snapshots={'sha256:snapshot-1': snapshot}
                             ).source == SOURCE_SOURCE


def test_zero_and_disabled_are_different_from_absent():
    catalog = make_catalog()
    state = make_state(with_override=True)
    wood = state.materials[0]
    values = effective_properties(wood, catalog)

    assert values['roughness_factor'].source == SOURCE_WORKING
    assert values['roughness_factor'].value == 0.0        # a real zero
    assert values['metallic_factor'].source == SOURCE_DISABLED
    assert values['metallic_factor'].value is None        # disabled, not 0
    # A key no layer states is simply absent from the mapping.
    assert 'alpha_mode' not in values


# ---------------------------------------------------------------------------
# payload round-trip
# ---------------------------------------------------------------------------
def test_state_payload_round_trip_keeps_every_field():
    state = make_state(with_override=True, snapshot='sha256:snapshot-1')
    payload = encode_state(state)
    rebuilt = decode_state(json.loads(json.dumps(payload)), state.target)

    assert rebuilt.target.state_revision == state.target.state_revision
    assert [item.working_id for item in rebuilt.materials] == ['w-wood']
    assert rebuilt.materials[0].display_name == 'wood'
    assert rebuilt.materials[0].base_snapshot_ref == 'sha256:snapshot-1'
    assert rebuilt.materials[0].texture_patches[0].value.resource_id == 'sha256:my-wood'
    assert json.loads(rebuilt.materials[0].property_patch_json) == (
        json.loads(state.materials[0].property_patch_json))
    assert rebuilt.assignments == state.assignments
    assert state_signature(rebuilt) == state_signature(state)


def test_unknown_state_fields_survive_a_round_trip():
    state = make_state()
    payload = encode_state(state)
    payload['future_field'] = {'written_by': 'prism-next'}
    payload['materials'][0]['future_material_field'] = 7

    rebuilt = decode_state(payload, state.target)
    again = encode_state(rebuilt)

    assert again['future_field'] == {'written_by': 'prism-next'}
    assert again['materials'][0]['future_material_field'] == 7


# ---------------------------------------------------------------------------
# state validation
# ---------------------------------------------------------------------------
def test_a_valid_state_passes_every_check():
    catalog = make_catalog()
    state = make_state(with_override=True)
    validate_state(state, catalog, resources={'sha256:my-wood', 'sha256:wood-colour'},
                   snapshots=())


def test_an_assignment_to_an_unknown_binding_is_refused():
    catalog = make_catalog()
    state = make_state()
    bad = MaterialState(target=state.target, materials=state.materials,
                        assignments=(MaterialAssignment('bind-nope', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(bad, catalog)
    assert caught.value.code == E_UNKNOWN_BINDING


def test_an_assignment_to_an_unknown_material_is_refused():
    catalog = make_catalog()
    state = make_state()
    bad = MaterialState(target=state.target, materials=state.materials,
                        assignments=(MaterialAssignment('bind-leg-fl', 'w-missing'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(bad, catalog)
    assert caught.value.code == E_UNKNOWN_MATERIAL


def test_a_missing_texture_resource_is_refused():
    catalog = make_catalog()
    state = make_state(with_override=True)
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog, resources={'sha256:wood-colour'})
    assert caught.value.code == E_RESOURCE_MISSING


def test_a_source_role_cannot_be_edited():
    catalog = make_catalog()
    wood = WorkingMaterial(
        working_id='w-wood', source_material_id='src:wood', display_name='wood',
        texture_patches=(TexturePatch(
            'bump', PatchAction.SET,
            TextureUse('bump', 'sha256:x', 'R', 0, 'linear')),))
    state = MaterialState(target=make_target(), materials=(wood,),
                          assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog, verified_slots=())
    assert caught.value.code == E_SLOT_READ_ONLY


def test_an_unverified_standard_slot_cannot_be_edited():
    catalog = make_catalog()
    wood = WorkingMaterial(
        working_id='w-wood', source_material_id='src:wood', display_name='wood',
        texture_patches=(TexturePatch(
            'displacement', PatchAction.SET,
            TextureUse('displacement', 'sha256:x', 'R', 0, 'linear')),))
    state = MaterialState(target=make_target(), materials=(wood,),
                          assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog, verified_slots=('basecolor', 'normal'))
    assert caught.value.code == E_SLOT_READ_ONLY
    assert 'displacement' in str(caught.value)


def test_a_set_patch_without_a_value_is_refused():
    catalog = make_catalog()
    wood = WorkingMaterial(
        working_id='w-wood', source_material_id='src:wood', display_name='wood',
        texture_patches=(TexturePatch('basecolor', PatchAction.SET, None),))
    state = MaterialState(target=make_target(), materials=(wood,),
                          assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog, verified_slots=('basecolor',))
    assert caught.value.code == E_PARAM_INVALID


def test_an_unknown_action_is_refused():
    catalog = make_catalog()
    wood = WorkingMaterial(
        working_id='w-wood', source_material_id='src:wood', display_name='wood',
        texture_patches=(TexturePatch('basecolor', 'reset', None),))
    state = MaterialState(target=make_target(), materials=(wood,),
                          assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog)
    assert caught.value.code == E_ACTION_INVALID


def test_a_dangling_usage_point_cannot_be_assigned():
    catalog = make_catalog(dangling=True)
    wood = make_state().materials[0]
    state = MaterialState(target=make_target(), materials=(wood,),
                          assignments=(MaterialAssignment('bind-broken', 'w-wood'),))
    with pytest.raises(MaterialValidationError) as caught:
        validate_state(state, catalog)
    assert caught.value.code == E_UNKNOWN_MATERIAL
    assert caught.value.detail['binding_id'] == 'bind-broken'


def test_a_duplicate_material_id_is_refused():
    catalog = CatalogBuilder('test-parser', main_path='<memory>')
    catalog.add_binding(BindingSite('b1', 's', 'n', 'g', 'p', None))
    built = catalog.build()
    validate_catalog(built)          # one unassigned site is fine
    with pytest.raises(MaterialValidationError):
        validate_catalog(type(built)(
            schema_version=1, revision=1, source_digest='',
            parser_id='p',
            definitions=(MaterialDefinition('d', 'k', 'n', 'w'),
                         MaterialDefinition('d', 'k2', 'n2', 'w')),
            binding_sites=()))


# ---------------------------------------------------------------------------
# requests: one target entry point, fixed semantics
# ---------------------------------------------------------------------------
def _request(operation, *, working_ids=(), binding_ids=(), parameters=None,
             request_id='req-1', target=None):
    return MaterialEditRequest(request_id, 'gesture-1', target or make_target(),
                               operation, tuple(working_ids), tuple(binding_ids),
                               json.dumps(parameters or {}))


def test_targets_come_from_the_id_lists_only():
    catalog = make_catalog()
    state = make_state()

    with pytest.raises(MaterialValidationError) as caught:
        parse_parameters(_request(MaterialOperation.PATCH,
                                  parameters={'working_ids': ['w-wood'],
                                              'slot': 'basecolor',
                                              'action': 'inherit'}))
    assert caught.value.code == E_PARAM_INVALID
    assert 'working_ids' in caught.value.detail['rejected']

    # The accepted form: targets in the request, parameters only for the edit.
    request = _request(MaterialOperation.PATCH, working_ids=('w-wood',),
                       parameters={'slot': 'basecolor', 'action': 'inherit'})
    assert resolve_targets(request, catalog, state) == ('w-wood',)


def test_a_content_edit_cannot_address_usage_points():
    catalog = make_catalog()
    state = make_state()
    request = _request(MaterialOperation.PATCH, binding_ids=('bind-leg-fl',),
                       parameters={'slot': 'basecolor', 'action': 'inherit'})
    with pytest.raises(MaterialValidationError) as caught:
        resolve_targets(request, catalog, state)
    assert caught.value.code == E_PARAM_INVALID


def test_an_empty_request_is_refused():
    with pytest.raises(MaterialValidationError) as caught:
        resolve_targets(_request(MaterialOperation.PATCH, parameters={
            'slot': 'basecolor', 'action': 'inherit'}), make_catalog(), make_state())
    assert caught.value.code == E_TARGET_MISSING


def test_surface_and_full_replacement_do_not_take_a_keep_switch():
    for operation in (MaterialOperation.REPLACE_SURFACE,
                      MaterialOperation.REPLACE_FULL):
        for rejected in ({'keep': []}, {'apply_defaults': False},
                         {'keep': ['opacity']}):
            parameters = dict(rejected, library_material_id='lib-1',
                              library_revision=1)
            with pytest.raises(MaterialValidationError) as caught:
                parse_parameters(_request(operation, working_ids=('w-wood',),
                                          parameters=parameters))
            assert caught.value.code == E_PARAM_INVALID
    # What a surface replacement keeps is the contract's list, not a parameter.
    assert 'opacity' in SURFACE_REPLACEMENT_KEEPS
    assert 'sss' in SURFACE_REPLACEMENT_KEEPS


def test_restore_material_takes_no_assignment_switch():
    with pytest.raises(MaterialValidationError) as caught:
        parse_parameters(_request(MaterialOperation.RESTORE_MATERIAL,
                                  working_ids=('w-wood',),
                                  parameters={'restore_assignments': True}))
    assert caught.value.code == E_PARAM_INVALID


def test_idempotency_needs_the_same_content():
    first = _request(MaterialOperation.PATCH, working_ids=('w-wood',),
                     parameters={'slot': 'basecolor', 'action': 'inherit'})
    same = _request(MaterialOperation.PATCH, working_ids=('w-wood',),
                    parameters={'slot': 'basecolor', 'action': 'inherit'})
    different = _request(MaterialOperation.PATCH, working_ids=('w-wood',),
                         parameters={'slot': 'normal', 'action': 'inherit'})

    signature = request_signature(first)
    assert request_signature(same) == signature
    recorded = {'req-1': signature}
    check_idempotent('req-1', signature, recorded)          # a true retry

    with pytest.raises(MaterialValidationError) as caught:
        check_idempotent('req-1', request_signature(different), recorded)
    assert caught.value.code == E_REQUEST_CONFLICT


def test_make_unique_copies_once_per_source_material():
    catalog = make_catalog()
    state = make_state()
    counter = {'n': 0}

    def factory(source):
        counter['n'] += 1
        return f'w-copy-{counter["n"]}'

    plan = plan_make_unique(('bind-leg-fl', 'bind-leg-fr'), state,
                            new_id_factory=factory)
    # Two targets, one source material: one copy, shared by both.
    assert plan == {'bind-leg-fl': 'w-copy-1', 'bind-leg-fr': 'w-copy-1'}

    other = MaterialState(
        target=state.target,
        materials=(state.materials[0],
                   WorkingMaterial('w-metal', None, 'metal')),
        assignments=(MaterialAssignment('bind-leg-bl', 'w-wood'),
                     MaterialAssignment('bind-leg-br', 'w-metal')))
    counter['n'] = 0
    plan = plan_make_unique(('bind-leg-bl', 'bind-leg-br'), other,
                            new_id_factory=factory)
    # Two targets, two source materials: two copies, never merged.
    assert plan == {'bind-leg-bl': 'w-copy-1', 'bind-leg-br': 'w-copy-2'}
    del catalog


# ---------------------------------------------------------------------------
# resource roots and leftover materials
# ---------------------------------------------------------------------------
def test_every_root_of_a_resource_is_listed():
    catalog = make_catalog()
    snapshot = MaterialDefinition(
        material_id='lib:walnut', source_key='lib-1', name='walnut', workflow='pbr',
        textures=(TextureUse('basecolor', 'sha256:walnut', 'RGB', 0, 'srgb'),))
    state = make_state(with_override=True, snapshot='sha256:snapshot-1')
    references = collect_references(catalog, state,
                                    snapshots={'sha256:snapshot-1': snapshot},
                                    extra_roots=('sha256:lease-preview',))

    assert 'catalog:src:wood:basecolor' in references['sha256:wood-colour']
    assert 'working:w-wood:basecolor' in references['sha256:my-wood']
    assert 'snapshot:w-wood' in references['sha256:snapshot-1']
    assert 'snapshot:sha256:snapshot-1:basecolor' in references['sha256:walnut']
    assert references['sha256:lease-preview'] == ['extra']
    # Nothing references this, so cleanup may collect it.
    assert orphaned_resources(references, ('sha256:my-wood', 'sha256:old')) == (
        'sha256:old',)


def test_a_material_nobody_assigns_is_reported_not_dropped():
    state = make_state()
    extra = MaterialState(
        target=state.target,
        materials=state.materials + (WorkingMaterial('w-spare', None, 'spare'),),
        assignments=state.assignments)
    assert leftover_materials(extra) == ('w-spare',)


def test_the_ui_gets_three_states_per_slot():
    report = editable_slot_report(('basecolor', 'normal', 'roughness'))
    assert report['basecolor'] == 'editable'
    assert report['normal'] == 'editable'
    assert report['sss'] == 'unverified'
    assert report['displacement'] == 'unverified'
    assert report['bump'] == 'source_role'
    assert 'editable' not in {report['bump'], report['specular']}
