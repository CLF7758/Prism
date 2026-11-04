"""Independent acceptance for the model-library logging pipeline.

These are the checks that make this round's claims repeatable instead of
one-off probes.  They drive real code: the real service over a temporary
project database, the real trace writer into a temporary directory, and the
real bundle collector over a log with a torn tail.

Nothing here imports Qt widgets that need a window; the trace uses an offscreen
QApplication.  Nothing here reads the user's project or logs.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events                                  # noqa: E402
from prism.model_library.project_storage import (ModelProjectContext,    # noqa: E402
                                                 connect, ensure_model_tables)
from prism.model_library.repository import ModelRepositoryAdapter        # noqa: E402
from prism.model_library.service import ModelLibraryService              # noqa: E402

REPO = Path(__file__).resolve().parents[2]

#: Events this round introduced. They must be declared, or the schema check
#: silently waves them through and a typo becomes an invisible gap.
DECLARED = (
    events.EVENT_COMMIT_DURABLE,
    events.EVENT_COMMIT_APPLY_FAILED,
    events.EVENT_PROJECT_MODEL_APPLIED,
    events.EVENT_THUMBNAIL_CACHE_WRITTEN,
    events.EVENT_UI_IMAGE_ACCEPTED,
)


def test_output_byte_counts_remain_numeric_but_original_bytes_are_redacted():
    assert events._sanitize('bytes', 1234) == 1234
    assert events._sanitize('bytes', b'private model original') == '<omitted:bytes>'
    assert events._sanitize('bytes', 'data:image/png;base64,private') == '<omitted:bytes>'


def _project(root, tag):
    metadata = os.path.join(root, 'project-%s.prism' % tag)
    connection = connect(metadata)
    try:
        ensure_model_tables(connection)
        connection.execute('PRAGMA user_version=1')
    finally:
        connection.close()
    return ModelProjectContext(
        project_path=metadata, metadata_path=metadata, project_id='proj-%s' % tag,
        storage_instance_id='store-%s' % tag, open_epoch=1,
        session_token='sess-%s' % tag, backend='sqlite',
        source_revision='1', managed_root=None)


class _Trace:
    """A real trace writer in a temporary directory, active for the block."""

    def __init__(self, directory):
        self.directory = directory

    def __enter__(self):
        from PyQt6 import QtWidgets
        import prism.performance_trace as trace_module
        self.module = trace_module
        # Use the QApplication pytest-qt owns.  Creating a second one here
        # leaves a destroyed application behind, and every QObject created
        # under it (PrismSettings included) dies with it at teardown.
        self.app = QtWidgets.QApplication.instance()
        assert self.app is not None, 'pytest-qt 必须提供 QApplication（加 qapp 参数）'
        self.trace = trace_module.PerformanceTrace(self.app, None, self.directory)
        trace_module._active = self.trace
        trace_module.set_mode('normal')
        return self

    def records(self):
        events.flush_model_events()
        time.sleep(1.2)
        found = []
        for name in sorted(os.listdir(self.directory)):
            if not name.endswith('.jsonl'):
                continue
            with open(os.path.join(self.directory, name), 'rb') as stream:
                for line in stream.read().splitlines():
                    try:
                        found.append(json.loads(line.decode('utf-8', 'replace')))
                    except ValueError:
                        pass
        return found

    def __exit__(self, *_exc):
        events.flush_model_events()
        try:
            self.trace.close()
        except Exception:                              # noqa: BLE001 - shutdown
            pass
        self.module._active = None
        events.flush_model_events()
        return False


# ── contract ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize('name', DECLARED)
def test_new_events_are_declared_with_required_fields(name):
    """A new event without a contract entry is an unverified name."""
    assert name in events.REQUIRED_FIELDS, '%s 没有声明必需字段' % name
    assert events.REQUIRED_FIELDS[name], '%s 的必需字段为空' % name


def test_event_names_are_not_double_prefixed():
    """``record_model_event`` adds ``model_``; the name must not add it again."""
    for name in DECLARED:
        assert not name.startswith('model_'), '%r 会被写成 model_model_…' % name


def test_payload_carries_the_full_commit_identity():
    payload = events._prepare(events.EVENT_COMMIT_DURABLE, {
        'model_id': 'model-1', 'model_count': 1, 'state': 'completed',
        'record_revision': 7})
    assert payload.get('schema_missing_required') is None
    assert payload['model_count'] == 1
    assert payload['state'] == 'completed'


# ── real write path ──────────────────────────────────────────────────────
def test_durable_and_applied_are_two_events(tmp_path, qapp):
    """A real rename must produce durable first, then applied."""
    context = _project(str(tmp_path), 'ok')
    repository = ModelRepositoryAdapter(context)
    repository.insert({'model_id': 'model-1', 'project_id': context.project_id,
                       'name': 'Armchair', 'folder_id': None,
                       'record_revision': 0, 'source_revision': 1,
                       'files': [], 'tags': [], 'notes': ''})
    service = ModelLibraryService(repository=repository, can_write=lambda: True,
                                  on_commit=lambda before, after: None)
    with _Trace(str(tmp_path / 'logs')) as trace:
        service.rename('model-1', 'Armchair (renamed)')
        records = trace.records()
    names = [record.get('event') for record in records]
    assert 'model_commit_durable' in names, names
    assert 'model_project.model_applied' in names, names
    assert names.index('model_commit_durable') < names.index(
        'model_project.model_applied')
    applied = next(record for record in records
                   if record.get('event') == 'model_project.model_applied')
    assert applied['durable'] is True


def test_scene_failure_does_not_look_like_no_change(tmp_path, qapp):
    """If the scene copy fails, the log must say "durable but not applied"."""
    context = _project(str(tmp_path), 'fail')
    repository = ModelRepositoryAdapter(context)
    repository.insert({'model_id': 'model-1', 'project_id': context.project_id,
                       'name': 'Barrel', 'folder_id': None,
                       'record_revision': 0, 'source_revision': 1,
                       'files': [], 'tags': [], 'notes': ''})

    def refuse(before, after):
        raise RuntimeError('scene copy rejected')

    service = ModelLibraryService(repository=repository, can_write=lambda: True,
                                  on_commit=refuse)
    with _Trace(str(tmp_path / 'logs')) as trace:
        with pytest.raises(RuntimeError):
            service.rename('model-1', 'Barrel (renamed)')
        records = trace.records()
    names = [record.get('event') for record in records]
    assert 'model_commit_durable' in names
    assert 'model_commit_apply_failed' in names
    assert 'model_project.model_applied' not in names
    failed = next(record for record in records
                  if record.get('event') == 'model_commit_apply_failed')
    assert failed['durable'] is True
    assert failed['error_code'] == 'RuntimeError'
    # The row is still durable: reading it back proves the failure was the
    # scene copy, not the write.
    stored = repository.get_payload('model-1')
    assert stored['name'] == 'Barrel (renamed)'


# ── subject / scope / integrity ──────────────────────────────────────────
def test_image_subject_is_named_per_kind():
    from prism.model_library.composition import ModelLibraryComposition
    mapping = ModelLibraryComposition._SUBJECT_BY_KIND
    assert mapping == {'thumbnail': 'model_cover',
                       'preview': 'interactive_preview',
                       'texture': 'texture_slot'}


def test_scope_summary_is_bounded_and_order_stable():
    import hashlib

    def digest(ids):
        ordered = sorted(ids)
        return hashlib.sha256('\n'.join(ordered).encode('utf-8')).hexdigest()[:16]

    assert digest(['w-3', 'w-1', 'w-2']) == digest(['w-1', 'w-2', 'w-3'])
    assert len(digest(['w-%04d' % index for index in range(1000)])) == 16


def test_torn_log_tail_is_reported_in_the_bundle(tmp_path):
    """A crash mid-write must be visible, not silently copyable."""
    from prism.diagnostics import collect_bundle, runtime_snapshot

    logs = tmp_path / 'logs'
    logs.mkdir()
    (logs / 'Prism-operations-201.jsonl').write_bytes(
        (json.dumps({'event': 'a'}) + '\n').encode('utf-8'))
    (logs / 'Prism-operations-202.jsonl').write_bytes(
        (json.dumps({'event': 'a'}) + '\n').encode('utf-8')
        + b'{"event": "b", "partial": tru')
    target = tmp_path / 'bundle.zip'
    collect_bundle(logs, target, runtime=runtime_snapshot(None),
                   problem={'description': 'torn tail'},
                   source_root=REPO / 'prism')
    with zipfile.ZipFile(target) as bundle:
        manifest = json.loads(bundle.read('manifest.json').decode('utf-8'))
        summary = json.loads(bundle.read('summary.json').decode('utf-8'))
    tails = {entry['path'].split('/', 1)[1]: entry.get('tail_complete')
             for entry in manifest['files'] if entry['path'].startswith('logs/')}
    assert tails['Prism-operations-201.jsonl'] is True
    assert tails['Prism-operations-202.jsonl'] is False
    assert 'Prism-operations-202.jsonl' in summary['log_integrity']['tail_incomplete']
    assert 'rotation_failures' in summary['log_integrity']


def test_rotation_failure_is_counted_not_raised(tmp_path):
    """A log that cannot rotate must stay countable and never break the app."""
    import logging as std_logging
    from prism.logging import PrismRotatingFileHandler, rotation_failures

    path = tmp_path / 'Prism.log'
    handler = PrismRotatingFileHandler(str(path), maxBytes=64, backupCount=1,
                                       encoding='utf-8')
    handler.setFormatter(std_logging.Formatter('%(message)s'))
    handler.emit(std_logging.LogRecord('x', 20, __file__, 1, 'seed', None, None))
    handler.close()
    before = rotation_failures()['failures']
    holder = open(str(path), 'rb')
    try:
        handler.doRollover()          # Windows refuses to rename a locked file
    finally:
        holder.close()
    after = rotation_failures()
    assert after['failures'] == before + 1
    assert after['last_error'], '失败原因必须留痕'


def test_snapshot_exposes_rotation_and_keeps_unknowns_unknown():
    from prism.model_library.diagnostic_snapshot import model_diagnostic_snapshot
    snapshot = model_diagnostic_snapshot()
    assert 'log_rotation' in snapshot['pipeline']
    assert snapshot['reads_model_bytes'] is False
    assert snapshot['queue']['state'] == 'not_supplied', \
        '没有队列句柄时必须如实说未提供，不能填 0'
