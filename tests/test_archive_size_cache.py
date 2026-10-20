"""Collection statistics must reuse project IO metadata during UI navigation."""
import os
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from prism.source_store import remember_archive_sizes, storage_bytes


def test_indexed_archive_statistics_never_open_another_database(tmp_path, monkeypatch):
    path = tmp_path/'project.prism'
    with sqlite3.connect(path) as db:
        db.execute('create table sqlar (item_id integer,sz integer,data blob)')
        db.execute('insert into sqlar values (1,8,?)',(b'original',))
    remember_archive_sizes(str(path),{1:8})
    monkeypatch.setattr('prism.source_store.sqlite3.connect',Mock(side_effect=AssertionError('UI database read')))
    items = [SimpleNamespace(_prism_source=(str(path),1))]
    assert storage_bytes(items,{}) == 8
    assert storage_bytes(items,{}) == 8  # another surface, same published metadata


def test_modified_archive_invalidates_published_statistics(tmp_path):
    path = tmp_path/'project.prism'
    with sqlite3.connect(path) as db:
        db.execute('create table sqlar (item_id integer,sz integer,data blob)')
        db.execute('insert into sqlar values (1,8,?)',(b'original',))
    remember_archive_sizes(str(path),{1:8})
    before = path.stat()
    with sqlite3.connect(path) as db:
        db.execute('update sqlar set sz=12')
    os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns+10_000_000))
    assert storage_bytes([SimpleNamespace(_prism_source=(str(path),1))],{}) == 12


def test_external_locked_archive_returns_unknown_without_five_second_wait(tmp_path):
    import time
    path = tmp_path/'external.prism'
    db = sqlite3.connect(path)
    db.execute('create table sqlar (item_id integer,sz integer,data blob)')
    db.execute('insert into sqlar values (1,8,?)',(b'original',))
    db.commit()
    db.execute('begin exclusive')
    try:
        started = time.perf_counter()
        assert storage_bytes([SimpleNamespace(_prism_source=(str(path),1))],{}) is None
        assert time.perf_counter()-started < .5
    finally:
        db.rollback()
        db.close()
