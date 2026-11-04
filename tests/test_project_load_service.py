"""T9-5 的 ProjectLoadService：索引、延迟加载和后台任务协调。

这个服务**不重写加载** —— 实测当前的分层已经在跑（0.147 秒可操作，
1563 个素材只建了 993 个）。它补的是当时缺的三件事：给外面一个纯读的
索引入口、把阶段耗时显式报出来、让后台任务可取消。

所以这个文件测的重点是：
  * 索引读得对（表名、页数、计数）
  * 索引是**纯读**的 —— 不建对象、不依赖 Qt、不碰界面
  * 阶段记账在异常路径上也准（用上下文管理器就是为这个）
  * 取消能生效，而且取消后不再白读
"""
import os
import sqlite3
from unittest.mock import patch

import pytest

from prism.fileio.errors import PrismFileIOError
from prism.project_load_service import (ATTACHMENTS_TABLE, ITEMS_TABLE,
                                        METADATA_TABLE, PAGES_TABLE,
                                        ProjectIndex, ProjectLoadService,
                                        read_project_index)


def build_project(path, *, items=0, attachments=0, pages=(), metadata=(),
                  with_sqlar=True):
    """造一个最小可读的 .prism（就是带那几张表的 sqlite 文件）。"""
    connection = sqlite3.connect(path)
    try:
        connection.execute('CREATE TABLE prism_metadata '
                           '(key TEXT, value TEXT)')
        connection.execute('CREATE TABLE items (id INTEGER, type TEXT)')
        connection.execute('CREATE TABLE attachments (id INTEGER)')
        connection.execute('CREATE TABLE workspace_pages '
                           '(id TEXT, kind TEXT, title TEXT)')
        if with_sqlar:
            connection.execute('CREATE TABLE sqlar (name TEXT, data BLOB)')
        connection.executemany('INSERT INTO prism_metadata VALUES (?, ?)',
                               metadata)
        connection.executemany('INSERT INTO items VALUES (?, ?)',
                               [(i, 'pixmap') for i in range(items)])
        connection.executemany('INSERT INTO attachments VALUES (?)',
                               [(i,) for i in range(attachments)])
        connection.executemany(
            'INSERT INTO workspace_pages VALUES (?, ?, ?)', pages)
        connection.commit()
    finally:
        connection.close()
    return path


@pytest.fixture
def sample(tmp_path):
    return build_project(
        str(tmp_path / 'sample.prism'),
        items=12, attachments=3,
        pages=[('p1', 'canvas', '画布一'),
               ('p2', 'document', '笔记'),
               ('p3', 'mindmap', '脑图')],
        metadata=[('title', '测试工程'), ('version', '3')])


# ── 索引 ────────────────────────────────────────────────────────

def test_index_reads_the_counts(sample):
    index = read_project_index(sample)
    assert index.item_count == 12
    assert index.attachment_count == 3
    assert len(index.pages) == 3


def test_index_reads_metadata(sample):
    index = read_project_index(sample)
    assert index.metadata['title'] == '测试工程'
    assert index.metadata['version'] == '3'


def test_index_pages_carry_id_kind_title(sample):
    index = read_project_index(sample)
    first = index.pages[0]
    assert first == {'id': 'p1', 'kind': 'canvas', 'title': '画布一'}


def test_index_reports_how_long_it_took(sample):
    index = read_project_index(sample)
    assert index.seconds >= 0
    assert index.seconds < 5, '索引本来就该是毫秒级的'


def test_index_is_cheap_even_with_many_items(tmp_path):
    """索引不能随素材数变慢 —— 它是 COUNT，不是把行读出来。"""
    small = read_project_index(build_project(
        str(tmp_path / 'small.prism'), items=10))
    big = read_project_index(build_project(
        str(tmp_path / 'big.prism'), items=2000))
    assert big.item_count == 2000
    # 不断言"更快"，只断言两者都是毫秒级 —— 机器噪声会让严格比较闪。
    assert big.seconds < 0.5


def test_index_without_sqlar_still_works(tmp_path):
    """旧工程可能没有 sqlar。缺表不该让整个索引读不出来。"""
    path = build_project(str(tmp_path / 'old.prism'), items=5,
                         with_sqlar=False)
    index = read_project_index(path)
    assert index.item_count == 5


def test_index_missing_file_raises(tmp_path):
    with pytest.raises((sqlite3.DatabaseError, OSError)):
        read_project_index(str(tmp_path / 'nope.prism'))


def test_index_does_not_modify_the_file(sample):
    """只读打开 —— 索引探查不该动工程文件。"""
    before = os.path.getmtime(sample)
    size_before = os.path.getsize(sample)
    read_project_index(sample)
    assert os.path.getmtime(sample) == before
    assert os.path.getsize(sample) == size_before


# ── ProjectIndex 自己 ───────────────────────────────────────────

def test_index_title_falls_back_to_filename(tmp_path):
    path = build_project(str(tmp_path / '没有标题.prism'), items=1,
                         metadata=[('version', '3')])
    index = read_project_index(path)
    assert index.title == '没有标题'


def test_index_title_prefers_metadata(sample):
    assert read_project_index(sample).title == '测试工程'


def test_pages_of_kind_filters(sample):
    index = read_project_index(sample)
    assert [p['id'] for p in index.pages_of_kind('canvas')] == ['p1']
    assert index.pages_of_kind('没有这种') == ()


def test_large_flag_matches_the_task_book_threshold(sample):
    """任务书 §10 的样例分 100 / 1000 / 1500+ 三档。"""
    small = read_project_index(sample)
    assert small.is_large is False
    assert ProjectIndex(path='x', metadata={}, pages=(), item_count=1500,
                        attachment_count=0).is_large is True


def test_summary_mentions_the_counts(sample):
    text = read_project_index(sample).summary()
    assert '12' in text and '3' in text


# ── 服务：阶段记账 ──────────────────────────────────────────────

def test_stage_records_timings(qapp):
    service = ProjectLoadService()
    with service.stage(ProjectLoadService.STAGE_PAGE):
        pass
    timings = service.timings()
    assert len(timings) == 1
    assert timings[0][0] == ProjectLoadService.STAGE_PAGE
    assert timings[0][1] >= 0


def test_stage_emits_a_signal(qapp, qtbot):
    service = ProjectLoadService()
    with qtbot.waitSignal(service.stage_done, timeout=1000) as blocker:
        with service.stage(ProjectLoadService.STAGE_INDEX):
            pass
    assert blocker.args[0] == ProjectLoadService.STAGE_INDEX
    assert blocker.args[1] >= 0


def test_stage_still_records_when_the_body_raises(qapp):
    """失败也记时间 —— 「这一步花了 3 秒然后炸了」比「这一步炸了」有用。

    用上下文管理器而不是 start/end 一对，就是为这个：异常路径上不会漏。
    """
    service = ProjectLoadService()
    with pytest.raises(RuntimeError):
        with service.stage(ProjectLoadService.STAGE_PAGE):
            raise RuntimeError('假装这一步炸了')
    assert len(service.timings()) == 1
    assert service.timings()[0][0] == ProjectLoadService.STAGE_PAGE


def test_stages_run_in_order(qapp):
    service = ProjectLoadService()
    for name in (ProjectLoadService.STAGE_INDEX,
                 ProjectLoadService.STAGE_WINDOW,
                 ProjectLoadService.STAGE_PAGE):
        with service.stage(name):
            pass
    assert [name for name, _ in service.timings()] == [
        ProjectLoadService.STAGE_INDEX,
        ProjectLoadService.STAGE_WINDOW,
        ProjectLoadService.STAGE_PAGE]


def test_every_stage_has_a_label():
    for name in (ProjectLoadService.STAGE_INDEX,
                 ProjectLoadService.STAGE_WINDOW,
                 ProjectLoadService.STAGE_PAGE,
                 ProjectLoadService.STAGE_THUMBNAILS,
                 ProjectLoadService.STAGE_BACKGROUND):
        assert ProjectLoadService.STAGE_LABELS.get(name)


def test_timings_summary_reads_like_a_sentence(qapp):
    service = ProjectLoadService()
    assert '还没有' in service.timings_summary()
    with service.stage(ProjectLoadService.STAGE_INDEX):
        pass
    summary = service.timings_summary()
    assert '读取项目索引' in summary
    assert '合计' in summary


# ── 服务：读索引 ────────────────────────────────────────────────

def test_service_reads_and_announces_the_index(qapp, qtbot, sample):
    service = ProjectLoadService()
    with qtbot.waitSignal(service.index_ready, timeout=5000) as blocker:
        index = service.read_index(sample)
    assert blocker.args[0] is index
    assert index.item_count == 12
    assert len(service.timings()) == 1, '读索引本身也算一个阶段'


def test_service_reports_a_missing_file_instead_of_raising(qapp, qtbot,
                                                           tmp_path):
    """打开工程的第一步失败要变成一句提示，不是异常。"""
    service = ProjectLoadService()
    with qtbot.waitSignal(service.failed, timeout=5000) as blocker:
        result = service.read_index(str(tmp_path / 'nope.prism'))
    assert result is None
    assert blocker.args[0].strip()


def test_service_reports_a_corrupt_file(qapp, qtbot, tmp_path):
    broken = tmp_path / 'broken.prism'
    # bytes 字面量只能放 ASCII，中文得 encode 一下
    broken.write_bytes('这不是数据库'.encode('utf-8') * 40)
    service = ProjectLoadService()
    with qtbot.waitSignal(service.failed, timeout=5000):
        result = service.read_index(str(broken))
    assert result is None


def test_reading_a_bad_index_records_no_stage(qapp, qtbot, tmp_path):
    """失败的那次不该留下一个假的耗时记录。"""
    service = ProjectLoadService()
    with qtbot.waitSignal(service.failed, timeout=5000):
        service.read_index(str(tmp_path / 'nope.prism'))
    assert service.timings() == ()


# ── 服务：取消 ──────────────────────────────────────────────────

def test_cancel_stops_a_later_index_read(qapp, sample):
    """用户点了取消之后别再白读一遍。"""
    service = ProjectLoadService()
    service.cancel()
    assert service.cancelled is True
    assert service.read_index(sample) is None
    assert service.timings() == ()


def test_reset_clears_both_the_flag_and_the_timings(qapp, sample):
    """准备加载下一个工程时，上一轮的状态不能带过来。"""
    service = ProjectLoadService()
    service.cancel()
    with service.stage(ProjectLoadService.STAGE_INDEX):
        pass
    service.reset()
    assert service.cancelled is False
    assert service.timings() == ()
    assert service.read_index(sample) is not None


def test_check_cancelled_mirrors_the_flag(qapp):
    service = ProjectLoadService()
    assert service.check_cancelled() is False
    service.cancel()
    assert service.check_cancelled() is True


# ── 不依赖 Qt / 不碰界面 ────────────────────────────────────────

def test_read_project_index_needs_no_qt(sample):
    """这个函数是纯的 —— 工具和测试都想直接用它。

    这里刻意不请求 qapp fixture：如果实现里偷偷用了 QApplication 或者
    界面对象，这条会炸。
    """
    index = read_project_index(sample)
    assert index.item_count == 12


def test_table_names_match_a_real_project():
    """表名常量核对过真实工程，这里把它们钉住。

    拼错的话 sqlite_master 那一查会静默给空，然后得到一个「看起来正常
    但是空的」索引 —— 那种 bug 比报错难查。
    """
    assert METADATA_TABLE == 'prism_metadata'
    assert PAGES_TABLE == 'workspace_pages'
    assert ITEMS_TABLE == 'items'
    assert ATTACHMENTS_TABLE == 'attachments'


def test_index_works_against_the_real_sample_project():
    """拿用户那个 306 MB 的工程真读一次。

    这是唯一一条用真项目数据的测试 —— 上面那些都是造的。样例不在就跳过。
    """
    explicit = os.environ.get('PRISM_AUDIT_PROJECT')
    real = explicit or os.path.join(os.path.expanduser('~'), 'Desktop', '秋雨参考.prism')
    if explicit:
        assert os.path.isfile(real), 'PRISM_AUDIT_PROJECT must name an existing audit copy'
    if not os.path.exists(real):
        pytest.skip('固定样例不在')
    if os.path.exists(f'{real}-journal'):
        # 工程上次没正常关闭，还留着回滚日志。只读连接打不开这种库
        # （SQLite 要靠写来回滚）。用 Prism 打开一次就会好 ——
        # 那是用户的事，不该让测试红。
        pytest.skip('样例工程有未回滚的 -journal，先用 Prism 打开一次')
    index = read_project_index(real)
    assert index.item_count > 1000, f'实得 {index.item_count}'
    assert index.attachment_count > 0
    assert len(index.pages) >= 1
    assert index.seconds < 2, f'索引读了 {index.seconds} 秒，太慢了'


def test_a_project_left_with_a_journal_says_so(tmp_path):
    """留了回滚日志的工程要说清楚，而不是抛一句 SQLite 的原文。

    这条是这一轮真碰上的：用户的样例工程崩过一次，留下
    `秋雨参考.prism-journal`，然后读索引报
    `attempt to write a readonly database` —— 那句话完全看不出
    是"上次没正常关闭"的意思。
    """
    path = str(tmp_path / 'half-written.prism')
    connection = sqlite3.connect(path)
    connection.execute('CREATE TABLE t (x)')
    connection.commit()
    connection.close()
    with open(f'{path}-journal', 'w', encoding='utf-8') as handle:
        handle.write('假装有一份没回滚的日志')

    with pytest.raises(PrismFileIOError) as info:
        read_project_index(path)
    message = str(info.value)
    assert '正常关闭' in message or '回滚' in message, (
        f'该说清是怎么回事，实得：{message!r}')
    assert '.bak' in message, '该提一句备份还在'


# ── 计数来源：注入的统计 vs 全库扫描 ──────────────────────────────
#
# 十万图工程上 `SELECT COUNT(*) FROM items` 约 56 秒，而它只是界面里的一个
# 数字。这几条钉住"有可信统计就不许再扫全库"，以及"没统计时明确回退并留下
# 证据"，而不是把没统计静默当成 0。

def _counting_connection(monkeypatch):
    """把每个发往工程的 SELECT 记下来，用来断言"根本没有 COUNT"。"""
    from prism import project_load_service
    executed = []
    original = sqlite3.connect

    class RecordingConnection:
        def __init__(self, connection):
            self._connection = connection

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, sql, *parameters):
            executed.append(' '.join(str(sql).split()))
            return self._connection.execute(sql, *parameters)

    monkeypatch.setattr(project_load_service.sqlite3, 'connect',
                        lambda *args, **kwargs: RecordingConnection(
                            original(*args, **kwargs)))
    return executed


TRUSTED = {'items': 12, 'attachments': 3}


def test_injected_counts_skip_the_full_scan(sample, monkeypatch):
    from prism import project_load_service
    executed = _counting_connection(monkeypatch)
    with patch.object(project_load_service, 'trace_record') as record:
        index = read_project_index(sample, counts=TRUSTED)
    assert (index.item_count, index.attachment_count) == (12, 3)
    assert index.counts_source == project_load_service.COUNTS_SOURCE_PROVIDED
    assert not [sql for sql in executed if 'count(' in sql.casefold()], executed
    record.assert_not_called()


def test_provider_is_used_instead_of_a_scan(sample, monkeypatch):
    from prism import project_load_service
    executed = _counting_connection(monkeypatch)
    index = read_project_index(sample, counts_provider=lambda: TRUSTED)
    assert index.item_count == 12
    assert index.counts_source == project_load_service.COUNTS_SOURCE_PROVIDED
    assert not [sql for sql in executed if 'count(' in sql.casefold()], executed


def test_missing_counts_fall_back_and_leave_a_trace(sample):
    from prism import project_load_service
    with patch.object(project_load_service, 'trace_record') as record:
        index = read_project_index(sample)
    assert index.item_count == 12
    assert index.counts_source == project_load_service.COUNTS_SOURCE_FALLBACK
    event, = [call.args[0] for call in record.call_args_list]
    assert event == project_load_service.COUNT_FALLBACK_EVENT
    assert record.call_args.kwargs['reason'] == 'absent'
    assert record.call_args.kwargs['items'] == 12


@pytest.mark.parametrize('counts', [
    {'items': 12},                                  # 只给一半
    {'items': -1, 'attachments': 3},                 # 负数
    {'items': True, 'attachments': 3},               # bool 是 int 的陷阱
    {'items': '12', 'attachments': 3},               # 数字字符串
    {'items': 12.0, 'attachments': 3},               # 浮点
    {'item_count': 12, 'attachment_count': 3},       # 键名写错
    [12, 3],                                         # 根本不是 mapping
])
def test_untrusted_counts_fall_back_instead_of_being_used(sample, counts):
    from prism import project_load_service
    with patch.object(project_load_service, 'trace_record') as record:
        index = read_project_index(sample, counts=counts)
    assert index.item_count == 12, '不可信的统计绝不能被采用'
    assert index.attachment_count == 3
    assert index.counts_source == project_load_service.COUNTS_SOURCE_FALLBACK
    assert record.call_args.kwargs['reason'] == 'untrusted'


def test_a_failing_provider_falls_back(sample):
    from prism import project_load_service

    def broken():
        raise RuntimeError('cache went away')

    with patch.object(project_load_service, 'trace_record') as record:
        index = read_project_index(sample, counts_provider=broken)
    assert index.item_count == 12
    assert index.counts_source == project_load_service.COUNTS_SOURCE_FALLBACK
    assert record.call_args.kwargs['reason'] == 'provider_error'


def test_counts_source_defaults_to_fallback():
    index = ProjectIndex(path='x', metadata={}, pages=(), item_count=0,
                         attachment_count=0)
    assert index.counts_source == 'fallback'


def test_service_forwards_the_injected_counts(qapp, qtbot, sample):
    from prism import project_load_service
    service = ProjectLoadService()
    with qtbot.waitSignal(service.index_ready, timeout=5000) as blocker:
        index = service.read_index(sample, counts=TRUSTED)
    assert blocker.args[0] is index
    assert (index.item_count, index.counts_source) == (
        12, project_load_service.COUNTS_SOURCE_PROVIDED)


def test_service_without_counts_records_the_fallback(qapp, sample):
    from prism import project_load_service
    with patch.object(project_load_service, 'trace_record') as record:
        index = ProjectLoadService().read_index(sample)
    assert index.counts_source == project_load_service.COUNTS_SOURCE_FALLBACK
    record.assert_called_once()
