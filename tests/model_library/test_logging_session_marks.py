"""L10: first frame, thumbnail store, project binding.

Two of the three can be driven for real here:
* the thumbnail store is a plain method (fake assets + fake web);
* the project binding fires when a service is constructed over a real temporary
  SQLite project.
The first-frame event lives inside the preview worker's poll loop, which needs a
VTK window and an IPC channel; that one is verified by contract + source only,
and labelled as such.
"""
import json
import os
import sys
import types

import pytest

REPO = r'C:\Users\admin\Desktop\Prism-main\Prism-main'
sys.path.insert(0, REPO)

from prism.model_library import events as model_events                  # noqa: E402
from prism.model_library.project_storage import (ModelProjectContext,   # noqa: E402
                                                 connect, ensure_model_tables)
from prism.model_library.repository import ModelRepositoryAdapter       # noqa: E402
from prism.model_library import service as service_module               # noqa: E402
import prism.widgets.material_cards as cards                            # noqa: E402


def _project(root):
    metadata = os.path.join(root, 'project.prism')
    connection = connect(metadata)
    try:
        ensure_model_tables(connection)
        connection.execute('PRAGMA user_version=1')
    finally:
        connection.close()
    return ModelProjectContext(project_path=metadata, metadata_path=metadata,
                               project_id='proj-bind', storage_instance_id='store-bind',
                               open_epoch=3, session_token='sess-bind',
                               backend='sqlite', source_revision='1',
                               managed_root=None)


def test_project_binding_is_recorded(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr('prism.model_library.service.record_model_event',
                        lambda name, **fields: seen.append((name, fields)))
    repository = ModelRepositoryAdapter(_project(str(tmp_path)))
    service_module.ModelLibraryService(repository=repository, can_write=lambda: True)
    bound = [fields for name, fields in seen if name == 'model_project_bound']
    assert bound, [name for name, _ in seen]
    assert bound[0]['project_session'] == 'sess-bind'
    assert bound[0]['project_id'] == 'proj-bind'
    assert bound[0]['open_epoch'] == 3
    assert bound[0]['state'] == 'bound'


def _grid(assets):
    grid = types.SimpleNamespace()
    grid.assets = assets
    grid._signature = None
    grid.ready = True
    grid._dom_to_id = {}
    grid._dom_id = lambda record_id: record_id
    written = []

    class _Page:
        def runJavaScript(self, script):
            written.append(script)

    class _Web:
        def page(self):
            return _Page()

    grid.web = _Web()
    return grid, written


def test_thumbnail_store_marks_placeholder(monkeypatch, qapp):
    seen = []
    monkeypatch.setattr(cards, 'trace_record',
                        lambda name, **fields: seen.append((name, fields)))
    record = {'id': 'm-1', 'assetKind': 'material', 'name': 'Wood'}
    grid, written = _grid([{'id': 'm-1', 'thumbnail': '', 'thumbnailPending': True}])
    cards.MaterialCards.update_thumbnail(grid, record, None)      # no image
    stored = [fields for name, fields in seen if name == 'material_thumbnail_stored']
    assert stored, [name for name, _ in seen]
    assert stored[0]['real'] is False
    assert stored[0]['surface_ready'] is True
    # The card still gets a picture -- a placeholder -- and stops pending.
    assert grid.assets[0]['thumbnailPending'] is False
    assert grid.assets[0]['thumbnail']


def test_thumbnail_store_marks_real(monkeypatch, qapp):
    seen = []
    monkeypatch.setattr(cards, 'trace_record',
                        lambda name, **fields: seen.append((name, fields)))
    record = {'id': 'm-2', 'assetKind': 'material', 'name': 'Steel'}
    grid, written = _grid([{'id': 'm-2', 'thumbnail': '', 'thumbnailPending': True}])
    cards.MaterialCards.update_thumbnail(grid, record, 'data:image/png;base64,AAAA')
    stored = [fields for name, fields in seen if name == 'material_thumbnail_stored']
    assert stored and stored[0]['real'] is True
    assert grid.assets[0]['thumbnail'].startswith('data:image/png')


@pytest.mark.parametrize('name,required', [
    ('material.first_frame', ('bindings', 'shown')),
    ('material_thumbnail_stored', ('material', 'real')),
    ('model_project_bound', ('project_session', 'state')),
])
def test_new_events_are_declared(name, required):
    assert name in model_events.REQUIRED_FIELDS, '%s 未声明' % name
    for field in required:
        assert field in model_events.REQUIRED_FIELDS[name]


def test_first_frame_event_source_is_present():
    """The worker needs VTK + IPC to run, so the placement is checked here."""
    source = open(os.path.join(REPO, 'prism', 'components', 'material_preview.py'),
                  encoding='utf-8').read()
    assert "event('material.first_frame'" in source
    # It must be reported once per loaded model, after the render call.
    # The checked wrapper raises on VTK shader errors; a raw Render() can
    # return while the shader failed and must not count as a first frame.
    assert source.index('renderer.render()',source.index("elif action=='reset'")) < source.index(
        "event('material.first_frame'")
    assert "state.get('first_frame_sent')" in source
