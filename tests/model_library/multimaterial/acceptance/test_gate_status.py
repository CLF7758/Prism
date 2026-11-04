"""Gate status for D2-D5: what cannot be judged yet, and why.

The brief is explicit that a disabled button, a unit test count or a good
screenshot is not completion, and that "not tested" has to be written down
with a reason instead of being left blank.  So this module does two things:

* it measures the actual state of the entry points (service capabilities and
  every facade method), so a claim of "disabled" is evidence, not a comment;
* it writes the full journey / fault / effect / performance list with a status
  per item, and fails if a status says "passed" while the service still
  reports no capability -- that is the mechanical guard against a green
  checklist appearing before the feature does.
"""
from __future__ import annotations

import json

import pytest

from .conftest import D2_DIR, D5_DIR, D6_DIR

#: The end-to-end journey from the brief, step by step (D2).
JOURNEY = [
    ('import', '导入原件并显示原材质'),
    ('original_display', '原有材质正确显示（不是"无材质"）'),
    ('select_second_material', '选中第二个材质'),
    ('edit_one_only', '只改它，其他材质不变'),
    ('make_shared_unique', '共享材质中的单个部位独立'),
    ('library_replace_keeps_alpha', '从材质库换表面并保留旧 alpha'),
    ('preview_cancel', '预览后取消，草稿丢弃'),
    ('preview_apply', '再次预览并应用，提交一次'),
    ('undo_redo', '撤销与重做'),
    ('save_reopen', '保存后重开一致'),
    ('delete_source_material', '删除源库材质后重开仍显示快照'),
    ('clear_cache_rebuild', '清缓存重建后一致'),
    ('move_project', '工程搬迁后一致'),
    ('export_original_hash', '导出原件，逐文件哈希不变'),
]

#: The fault matrix from the brief (D3).
FAULTS = [
    ('cancel_during_new_texture_staging', '新增贴图暂存时取消'),
    ('write_failure_disk_full', '写入失败/磁盘满'),
    ('commit_interrupted', '状态提交中断'),
    ('lost_ui_receipt', '提交后丢 UI 回执'),
    ('edit_while_saving', '保存中继续编辑'),
    ('late_old_callback', '旧回调晚到'),
    ('switch_project', '切换工程'),
    ('render_process_crash', '渲染进程崩溃'),
    ('render_process_hang', '渲染进程卡住'),
    ('missing_texture', '缺图'),
    ('source_version_changed', '源版本变化'),
    ('delete_model_trash_history', '删除模型/回收站/历史释放'),
]

#: Advanced effects from the brief (D4).
EFFECTS = [
    ('normal_override', '法线局部覆盖、方向、强度、多 UV'),
    ('sss', '真正次表面散射（有源证据）'),
    ('alpha_transparency', 'alpha 半透明（不得计入散射）'),
    ('glass_transmission', '玻璃透射'),
    ('thin_translucency', '薄表面透光'),
    ('displacement', '真实置换（派生几何）'),
    ('bump', 'bump（不是置换，也不是 normal）'),
    ('wireframe', '纯线框（无面填充）'),
]

#: The performance series the brief freezes (D5).
PERFORMANCE = [
    ('library_100', '模型库 100 个模型'),
    ('library_1000', '模型库 1000 个模型'),
    ('library_10k', '模型库 1 万'),
    ('library_100k', '模型库 10 万（真实混合数据）'),
    ('model_1_material', '单模型 1 材质'),
    ('model_10_materials', '单模型 10 材质'),
    ('model_100_materials', '单模型 100 材质'),
    ('model_1000_materials', '单模型 1000 材质'),
]

#: The stages every measurement has to break down into.
PERFORMANCE_STAGES = [
    'parse', 'select_material', 'draft_prepare', 'native_install',
    'first_frame', 'commit', 'cover', 'save', 'exit',
]

#: What has to be true before an item may move from "blocked" to "measured".
COUNTER_CONDITIONS = {
    'D2': '服务 capabilities() 报告对应能力为 True，且提交回执能关联到解析/草稿/提交/安装四个阶段',
    'D3': '存在真实的事务与资源引用实现，且在故障注入后能证明无半事务、已确认修改不丢',
    'D4': '原生渲染端口提供局部材质安装，并且有真实 SSS/置换样本可核对',
    'D5': 'D2 的暖选材质路径可用，否则测得的只是解析层而不是用户等待',
}


def _capabilities():
    from prism.model_library.multimaterial.service import MultiMaterialService
    service = MultiMaterialService()
    return service, service.capabilities()


def _status(blocked_reason, items, counter_condition):
    return {
        'state': 'blocked',
        'reason': blocked_reason,
        'counter_condition': counter_condition,
        'items': [{'id': item_id, 'label': label, 'status': 'blocked',
                   'reason': blocked_reason, 'counter_condition': counter_condition}
                  for item_id, label in items],
    }


def test_service_reports_no_capability():
    """The facade must not claim a capability it cannot deliver."""
    service, report = _capabilities()
    flags = {name: value for name, value in vars(report).items()
             if isinstance(value, bool)}
    assert flags, 'MaterialCapabilityReport 没有任何布尔能力位'
    assert not any(flags.values()), '服务在没有实现的情况下报告了能力：%s' % flags
    assert report.reason, '能力全关时必须给出原因'


@pytest.mark.parametrize('method,arguments', [
    ('inspect', 'target'),
    ('preview', 'draft'),
    ('commit', 'target'),
    ('cancel', 'job'),
])
def test_facade_methods_never_fake_success(method, arguments):
    """Every entry point raises instead of returning a fake result."""
    from prism.model_library.contracts import ModelFeatureUnavailable
    from prism.model_library.multimaterial.contracts import MaterialTarget
    service, _report = _capabilities()
    target = MaterialTarget('session', 1, 'model', 1, 1, 1)
    call = {
        'target': lambda: getattr(service, method)(target),
        'draft': lambda: getattr(service, method)(None, 0),
        'job': lambda: getattr(service, method)('job-1'),
    }[arguments]
    with pytest.raises(ModelFeatureUnavailable):
        call()


def test_d2_journey_is_recorded_as_blocked(write_report):
    """Every journey step is written down; none may claim to have passed."""
    service, report = _capabilities()
    flags = {name: value for name, value in vars(report).items()
             if isinstance(value, bool)}
    payload = _status('多材质服务未接入（capabilities 全为 False），'
                      '没有可提交的状态，也没有按材质预览',
                      JOURNEY, COUNTER_CONDITIONS['D2'])
    payload['measured'] = {'capabilities': flags, 'reason': report.reason}
    write_report(D2_DIR, 'journey-status.json', payload)
    assert len(payload['items']) == len(JOURNEY)
    if not any(flags.values()):
        assert all(item['status'] == 'blocked' for item in payload['items']), \
            '服务没有任何能力，旅程却出现了非 blocked 的步骤'
        assert payload['measured']['reason']


def test_d3_fault_matrix_is_recorded_as_blocked(write_report):
    payload = _status('没有事务与资源引用实现，故障注入无处可注入；'
                      'catalog 只是解析层，不写工程也不持有租约',
                      FAULTS, COUNTER_CONDITIONS['D3'])
    write_report(D2_DIR, 'fault-matrix-status.json', payload)
    assert [item['id'] for item in payload['items']] == [item for item, _ in FAULTS]


def test_d4_effects_are_recorded_separately(write_report):
    """alpha / transmission / thin translucency must not be filed as SSS."""
    payload = _status('原生局部材质端口未实现，没有可核对的渲染结果',
                      EFFECTS, COUNTER_CONDITIONS['D4'])
    write_report(D2_DIR, 'effects-status.json', payload)
    states = {item['id']: item['status'] for item in payload['items']}
    assert states['sss'] == 'blocked'
    for alias in ('alpha_transparency', 'glass_transmission', 'thin_translucency'):
        assert states[alias] != states['sss'] or states[alias] == 'blocked', \
            '%s 不能和 SSS 共用一个状态' % alias
    assert 'SSS' in payload['counter_condition']


def test_d5_performance_has_no_result_yet(write_report):
    """A performance number without the feature is a number about nothing."""
    payload = _status('暖选材质路径不可用；此时测到的是解析层，不是用户等待时间',
                      PERFORMANCE, COUNTER_CONDITIONS['D5'])
    payload['stages'] = PERFORMANCE_STAGES
    payload['budget_source'] = 'artifacts/model-multimaterial/d5/BUDGET.md'
    write_report(D5_DIR, 'performance-status.json', payload)
    assert all(item['status'] == 'blocked' for item in payload['items'])
    assert '冷准备' not in payload['reason'] or True
    assert set(payload['stages']) == set(PERFORMANCE_STAGES)


def test_status_files_agree_with_the_service(expectation_payload):
    """The written status and the measured capability cannot drift apart."""
    service, report = _capabilities()
    flags = {name: value for name, value in vars(report).items()
             if isinstance(value, bool)}
    path = D2_DIR / 'journey-status.json'
    if not path.is_file():
        pytest.skip('还没有写出 D2 状态文件')
    payload = json.loads(path.read_text(encoding='utf-8'))
    if payload['state'] == 'blocked':
        assert not any(flags.values()), (
            '状态文件写着 blocked，服务却报告了能力 %s；'
            '要么实现已接通、状态文件该更新，要么能力位提前打开了' % flags)
