"""Cancellation publication boundaries and skipped-page accounting."""
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import zipfile

import pytest

from prism.fileio import resource_export as exports
from prism.fileio.resource_export_preparation import PageBodyReader
from prism.workspace_service import ResourceTreeService


class Signal:
    def __init__(self, callback=None):
        self.calls = []
        self.callback = callback

    def emit(self, *args):
        self.calls.append(args)
        if self.callback:
            self.callback(*args)


def make_worker():
    return SimpleNamespace(canceled=False, begin_processing=Signal(),
                           progress=Signal(), finished=Signal())


def make_plan(*entries):
    return exports.ExportPlan('notes', entries=entries,
                              bodies=PageBodyReader())


def assert_cancelled(worker, plan):
    assert plan.bodies.cancelled
    assert len(worker.finished.calls) == 1
    errors = worker.finished.calls[0][1]
    assert sum(isinstance(error, exports.ExportCancelled)
               for error in errors) == 1


@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('stage', ['before', 'last-entry', 'archive', 'close'])
def test_cancelled_zip_does_not_publish(tmp_path, monkeypatch, existing, stage):
    target = tmp_path / 'out.zip'
    previous = b'previous export must survive'
    if existing:
        target.write_bytes(previous)
    worker = make_worker()
    plan = make_plan(exports.ExportEntry('page.txt', payload=b'new export'))
    if stage == 'before':
        worker.canceled = True
    elif stage == 'last-entry':
        worker.progress.callback = lambda _value: setattr(worker, 'canceled', True)
    elif stage == 'archive':
        original = zipfile.ZipFile.write

        def cancel_after_archive_member(archive, *args, **kwargs):
            original(archive, *args, **kwargs)
            worker.canceled = True

        monkeypatch.setattr(zipfile.ZipFile, 'write', cancel_after_archive_member)
    else:
        original = zipfile.ZipFile.close

        def cancel_after_archive_close(archive):
            writing = archive.fp is not None and archive.mode == 'w'
            original(archive)
            if writing:
                worker.canceled = True

        monkeypatch.setattr(zipfile.ZipFile, 'close', cancel_after_archive_close)

    exports.write_zip(plan, target, worker)

    assert_cancelled(worker, plan)
    assert target.exists() == existing
    if existing:
        assert target.read_bytes() == previous
    assert sorted(path.name for path in tmp_path.iterdir()) == (
        ['out.zip'] if existing else [])


@pytest.mark.parametrize('stage', ['writer', 'progress'])
def test_cancel_after_only_entry_removes_new_file(tmp_path, stage):
    worker = make_worker()

    def write(target):
        Path(target).write_bytes(b'new export')
        if stage == 'writer':
            worker.canceled = True

    if stage == 'progress':
        worker.progress.callback = lambda _value: setattr(worker, 'canceled', True)
    plan = make_plan(exports.ExportEntry('page.txt', writer=write))

    exports.write_directory(plan, tmp_path, worker)

    assert_cancelled(worker, plan)
    assert not (tmp_path / 'notes').exists()


def test_deferred_empty_page_keeps_old_file_but_is_not_counted(tmp_path):
    source = tmp_path / 'source.prism'
    with sqlite3.connect(source) as connection:
        connection.execute('CREATE TABLE workspace_pages(id TEXT, data TEXT)')
        connection.execute('INSERT INTO workspace_pages VALUES (?, ?)',
                           ('page', '""'))
    pages = [{'id': 'page', 'kind': 'document', 'title': 'Empty'}]
    service = ResourceTreeService('director-export')
    service.createPage('document', 'Empty', 'page')
    bodies = PageBodyReader(pages, source, {'page'})
    exporter = exports.ResourceSubtreeExporter(service, pages=bodies.pages,
                                               bodies=bodies)
    plan = exporter.build_plan('document')
    assert len(plan.entries) == 1
    target = Path(plan.entries[0].target_for(tmp_path / plan.root_name))
    target.parent.mkdir(parents=True)
    target.write_bytes(b'old document')
    worker = make_worker()

    exports.write_directory(plan, tmp_path, worker)

    assert target.read_bytes() == b'old document'
    assert worker.finished.calls[0][1] == []
    assert plan.file_count == 0
    assert bodies.written_files == 0
    assert bodies.skipped_files == 1
    assert plan.all_notes


def test_reusing_skipped_plan_resets_write_counts(tmp_path):
    plan = make_plan(exports.ExportEntry('empty.docx', writer=lambda _path: None))
    for folder in ('first', 'second'):
        exports.write_directory(plan, tmp_path / folder)
        assert plan.file_count == 0
        assert plan.bodies.skipped_files == 1
        assert plan.bodies.written_files == 0


def test_completed_zip_publishes_new_bytes(tmp_path):
    target = tmp_path / 'out.zip'
    target.write_bytes(b'previous export')
    worker = make_worker()
    plan = make_plan(exports.ExportEntry('page.txt', payload=b'new export'))

    exports.write_zip(plan, target, worker)

    assert worker.finished.calls[0][1] == []
    assert not plan.bodies.cancelled
    assert plan.file_count == 1
    assert plan.bodies.written_files == 1
    with zipfile.ZipFile(target) as archive:
        assert archive.read('notes/page.txt') == b'new export'
