"""The model domain really belongs to the project.

These tests use a **real** project file written by the application's own
writer.  They do not need F3D and they do not use a fake repository.
"""
import json
import sqlite3

import pytest

from prism.model_library.contracts import ModelQuery, ModelRecord
from prism.model_library.project_storage import (
    ModelProjectContext,
    ModelRevisionConflict,
    ModelResourceStore,
    ModelStorageError,
    open_model_context,
)
from prism.model_library.repository import ModelRepositoryAdapter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_scene():
    from PyQt6.QtGui import QUndoStack
    from prism.scene import PrismGraphicsScene
    return PrismGraphicsScene(QUndoStack())


def create_project(path, scene):
    """Write a project with the application's own writer."""
    from prism.fileio.sql import SQLiteIO
    io = SQLiteIO(str(path), scene, create_new=True)
    try:
        io.write()
    finally:
        io._close_connection()
    return str(path)


def open_project(path, scene):
    from prism.fileio.sql import SQLiteIO
    io = SQLiteIO(str(path), scene, readonly=True)
    try:
        io.read()
    finally:
        io._close_connection()
    return scene


def sample_payload(model_id='model-1', name='Chair', **extra):
    payload = {
        'id': model_id,
        'model_id': model_id,
        'project_id': '',
        'name': name,
        'main_file_id': 'file-1',
        'format': 'fbx',
        'source_revision': 1,
        'record_revision': 0,
        'folder_id': None,
        'tags': [],
        'notes': '',
        'files': [{
            'file_id': 'file-1',
            'relative_path': 'models/chair.fbx',
            'role': 'main',
            'size_bytes': 12,
            'sha256': 'a' * 64,
            'source_ref': 'attachment:1',
            'origin': 'imported',
            'missing': False,
            'attachment_id': 1,
            'original_reference': 'chair.fbx',
            'absolute_reference': False,
        }],
        'appearance': {'revision': 0, 'bindings': {}, 'properties': {}},
        'statistics': {'vertices': None, 'triangles': None, 'polygons': None,
                       'instances': None, 'basis': 'unknown', 'backend_version': ''},
        'unknown_fields': {},
        'created_at': '2026-10-26T00:00:00',
        'updated_at': '2026-10-26T00:00:00',
        'sort_order': 0,
        'total_size': 12,
    }
    payload.update(extra)
    return payload


@pytest.fixture
def project(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'project.prism', scene)
    return path, scene


@pytest.fixture
def context(project):
    path, _scene = project
    return open_model_context(path, open_epoch=7)


@pytest.fixture
def repository(context):
    return ModelRepositoryAdapter(context)


# ---------------------------------------------------------------------------
# A1 / A2: identity and persistence
# ---------------------------------------------------------------------------


def test_context_binds_a_real_project(context, project):
    path, scene = project
    assert context.project_path == str(path)
    assert context.metadata_path == str(path)
    assert context.backend == 'legacy_sqlite'
    assert context.open_epoch == 7
    assert context.session_token
    # The business identity is the one the project itself stores.
    with sqlite3.connect(path) as db:
        stored = db.execute(
            "SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()[0]
    assert context.project_id == stored
    assert context.storage_instance_id.startswith('legacy:')
    assert context.source_revision


def test_two_opens_of_one_project_have_different_sessions(project):
    path, _scene = project
    first = open_model_context(path, open_epoch=1)
    second = open_model_context(path, open_epoch=1)
    assert first.project_session != second.project_session
    assert first.project_id == second.project_id
    assert first.storage_instance_id == second.storage_instance_id


def test_insert_get_and_query(repository, context):
    payload = repository.insert(sample_payload())
    assert payload['project_id'] == context.project_id
    assert payload['record_revision'] == 1

    record = repository.get('model-1')
    assert isinstance(record, ModelRecord)
    assert record.name == 'Chair'
    assert record.format == 'fbx'
    assert record.files[0].relative_path == 'models/chair.fbx'

    page = repository.query(ModelQuery())
    assert page.total == 1
    assert page.items[0].model_id == 'model-1'
    assert page.items[0].size_bytes == 12


def test_query_pages_and_filters(repository):
    repository.insert(sample_payload('a', 'Alpha'))
    repository.insert(sample_payload('b', 'Beta', format='obj'))
    repository.insert(sample_payload('c', 'Gamma', format='glb'))

    page = repository.query(ModelQuery(limit=2, offset=0, sort='name'))
    assert page.total == 3
    assert [item.name for item in page.items] == ['Alpha', 'Beta']

    page = repository.query(ModelQuery(limit=10, sort='format'))
    assert [item.format for item in page.items] == ['fbx', 'glb', 'obj']

    page = repository.query(ModelQuery(limit=10, text='amm'))
    assert [item.name for item in page.items] == ['Gamma']

    with pytest.raises(ValueError):
        repository.query(ModelQuery(sort='not-a-sort-key'))


def test_query_reads_no_blob(repository, context):
    repository.insert(sample_payload())
    # A blob that cannot be read must not affect a query: the query only
    # touches the metadata table.
    page = repository.query(ModelQuery())
    assert page.total == 1
    assert page.items[0].thumbnail_state == 'missing'


def test_save_uses_expected_revision(repository):
    repository.insert(sample_payload())
    record = repository.get('model-1')
    updated = ModelRecord(**{**record.__dict__, 'notes': 'edited'})
    repository.save(updated, expected_revision=1)
    assert repository.get('model-1').notes == 'edited'
    assert repository.get('model-1').record_revision == 2


def test_save_refuses_to_overwrite_a_later_edit(repository):
    repository.insert(sample_payload())
    first = repository.get('model-1')
    second = repository.get('model-1')

    # A second writer wins the race.
    repository.save(ModelRecord(**{**second.__dict__, 'notes': 'B'}),
                    expected_revision=1)

    # The first writer must fail, not overwrite B.
    with pytest.raises(ModelRevisionConflict) as failure:
        repository.save(ModelRecord(**{**first.__dict__, 'notes': 'A'}),
                        expected_revision=1)
    assert failure.value.actual_revision == 2
    assert repository.get('model-1').notes == 'B'


def test_save_of_a_missing_record_is_a_conflict(repository, context):
    with pytest.raises(ModelRevisionConflict):
        repository.save(ModelRecord(model_id='nope', project_id=context.project_id,
                                    name='n', main_file_id='f', format='fbx'),
                        expected_revision=1)


def test_unknown_fields_are_preserved(repository):
    payload = repository.insert(sample_payload(futureField={'nested': 42}))
    assert 'futureField' in payload
    stored = repository.get_payload('model-1')
    assert stored['futureField'] == {'nested': 42}

    record = repository.get('model-1')
    repository.save(ModelRecord(**{**record.__dict__, 'notes': 'edit'}),
                    expected_revision=1)
    stored = repository.get_payload('model-1')
    assert stored['futureField'] == {'nested': 42}
    assert stored['notes'] == 'edit'


def test_appearance_is_detached_from_the_caller(repository):
    from prism.model_library.contracts import ModelAppearance
    properties = {'flipX': True}
    payload = repository.insert(sample_payload())
    payload['appearance'] = {'revision': 1, 'bindings': {}, 'properties': properties}
    repository.update(payload, expected_revision=1)
    properties['flipX'] = False
    stored = repository.get('model-1')
    assert stored.appearance.properties['flipX'] is True
    assert stored.appearance.revision == 1


def test_records_of_another_project_are_refused(repository):
    payload = sample_payload(project_id='some-other-project')
    with pytest.raises(ValueError):
        repository.insert(payload)


# ---------------------------------------------------------------------------
# A2: the full save chain
# ---------------------------------------------------------------------------


def test_full_save_and_reopen_keeps_the_models(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    context = open_model_context(path)
    repository = ModelRepositoryAdapter(context)
    payload = repository.insert(sample_payload())

    # The scene is the source of truth for a full save, exactly like the
    # material library.
    scene.model_records = [payload]
    scene.project_id = context.project_id
    create_project(tmp_path / 'models2.prism', scene)  # keep the first file
    from prism.fileio.sql import SQLiteIO
    io = SQLiteIO(str(path), scene)
    try:
        io.write()
    finally:
        io._close_connection()

    reopened = make_scene()
    open_project(path, reopened)
    assert len(reopened.model_records) == 1
    assert reopened.model_records[0]['name'] == 'Chair'
    assert reopened.model_records[0]['files'][0]['attachment_id'] == 1


def test_save_keeps_the_originals_of_the_models(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    context = open_model_context(path)
    repository = ModelRepositoryAdapter(context)
    payload = repository.insert(sample_payload())

    # A real original in the project's attachment table.
    resources = ModelResourceStore(context)
    original = tmp_path / 'chair.fbx'
    original.write_bytes(b'not really an fbx')
    stored = resources.put_batch([(original, 'chair.fbx')])
    payload['files'][0]['attachment_id'] = stored[0].attachment_id
    payload['files'][0]['source_ref'] = stored[0].source_ref
    repository.update(payload, expected_revision=1)

    scene.model_records = [repository.get_payload('model-1')]
    scene.project_id = context.project_id
    from prism.fileio.sql import SQLiteIO
    io = SQLiteIO(str(path), scene)
    try:
        io.write()
    finally:
        io._close_connection()
    reopened = make_scene()
    open_project(path, reopened)
    assert len(reopened.model_records) == 1
    with ModelResourceStore(open_model_context(path)).open(stored[0].attachment_id) as reader:
        assert reader.read() == b'not really an fbx'


# ---------------------------------------------------------------------------
# A2: the partial save chain
# ---------------------------------------------------------------------------


def test_partial_domain_keeps_unloaded_models(tmp_path, qapp):
    from prism.fileio.partial_save import capture_partial_save, write_partial_snapshot
    from prism.partial_domains import DomainTracker, read_domain_baseline

    path = tmp_path / 'partial.prism'
    scene = make_scene()
    create_project(path, scene)
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload('model-1', 'Chair'))
    repository.insert(sample_payload('model-2', 'Table'))

    tracker = DomainTracker(read_domain_baseline(str(path)))
    models = [repository.get_payload('model-1'), repository.get_payload('model-2')]
    models[0]['notes'] = 'edited'
    diff = tracker.capture([], [], models)
    snapshot = capture_partial_save(str(path), diff.patches, inserts=diff.inserts,
                                    baseline=diff.baseline)
    write_partial_snapshot(snapshot)
    tracker.acknowledge(snapshot)

    assert repository.get('model-1').notes == 'edited'
    assert repository.get('model-2').name == 'Table'


def test_partial_domain_refuses_a_silent_delete(tmp_path, qapp):
    from prism.partial_domains import DomainTracker, read_domain_baseline
    from prism.fileio.partial_save import PartialSaveConflict

    path = tmp_path / 'partial.prism'
    scene = make_scene()
    create_project(path, scene)
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload('model-1', 'Chair'))

    tracker = DomainTracker(read_domain_baseline(str(path)))
    with pytest.raises(PartialSaveConflict):
        tracker.capture([], [], [])


def test_partial_domain_preserves_unknown_model_fields(tmp_path, qapp):
    from prism.fileio.partial_save import capture_partial_save, write_partial_snapshot
    from prism.partial_domains import DomainTracker, read_domain_baseline

    path = tmp_path / 'partial.prism'
    scene = make_scene()
    create_project(path, scene)
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload(futureField={'nested': 42}))

    tracker = DomainTracker(read_domain_baseline(str(path)))
    models = [repository.get_payload('model-1')]
    models[0]['notes'] = 'edited'
    diff = tracker.capture([], [], models)
    snapshot = capture_partial_save(str(path), diff.patches, inserts=diff.inserts,
                                    baseline=diff.baseline)
    write_partial_snapshot(snapshot)
    tracker.acknowledge(snapshot)

    stored = repository.get_payload('model-1')
    assert stored['notes'] == 'edited'
    assert stored['futureField'] == {'nested': 42}


# ---------------------------------------------------------------------------
# A1: the large file channel
# ---------------------------------------------------------------------------


def test_large_original_streams_without_loading_it_whole(tmp_path, qapp):
    """A 64 MiB original must not be resident in memory while it is copied."""
    import hashlib
    import os
    import tracemalloc

    path = tmp_path / 'project.prism'
    scene = make_scene()
    create_project(path, scene)
    context = open_model_context(str(path))
    resources = ModelResourceStore(context)

    source = tmp_path / 'big.bin'
    size = 64 * 1024 * 1024
    digest = hashlib.sha256()
    with source.open('wb') as handle:
        block = os.urandom(1024 * 1024)
        for _ in range(64):
            handle.write(block)
            digest.update(block)

    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    stored = resources.put_batch([(source, 'big.bin')])
    peak = tracemalloc.get_traced_memory()[1] - before
    tracemalloc.stop()

    assert stored[0].size_bytes == size
    assert stored[0].sha256 == digest.hexdigest()
    # The copy must stay far below the size of the file itself.
    assert peak < 16 * 1024 * 1024, f'peak resident memory {peak}'

    with resources.open(stored[0].attachment_id) as reader:
        assert reader.verify() == digest.hexdigest()


def test_missing_original_is_reported(repository, context):
    resources = ModelResourceStore(context)
    with pytest.raises(ModelStorageError):
        resources.read_all(4242)


# ---------------------------------------------------------------------------
# A2: delete and restore, project move, save-as
# ---------------------------------------------------------------------------


def test_retire_hides_a_model_and_restore_brings_it_back(tmp_path, qapp):
    path = tmp_path / 'retire.prism'
    create_project(path, make_scene())
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload('model-1', 'Chair'))
    resources = ModelResourceStore(context)
    original = tmp_path / 'chair.fbx'
    original.write_bytes(b'the original bytes')
    stored = resources.put_batch([(original, 'chair.fbx')])[0]
    payload = repository.get_payload('model-1')
    payload['files'][0]['attachment_id'] = stored.attachment_id
    payload['files'][0]['sha256'] = stored.sha256
    repository.update(payload, expected_revision=1)

    repository.retire('model-1', expected_revision=2)
    assert repository.query(ModelQuery()).total == 0
    assert repository.count() == 0
    assert [item['model_id'] for item in repository.retired()] == ['model-1']
    # Retiring is metadata only: the original is still in the project.
    with resources.open(stored.attachment_id) as reader:
        assert reader.read() == b'the original bytes'

    repository.restore('model-1', expected_revision=3)
    assert repository.query(ModelQuery()).total == 1
    assert repository.retired() == ()
    assert repository.get('model-1').files[0].relative_path == 'models/chair.fbx'
    with resources.open(stored.attachment_id) as reader:
        assert reader.read() == b'the original bytes'


def test_retire_refuses_a_stale_revision(repository):
    repository.insert(sample_payload())
    repository.retire('model-1', expected_revision=1)
    with pytest.raises(ModelRevisionConflict):
        repository.retire('model-1', expected_revision=1)
    assert repository.query(ModelQuery()).total == 0


def test_editing_a_note_does_not_rewrite_the_originals(tmp_path, qapp):
    """A metadata edit must not copy the large originals again."""
    import os

    path = tmp_path / 'edits.prism'
    create_project(path, make_scene())
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload())
    resources = ModelResourceStore(context)
    original = tmp_path / 'big.obj'
    original.write_bytes(os.urandom(4 * 1024 * 1024))
    stored = resources.put_batch([(original, 'big.obj')])[0]
    payload = repository.get_payload('model-1')
    payload['files'][0]['attachment_id'] = stored.attachment_id
    payload['files'][0]['size_bytes'] = stored.size_bytes
    payload['files'][0]['sha256'] = stored.sha256
    repository.update(payload, expected_revision=1)

    def attachment_state():
        with sqlite3.connect(str(path)) as db:
            return db.execute('SELECT id, name, length(blob) FROM attachments '
                              'ORDER BY id').fetchall()

    def blob_hash():
        with ModelResourceStore(context).open(stored.attachment_id) as reader:
            return reader.verify()

    before_rows = attachment_state()
    before_hash = blob_hash()
    size_before = path.stat().st_size

    for notes in ('first note', 'second note', 'third note'):
        current = repository.get('model-1')
        repository.save(ModelRecord(**{**current.__dict__, 'notes': notes}),
                        expected_revision=current.record_revision)
    from prism.model_library.contracts import ModelAppearance
    current = repository.get('model-1')
    repository.save(ModelRecord(**{**current.__dict__,
                                   'appearance': ModelAppearance(
                                       revision=1, bindings={},
                                       properties={'flipX': True})}),
                    expected_revision=current.record_revision)

    assert attachment_state() == before_rows
    assert blob_hash() == before_hash
    # Only the metadata table may have grown; the four MiB original is never
    # copied into a second row.
    assert path.stat().st_size - size_before < 512 * 1024


def test_a_moved_project_keeps_its_models_and_originals(tmp_path, qapp):
    import shutil

    path = tmp_path / 'first' / 'models.prism'
    path.parent.mkdir()
    create_project(path, make_scene())
    context = open_model_context(str(path))
    repository = ModelRepositoryAdapter(context)
    repository.insert(sample_payload())
    resources = ModelResourceStore(context)
    original = tmp_path / 'chair.fbx'
    original.write_bytes(b'moved project original')
    stored = resources.put_batch([(original, 'chair.fbx')])[0]
    payload = repository.get_payload('model-1')
    payload['files'][0]['attachment_id'] = stored.attachment_id
    payload['files'][0]['sha256'] = stored.sha256
    repository.update(payload, expected_revision=1)

    moved = tmp_path / 'second' / 'renamed.prism'
    moved.parent.mkdir()
    shutil.move(str(path), str(moved))

    moved_context = open_model_context(str(moved), open_epoch=9)
    moved_repository = ModelRepositoryAdapter(moved_context)
    assert moved_repository.query(ModelQuery()).total == 1
    moved_record = moved_repository.get('model-1')
    attachment_id = moved_record.files[0].source_ref.split(':')[1]
    with ModelResourceStore(moved_context).open(int(attachment_id)) as reader:
        assert reader.read() == b'moved project original'


def test_save_as_writes_a_new_project_with_the_model_domain(tmp_path, qapp):
    """Save As must carry the records and their originals, not just the rows."""
    from prism.fileio.sql import SQLiteIO

    source = tmp_path / 'original.prism'
    scene = make_scene()
    create_project(source, scene)
    context = open_model_context(str(source))
    repository = ModelRepositoryAdapter(context)
    payload = repository.insert(sample_payload())
    resources = ModelResourceStore(context)
    original = tmp_path / 'chair.fbx'
    original.write_bytes(b'save-as original')
    stored = resources.put_batch([(original, 'chair.fbx')])[0]
    payload['files'][0]['attachment_id'] = stored.attachment_id
    payload['files'][0]['sha256'] = stored.sha256
    repository.update(payload, expected_revision=1)

    from prism.model_library.project_storage import register_model_attachments
    # Save As happens on an open project: reopen it so the scene carries the
    # real attachment store, exactly like the application does.
    open_project(source, scene)
    scene.project_id = context.project_id
    scene.model_records = list(repository.list_payloads())
    register_model_attachments(scene, scene.model_records[0])
    assert set(scene.attachments) == {stored.attachment_id}

    target = tmp_path / 'copy.prism'
    io = SQLiteIO(str(target), scene, create_new=True)
    try:
        io.write()
    finally:
        io._close_connection()

    copied_context = open_model_context(str(target))
    copied_repository = ModelRepositoryAdapter(copied_context)
    assert copied_repository.query(ModelQuery()).total == 1
    copied = copied_repository.get_payload('model-1')
    copied_attachment = copied['files'][0]['attachment_id']
    with ModelResourceStore(copied_context).open(int(copied_attachment)) as reader:
        assert reader.read() == b'save-as original'
    # The copy is a different *storage instance*: one logical project may live
    # in several files, and the model domain refuses to mix their rows.
    assert copied_context.storage_instance_id != context.storage_instance_id
    from prism.model_library.project_storage import require_same_project
    with pytest.raises(ModelStorageError):
        require_same_project(copied_context, context)
    with ModelResourceStore(context).open(stored.attachment_id) as reader:
        assert reader.read() == b'save-as original'


# ---------------------------------------------------------------------------
# A2: the model domain does not disturb the data that was already there
# ---------------------------------------------------------------------------


def _material_record(project_id, record_id='material-1'):
    """The smallest record the material library accepts."""
    return {
        'id': record_id, 'projectId': project_id, 'schemaVersion': 1,
        'name': 'Wood', 'notes': 'kept as it was', 'folderId': None,
        'tags': ['wood'], 'textures': {}, 'preview': {}, 'nativePackages': [],
        'status': 'draft', 'createdAt': '2026-10-26T00:00:00',
        'updatedAt': '2026-10-26T00:00:00', 'deletedAt': None,
    }


def test_existing_material_records_survive_a_model_import(tmp_path, qapp):
    from prism.fileio.sql import SQLiteIO
    from prism.model_library.importer import ModelImporter

    path = tmp_path / 'mixed.prism'
    scene = make_scene()
    create_project(path, scene)
    context = open_model_context(str(path))
    material = _material_record(context.project_id)
    scene.project_id = context.project_id
    scene.material_records = [material]
    io = SQLiteIO(str(path), scene)
    try:
        io.write()
    finally:
        io._close_connection()

    repository = ModelRepositoryAdapter(context)
    importer = ModelImporter(context, repository)
    original = tmp_path / 'simple.obj'
    original.write_text('v 0 0 0\n', encoding='utf-8')
    job_id = importer.import_paths((str(original),))
    while importer.job_state(job_id) not in ('completed', 'failed', 'cancelled'):
        pass
    assert importer.job_state(job_id) == 'completed'

    reopened = make_scene()
    open_project(path, reopened)
    assert reopened.material_records == [material]
    assert len(reopened.model_records) == 1
    assert reopened.model_records[0]['format'] == 'obj'
