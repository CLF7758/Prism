"""Deterministic worker gates for the materialization closing boundary."""
import threading

import pytest

from prism import source_store


@pytest.fixture
def fake_source(monkeypatch):
    monkeypatch.setattr(source_store, 'archive_video_key',
                        lambda archive, suffix: (archive, suffix))
    monkeypatch.setattr(source_store, 'retain_archive_video',
                        lambda archive, suffix: str(archive))


def test_shutdown_cannot_pass_an_accepted_submission(monkeypatch, fake_source):
    service = source_store.MaterializationService(workers=1)
    submit_entered = threading.Event()
    release_submit = threading.Event()
    shutdown_entered = threading.Event()
    shutdown_done = threading.Event()
    results, errors = [], []
    submit = service._executor.submit

    def gated_submit(*args, **kwargs):
        submit_entered.set()
        assert release_submit.wait(5)
        return submit(*args, **kwargs)

    def request():
        try:
            results.append(service.request('original'))
        except BaseException as error:
            errors.append(error)

    def shutdown():
        shutdown_entered.set()
        service.shutdown(wait=True)
        shutdown_done.set()

    monkeypatch.setattr(service._executor, 'submit', gated_submit)
    requester = threading.Thread(target=request)
    closer = threading.Thread(target=shutdown)
    try:
        requester.start()
        assert submit_entered.wait(5)
        closer.start()
        assert shutdown_entered.wait(5)
        # Shutdown must wait for the accepted request to obtain its future.
        assert not shutdown_done.wait(.2)
    finally:
        release_submit.set()
        requester.join(5)
        if closer.ident is not None:
            closer.join(5)
        service.shutdown(wait=True)
    assert not requester.is_alive() and not closer.is_alive()
    assert not errors
    assert results[0].wait(1) == 'original'
    assert service.is_drained()


def test_queued_mandatory_survives_shutdown_but_optional_is_released(
        monkeypatch, fake_source):
    service = source_store.MaterializationService(workers=1)
    running = threading.Event()
    release = threading.Event()

    def extract(archive, suffix):
        if archive == 'first':
            running.set()
            assert release.wait(5)
        return archive

    monkeypatch.setattr(source_store, 'retain_archive_video', extract)
    try:
        first = service.request('first')
        assert running.wait(5)
        mandatory = service.request('save')
        optional = service.request('preview', cancel_event=threading.Event())
        service.shutdown(wait=False)
        assert not mandatory.done
        with pytest.raises(source_store.MaterializationCancelled):
            optional.wait(1)
        release.set()
        service.shutdown(wait=True)
        assert first.wait(1) == 'first'
        assert mandatory.wait(1) == 'save'
        assert service.is_drained()
    finally:
        release.set()
        service.shutdown(wait=True)


def test_resume_only_reverses_stop_not_final_shutdown(monkeypatch, fake_source):
    service = source_store.MaterializationService(workers=1)
    monkeypatch.setattr(source_store, '_service', service)
    try:
        service.stop_accepting()
        assert source_store.resume_accepting_materialization() is True
        assert service.request('saved').wait(1) == 'saved'
        service.shutdown()
        assert source_store.resume_accepting_materialization() is False
        assert service.closing
        with pytest.raises(source_store.MaterializationClosing):
            source_store.materialization_service().request('later')
    finally:
        service.shutdown()


def test_submit_failure_finishes_and_removes_registered_task(monkeypatch, fake_source):
    service = source_store.MaterializationService(workers=1)
    observed = []

    def fail_submit(*args, **kwargs):
        observed.extend(service._tasks.values())
        raise RuntimeError('submission failed')

    monkeypatch.setattr(service._executor, 'submit', fail_submit)
    try:
        with pytest.raises(RuntimeError, match='submission failed'):
            service.request('original')
        assert observed[0].finished.is_set()
        assert isinstance(observed[0].error, RuntimeError)
        assert service.state()['tasks'] == 0
        assert service.is_drained()
    finally:
        service.shutdown()
