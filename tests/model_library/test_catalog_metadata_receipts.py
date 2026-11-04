import sqlite3
import json
from pathlib import Path
import pytest

from tests.model_library.test_storage import project, context, repository, sample_payload
from prism.project_catalog_index import ProjectCatalogIndex


@pytest.mark.parametrize('managed', [False, True])
def test_material_resource_commit_and_cover_keep_one_incremental_chain(repository, context, tmp_path, managed):
    if managed:
        from prism.storage.entrypoint import convert
        from prism.model_library.project_storage import open_model_context
        from prism.model_library.repository import ModelRepositoryAdapter
        entry = tmp_path / 'managed.prism'
        convert(Path(context.metadata_path), entry)
        context = open_model_context(str(entry))
        repository = ModelRepositoryAdapter(context)
    payload = repository.insert(sample_payload())
    repository.take_catalog_receipts()
    index = ProjectCatalogIndex(context.metadata_path, tmp_path / 'catalog.sqlite')
    assert index.build().complete
    original_count = index.count()
    source = tmp_path / 'material-original.bin'
    source.write_bytes(b'original texture bytes')
    def update_material(db, stored):
        payload['appearance'] = {'resource': stored[0].attachment_id}
        db.execute('UPDATE model_records SET payload=? WHERE id=?',
                   (json.dumps(payload), payload['id']))
    repository.resources.put_batch([(source, source.name)], commit=update_material)
    repository.update_cache(payload['id'], expected_revision=payload['record_revision'],
                            thumbnail={'state': 'ready'})
    receipts = repository.take_catalog_receipts()
    assert len(receipts) == 2
    assert receipts[0].source_after == receipts[1].source_before
    result = index.apply_save_receipts(receipts)
    assert result is not None and result.processed_records == 0
    assert index.count() == original_count  # count() also validates the current source identity.
    assert source.read_bytes() == b'original texture bytes'


@pytest.mark.parametrize('managed', [False, True])
def test_model_restore_and_cover_advance_image_catalog_without_rebuild(repository, context, tmp_path, managed):
    if managed:
        from prism.storage.entrypoint import convert
        from prism.model_library.project_storage import open_model_context
        from prism.model_library.repository import ModelRepositoryAdapter
        entry = tmp_path / 'managed.prism'
        convert(Path(context.metadata_path), entry)
        context = open_model_context(str(entry))
        repository = ModelRepositoryAdapter(context)
    payload = repository.insert(sample_payload())
    index = ProjectCatalogIndex(context.metadata_path, tmp_path / 'catalog.sqlite')
    assert index.build().complete
    repository.patch_batch([('model-1', payload['record_revision'], {'deleted_at': '2026-11-03'})])
    result = index.apply_save_receipts(repository.take_catalog_receipts())
    assert result is not None and result.processed_records == 0
    payload = repository.get_payload('model-1')
    repository.update_cache('model-1', expected_revision=payload['record_revision'],
                            thumbnail={'state': 'ready'})
    result = index.apply_save_receipts(repository.take_catalog_receipts())
    assert result is not None and result.processed_records == 0


def test_trigger_that_changes_images_cannot_publish_metadata_only_receipt(repository, context):
    payload = repository.insert(sample_payload())
    repository.take_catalog_receipts()  # Only the following triggered transaction is under test.
    with sqlite3.connect(context.metadata_path) as db:
        db.execute('CREATE TRIGGER touch_images AFTER UPDATE ON model_records BEGIN UPDATE items SET x=x; END')
    repository.patch_batch([('model-1', payload['record_revision'], {'name': 'renamed'})])
    assert repository.take_catalog_receipts() == ()


def test_existing_view_coordinator_consumes_model_receipt_without_full_build(
        repository, context, tmp_path, view, qtbot, monkeypatch):
    from prism.project_open_scope import ProjectOpenScope
    payload = repository.insert(sample_payload())
    cache = tmp_path / 'directory.sqlite'
    scope = ProjectOpenScope(context.metadata_path, cache)
    scope.build()
    view._open_scope = scope
    repository.patch_batch([('model-1', payload['record_revision'], {'name': 'renamed'})])
    def unexpected(*args, **kwargs):
        raise AssertionError('A model-only transaction must not rebuild image rows')
    monkeypatch.setattr(ProjectCatalogIndex, 'build', unexpected)
    view._refresh_catalog_after_save(str(context.metadata_path),
        receipts=repository.take_catalog_receipts(), reason='model_metadata_committed')
    qtbot.waitUntil(lambda: view._catalog_build_active is None, timeout=10000)
    assert view._open_scope.ready
