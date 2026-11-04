from contextlib import closing
import sqlite3
import zipfile
import pytest
from tests.test_partial_project import project,png
from prism.storage.migration import convert_legacy,read_manifest
from prism.storage.portable import compact,export_bundle,open_bundle
from prism.storage.managed import database_path
from prism.source_store import read_archive


def test_packing_and_bundle_keep_exact_originals(project,png,tmp_path):
    folder=tmp_path/'working.prismproj';convert_legacy(project,folder,experimental=True,managed=True)
    compact(folder)
    assert read_archive((database_path(folder),1))==png
    bundle=tmp_path/'portable.prismbundle';export_bundle(folder,bundle)
    target=tmp_path/'restored.prismproj';open_bundle(bundle,target)
    assert read_archive((database_path(target),12))==png
    assert read_manifest(folder)['storage_instance_id']!=read_manifest(target)['storage_instance_id']
    with closing(sqlite3.connect(database_path(target))) as connection:
        assert connection.execute('SELECT COUNT(*) FROM items').fetchone()[0]==12


@pytest.mark.parametrize('name',['../escape','objects/.. /escape','CON','C:/escape'])
def test_bundle_rejects_unsafe_names_before_extracting(tmp_path,name):
    path=tmp_path/'bad.prismbundle'
    with zipfile.ZipFile(path,'w') as archive:archive.writestr(name,b'no')
    target=tmp_path/'out.prismproj'
    with pytest.raises(ValueError):open_bundle(path,target)
    assert not target.exists()


def test_verified_checkpoint_can_be_recovered(project,png,tmp_path):
    from prism.storage.checkpoint import recover
    folder=tmp_path/'working.prismproj';convert_legacy(project,folder,experimental=True,managed=True)
    checkpoint=folder.with_name(folder.name+'.incomplete');folder.rename(checkpoint)
    assert recover(checkpoint)==folder
    assert read_archive((database_path(folder),12))==png
    assert not checkpoint.exists()


def test_visual_search_reads_object_and_keeps_identity_after_metadata_save(project,png,tmp_path):
    from prism.components.visual_search_worker import source_bytes,source_identity
    folder=tmp_path/'working.prismproj';convert_legacy(project,folder,experimental=True,managed=True)
    source={'archive':[database_path(folder),1]}
    before=source_identity(source)
    assert source_bytes(source)==png
    with closing(sqlite3.connect(database_path(folder))) as conn:
        conn.execute('UPDATE items SET x=x+1 WHERE id=1');conn.commit()
    assert source_identity(source)==before
