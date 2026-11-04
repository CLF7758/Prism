"""最近使用账本的单元测试。

所有用例都用 ``tmp_path`` 注入账本路径，不碰真实的 `%APPDATA%`。
"""
import json
import threading

from prism import recent_usage
from prism.recent_usage import RecentUsage


def test_history_does_not_expire_after_thirty_days(tmp_path, monkeypatch):
    from types import SimpleNamespace
    clock = [2_000_000_000.0]
    monkeypatch.setattr(recent_usage, 'time', SimpleNamespace(time=lambda: clock[0]))
    usage = _ledger(tmp_path)
    usage.record('p', 'model', 'old', 'Old model')
    clock[0] += 31 * 86400
    assert usage.recent('p')[0]['resource_id'] == 'old'
    usage.record('p', 'model', 'old', 'Used again')
    assert len(usage.recent('p')) == 1
    assert usage.recent('p')[0]['ts'] == clock[0]


def test_background_write_does_not_block_selection_or_lose_latest_snapshot(tmp_path, monkeypatch):
    usage = RecentUsage(tmp_path/'background.json', background_writes=True)
    entered, release = threading.Event(), threading.Event()
    write = usage._write_disk
    def delayed(state):
        entered.set()
        assert release.wait(5)
        return write(state)
    monkeypatch.setattr(usage, '_write_disk', delayed)
    try:
        usage.record('project', 'model', 'first')
        assert entered.wait(5)
        usage.record('project', 'model', 'second')
        assert usage.forget('project', 'first')
        assert [item['resource_id'] for item in usage.recent('project')] == ['second']
    finally:
        release.set()
        usage.flush()
        usage._executor.shutdown()
    assert RecentUsage(usage.path).recent('project') == usage.recent('project')


def _ledger(tmp_path):
    return RecentUsage(tmp_path / 'recent-usage.json')


def test_slow_disk_check_never_holds_the_foreground_ledger_lock(tmp_path, monkeypatch):
    usage = RecentUsage(tmp_path/'async-check.json', background_writes=True)
    usage.record('p','canvas','first')
    usage.flush()
    entered, release = threading.Event(), threading.Event()
    main = threading.get_ident()
    observed = []
    snapshot = usage._snapshot
    def delayed():
        observed.append(threading.get_ident())
        entered.set()
        assert release.wait(5)
        return snapshot()
    monkeypatch.setattr(usage, '_snapshot', delayed)
    try:
        check = usage.refresh()
        assert entered.wait(5)
        # Both operations must finish while the filesystem check is blocked.
        usage.record('p','canvas','second')
        assert usage.recent('p')[0]['resource_id'] == 'second'
        assert main not in observed
    finally:
        release.set()
        usage.flush()
        usage._executor.shutdown()


def test_async_refresh_preserves_external_changes_and_local_edits(tmp_path):
    path = tmp_path/'shared-ledger.json'
    other = RecentUsage(path)
    other.record('p','canvas','external')
    usage = RecentUsage(path, background_writes=True)
    try:
        assert usage.refresh().result(timeout=5)
        assert usage.recent('p')[0]['resource_id'] == 'external'
        usage.record('p','model','local')
        usage.flush()
        other.record('p','mindmap','external-new')
        assert usage.refresh().result(timeout=5)
        assert {r['resource_id'] for r in usage.recent('p')} == {'external','local','external-new'}
    finally:
        usage.flush()
        usage._executor.shutdown()


def test_record_then_recent_returns_newest_first(tmp_path):
    """记过的条目都能取到，而且新的在前。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '第一个画布')
    usage.record('p1', 'canvas', 'r2', '第二个画布')
    usage.record('p1', 'mindmap', 'r3', '脑图')

    items = usage.recent('p1')
    assert [item['resource_id'] for item in items] == ['r3', 'r2', 'r1']
    assert items[0] == {'section': 'mindmap', 'resource_id': 'r3',
                        'name': '脑图', 'ts': items[0]['ts']}
    assert items[0]['ts'] > items[1]['ts'] > items[2]['ts']


def test_repeat_record_updates_timestamp_without_adding_entry(tmp_path):
    """同一个资源再记一次：更新时间戳，不新增条目。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '画布')
    first = usage.recent('p1')[0]
    usage.record('p1', 'canvas', 'r1', '画布改名')
    items = usage.recent('p1')

    assert len(items) == 1
    assert items[0]['ts'] > first['ts']
    assert items[0]['name'] == '画布改名'


def test_repeat_record_without_name_keeps_stored_name(tmp_path):
    """空名字不该把已经记下的名字抹掉。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '画布')
    usage.record('p1', 'canvas', 'r1')
    assert usage.recent('p1')[0]['name'] == '画布'


def test_one_resource_type_can_fill_all_hundred_slots(tmp_path):
    """单一类型也能占满 100 项，不单独限制为 50 项。"""
    usage = _ledger(tmp_path)
    for index in range(110):
        usage.record('p1', 'canvas', f'r{index}', f'画布 {index}')

    items = usage.recent('p1')
    assert len(items) == 100
    assert [item['name'] for item in items] == [f'画布 {i}' for i in range(109, 9, -1)]


def test_project_limit_keeps_only_one_hundred(tmp_path):
    """所有资源类型共用整个工程的 100 项限额。"""
    usage = _ledger(tmp_path)
    for section in range(12):
        for index in range(50):
            usage.record('p1', f's{section}', f'r{section}-{index}')

    items = usage.recent('p1', limit=1000)
    assert len(items) == 100
    assert {item['section'] for item in items} == {'s10', 's11'}
    assert all(item['name'] == '' for item in items)
    for section in range(10, 12):
        assert sum(1 for item in items if item['section'] == f's{section}') == 50


def test_full_list_reuse_moves_to_front_and_next_new_item_evicts_oldest(tmp_path):
    usage = _ledger(tmp_path)
    for index in range(100):
        usage.record('p', 'model', str(index))
    usage.record('p', 'model', '0')
    assert len(usage.recent('p')) == 100
    assert usage.recent('p')[0]['resource_id'] == '0'
    usage.record('p', 'material', 'new')
    rows = usage.recent('p')
    assert len(rows) == 100 and rows[0]['resource_id'] == 'new'
    assert ('model', '1') not in {(row['section'], row['resource_id']) for row in rows}
    assert RecentUsage(usage.path).recent('p') == rows


def test_corrupt_file_is_read_as_empty_ledger(tmp_path):
    """JSON 坏了：按空账本处理，不抛异常。"""
    path = tmp_path / 'recent-usage.json'
    path.write_text('{"version": 1, "projects": {', encoding='utf-8')

    usage = RecentUsage(path)
    assert usage.recent('p1') == []

    # 坏掉的账本还得能继续用：下一次记录会把文件重写好。
    usage.record('p1', 'canvas', 'r1', '画布')
    assert RecentUsage(path).recent('p1')[0]['resource_id'] == 'r1'


def test_missing_file_is_read_as_empty_ledger(tmp_path):
    """账本文件还不存在：按空账本处理，不抛异常。"""
    usage = RecentUsage(tmp_path / 'does-not-exist.json')
    assert usage.recent('p1') == []
    assert usage.forget('p1', 'r1') is False
    assert usage.clear('p1') is False


def test_sections_filter_limits_result(tmp_path):
    """给了 sections 就只返回这些分区。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '画布')
    usage.record('p1', 'mindmap', 'r2', '脑图')
    usage.record('p1', 'document', 'r3', '文档')

    assert [item['resource_id'] for item in
            usage.recent('p1', sections=['canvas', 'document'])] == ['r3', 'r1']
    assert [item['section'] for item in usage.recent('p1', sections=[])] == []
    assert len(usage.recent('p1')) == 3


def test_forget_removes_resource_across_sections(tmp_path):
    """forget 跨分区找到并删掉这个资源。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '画布')
    usage.record('p1', 'mindmap', 'r1', '脑图')
    usage.record('p1', 'canvas', 'r2', '别的画布')

    assert usage.forget('p1', 'r1') is True
    assert [item['resource_id'] for item in usage.recent('p1')] == ['r2']
    assert usage.forget('p1', 'r1') is False


def test_clear_removes_whole_project_only(tmp_path):
    """clear 只清掉这一个工程的账本。"""
    usage = _ledger(tmp_path)
    usage.record('p1', 'canvas', 'r1', '画布')
    usage.record('p2', 'canvas', 'r2', '另一个工程的画布')

    assert usage.clear('p1') is True
    assert usage.recent('p1') == []
    assert len(usage.recent('p2')) == 1


def test_atomic_write_roundtrips_through_new_instance(tmp_path):
    """写盘后换一个实例读回来，内容一致；临时文件不留下。"""
    path = tmp_path / 'recent-usage.json'
    usage = RecentUsage(path)
    usage.record('p1', 'canvas', 'r1', '画布')
    usage.record('p1', 'material', 'r2', '素材')

    assert not (tmp_path / 'recent-usage.json.tmp').exists()

    data = json.loads(path.read_text(encoding='utf-8'))
    assert data['version'] == 1
    assert data['projects']['p1']['canvas']['r1']['name'] == '画布'
    assert data['projects']['p1']['material']['r2']['name'] == '素材'

    reopened = RecentUsage(path)
    assert reopened.recent('p1') == usage.recent('p1')


def test_module_ledger_is_a_singleton(tmp_path, monkeypatch):
    """模块级单例：进程里只能有一份账本。

    单例的落盘位置来自 %APPDATA%，这里把它指到临时目录，免得测试写进
    真实的用户目录。
    """
    monkeypatch.setenv('APPDATA', str(tmp_path))
    monkeypatch.setattr(recent_usage, '_LEDGER', None)
    try:
        first = recent_usage.ledger()
        assert first is recent_usage.ledger()
        assert first.path == tmp_path / 'Prism' / 'recent-usage.json'
    finally:
        monkeypatch.setattr(recent_usage, '_LEDGER', None)
