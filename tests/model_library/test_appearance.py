"""C4: the existing slot conventions, adapted into a detached appearance.

The model library does not own a second slot system. It reuses
``MaterialSlot``, ``prism.material_parameters`` and ``prism.material_textures``
and stores the result as a snapshot. These tests pin down the three rules that
make that safe:

* what is stored is detached, so a later edit of the source cannot reach it;
* a slider drag is one appearance revision, not one revision per pixel;
* a channel the native runtime has not verified is blocked *with a reason*,
  never silently ignored.
"""
import json

import pytest

from prism.material_textures import SLOTS
from prism.model_library import appearance as adapter
from prism.model_library.contracts import ModelAppearance, ModelRecord


# ----------------------------------------------------------------------
# Detached snapshots
# ----------------------------------------------------------------------
def test_an_appearance_snapshot_shares_no_state_with_its_source():
    source = ModelAppearance(revision=2, bindings={'basecolor': {'sha256': 'a'}},
                             properties={'slots': {'basecolor': {'value': 0.4}}})
    snapshot = adapter.detached_appearance(source)
    snapshot.bindings['basecolor']['sha256'] = 'changed'
    snapshot.properties['slots']['basecolor']['value'] = 1.0

    assert source.bindings['basecolor']['sha256'] == 'a'
    assert source.properties['slots']['basecolor']['value'] == 0.4


def test_the_payload_round_trip_is_json_safe():
    appearance = ModelAppearance(
        revision=3,
        bindings={'normal': {'sha256': 'abc', 'channel': 'RGB',
                             'attachmentId': 7, 'name': 'n.png'}},
        properties={'slots': {'normal': {'invert': True, 'value': 1.0}}})
    payload = adapter.appearance_payload(appearance)
    wire = json.loads(json.dumps(payload))
    restored = adapter.appearance_from_payload(wire)

    assert restored.revision == 3
    assert restored.bindings == appearance.bindings
    assert restored.properties == appearance.properties
    assert adapter.appearance_signature(restored) == \
        adapter.appearance_signature(appearance)


def test_the_appearance_of_a_record_is_read_from_the_full_record_only():
    record = ModelRecord(
        model_id='m1', project_id='p', name='m1', main_file_id='f1',
        format='fbx',
        appearance=ModelAppearance(revision=4, bindings={'ao': {'sha256': 'z'}}))
    appearance = adapter.appearance_from_record(record)
    assert appearance.revision == 4
    assert appearance.bindings == {'ao': {'sha256': 'z'}}

    # A card summary has no appearance at all; nothing is invented from it.
    from prism.model_library.contracts import ModelCardSummary
    assert adapter.appearance_from_record(ModelCardSummary('m1', 'm1', 'fbx')) \
        == ModelAppearance()


def test_two_appearances_with_the_same_configuration_share_a_signature():
    one = ModelAppearance(revision=1, bindings={'ao': {'sha256': 'x', 'channel': 'R'}},
                          properties={'slots': {'ao': {'value': 0.5}}})
    two = ModelAppearance(revision=9, bindings={'ao': {'sha256': 'x', 'channel': 'R'}},
                          properties={'slots': {'ao': {'value': 0.5}}})
    other = ModelAppearance(revision=1, bindings={'ao': {'sha256': 'y', 'channel': 'R'}},
                            properties={'slots': {'ao': {'value': 0.5}}})

    assert adapter.appearance_signature(one) == adapter.appearance_signature(two)
    assert adapter.appearance_signature(one) != adapter.appearance_signature(other)


# ----------------------------------------------------------------------
# Editing
# ----------------------------------------------------------------------
def test_a_slot_patch_is_validated_with_the_existing_rules():
    with pytest.raises(ValueError):
        adapter.apply_slot_patch(ModelAppearance(), 'basecolor', {'value': 5.0})
    with pytest.raises(ValueError):
        adapter.apply_slot_patch(ModelAppearance(), 'no-such-slot', {'value': 1.0})
    with pytest.raises(ValueError):
        adapter.apply_slot_patch(ModelAppearance(), 'ao', {'unknown': True})


def test_a_slot_patch_changes_only_its_own_slot():
    base = adapter.apply_slot_patch(
        ModelAppearance(), 'roughness', {'value': 0.25})
    patched = adapter.apply_slot_patch(base, 'metallic', {'value': 0.75})

    assert adapter.slot_state(patched, 'roughness')['value'] == 0.25
    assert adapter.slot_state(patched, 'metallic')['value'] == 0.75
    assert patched.revision == base.revision + 1


def test_a_slider_drag_becomes_one_revision_not_one_per_step():
    """C4.6: undo has to group real user actions, not pixels."""
    session = adapter.AppearanceSession(ModelAppearance(revision=5))
    for step in range(40):
        assert session.patch('basecolor', {'value': 0.1 + step * 0.002})

    working = session.working
    assert session.dirty
    assert working.revision != 5, '工作副本是编辑中的状态，还不是已提交的修订'
    assert adapter.slot_state(working, 'basecolor')['value'] == pytest.approx(0.178)

    committed = session.commit()
    assert committed.revision == 6, '一次拖动只提交一个新外观修订'
    assert adapter.slot_state(committed, 'basecolor')['value'] == pytest.approx(0.178)
    assert not session.dirty


def test_committing_an_untouched_session_keeps_the_original_revision():
    session = adapter.AppearanceSession(ModelAppearance(revision=2))
    assert session.commit().revision == 2


def test_discarding_a_session_returns_to_the_stored_appearance():
    session = adapter.AppearanceSession(ModelAppearance(revision=2))
    session.patch('ao', {'value': 0.9})
    session.discard()
    assert adapter.slot_state(session.working, 'ao')['value'] == 1.0
    assert not session.dirty


def test_attaching_and_clearing_a_texture_binding():
    session = adapter.AppearanceSession()
    assert session.set_binding('normal', {'sha256': 'abc', 'channel': 'RGB'})
    assert adapter.slot_binding(session.working, 'normal')['sha256'] == 'abc'

    assert session.set_channel('normal', 'R')
    assert adapter.slot_binding(session.working, 'normal')['channel'] == 'R'

    assert session.set_binding('normal', None)
    assert adapter.slot_binding(session.working, 'normal') is None


# ----------------------------------------------------------------------
# Applying a material from the material library
# ----------------------------------------------------------------------
def test_applying_a_material_takes_a_snapshot_of_that_moment():
    """C4.4: editing the source material later must not move the model."""
    material = {'textures': {'basecolor': {'sha256': 'tex-1', 'channel': 'RGB'}},
                'preview': {'properties': {'slots': {'basecolor': {'value': 0.6}}}}}
    session = adapter.AppearanceSession()
    assert session.apply_material(material)

    # The material record is mutated afterwards, as a later edit would.
    material['textures']['basecolor']['sha256'] = 'tex-2'
    material['preview']['properties']['slots']['basecolor']['value'] = 0.1

    appearance = session.working
    assert adapter.slot_binding(appearance, 'basecolor')['sha256'] == 'tex-1'
    assert adapter.slot_state(appearance, 'basecolor')['value'] == 0.6


def test_applying_a_material_skips_unknown_slots_instead_of_inventing_them():
    material = {'textures': {'basecolor': {'sha256': 'a'},
                             'not-a-channel': {'sha256': 'b'}}}
    appearance = adapter.appearance_from_material_record(material)
    assert set(appearance.bindings) == {'basecolor'}


# ----------------------------------------------------------------------
# Slot policy
# ----------------------------------------------------------------------
def test_unverified_channels_are_blocked_with_a_reason():
    policy = adapter.resolve_slot_policy()
    for slot in ('sss', 'displacement'):
        assert policy[slot], '未验证的通道必须带着原因被禁用'
    assert 'sss' not in adapter.usable_slots(policy)
    assert 'basecolor' in adapter.usable_slots(policy)


def test_the_native_runtime_can_open_a_channel_it_verified():
    policy = adapter.resolve_slot_policy({'sss': True, 'basecolor': False})
    assert 'sss' not in policy
    assert policy['basecolor'] == '原生运行时未验证该通道'
    # Unknown keys never enter the policy.
    assert 'not-a-slot' not in adapter.resolve_slot_policy({'not-a-slot': True})


def test_an_edit_on_a_blocked_channel_is_refused_and_explained():
    session = adapter.AppearanceSession(supported={'sss': '未验证 SSS'})
    assert session.patch('sss', {'value': 1.0}) is False
    assert session.block_reason('sss') == '未验证 SSS'
    assert not session.dirty, '被拒的编辑不能悄悄改到工作副本'


def test_applying_a_material_reports_the_channels_it_had_to_drop():
    session = adapter.AppearanceSession(supported={'sss': '未验证 SSS'})
    complete = session.apply_material({'textures': {'sss': {'sha256': 'x'},
                                                   'ao': {'sha256': 'y'}}})
    assert complete is False, '有通道被丢掉时必须报告，不能装作完整应用'
    assert adapter.slot_binding(session.working, 'sss') is None
    assert adapter.slot_binding(session.working, 'ao')['sha256'] == 'y'


# ----------------------------------------------------------------------
# Collecting from the real controls
# ----------------------------------------------------------------------
def test_collecting_from_live_slots_produces_a_detached_snapshot(qtbot):
    from prism.widgets.material_slots import MaterialSlot

    slots = {}
    for key in ('basecolor', 'roughness'):
        slot = MaterialSlot(key, SLOTS[key])
        qtbot.addWidget(slot)
        slots[key] = slot
    slots['basecolor'].enabled.setChecked(False)
    slots['basecolor'].invert.setChecked(True)
    slots['basecolor'].amount.setValue(0.25)
    slots['roughness'].amount.setValue(0.8)

    stored = ModelAppearance(revision=3, bindings={
        'roughness': {'sha256': 'r', 'channel': 'R', 'name': 'r.png'}},
        properties={'slots': {'emissive': {'value': 2.0}}})
    collected = adapter.collect_appearance(
        slots.items(), stored, adapter.appearance_properties(stored))

    assert isinstance(collected, ModelAppearance)
    assert collected.revision == 3
    assert collected.bindings['roughness']['sha256'] == 'r'
    assert adapter.slot_state(collected, 'basecolor')['enabled'] is False
    assert adapter.slot_state(collected, 'basecolor')['invert'] is True
    assert adapter.slot_state(collected, 'basecolor')['value'] == pytest.approx(0.25)
    assert adapter.slot_state(collected, 'roughness')['factor'] == pytest.approx(0.8)
    # A slot this session did not touch keeps its stored value.
    assert adapter.slot_state(collected, 'emissive')['value'] == 2.0

    # Nothing in the result points back at a widget or a live dictionary.
    collected.bindings['roughness']['sha256'] = 'changed'
    assert stored.bindings['roughness']['sha256'] == 'r'
