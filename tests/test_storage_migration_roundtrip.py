"""Migration gates against the independent verifier, on disposable fixtures."""
import hashlib
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3

import pytest
pytest.importorskip('disk_objectstore', reason='requires storage-experimental optional dependency')

from tests.test_partial_project import project, png
from prism.storage.contracts import StorageError, StorageErrorKind
from prism.storage.migration import convert_legacy, read_manifest
from prism.storage.legacy_export import export_legacy


def independent_compare(source, target):
    spec = importlib.util.spec_from_file_location('independent_storage_verifier',
        Path(__file__).resolve().parents[1] / 'tools/verify_storage_conversion.py')
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    report = verifier.compare_databases(source, target)
    assert report['verdict'] == 'PASS', report


def test_exact_roundtrip_preserves_null_empty_unknown_and_original_bytes(project, tmp_path):
    with sqlite3.connect(project) as conn:
        conn.execute("INSERT INTO attachments(id,name,blob) VALUES(2,'empty',?)", (b'',))
        conn.execute("INSERT INTO attachments(id,name,blob) VALUES(3,'missing',NULL)")
        conn.execute('CREATE INDEX future_value_index ON future_extension(original)')
    source_digest = hashlib.sha256(project.read_bytes()).hexdigest()
    directory, exported = tmp_path / 'separated', tmp_path / 'roundtrip.prism'
    manifest = convert_legacy(project, directory, experimental=True)
    assert manifest['phase'] == 'verified'
    assert read_manifest(directory)['metadata_sha256'] == manifest['metadata_sha256']
    with sqlite3.connect(directory / 'metadata.sqlite') as conn:
        refs = conn.execute('SELECT owner,entity,size FROM object_refs').fetchall()
        assert ('attachments', '2', 0) in refs
        assert not any(row[:2] == ('attachments', '3') for row in refs)
        # All 12 images are byte-identical but their instances remain separate.
        assert conn.execute("SELECT COUNT(DISTINCT hashkey) FROM object_refs WHERE owner='sqlar'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 12
    export_legacy(directory, exported, experimental=True)
    independent_compare(project, exported)
    assert hashlib.sha256(project.read_bytes()).hexdigest() == source_digest


def test_cancel_resume_has_no_published_incomplete_directory(project, tmp_path, png):
    with sqlite3.connect(project) as conn:
        for key in range(13, 90):
            conn.execute('INSERT INTO items SELECT ?,type,x,y,z,scale,rotation,flip,data FROM items WHERE id=1', (key,))
            conn.execute('INSERT INTO sqlar(name,item_id,sz,data) VALUES(?,?,?,?)',
                         (str(key)+'.png', key, len(png), png))
    directory = tmp_path / 'resume'
    cancelled = False
    def progress(value):
        nonlocal cancelled
        if value['phase'] == 'objects': cancelled = True
    with pytest.raises(StorageError) as caught:
        convert_legacy(project, directory, experimental=True, cancel=lambda: cancelled, progress=progress)
    assert caught.value.kind is StorageErrorKind.CANCELLED
    assert not directory.exists()
    staging = directory.with_name('resume.incomplete')
    assert staging.exists()
    with closing(sqlite3.connect(staging / 'metadata.sqlite')) as conn:
        assert conn.execute('SELECT COUNT(*) FROM object_refs').fetchone()[0] == 64
    convert_legacy(project, directory, experimental=True)
    exported = tmp_path / 'resumed.prism'
    export_legacy(directory, exported, experimental=True)
    independent_compare(project, exported)


def test_changed_source_refuses_resume(project, tmp_path):
    directory = tmp_path / 'changed-source'
    def progress(value):
        if value['phase'] == 'metadata':
            raise StorageError(StorageErrorKind.CANCELLED)
    with pytest.raises(StorageError):
        convert_legacy(project, directory, experimental=True, progress=progress)
    with sqlite3.connect(project) as conn:
        conn.execute('UPDATE items SET x=x+1 WHERE id=1')
    with pytest.raises(StorageError, match='Source changed since checkpoint'):
        convert_legacy(project, directory, experimental=True)
    assert not directory.exists()


def test_reference_corruption_prevents_export_publication(project, tmp_path):
    directory, target = tmp_path / 'corrupt', tmp_path / 'must-not-exist.prism'
    convert_legacy(project, directory, experimental=True)
    with sqlite3.connect(directory / 'metadata.sqlite') as conn:
        conn.execute("UPDATE object_refs SET hashkey=? WHERE owner='sqlar'", ('0'*64,))
    with pytest.raises(StorageError, match='metadata identity changed'):
        export_legacy(directory, target, experimental=True)
    assert not target.exists()


def test_opt_in_and_no_overwrite_are_enforced(project, tmp_path):
    directory = tmp_path / 'off'
    with pytest.raises(StorageError):
        convert_legacy(project, directory)
    assert not directory.exists()
    directory.mkdir()
    with pytest.raises(FileExistsError):
        convert_legacy(project, directory, experimental=True)


def test_headless_conversion_does_not_import_qt_or_user_settings(project, tmp_path):
    import subprocess
    import sys
    directory = tmp_path / 'headless'
    result = subprocess.run([sys.executable, '-c',
        'import sys; from prism.storage.migration import convert_legacy; '
        'convert_legacy(sys.argv[1],sys.argv[2],experimental=True); '
        'assert not any(m.startswith("PyQt6") or m == "prism.config" for m in sys.modules); '
        'print("HEADLESS_NO_SETTINGS_PASS")', str(project), str(directory)],
        capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'HEADLESS_NO_SETTINGS_PASS' in result.stdout


def test_implicit_rowids_and_autoincrement_high_water_survive(project, tmp_path):
    with sqlite3.connect(project) as conn:
        conn.execute('CREATE TABLE rowid_extension(value TEXT)')
        conn.execute("INSERT INTO rowid_extension(rowid,value) VALUES(-7,'negative'),(103,'gap')")
        conn.execute("INSERT INTO attachments(id,name,blob) VALUES(1000,'deleted',?)", (b'unreferenced',))
        conn.execute('DELETE FROM attachments WHERE id=1000')
    directory, exported = tmp_path / 'rowids', tmp_path / 'rowids.prism'
    convert_legacy(project, directory, experimental=True)
    export_legacy(directory, exported, experimental=True)
    with closing(sqlite3.connect(exported)) as conn:
        assert conn.execute('SELECT rowid,value FROM rowid_extension ORDER BY rowid').fetchall() == [(-7, 'negative'), (103, 'gap')]
        assert conn.execute("SELECT seq FROM sqlite_sequence WHERE name='attachments'").fetchone()[0] == 1000
    independent_compare(project, exported)


def test_destination_lease_rejects_concurrent_writer_and_survives_process_exit(tmp_path):
    from prism.storage.leases import exclusive_destination
    import subprocess
    import sys
    destination = tmp_path / 'leased'
    with exclusive_destination(destination):
        assert not destination.exists()
        assert not destination.with_name('.leased.prism-convert.lock').exists()
        with pytest.raises(StorageError, match='Another conversion'):
            with exclusive_destination(destination):
                pytest.fail('A second writer must not enter')
    child = subprocess.run([sys.executable, '-c',
        'import os,sys; from prism.storage.leases import exclusive_destination; '
        'lease=exclusive_destination(sys.argv[1]); lease.__enter__(); os._exit(0)',
        str(destination)], capture_output=True, timeout=5)
    assert child.returncode == 0
    with exclusive_destination(destination):
        pass
