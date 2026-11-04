"""L6 verification: the four cover-request decisions, and import stages.

Uses the real service object with a stub repository, so the decision branch is
the production one; only the data source is a stub.  Every branch must leave a
line in the log, or "封面一直待生成" stays unanswerable.
"""
import dataclasses
import json
import os
import sys

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from prism.model_library import events                          # noqa: E402
from prism.model_library.service import ModelLibraryService      # noqa: E402


class _Summary:
    def __init__(self, model_id, state='pending', data_url=''):
        self.model_id = model_id
        self.thumbnail_state = state
        self.thumbnail_data_url = data_url


class _Repository:
    def __init__(self):
        self.cover_cache_root = None

    def get_payload(self, model_id):
        return {'model_id': model_id, 'record_revision': 1}

    def list_payloads(self):
        return ()

    def update_cache(self, *_args, **_kwargs):
        raise AssertionError('不该走到这里')

    def update(self, *_args, **_kwargs):
        raise AssertionError('不该走到这里')


def collected(monkeypatch):
    # Patch the name the service module actually calls: it imported the
    # function into its own namespace, so patching events.record_model_event
    # would have no effect and every assertion below would silently pass on an
    # empty list.
    seen = []
    monkeypatch.setattr('prism.model_library.service.record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    return seen


def test_capability_off_is_reported(monkeypatch):
    seen = collected(monkeypatch)
    service = ModelLibraryService(repository=_Repository(), can_write=lambda: True)
    # Default capabilities have preview off; that is the "nothing happens" case.
    service.ensure_covers([_Summary('m1')])
    names = [name for name, _fields in seen]
    assert events.EVENT_THUMBNAIL_REQUEST_SKIPPED in names, names
    reason = [fields for name, fields in seen
              if name == events.EVENT_THUMBNAIL_REQUEST_SKIPPED][0]
    assert reason['reason'] == 'preview_capability_off'
    assert reason['model_count'] == 1


def test_readonly_is_reported(monkeypatch):
    seen = collected(monkeypatch)
    service = ModelLibraryService(repository=_Repository(), can_write=lambda: True)
    service._caps = dataclasses.replace(service._caps, preview=True)
    service._readonly = True
    service.ensure_covers([_Summary('m1')])
    reasons = [fields['reason'] for name, fields in seen
               if name == events.EVENT_THUMBNAIL_REQUEST_SKIPPED]
    assert reasons == ['readonly']


def test_closed_is_reported(monkeypatch):
    seen = collected(monkeypatch)
    service = ModelLibraryService(repository=_Repository(), can_write=lambda: True)
    service._caps = dataclasses.replace(service._caps, preview=True)
    service._closed = True
    service.ensure_covers([_Summary('m1')])
    reasons = [fields['reason'] for name, fields in seen
               if name == events.EVENT_THUMBNAIL_REQUEST_SKIPPED]
    assert reasons == ['closed']


def test_planned_counts_split_ready_and_requested(monkeypatch):
    seen = collected(monkeypatch)
    service = ModelLibraryService(repository=_Repository(), can_write=lambda: True)
    service._caps = dataclasses.replace(service._caps, preview=True)

    requests = []

    def fake_render(model_id):
        requests.append(model_id)
        return 'handle-%s' % model_id

    service.render_cover = fake_render
    service.ensure_covers([_Summary('ready-one', state='ready', data_url='data:x'),
                           _Summary('needs-one')])
    planned = [fields for name, fields in seen
               if name == events.EVENT_THUMBNAIL_CALCULATED]
    assert planned, [name for name, _ in seen]
    assert planned[0]['requested'] == 1
    assert planned[0]['already_ready'] == 1
    assert requests == ['needs-one']


def test_render_cover_names_the_route(monkeypatch):
    seen = collected(monkeypatch)
    service = ModelLibraryService(repository=_Repository(), can_write=lambda: True)
    service._native_job = lambda model_id, preview=False: 'job'
    service.render_cover('m1')
    requested = [fields for name, fields in seen
                 if name == events.EVENT_THUMBNAIL_REQUESTED]
    assert requested and requested[0]['via'] == 'native'


def test_new_events_are_declared():
    for name in (events.EVENT_THUMBNAIL_REQUESTED,
                 events.EVENT_THUMBNAIL_REQUEST_SKIPPED,
                 events.EVENT_THUMBNAIL_CALCULATED,
                 events.EVENT_IMPORT_DEPENDENCIES_DETECTED,
                 events.EVENT_IMPORT_COPIED,
                 events.EVENT_IMPORT_VALIDATED,
                 events.EVENT_IMPORT_COMMITTED,
                 events.EVENT_EXPORT_MANIFEST_FIXED,
                 events.EVENT_EXPORT_ITEM_FAILED):
        assert name in events.REQUIRED_FIELDS, '%s 未声明' % name


def test_import_stage_events_land_in_the_log(tmp_path, monkeypatch):
    """The real importer must write its stages (verified through the trace)."""
    import threading
    from unittest.mock import Mock
    from prism.model_library.importer import ModelImporter
    records = []
    monkeypatch.setattr('prism.performance_trace.record',
        lambda event, **fields: records.append(event))
    source = tmp_path/'model.obj'
    original = b'v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n'
    source.write_bytes(original)
    importer = ModelImporter(None, None)
    monkeypatch.setattr(importer, '_commit', Mock(side_effect=RuntimeError('injected commit failure')))
    with pytest.raises(RuntimeError, match='injected commit failure'):
        importer._import_one('stage-test', str(source), None, threading.Event())
    names = [name for name in records if name.startswith('model_import.')]
    assert names[:3] == ['model_import.dependencies_detected',
                         'model_import.copied',
                         'model_import.validated'], names
    # The run failed at commit on purpose: no committed event may appear.
    assert 'model_import.committed' not in names
    assert source.read_bytes() == original
