"""A-F verification: un-flattened events, export stages, log footprint, stacks."""
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events as model_events               # noqa: E402


def test_lifecycle_states_are_emitted_under_their_own_names():
    """B: the eleven declared names must not stay a `state` field forever."""
    source = open(os.path.join(REPO, 'prism', 'model_library', 'composition.py'),
                  encoding='utf-8').read()
    assert '_LIFECYCLE_STATES' in source
    for state in ('queued', 'started', 'source_prepared', 'completed', 'failed'):
        assert "'%s'" % state in source
    # The aggregated line stays, so existing queries keep working.
    assert "record_model_event('service_job', **payload)" in source
    assert 'record_model_event(state, **payload)' in source


def test_export_stages_are_emitted():
    source = open(os.path.join(REPO, 'prism', 'model_library', 'exporter.py'),
                  encoding='utf-8').read()
    for name in ('EVENT_EXPORT_WRITTEN', 'EVENT_EXPORT_VALIDATED',
                 'EVENT_EXPORT_PUBLISHED'):
        assert 'record_model_event(%s' % name in source, name


def test_dead_events_now_have_call_sites():
    """The seven events the audit found had no call site at all."""
    wanted = {
        'appearance.snapshot': 'model_library/service.py',
        'export.written': 'model_library/exporter.py',
        'export.validated': 'model_library/exporter.py',
        'export.published': 'model_library/exporter.py',
    }
    for event, relative in wanted.items():
        text = open(os.path.join(REPO, 'prism', relative), encoding='utf-8').read()
        assert event.split('.')[-1].upper() in text.upper(), event


def test_rollback_is_recorded():
    source = open(os.path.join(REPO, 'prism', 'model_library', 'repository.py'),
                  encoding='utf-8').read()
    assert "'model_tx.rollback'" in source
    assert "'model_tx.committed'" in source


def test_attachment_miss_is_recorded(monkeypatch, tmp_path):
    import prism.fileio.attachments_store as store_module
    seen = []
    monkeypatch.setattr('prism.performance_trace.record',
                        lambda name, **fields: seen.append((name, fields)))
    attachments = store_module.StoredAttachments({}, entries={})
    assert attachments.get(404) is None
    misses = [fields for name, fields in seen if name == 'material_attachment_miss']
    assert misses, [name for name, _ in seen]
    assert misses[0]['attachment'] == 404


def test_bundle_carries_footprint_and_stacks(tmp_path):
    from prism.diagnostics import collect_bundle, runtime_snapshot

    logs = tmp_path / 'logs'
    logs.mkdir()
    payload = (json.dumps({'event': 'a'}) + '\n'
               + 'Traceback (most recent call last):\n'
                 '  File "C:\\\\Users\\\\someone\\\\x.py", line 3, in run\n'
                 'ValueError: boom\n')
    (logs / 'Prism-operations-301.jsonl').write_bytes(payload.encode('utf-8'))
    target = tmp_path / 'bundle.zip'
    collect_bundle(logs, target, runtime=runtime_snapshot(None),
                   problem={'description': 'A-F probe'},
                   source_root=Path(REPO) / 'prism')
    with zipfile.ZipFile(target) as bundle:
        summary = json.loads(bundle.read('summary.json').decode('utf-8'))
    footprint = summary['log_footprint']
    assert footprint['files'] == 1
    assert footprint['total_bytes'] > 0
    assert footprint['limits']['retained_files'] == 3
    stacks = summary['exceptions']
    assert stacks and stacks[0] != 'none_found_in_retained_logs'
    assert 'ValueError: boom' in stacks[0]['sanitized']
    # The user's own directory must not survive into the bundle.
    assert 'someone' not in stacks[0]['sanitized']


def test_lod_and_present_events_are_in_the_worker():
    source = open(os.path.join(REPO, 'prism', 'components', 'material_preview.py'),
                  encoding='utf-8').read()
    assert "event('material.lod_selected'" in source
    assert "event('material.preview_presented'" in source
    # Frontmost is not provable, so it must be recorded as unknown, not true.
    assert "frontmost='unknown'" in source


@pytest.mark.parametrize('name', [
    'material.preview_presented', 'material.lod_selected',
    'model_tx.committed', 'model_tx.rollback', 'appearance.snapshot',
])
def test_contract_declares_the_new_events(name):
    assert name in model_events.REQUIRED_FIELDS, name
