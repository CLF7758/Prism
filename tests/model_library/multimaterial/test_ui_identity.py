"""Counter-examples: stale previews, interleaved commits, cancel semantics.

Every case here is a message that arrives *late*, *twice*, or *for the wrong
command*. They are the failure modes that make a UI claim a state the backend
never confirmed, so each one asserts what must NOT happen.
"""
import pytest

from prism.model_library.multimaterial.contracts import (
    MaterialCommitReceipt, MaterialRenderReceipt, PreparedMaterialView)
from tests.model_library.multimaterial.conftest import (
    chair_catalog, make_target, start_commit, start_preview)


def _receipt(request_id, *, command='cmd', before=None, after=None):
    return MaterialCommitReceipt(request_id, before or make_target(),
                                 after or make_target(state_revision=5), command)


# ----------------------------------------------------------------------
# 一、预览结果身份：乱序、重复、取消后晚到
# ----------------------------------------------------------------------
def test_a_preview_of_an_older_generation_is_refused(loaded, port):
    first, first_generation = start_preview(loaded, port, 0.21)
    second, second_generation = start_preview(loaded, port, 0.52)
    stale = MaterialRenderReceipt(first, make_target(), first_generation, 'frame-old')
    assert loaded.publish_preview(stale) is False
    assert loaded.preview_status.text().startswith('预览中'), '旧帧不能覆盖新预览状态'
    fresh = MaterialRenderReceipt(second, make_target(), second_generation, 'frame-new')
    assert loaded.publish_preview(fresh) is True
    assert '预览已更新' in loaded.preview_status.text()


def test_a_duplicate_preview_frame_is_accepted_once_and_changes_nothing(loaded, port):
    request_id, generation = start_preview(loaded, port)
    receipt = MaterialRenderReceipt(request_id, make_target(), generation, 'frame-1')
    assert loaded.publish_preview(receipt) is True
    before = loaded.preview_status.text()
    assert loaded.publish_preview(receipt) is True
    assert loaded.preview_status.text() == before


def test_a_preview_that_arrives_after_cancel_is_refused_and_its_lease_released(
        loaded, port):
    request_id, generation = start_preview(loaded, port)
    loaded.cancel_preview('测试取消')
    stale = PreparedMaterialView(request_id, make_target(), generation,
                                 'artifact-old', 'lease-old')
    assert loaded.publish_preview(stale) is False
    assert ('release', 'lease-old') in port.calls, '过期预览的租约必须按协议释放'
    assert '预览已取消' in loaded.preview_status.text()
    assert loaded.stale_publications() >= 1


def test_a_preview_failure_of_another_request_is_ignored(loaded, port):
    request_id, generation = start_preview(loaded, port)
    assert loaded.publish_preview_failed(
        make_target(), '别的请求失败', request_id='other', draft_generation=generation) is False
    assert '预览失败' not in loaded.preview_status.text()
    assert loaded.publish_preview_failed(
        make_target(), '本请求失败', request_id=request_id,
        draft_generation=generation) is True
    assert '预览失败' in loaded.preview_status.text()


def test_cancel_only_passes_the_job_id_never_a_request_id(loaded, port):
    start_preview(loaded, port)
    loaded.cancel_preview('测试')
    cancels = [call[1] for call in port.calls_of('cancel')]
    assert cancels and all(str(value).startswith('job-preview-') for value in cancels), \
        'cancel 只能传契约规定的 job_id'


# ----------------------------------------------------------------------
# 二、提交回执只确认自己
# ----------------------------------------------------------------------
def test_a_receipt_confirms_only_its_own_command_while_b_is_in_flight(loaded, port):
    first = start_commit(loaded, port, 0.31)
    loaded.select_material('w-metal')
    second = start_commit(loaded, port, 0.62)
    assert len(loaded.pending_commits()) == 2

    assert loaded.publish_commit(_receipt(first.request_id, command='cmd-A')) is True
    # A is confirmed, B must still be pending: its draft, its status and its
    # retry affordance may not be cleared by A's receipt.
    pending = loaded.pending_commits()
    assert [item.request_id for item in pending] == [second.request_id]
    assert '另有 1 条提交仍在等待回执' in loaded.save_status.text()
    assert loaded.editor.draft_failure() == '', 'A 的回执不能清掉 B 的草稿状态'


def test_a_duplicate_receipt_does_not_step_the_state_twice(loaded, port):
    request = start_commit(loaded, port)
    receipt = _receipt(request.request_id, command='cmd-A')
    assert loaded.publish_commit(receipt) is True
    text_after_first = loaded.save_status.text()
    inspects = len(port.calls_of('inspect'))
    assert loaded.publish_commit(receipt) is True
    assert loaded.save_status.text() == text_after_first
    assert len(port.calls_of('inspect')) == inspects, '重复回执不得再次触发重新读取'


def test_a_receipt_for_a_command_this_panel_never_sent_is_refused(loaded, port):
    start_commit(loaded, port)
    assert loaded.publish_commit(_receipt('unknown-request')) is False
    assert '已保存' not in loaded.save_status.text()
    assert '保存中' in loaded.save_status.text()


def test_a_late_failure_of_an_older_command_does_not_pollute_the_newer_one(loaded, port):
    first = start_commit(loaded, port, 0.31)
    loaded.select_material('w-metal')
    second = start_commit(loaded, port, 0.62)
    assert loaded.publish_commit(_receipt(first.request_id, command='cmd-A')) is True
    # The failure of an already confirmed command arrives afterwards.
    assert loaded.publish_commit_failed(make_target(), 'A 的迟到失败',
                                        request_id=first.request_id) is False
    assert 'A 的迟到失败' not in loaded.save_status.text()
    assert '已保存' in loaded.save_status.text()
    assert [item.request_id for item in loaded.pending_commits()] == [second.request_id]


def test_a_failure_keeps_the_retry_and_retry_does_not_duplicate_the_command(loaded, port):
    request = start_commit(loaded, port)
    assert loaded.publish_commit_failed(make_target(), '磁盘已满',
                                        request_id=request.request_id) is True
    assert '保存失败' in loaded.save_status.text()
    assert '未保存' in loaded.editor.draft_failure()
    before = len(port.calls_of('commit'))
    loaded.editor.retry.click()
    commits = port.calls_of('commit')
    assert len(commits) == before + 1
    assert commits[-1][1].request_id == request.request_id, 'Retry must reuse identity when the durable outcome is unknown'
    assert len(loaded.pending_commits()) == 1


def test_a_catalog_refresh_during_a_commit_keeps_the_draft(loaded, port):
    request = start_commit(loaded, port, 0.71)
    slot = loaded.editor.slots['roughness']
    catalog, state = chair_catalog()
    # The backend re-reads the model before the commit is confirmed: the values
    # it returns are the old ones, so applying them would look like a revert.
    assert loaded.publish_catalog(make_target(), catalog, state) is True
    assert slot.amount.value() == pytest.approx(0.71)
    assert [item.request_id for item in loaded.pending_commits()] == [request.request_id]
    assert '已保存' not in loaded.save_status.text()


# ----------------------------------------------------------------------
# 四、取消与关闭：五种动作五种含义
# ----------------------------------------------------------------------
def test_switching_material_finishes_the_edit_instead_of_dropping_it(loaded, port):
    loaded.select_material('w-wood')
    slot = loaded.editor.slots['roughness']
    slot.slider.setSliderDown(True)
    slot.slider.setValue(300)
    loaded.select_material('w-metal')
    commits = port.calls_of('commit')
    assert commits, '切材质要完成当前手势'
    assert tuple(commits[-1][1].working_ids) == ('w-wood',)
    assert loaded.editor.slots['roughness'].amount.value() != pytest.approx(0.3)


def test_switching_model_discards_the_draft_without_committing(loaded, port):
    loaded.select_material('w-wood')
    loaded.editor.slots['roughness'].amount.setValue(0.42)
    before = len(port.calls_of('commit'))
    loaded.reset(make_target('model-b', catalog_revision=9))
    # reset() is not "finish the edit": the new model must not receive it.
    assert len(port.calls_of('commit')) == before
    assert loaded.editor.title.text() == '未选择材质'


def test_shutdown_cancels_every_job_and_frees_the_lease(loaded, port):
    start_commit(loaded, port, 0.55)
    request_id, generation = start_preview(loaded, port, 0.35)
    prepared = PreparedMaterialView(request_id, make_target(), generation,
                                    'artifact', 'lease-x')
    assert loaded.publish_preview(prepared) is True
    loaded.shutdown()
    assert ('release', 'lease-x') in port.calls
    assert port.calls_of('cancel'), '关闭要取消在途任务'
    assert loaded.pending_commits() == ()


def test_closing_the_picker_restores_the_committed_look(loaded, port):
    loaded.select_material('w-wood')
    picker = loaded.build_picker()
    picker._card_focused('lib-1')
    job = loaded.close_picker()
    assert job == 'job-restore-1'
    assert port.calls_of('restore'), '关闭选择器必须请求恢复已提交外观'
    assert '正在恢复已提交外观' in loaded.preview_status.text()
    assert port.calls_of('commit') == []


# ----------------------------------------------------------------------
# 六、结果订阅：只订阅一次
# ----------------------------------------------------------------------
def test_attaching_the_same_port_twice_does_not_double_the_callbacks(qtbot, port):
    from prism.widgets.model_material_panel import ModelMaterialPanel
    panel = ModelMaterialPanel()
    qtbot.addWidget(panel)
    panel.set_port(port)
    panel.set_port(port)
    assert len(port.subscribers) == 1
    panel.set_port(port)
    panel.detach_port()
    assert port.unsubscribed, '解除接线必须退订'
    assert port.subscribers == []
    assert panel.subscription_id() is None


def test_a_pushed_catalog_event_goes_through_the_same_acceptance_rules(qtbot, port):
    from prism.widgets.model_material_panel import ModelMaterialPanel
    panel = ModelMaterialPanel()
    qtbot.addWidget(panel)
    panel.set_port(port)
    panel.reset(make_target())
    catalog, state = chair_catalog()
    results = port.deliver({'kind': 'catalog', 'target': make_target(),
                            'catalog': catalog, 'state': state})
    assert results == [True]
    assert len(panel.rows()) >= 4
    # A different model is dropped; a newer revision of the same model is
    # adopted forward (the service replays onto newer state).
    other = port.deliver({'kind': 'catalog', 'target': make_target('model-b'),
                          'catalog': catalog, 'state': state})
    assert other == [False]
    newer = port.deliver({'kind': 'catalog', 'target': make_target(state_revision=99),
                          'catalog': catalog, 'state': state})
    assert newer == [True]
    assert panel.target().state_revision == 99


# ----------------------------------------------------------------------
# 六、缩略图：字节 + 元数据，驻留预算
# ----------------------------------------------------------------------
def test_a_thumbnail_arrives_as_bytes_with_metadata(loaded):
    from PyQt6 import QtGui
    from prism.widgets.model_material_panel import ThumbnailImage
    image = QtGui.QImage(8, 8, QtGui.QImage.Format.Format_RGB32)
    image.fill(0x336699)
    from PyQt6 import QtCore
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    payload = ThumbnailImage(data=bytes(buffer.data()), digest='sha256:x', width=8,
                             height=8, state='ok')
    assert loaded.publish_thumbnail(make_target(), 'working:w-wood', payload) is True
    assert loaded.materials.material_model().thumbnail('working:w-wood') is not None


def test_resident_thumbnails_stay_inside_the_budget(loaded):
    from PyQt6 import QtCore, QtGui
    from prism.widgets.model_material_list import ROW_WORKING, MaterialRow
    from prism.widgets.model_material_panel import MAX_RESIDENT_THUMBNAILS
    image = QtGui.QImage(4, 4, QtGui.QImage.Format.Format_RGB32)
    image.fill(0x102030)
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, 'PNG')
    data = bytes(buffer.data())
    rows = tuple(MaterialRow(key=f'working:extra-{index}', kind=ROW_WORKING,
                             name=f'材质 {index}', site_count=1,
                             working_id=f'extra-{index}',
                             thumbnail_key=f'working:extra-{index}')
                 for index in range(MAX_RESIDENT_THUMBNAILS + 5))
    loaded.materials.install(rows)
    model = loaded.materials.material_model()
    for row in rows:
        assert loaded.publish_thumbnail(make_target(), row.thumbnail_key, data) is True
    assert len(loaded.resident_thumbnails()) <= MAX_RESIDENT_THUMBNAILS
    assert model.resident_count() <= MAX_RESIDENT_THUMBNAILS, '驻留预算不等于读取预算'


# ----------------------------------------------------------------------
# 服务端重放：旧 state_revision 不再被拒
# ----------------------------------------------------------------------
def test_a_stale_state_revision_is_replayed_not_rejected():
    """The root fix: a request built while an earlier commit was queued must
    replay onto the current state instead of failing with 材质版本已变化."""
    from types import SimpleNamespace
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import (
        MaterialEditRequest, MaterialOperation)

    service = object.__new__(ProjectMaterialService)
    current = make_target(state_revision=10)
    item = {'state': SimpleNamespace(target=current)}

    stale = MaterialEditRequest('r1', 'g1', make_target(state_revision=9),
                                MaterialOperation.PATCH, working_ids=('w-1',))
    service._check_revision(stale, item)  # must not raise

    # A different source revision (the original changed) is still refused.
    other_source = MaterialEditRequest('r2', 'g2', make_target(source_revision=99),
                                       MaterialOperation.PATCH, working_ids=('w-1',))
    with pytest.raises(ValueError):
        service._check_revision(other_source, item)
