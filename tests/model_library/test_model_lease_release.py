"""The lease working copies must not survive the process.

Every open model holds a lease: a working copy of the model in the cache
folder.  ``ModelLibraryService.close`` releases them on a daemon thread,
because it must not join inside a GUI callback.  A process that ends before
that thread finishes leaves the copies on the disk - the machine this was
measured on held 640 MB of them - and they are only reclaimed when the
library is opened again, an hour later.

The owner of the process end waits for the thread.  This test pins that
waiting gives the working copy back.
"""
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace


def _import_one(tmp_path):
    from prism.model_library.importer import ModelImporter
    from prism.model_library.project_storage import open_model_context
    from prism.model_library.repository import ModelRepositoryAdapter
    from tests.model_library.test_storage import create_project, make_scene
    project = tmp_path / 'lease.prism'
    create_project(project, make_scene())
    source = tmp_path / 'one.obj'
    source.write_text('v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n')
    context = open_model_context(project, open_epoch=1)
    repo = ModelRepositoryAdapter(context)
    importer = ModelImporter(context, repo)
    job = importer.import_paths([str(source)])
    deadline = time.monotonic() + 30
    model_id = None
    while time.monotonic() < deadline:
        with sqlite3.connect(context.metadata_path) as db:
            row = db.execute('SELECT id FROM model_records').fetchone()
        if row:
            model_id = row[0]
            break
        time.sleep(.02)
    assert model_id, 'the model was not imported'
    return context, repo, model_id


def test_waiting_for_the_close_gives_the_working_copy_back(tmp_path, qapp):
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService
    context, repo, model_id = _import_one(tmp_path)
    bundles = ModelBundleStore(context, repo, root=str(tmp_path / 'bundles'))
    service = ModelLibraryService(repo, bundles=bundles,
                                  on_commit=lambda before, after: None)
    payload = repo.get_payload(model_id)
    bundle = bundles.prepare(model_id, int(payload['source_revision']))
    working_copy = Path(bundle.package_root)
    assert working_copy.exists(), 'the working copy was not created'
    service.close()
    assert service.wait_closed(timeout=30), 'the close thread did not finish'
    assert not working_copy.exists(), \
        'the working copy was not given back: %s' % (working_copy,)


def test_a_service_without_a_close_thread_is_already_done(tmp_path, qapp):
    from prism.model_library.service import ModelLibraryService
    service = ModelLibraryService(None)
    assert service.wait_closed(timeout=0)


def _leased_service(tmp_path):
    from prism.model_library.bundles import ModelBundleStore
    from prism.model_library.service import ModelLibraryService
    context, repo, model_id = _import_one(tmp_path)
    bundles = ModelBundleStore(context, repo, root=str(tmp_path / 'bundles'))
    service = ModelLibraryService(repo, bundles=bundles,
                                  on_commit=lambda before, after: None)
    payload = repo.get_payload(model_id)
    bundle = bundles.prepare(model_id, int(payload['source_revision']))
    return service, Path(bundle.package_root)


def test_the_composition_waits_for_the_service_it_closed(tmp_path, qapp):
    from prism.model_library.composition import ModelLibraryComposition
    service, working_copy = _leased_service(tmp_path)
    service.close()
    owner = SimpleNamespace(_closed_services=[])
    ModelLibraryComposition._track_closed(owner, service)
    assert ModelLibraryComposition.wait_closed(owner, timeout=30)
    assert not working_copy.exists(), \
        'the working copy was not given back: %s' % (working_copy,)


def test_opening_another_project_keeps_the_closed_service_waitable(tmp_path, qapp):
    """A project switch closes the service and drops the reference to it.

    The working copies are given back on a background thread, so the dropped
    service must still be waited for at the end of the process.
    """
    from prism.model_library.composition import ModelLibraryComposition
    service, working_copy = _leased_service(tmp_path)
    owner = SimpleNamespace(_scope=None, _generation=0, service=service,
                             _closed_services=[],
                             _key=lambda: 'project',
                             window=SimpleNamespace(
                                 model_library=SimpleNamespace(initialized=False)))
    owner._track_closed = lambda item: ModelLibraryComposition._track_closed(owner, item)
    ModelLibraryComposition.invalidate(owner)
    assert owner.service is None, 'the service was not closed'
    assert owner._closed_services == [service], \
        'the closed service was forgotten: %r' % (owner._closed_services,)
    assert ModelLibraryComposition.wait_closed(owner, timeout=30)
    assert not working_copy.exists(), \
        'the working copy was not given back: %s' % (working_copy,)
