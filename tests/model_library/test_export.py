"""Exporting originals copies the exact bytes, and says so honestly.

Every check here is made with an independent verifier: the expected file set,
sizes and SHA-256 values are computed from the sample files on disk, never
from the export's own report.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import time
from pathlib import Path

import pytest

from prism.model_library.exporter import (
    REFERENCE_DIRECTORY,
    STAGING_PREFIX,
    ModelOriginalExporter,
)
from prism.model_library.importer import ModelImporter
from prism.model_library.project_storage import (
    ModelProjectContext,
    ModelStorageError,
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


def wait_for_job(controller, job_id, timeout=120.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = controller.job_state(job_id)
        if state in ('completed', 'failed', 'cancelled'):
            return state
        time.sleep(0.01)
    raise AssertionError(f'job {job_id} did not finish in {timeout}s')


def export_result(events):
    from prism.model_library.project_storage import ModelExportCompletedEvent
    for event in events:
        if isinstance(event, ModelExportCompletedEvent):
            return event.result
    return None


def import_model(importer, path):
    events = []
    importer._publish = events.append
    job_id = importer.import_paths((str(path),))
    assert wait_for_job(importer, job_id) == 'completed'
    from prism.model_library.project_storage import ModelImportCompletedEvent
    for event in events:
        if isinstance(event, ModelImportCompletedEvent):
            return event.result.model_id
    raise AssertionError('the import published no result')


def assert_directory_matches_source(folder, source_directory, relatives):
    """The independent verifier: set, paths, sizes and hashes."""
    written = {}
    for path in sorted(Path(folder).rglob('*')):
        if path.is_file():
            written[path.relative_to(folder).as_posix()] = path
    assert set(written) == set(relatives)
    for relative, path in written.items():
        source = Path(source_directory) / relative
        assert path.stat().st_size == source.stat().st_size, relative
        assert sha256_file(path) == sha256_file(source), relative
    return written


def staging_directories(destination):
    return [path for path in Path(destination).glob(STAGING_PREFIX + '*')]


@pytest.fixture
def project(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    return path, scene


@pytest.fixture
def context(project):
    path, _scene = project
    return open_model_context(path, open_epoch=5)


@pytest.fixture
def repository(context):
    return ModelRepositoryAdapter(context)


@pytest.fixture
def importer(context, repository):
    return ModelImporter(context, repository)


@pytest.fixture
def exporter(context, repository):
    return ModelOriginalExporter(context, repository)


# ---------------------------------------------------------------------------
# A5: single and batch export
# ---------------------------------------------------------------------------


def test_single_export_copies_the_originals_byte_for_byte(
        importer, exporter, tmp_path):
    folder = SAMPLES / 'assimp_spider'
    model_id = import_model(importer, folder / 'spider.obj')
    destination = tmp_path / 'out'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'completed'

    result = export_result(events)
    assert result.state == 'completed'
    assert len(result.items) == 1
    item = result.items[0]
    assert item.state == 'completed'
    assert item.files == 3
    assert sorted(item.missing) == [
        '.\\drkwood2.jpg', '.\\engineflare1.jpg', '.\\wal67ar_small.jpg',
        '.\\wal69ar_small.jpg']
    written = assert_directory_matches_source(
        destination / 'spider.obj', folder,
        ['spider.obj', 'spider.mtl', 'SpiderTex.jpg'])
    assert sum(path.stat().st_size for path in written.values()) == item.bytes_written
    assert not staging_directories(destination)


@pytest.mark.parametrize('material_system', ['appearance', 'multimaterial'])
def test_export_includes_later_project_managed_textures(
        importer, exporter, repository, tmp_path, monkeypatch, material_system):
    model_id = import_model(importer, SAMPLES / 'assimp_spider' / 'spider.obj')
    texture = tmp_path / 'user texture.png'
    texture.write_bytes(b'project-managed texture original bytes')
    stored = repository.resources.put_file(texture)
    payload = repository.get_payload(model_id)
    if material_system == 'appearance':
        payload['appearance'] = dict(bindings={'basecolor': dict(
            attachmentId=stored.attachment_id, name=texture.name,
            sha256=stored.sha256, size=stored.size_bytes)})
    else:
        payload['multimaterial'] = dict(resources={'sha256:' + stored.sha256: dict(
            attachment_id=stored.attachment_id, locator=texture.name,
            sha256=stored.sha256, size=stored.size_bytes)})
    monkeypatch.setattr(repository, 'get_payload', lambda _: payload)
    events = []
    exporter._publish = events.append
    destination = tmp_path / 'with-textures'
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'completed'
    item = export_result(events).items[0]
    exported = Path(item.directory) / texture.name
    assert exported.read_bytes() == texture.read_bytes()
    assert (Path(item.directory) / 'spider.obj').read_bytes() == (
        SAMPLES / 'assimp_spider' / 'spider.obj').read_bytes()
    assert not staging_directories(destination)


def test_batch_export_puts_each_model_in_its_own_directory(
        importer, exporter, tmp_path):
    glb_folder = SAMPLES / 'khronos_box_glb'
    gltf_folder = SAMPLES / 'khronos_boxtextured_gltf'
    first = import_model(importer, glb_folder / 'Box.glb')
    second = import_model(importer, gltf_folder / 'BoxTextured.gltf')

    destination = tmp_path / 'batch'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((first, second), str(destination))
    assert wait_for_job(exporter, job_id) == 'completed'
    result = export_result(events)
    assert result.state == 'completed'
    assert {item.state for item in result.items} == {'completed'}

    assert_directory_matches_source(destination / 'Box.glb', glb_folder, ['Box.glb'])
    assert_directory_matches_source(
        destination / 'BoxTextured.gltf', gltf_folder,
        ['BoxTextured.gltf', 'BoxTextured0.bin', 'CesiumLogoFlat.png'])
    assert not staging_directories(destination)


def test_a_batch_with_an_unknown_model_is_not_reported_as_success(
        importer, exporter, tmp_path):
    folder = SAMPLES / 'khronos_box_glb'
    good = import_model(importer, folder / 'Box.glb')
    destination = tmp_path / 'mixed'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((good, 'no-such-model'), str(destination))
    assert wait_for_job(exporter, job_id) == 'failed'
    result = export_result(events)
    assert result.state == 'failed'
    states = {item.model_id: item.state for item in result.items}
    assert states == {good: 'completed', 'no-such-model': 'missing'}
    # The model that could be exported really is there.
    assert_directory_matches_source(destination / 'Box.glb', folder, ['Box.glb'])


def test_an_existing_directory_is_never_overwritten(importer, exporter, tmp_path):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    destination = tmp_path / 'occupied'
    existing = destination / 'Box.glb' / 'keep.txt'
    existing.parent.mkdir(parents=True)
    existing.write_text('the user already had this', encoding='utf-8')

    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'failed'
    item = export_result(events).items[0]
    assert item.state == 'failed'
    assert '已存在' in item.error
    assert existing.read_text(encoding='utf-8') == 'the user already had this'
    assert not (destination / 'Box.glb' / 'Box.glb').exists()


def test_missing_dependencies_are_reported_but_the_rest_is_exported(
        importer, exporter, tmp_path):
    folder = SAMPLES / 'derived_missing_dependency'
    model_id = import_model(importer, folder / 'spider.obj')
    destination = tmp_path / 'partial'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'completed'
    item = export_result(events).items[0]
    assert item.state == 'completed'
    assert len(item.missing) == 5
    assert_directory_matches_source(destination / 'spider.obj', folder,
                                    ['spider.obj', 'spider.mtl'])


# ---------------------------------------------------------------------------
# A5: absolute references get a path correspondence, not a rewritten model
# ---------------------------------------------------------------------------


def test_absolute_references_get_a_path_correspondence(
        tmp_path, project, context, repository, importer, exporter):
    texture_folder = tmp_path / 'external textures'
    texture_folder.mkdir()
    texture = texture_folder / 'paint.png'
    texture.write_bytes(b'\x89PNG absolute reference')
    model_folder = tmp_path / 'model'
    model_folder.mkdir()
    source = model_folder / 'prop.obj'
    source.write_text('mtllib prop.mtl\nv 0 0 0\n', encoding='utf-8')
    (model_folder / 'prop.mtl').write_text(
        f'newmtl a\nmap_Kd {texture}\n', encoding='utf-8')

    model_id = import_model(importer, source)
    payload = repository.get_payload(model_id)
    stored = {item['relative_path']: item for item in payload['files']}
    texture_entry = [item for item in payload['files']
                     if item['role'] == 'texture'][0]
    assert texture_entry['absolute_reference'] is True

    destination = tmp_path / 'out'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'completed'
    item = export_result(events).items[0]
    assert item.state == 'completed'

    mapping_file = destination / REFERENCE_DIRECTORY / f'{model_id}.json'
    mapping = json.loads(mapping_file.read_text(encoding='utf-8'))
    assert mapping['model_id'] == model_id
    assert mapping['references'] == [{
        'original_reference': str(texture),
        'exported_relative_path': texture_entry['relative_path'],
    }]
    # The bytes are untouched: the correspondence is the only repair offered.
    model_directory = Path(item.directory)
    exported_texture = model_directory / texture_entry['relative_path']
    assert sha256_file(exported_texture) == sha256_file(texture)
    material_entry = [entry for entry in payload['files']
                      if entry['role'] == 'material'][0]
    exported_mtl = model_directory / material_entry['relative_path']
    assert f'map_Kd {texture}' in exported_mtl.read_text(encoding='utf-8')
    assert stored[texture_entry['relative_path']]['sha256'] == sha256_file(texture)
    assert_directory_matches_source(
        model_directory, tmp_path,
        sorted(entry['relative_path'] for entry in payload['files']
               if not entry.get('missing')))


# ---------------------------------------------------------------------------
# A5: failure, cancellation and disk-full behaviour
# ---------------------------------------------------------------------------


def test_cancelling_stops_the_batch_and_cleans_only_its_own_files(
        importer, exporter, tmp_path, monkeypatch):
    first_folder = SAMPLES / 'khronos_box_glb'
    second_folder = SAMPLES / 'khronos_boxtextured_gltf'
    first = import_model(importer, first_folder / 'Box.glb')
    second = import_model(importer, second_folder / 'BoxTextured.gltf')

    destination = tmp_path / 'cancel'
    destination.mkdir()
    user_file = destination / 'my-notes.txt'
    user_file.write_text('not yours to delete', encoding='utf-8')

    real_export_one = exporter._export_one
    seen = []

    def cancel_after_the_first_model(job_id, payload, folder, cancel):
        result = real_export_one(job_id, payload, folder, cancel)
        seen.append(result)
        if len(seen) == 1:
            exporter.cancel(job_id)
        return result

    monkeypatch.setattr(exporter, '_export_one', cancel_after_the_first_model)
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((first, second), str(destination))
    assert wait_for_job(exporter, job_id) == 'cancelled'

    result = export_result(events)
    assert result.state == 'cancelled'
    assert seen[0].model_id == first and seen[0].state == 'completed'
    assert_directory_matches_source(destination / 'Box.glb', first_folder,
                                    ['Box.glb'])
    assert not (destination / 'BoxTextured.gltf').exists()
    assert not staging_directories(destination)
    assert user_file.read_text(encoding='utf-8') == 'not yours to delete'


def test_a_full_disk_is_reported_instead_of_claiming_success(
        importer, exporter, tmp_path, monkeypatch):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    destination = tmp_path / 'nospace'

    def no_space(source, target):
        raise OSError(28, 'No space left on device')

    monkeypatch.setattr('prism.model_library.exporter.os.rename', no_space)
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'failed'
    result = export_result(events)
    assert result.state == 'failed'
    assert result.items[0].state == 'failed'
    assert 'No space left on device' in result.items[0].error
    assert not (destination / 'Box.glb').exists()
    assert not staging_directories(destination)


def test_a_write_permission_failure_is_reported(
        importer, exporter, tmp_path, monkeypatch):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    destination = tmp_path / 'denied'

    real_open = Path.open

    def denied(self, mode='r', *args, **kwargs):
        if 'w' in mode or 'a' in mode:
            raise PermissionError(13, 'Permission denied')
        return real_open(self, mode, *args, **kwargs)

    events = []
    exporter._publish = events.append
    monkeypatch.setattr('pathlib.Path.open', denied)
    try:
        job_id = exporter.export_originals((model_id,), str(destination))
        state = wait_for_job(exporter, job_id)
    finally:
        monkeypatch.undo()
    assert state == 'failed'
    result = export_result(events)
    assert result.state == 'failed'
    assert 'Permission denied' in result.items[0].error
    assert not (destination / 'Box.glb').exists()
    assert not staging_directories(destination)


def test_a_destination_that_cannot_be_created_is_reported(
        importer, exporter, tmp_path):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    blocked = tmp_path / 'blocked'
    blocked.write_text('a file, not a folder', encoding='utf-8')

    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(blocked / 'out'))
    assert wait_for_job(exporter, job_id) == 'failed'
    result = export_result(events)
    assert result.state == 'failed'
    assert result.items[0].state == 'failed'
    assert result.items[0].error


def test_an_original_that_left_the_project_fails_its_model(
        importer, exporter, context, tmp_path):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    with sqlite3.connect(context.metadata_path) as db:
        db.execute('DELETE FROM attachments')
    destination = tmp_path / 'gone'
    events = []
    exporter._publish = events.append
    job_id = exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(exporter, job_id) == 'failed'
    result = export_result(events)
    assert result.state == 'failed'
    assert result.items[0].state == 'failed'
    assert not (destination / 'Box.glb').exists()
    assert not staging_directories(destination)


def test_an_empty_selection_is_refused(exporter, tmp_path):
    with pytest.raises(ValueError):
        exporter.export_originals((), str(tmp_path))
    blocked = tmp_path / 'a-file'
    blocked.write_text('x', encoding='utf-8')
    with pytest.raises(ValueError):
        exporter.export_originals(('model',), str(blocked))


# ---------------------------------------------------------------------------
# A5: the project may move or become a directory project
# ---------------------------------------------------------------------------


def test_export_works_after_the_project_was_moved(
        importer, context, project, tmp_path):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')

    moved = tmp_path / 'moved' / 'models.prism'
    moved.parent.mkdir()
    shutil.copyfile(context.metadata_path, moved)
    moved_context = open_model_context(str(moved))
    moved_repository = ModelRepositoryAdapter(moved_context)
    moved_exporter = ModelOriginalExporter(moved_context, moved_repository)
    events = []
    moved_exporter._publish = events.append
    destination = tmp_path / 'moved-out'
    job_id = moved_exporter.export_originals((model_id,), str(destination))
    assert wait_for_job(moved_exporter, job_id) == 'completed'
    assert_directory_matches_source(destination / 'Box.glb', folder, ['Box.glb'])


def test_import_and_export_work_in_a_directory_project(
        importer, context, project, tmp_path):
    from prism.storage.entrypoint import convert
    legacy_folder = SAMPLES / 'khronos_box_glb'
    first = import_model(importer, legacy_folder / 'Box.glb')

    entry = tmp_path / 'converted.prism'
    convert(Path(context.metadata_path), entry)
    managed_context = open_model_context(str(entry))
    assert managed_context.is_managed
    managed_repository = ModelRepositoryAdapter(managed_context)
    managed_importer = ModelImporter(managed_context, managed_repository)
    gltf_folder = SAMPLES / 'khronos_boxtextured_gltf'
    second = import_model(managed_importer, gltf_folder / 'BoxTextured.gltf')

    managed_exporter = ModelOriginalExporter(managed_context, managed_repository)
    events = []
    managed_exporter._publish = events.append
    destination = tmp_path / 'managed-out'
    job_id = managed_exporter.export_originals((first, second), str(destination))
    assert wait_for_job(managed_exporter, job_id) == 'completed'
    assert export_result(events).state == 'completed'
    assert_directory_matches_source(destination / 'Box.glb', legacy_folder,
                                    ['Box.glb'])
    assert_directory_matches_source(
        destination / 'BoxTextured.gltf', gltf_folder,
        ['BoxTextured.gltf', 'BoxTextured0.bin', 'CesiumLogoFlat.png'])
