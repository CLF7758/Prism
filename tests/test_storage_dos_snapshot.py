import pytest
pytest.importorskip('disk_objectstore', reason='requires storage-experimental optional dependency')

from prism.storage.contracts import StorageError, StorageErrorKind
from prism.storage.dos_repository import DosObjectRepository, ReadOnlyDosRepository
from prism.storage.object_repository import ReadOnlyViolation, ActiveWriter
from prism.storage.source_binding import state_for_exception


def test_equal_folder_names_never_share_a_mutable_private_index(tmp_path):
    folders = [tmp_path / 'a/objects', tmp_path / 'b/objects']
    keys = []
    for index, folder in enumerate(folders):
        writer = DosObjectRepository.create(folder)
        try:
            keys.append(writer.put_stream(iter([f'payload-{index}'.encode()])).hashkey)
            writer._container.pack_all_loose(do_fsync=True)
        finally:
            writer.close()
    cache = tmp_path / 'shared-cache'
    first = ReadOnlyDosRepository.open(folders[0], cache_dir=cache)
    second = ReadOnlyDosRepository.open(folders[1], cache_dir=cache)
    try:
        assert first._index_copy != second._index_copy
        assert b''.join(first.open_stream(keys[0])) == b'payload-0'
        assert b''.join(second.open_stream(keys[1])) == b'payload-1'
        assert first.stat(keys[1]) is None
    finally:
        first.close()
        second.close()


def test_readonly_cache_cannot_be_inside_source(tmp_path):
    folder = tmp_path / 'source'
    writer = DosObjectRepository.create(folder)
    writer.close()
    with pytest.raises(ReadOnlyViolation):
        ReadOnlyDosRepository.open(folder, cache_dir=folder / 'cache')
    assert not (folder / 'cache').exists()


def test_busy_and_unknown_errors_do_not_become_missing_content():
    with pytest.raises(StorageError) as caught:
        state_for_exception(ActiveWriter('active'))
    assert caught.value.kind is StorageErrorKind.SOURCE_BUSY
    with pytest.raises(StorageError) as caught:
        state_for_exception(RuntimeError('unknown'))
    assert caught.value.kind is StorageErrorKind.UNKNOWN
