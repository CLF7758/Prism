"""C4 through the real panel: save states, recovery, undo order, Delete safety."""
from PyQt6 import QtCore, QtWidgets

from prism.model_library.multimaterial.contracts import (
    MaterialCommitReceipt, MaterialRenderReceipt)
from tests.model_library.multimaterial.conftest import (
    make_target, modified_catalog, start_preview)


# ----------------------------------------------------------------------
# Save states
# ----------------------------------------------------------------------
def test_a_job_id_is_never_reported_as_a_save(loaded):
    loaded.select_material('w-wood')
    loaded.editor.slots['roughness'].amount.setValue(0.4)
    text = loaded.save_status.text()
    assert '保存中' in text
    assert '等待保存回执' in text
    assert '已保存' not in text


def test_a_commit_receipt_is_what_saves_and_the_cover_stays_pending(loaded, qtbot):
    loaded.select_material('w-wood')
    loaded.editor.slots['roughness'].amount.setValue(0.4)
    request = loaded.port.calls_of('commit')[-1][1]
    after = make_target(state_revision=5)
    assert loaded.publish_commit(MaterialCommitReceipt(
        request.request_id, make_target(), after, 'cmd-1')) is True
    assert '已保存' in loaded.save_status.text()
    assert 'cmd-1' in loaded.save_status.text()
    assert loaded.cover_status.isVisibleTo(loaded) is False
    assert '封面待更新' in loaded.cover_status.text()
    # The cover is only "updated" when the cover receipt arrives.
    assert loaded.publish_cover_updated(after) is True
    assert loaded.cover_status.text() == '封面已更新'


def test_a_receipt_for_another_session_is_ignored(loaded):
    stale = make_target(state_revision=99)
    receipt = MaterialCommitReceipt('request', stale, stale, 'cmd-9')
    assert loaded.publish_commit(receipt) is False
    assert loaded.stale_publications() == 1


def test_the_rendered_frame_is_not_the_saved_state(loaded, port):
    request_id, generation = start_preview(loaded, port)
    receipt = MaterialRenderReceipt(request_id, make_target(), generation, 'frame-1')
    assert loaded.publish_preview(receipt) is True
    assert '预览已更新' in loaded.preview_status.text()
    assert '未提交' in loaded.preview_status.text()
    assert '已保存' not in loaded.save_status.text()


# ----------------------------------------------------------------------
# Recovery is explicit
# ----------------------------------------------------------------------
def test_restoring_one_material_and_the_whole_model_are_different_commands(loaded):
    catalog, state = modified_catalog()
    loaded.publish_catalog(make_target(), catalog, state)
    loaded.select_material('w-wood')
    assert loaded.actions['restore'].isEnabled()
    assert loaded.actions['restore_model'].isEnabled()
    assert loaded.restore_material()
    restore = loaded.port.calls_of('commit')[-1][1]
    assert restore.operation.value == 'restore_material'
    assert tuple(restore.working_ids) == ('w-wood',)
    assert loaded.restore_model()
    restore_model = loaded.port.calls_of('commit')[-1][1]
    assert restore_model.operation.value == 'restore_model'
    assert tuple(restore_model.working_ids) == ()


def test_a_material_without_local_changes_cannot_be_restored(loaded):
    loaded.select_material('w-metal')
    assert not loaded.actions['restore'].isEnabled()
    assert '没有本地修改' in loaded.action_reasons['restore']
    assert loaded.restore_material() is None


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------
def test_ctrl_z_edits_text_before_it_touches_project_history(qtbot, loaded):
    edit = QtWidgets.QLineEdit()
    loaded.layout().addWidget(edit)
    undo = []
    loaded.undo_requested.connect(undo.append)
    loaded.show()
    edit.show()
    edit.setFocus()
    qtbot.waitUntil(lambda: QtWidgets.QApplication.focusWidget() is edit, timeout=2000)
    qtbot.keyClicks(edit, 'abc')
    assert edit.text() == 'abc'
    qtbot.keyClick(edit, QtCore.Qt.Key.Key_Z, QtCore.Qt.KeyboardModifier.ControlModifier)
    assert 'abc' not in edit.text(), 'Ctrl+Z 必须先撤销文字'
    assert undo == [], '文字撤销不得触达项目历史'


def test_ctrl_z_outside_a_text_field_asks_for_one_project_step(qtbot, loaded):
    undo = []
    loaded.undo_requested.connect(undo.append)
    loaded.show()
    loaded.materials.setFocus()
    qtbot.keyClick(loaded.materials, QtCore.Qt.Key.Key_Z,
                   QtCore.Qt.KeyboardModifier.ControlModifier)
    assert undo == [make_target()], '面板内非文本区域的 Ctrl+Z 走项目历史'


def test_delete_never_deletes_anything_from_here(qtbot, loaded):
    before = len(loaded.port.calls)
    loaded.materials.setFocus()
    qtbot.keyClick(loaded.materials, QtCore.Qt.Key.Key_Delete)
    qtbot.keyClick(loaded, QtCore.Qt.Key.Key_Delete)
    assert len(loaded.port.calls) == before, 'Delete 不能触发删除或提交'


# ----------------------------------------------------------------------
# Orientation states do not pollute the real cover
# ----------------------------------------------------------------------
def test_highlighting_never_commits_or_touches_the_cover(loaded, port):
    loaded.select_material('w-wood')
    loaded.select_site('bind-leg-fr')
    highlights = port.calls_of('highlight')
    assert highlights
    assert port.calls_of('commit') == []
    assert '已保存' not in loaded.save_status.text()
    assert loaded.cover_status.isVisibleTo(loaded) is False


# ----------------------------------------------------------------------
# Session lifecycle
# ----------------------------------------------------------------------
def test_switching_the_model_drops_drafts_and_jobs(loaded, port):
    loaded.select_material('w-wood')
    loaded.editor.slots['roughness'].amount.setValue(0.6)
    selected = []
    loaded.material_selected.connect(selected.append)
    loaded.reset(make_target('model-b', catalog_revision=7))
    assert loaded.model_id == 'model-b'
    assert loaded.selected_working_id is None
    assert loaded.selected_binding_id is None
    assert loaded.materials.model() is None
    assert loaded.materials.material_model().rowCount() == 0
    assert loaded.editor.title.text() == '未选择材质'
    assert loaded.sites.count() == 0
    assert port.calls_of('cancel'), '旧预览与旧任务必须被取消'
    assert selected == []


def test_closing_the_project_releases_the_preview_lease(loaded, port):
    from prism.model_library.multimaterial.contracts import PreparedMaterialView
    request_id, generation = start_preview(loaded, port)
    prepared = PreparedMaterialView(request_id, make_target(), generation,
                                    'artifact', 'lease-1')
    assert loaded.publish_preview(prepared) is True
    assert '预览已准备' in loaded.preview_status.text()
    loaded.shutdown()
    assert port.calls_of('release'), '关闭时必须释放预览租约'
    assert ('release', 'lease-1') in port.calls


def test_the_legacy_area_is_told_when_local_editing_becomes_real(loaded):
    seen = []
    loaded.local_editing_changed.connect(seen.append)
    from tests.model_library.multimaterial.conftest import chair_catalog
    loaded.reset(make_target())
    catalog, state = chair_catalog()
    loaded.publish_catalog(make_target(), catalog, state)
    assert seen[-1] is True
    loaded.reset(make_target('model-b'))
    assert seen[-1] is False


def test_committed_render_failure_survives_cleared_draft_and_rejects_old_identity(loaded):
    loaded.select_material('w-wood')
    loaded.editor.slots['roughness'].amount.setValue(.38)
    request = loaded.port.calls_of('commit')[-1][1]
    after = make_target(state_revision=5)
    assert loaded.publish_commit(MaterialCommitReceipt(request.request_id, make_target(), after, 'saved-command'))
    loaded.cancel_preview(superseded=True)
    saved_text = loaded.save_status.text()
    event = dict(kind='runtime_failed', target=after, request_id=request.request_id, reason='injected render failure')
    assert loaded._on_port_event(event)
    assert '已保存' in loaded.preview_status.text()
    assert 'injected render failure' in loaded.preview_status.text()
    assert loaded.save_status.text() == saved_text
    assert not loaded._on_port_event(dict(event, request_id='old-command'))
    assert not loaded._on_port_event(dict(event, target=make_target(model_id='other', state_revision=5)))
    assert loaded.save_status.text() == saved_text


def test_render_scheduler_failure_carries_commit_identity():
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.scheduling import MaterialScheduler
    service = object.__new__(ProjectMaterialService)
    service._closed = False
    service._render_pool = MaterialScheduler()
    events = []
    service._publish = lambda **event: events.append(event)
    def fail(_):
        raise RuntimeError('injected native failure')
    try:
        service._schedule_draw(make_target(), fail, ('scene', 'model-a'), request_id='saved-id').result(5)
        assert events[0]['request_id'] == 'saved-id'
        assert events[0]['target'] == make_target()
        assert events[0]['kind'] == 'runtime_failed'
    finally:
        service._render_pool.shutdown()


def test_saved_preview_retry_never_recommits_and_tracks_latest_attempt(loaded):
    loaded.select_material('w-wood')
    attempts=[]
    loaded.port.retry_committed_preview=lambda target, rid: (attempts.append((target,rid)) or 'retry-job')
    loaded.editor.slots['roughness'].amount.setValue(.37)
    request=loaded.port.calls_of('commit')[-1][1]
    after=make_target(state_revision=5)
    loaded.publish_commit(MaterialCommitReceipt(request.request_id,make_target(),after,'saved'))
    loaded._on_port_event(dict(kind='runtime_failed',target=after,request_id=request.request_id,reason='fail'))
    assert not loaded.retry_preview_button.isHidden()
    assert loaded.retry_preview_button.isEnabled()
    saved=loaded.save_status.text()
    loaded.retry_preview_button.click()
    target, rid=attempts[-1]
    assert not loaded.retry_preview_button.isEnabled()
    assert rid != request.request_id
    assert not loaded._on_port_event(dict(kind='runtime_completed',target=after,request_id=request.request_id))
    assert loaded._on_port_event(dict(kind='runtime_failed',target=target,request_id=rid,reason='retry fail'))
    assert loaded.retry_preview_button.isEnabled()
    loaded.retry_preview_button.click()
    assert len(attempts)==2
    assert loaded._on_port_event(dict(kind='runtime_completed',target=after,request_id=attempts[-1][1]))
    assert loaded.retry_preview_button.isHidden()
    assert loaded.save_status.text()==saved
    assert len(loaded.port.calls_of('commit'))==1


def test_undo_finishes_active_gesture_before_history_command(loaded):
    from prism.model_library.multimaterial.contracts import MaterialOperation
    loaded.select_material('w-wood')
    slot=loaded.editor.slots['roughness']
    slot.slider.setSliderDown(True)
    slot.slider.setValue(230)
    loaded.request_undo()
    operations=[call[1].operation for call in loaded.port.calls_of('commit')]
    assert operations==[MaterialOperation.PATCH,MaterialOperation.UNDO]
    loaded.request_redo()
    assert loaded.port.calls_of('commit')[-1][1].operation==MaterialOperation.REDO
