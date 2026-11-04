import errno
from types import SimpleNamespace
from unittest.mock import Mock

from prism.model_library import native_protocol as wire


def test_peer_reset_is_classified_and_terminal():
    sock = Mock()
    sock.recv.side_effect = ConnectionResetError(errno.ECONNRESET, 'reset')
    channel = wire.Channel(sock)
    import pytest
    with pytest.raises(wire.PeerDisconnected):
        channel.recv(.1)
    assert channel.closed
    assert channel.recv(.1) is None


def test_catalog_progress_cannot_hide_current_open_failure(main_window):
    view = main_window.view
    view._trace_open_epoch = 4
    view._paged_open_failed = True
    view._catalog_status('Source changed; reopen before saving', error=True)
    view._catalog_status('Preparing the project index')
    assert main_window.statusBar().currentMessage() == 'Source changed; reopen before saving'
    view._trace_open_epoch = 5
    view._catalog_status('Opening another project')
    assert main_window.statusBar().currentMessage() == 'Opening another project'


def test_unsaved_confirmation_does_not_construct_hidden_model_page(main_window):
    assert not main_window.model_library.initialized
    assert main_window.view.get_confirmation_unsaved_changes('Continue?')
    assert not main_window.model_library.initialized


def test_stale_catalog_requests_background_repair_once(main_window, qtbot, monkeypatch):
    view = main_window.view
    stale = SimpleNamespace(ready=False, index=SimpleNamespace(source='project.prism'), cache_path='catalog.sqlite')
    view._open_scope = stale
    calls = []
    monkeypatch.setattr(view, '_maybe_start_catalog_build', lambda source, on_ready: calls.append((source, on_ready)))
    assert view.current_catalog_scope() is stale
    assert view.current_catalog_scope() is stale
    qtbot.waitUntil(lambda: len(calls) == 1)
    installed = SimpleNamespace(ready=True)
    monkeypatch.setattr('prism.project_open_scope.ProjectOpenScope', lambda *args: installed)
    monkeypatch.setattr(view.category_panel, 'update_counts', Mock())
    monkeypatch.setattr(view.category_panel, 'search_content_changed', Mock())
    calls[0][1]('catalog.sqlite')
    assert view._open_scope is installed
    view.category_panel.search_content_changed.assert_called_once()


def test_range_selection_updates_inspector_only_once(qtbot):
    from prism.widgets.material_cards import MaterialCards
    cards = MaterialCards()
    qtbot.addWidget(cards)
    cards.set_display_assets([dict(id=str(i), name=str(i)) for i in range(20)])
    cards.select_record('0')
    changes = []
    cards.selection_changed.connect(changes.append)
    cards.bridge.chooseRange(cards._dom_id('19'), cards.generation, False)
    assert cards.selected_ids == {str(i) for i in range(20)}
    assert changes == ['19']


def test_native_preparation_reuses_unchanged_materials(tmp_path, monkeypatch):
    from dataclasses import replace
    from prism.model_library.multimaterial import presentation as p
    from prism.model_library.multimaterial.contracts import MaterialCatalog, MaterialState, MaterialTarget, MaterialDefinition
    target = MaterialTarget('project', 1, 'model', 1, 1, 1)
    catalog = MaterialCatalog(1, 1, 'digest', 'test', (), ())
    item = dict(path=tmp_path/'source.fbx', paths={}, catalog=catalog)
    state = MaterialState(target, (), ())
    one = MaterialDefinition('one', 'one', 'one', 'pbr', ())
    two = MaterialDefinition('two', 'two', 'two', 'pbr', ())
    original = p.material_spec
    prepare = Mock(side_effect=original)
    monkeypatch.setattr(p, 'material_spec', prepare)
    p.material_artifact(item, state, tmp_path, 'assimp.dll', {'one': one, 'two': two})
    assert prepare.call_count == 2
    p.material_artifact(item, state, tmp_path, 'assimp.dll', {'one': one, 'two': two})
    assert prepare.call_count == 2
    changed = replace(one, properties_json='{"slots":{"roughness":{"value":0.2}}}')
    p.material_artifact(item, state, tmp_path, 'assimp.dll', {'one': changed, 'two': two})
    assert prepare.call_count == 3


def test_camera_waits_for_pending_canvas_without_resetting_user_transform(main_window):
    from PyQt6 import QtGui
    from prism import camera_policy
    view = main_window.view
    view.current_canvas_id = 'waiting'
    view._pending_canvas_demand = 'waiting'
    view.setTransform(QtGui.QTransform().scale(2, 2))
    previous = QtGui.QTransform(view.transform())
    assert camera_policy.request_reset(view, lambda view: None)
    assert view.transform() == previous
    assert view._camera_pending_reset[3] is True


def test_closed_or_invalid_owner_does_not_create_native_window(qapp, monkeypatch):
    from PyQt6 import QtWidgets, QtGui
    from prism.qt_window_owner import bind_window_owner
    widget = Mock()
    # An exiting application must never re-create a foreign/native window.
    wrap = Mock()
    with monkeypatch.context() as scoped:
        scoped.setattr(QtWidgets.QApplication, 'instance', lambda: SimpleNamespace(closingDown=lambda: True))
        scoped.setattr(QtGui.QWindow, 'fromWinId', wrap)
        assert bind_window_owner(widget, 12345) is None
        widget.winId.assert_not_called()
        wrap.assert_not_called()


def test_library_orm_source_is_read_once_per_material(tmp_path):
    import threading
    from prism.model_library.multimaterial.application import ProjectMaterialService
    read = Mock(return_value=b'immutable-source')
    record = dict(id='orm', name='ORM', textures={
        slot: dict(attachmentId=1, channel=channel)
        for slot, channel in [('ao', 'R'), ('roughness', 'G'), ('metallic', 'B')]})
    service = SimpleNamespace(_lock=threading.Lock(), _library_records={'snapshot': record},
                              _root=tmp_path, owner=SimpleNamespace(repository=SimpleNamespace(
                                  resources=SimpleNamespace(read_all=read))))
    item = dict(paths={})
    definition = ProjectMaterialService._library_definition(service, item, 'snapshot')
    assert read.call_count == 1
    assert len(definition.textures) == 3
    assert [use.channels for use in definition.textures] == ['R', 'G', 'B']


def test_shared_embedded_texture_does_not_reparse_glb(tmp_path, monkeypatch):
    import hashlib
    from prism.model_library.multimaterial.application import ProjectMaterialService
    from prism.model_library.multimaterial.contracts import MaterialCatalog, MaterialDefinition, TextureUse, MaterialTarget
    from prism.model_library import preview_channels
    blob = b'embedded-pixels'
    rid = 'sha256:' + hashlib.sha256(blob).hexdigest()
    use = TextureUse('basecolor', rid, 'RGB', 0, 'srgb', unknown_json='{"texture_index":0}')
    definition = MaterialDefinition('one', 'one', 'one', 'pbr', (use, use))
    catalog = MaterialCatalog(1, 1, 'digest', 'test', (definition,), ())
    (tmp_path / 'model.glb').write_bytes(b'container')
    parse = Mock(return_value=({'textures': [{'source': 0}]}, b'binary'))
    image = Mock(return_value=blob)
    monkeypatch.setattr(preview_channels, 'read_gltf', parse)
    monkeypatch.setattr(preview_channels, '_image_bytes', image)
    monkeypatch.setattr('prism.model_library.multimaterial.application.inspect_source', lambda *args, **kw: catalog)
    owner = SimpleNamespace(repository=SimpleNamespace(get_payload=lambda key: {'source_revision': 1, 'files': []}),
                            bundles=SimpleNamespace(prepare=lambda *args: SimpleNamespace(
                                package_root=tmp_path, main_relative_path='model.glb')))
    service = SimpleNamespace(owner=owner, _assert_session=lambda target: None,
                              _models={}, _root=tmp_path, _initial=ProjectMaterialService._initial)
    ProjectMaterialService._load_serial(service, MaterialTarget('p', 1, 'm', 1, 1, 1))
    assert parse.call_count == 1
    assert image.call_count == 1
