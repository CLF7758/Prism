"""Query-scoped, bounded evidence shared by directory delivery and renderers.

Counts describe operations, not proof that pixels reached a display. Observers
are optional and isolated from business logic; production keeps no ID sets.
"""
import copy
from collections import OrderedDict
import hashlib
import json
import logging
import os
import sys
import threading
import time
import uuid
import weakref

from PyQt6 import QtCore

logger = logging.getLogger(__name__)

_OBSERVERS = []
_SESSION_ID = uuid.uuid4().hex

#: Every query that is still in flight.  The set holds **weak** references:
#: a query nobody keeps alive is gone, and the set never keeps one alive by
#: itself.  It exists so that the shutdown path can give every in-flight
#: query a terminal state - a query that is never finished leaves the log
#: without an answer, and a log without an answer cannot tell "the user
#: closed the application" from "the product forgot to finish".
_LIVE_EVIDENCE = weakref.WeakSet()

#: The set is shared by the GUI thread and the directory workers.  The lock
#: is never held while an evidence method runs, so it cannot invert the
#: order of the evidence's own lock.
_LIVE_LOCK = threading.RLock()

#: The application whose quit signal terminates the live queries, and the
#: operation-log recorder whose own quit slot this module took over.
_SHUTDOWN = {'app': None, 'recorder': None}


def add_presentation_observer(callback):
    if callback not in _OBSERVERS:
        _OBSERVERS.append(callback)


def remove_presentation_observer(callback):
    try:
        _OBSERVERS.remove(callback)
    except ValueError:
        pass


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str,
                                     ensure_ascii=False).encode('utf-8')).hexdigest()[:24]


def _trace_session():
    module = sys.modules.get('prism.performance_trace')
    return getattr(getattr(module, '_active', None), 'session', None)


class _Counts:
    def __init__(self, diagnostic, budget):
        self.logical = 0
        self.ids = set() if diagnostic else None
        self.budget = budget
        self.exhausted = False
        self.lock = threading.RLock()

    def observe(self, ids):
        with self.lock:
            for key in ids:
                self.logical += 1
                if self.ids is not None and key is not None:
                    key = str(key)
                    if key not in self.ids:
                        if len(self.ids) < self.budget:
                            self.ids.add(key)
                        else:
                            self.exhausted = True

    def snapshot(self):
        with self.lock:
            return dict(logical_rows=self.logical,
                        unique_rows=len(self.ids) if self.ids is not None else None,
                        logical_rows_unit='row_accesses', logical_rows_cumulative=True,
                        logical_rows_deduplicated=False,
                        unique_rows_unit='asset_ids', unique_rows_cumulative=True,
                        unique_rows_deduplicated=True,
                        unique_rows_complete=self.ids is not None and not self.exhausted,
                        unique_rows_basis=('lower_bound' if self.exhausted else
                                           'exact' if self.ids is not None else 'not_collected'),
                        id_budget=self.budget, id_budget_exhausted=self.exhausted)


class QueryEvidence:
    """Public contract: identity, bounded counters and safe ``record``.

    ``final`` means this model installation finished, not that rendering is
    finished. A published evidence remains active until cancelled/replaced.
    ``observe(stage, ids)`` only aggregates; it never emits per-ID events.
    ``finish()`` gives the query its terminal state and says whether the
    query published a model - the log can prove that, so it may claim it.
    """
    def __init__(self, scope=None, *, source_revision=None, diagnostic=None,
                 id_budget=None, observer=None, query_id=None, session_id=None):
        self.active = True
        self.cancelled = False
        self.final = False
        self._terminating = False
        self._terminal_event = None
        self.diagnostic = (os.environ.get('PRISM_PRESENTATION_DIAGNOSTIC') == '1'
                           if diagnostic is None else bool(diagnostic))
        try:
            self.id_budget = max(0, int(id_budget if id_budget is not None else
                os.environ.get('PRISM_PRESENTATION_ID_BUDGET', '150000')))
        except (TypeError, ValueError):
            self.id_budget = 150000
        self.session_id = session_id or _trace_session() or _SESSION_ID
        self.query_id = query_id or uuid.uuid4().hex
        self.scope = json.loads(json.dumps(scope or {}, default=str))
        self.source_revision = source_revision or 'unknown'
        self.observer = observer
        self.read_id = uuid.uuid4().hex
        self.source_query_id = self.query_id
        self.read_source_revision = self.source_revision
        self.read_reused = False
        self._counts = {}
        self._lock = threading.RLock()
        self._last_emit = {}
        self._frame_trace_decisions = OrderedDict()
        self._trace_seen = _trace_session()
        self._baseline_states = {}
        self._read_this_query = 0
        #: True only when **this** query published a model.  The log can prove
        #: it through the ``asset_model_published`` event, so a terminal
        #: event may say so; a query that never published must not claim it.
        self._published = False
        with _LIVE_LOCK:
            _LIVE_EVIDENCE.add(self)

    def fields(self):
        return dict(schema_version=2, presentation_schema_version=1,
                    session_id=self.session_id, query_id=self.query_id,
                    scope=copy.deepcopy(self.scope), source_revision=self.source_revision,
                    read_id=self.read_id, source_query_id=self.source_query_id,
                    read_reused=self.read_reused, read_source_revision=self.read_source_revision,
                    cancelled=self.cancelled, final=self.final)

    def _counter(self, stage):
        with self._lock:
            counter = self._counts.get(stage)
            if counter is None:
                counter = self._counts[stage] = _Counts(self.diagnostic, self.id_budget)
            return counter

    def observe(self, stage, ids):
        try:
            with self._lock:
                if self.active and not self._terminating:
                    self._counter(stage).observe(ids)
        except Exception:
            pass

    def snapshot(self, stage):
        return self._counter(stage).snapshot()

    def reuse_read(self, previous):
        """Link a new query to a bounded replay, without rebasing old callbacks."""
        with self._lock:
            self.read_id = previous.read_id
            self.source_query_id = previous.source_query_id
            self.read_source_revision = previous.read_source_revision
            self.read_reused = True
            self._counts['read'] = previous._counter('read')

    def read_pulled(self, count):
        with self._lock:
            if self.active:
                self._read_this_query += count

    def reuse_install(self, previous):
        """Copy bounded prefix accounting without claiming a new physical read."""
        for stage in ('consumer_consumed', 'sequence_access'):
            old = previous._counter(stage)
            new = _Counts(self.diagnostic, self.id_budget)
            with old.lock:
                new.logical = old.logical
                if new.ids is not None and old.ids is not None:
                    new.ids = set(list(old.ids)[:new.budget])
                    new.exhausted = old.exhausted or len(old.ids) > new.budget
                elif new.ids is not None:
                    new.exhausted = bool(old.logical)
            self._counts[stage] = new

    def read_fields(self):
        result = self.snapshot('read')
        result.update(read_id=self.read_id, source_query_id=self.source_query_id,
                      read_reused=self.read_reused, read_source_revision=self.read_source_revision,
                      count_basis='read_id_lifetime',
                      read_logical_rows_this_query=self._read_this_query,
                      read_logical_rows_this_query_unit='row_reads',
                      read_logical_rows_this_query_cumulative=True,
                      read_logical_rows_this_query_deduplicated=False)
        result['logical_rows_unit'] = 'row_reads'
        result.update(rows_read=result['logical_rows'], rows_read_unit='row_reads',
                      rows_read_cumulative=True, rows_read_deduplicated=False)
        return result

    def delivery(self, stage, *, complete=False, final=False):
        fields = self.snapshot(stage)
        fields.update(stage=stage, read_id=self.read_id,
                      source_query_id=self.source_query_id, read_reused=self.read_reused,
                      read_source_revision=self.read_source_revision,
                      count_basis='query_lifetime', complete=bool(complete), final=bool(final))
        self.record('catalog_rows_delivered', **fields)

    def attach_trace(self):
        """Replay bounded state on a new transport, without inventing new work."""
        with self._lock:
            trace_session = _trace_session()
            if (not self.active or self._terminating or not trace_session
                    or trace_session == self._trace_seen):
                return
            previous = self._trace_seen
            self._trace_seen = trace_session
            states = copy.deepcopy(self._baseline_states)
            self.record('catalog_query_baseline', baseline_only=True,
                        baseline_rows_are_new_work=False, final=False,
                        complete=all(key in states for key in ('read', 'consumer', 'model')),
                        previous_trace_session_id=previous,
                        baseline_layers=list(states))
            for key, event in (('read', 'catalog_rows_read_evidence'),
                               ('consumer', 'catalog_rows_delivered'),
                               ('model', 'asset_model_published')):
                if key in states:
                    state = states[key]
                    state.update(baseline_only=True, baseline_rows_are_new_work=False,
                                 state_origin_trace_session_id=state.get('trace_session_id'))
                    self.record(event, **state)

    def _sample_trace(self, event, payload, trace_session, now):
        if payload.get('baseline_only') or payload['final']:
            return True
        if event in ('asset_paint_frame', 'asset_visibility_snapshot'):
            frame = payload.get('frame_id')
            key = trace_session, frame
            if frame is not None and key in self._frame_trace_decisions:
                emit = self._frame_trace_decisions[key]
            else:
                sample_key = 'paint_pair', trace_session
                emit = now - self._last_emit.get(sample_key, -1) >= .25
                if emit:
                    self._last_emit[sample_key] = now
                if frame is not None:
                    self._frame_trace_decisions[key] = emit
                    while len(self._frame_trace_decisions) > 64:
                        self._frame_trace_decisions.popitem(last=False)
            if event == 'asset_visibility_snapshot':
                self._frame_trace_decisions.pop(key, None)
            return emit
        progress = event in ('catalog_rows_read_evidence', 'catalog_rows_delivered',
                             'asset_model_install_progress', 'asset_resource_generated',
                             'asset_cache_installed')
        key = event, payload.get('stage'), trace_session
        emit = not progress or now - self._last_emit.get(key, -1) >= .25
        if emit:
            self._last_emit[key] = now
        return emit

    def record(self, event, **fields):
        """Observers see all facts; traces sample paint/visibility as one pair.

        Query identity never changes when a transport starts or switches.
        Completed state replays are explicitly marked, not fresh work.
        """
        try:
            with self._lock:
                if not self.active or (self._terminating and event != self._terminal_event):
                    return
                if not fields.get('baseline_only'):
                    self.attach_trace()
                if not self.active or (self._terminating and event != self._terminal_event):
                    return
                payload = dict(fields)
                payload.update(self.fields())
                # Per-event final is distinct from the installation's state.
                payload['final'] = bool(fields.get('final', self.final))
                payload.setdefault('complete', False)
                trace_session = _trace_session()
                payload['trace_session_id'] = trace_session
                if not payload.get('baseline_only'):
                    state = ('read' if event == 'catalog_rows_read_evidence'
                             else 'consumer' if event == 'catalog_rows_delivered'
                                and payload.get('stage') == 'consumer_consumed'
                             else 'model' if event == 'asset_model_published' else None)
                    if state and payload['final'] and payload['complete']:
                        self._baseline_states[state] = copy.deepcopy(payload)
                        if state == 'model':
                            # The log can now prove that this query published
                            # a model.  A later terminal event may say so -
                            # and only then.
                            self._published = True
                callbacks = list(_OBSERVERS)
                if self.observer is not None:
                    callbacks.append(self.observer)
                for callback in callbacks:
                    try:
                        callback(event, **copy.deepcopy(payload))
                    except Exception:
                        pass
                if not self.active or (self._terminating and event != self._terminal_event):
                    return  # Observer reentry may have cancelled this query.
                transport = _trace_session()
                if transport != trace_session:
                    self.attach_trace()
                    if not self.active or (self._terminating and event != self._terminal_event):
                        return
                    payload['trace_switch_during_observation'] = True
                    payload['trace_session_at_observation'] = trace_session
                    payload['trace_session_id'] = transport
                    trace_session = transport
                now = time.monotonic()
                emit = self._sample_trace(event, payload, trace_session, now)
                if emit:
                    module = sys.modules.get('prism.performance_trace')
                    if module is not None:
                        try:
                            wire = dict(payload)
                            if event in ('asset_paint_frame', 'asset_visibility_snapshot'):
                                wire['trace_sample_interval_ms'] = 250
                                clip = wire.get('effective_clip')
                                if isinstance(clip, list):
                                    wire['effective_clip'] = [[value for point in polygon for value in point]
                                                              for polygon in clip]
                                    wire['effective_clip_encoding'] = 'flat_xy_polygons'
                                    wire['effective_clip_trace_complete'] = (len(clip) <= 24 and
                                        all(len(polygon) <= 12 for polygon in clip))
                                regions = wire.pop('diagnostic_regions', None)
                                if regions is not None:
                                    wire['diagnostic_regions_in_trace'] = False
                                    wire['diagnostic_region_count'] = len(regions)
                                    wire['diagnostic_region_evidence_source'] = 'bounded_observer_capture'
                                ids = wire.get('diagnostic_ids')
                                if isinstance(ids, list):
                                    wire['diagnostic_ids_in_trace_complete'] = len(ids) <= 24
                            module.record(event, priority='terminal' if payload['final']
                                          else 'state', **wire)
                        except Exception:
                            pass
        except Exception:
            pass

    def finish(self, event, *, cancelled=False, **fields):
        """Give this query its terminal state.

        A query that published its model is **complete** for that query, even
        when it is cancelled afterwards: the published model is the answer
        the user is looking at, and a log that says "complete" and
        "not complete" about the same query is a log nobody can read.  A
        query that never published must not claim a model, so the terminal
        event carries ``published`` only when the log can prove it - the
        field is absent otherwise, not ``False``.

        Returns ``True`` when this call produced the terminal event.
        """
        with self._lock:
            if not self.active or self._terminating:
                return False
            self._terminating = True
            self._terminal_event = event
            self.cancelled = bool(cancelled)
            self.final = True
            published = self._published
            payload = dict(fields)
            requested = payload.pop('complete', None)
            # An explicit claim can never be stronger than what the log
            # proves: ``complete`` means "this query published its model".
            payload['complete'] = (published if requested is None
                                   else bool(requested) and published)
            if published:
                payload['published'] = True
            try:
                self.record(event, cancelled=self.cancelled, final=True, **payload)
            finally:
                self.active = False
                self._terminating = False
                with _LIVE_LOCK:
                    _LIVE_EVIDENCE.discard(self)
            return True


def live_queries():
    """The queries that are still in flight."""
    with _LIVE_LOCK:
        return list(_LIVE_EVIDENCE)


def terminate_live_queries(*, event='catalog_query_cancelled',
                           reason='application_shutdown'):
    """Give every in-flight query a terminal state.

    A query that is never finished leaves the log without an answer, and a
    log without an answer cannot tell "the user closed the application" from
    "the product forgot to finish".  The reason belongs to the caller: a
    query the user changed its mind about and a query that the session ended
    are different facts and must not share one reason.
    """
    terminated = 0
    for evidence in live_queries():
        try:
            if evidence.finish(event, cancelled=True, reason=reason):
                terminated += 1
        except Exception:
            logger.exception('Cannot terminate the in-flight query %s',
                             getattr(evidence, 'query_id', None))
    return terminated


def install_shutdown_hook(app=None):
    """Terminate the in-flight queries when the application quits.

    The operation log's recorder closes itself on ``aboutToQuit`` and stops
    accepting records when it does: a terminal record that is enqueued after
    that point is dropped, and the log would end without the answer this
    hook produces.  The recorder's own slot is therefore taken over here -
    it is disconnected and called **after** the terminal records are
    enqueued, so the log keeps its own order (``session_end`` is still the
    recorder's last word) and the terminal record is written while the
    writer still accepts records.

    Without a recorder there is no order to keep and the hook is a plain
    ``aboutToQuit`` slot.  The hook never raises into the event loop.
    """
    app = app or QtCore.QCoreApplication.instance()
    if app is None:
        return False
    if _SHUTDOWN.get('app') is app:
        return True
    _SHUTDOWN['app'] = app
    _SHUTDOWN['recorder'] = None
    app.aboutToQuit.connect(_terminate_on_quit)
    recorder = getattr(app, '_performance_trace', None)
    closer = getattr(recorder, 'close', None)
    if closer is not None:
        try:
            app.aboutToQuit.disconnect(closer)
        except (TypeError, RuntimeError):
            logger.warning('Cannot take over the operation log quit slot; '
                           'a shutdown record may not reach the log')
        else:
            _SHUTDOWN['recorder'] = recorder
    return True


def _terminate_on_quit():
    """The application's quit signal must not leave a query in flight."""
    try:
        terminate_live_queries(reason='application_shutdown')
    except Exception:
        logger.exception('Cannot terminate the in-flight queries at shutdown')
    finally:
        recorder = _SHUTDOWN.get('recorder')
        if recorder is not None:
            try:
                recorder.close()
            except Exception:
                logger.exception('Cannot close the operation log at shutdown')


def model_item_id(item):
    """Diagnostic only. Domain-qualify search hits and ID-less live objects."""
    if isinstance(item, dict):
        return 'search:' + fingerprint(item)
    key = getattr(item, 'save_id', None)
    if key is None:
        key = getattr(item, 'original_save_id', None)
    return 'asset:' + str(key) if key is not None else 'live:' + str(id(item))
