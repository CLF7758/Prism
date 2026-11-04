"""呈现证据解析器：发布→绘制→取消、提前取消、发布未绘制、终态缺失。

只用合成记录（隔离临时文件），不碰真实日志、不启动用户工程。
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
ROOT = Path(__file__).resolve().parents[1]
ANALYZER = ROOT / 'tools/analyze_prism_trace.py'
if not ANALYZER.exists():
    ROOT = Path(r'C:\Users\admin\Desktop\Prism-main\Prism-main')
    ANALYZER = ROOT / 'tools/analyze_prism_trace.py'


def scenario(session='transport', query='q'):
    facts = []

    def emit(event, **fields):
        fact = dict(event=event, session=session, session_id='frozen-protocol',
                    trace_session_id=session, sequence=len(facts) + 1,
                    monotonic=len(facts) / 10, schema_version=2,
                    presentation_schema_version=1, query_id=query,
                    scope={'filter': 'all'}, source_revision='current',
                    read_id='read-1', source_query_id=query,
                    read_source_revision='current',
                    complete=True, final=True, cancelled=False, model_rows=6)
        fact.update(fields)
        facts.append(fact)
        return fact

    return facts, emit


def upstream(emit):
    """读取→交付→完整发布模型。"""
    emit('catalog_query_started', final=False)
    emit('catalog_rows_read_evidence', rows_read=10, logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('catalog_rows_delivered', stage='consumer_consumed', logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    emit('asset_model_published', model_unique_ids=6, model_unique_ids_complete=True)


def paint(emit, frame='f1', view='v1', **fields):
    values = dict(frame_id=frame, view_id=view, viewport_drawn=12,
                  current_revision_coverage=9, content_ready=4,
                  count_units='content_draw_operations', coverage_scope='paint_clip',
                  counts_are_lower_bounds=True, incomplete=False, unknown=0)
    values.update(fields)
    return emit('asset_paint_frame', **values)


def visibility(emit, frame='f1', view='v1', **fields):
    values = dict(frame_id=frame, view_id=view, viewport_drawn=10,
                  current_revision_coverage=8, content_ready=5,
                  count_units='unique_model_rows', coverage_scope='current_viewport',
                  counts_are_lower_bounds=False, current_viewport_exact=True,
                  incomplete=False, unknown=0)
    values.update(fields)
    return emit('asset_visibility_snapshot', **values)


def cancel(emit, reason='cancel_or_switch'):
    return emit('catalog_query_cancelled', cancelled=True, final=True,
                complete=False, reason=reason)


def run(tmp_path, facts):
    source = tmp_path / 'input.jsonl'
    payload = b''.join((json.dumps(fact) + '\n').encode() for fact in facts)
    source.write_bytes(payload)
    result = subprocess.run(
        [sys.executable, '-B', str(ANALYZER), str(source),
         '--out-dir', str(tmp_path), '--label', 'zz-presentation'],
        env=dict(os.environ, QT_QPA_PLATFORM='offscreen', PYTHONDONTWRITEBYTECODE='1'),
        capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr.decode(errors='replace')
    assert source.read_bytes() == payload
    report = json.loads((tmp_path / 'zz-presentation.json').read_text(encoding='utf-8'))
    markdown = (tmp_path / 'zz-presentation.md').read_text(encoding='utf-8')
    return report, markdown


def row_of(report):
    rows = report['reports'][0]['presentation_evidence']['queries']
    assert len(rows) == 1
    return rows[0]


def test_published_painted_then_cancelled_keeps_presentation_evidence(tmp_path):
    """发布→绘制→之后被取消：用户确实看到了结果，呈现证据仍然有效。"""
    facts, emit = scenario()
    emit('session_start')
    upstream(emit)
    paint(emit)
    visibility(emit)
    cancel(emit)
    emit('session_end')
    report, markdown = run(tmp_path, facts)
    row = row_of(report)
    assert row['evidence_valid'] is True
    assert row['all_layers_observed'] is True
    assert row['unique_count_complete'] is True
    assert row['superseded'] is True
    assert row['superseded_reason'] == 'cancel_or_switch'
    assert row['presented_before_supersede'] is True
    assert row['cancelled'] is True
    assert row['cancel_reason'] == 'cancel_or_switch'
    assert row['terminal_state'] == 'cancelled'
    assert row['terminal_event'] == 'catalog_query_cancelled'
    assert row['terminal_reason'] == 'cancel_or_switch'
    assert row['model_publication_complete'] is True
    assert row['verdict'] == 'presented_then_superseded'
    # 取帧口径：内容帧就是画出了内容的那一帧。
    assert row['latest_content_frame']['paint']['seq'] == paint_seq(facts)
    assert row['paint_clip']['viewport_drawn'] == 12
    assert 'superseded' in markdown or '被取代' in markdown


def test_latest_started_frame_is_kept_next_to_latest_content_frame(tmp_path):
    """最后一帧什么都没画时，内容帧必须回到真正画过内容的那一帧。"""
    facts, emit = scenario()
    emit('session_start')
    upstream(emit)
    paint(emit, 'f1')
    visibility(emit, 'f1')
    empty_seq = paint(emit, 'f2', viewport_drawn=0, current_revision_coverage=0,
                      content_ready=0)['sequence']
    visibility(emit, 'f2', viewport_drawn=0, current_revision_coverage=0,
               content_ready=0)
    cancel(emit)
    emit('session_end')
    report, _ = run(tmp_path, facts)
    row = row_of(report)
    selection = row['frame_selection']
    # 两个口径都保留，差异可见。
    assert selection['latest_started_frame']['paint_sequence'] == empty_seq
    assert selection['latest_started_frame']['paint_viewport_drawn'] == 0
    assert selection['latest_content_frame']['paint_sequence'] != empty_seq
    assert selection['latest_content_frame']['paint_viewport_drawn'] == 12
    assert selection['content_frame_found'] is True
    assert row['latest_content_frame']['paint']['seq'] == selection['latest_content_frame']['paint_sequence']
    # 顶层计数按内容帧走，不再被空帧抹成 0。
    assert row['paint_clip']['viewport_drawn'] == 12
    assert row['evidence_valid'] is True


def test_cancelled_before_publish_is_invalid_and_reason_stays_visible(tmp_path):
    """未发布就被取消：呈现证据无效，原始 reason 仍然可见。"""
    facts, emit = scenario()
    emit('session_start')
    emit('catalog_query_started', final=False)
    emit('catalog_rows_read_evidence', rows_read=10, logical_rows=10,
         unique_rows=10, unique_rows_complete=True)
    cancel(emit)
    emit('session_end')
    report, markdown = run(tmp_path, facts)
    row = row_of(report)
    assert row['evidence_valid'] is False
    assert row['all_layers_observed'] is False
    assert row['unique_count_complete'] is False
    assert row['superseded'] is False
    assert row['presented_before_supersede'] is False
    assert row['cancelled'] is True
    assert row['cancel_reason'] == 'cancel_or_switch'
    assert row['terminal_state'] == 'cancelled'
    assert row['terminal_reason'] == 'cancel_or_switch'
    assert row['model_publication_complete'] is False
    assert row['verdict'] == 'cancelled'
    assert 'paint' in row['missing_or_invalid']
    assert 'model' in row['missing_or_invalid']
    assert 'cancel_or_switch' in markdown


def test_published_but_never_painted_is_invalid(tmp_path):
    """已发布但从未绘制：缺 paint 层，呈现证据无效。"""
    facts, emit = scenario()
    emit('session_start')
    upstream(emit)
    cancel(emit)
    emit('session_end')
    report, _ = run(tmp_path, facts)
    row = row_of(report)
    assert row['evidence_valid'] is False
    assert row['all_layers_observed'] is False
    assert row['unique_count_complete'] is False
    assert row['layers_observed']['paint'] is False
    assert row['layers_observed']['visibility'] is False
    assert 'paint' in row['missing_or_invalid']
    assert 'visibility' in row['missing_or_invalid']
    assert row['model_publication_complete'] is True
    assert row['superseded'] is True
    assert row['presented_before_supersede'] is False
    assert row['frame_selection']['content_frame_found'] is False


def test_missing_terminal_state_is_reported_explicitly(tmp_path):
    """只有 started 没有终态：显式记为终态缺失，并给出日志自证。"""
    facts, emit = scenario()
    emit('session_start')
    emit('catalog_query_started', final=False)
    emit('asset_model_install_progress', installed=3, total=10)
    # 会话在查询仍在飞行时结束：最后一条记录紧邻查询的最后一条记录。
    emit('slow_dispatch')
    report, markdown = run(tmp_path, facts)
    row = row_of(report)
    assert row['terminal_state'] == 'missing'
    assert row['terminal_event'] is None
    assert row['terminal_reason'] is None
    assert row['terminal_seq'] is None
    assert 'terminal_state_missing' in row['missing_or_invalid']
    assert row['evidence_valid'] is False
    assert row['unique_count_complete'] is False
    evidence = row['terminal_state_evidence']
    assert evidence is not None
    assert evidence['query_last_sequence'] == 3
    assert evidence['query_last_event'] == 'asset_model_install_progress'
    assert evidence['session_last_sequence'] == 4
    assert evidence['session_last_event'] == 'slow_dispatch'
    assert evidence['query_last_is_session_tail'] is True
    assert '终态=missing' in markdown


def paint_seq(facts):
    for fact in facts:
        if fact['event'] == 'asset_paint_frame':
            return fact['sequence']
    raise AssertionError('no paint frame')
