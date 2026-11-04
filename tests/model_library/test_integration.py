"""Integrator checks: real desktop composition and real persistence, not fakes."""
import hashlib
import json
import os
import sqlite3
from pathlib import Path

import pytest

from prism.model_library.contracts import ModelQuery
from prism.model_library.project_storage import decode_record, encode_record


def test_manifest_contract_keeps_missing_and_unknown_file_fields():
    payload = dict(model_id='x', project_id='p', name='x', main_file_id='main',
        format='obj', unknown_fields={'future': {'keep': 7}}, files=[dict(
            file_id='missing', relative_path='_missing/albedo.png', role='texture',
            size_bytes=0, sha256='', source_ref='', missing=True,
            original_reference='../source/albedo.png', attachment_id=None)])
    record = decode_record(payload)
    assert record.files[0].missing
    assert record.files[0].original_reference == '../source/albedo.png'
    assert encode_record(record)['files'][0]['missing']
    assert encode_record(record)['unknown_fields']['future'] == {'keep': 7}


def test_real_gui_import_save_reopen_and_original_export(view, qtbot, tmp_path, monkeypatch):
    from prism.model_library.service import ModelLibraryService
    # The real native runtime has its own subprocess integration test. This
    # test explicitly leaves preview unavailable and exercises persistence UI.
    monkeypatch.setattr(ModelLibraryService, 'probe_native', lambda self: None)
    source = tmp_path / 'source'
    source.mkdir()
    obj = source / 'chair.obj'
    obj.write_text('mtllib chair.mtl\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    (source / 'chair.mtl').write_text('newmtl paint\nKd 1 0 0\nmap_Kd absent.png\n')
    expected = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in source.iterdir()}
    target = tmp_path / 'integration.prism'
    view.do_save(str(target), create_new=True, silent=True)
    qtbot.waitUntil(lambda: view.filename == str(target) and
        getattr(view, '_save_operation', None) is None, timeout=30000)
    window = view.parent
    window.open_model_library()
    manager = window.model_composition
    qtbot.waitUntil(lambda: manager.service is not None, timeout=15000)
    service = manager.service
    assert service.capabilities.import_models
    assert not service.capabilities.preview
    job = window.model_library.import_paths((str(obj),))
    assert job
    qtbot.waitUntil(lambda: not service.pending_writes and bool(view.scene.model_records), timeout=15000)
    record = service.query(ModelQuery()).items[0]
    full = service.get(record.model_id)
    assert full.files[-1].missing
    assert full.files[-1].original_reference == 'absent.png'
    assert full.record_revision == 1
    edited = service.update_metadata(record.model_id, tags='wood, chair', notes='note survives',
                                    expected_revision=full.record_revision)
    assert edited.notes == 'note survives'
    # A separate canvas edit must remain dirty when a durable model command
    # gets acknowledged. Only the exact model row may advance its baseline.
    view.mark_content_dirty()
    service.update_metadata(record.model_id, tags='wood', notes='new note',
                            expected_revision=edited.record_revision)
    assert view.is_dirty()
    view.do_save(str(target), create_new=False, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=30000)
    assert not view.is_dirty()
    from prism.storage.managed import database_path
    with sqlite3.connect(database_path(target)) as db:
        stored = json.loads(db.execute('SELECT payload FROM model_records WHERE id=?',
                                      (record.model_id,)).fetchone()[0])
        assert stored['notes'] == 'new note'
        assert len(stored['files']) == 3
    output = tmp_path / 'export'
    output.mkdir()
    export_job = service.export_originals((record.model_id,), str(output))
    qtbot.waitUntil(lambda: service.exporter.job_state(export_job) in ('completed', 'failed'), timeout=15000)
    assert service.exporter.job_state(export_job) == 'completed'
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
             for p in output.rglob('*') if p.is_file() and p.name in expected}
    assert files == expected
    # The GUI save path made a real managed project; independently reopen its
    # model context rather than reusing the service's in-memory record.
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    reopened = ModelRepositoryAdapter(open_model_context(target))
    assert reopened.get(record.model_id).notes == 'new note'
    assert reopened.get(record.model_id).files[-1].missing


def test_search_includes_tags_and_respects_literal_wildcards(tmp_path, qapp):
    from tests.model_library.test_storage import create_project, make_scene, sample_payload
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    target = tmp_path / 'search.prism'
    create_project(target, make_scene())
    repo = ModelRepositoryAdapter(open_model_context(target))
    repo.insert(sample_payload(name='zebra', tags=['metal_100%']))
    repo.insert(sample_payload('second', 'Alpha', tags=['wood']))
    assert [r.name for r in repo.query(ModelQuery()).items] == ['Alpha', 'zebra']
    assert repo.query(ModelQuery(text='metal_100%')).total == 1
    assert repo.query(ModelQuery(text='metal_100x')).total == 0


def test_cancel_during_original_storage_rolls_back(tmp_path, qapp, monkeypatch):
    import threading
    from tests.model_library.test_storage import create_project, make_scene
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from prism.model_library.importer import ModelImporter
    target = tmp_path / 'cancel.prism'
    create_project(target, make_scene())
    repo = ModelRepositoryAdapter(open_model_context(target))
    importer = ModelImporter(repo.context, repo)
    source = tmp_path / 'large.obj'
    source.write_bytes(b'v 0 0 0\n' + b'#' + b'x' * (3 * 1024 * 1024))
    original = repo.resources._chunks
    copied = threading.Event()
    def chunks(path, digest, cancel=None):
        for chunk in original(path, digest, cancel):
            yield chunk
            copied.set()
            for job in list(importer._jobs.values()):
                job['cancel'].set()
    monkeypatch.setattr(repo.resources, '_chunks', chunks)
    job = importer.import_paths((str(source),))
    assert copied.wait(5)
    importer._jobs[job]['thread'].join(5)
    assert importer.job_state(job) == 'cancelled'
    assert repo.query(ModelQuery()).total == 0
    with sqlite3.connect(repo.context.metadata_path) as db:
        assert db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0] == 0


def test_unloaded_partial_domain_cannot_start_a_durable_model_command():
    from types import SimpleNamespace
    from prism.model_library.composition import ModelLibraryComposition
    from prism.model_library.service import ModelLibraryService
    scene = SimpleNamespace(_partial_project_source='project', _partial_project_session=None)
    owner = SimpleNamespace(window=SimpleNamespace(view=SimpleNamespace(scene=scene)),
                            _key=lambda: 'current')
    assert not ModelLibraryComposition._can_write(owner, 'current')
    scene._partial_project_session = SimpleNamespace(_domains=None)
    assert not ModelLibraryComposition._can_write(owner, 'current')
    scene._partial_project_session._domains = object()
    assert ModelLibraryComposition._can_write(owner, 'current')
    gate = False
    service = ModelLibraryService(repository=object(), on_commit=lambda a,b: None,
                                  can_write=lambda: gate)
    assert not service.capabilities.edit_metadata
    with pytest.raises(Exception):
        service.import_paths(('must-not-read.obj',))
    service.close()


@pytest.mark.skipif(os.environ.get('PRISM_TEST_MODEL_NATIVE_UI') != '1',
                    reason='Explicit real GPU/desktop preview test')
def test_native_gui_cover_preview_and_texture_roundtrip(view, qtbot, tmp_path, monkeypatch):
    """Real file dialog entry, DOM click/doubleclick, helper first frame,
    changed texture, persistent appearance and byte-exact original export.
    """
    from dataclasses import replace
    from PyQt6 import QtCore, QtWidgets
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    source = Path.cwd() / 'third_party/model-library/samples'
    original = source / 'gltf_external_bin.gltf'
    project = tmp_path / 'native.prism'
    view.do_save(str(project), create_new=True, silent=True)
    qtbot.waitUntil(lambda: view.filename == str(project) and
        getattr(view, '_save_operation', None) is None, timeout=30000)
    window = view.parent
    window.show()
    window.open_model_library()
    manager = window.model_composition
    qtbot.waitUntil(lambda: manager.service is not None, timeout=20000)
    service = manager.service
    qtbot.waitUntil(lambda: service.backend._probe is not None, timeout=60000)
    qtbot.waitUntil(lambda: service._caps.preview, timeout=10000)
    # The parent test uses offscreen Qt. The isolated preview has its own
    # real Windows OpenGL context; don't turn the user's settings into a test.
    service.preview_controller._config = replace(service.preview_controller._config,
        env={'QT_QPA_PLATFORM': 'windows'})
    library = window.model_library
    events = []
    publisher = service._publish
    def record_event(event):
        events.append(event)
        publisher(event)
    service._publish = record_event
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getOpenFileNames',
                        lambda *a, **k: ([str(original)], ''))
    qtbot.mouseClick(library.import_button, QtCore.Qt.MouseButton.LeftButton)
    qtbot.waitUntil(lambda: bool(view.scene.model_records) and not service.pending_writes, timeout=20000)
    model_id = view.scene.model_records[0]['id']
    qtbot.waitUntil(lambda: bool(service.repository.thumbnail_data_url(model_id)), timeout=60000)
    initial_cover = service.repository.get_payload(model_id)['thumbnail']['cache_key']
    qtbot.waitUntil(lambda: library.cards is not None and library.cards.ready, timeout=20000)

    def js(script):
        result = []
        library.cards.web.page().runJavaScript(script, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=10000)
        return result[0]

    qtbot.waitUntil(lambda: js('document.querySelectorAll(".asset-card").length') > 0, timeout=15000)
    js('document.querySelector(".asset-card").click(); true')
    qtbot.waitUntil(lambda: library.selection.current == model_id, timeout=10000)
    js('document.querySelector(".asset-card").dispatchEvent(new MouseEvent("dblclick", {bubbles:true})); true')
    qtbot.waitUntil(lambda: getattr(service, '_active_preview', None) is not None, timeout=60000)
    assert len(service._leases) == 1
    assert service.get(model_id).statistics.basis == 'f3d-loaded'
    assert service.get(model_id).statistics.vertices > 0
    assert 'f3d' not in __import__('sys').modules
    library.details.notes.setFocus()
    qtbot.keyClicks(library.details.notes, 'draft survives background cover')
    revision = service.get(model_id).record_revision
    service.render_cover(model_id)
    qtbot.waitUntil(lambda: service.get(model_id).notes == 'draft survives background cover', timeout=10000)
    assert service.get(model_id).record_revision == revision + 1

    texture = source / 'obj_albedo.png'
    service.attach_texture(model_id, 'basecolor', str(texture))
    qtbot.waitUntil(lambda: not service.pending_writes, timeout=20000)
    qtbot.waitUntil(lambda: service.get(model_id).appearance.revision == 1, timeout=10000)
    qtbot.waitUntil(lambda: len(service._preparing) == 0, timeout=60000)
    try:
        qtbot.waitUntil(lambda: service.repository.get_payload(model_id).get('thumbnail', {}).get(
            'appearance_revision') == 1, timeout=15000)
    except Exception:
        raise AssertionError(dict(events=events[-20:], status=library.status.text(),
            payload=service.repository.get_payload(model_id),
            logs=list(service.backend.controller._log_tail)[-12:]))
    assert service.repository.get_payload(model_id)['thumbnail']['cache_key'] != initial_cover
    service.capture_preview_cover()
    try:
        qtbot.waitUntil(lambda: service.repository.get_payload(model_id).get('thumbnail', {}).get(
            'camera_revision', 0) > 0, timeout=15000)
    except Exception:
        raise AssertionError(dict(status=library.status.text(),
            active=repr(getattr(service, '_active_preview', None)),
            preview_jobs=[dict(state=j.state, result=j.result) for j in service.preview_controller._jobs.values()],
            logs=list(service.preview_controller._log_tail)[-15:]))
    view.do_save(str(project), create_new=False, silent=True)
    qtbot.waitUntil(lambda: getattr(view, '_save_operation', None) is None, timeout=30000)
    reopened = ModelRepositoryAdapter(open_model_context(project))
    assert reopened.get(model_id).appearance.bindings['basecolor']['sha256'] == hashlib.sha256(texture.read_bytes()).hexdigest()
    # The original package must not acquire the applied appearance texture.
    destination = tmp_path / 'export'
    destination.mkdir()
    job = service.export_originals((model_id,), str(destination))
    qtbot.waitUntil(lambda: service.exporter.job_state(job) in ('completed', 'failed'), timeout=15000)
    assert service.exporter.job_state(job) == 'completed'
    output = [p for p in destination.rglob(original.name) if p.is_file()]
    assert len(output) == 1
    assert output[0].read_bytes() == original.read_bytes()
    assert not list(destination.rglob(texture.name))
    for name in ('gltf_external_bin.bin', 'gltf_albedo.png'):
        exported = [p for p in destination.rglob(name) if p.is_file()]
        assert len(exported) == 1 and exported[0].read_bytes() == (source / name).read_bytes()
    evidence = Path('artifacts/model-library-integration')
    evidence.mkdir(parents=True, exist_ok=True)
    payload = service.repository.get_payload(model_id)
    cover_path = service.repository.cover_cache_root / (payload['thumbnail']['cache_key'] + '.png')
    (evidence / 'native-final-cover.png').write_bytes(cover_path.read_bytes())
    (evidence / 'native-evidence.json').write_text(json.dumps(dict(
        test='test_native_gui_cover_preview_and_texture_roundtrip',
        runtime=service.backend.describe(),
        input_sha256=hashlib.sha256(original.read_bytes()).hexdigest(),
        texture_sha256=hashlib.sha256(texture.read_bytes()).hexdigest(),
        source_format='glTF + external BIN + PNG',
        record_revision=payload['record_revision'], appearance=payload['appearance'],
        statistics=payload['statistics'], thumbnail=payload['thumbnail'],
        gui_entry='file dialog button, DOM click and doubleclick',
        native_open_ack='first rendered frame, not widget existence',
        original_and_dependencies_byte_equal=True,
        applied_texture_not_in_original_export=True,
        source_bytes_not_modified=hashlib.sha256(original.read_bytes()).hexdigest() ==
            hashlib.sha256(output[0].read_bytes()).hexdigest(),
        not_proven=['all source formats through GUI', 'SSS', 'displacement', 'pure wireframe',
                    'full PBR visual equivalence', '100k real-model GUI/VRAM/cache budgets']
    ), ensure_ascii=False, indent=2), encoding='utf-8')


def test_pending_notes_follow_the_edited_model_and_failure_preserves_draft(qtbot):
    from tests.model_library.test_ui import FakeHost, FakeRepository, FakeService
    from prism.widgets.model_library import ModelLibrary, ModelLibraryState
    from prism.model_library.contracts import ModelCapabilities
    host = FakeHost()
    qtbot.addWidget(host)
    page = ModelLibrary(host)
    qtbot.addWidget(page)
    service = FakeService(FakeRepository(2), ModelCapabilities(browse=True, edit_metadata=True))
    page.set_service(service)
    page.bind_scope('project-a', 1)
    qtbot.waitUntil(lambda: page.state == ModelLibraryState.READY)
    page._selection_changed('model-000')
    qtbot.waitUntil(lambda: page.details._record is not None and page.details._record.model_id == 'model-000')
    page.details.notes.setPlainText('old model draft')
    page._selection_changed('model-001')
    writes = [call for call in service.calls if call[0] == 'update_metadata']
    assert writes[-1][1] == 'model-000'
    assert writes[-1][3] == 'old model draft'
    qtbot.waitUntil(lambda: page.details._record is not None and page.details._record.model_id == 'model-001')
    def refused(*args, **kwargs):
        raise RuntimeError('test storage refusal')
    service.update_metadata = refused
    page.details.notes.setPlainText('must stay available')
    page._selection_changed('model-000')
    assert page.selection.current == 'model-001'
    assert page.details.notes.toPlainText() == 'must stay available'
    assert page.details._pending_notes == 'must stay available'
    page.details._pending_notes = None
    page.details._pending_tags = None
    page.details._metadata_timer.stop()
    if page._pool is not None:
        page._pool.waitForDone(5000)
