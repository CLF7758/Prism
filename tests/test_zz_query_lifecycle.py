"""The lifecycle of a catalog query in the operation log.

Two facts must hold for **every** query:

* the log must not contradict itself.  A query that published a model is
  complete for that query, also when it is cancelled afterwards - otherwise
  the same ``query_id`` says "complete" and "not complete" and nobody can
  read the answer.  A query that never published must not claim a model.
* every query must reach a terminal state, also when the session ends while
  the query is still in flight.  A query that hangs leaves the log without an
  answer, and a log without an answer cannot tell "the user closed the
  application" from "the product forgot to finish".
"""
import json
import weakref

import pytest

from PyQt6 import QtCore

from prism import presentation_evidence
from prism.presentation_evidence import (
    QueryEvidence, install_shutdown_hook, terminate_live_queries)


class FakeApp(QtCore.QObject):
    """A stand-in for the application: only the quit signal is used."""
    aboutToQuit = QtCore.pyqtSignal()


class FakeRecorder(QtCore.QObject):
    """A stand-in for the operation log's recorder.

    It behaves like the real one at the one point that matters: it stops
    accepting records when it is closed, and it says so in the shared order
    list so that the test can see which slot ran first.
    """
    def __init__(self, order):
        super().__init__()
        self.order = order
        self.accepting = True

    @QtCore.pyqtSlot()
    def close(self):
        self.order.append('recorder.close')
        self.accepting = False


@pytest.fixture
def isolated_queries(monkeypatch):
    """Only this test's queries are in flight, and the hook is re-armed."""
    monkeypatch.setattr(presentation_evidence, '_LIVE_EVIDENCE', weakref.WeakSet())
    monkeypatch.setitem(presentation_evidence._SHUTDOWN, 'app', None)
    monkeypatch.setitem(presentation_evidence._SHUTDOWN, 'recorder', None)
    yield


def observed(events):
    evidence = QueryEvidence(
        dict(mode='all'),
        observer=lambda event, **fields: events.append((event, fields)))
    return evidence


# ---------------------------------------------------------------- the cancel path

def test_cancel_after_publication_is_complete_and_says_published(qapp):
    """The published model is the answer: the terminal event must say so."""
    from prism.widgets.asset_browser import AssetModel
    events = []
    evidence = observed(events)
    model = AssetModel()
    assert model.replace([dict(title='a'), dict(title='b')], evidence=evidence)
    assert evidence.finish('catalog_query_cancelled', cancelled=True,
                           reason='cancel_or_switch')
    event, fields = events[-1]
    assert event == 'catalog_query_cancelled'
    assert fields['complete'] is True
    assert fields['published'] is True
    assert fields['reason'] == 'cancel_or_switch'
    assert fields['cancelled'] is True and fields['final'] is True
    assert fields['query_id'] == evidence.query_id
    # The log must not contradict itself: one query, one answer.
    published = [f for kind, f in events if kind == 'asset_model_published']
    assert len(published) == 1 and published[0]['complete'] is True


def test_cancel_before_publication_claims_no_model(qapp):
    """A query that never published must not claim that it did."""
    events = []
    evidence = observed(events)
    assert evidence.finish('catalog_query_cancelled', cancelled=True,
                           reason='cancel_or_switch')
    event, fields = events[-1]
    assert event == 'catalog_query_cancelled'
    assert fields['complete'] is False
    assert 'published' not in fields
    assert fields.get('published') is not True
    assert fields['reason'] == 'cancel_or_switch'
    assert fields['cancelled'] is True and fields['final'] is True


def test_the_ordinary_cancel_gains_no_field(qapp):
    """The ordinary cancellation must look exactly like it looked before.

    The terminal payload of a query that never published carries the fields
    it always carried - no ``published``, nothing else.
    """
    events = []
    evidence = observed(events)
    assert evidence.finish('catalog_query_cancelled', cancelled=True,
                           reason='cancel_or_switch')
    event, fields = events[-1]
    assert set(fields) == set(evidence.fields()) | {'complete', 'trace_session_id', 'reason'}
    assert fields['complete'] is False
    assert events[-1][0] == 'catalog_query_cancelled'
    assert len(events) == 1  # One terminal event, no extra work on the way.


def test_a_published_query_stays_complete_when_it_is_cancelled(qapp):
    """The contradiction the log used to write is gone."""
    from prism.widgets.asset_browser import AssetModel
    events = []
    evidence = observed(events)
    model = AssetModel()
    model.replace([dict(title='a')], evidence=evidence)
    evidence.finish('catalog_query_cancelled', cancelled=True, reason='cancel_or_switch')
    completions = {f['complete'] for kind, f in events
                   if kind in ('asset_model_published', 'catalog_query_cancelled')}
    assert completions == {True}


def test_the_ordinary_cancel_asks_no_disk_no_database_no_registry(qapp, monkeypatch):
    """The ordinary cancellation must not look anything up.

    The terminal state is a fact the evidence object already holds, so the
    cancel path may not read the disk, open a database or ask the registry.
    """
    import os
    import sqlite3

    calls = []

    def forbidden(name):
        def guard(*args, **kwargs):
            calls.append(name)
            raise AssertionError('the cancel path must not use ' + name)
        return guard

    monkeypatch.setattr(sqlite3, 'connect', forbidden('sqlite'))
    monkeypatch.setattr(os, 'scandir', forbidden('scandir'))
    monkeypatch.setattr(os, 'listdir', forbidden('listdir'))
    try:
        import winreg
    except ImportError:
        winreg = None
    if winreg is not None:
        monkeypatch.setattr(winreg, 'OpenKey', forbidden('registry'))
    events = []
    evidence = observed(events)
    assert evidence.finish('catalog_query_cancelled', cancelled=True,
                           reason='cancel_or_switch')
    assert calls == []
    assert events[-1][1]['complete'] is False
    assert 'published' not in events[-1][1]


# ----------------------------------------------------------------- the shutdown

def test_shutdown_gives_the_in_flight_query_a_shutdown_reason(isolated_queries):
    """An in-flight query must not hang when the session ends."""
    app = FakeApp()
    events = []
    evidence = observed(events)
    assert evidence in presentation_evidence.live_queries()
    install_shutdown_hook(app)
    app.aboutToQuit.emit()
    event, fields = events[-1]
    assert event == 'catalog_query_cancelled'
    assert fields['reason'] == 'application_shutdown'
    assert fields['cancelled'] is True and fields['final'] is True
    assert fields['complete'] is False
    assert 'published' not in fields
    assert not evidence.active and evidence.cancelled


def test_shutdown_and_user_cancellation_use_different_reasons(isolated_queries):
    """The user changed its mind; the session ended.  Two different facts."""
    app = FakeApp()
    events = []
    evidence = observed(events)
    install_shutdown_hook(app)
    app.aboutToQuit.emit()
    assert events[-1][1]['reason'] == 'application_shutdown'
    other = observed(events)
    other.finish('catalog_query_cancelled', cancelled=True, reason='cancel_or_switch')
    assert events[-1][1]['reason'] == 'cancel_or_switch'
    assert evidence.query_id != other.query_id


def test_the_shutdown_record_is_produced_before_the_log_stops_recording(isolated_queries):
    """The log's recorder stops accepting records when it closes.

    The terminal record has to be produced **before** that, otherwise it is
    dropped and the log ends without the answer.  The recorder connects its
    own quit slot first - exactly like the real one - and the hook has to
    take that slot over.
    """
    app = FakeApp()
    order = []
    recorder = FakeRecorder(order)
    app._performance_trace = recorder
    app.aboutToQuit.connect(recorder.close)
    events = []

    def watch(event, **fields):
        order.append('terminal')
        events.append((event, fields))

    evidence = QueryEvidence(dict(mode='all'), observer=watch)
    install_shutdown_hook(app)
    app.aboutToQuit.emit()
    assert order == ['terminal', 'recorder.close']
    assert recorder.accepting is False
    assert events[-1][1]['reason'] == 'application_shutdown'
    assert not evidence.active


def test_the_shutdown_record_reaches_the_real_log_file(
        qapp, tmp_path, monkeypatch, isolated_queries):
    """The terminal record must be **in the log**, before ``session_end``.

    This is the real recorder and the real file.  The recorder connects its
    own quit slot first - exactly like the real application - and the hook
    has to take that slot over: the record is enqueued while the writer
    still accepts records, the recorder closes afterwards, and the file
    carries both facts in that order.
    """
    from prism import performance_trace
    trace = performance_trace.PerformanceTrace(qapp, None, str(tmp_path))
    monkeypatch.setattr(performance_trace, '_active', trace)
    app = FakeApp()
    app._performance_trace = trace
    app.aboutToQuit.connect(trace.close)
    install_shutdown_hook(app)
    evidence = QueryEvidence(dict(mode='all'))
    app.aboutToQuit.emit()
    try:
        trace.close()
    except Exception:
        pass
    records = [json.loads(line)
               for line in trace.path.read_text(encoding='utf-8').splitlines()
               if line.strip()]
    terminal = [r for r in records if r.get('event') == 'catalog_query_cancelled']
    assert len(terminal) == 1
    assert terminal[0]['reason'] == 'application_shutdown'
    assert terminal[0]['complete'] is False
    assert terminal[0]['query_id'] == evidence.query_id
    end = [r for r in records if r.get('event') == 'session_end']
    assert end, 'the log must still end with the recorder own session end'
    assert terminal[0]['sequence'] < end[0]['sequence']


def test_terminate_live_queries_counts_only_the_queries_it_finished(isolated_queries):
    events = []
    first = observed(events)
    second = observed(events)
    assert terminate_live_queries(reason='application_shutdown') == 2
    assert not first.active and not second.active
    assert terminate_live_queries(reason='application_shutdown') == 0


def test_the_browser_installs_the_shutdown_hook(main_window, qapp):
    """The browser owns the catalog queries, so it installs the hook."""
    assert presentation_evidence._SHUTDOWN['app'] is qapp
