"""C2 through the real panel: three states, provenance, gestures, drafts."""
import json

from prism.model_library.multimaterial.contracts import (
    MaterialCatalog, MaterialDefinition, MaterialState,
    MaterialAssignment, WorkingMaterial)
from prism.widgets.model_material_editor import (
    SOURCE_CLEARED, SOURCE_ORIGINAL, STATE_CLEAR, STATE_INHERIT,
    STATE_SET, build_material_view)
from prism.widgets.model_material_list import build_rows
from tests.model_library.multimaterial.conftest import make_target, start_preview


def _slot_param(call):
    request = call[1]
    return json.loads(request.parameters_json)['patch']


def _patch_catalog(display_name='木材'):
    catalog = MaterialCatalog(
        1, 12, 'digest', 'parser',
        definitions=(MaterialDefinition('mat-wood', 'src-wood', display_name, 'pbr'),),
        binding_sites=())
    state = MaterialState(
        make_target(),
        materials=(WorkingMaterial('w-wood', 'mat-wood', display_name),),
        assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),))
    return catalog, state


# ----------------------------------------------------------------------
# Three states and provenance
# ----------------------------------------------------------------------
def test_clear_and_inherit_are_not_the_same_state(loaded):
    loaded.select_material('w-wood')
    view = loaded.material_view()
    assert view.slot('basecolor').state == STATE_INHERIT
    assert view.slot('basecolor').source_label == SOURCE_ORIGINAL
    # The catalog's normal slot is inherited; the same slot explicitly cleared
    # must read differently even though neither shows an image.
    from prism.model_library.multimaterial.contracts import PatchAction, TexturePatch
    working = loaded._working('w-wood')
    cleared = WorkingMaterial('w-wood', 'mat-wood', '木材',
                              texture_patches=(TexturePatch('normal', PatchAction.CLEAR),))
    view = build_material_view(
        loaded._catalog.definitions[0], cleared)
    normal = view.slot('normal')
    assert normal.state == STATE_CLEAR
    assert normal.source_label == SOURCE_CLEARED
    assert normal.binding == {}
    assert normal.state != STATE_INHERIT
    assert normal.state != STATE_SET
    assert working is not None


def test_a_pure_numeric_material_is_legal(loaded):
    catalog, state = _patch_catalog()
    loaded.publish_catalog(make_target(), catalog, state)
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    assert loaded.editor.editable() is True
    assert slot.amount.isEnabled()
    # A numeric-only material has no image: the texture controls say so, and
    # the number is still a real, editable value.
    assert slot.channel.isEnabled() is False
    assert slot.amount.value() == pytest_value('roughness')
    slot.amount.setValue(0.31)
    commits = loaded.port.calls_of('commit')
    assert commits, '数值修改必须提交'
    assert _slot_param(commits[-1])['slots']['roughness']['value'] == 0.31


def pytest_value(slot):
    from prism.material_parameters import RANGES
    return RANGES[slot][2]


def test_a_numeric_override_is_labelled_as_a_local_override(loaded):
    from prism.model_library.multimaterial.contracts import MaterialDefinition as Definition
    working = WorkingMaterial('w-wood', 'mat-wood', '木材', property_patch_json=json.dumps(
        {'slots': {'roughness': {'value': .25}}}))
    view = build_material_view(Definition('mat-wood', 'src', '木材', 'pbr'), working)
    roughness = view.slot('roughness')
    assert roughness.numeric_override is True
    assert '数值覆盖' in roughness.source_label


def test_a_snapshot_base_is_named_as_a_snapshot(loaded):
    from prism.model_library.multimaterial.contracts import MaterialDefinition as Definition
    working = WorkingMaterial('w-wood', 'mat-wood', '木材', base_snapshot_ref='snap-1')
    view = build_material_view(Definition('snap-1', 'src', '木纹快照', 'pbr'), working,
                              base_kind='snapshot')
    assert view.slots[0].source_label.startswith('库快照')
    assert '快照' in view.source_note


def test_an_unknown_base_says_so_instead_of_inventing_numbers(loaded):
    working = WorkingMaterial('w-wood', 'mat-wood', '木材')
    view = build_material_view(None, working)
    assert view.unknown_base is True
    assert view.slots[0].source_label == '原始值未取得'
    assert '原始定义未取得' in view.source_note


def test_cleared_slots_keep_their_numbers(loaded):
    from prism.model_library.multimaterial.contracts import PatchAction, TexturePatch
    catalog, state = _patch_catalog()
    cleared = WorkingMaterial(
        'w-wood', 'mat-wood', '木材',
        texture_patches=(TexturePatch('basecolor', PatchAction.CLEAR),))
    catalog = MaterialCatalog(1, 13, 'digest', 'parser',
                              definitions=(MaterialDefinition(
                                  'mat-wood', 'src-wood', '木材', 'pbr',
                                  textures=()),),
                              binding_sites=())
    state = MaterialState(make_target(), (cleared,), state.assignments)
    loaded.publish_catalog(make_target(), catalog, state)
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['basecolor']
    assert loaded.editor.bars['basecolor'].state() == STATE_CLEAR
    assert slot.amount.isEnabled(), '清除贴图后数值仍然有效'
    assert slot.channel.isEnabled() is False


# ----------------------------------------------------------------------
# Gestures
# ----------------------------------------------------------------------
def test_a_slider_burst_coalesces_to_latest_and_commits_once(loaded):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    before_commits = len(loaded.port.calls_of('commit'))
    slot.slider.setSliderDown(True)
    for value in (120, 260, 400):
        slot.slider.setValue(value)
    slot.slider.setSliderDown(False)
    previews = loaded.port.calls_of('preview')
    # A synchronous burst is one latest intent, not one native job per value.
    assert len(previews) == 1
    import json
    assert json.loads(previews[-1][1].parameters_json)['patch']['slots']['roughness']['value'] == .4
    gesture_ids = {call[1].gesture_id for call in previews}
    assert len(gesture_ids) == 1, '一次拖动属于同一个 gesture'
    generations = [call[2] for call in previews]
    assert generations == sorted(generations)
    assert len(set(generations)) == len(generations), '预览代次必须递增'
    commits = loaded.port.calls_of('commit')
    assert len(commits) - before_commits == 1, '一次手势一次提交（撤销一条）'
    assert commits[-1][1].gesture_id == gesture_ids.pop()


def test_switching_the_target_commits_the_pending_gesture_first(loaded):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    slot.slider.setSliderDown(True)
    slot.slider.setValue(300)
    loaded.select_material('w-metal')
    commits = loaded.port.calls_of('commit')
    assert commits, '切换材质前必须处理未完成的手势'
    request = commits[-1][1]
    assert tuple(request.working_ids) == ('w-wood',), '旧回调不得写到新目标'


def test_a_texture_state_change_is_one_discrete_transaction(loaded):
    loaded.select_material('w-wood')
    before = len(loaded.port.calls_of('commit'))
    loaded.editor.bars['normal'].buttons[STATE_CLEAR].click()
    commits = loaded.port.calls_of('commit')
    assert len(commits) - before == 1
    parameters = json.loads(commits[-1][1].parameters_json)['patch']
    assert parameters['texture']['slot'] == 'normal'
    assert parameters['texture']['action'] == 'clear'
    assert tuple(commits[-1][1].working_ids) == ('w-wood',)


def test_a_channel_change_is_forwarded_as_a_texture_intent(loaded):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['basecolor']
    index = slot.channel.findData('G')
    slot.channel.setCurrentIndex(index)
    parameters = json.loads(loaded.port.calls_of('commit')[-1][1].parameters_json)['patch']
    assert parameters['texture'] == {'slot': 'basecolor', 'action': 'channel',
                                     'channel': 'G'}


# ----------------------------------------------------------------------
# Failure handling
# ----------------------------------------------------------------------
def test_a_failed_commit_keeps_the_draft_and_offers_a_retry(loaded):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    slot.amount.setValue(0.42)
    assert '保存中' in loaded.save_status.text()
    assert '已保存' not in loaded.save_status.text()
    loaded.publish_commit_failed(make_target(), '磁盘已满')
    assert '保存失败' in loaded.save_status.text()
    assert '磁盘已满' in loaded.save_status.text()
    assert slot.amount.value() == 0.42, '失败不能悄悄跳回旧值'
    assert '未保存' in loaded.editor.draft_failure()
    assert not loaded.editor.retry.isVisibleTo(loaded.editor) is True
    before = len(loaded.port.calls_of('commit'))
    loaded.editor.retry.click()
    assert len(loaded.port.calls_of('commit')) == before + 1, '重试必须重新提交草稿'


def test_a_preview_failure_does_not_claim_the_edit_landed(loaded):
    request_id, generation = start_preview(loaded, loaded.port)
    loaded.publish_preview_failed(make_target(), 'GPU 设备丢失',
                                    request_id=request_id, draft_generation=generation)
    assert '预览失败' in loaded.preview_status.text()
    assert 'GPU 设备丢失' in loaded.preview_status.text()
    assert '已保存' not in loaded.save_status.text()


# ----------------------------------------------------------------------
# Capability limits
# ----------------------------------------------------------------------
def test_unverified_render_effects_do_not_disable_texture_management(loaded):
    loaded.select_material('w-wood')
    for name in ('sss', 'displacement', 'basecolor'):
        assert loaded.editor.slots[name].isEnabled()


def test_an_unsupported_edit_capability_keeps_the_editor_read_only(loaded, port):
    from prism.model_library.multimaterial.contracts import MaterialCapabilityReport
    port.report = MaterialCapabilityReport(
        read_catalog=True, edit_material=False, reason='后端未验证局部编辑')
    loaded.set_capabilities(port.report)
    loaded.select_material('w-wood')
    assert loaded.editor.editable() is False
    assert '后端未验证局部编辑' in loaded.editor.notice.text()
    assert not loaded.actions['replace'].isEnabled()
    assert '后端未验证局部编辑' in loaded.action_reasons['replace']


def test_renaming_a_material_does_not_rebind_the_selection(loaded):
    loaded.select_material('w-wood')
    catalog, state = _patch_catalog(display_name='胡桃木（改名）')
    loaded.publish_catalog(make_target(), catalog, state)
    row = loaded.material_row('w-wood')
    assert row is not None and row.name == '胡桃木（改名）'
    assert loaded.selected_working_id == 'w-wood', '改名不改变材质身份'
    assert loaded.editor.title.text() == '胡桃木（改名）'


def test_the_material_list_keeps_a_stable_identity_for_each_row():
    catalog, state = _patch_catalog()
    rows = build_rows(catalog, state)
    assert [row.key for row in rows] == ['working:w-wood']


def test_long_drag_renders_between_ticks_and_discards_queued_old_target(loaded, qtbot):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    slot.slider.setSliderDown(True)
    slot.slider.setValue(100)
    qtbot.waitUntil(lambda: len(loaded.port.calls_of('preview')) == 1)
    slot.slider.setValue(200)
    qtbot.waitUntil(lambda: len(loaded.port.calls_of('preview')) == 2)
    slot.slider.setValue(300)
    loaded.discard_edit()
    qtbot.wait(60)
    assert len(loaded.port.calls_of('preview')) == 2
    assert not loaded._preview_updates.isActive()


def test_preview_carries_all_changes_in_current_gesture(loaded):
    loaded.select_material('w-wood')
    editor = loaded.editor
    editor._slider_pressed()
    editor._options_changed('roughness', {'value': .27})
    editor._options_changed('roughness', {'invert': True})
    editor.flush_gesture()
    import json
    previews = loaded.port.calls_of('preview')
    assert len(previews) == 1
    patch = json.loads(previews[0][1].parameters_json)['patch']['slots']['roughness']
    assert patch == {'value': .27, 'invert': True}


def test_new_preview_does_not_restore_old_committed_frame(loaded, qtbot):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    slot.slider.setSliderDown(True)
    slot.slider.setValue(100)
    qtbot.waitUntil(lambda: len(loaded.port.calls_of('preview')) == 1)
    cancels = len(loaded.port.calls_of('cancel'))
    slot.slider.setValue(200)
    qtbot.waitUntil(lambda: len(loaded.port.calls_of('preview')) == 2)
    # Fake port has no supersede method: don't fall back to restore-producing cancel.
    assert len(loaded.port.calls_of('cancel')) == cancels
    slot.slider.setSliderDown(False)
    assert len(loaded.port.calls_of('commit')) == 1


def test_material_name_edit_is_inline_and_escape_cancels(loaded,qtbot,monkeypatch):
    from PyQt6 import QtWidgets, QtCore
    from unittest.mock import Mock
    loaded.select_material('w-wood')
    monkeypatch.setattr(QtWidgets.QInputDialog,'getText',Mock(side_effect=AssertionError('No rename dialog')))
    before=len(loaded.port.calls_of('commit'))
    loaded.rename_material(loaded._current_row())
    edit=loaded.materials._inline_name_editor
    assert isinstance(edit,QtWidgets.QLineEdit)
    edit.setText('Inline wood')
    qtbot.keyClick(edit,QtCore.Qt.Key.Key_Return)
    import json
    request=loaded.port.calls_of('commit')[-1][1]
    assert json.loads(request.parameters_json)['patch']['display_name']=='Inline wood'
    assert len(loaded.port.calls_of('commit'))==before+1
    qtbot.wait(1)
    loaded.rename_material(loaded._current_row())
    edit=loaded.materials._inline_name_editor
    edit.setText('Cancel name')
    qtbot.keyClick(edit,QtCore.Qt.Key.Key_Escape)
    assert len(loaded.port.calls_of('commit'))==before+1
