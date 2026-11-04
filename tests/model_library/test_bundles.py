"""A prepared bundle is all a native preview host needs.

The tests read the bundle exactly like another process would: from the
directory alone, with no repository, no database and no knowledge of where
the model was imported from.
"""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

import pytest

from prism.model_library.bundles import (
    MANIFEST_NAME,
    PREVIEW_DIRECTORY,
    BundleLeaseError,
    ModelBundleStore,
    PreparedBundle,
)
from prism.model_library.importer import ModelImporter
from prism.model_library.project_storage import (
    ModelProjectContext,
    ModelResourceStore,
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


@pytest.fixture
def project(tmp_path, qapp):
    scene = make_scene()
    path = create_project(tmp_path / 'models.prism', scene)
    return path, scene


@pytest.fixture
def context(project):
    path, _scene = project
    return open_model_context(path, open_epoch=11)


@pytest.fixture
def repository(context):
    return ModelRepositoryAdapter(context)


@pytest.fixture
def importer(context, repository):
    return ModelImporter(context, repository)


@pytest.fixture
def bundles(context, repository, tmp_path):
    store = ModelBundleStore(context, repository, root=str(tmp_path / 'bundles'))
    yield store
    store.release_all()


# ---------------------------------------------------------------------------
# A4: the bundle is the whole contract with the native host
# ---------------------------------------------------------------------------


def test_a_bundle_carries_the_model_and_its_dependencies(
        importer, bundles):
    folder = SAMPLES / 'assimp_spider'
    model_id = import_model(importer, folder / 'spider.obj')

    bundle = bundles.prepare(model_id, 1)
    assert isinstance(bundle, PreparedBundle)
    assert bundle.model_id == model_id
    assert bundle.source_revision == 1
    assert bundle.lease_id
    assert bundle.package_root

    root = Path(bundle.package_root)
    assert root.is_dir()
    assert bundle.manifest_path == str(root / MANIFEST_NAME)
    manifest = json.loads(Path(bundle.manifest_path).read_text(encoding='utf-8'))
    assert manifest['model_id'] == model_id
    assert manifest['source_revision'] == 1
    assert bundle.main_relative_path == 'spider.obj'
    assert manifest['main_relative_path'] == 'spider.obj'

    present = {item['relative_path']: item for item in manifest['files']
               if item['state'] == 'present'}
    assert set(present) == {'spider.obj', 'spider.mtl', 'SpiderTex.jpg'}
    for relative, item in present.items():
        path = root / relative
        assert path.is_file(), relative
        assert item['sha256'] == sha256_file(folder / relative)
        assert sha256_file(path) == sha256_file(folder / relative)
        assert item['size_bytes'] == path.stat().st_size


def test_the_bundle_directory_alone_resolves_the_model(importer, bundles):
    """No repository, no database: relative references must still resolve."""
    folder = SAMPLES / 'assimp_spider'
    model_id = import_model(importer, folder / 'spider.obj')
    bundle = bundles.prepare(model_id, 1)
    root = Path(bundle.package_root)
    main = root / bundle.main_relative_path
    assert main.read_bytes() == (folder / 'spider.obj').read_bytes()

    # Walk the material library exactly like a loader would: the OBJ names the
    # MTL relative to itself, and the MTL names its texture the same way.
    material_library = (main.parent
                        / next(line.split(None, 1)[1].strip()
                               for line in main.read_text(encoding='utf-8',
                                                          errors='replace').splitlines()
                               if line.lower().startswith('mtllib')))
    assert material_library.is_file()
    textures = [line.split(None, 1)[1].strip()
                for line in material_library.read_text(
                    encoding='utf-8', errors='replace').splitlines()
                if line.lower().startswith('map_kd')]
    # The one texture that exists is inside the bundle; the four that do not
    # exist are listed as missing instead of being invented.
    resolvable = [name for name in textures
                  if (material_library.parent / name).is_file()]
    assert resolvable == ['.\\SpiderTex.jpg']
    assert (material_library.parent / resolvable[0]).read_bytes() == \
        (folder / 'SpiderTex.jpg').read_bytes()
    missing = [item for item in bundle.manifest['files']
               if item['state'] == 'missing']
    assert len(missing) == 4
    assert all(item['relative_path'] == '' for item in missing)


def test_the_bundle_never_rewrites_the_stored_original(
        importer, bundles, repository, context):
    folder = SAMPLES / 'khronos_avocado_gltf'
    model_id = import_model(importer, folder / 'Avocado.gltf')
    payload = repository.get_payload(model_id)
    stored = {item['relative_path']: item for item in payload['files']}

    bundle = bundles.prepare(model_id, 1)
    root = Path(bundle.package_root)
    for relative, item in stored.items():
        with ModelResourceStore(context).open(int(item['attachment_id'])) as reader:
            assert reader.verify() == item['sha256']
        # The copy in the bundle is a copy; the project still owns the bytes.
        assert sha256_file(root / relative) == item['sha256']
        assert sha256_file(folder / relative) == item['sha256']


def test_a_missing_dependency_is_listed_and_not_written(importer, bundles):
    folder = SAMPLES / 'derived_missing_dependency'
    model_id = import_model(importer, folder / 'spider.obj')
    bundle = bundles.prepare(model_id, 1)
    root = Path(bundle.package_root)
    missing = [item for item in bundle.manifest['files']
               if item['state'] == 'missing']
    assert len(missing) == 5
    for item in missing:
        assert item['original_reference']
        if item['relative_path']:
            assert not (root / item['relative_path']).exists()
    # The model itself is still openable.
    assert (root / bundle.main_relative_path).is_file()


# ---------------------------------------------------------------------------
# A4: leases protect the working copy
# ---------------------------------------------------------------------------


def test_a_lease_protects_the_directory_until_it_is_released(
        importer, bundles):
    folder = SAMPLES / 'khronos_box_glb'
    model_id = import_model(importer, folder / 'Box.glb')
    bundle = bundles.prepare(model_id, 1)
    root = Path(bundle.package_root)
    assert root.is_dir()
    assert bundles.is_leased(root)
    assert [lease for lease in bundles.leased() if lease[0] == bundle.lease_id]

    bundles.release(bundle.lease_id)
    assert not root.exists()
    assert not bundles.is_leased(root)
    with pytest.raises(BundleLeaseError):
        bundles.release(bundle.lease_id)


def test_releasing_one_lease_keeps_the_other_working_copy(
        importer, bundles):
    first = import_model(importer, SAMPLES / 'khronos_box_glb' / 'Box.glb')
    second = import_model(importer,
                          SAMPLES / 'khronos_boxtextured_gltf' / 'BoxTextured.gltf')
    first_bundle = bundles.prepare(first, 1)
    second_bundle = bundles.prepare(second, 1)
    assert Path(first_bundle.package_root) != Path(second_bundle.package_root)

    bundles.release(first_bundle.lease_id)
    assert not Path(first_bundle.package_root).exists()
    assert Path(second_bundle.package_root).is_dir()
    assert bundles.is_leased(second_bundle.package_root)
    assert (Path(second_bundle.package_root)
            / second_bundle.main_relative_path).is_file()


def test_release_all_clears_every_lease(importer, bundles):
    first = import_model(importer, SAMPLES / 'khronos_box_glb' / 'Box.glb')
    second = import_model(importer,
                          SAMPLES / 'khronos_boxtextured_gltf' / 'BoxTextured.gltf')
    roots = [Path(bundles.prepare(model_id, 1).package_root)
             for model_id in (first, second)]
    bundles.release_all()
    assert bundles.leased() == ()
    assert not any(root.exists() for root in roots)


# ---------------------------------------------------------------------------
# A4: revisions decide identity
# ---------------------------------------------------------------------------


def test_a_stale_source_revision_is_refused(importer, bundles):
    model_id = import_model(importer, SAMPLES / 'khronos_box_glb' / 'Box.glb')
    with pytest.raises(ModelStorageError):
        bundles.prepare(model_id, 99)
    with pytest.raises(ModelStorageError):
        bundles.prepare('not-a-model', 1)
    assert bundles.leased() == ()


def test_the_bundle_key_carries_the_revisions(importer, bundles):
    model_id = import_model(importer, SAMPLES / 'khronos_box_glb' / 'Box.glb')
    plain = bundles.prepare(model_id, 1)
    assert plain.key == f'{model_id}:r1'
    with_appearance = bundles.prepare(model_id, 1, appearance_revision=4)
    assert with_appearance.key == f'{model_id}:r1:a4'
    assert with_appearance.appearance_revision == 4
    assert Path(plain.package_root) != Path(with_appearance.package_root)
    assert json.loads(Path(with_appearance.manifest_path).read_text(
        encoding='utf-8'))['appearance_revision'] == 0


# ---------------------------------------------------------------------------
# A4: absolute references get a derived work copy
# ---------------------------------------------------------------------------


def test_an_absolute_reference_gets_a_derived_work_copy(
        tmp_path, importer, bundles, repository):
    texture_folder = tmp_path / 'outside'
    texture_folder.mkdir()
    texture = texture_folder / 'paint.png'
    texture.write_bytes(b'\x89PNG absolute')
    model_folder = tmp_path / 'model dir'
    model_folder.mkdir()
    source = model_folder / 'prop.obj'
    source.write_text('mtllib prop.mtl\nv 0 0 0\n', encoding='utf-8')
    material = model_folder / 'prop.mtl'
    material.write_text(f'newmtl a\nmap_Kd {texture}\n', encoding='utf-8')

    model_id = import_model(importer, source)
    bundle = bundles.prepare(model_id, 1)
    root = Path(bundle.package_root)
    payload = repository.get_payload(model_id)
    material_entry = [item for item in payload['files']
                      if item['role'] == 'material'][0]
    texture_entry = [item for item in payload['files']
                     if item['role'] == 'texture'][0]

    # The original bytes in the package are untouched.
    assert (root / material_entry['relative_path']).read_bytes() == \
        material.read_bytes()
    assert bundle.manifest['absolute_reference_map'] == [{
        'original_reference': str(texture),
        'relative_path': texture_entry['relative_path'],
    }]
    # The derived copy lives in its own folder and re-points the reference.
    work_copies = bundle.manifest['work_copies']
    assert work_copies
    work_copy = root / work_copies[0]['work_relative_path']
    assert work_copy.is_file()
    assert work_copy.parent == root / PREVIEW_DIRECTORY
    text = work_copy.read_text(encoding='utf-8')
    assert str(texture) not in text
    assert texture_entry['relative_path'] in text
    # The derived copy resolves to the packaged texture.
    assert (root / texture_entry['relative_path']).read_bytes() == texture.read_bytes()


def test_two_models_of_the_same_file_get_two_bundles(importer, bundles):
    source = SAMPLES / 'khronos_box_glb' / 'Box.glb'
    first = import_model(importer, source)
    second = import_model(importer, source)
    first_bundle = bundles.prepare(first, 1)
    second_bundle = bundles.prepare(second, 1)
    assert first_bundle.key != second_bundle.key
    assert Path(first_bundle.package_root) != Path(second_bundle.package_root)
    assert bundles.is_leased(first_bundle.package_root)
    assert bundles.is_leased(second_bundle.package_root)


# ---------------------------------------------------------------------------
# 会话目录回收
# ---------------------------------------------------------------------------


def _session_directory(cache_base, name, age_seconds=0.0):
    """A session directory as the model library leaves it behind."""
    directory = cache_base / name
    (directory / 'bundles').mkdir(parents=True)
    (directory / 'bundles' / 'payload.bin').write_bytes(b'x' * 32)
    if age_seconds:
        stamp = time.time() - age_seconds
        os.utime(directory, (stamp, stamp))
    return directory


def test_reclaim_removes_the_sessions_that_are_gone_and_keeps_the_rest(tmp_path):
    """Every open used to leave a directory behind: 188 of them, 3.81 GB."""
    from prism.model_library.bundles import reclaim_stale_sessions

    cache_base = tmp_path / 'model-library'
    cache_base.mkdir()
    gone = _session_directory(cache_base, 'a' * 32, age_seconds=7200)
    fresh = _session_directory(cache_base, 'b' * 32)
    running = _session_directory(cache_base, 'c' * 32, age_seconds=7200)
    covers = cache_base / 'covers'
    covers.mkdir()
    work = cache_base / 'material-work'
    work.mkdir()

    removed = reclaim_stale_sessions(cache_base, 'c' * 32)

    assert removed == 1
    assert not gone.exists()
    # The running session, the fresh one and the shared caches stay.
    assert fresh.exists()
    assert running.exists()
    assert covers.exists()
    assert work.exists()


def test_reclaim_never_waits_for_the_disk(tmp_path):
    """An open must not spend its time deleting cache."""
    from prism.model_library.bundles import reclaim_stale_sessions

    cache_base = tmp_path / 'model-library'
    cache_base.mkdir()
    gone = _session_directory(cache_base, 'd' * 32, age_seconds=7200)

    assert reclaim_stale_sessions(cache_base, older_than=0.0, budget=0.0) == 0
    assert gone.exists()
