"""The importer collects exactly what a model declares, and nothing else.

The samples under ``acceptance/samples`` are third-party files; the
expectations in this module were written from the files themselves (a text
search of the declared references, or reading the JSON of a glTF) and never
from the product's own import output.
"""
import hashlib
import json
import shutil
import sqlite3
import struct
import tempfile
import time
from pathlib import Path

import pytest

from prism.model_library.contracts import ModelQuery, ModelFileRef
from prism.model_library.importer import (
    ROLE_BUFFER,
    ROLE_IMAGE,
    ROLE_MAIN,
    ROLE_MATERIAL,
    ROLE_TEXTURE,
    ModelImporter,
    detect_references,
    plan_package,
)
from prism.model_library.project_storage import (
    ModelProjectContext,
    ModelResourceStore,
    ModelStorageError,
    validate_model_records,
    open_model_context,
)
from prism.model_library.repository import ModelRepositoryAdapter

SAMPLES = Path(__file__).resolve().parent / 'acceptance' / 'samples'


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


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def attachment_rows(context):
    with sqlite3.connect(context.metadata_path) as db:
        return db.execute('SELECT COUNT(*) FROM attachments').fetchone()[0]


def wait_for_job(controller, job_id, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = controller.job_state(job_id)
        if state in ('completed', 'failed', 'cancelled'):
            return state
        time.sleep(0.01)
    raise AssertionError(f'import job {job_id} did not finish in {timeout}s')


def import_one(importer, path):
    """Import one file and return (state, events of that job)."""
    events = []
    importer._publish = events.append
    job_id = importer.import_paths((str(path),))
    state = wait_for_job(importer, job_id)
    return state, job_id, events


def completed_result(events):
    from prism.model_library.project_storage import ModelImportCompletedEvent
    for event in events:
        if isinstance(event, ModelImportCompletedEvent):
            return event.result
    return None


def stored_files(repository, model_id):
    payload = repository.get_payload(model_id)
    return {item['relative_path']: item for item in payload['files']
            if not item.get('missing')}


@pytest.fixture
def project(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    return path, scene


@pytest.fixture
def context(project):
    path, _scene = project
    return open_model_context(path, open_epoch=3)


@pytest.fixture
def repository(context):
    return ModelRepositoryAdapter(context)


@pytest.fixture
def importer(context, repository):
    return ModelImporter(context, repository)


# ---------------------------------------------------------------------------
# A3: the four formats, each on a real sample
# ---------------------------------------------------------------------------


def test_obj_collects_the_material_library_and_its_textures(
        importer, repository):
    source = SAMPLES / 'assimp_spider' / 'spider.obj'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    assert result is not None

    model_id = result.model_id
    files = stored_files(repository, model_id)
    # The OBJ declares spider.mtl; the MTL declares five textures, of which
    # only SpiderTex.jpg is in that directory.  Nothing else in the directory
    # belongs to the model.
    assert set(files) == {'spider.obj', 'spider.mtl', 'SpiderTex.jpg'}
    roles = {path: item['role'] for path, item in files.items()}
    assert roles == {'spider.obj': ROLE_MAIN, 'spider.mtl': ROLE_MATERIAL,
                     'SpiderTex.jpg': ROLE_TEXTURE}
    assert sorted(result.missing) == [
        '.\\drkwood2.jpg', '.\\engineflare1.jpg', '.\\wal67ar_small.jpg',
        '.\\wal69ar_small.jpg']
    for relative, item in files.items():
        assert item['sha256'] == sha256_file(source.parent / relative)
        assert item['size_bytes'] == (source.parent / relative).stat().st_size
        assert not item['missing']
        assert item['attachment_id'] is not None
        with ModelResourceStore(repository.context).open(
                int(item['attachment_id'])) as reader:
            assert reader.verify() == item['sha256']

    record = repository.get(model_id)
    assert record.main_file_id == files['spider.obj']['file_id']
    assert record.format == 'obj'
    page = repository.query(ModelQuery())
    assert page.total == 1
    assert page.items[0].model_id == model_id
    assert page.items[0].size_bytes == sum(
        item['size_bytes'] for item in files.values())


def test_gltf_collects_the_external_buffer_and_image(importer, repository):
    source = SAMPLES / 'khronos_boxtextured_gltf' / 'BoxTextured.gltf'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    files = stored_files(repository, completed_result(events).model_id)
    # LICENSE.md sits next to the model but the document never names it.
    assert set(files) == {'BoxTextured.gltf', 'BoxTextured0.bin',
                          'CesiumLogoFlat.png'}
    assert files['BoxTextured0.bin']['role'] == ROLE_BUFFER
    assert files['CesiumLogoFlat.png']['role'] == ROLE_IMAGE
    for relative, item in files.items():
        assert item['sha256'] == sha256_file(source.parent / relative)


def test_glb_keeps_its_own_bytes_and_adds_no_dependency(importer, repository):
    source = SAMPLES / 'khronos_box_glb' / 'Box.glb'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    payload = repository.get_payload(result.model_id)
    assert [item['relative_path'] for item in payload['files']] == ['Box.glb']
    assert not result.missing
    stored = payload['files'][0]
    assert stored['sha256'] == sha256_file(source)
    with ModelResourceStore(repository.context).open(
            int(stored['attachment_id'])) as reader:
        assert reader.read() == source.read_bytes()


def test_glb_with_an_external_buffer_is_not_assumed_self_contained(
        tmp_path, importer, repository):
    source = tmp_path / 'external.glb'
    document = {'asset': {'version': '2.0'},
                'buffers': [{'uri': 'external.bin', 'byteLength': 4}],
                'meshes': []}
    json_chunk = json.dumps(document).encode('utf-8')
    json_chunk += b' ' * (-len(json_chunk) % 4)
    body = struct.pack('<II', len(json_chunk), 0x4E4F534A) + json_chunk
    source.write_bytes(struct.pack('<4sII', b'glTF', 2, 12 + len(body)) + body)

    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    payload = repository.get_payload(result.model_id)
    present = [item for item in payload['files'] if not item.get('missing')]
    missing = [item for item in payload['files'] if item.get('missing')]
    assert [item['relative_path'] for item in present] == ['external.glb']
    assert [item['original_reference'] for item in missing] == ['external.bin']


def test_fbx_without_texture_objects_gets_no_invented_texture(
        importer, repository):
    source = SAMPLES / 'assimp_box' / 'box.fbx'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    payload = repository.get_payload(result.model_id)
    # box.fbx declares no Texture/Video object at all.  No .jpg next to it is
    # collected either -- the file names are never guessed at.
    assert [item['relative_path'] for item in payload['files']] == ['box.fbx']
    assert not result.missing


def test_fbx_reports_declared_textures_that_are_not_there(
        importer, repository):
    source = (SAMPLES / 'polyhaven_armchair_01'
              / 'fbx_1k_ArmChair_01_ArmChair_01_1k.fbx')
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    assert sorted(result.missing) == [
        'textures\\Armchair_01_diff_1k.jpg',
        'textures\\Armchair_01_metallic_1k.exr',
        'textures\\Armchair_01_nor_gl_1k.exr',
        'textures\\Armchair_01_roughness_1k.jpg',
    ]
    payload = repository.get_payload(result.model_id)
    present = [item for item in payload['files'] if not item.get('missing')]
    missing = [item for item in payload['files'] if item.get('missing')]
    # Only the model itself was available: an incomplete asset, honestly
    # labelled, instead of a fake complete one.
    assert [item['relative_path'] for item in present] == [source.name]
    assert len(missing) == 4
    assert all(item['missing'] for item in missing)
    assert all(item['sha256'] == '' for item in missing)


def _fbx_string(value):
    data = value.encode('utf-8')
    return b'S' + struct.pack('<I', len(data)) + data


def _fbx_node(name, properties, children, offset):
    """One binary FBX record; returns (bytes, end offset of the record)."""
    name_bytes = name.encode('utf-8')
    property_bytes = b''.join(properties)
    cursor = offset + 13 + len(name_bytes) + len(property_bytes)
    child_bytes = b''
    for child in children:
        built, cursor = _fbx_node(child[0], child[1], child[2], cursor)
        child_bytes += built
    end = cursor + 13
    header = struct.pack('<III', end, len(properties), len(property_bytes))
    header += bytes([len(name_bytes)])
    return header + name_bytes + property_bytes + child_bytes + b'\x00' * 13, end


def write_binary_fbx(path, objects):
    """A minimal FBX 7.4 container with the ``Objects`` records given.

    The samples under ``acceptance/samples`` belong to another work line and
    change while this one is in progress; an FBX whose exact content is known
    is the honest way to test what the reader does with absolute paths and
    embedded content.
    """
    header = b'Kaydara FBX Binary  \x00\x1a\x00' + struct.pack('<I', 7400)
    body = b''
    cursor = len(header)
    for node in objects:
        built, cursor = _fbx_node(node[0], node[1], node[2], cursor)
        body += built
    body += b'\x00' * 13
    path.write_bytes(header + body)


def texture_object(reference):
    return ('Texture', [_fbx_int(1), _fbx_string('tex\x00\x01Texture'),
                        _fbx_string('')],
            [('RelativeFilename', [_fbx_string(reference)], []),
             ('FileName', [_fbx_string(reference)], [])])


def _fbx_int(value):
    return b'L' + struct.pack('<q', value)


def test_a_stored_absolute_reference_is_recorded_as_absolute(
        tmp_path, importer, repository):
    source = tmp_path / 'absolute.fbx'
    write_binary_fbx(source, [
        ('Objects', [], [texture_object('D:\\projects\\textures\\paint.png')]),
    ])
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    payload = repository.get_payload(result.model_id)
    present = [item for item in payload['files'] if not item.get('missing')]
    missing = [item for item in payload['files'] if item.get('missing')]
    assert [item['relative_path'] for item in present] == ['absolute.fbx']
    assert [item['original_reference'] for item in missing] == [
        'D:\\projects\\textures\\paint.png']
    # Two statements of one file (relative and absolute property, Texture and
    # Media record) must not become two missing dependencies.
    assert len(missing) == 1
    assert missing[0]['absolute_reference'] is True


def test_an_embedded_fbx_texture_is_not_a_missing_file(tmp_path, importer,
                                                       repository):
    source = tmp_path / 'embedded.fbx'
    content = b'R' + struct.pack('<I', 8) + b'\x89PNGdata'
    write_binary_fbx(source, [
        ('Objects', [], [
            ('Media', [_fbx_int(2), _fbx_string('video'), _fbx_string('')],
             [('Content', [content], [])]),
        ]),
    ])
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    payload = repository.get_payload(result.model_id)
    assert [item['relative_path'] for item in payload['files']
            if not item.get('missing')] == ['embedded.fbx']
    # Embedded content is part of the model file, not a dependency to collect.
    assert result.missing == ()
    assert all(item['role'] != 'texture' or item['missing']
               for item in payload['files'])


# ---------------------------------------------------------------------------
# A3: paths, same names, missing files
# ---------------------------------------------------------------------------


def test_gltf_in_a_chinese_directory_keeps_the_relative_layout(
        importer, repository):
    folder = SAMPLES / 'derived_path_chinese_space' / '模型 库 示例'
    source = folder / 'BoxTextured.gltf'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    files = stored_files(repository, completed_result(events).model_id)
    assert set(files) == {'BoxTextured.gltf', 'BoxTextured0.bin',
                          'CesiumLogoFlat.png'}
    for relative, item in files.items():
        assert item['sha256'] == sha256_file(folder / relative)


def test_material_library_in_a_subdirectory_keeps_its_path(
        importer, repository):
    source = SAMPLES / 'assimp_mtl_other_dir' / 'mtl_different_folder.obj'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    files = stored_files(repository, completed_result(events).model_id)
    assert set(files) == {'mtl_different_folder.obj',
                          'folder/mtl_different_folder.mtl'}
    assert files['folder/mtl_different_folder.mtl']['role'] == ROLE_MATERIAL


def test_same_texture_name_in_two_directories_is_kept_twice(
        importer, repository):
    folder = SAMPLES / 'derived_same_name_texture_two_dirs'
    source = folder / 'model.obj'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    files = stored_files(repository, completed_result(events).model_id)
    assert set(files) == {'model.obj', 'model.mtl',
                          'textures/a/diffuse.png', 'textures/b/diffuse.png'}
    first = files['textures/a/diffuse.png']
    second = files['textures/b/diffuse.png']
    assert first['sha256'] != second['sha256']
    assert first['sha256'] == sha256_file(folder / 'textures/a/diffuse.png')
    assert second['sha256'] == sha256_file(folder / 'textures/b/diffuse.png')
    # The third texture in that directory is not referenced by the material
    # library, so it is not part of the model.
    assert 'textures/c/diffuse.png' not in files


def test_missing_texture_is_reported_without_blocking_the_import(
        importer, repository):
    source = SAMPLES / 'derived_missing_dependency' / 'spider.obj'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)
    # That directory keeps the material library but no texture at all.
    assert sorted(result.missing) == [
        '.\\SpiderTex.jpg', '.\\drkwood2.jpg', '.\\engineflare1.jpg',
        '.\\wal67ar_small.jpg', '.\\wal69ar_small.jpg']
    files = stored_files(repository, result.model_id)
    assert set(files) == {'spider.obj', 'spider.mtl'}
    # The model is stored and readable even though its textures are absent.
    assert repository.get(result.model_id) is not None


def test_a_dependency_outside_the_model_directory_stays_inside_the_package(
        tmp_path, importer, repository):
    root = tmp_path / 'kit'
    (root / 'models').mkdir(parents=True)
    (root / 'textures').mkdir()
    (root / 'textures' / 'shared.png').write_bytes(b'\x89PNG shared')
    source = root / 'models' / 'part.obj'
    source.write_text('mtllib part.mtl\nv 0 0 0\n', encoding='utf-8')
    (root / 'models' / 'part.mtl').write_text(
        'newmtl a\nmap_Kd ../textures/shared.png\n', encoding='utf-8')

    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    payload = repository.get_payload(completed_result(events).model_id)
    paths = {item['relative_path'] for item in payload['files']}
    assert paths == {'models/part.obj', 'models/part.mtl',
                     'textures/shared.png'}
    for path in paths:
        assert '..' not in Path(path).parts
        assert not Path(path).is_absolute()
    # The layout keeps the relation the model declares, so ``../textures``
    # still resolves inside the project package.
    validate_model_records([payload], repository.context.project_id)


def test_two_imports_of_one_file_are_two_entries(importer, repository):
    source = SAMPLES / 'derived_same_name_texture_two_dirs' / 'model.obj'
    _state, _job, first_events = import_one(importer, source)
    _state, _job, second_events = import_one(importer, source)
    first = completed_result(first_events).model_id
    second = completed_result(second_events).model_id
    assert first != second
    assert repository.query(ModelQuery()).total == 2
    assert repository.get(first).files != repository.get(second).files


# ---------------------------------------------------------------------------
# A3: what must not be committed
# ---------------------------------------------------------------------------


def test_a_corrupt_document_fails_and_writes_nothing(
        tmp_path, repository, importer):
    source = tmp_path / 'broken.gltf'
    source.write_text('{"asset": [}', encoding='utf-8')
    state, job_id, events = import_one(importer, source)
    assert state == 'failed'
    from prism.model_library.project_storage import ModelImportFailedEvent
    failures = [event for event in events
                if isinstance(event, ModelImportFailedEvent)]
    assert failures and failures[0].job_id == job_id
    assert failures[0].failure.error_code == 'import_failed'
    assert repository.count() == 0
    assert attachment_rows(repository.context) == 0


def test_an_unknown_extension_is_refused_before_a_job_starts(
        tmp_path, importer, repository):
    source = tmp_path / 'notes.txt'
    source.write_text('not a model', encoding='utf-8')
    with pytest.raises(ValueError):
        importer.import_paths((str(source),))
    with pytest.raises(ValueError):
        importer.import_paths(())
    assert repository.count() == 0


def test_cancel_before_commit_leaves_no_ready_asset(tmp_path, monkeypatch,
                                                    importer, repository):
    folder = SAMPLES / 'khronos_avocado_gltf'
    source = folder / 'Avocado.gltf'
    real_copy = importer._copy_to_stage
    calls = {'count': 0}
    job = {}

    def slow_copy(src, dst, cancel):
        calls['count'] += 1
        if calls['count'] == 2:
            # Cancel while the second file is being staged: the job must not
            # publish a half-collected model.
            cancel.set()
        return real_copy(src, dst, cancel)

    monkeypatch.setattr(importer, '_copy_to_stage', slow_copy)
    events = []
    importer._publish = events.append
    job['id'] = importer.import_paths((str(source),))
    state = wait_for_job(importer, job['id'])
    assert state == 'cancelled'
    assert repository.count() == 0
    assert attachment_rows(repository.context) == 0


def test_a_source_that_changes_while_it_is_copied_is_not_stored(
        tmp_path, monkeypatch, importer, repository):
    source = tmp_path / 'moving.obj'
    source.write_text('v 0 0 0\n', encoding='utf-8')
    real_copy = importer._copy_to_stage

    def changing_copy(src, dst, cancel):
        result = real_copy(src, dst, cancel)
        Path(src).write_bytes(Path(src).read_bytes() + b'v 1 1 1\n')
        return result

    monkeypatch.setattr(importer, '_copy_to_stage', changing_copy)
    state, _job, events = import_one(importer, source)
    assert state == 'failed'
    assert repository.count() == 0
    assert attachment_rows(repository.context) == 0


def test_a_finished_import_leaves_no_staging_directory(importer, repository):
    pattern = 'prism-model-import-*'
    before = {path.name for path in Path(tempfile.gettempdir()).glob(pattern)}
    source = SAMPLES / 'assimp_mtl_other_dir' / 'mtl_different_folder.obj'
    state, _job, _events = import_one(importer, source)
    assert state == 'completed'
    after = {path.name for path in Path(tempfile.gettempdir()).glob(pattern)}
    assert after == before


def test_cancel_and_close_release_every_job_of_the_session(
        importer, context):
    source = SAMPLES / 'khronos_box_glb' / 'Box.glb'
    job_id = importer.import_paths((str(source),))
    importer.close_project(context.project_session)
    assert importer.job_state(job_id) in ('completed', 'cancelled')
    # A second project session must not be affected by that call.
    importer.close_project('another-session')


# ---------------------------------------------------------------------------
# A3: the job really is asynchronous, and its events carry the outcome
# ---------------------------------------------------------------------------


def test_the_job_id_is_an_acceptance_not_a_completion(importer, repository):
    source = SAMPLES / 'khronos_box_glb' / 'Box.glb'
    job_id = importer.import_paths((str(source),))
    assert isinstance(job_id, str) and job_id
    assert importer.job_state(job_id) in ('queued', 'running', 'completed')
    assert wait_for_job(importer, job_id) == 'completed'
    assert repository.count() == 1


def test_completed_event_describes_the_committed_record(importer, context):
    source = SAMPLES / 'assimp_spider' / 'spider.obj'
    state, job_id, events = import_one(importer, source)
    assert state == 'completed'
    from prism.model_library.project_storage import (
        ModelImportCompletedEvent, ModelJobPhaseEvent)
    phases = [event for event in events if isinstance(event, ModelJobPhaseEvent)]
    assert phases and phases[0].job_kind == 'import'
    assert phases[0].total == 1
    result = completed_result(events)
    assert result.payload['project_id'] == context.project_id
    assert result.payload['source_revision'] == 1
    # Three files are stored, four declared textures are missing.
    assert len(result.files) == 7
    assert len(result.attachment_ids) == 3
    assert result.format == 'obj'
    assert result.name == 'spider.obj'


def test_the_published_originals_are_registered_for_the_save_chain(
        importer, repository, project):
    """A save must keep the model originals instead of dropping them."""
    path, scene = project
    source = SAMPLES / 'assimp_spider' / 'spider.obj'
    state, _job, events = import_one(importer, source)
    assert state == 'completed'
    result = completed_result(events)

    # Reopen the project so the scene carries the real attachment store the
    # save chain uses (metadata resident, blobs read on demand).
    from prism.fileio.sql import SQLiteIO
    io = SQLiteIO(str(path), scene, readonly=True)
    try:
        io.read()
    finally:
        io._close_connection()

    from prism.model_library.project_storage import register_model_attachments
    for payload in repository.list_payloads():
        register_model_attachments(scene, payload)
    assert set(scene.attachments) == set(result.attachment_ids)
