"""D2 sequencing counter-examples: the races that decide whether an edit is real.

These are the cases a happy-path walkthrough never reaches, and each one has a
specific way of going wrong that the user can see:

* a stale preview frame replacing a newer one;
* a cancelled edit that installs anyway;
* a receipt for edit A clearing the dirty flag of edit B;
* picking a material in the viewport that leaves the list pointing elsewhere;
* applying a library material that changed (or vanished) after the preview.

None of them can be judged while the service has no scheduler, so each test
writes its own specification to ``artifacts/model-multimaterial/d2`` and then
skips with a reason.  When the port is wired, the skip turns into a real check
without anyone having to invent the expected behaviour then -- and inventing it
then is exactly how "it works" gets defined by whatever the code happens to do.
"""
from __future__ import annotations

import json

import pytest

from .conftest import D2_DIR

#: One entry per counter-example: what to do, what must hold, and how to falsify
#: a "passed" claim.
SEQUENCING = [
    {
        'id': 'preview_out_of_order',
        'situation': '同一目标连续两次预览：generation 5 先发出，generation 6 后发出。',
        'counterexample': ('让 generation 6 的工件先安装完成，再让 generation 5 的安装'
                           '回执晚到。'),
        'must_hold': [
            '画面保留 generation 6 的结果，generation 5 的帧不得安装',
            'generation 5 的回执被标记为 stale 并记录 draft_generation',
            '正式状态与封面不受任何一代预览影响',
        ],
        'falsify': ('若 generation 5 的帧出现在画面上，或日志里没有 stale 记录，'
                    '该项判失败；"最终看起来对"不算通过。'),
        'observable': 'MaterialRenderReceipt.draft_generation、model_events 的 material.install.stale',
        'blocked_by': 'capabilities().edit_material=False；没有 prepare/install 调度',
    },
    {
        'id': 'cancel_then_late_result',
        'situation': '预览到一半用户取消。',
        'counterexample': '取消 generation 7 之后，它的安装完成回执才到达。',
        'must_hold': [
            '取消后的结果不得安装、不得改变提交状态',
            '该次预览的租约被释放，暂存资源回收',
            '用户看到的仍是取消前的最后一帧正确画面',
        ],
        'falsify': '若取消后画面变了、或工程被写入，判失败。',
        'observable': 'leases 释放记录、material.install.stale、暂存目录清空',
        'blocked_by': '没有 lease/release 实现（PreviewPort）',
    },
    {
        'id': 'commit_a_while_editing_b',
        'situation': '材质 A 提交进行中，用户继续编辑材质 B。',
        'counterexample': 'A 的提交回执在 B 已经产生新草稿之后到达。',
        'must_hold': [
            'A 的回执只能确认 A 范围内的变化',
            'B 的未提交草稿与新脏标记不得被 A 的回执清掉',
            'B 的草稿在新 state_revision 下仍然指向 B 的目标',
        ],
        'falsify': '若 B 的草稿消失或脏标记被清，判失败（旧回执清掉新脏）。',
        'observable': 'MaterialCommitReceipt.before/after 的 state_revision、草稿代次',
        'blocked_by': 'MultiMaterialService.commit 抛 ModelFeatureUnavailable',
    },
    {
        'id': 'selection_follows_other_part',
        'situation': '先在列表选中材质 X，再在视口点中另一个部位（用的是材质 Y）。',
        'counterexample': '视口拾取与列表各自维护"当前材质"。',
        'must_hold': [
            '列表高亮、视口拾取、属性面板读同一个已提交状态',
            '选中只影响高亮，不改材质、不进封面',
            '点中的是哪个使用点必须可读（同材质多部位时尤其如此）',
        ],
        'falsify': '若列表与视口显示不同材质、或高亮改变了材质，判失败。',
        'observable': 'pick 结果 → 使用点标识 → 列表选中行三者一致',
        'blocked_by': 'capabilities().pick_material=False',
    },
    {
        'id': 'snapshot_survives_source_change',
        'situation': '从材质库预览某材质后，该材质在库里被修改或删除，然后用户点应用。',
        'counterexample': '应用时重新去库里读"最新"定义。',
        'must_hold': [
            '应用提交的是预览时确定的快照内容',
            '快照带来源 ID 与版本，可在工程里追溯',
            '源库删除后重开工程，模型仍显示快照',
        ],
        'falsify': '若应用后的材质与预览时不同、或源库删除后模型丢材质，判失败。',
        'observable': 'WorkingMaterial.base_snapshot_ref + provenance_json；源库删除后重开',
        'blocked_by': '没有快照存取实现；没有库替换链路',
    },
]


def _capabilities():
    from prism.model_library.multimaterial.service import MultiMaterialService
    return MultiMaterialService().capabilities()


def _write_status(write_report):
    report = _capabilities()
    flags = {name: value for name, value in vars(report).items()
             if isinstance(value, bool)}
    closed = not any(flags.values())
    payload = {
        'state': 'blocked' if closed else 'open',
        'capabilities': flags,
        'reason': report.reason,
        'items': [dict(item, status='blocked' if closed else 'runnable',
                       reason=report.reason if closed else '')
                  for item in SEQUENCING],
    }
    write_report(D2_DIR, 'sequencing-status.json', payload)
    return payload


@pytest.mark.parametrize('case', SEQUENCING, ids=[item['id'] for item in SEQUENCING])
def test_sequencing_counterexample(case, write_report):
    payload = _write_status(write_report)
    if payload['state'] == 'blocked':
        pytest.skip('%s：%s（服务能力全关：%s）'
                    % (case['id'], case['blocked_by'], payload['reason']))
    pytest.fail('服务已接通但没有对应的验收实现：%s 需要按规格补测，'
                '不得因为能力位打开就记为通过' % case['id'])


def test_sequencing_spec_is_written(write_report):
    """The specification exists on disk even while nothing can be run."""
    payload = _write_status(write_report)
    assert len(payload['items']) == len(SEQUENCING)
    for item in payload['items']:
        for key in ('id', 'situation', 'counterexample', 'must_hold',
                    'falsify', 'observable', 'blocked_by'):
            assert item.get(key), '%s 缺少 %s' % (item['id'], key)
    on_disk = json.loads((D2_DIR / 'sequencing-status.json')
                         .read_text(encoding='utf-8'))
    assert on_disk['items'] == payload['items']


def test_sequencing_contract_fields_exist():
    """The fields these counter-examples depend on must exist in the contract.

    A race cannot be expressed at all if a prepared view has no generation and
    a receipt has no generation to compare against.
    """
    from prism.model_library.multimaterial import contracts
    prepared = contracts.PreparedMaterialView(
        request_id='r', target=contracts.MaterialTarget('s', 1, 'm', 1, 1, 1),
        draft_generation=6, artifact_ref='a', lease_id='l')
    receipt = contracts.MaterialRenderReceipt(
        request_id='r', target=prepared.target, draft_generation=6, frame_id='f')
    assert prepared.draft_generation == receipt.draft_generation == 6
    request = contracts.MaterialEditRequest(
        request_id='r', gesture_id='g', target=prepared.target,
        operation=contracts.MaterialOperation.PATCH)
    assert request.gesture_id == 'g'
    commit = contracts.MaterialCommitReceipt(
        request_id='r', before=prepared.target, after=prepared.target,
        command_id='c')
    assert commit.before.state_revision == commit.after.state_revision
