"""Cancellation stops obsolete opening baselines without returning partial state."""
import threading
from unittest.mock import Mock

import pytest

from prism import paged_open_preparation as preparation
from prism import partial_project
from prism.fileio.partial_save import PartialReadSnapshot, PartialSaveCancelled
from tests.test_partial_project import project, png


def test_cancel_before_baseline_does_not_read(monkeypatch):
    read = Mock()
    monkeypatch.setattr(partial_project, 'read_partial_records', read)
    with pytest.raises(PartialSaveCancelled, match='cancelled'):
        partial_project.read_baseline('not-opened.prism', [('items', 1)], cancel=lambda: True)
    read.assert_not_called()


def test_cancel_between_baseline_batches_stops_next_read(monkeypatch):
    cancelled = threading.Event()
    batches = []
    def read(source, references):
        batches.append(tuple(references))
        cancelled.set()
        return PartialReadSnapshot(source, (1, 2), 'p', 1, ())
    monkeypatch.setattr(partial_project, 'read_partial_records', read)
    with pytest.raises(PartialSaveCancelled):
        partial_project.read_baseline('p', [('items', key) for key in range(2049)],
                                      cancel=cancelled.is_set)
    assert len(batches) == 1
    assert len(batches[0]) == 1024


def test_session_domain_reader_receives_cancel_and_cannot_return_half_session(project, monkeypatch):
    from prism import partial_domains
    cancelled = threading.Event()
    received = []
    def read_domains(source, *, cancel):
        received.append(cancel)
        cancelled.set()
        raise PartialSaveCancelled('Domain baseline cancelled')
    monkeypatch.setattr(partial_domains, 'read_domain_baseline', read_domains)
    with pytest.raises(PartialSaveCancelled, match='Domain baseline'):
        partial_project.PartialProjectSession.open(project, item_ids=[1],
            include_domains=True, cancel=cancelled.is_set)
    assert received == [cancelled.is_set]


def test_prepare_session_stops_before_stamp_or_source_access(monkeypatch):
    stamp = Mock()
    monkeypatch.setattr(preparation, 'require_stamp', stamp)
    with pytest.raises(PartialSaveCancelled):
        preparation.prepare_session('absent.prism', [], [], None, lambda: True)
    stamp.assert_not_called()


def test_worker_cancel_context_reaches_transparent_wrapper_and_is_reset(monkeypatch):
    cancel = threading.Event()
    seen = []
    def session(cls, *args, **kwargs):
        seen.append(kwargs['cancel'])
        cancel.set()
        return object()
    monkeypatch.setattr(partial_project.PartialProjectSession, 'open', classmethod(session))
    monkeypatch.setattr(preparation, 'require_stamp', lambda *args: None)
    delivery = Mock()
    monkeypatch.setattr(preparation, '_deliver', delivery)
    def wrapper(*args):
        return preparation.prepare_session(*args)
    preparation._Task(1, 'session', wrapper, ('p', [], [], None), cancel, object()).run()
    assert len(seen) == 1 and seen[0]()
    payload = delivery.call_args.args[1]
    assert payload[2] is None
    assert isinstance(payload[3], PartialSaveCancelled)
    assert preparation._task_cancel.get() is None


def test_worker_generic_session_callable_contract_unchanged(monkeypatch):
    delivery = Mock()
    monkeypatch.setattr(preparation, '_deliver', delivery)
    value = object()
    preparation._Task(1, 'session', lambda: value, (), threading.Event(), object()).run()
    assert delivery.call_args.args[1][2] is value
    assert preparation._task_cancel.get() is None
