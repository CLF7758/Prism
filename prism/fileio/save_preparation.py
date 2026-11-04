"""Budgeted GUI capture followed by worker-owned save preparation.

The full writer still owns the existing canvas persistence protocol. Partial
preparation and writing use detached item projections, never graphics objects.

This module also owns the save *result* contract: one immutable
``SaveOutcome`` per operation, one operation identity, and the autosave
policy. The writer itself only reports plain facts about its transaction; the
outcome is assembled here, in the save adapter layer, so that the generic
``ThreadedIO.finished(filename, errors)`` signature stays untouched.
"""
import os
import threading
import time
from time import monotonic as _capture_monotonic
import uuid
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from PyQt6 import QtCore

from prism.fileio import ThreadedIO, logger
from prism.fileio.save_snapshot import BoardSaveSnapshot
from prism.fileio.sliced_copy import sliced_copy
from prism.fileio.partial_save import NewItem, PartialReadSnapshot, PartialSaveCancelled, PartialSaveConflict
from prism.image_source import ImageSourceMixin
from prism.performance_trace import operation, record, mode, MODE_DIAGNOSTIC


#: The five phases a save moves through. ``accepted`` is emitted on the GUI
#: thread inside ``do_save``; the rest are emitted by the worker.
PHASE_ACCEPTED = 'accepted'
PHASE_PREPARING = 'preparing'
PHASE_WRITING = 'writing'
PHASE_CONFIRMING = 'confirming'
PHASE_END = 'end'

#: The terminal states. They are not interchangeable: "the user cancelled"
#: and "the write failed" are different facts, and "the bytes are on disk but
#: nobody confirmed them" is a third one.
TERMINAL_CONFIRMED = 'confirmed'
TERMINAL_CANCELLED = 'cancelled'
TERMINAL_REFUSED = 'refused'
TERMINAL_FAILED = 'failed'
TERMINAL_WRITTEN_UNCONFIRMED = 'written_unconfirmed'

#: What happened to the transaction. Independent of the acknowledgement.
COMMIT_NOT_STARTED = 'not_started'
COMMIT_ROLLED_BACK = 'rolled_back'
COMMIT_COMPLETE = 'committed_complete'
COMMIT_INCOMPLETE = 'incomplete'
COMMIT_UNKNOWN = 'unknown'

#: Whether the GUI could apply the result to the live session.
ACK_CONFIRMED = 'confirmed'
ACK_FAILED = 'failed'
ACK_NOT_APPLICABLE = 'not_applicable'

#: Whether the result was delivered to the project the user is looking at.
#: A stale result is still a real result: the old file may well have been
#: written. "stale" describes the delivery, never the disk.
DELIVERY_CURRENT = 'current'
DELIVERY_STALE_IGNORED = 'stale_ignored'


@dataclass(frozen=True)
class SourceFingerprint:
    """A cheap identification of a file, not a proof of its content.

    The fingerprint is deliberately *not* a full hash: hashing a 55 GiB
    project on every save would cost more than the save itself. It identifies
    the input for the protection record; it never proves that the logical
    content is complete.
    """
    path: str
    exists: bool
    size: int | None = None
    mtime_ns: int | None = None

    @classmethod
    def of(cls, path):
        try:
            stat = os.stat(path)
        except OSError:
            return cls(path=str(path), exists=False)
        return cls(path=str(path), exists=True, size=stat.st_size,
                   mtime_ns=stat.st_mtime_ns)

    def as_dict(self):
        return {'path': self.path, 'exists': self.exists, 'size': self.size,
                'mtime_ns': self.mtime_ns}


@dataclass(frozen=True)
class SaveOutcome:
    """The immutable result of one save operation.

    Every consumer (the GUI, the catalog refresh, AI-03's counts and AI-06's
    query cache) reads the same object. Three axes are kept apart on purpose:

    * ``commit_state`` -- what happened to the transaction on disk.
    * ``ack_state``    -- whether the GUI could apply the result.
    * ``ui_delivery``  -- whether the result reached the project the user is
      looking at now.

    A late result for a project the user has already left is *stale* for the
    UI and still *committed* for the disk. Collapsing the two would either
    hide a real write or invent one.
    """
    operation_id: str
    project_id: str
    open_epoch: str
    target: str
    captured_revision: tuple
    mode: str                       # 'full' or 'partial'
    commit_state: str
    ack_state: str
    terminal: str
    source_before: SourceFingerprint | None
    source_after: SourceFingerprint | None
    changed_entities: tuple | None
    changed_entities_complete: bool
    #: How many rows the write touched. A count is NOT an identity list and
    #: it is NOT a complete change set. Kept for compatibility with the
    #: partial receipt's ``changed_rows``.
    changed_row_count: int | None = None
    ui_delivery: str = DELIVERY_CURRENT
    cancel_too_late: bool = False
    reason: str = ''
    error: str = ''

    @property
    def confirmed(self):
        return self.terminal == TERMINAL_CONFIRMED

    @property
    def wrote_bytes(self):
        return self.commit_state in (COMMIT_COMPLETE, COMMIT_INCOMPLETE)

    def as_dict(self):
        return {
            'operation_id': self.operation_id,
            'project_id': self.project_id,
            'open_epoch': self.open_epoch,
            'target': self.target,
            'captured_revision': list(self.captured_revision),
            'mode': self.mode,
            'commit_state': self.commit_state,
            'ack_state': self.ack_state,
            'terminal': self.terminal,
            'source_before': self.source_before.as_dict() if self.source_before else None,
            'source_after': self.source_after.as_dict() if self.source_after else None,
            # None means "not known". An empty tuple would claim that nothing
            # changed, which is a different claim and a wrong one.
            'changed_entities': (list(self.changed_entities)
                                 if self.changed_entities is not None else None),
            'changed_entities_complete': self.changed_entities_complete,
            'changed_row_count': self.changed_row_count,
            'ui_delivery': self.ui_delivery,
            'cancel_too_late': self.cancel_too_late,
            'reason': self.reason,
            'error': self.error,
        }


class SaveWorker(ThreadedIO):
    """The save thread, plus the two save-specific signals.

    ``ThreadedIO`` keeps its generic ``finished(filename, errors)`` contract
    so that opening and exporting are unaffected. The save layer adds what a
    save needs and nothing else:

    * ``commit_started`` -- the writer entered the region in which the
      transaction can no longer be rolled back. The GUI disables Cancel here.
    * ``cancel_too_late`` -- a cancel arrived after the commit was no longer
      interruptible; the write finished and the result says so.
    """
    commit_started = QtCore.pyqtSignal()
    cancel_too_late = QtCore.pyqtSignal()


@dataclass
class AutosavePolicy:
    """How the automatic save behaves when it keeps failing.

    A failed automatic save has to be visible, but it must not shout on every
    tick and it must not retry forever in silence. The policy counts the
    failures, widens the interval between attempts and announces the problem
    on a widening cadence.
    """
    # Retry base, separate from the idle/maximum-edit deadlines below.
    base_interval_ms: int = 30000
    backoff_factor: int = 2
    max_backoff: int = 8
    notice_after: int = 1
    failures: int = 0
    last_error: str = ''
    last_announced_failures: int = 0

    def record_success(self):
        recovered = self.failures
        self.failures = 0
        self.last_error = ''
        self.last_announced_failures = 0
        return recovered

    def record_failure(self, reason):
        self.failures += 1
        self.last_error = str(reason)
        return self.failures

    @property
    def backoff(self):
        if self.failures <= 0:
            return 1
        return min(self.backoff_factor ** self.failures, self.max_backoff)

    def next_interval_ms(self):
        return self.base_interval_ms * self.backoff

    def should_announce(self):
        """True when this failure has to become visible to the user.

        The first announcements are rare on purpose: the notice widens with
        the failure count, so a permanent failure stays visible without
        repeating the same sentence on every attempt.
        """
        if self.failures < self.notice_after or self.failures == self.last_announced_failures:
            return False
        overdue = self.failures - self.notice_after
        if overdue % 2 == 0:
            self.last_announced_failures = self.failures
            return True
        return False

    def reset(self):
        """Forget the failures. Used when the project changes."""
        self.failures = 0
        self.last_error = ''
        self.last_announced_failures = 0


@dataclass
class AutosaveSchedule:
    """Observe revisions, rather than scanning resources or queuing saves."""
    idle_seconds: float = 30
    max_seconds: float = 120
    token: object = None
    first_dirty: float | None = None
    last_change: float = 0
    retry_at: float = 0

    def observe(self, token, dirty, now):
        if not dirty:
            self.token = token
            self.first_dirty = None
            self.retry_at = 0
            return False
        if self.first_dirty is None:
            self.first_dirty = now
            self.last_change = now
        if token != self.token:
            self.token = token
            self.last_change = now
        return (now >= self.retry_at and
                (now-self.last_change >= self.idle_seconds or
                 now-self.first_dirty >= self.max_seconds))

    def attempted(self, now, delay):
        self.retry_at = now + delay

    def reset(self):
        self.token = None
        self.first_dirty = None
        self.retry_at = 0


class _Point:
    def __init__(self, x, y):
        self._x, self._y = x, y

    def x(self):
        return self._x

    def y(self):
        return self._y


class CapturedItem(ImageSourceMixin):
    """Private plain-data item, including original source descriptors."""
    @property
    def _tags(self):
        return self.data.get('tags', ())

    @property
    def _categories(self):
        return self.data.get('categories', ())

    def pos(self):
        return _Point(*self.geometry[:2])

    def zValue(self):
        return self.geometry[2]

    def scale(self):
        return self.geometry[3]

    def rotation(self):
        return self.geometry[4]

    def flip(self):
        return self.geometry[5]

    def get_extra_save_data(self):
        return self.data

    def pixmap_to_bytes(self):
        return self._generated_source

    def captured_filename_for_export(self, imgformat, item_id):
        return self.get_filename_for_export(imgformat, save_id_default=item_id)

    def get_filename_for_export(self, imgformat, save_id_default=None):
        # This is worker-private state. A duplicate with an existing save_id
        # must use the INSERT's new key, not collide with the original name.
        if save_id_default is not None:
            self.save_id = save_id_default
        if self.TYPE == 'video':
            from prism.items import PrismVideoItem
            return PrismVideoItem.get_filename_for_export(self, imgformat, save_id_default)
        if self.TYPE == 'glb':
            from prism.items import PrismGlbItem
            return PrismGlbItem.get_filename_for_export(self, imgformat, save_id_default)
        return super().get_filename_for_export(imgformat, save_id_default)

    def _ensure_materialized(self):
        if self._video_source_path or not self._prism_source:
            return
        from prism.source_store import retain_archive_video
        suffix = os.path.splitext(self.filename or '')[1] or '.mp4'
        self._video_source_path = retain_archive_video(
            self._prism_source, suffix, cancel=self._cancel)


def _capture_item(item):
    from prism.item_lifecycle import logical_item
    item = logical_item(item)
    result = CapturedItem()
    result.TYPE = item.TYPE
    result.save_id = getattr(item, 'save_id', None)
    result.original_save_id = getattr(item, 'original_save_id', None)
    pos = item.pos()
    result.geometry = (pos.x(), pos.y(), item.zValue(), item.scale(), item.rotation(), item.flip())
    result.data = yield from sliced_copy(item.get_extra_save_data())
    for name, default in (('filename', None), ('_canvas_id', None),
                          ('_source_blob', None), ('_source_path', None),
                          ('_source_format', None), ('_prism_source', None),
                          ('_video_blob', None), ('_video_source_path', None),
                          ('_glb_blob', None), ('_is_reference', False)):
        setattr(result, name, (yield from sliced_copy(getattr(item, name, default))))
    result._generated_source = (None, None)
    # Generated images have no imported original to read in the worker. Their
    # existing fallback encoder requires Qt and therefore stays in GUI capture.
    if result.TYPE == 'pixmap' and not (
            result._source_blob or result._source_path or result._prism_source):
        from prism.image_source import generated_png_bytes
        result._generated_source = generated_png_bytes(item)
        result._source_blob, result._source_format = result._generated_source
    yield
    return result


def refused_outcome(view, filename, reason, *, silent=False, mode='full'):
    """The result of a save that was refused before it was ever accepted.

    A refusal is a result too. It gets the same identity, the same three
    axes and the same place in the log as a save that was accepted and then
    failed, so that "nothing happened" is a recorded fact rather than a
    silence.
    """
    scene = view.scene
    epoch = getattr(view, '_trace_open_epoch', 0) or 0
    return SaveOutcome(
        operation_id=uuid.uuid4().hex,
        project_id=str(getattr(scene, 'project_id', '')),
        open_epoch=str(epoch),
        target=os.path.normcase(os.path.abspath(str(filename))),
        captured_revision=(getattr(view, '_content_revision', 0),
                           view.undo_stack.index(),
                           getattr(view, '_undo_revision', 0)),
        mode=mode,
        commit_state=COMMIT_NOT_STARTED,
        ack_state=ACK_NOT_APPLICABLE,
        terminal=TERMINAL_REFUSED,
        source_before=SourceFingerprint.of(filename),
        # The target was not touched, so there is no "after" to report.
        source_after=None,
        # Nothing was written, so the changed set is not merely unknown: the
        # operation never started. It is still not an empty set of changes.
        changed_entities=None,
        changed_entities_complete=False,
        ui_delivery=DELIVERY_CURRENT,
        cancel_too_late=False,
        reason=str(reason),
        error='',
    )


class SavePreparation(QtCore.QObject):
    """One operation/worker identity from capture through GUI acknowledgment."""
    prepared = QtCore.pyqtSignal(object)
    phase_changed = QtCore.pyqtSignal(str)
    SLICE_SECONDS = .004
    #: How many capture steps one GUI turn may advance.
    #:
    #: The *time* budget is the real limit: a slice ends as soon as it has
    #: spent ``SLICE_SECONDS``.  The step count used to be the binding
    #: constraint instead, and that made the capture pay one event-loop
    #: round trip per 256 steps.  A 100k-material project needs ~900 000
    #: steps, so the capture made ~3 500 round trips to do 451 ms of copying
    #: — measured 7.77 s of wall clock for the copy alone.  With the step
    #: cap high enough to let the time budget decide, the same copy takes
    #: ~113 slices instead of ~3 500.
    SLICE_STEPS = 16384

    def __init__(self, view, filename, create_new, silent, session=None):
        super().__init__(view)
        self.view, self.scene = view, view.scene
        self.filename, self.create_new, self.silent = filename, create_new, silent
        self.session = session
        self.project_id = self.scene.project_id
        self.revision = self._revision()
        self.ready, self.accepted = threading.Event(), threading.Event()
        self.error = None
        self.board = self.partial = None
        self.canonical = {}
        self.worker = None
        self._receipt_finished = False
        self._worker_stopped = False
        self._capture_released = False
        #: One identity for the whole operation. It is unique across process
        #: sessions and across project reopens, so a late message can never
        #: be mistaken for a current one.
        self.operation_id = uuid.uuid4().hex
        # The epoch of the open this save belongs to. It changes whenever a
        # project is opened again, so a result that arrives after the user
        # left the project can be recognised as stale.
        epoch = getattr(view, '_trace_open_epoch', 0) or 0
        if session is not None:
            self.open_epoch = f'{epoch}:{getattr(session, "source_identity", "")}'
        else:
            self.open_epoch = str(epoch)
        self.source_before = SourceFingerprint.of(filename)
        self._capture_clock = time.monotonic()
        self._capture_last_end = self._capture_clock
        self._capture_work_s = self._capture_wait_s = 0.0
        self._capture_max_slice_s = self._capture_max_wait_s = 0.0
        self._capture_slices = self._capture_steps = 0
        self._capture_slice_samples = self._capture_slow_slices = 0
        self._capture_sample_mode = mode()
        self._capture_sample_limit = 64 if self._capture_sample_mode == MODE_DIAGNOSTIC else 16
        self._capture_last_sample = float('-inf')
        self._capture_last_sample_stage = None
        self._capture_max_slice_stage = self._capture_max_slice_stage_end = None
        self._capture_stage = 'board'
        self._steps = self._collect()
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._drain)
        self.prepared.connect(self._accept)
        self.phase_changed.connect(self._publish_phase)

    @QtCore.pyqtSlot(str)
    def _publish_phase(self, phase):
        if self._owned() and not self.silent:
            self.view._show_save_phase(phase)

    def _revision(self):
        return (getattr(self.view, '_content_revision', 0),
                self.view.undo_stack.index(), getattr(self.view, '_undo_revision', 0))

    def _owned(self):
        return (getattr(self.view, '_save_operation', None) is self
                and self.view.worker is self.worker
                and self.view.scene is self.scene
                and self.scene.project_id == self.project_id
                and getattr(self.scene, '_partial_project_session', None) is self.session)

    def start_capture(self):
        # The capture is the preparing stage. It is announced here, when it
        # really starts, and not only when the request was accepted: the two
        # are different moments and the user must be able to tell them.
        self.phase_changed.emit(PHASE_PREPARING)
        self.timer.start(0)

    def _drain(self):
        slice_start = getattr(time, 'monotonic', _capture_monotonic)()
        gap = max(0.0, slice_start - self._capture_last_end)
        self._capture_wait_s += gap
        self._capture_max_wait_s = max(self._capture_max_wait_s, gap)
        stage = self._capture_stage
        steps = 0
        finished = False
        try:
            if self.worker.canceled:
                raise PartialSaveCancelled('Save preparation cancelled')
            if not self._owned() or self._revision() != self.revision:
                raise PartialSaveConflict('Project changed during save capture; retry saving')
            start = time.perf_counter()
            for _ in range(self.SLICE_STEPS):
                next(self._steps)
                steps += 1
                if time.perf_counter() - start >= self.SLICE_SECONDS:
                    break
            self.timer.start(0)
        except StopIteration:
            finished = True
            self._steps = None
            if not self._owned() or self._revision() != self.revision:
                self.error = PartialSaveConflict('Project changed during save capture; retry saving')
            self.ready.set()
        except Exception as error:
            finished = True
            self.error = error
            # A suspended yield-from chain owns its copy memo and all partial
            # captures. Cancellation must release it before signaling ready.
            capture_steps, self._steps = self._steps, None
            try:
                if capture_steps is not None:
                    capture_steps.close()
            except Exception:
                logger.exception('Could not close cancelled save capture')
            self.ready.set()
        finally:
            end = getattr(time, 'monotonic', _capture_monotonic)()
            work = max(0.0, end - slice_start)
            self._capture_last_end = end
            self._capture_work_s += work
            if work >= self._capture_max_slice_s:
                self._capture_max_slice_s = work
                self._capture_max_slice_stage = stage
                self._capture_max_slice_stage_end = self._capture_stage
            self._capture_slices += 1
            self._capture_steps += steps
            # Work is actual GUI callback time; the gaps include event-loop
            # scheduling and other activity and are not claimed as blocking.
            try:
                slow = work >= .016 or gap >= .050
                self._capture_slow_slices += int(slow)
                stage_changed = stage != self._capture_last_sample_stage
                interval = .1 if self._capture_sample_mode == MODE_DIAGNOSTIC else .25
                if (self._capture_slice_samples < self._capture_sample_limit and
                        (stage_changed or (slow and end-self._capture_last_sample >= interval))):
                    self._capture_slice_samples += 1
                    self._capture_last_sample = end
                    self._capture_last_sample_stage = stage
                    record('project_save_capture_slice', priority='sample',
                           operation_id=self.operation_id,
                           stage=stage, stage_end=self._capture_stage, steps=steps,
                           work_ms=round(work * 1000, 3), wait_ms=round(gap * 1000, 3),
                           slice=self._capture_slices, finished=finished,
                           sampling='bounded_slow_or_stage_change')
                if finished:
                    record('project_save_capture', priority='terminal', operation_id=self.operation_id,
                           work_ms=round(self._capture_work_s * 1000, 3),
                           wait_ms=round(self._capture_wait_s * 1000, 3),
                           elapsed_ms=round((end-self._capture_clock) * 1000, 3),
                           max_slice_ms=round(self._capture_max_slice_s * 1000, 3),
                           max_wait_ms=round(self._capture_max_wait_s * 1000, 3),
                           slices=self._capture_slices, steps=self._capture_steps,
                            slow_slices=self._capture_slow_slices,
                            slice_samples=self._capture_slice_samples,
                            slice_samples_basis='record_attempts',
                            slices_coalesced=self._capture_slices-self._capture_slice_samples,
                            slice_sample_limit=self._capture_sample_limit,
                            sampling='bounded_slow_or_stage_change',
                            max_slice_stage=self._capture_max_slice_stage,
                            max_slice_stage_end=self._capture_max_slice_stage_end,
                           status='failed' if self.error is not None else 'captured')
            except Exception:
                pass  # Diagnostics must not change capture or release semantics.

    def _collect(self):
        from prism.partial_project import _assets
        from prism.item_lifecycle import logical_item
        # Navigation can materialize a document without changing content/undo
        # revision. Pin its load markers BEFORE yielding while copying bodies;
        # an older empty body must not acquire the later "already loaded" flag.
        deferred = set(getattr(self.scene, '_deferred_board_extras', ()) or ())
        deferred_pages = set(getattr(self.scene, '_deferred_page_ids', ()) or ())
        extras_source = getattr(self.scene, '_board_extras_source', None)
        self.board = yield from BoardSaveSnapshot.capture_steps(self.scene, defer_tag_system=True)
        state = self.board.state
        state._deferred_board_extras = deferred
        state._deferred_page_ids = deferred_pages
        state._board_extras_source = extras_source
        state.readonly_project = getattr(self.scene, 'readonly_project', False)
        state.readonly_project_path = getattr(self.scene, 'readonly_project_path', None)
        if self.session is None:
            from prism.storage.managed import is_directory_target, root_for
            original=getattr(self.view,'filename',None)
            if is_directory_target(self.filename) or root_for(self.filename) is not None:
                if original:self._managed_existing_source=str(original)
                else:self._managed_fresh_scene=True
                items=[]
                for item in _assets(self.scene):
                    captured=yield from _capture_item(logical_item(item))
                    captured._cancel=lambda:self.worker.canceled
                    captured._save_original_source=(captured._source_blob,captured._source_path,captured._prism_source)
                    items.append(captured);self.canonical[id(captured)]=logical_item(item)
                    yield
                state.iter_assets=lambda:iter(items)
                state.items_for_save=lambda:list(items)
                state.items_by_type=lambda kind:(item for item in items if item.TYPE==kind)
                state.clear_save_ids=lambda:[setattr(item,'save_id',None) for item in items]
                state._prism_archive_items=set()
            return
        from prism.partial_project import _assets
        from prism.item_lifecycle import logical_item
        session = self.session
        # Capture ownership here; construct large pure-data indexes in the
        # worker, not in a single unbounded GUI generator step.
        self.session_identity = (session.source, session.source_identity,
                                 session.project_id, session.schema_version)
        self.session_options = dict(loaded_pages=session.loaded_pages,
            pages_complete=session.pages_complete, note_key=session.note_key,
            mindmap_key=session.mindmap_key, attachments=session.attachments)
        self._capture_stage = 'session'
        fields = {name: getattr(session, name) for name in (
            '_canvas_rows', '_domain_deletions', '_library_state_before',
            '_valid_domain_attachments', '_mindmap_clear_authorized', '_page_bases',
            '_metadata_scope')}
        source = {'rows': session._rows, 'fields': fields}
        self.domain_identity = None
        if session._domains is not None:
            self.domain_identity = session._domains.identity
            source['domains'] = session._domains._rows
        # Baseline rows are shared across these indexes; traverse/copy each
        # nested row once while preserving any unknown nested metadata.
        captured = yield from sliced_copy(source)
        self.session_rows, self.session_fields = captured['rows'], captured['fields']
        if self.domain_identity is not None:
            self.domain_rows = captured['domains']
        self._capture_stage = 'assets'
        items, self.moves = [], {}
        for item in session._residency.assets_for_save(list(_assets(self.scene))):
            item = logical_item(item)
            captured = yield from _capture_item(item)
            captured._cancel = lambda: self.worker.canceled
            items.append(captured)
            self.canonical[id(captured)] = item
            admitted = session._canvas_moves.get(id(item))
            if admitted is not None and admitted[0] is item:
                self.moves[id(captured)] = (captured, set(admitted[1]))
            yield
        state.iter_assets = lambda: iter(items)
        state._partial_project_source = self.session_identity[0]

    def _prepare_session(self):
        from prism.partial_project import PartialProjectSession
        baseline = PartialReadSnapshot(*self.session_identity,
            tuple((t, k, row) for (t, k), row in self.session_rows.items()))
        private = self.private_session = PartialProjectSession(baseline, **self.session_options)
        for name, value in self.session_fields.items():
            setattr(private, name, value)
        private._canvas_moves = self.moves
        if self.domain_identity is not None:
            from prism.partial_domains import DomainTracker
            identity = self.domain_identity
            domain = PartialReadSnapshot(identity.source, identity.source_identity,
                identity.project_id, identity.schema_version,
                tuple((t, k, row) for (t, k), row in self.domain_rows.items()))
            private._domains = DomainTracker(domain)
        # This residency contains only projections; there are no Qt handles.
        private._residency.bind_live(self.board.state.iter_assets(), set(private.loaded()))

    def _wait(self, event, worker):
        while not event.wait(.05):
            if worker.canceled:
                raise PartialSaveCancelled('Save preparation cancelled')
        if worker.canceled:
            raise PartialSaveCancelled('Save preparation cancelled')

    def run(self, *, worker):
        try:
            self._wait(self.ready, worker)
            if self.error is not None:
                raise self.error
            state = self.board.state
            recovery_checkpoint = getattr(self, 'recovery_checkpoint', None)
            if recovery_checkpoint is not None:
                store, payload, session = recovery_checkpoint
                store.write(payload, session=session)
            with operation('project.save_prepare_background'):
                with operation('project.save_prepare_tags'):
                    from prism.tags import TagSystem
                    mappings = getattr(state, '_save_tag_mappings', None)
                    if mappings is not None:
                        siblings, parents = mappings
                        state.tag_system = TagSystem(siblings.items(),
                            ((child, parent) for child, values in parents.items() for parent in values))
                    else:
                        state.tag_system = TagSystem.from_dict(state.tag_system)
                from prism.fileio.sql import load_deferred_board_extras
                # Partial saves preserve missing page bodies from their original
                # baseline. Only singleton boards need loading for comparison.
                names = ('note_html', 'mindmap_tree') if self.session is not None or getattr(self,'_managed_existing_source',None) else None
                with operation('project.save_prepare_deferred'):
                    load_deferred_board_extras(state, names=names)
                if worker.canceled:
                    raise PartialSaveCancelled('Save preparation cancelled')
                if self.session is not None:
                    with operation('project.save_prepare_session'):
                        self._prepare_session()
                    with operation('project.save_prepare_diff'):
                        self.partial = self.private_session.capture(state, cancel=lambda: worker.canceled)
                    if self.create_new and os.path.normcase(os.path.abspath(self.filename)) == \
                            os.path.normcase(os.path.abspath(self.partial.source)):
                        self.create_new = False
                elif getattr(self,'_managed_existing_source',None):
                    from prism.partial_project import PartialProjectSession
                    from prism.storage.managed import database_path
                    from contextlib import closing
                    from pathlib import Path
                    import sqlite3
                    self._managed_existing_source = database_path(self._managed_existing_source)
                    ids=tuple(item.save_id for item in state.iter_assets() if item.save_id is not None)
                    # New imported pages have no disk row yet. Only persisted
                    # pages belong in the baseline; capture inserts the new ones.
                    uri=Path(self._managed_existing_source).resolve().as_uri()+'?mode=ro'
                    with closing(sqlite3.connect(uri,uri=True)) as connection:
                        pages=tuple(row[0] for row in connection.execute('SELECT id FROM workspace_pages'))
                    self.private_session=PartialProjectSession.open(self._managed_existing_source,
                        item_ids=ids,page_ids=pages,pages_complete=True,
                        include_domains=bool(state.material_records or state.workspace_nodes),
                        attachments=True,cancel=lambda:worker.canceled)
                    self._managed_new_session=self.private_session
                    self.partial=self.private_session.capture(state,cancel=lambda:worker.canceled)
            self.prepared.emit(self)
            self._wait(self.accepted, worker)
            if self.error is not None:
                raise self.error
            from prism import fileio
            self.phase_changed.emit(PHASE_WRITING)
            with operation('project.save_write_background'):
                # Partial SQL sees only private state. The full writer keeps its
                # established canvas protocol; detached board data is supplied.
                fileio.save_prism(self.filename, state if self.partial is not None or getattr(self,'_managed_fresh_scene',False) else self.scene,
                    create_new=self.create_new, worker=worker,
                    partial_snapshot=self.partial,
                    board_snapshot=state if self.partial is None else None)
        except Exception as error:
            self.error = error
            worker.finished.emit(self.filename, [str(error)])

    def _accept(self, operation_owner):
        try:
            if operation_owner is not self or not self._owned() or self.worker.canceled:
                raise PartialSaveCancelled('Save preparation no longer owns this project')
            if getattr(self,'_managed_new_session',None) is not None:
                self.session=self._managed_new_session
                from prism.partial_residency import PartialAssetResidency
                # This newly created baseline has only worker projections.
                # Transfer ownership to canonical GUI objects, not the old
                # projection identities. Existing sessions are never reset.
                self.session._residency=PartialAssetResidency()
                self.session.attach(self.scene)
                self.view._install_catalog_rebind_hook(self.session)
            if self.partial is not None:
                versions = self.private_session._captured_versions[self.partial.revision]
                self.session._remember_versions(self.partial.revision, versions)
                # GUI acknowledgment holds canonical objects; SQL holds only
                # projections. Both snapshots describe the same saved revision.
                gui_items = tuple(NewItem(self.canonical[id(new.item)], new.expected, new.tag)
                                  for new in self.partial.new_items)
                self.view._save_partial_snapshot = replace(self.partial, new_items=gui_items,
                    attachment_origin=self.board._attachment_origin)
            else:
                self.view._save_board_snapshot = self.board
        except Exception as error:
            self.error = error
        finally:
            self.accepted.set()

    # -- the result contract ------------------------------------------------
    def _writer_result(self):
        """What the writer reported about its transaction.

        The writer publishes plain facts on the worker; this is the adapter
        that turns them into the typed outcome. The writer never decides what
        the user is told.
        """
        worker = self.worker
        result = getattr(worker, 'save_write_result', None)
        if isinstance(result, dict):
            return result
        return {}

    def _changed_entities(self):
        """The changed entity identities, or None when they are not known.

        None means "not known". An empty collection would claim that nothing
        changed, and a consumer that trusts it would skip the refresh it
        actually needs.

        A row COUNT is not an identity list. The partial receipt reports how
        many rows were touched; it does not say which entities changed, and
        it does not cover metadata, domain or attachment changes. Turning the
        count into a list of "identities" would claim exactly what is not
        known.
        """
        result = self._writer_result()
        changed = result.get('changed_entities')
        if changed is not None:
            return tuple(changed), bool(result.get('changed_entities_complete', False))
        # Neither the partial receipt nor the full writer can name every
        # entity they touched yet, so the identity list stays unknown.
        return None, False

    def _changed_row_count(self):
        """How many rows the write touched, or None when that is unknown.

        Kept separate from the identity list on purpose: the count is what
        the partial receipt has always reported, and it is compatible with
        the old consumers.
        """
        result = self._writer_result()
        count = result.get('changed_row_count')
        if isinstance(count, int) and not isinstance(count, bool):
            return count
        receipt = getattr(self.worker, 'partial_save_receipt', None)
        rows = getattr(receipt, 'changed_rows', None)
        if isinstance(rows, int) and not isinstance(rows, bool):
            return rows
        return None

    def build_outcome(self, errors, *, commit_state=None, ack_state=None,
                      terminal=None, reason='', error='', ui_delivery=None):
        """Assemble the immutable result of this operation.

        This must never raise. The commit fact is read first and kept: a
        failure while building the result may not turn a committed save into
        a reported failure, and it may not escape as an exception out of the
        Qt slot that called it.
        """
        try:
            return self._build_outcome(errors, commit_state=commit_state,
                                        ack_state=ack_state, terminal=terminal,
                                        reason=reason, error=error,
                                        ui_delivery=ui_delivery)
        except Exception:
            logger.exception('The save result could not be assembled in full')
            return self._minimal_outcome(errors, commit_state=commit_state,
                                          ack_state=ack_state, terminal=terminal,
                                          reason=reason, error=error,
                                          ui_delivery=ui_delivery)

    def _minimal_outcome(self, errors, *, commit_state=None, ack_state=None,
                          terminal=None, reason='', error='', ui_delivery=None):
        """The result reduced to the facts that are guaranteed.

        Used when the full result could not be built. It keeps the commit
        fact, because that is the one thing that must not be lost: a save
        that was committed must never be reported as "nothing was written".
        """
        result = self._writer_result()
        cancelled = (bool(result.get('cancelled'))
                     or isinstance(self.error, PartialSaveCancelled))
        if commit_state is None:
            commit_state = result.get('commit_state') or COMMIT_UNKNOWN
        if ack_state is None:
            ack_state = ACK_NOT_APPLICABLE
        if ui_delivery is None:
            ui_delivery = (DELIVERY_CURRENT if self._owned_source()
                           else DELIVERY_STALE_IGNORED)
        if terminal is None:
            if isinstance(self.error, PartialSaveConflict):
                # The project changed underneath the save: nothing was
                # written, and that is a refusal.
                terminal = TERMINAL_REFUSED
            else:
                terminal = self._terminal_for(commit_state, ack_state, errors, cancelled)
        return SaveOutcome(
            operation_id=self.operation_id,
            project_id=str(self.project_id),
            open_epoch=str(self.open_epoch),
            target=os.path.normcase(os.path.abspath(str(self.filename))),
            captured_revision=tuple(self.revision),
            mode='partial' if self.partial is not None else 'full',
            commit_state=commit_state,
            ack_state=ack_state,
            terminal=terminal,
            source_before=self.source_before,
            source_after=None,
            changed_entities=None,
            changed_entities_complete=False,
            changed_row_count=None,
            ui_delivery=ui_delivery,
            cancel_too_late=bool(result.get('cancel_too_late', False)),
            reason=reason or result.get('reason', ''),
            error=error or (str(errors[0]) if errors else ''),
        )

    def _build_outcome(self, errors, *, commit_state=None, ack_state=None,
                       terminal=None, reason='', error='', ui_delivery=None):
        result = self._writer_result()
        # The writer says whether it was cancelled.  The exception type says
        # it too, for the paths where the writer never ran.
        cancelled = (bool(result.get('cancelled'))
                     or isinstance(self.error, PartialSaveCancelled))
        if commit_state is None:
            commit_state = result.get('commit_state')
        if commit_state is None:
            # The writer never ran or never reported. The exception type
            # decides, not the wording of the message: a cancellation is a
            # cancellation in any language.
            if isinstance(self.error, PartialSaveCancelled):
                commit_state = COMMIT_NOT_STARTED
                reason = reason or 'Save cancelled. Nothing was written.'
            elif isinstance(self.error, PartialSaveConflict):
                # The project changed underneath the save. Nothing was
                # written, and that is a refusal, not a failure.
                commit_state = COMMIT_NOT_STARTED
                reason = reason or str(self.error)
                terminal = terminal or TERMINAL_REFUSED
            elif self.error is not None:
                commit_state = COMMIT_UNKNOWN
            else:
                commit_state = COMMIT_UNKNOWN
        if ack_state is None:
            ack_state = ACK_NOT_APPLICABLE
        if ui_delivery is None:
            ui_delivery = (DELIVERY_CURRENT if self._owned_source()
                           else DELIVERY_STALE_IGNORED)
        changed, complete = self._changed_entities()
        mode = 'partial' if self.partial is not None else 'full'
        if terminal is None:
            if isinstance(self.error, PartialSaveConflict):
                # The project changed underneath the save: nothing was
                # written, and that is a refusal.
                terminal = TERMINAL_REFUSED
            else:
                terminal = self._terminal_for(commit_state, ack_state, errors, cancelled)
        if not reason and commit_state in (COMMIT_NOT_STARTED, COMMIT_ROLLED_BACK):
            reason = ('Save cancelled. Nothing was written.' if cancelled
                      else 'The save failed before anything was written.')
        # A cancel that arrives after the commit is no longer interruptible
        # does not undo the commit. The result has to say that the cancel was
        # late, so that nobody reads "confirmed" as "the save was undone".
        cancel_too_late = bool(result.get('cancel_too_late', False))
        if (not cancel_too_late and commit_state == COMMIT_COMPLETE
                and self.worker is not None and getattr(self.worker, 'canceled', False)):
            cancel_too_late = True
        return SaveOutcome(
            operation_id=self.operation_id,
            project_id=str(self.project_id),
            open_epoch=str(self.open_epoch),
            target=os.path.normcase(os.path.abspath(str(self.filename))),
            captured_revision=tuple(self.revision),
            mode=mode,
            commit_state=commit_state,
            ack_state=ack_state,
            terminal=terminal,
            source_before=self.source_before,
            source_after=SourceFingerprint.of(self.filename),
            changed_entities=changed,
            changed_entities_complete=complete,
            changed_row_count=self._changed_row_count(),
            ui_delivery=ui_delivery,
            cancel_too_late=cancel_too_late,
            reason=reason or result.get('reason', ''),
            error=error or (str(errors[0]) if errors else ''),
        )

    @staticmethod
    def _terminal_for(commit_state, ack_state, errors, cancelled=False):
        if commit_state in (COMMIT_NOT_STARTED, COMMIT_ROLLED_BACK):
            # "Nothing was written" is the disk fact.  "The user cancelled" is
            # a different fact, and only the writer knows it.  A write that
            # died before it wrote anything is a failure: reporting it as a
            # cancel hides the reason from the log and from the user.
            return TERMINAL_CANCELLED if cancelled else TERMINAL_FAILED
        if commit_state == COMMIT_COMPLETE:
            if ack_state == ACK_CONFIRMED:
                return TERMINAL_CONFIRMED
            # The bytes are on disk and nobody confirmed them here. That is
            # not a failure of the write: it is a write without a receipt.
            return TERMINAL_WRITTEN_UNCONFIRMED
        if errors:
            return TERMINAL_FAILED
        return TERMINAL_FAILED

    @QtCore.pyqtSlot()
    def _thread_finished(self):
        """The native QThread end, not its earlier filename/errors signal."""
        self._worker_stopped = True
        self._release_capture()

    def _release_capture(self):
        if (self._capture_released or not self._receipt_finished
                or not self._worker_stopped):
            return
        self._capture_released = True
        self.timer.stop()
        steps, self._steps = self._steps, None
        try:
            if steps is not None:
                steps.close()
        except Exception:
            logger.exception('Could not close completed save capture')
        worker, self.worker = self.worker, None
        if worker is not None:
            # Same completed-worker cleanup used by resource export. Never
            # clear these while the writer or GUI acknowledgment still uses them.
            worker.args = ()
            worker.kwargs = {}
            worker.func = None
            if hasattr(worker, 'managed_captured_items'):
                worker.managed_captured_items = ()
        # Drop only this save's private captures. Live scene/session data and
        # acknowledgment snapshots are not mutated or closed here.
        self.board = self.partial = self.error = None
        self.canonical = {}
        for name in ('private_session', 'session_rows', 'session_fields',
                     'domain_rows', 'moves', 'session_options', '_managed_new_session'):
            if hasattr(self, name):
                setattr(self, name, None)
        self.deleteLater()

    def finish(self, filename, errors):
        if getattr(self.view, '_save_operation', None) is not self or self.view.worker is not self.worker:
            record('project_save_result_ignored', filename=str(filename), reason='operation_changed')
            self._receipt_finished = True
            self._release_capture()
            return
        self.timer.stop()
        try:
            # The result is assembled before the acknowledgement, so that the
            # progress dialog already knows the truth even if the
            # acknowledgement itself goes wrong. build_outcome never raises.
            outcome = self.build_outcome(errors)
            if errors:
                record('project_save_refused', filename=str(filename), reason=str(errors[0])[:200], silent=self.silent)
                self.view._paged_save_refused = (time.time(), str(errors[0]))
            if not errors and not self.silent:
                self.view._show_save_phase(PHASE_CONFIRMING)
            try:
                self.view.on_saving_finished(filename, errors, silent=self.silent, outcome=outcome)
            except Exception:
                # The confirmation in the interface failed. The disk fact is
                # already published, so this must not become a reported
                # failure and it must not escape as an exception out of the
                # slot that called it.
                logger.exception('The save was written, but the interface '
                                 'could not confirm it')
                self._report_unconfirmed(filename, outcome)
            if (not errors and self._owned_source() and self.view.filename == filename
                    and (self.session is None or self.session.invalid is None)):
                # Keep lazy content lazy, but point future reads at the saved copy.
                if getattr(self.scene, '_deferred_board_extras', None):
                    self.scene._board_extras_source = str(filename)
        finally:
            # Acknowledgment/rebind hooks can reenter the event loop. Retain
            # protection until the matching GUI receipt has finished applying.
            if getattr(self.view, '_save_operation', None) is self:
                self.view._save_preparing = False
                self.view._save_operation = None
            self._receipt_finished = True
            self._release_capture()

    def _report_unconfirmed(self, filename, outcome):
        """Tell the user that the file was written but not confirmed here."""
        warning = ('The file was written but saving could not be confirmed. '
                   'Your edits remain unsaved; reopen the saved copy before continuing.')
        try:
            if hasattr(self.view.parent, 'statusBar'):
                self.view.parent.statusBar().showMessage(warning, 12000)
        except Exception:
            logger.exception('The unconfirmed save could not be announced')
        try:
            self.view.last_save_outcome = outcome
        except Exception:
            pass
        record('project_save_outcome', operation_id=self.operation_id,
               terminal='written_unconfirmed', commit_state=(
                   outcome.commit_state if outcome is not None else 'unknown'),
               ack_state='failed', ui_delivery='current', mode=(
                   'partial' if self.partial is not None else 'full'),
               changed_entities_complete=False, cancel_too_late=False)

    def _owned_source(self):
        return self.view.scene is self.scene and self.scene.project_id == self.project_id
