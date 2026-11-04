"""B1 hard gate 1+2: the mapping must be one-to-one and must not confuse
"identity" with "evidence".

The first tests are the exact holes the main dialogue reproduced:

* two different bindings ending up on one ``native_id`` while ``complete`` stayed
  True;
* ``carry_over`` trusting a surviving ``binding_id`` without re-checking content,
  which let a stale node/fingerprint/handle leak into the new scene.

Both must fail loudly now. The rest pins the rules that replaced the holes:
identity comes from the source, fingerprints are evidence, and a new scene always
gets a fresh native mapping.
"""
from dataclasses import dataclass

import pytest

from prism.model_library.multimaterial.native import (
    BindingMap, CarryResult, MappingContractError, MatchEvidence, MatchFailure,
    NativeObject, NativeSlot, build_slots, carry_over, fingerprint_content,
    fingerprint_geometry, fingerprint_transform, reconcile,
)

CUBE = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
LEG = [0.15, 0.8, 0.0, -0.15, 0.8, 0.0]
UV_A = [0.0, 0.0, 1.0, 0.0, 1.0, 1.0, 0.0, 1.0]
UV_FLIPPED = [0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 0.0, 0.0]
NORMALS = [0.0, 0.0, 1.0] * 4


@dataclass(frozen=True)
class Binding:
    """Test double for a binding site (the shared contract still has to grow
    ``instance_key`` / evidence; see the RFC in the B0.1 artefacts)."""

    binding_id: str
    node_path: str
    geometry_key: str
    primitive_key: str = 'primitive:0'
    instance_key: str = ''
    geometry_fingerprint: str = ''
    content_fingerprint: str = ''


def native(name, node_path, geometry_key, primitive_key='primitive:0', *,
           points=CUBE, instance_key='', content=''):
    return NativeObject(
        native_id=name, node_path=node_path, geometry_key=geometry_key,
        primitive_key=primitive_key, instance_key=instance_key,
        geometry_fingerprint=fingerprint_geometry(points),
        content_fingerprint=content or fingerprint_content(
            uvs=UV_A, normals=NORMALS, material_groups=['mat:0']))


def evidence(points=CUBE, uvs=UV_A, groups=('mat:0',)):
    return MatchEvidence(
        geometry_fingerprint=fingerprint_geometry(points),
        content_fingerprint=fingerprint_content(uvs=uvs, normals=NORMALS,
                                                material_groups=list(groups)))


# ---------------------------------------------------------------- reproduced holes
def test_two_bindings_cannot_share_one_native_object():
    """Reproduced: two different bindings on the same native_id reported complete."""
    bindings = [Binding('b-left', 'Chair/Leg', 'mesh:4'),
                Binding('b-right', 'Chair/Arm', 'mesh:6')]
    natives = [native('actor-1', 'Chair/Leg', 'mesh:4'),
               native('actor-1', 'Chair/Arm', 'mesh:6')]
    with pytest.raises(MappingContractError, match='native_id 重复'):
        reconcile(bindings, natives)


def test_duplicate_binding_ids_are_a_contract_error():
    with pytest.raises(MappingContractError, match='binding_id 重复'):
        reconcile([Binding('dup', 'A', 'mesh:0'), Binding('dup', 'B', 'mesh:1')],
                  [native('n1', 'A', 'mesh:0')])


def test_a_binding_already_claimed_cannot_take_a_second_native_object():
    """The reproduced hole in its other shape: two bindings, one native object.

    Different ``binding_id``s that resolve to the same structural key must not both
    be reported as paired.
    """
    bindings = [Binding('b-1', 'Chair/Leg', 'mesh:4'),
                Binding('b-2', 'Chair/Leg', 'mesh:4')]
    natives = [native('actor-1', 'Chair/Leg', 'mesh:4')]
    mapping = reconcile(bindings, natives)
    assert not mapping.complete
    shared = mapping.failures(MatchFailure.SHARED_NATIVE_OBJECT)
    assert len(shared) == 1 and shared[0].binding_id == 'b-2'
    assert mapping.slots[0].binding_id == 'b-1'
    assert mapping.native_ids_for(['b-1', 'b-2']) == ('actor-1',)


def test_build_slots_refuses_a_native_id_handed_out_twice():
    """Reproduced: build_slots accepted the same handle for two bindings."""
    created = []

    def make(binding):
        created.append(binding.binding_id)
        return 'actor-shared'

    bindings = [Binding('b-1', 'A', 'mesh:0'), Binding('b-2', 'B', 'mesh:1')]
    mapping = build_slots(bindings, make)
    assert created == ['b-1', 'b-2']
    duplicate = mapping.failures(MatchFailure.DUPLICATE_NATIVE_ID)
    assert len(duplicate) == 1
    assert duplicate[0].binding_id == 'b-2'
    assert not mapping.complete
    assert len(mapping.slots) == 1


def test_carry_over_rechecks_content_even_when_the_binding_id_survives():
    """Reproduced: a surviving binding_id skipped content checks and kept the old
    handle. Now the assignment is refused instead."""
    previous = reconcile([Binding('gltf|node:5', 'Chair/Leg', 'mesh:4')],
                         [native('actor-1', 'Chair/Leg', 'mesh:4')],
                         bindings_evidence={'gltf|node:5': evidence()})
    assert previous.complete

    changed_binding = Binding('gltf|node:5', 'Chair/Leg', 'mesh:9')
    result = carry_over(previous, [changed_binding], {'gltf|node:5': 'working-wood'},
                        bindings_evidence={'gltf|node:5': evidence(points=LEG)})
    assert result.failures(MatchFailure.CONTENT_CHANGED)
    assert result.working_id_for('gltf|node:5') is None


def test_carry_over_refuses_when_the_identity_behind_the_id_changed():
    previous = reconcile([Binding('gltf|node:5', 'Chair/Leg', 'mesh:4')],
                         [native('actor-1', 'Chair/Leg', 'mesh:4')],
                         bindings_evidence={'gltf|node:5': evidence()})
    moved = Binding('gltf|node:5', 'Chair/Arm', 'mesh:4')
    result = carry_over(previous, [moved], {'gltf|node:5': 'working-wood'},
                        bindings_evidence={'gltf|node:5': evidence()})
    assert result.failures(MatchFailure.IDENTITY_CHANGED)


def test_carry_over_never_carries_a_render_handle():
    previous = reconcile([Binding('b', 'Chair/Leg', 'mesh:4')],
                         [native('actor-1', 'Chair/Leg', 'mesh:4')],
                         bindings_evidence={'b': evidence()})
    result = carry_over(previous, [Binding('b', 'Chair/Leg', 'mesh:4')],
                        {'b': 'working-wood'}, bindings_evidence={'b': evidence()})
    assert result.complete
    assert result.working_id_for('b') == 'working-wood'
    transfer = result.transfers[0]
    assert not hasattr(transfer, 'native_id')
    assert transfer.matched_by == 'identity+content'


# ------------------------------------------------------- identity vs evidence
def test_source_instance_identity_separates_identical_parts():
    """Two GPU instances with the *same* transform and geometry are still two parts
    when the source names them; only the identity distinguishes them."""
    same_transform = fingerprint_transform([1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1])
    bindings = [Binding('inst-0', 'Grass', 'mesh:7', instance_key='node:3/instance:0'),
                Binding('inst-1', 'Grass', 'mesh:7', instance_key='node:3/instance:1')]
    natives = [native('n0', 'Grass', 'mesh:7', instance_key='node:3/instance:0'),
               native('n1', 'Grass', 'mesh:7', instance_key='node:3/instance:1')]
    mapping = reconcile(bindings, natives)
    assert mapping.complete
    assert mapping.slot_for('inst-1').native_id == 'n1'
    assert same_transform  # the transform digest is evidence, not the identity


def test_reconcile_without_source_instance_identity_refuses_to_choose():
    """When the native side cannot prove which instance is which, refuse."""
    bindings = [Binding('inst-0', 'Grass', 'mesh:7', instance_key='node:3/instance:0'),
                Binding('inst-1', 'Grass', 'mesh:7', instance_key='node:3/instance:1')]
    natives = [native('n0', 'Grass', 'mesh:7', instance_key=''),
               native('n1', 'Grass', 'mesh:7', instance_key='')]
    mapping = reconcile(bindings, natives)
    assert not mapping.complete
    assert len(mapping.failures(MatchFailure.MISSING_NATIVE)) == 2


def test_content_fingerprint_catches_uv_changes_that_geometry_cannot():
    """A position/index digest cannot prove UVs are unchanged."""
    bindings = [Binding('b', 'Panel', 'mesh:2')]
    natives = [native('actor-1', 'Panel', 'mesh:2',
                      content=fingerprint_content(uvs=UV_FLIPPED, normals=NORMALS,
                                                  material_groups=['mat:0']))]
    mapping = reconcile(bindings, natives,
                        bindings_evidence={'b': evidence(uvs=UV_A)})
    assert mapping.failures(MatchFailure.CONTENT_MISMATCH)
    assert not mapping.slots


def test_content_fingerprint_catches_material_group_changes():
    bindings = [Binding('b', 'Panel', 'mesh:2')]
    natives = [native('actor-1', 'Panel', 'mesh:2',
                      content=fingerprint_content(uvs=UV_A, normals=NORMALS,
                                                  material_groups=['mat:1']))]
    mapping = reconcile(bindings, natives,
                        bindings_evidence={'b': evidence(groups=('mat:0',))})
    assert mapping.failures(MatchFailure.CONTENT_MISMATCH)


def test_content_fingerprint_tracks_two_sided_and_alpha_mode():
    a = fingerprint_content(uvs=UV_A, normals=NORMALS, material_groups=['mat:0'],
                            two_sided=False, alpha_mode='OPAQUE')
    b = fingerprint_content(uvs=UV_A, normals=NORMALS, material_groups=['mat:0'],
                            two_sided=True, alpha_mode='OPAQUE')
    c = fingerprint_content(uvs=UV_A, normals=NORMALS, material_groups=['mat:0'],
                            two_sided=False, alpha_mode='MASK')
    assert len({a, b, c}) == 3


# --------------------------------------------------------------- normal paths
def test_duplicate_binding_ids_are_a_contract_error_in_build_slots():
    with pytest.raises(MappingContractError, match='binding_id 重复'):
        build_slots([Binding('dup', 'A', 'mesh:0'), Binding('dup', 'B', 'mesh:1')],
                    lambda binding: f'n::{binding.node_path}')


def test_duplicate_binding_ids_are_a_contract_error_in_carry_over():
    previous = reconcile([Binding('b', 'A', 'mesh:0')], [native('n1', 'A', 'mesh:0')])
    with pytest.raises(MappingContractError, match='binding_id 重复'):
        carry_over(previous, [Binding('dup', 'A', 'mesh:0'), Binding('dup', 'B', 'mesh:1')],
                   {'dup': 'w'})


def test_same_position_same_colour_pair_by_structure_not_by_look():
    bindings = [Binding('b1', 'Body/Post', 'mesh:0'), Binding('b2', 'Body/Post2', 'mesh:1')]
    natives = [native('actor-1', 'Body/Post', 'mesh:0'), native('actor-2', 'Body/Post2', 'mesh:1')]
    mapping = reconcile(bindings, natives)
    assert mapping.complete
    assert mapping.slot_for('b1').native_id == 'actor-1'


def test_multi_primitive_mesh_yields_one_slot_per_primitive():
    bindings = [Binding('p0', 'Cabinet', 'mesh:2', 'primitive:0'),
                Binding('p1', 'Cabinet', 'mesh:2', 'primitive:1')]
    natives = [native('a0', 'Cabinet', 'mesh:2', 'primitive:0', points=CUBE),
               native('a1', 'Cabinet', 'mesh:2', 'primitive:1', points=LEG)]
    mapping = reconcile(bindings, natives)
    assert mapping.complete
    assert [slot.native_id for slot in mapping.slots] == ['a0', 'a1']


def test_reordered_arrays_carry_assignments_by_content():
    before = reconcile([Binding('gltf|node:5', 'Chair/Leg', 'mesh:4')],
                       [native('actor-1', 'Chair/Leg', 'mesh:4')],
                       bindings_evidence={'gltf|node:5': evidence()})
    after_bindings = [Binding('gltf|node:1', 'Chair/Leg', 'mesh:9')]
    moved = carry_over(before, after_bindings, {'gltf|node:5': 'working-wood'},
                       bindings_evidence={'gltf|node:1': evidence()})
    assert moved.complete
    assert moved.working_id_for('gltf|node:1') == 'working-wood'
    assert moved.transfers[0].matched_by == 'content'
    assert moved.transfers[0].previous_binding_id == 'gltf|node:5'


def test_ambiguous_content_is_refused():
    same = dict(node_path='Leg', geometry_key='mesh:4', primitive_key='primitive:0',
                instance_key='', geometry_fingerprint=fingerprint_geometry(CUBE),
                content_fingerprint=fingerprint_content(uvs=UV_A, normals=NORMALS,
                                                        material_groups=['mat:0']))
    previous = BindingMap(slots=(NativeSlot(binding_id='a', native_id='n1', **same),
                                 NativeSlot(binding_id='b', native_id='n2', **same)))
    result = carry_over(previous, [Binding('new', 'Leg', 'mesh:9')],
                        {'a': 'w1', 'b': 'w2'}, bindings_evidence={'new': evidence()})
    assert result.failures(MatchFailure.FINGERPRINT_COLLISION)
    assert result.working_id_for('new') is None


def test_unassigned_binding_is_reported_not_silently_skipped():
    previous = reconcile([Binding('b', 'Leg', 'mesh:4')], [native('n1', 'Leg', 'mesh:4')],
                         bindings_evidence={'b': evidence()})
    result = carry_over(previous, [Binding('b', 'Leg', 'mesh:4')], {},
                        bindings_evidence={'b': evidence()})
    assert result.complete
    assert result.unassigned == ('b',)
    assert result.transfers == ()


def test_geometry_change_is_refused_in_reconcile():
    bindings = [Binding('b1', 'Chair/Leg', 'mesh:4')]
    natives = [native('a1', 'Chair/Leg', 'mesh:4', points=LEG)]
    mapping = reconcile(bindings, natives, bindings_evidence={'b1': evidence()})
    assert mapping.failures(MatchFailure.GEOMETRY_MISMATCH)


def test_leftover_native_object_is_reported():
    bindings = [Binding('b1', 'Chair/Leg', 'mesh:4')]
    natives = [native('a1', 'Chair/Leg', 'mesh:4'), native('ghost', 'Chair/Arm', 'mesh:6')]
    mapping = reconcile(bindings, natives)
    leftover = mapping.failures(MatchFailure.MISSING_BINDING)
    assert len(leftover) == 1 and leftover[0].native_id == 'ghost'


def test_build_slots_reports_creation_failure():
    def make(binding):
        if binding.binding_id == 'boom':
            raise RuntimeError('shader 编译失败')
        return f'native::{binding.binding_id}'

    bindings = [Binding('ok-1', 'A', 'mesh:0'), Binding('boom', 'B', 'mesh:1')]
    mapping = build_slots(bindings, make)
    assert [slot.binding_id for slot in mapping.slots] == ['ok-1']
    assert mapping.failures(MatchFailure.MISSING_NATIVE)[0].binding_id == 'boom'


def test_carry_result_is_a_distinct_type_from_binding_map():
    """Assignments and object pairings must not be confused by callers."""
    assert CarryResult is not BindingMap
    assert not hasattr(CarryResult(), 'slot_for')
