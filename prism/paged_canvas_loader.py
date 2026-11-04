"""Bounded page reads; only GUI slots may install logical assets.

The catalog selects IDs, while the immutable save baseline supplies the actual
values. No second detail query can race the baseline and lose an external edit.
One outstanding batch per controller provides backpressure to disk and GUI.

A canvas is not only images: it also holds text notes, brush strokes, videos
and 3D models. A page therefore covers **every** kind of the canvas, and the
items are built through the same registry path the full reader uses, so a
continuation installs the items the project really holds.

This module also owns the catalog build controller. The build is the one
phase that reads the whole project, so it runs in the background, reports its
progress and can be cancelled at the next checkpoint.
"""
import json
import logging
import sqlite3
import threading
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from PyQt6 import QtCore
from PyQt6 import sip

from prism.fileio.sql import build_native_item
from prism.i18n import _
from prism.partial_project import ITEM_FIELDS, ITEM_TABLE, read_baseline
from prism.partial_residency import key_of
from prism.performance_trace import record_catalog_build_progress

logger = logging.getLogger(__name__)

#: What the catalog build reports as "the project total is not known yet".
#: The build only knows the total once it ends, so a progress handler that
#: wants a percentage has to render this as an open-ended counter instead of
#: dividing by it.
CATALOG_TOTAL_UNKNOWN = 0


def _deliver(signals, result, cancel):
    """A destroyed receiver is normal cancellation, never a QRunnable abort."""
    if cancel.is_set() or sip.isdeleted(signals):
        return
    try:
        signals.result.emit(result)
    except RuntimeError:
        # QObject destruction may race the check above. Do not emit an error
        # through the same deleted signal object from the worker's exception
        # handler, which would escape QRunnable.run into Qt.
        if not cancel.is_set() and not sip.isdeleted(signals):
            logger.exception('Cannot deliver a canvas page result')


class _Signals(QtCore.QObject):
    result = QtCore.pyqtSignal(object)


class _Archive:
    """Read-only archive access for one worker batch.

    The worker has to know whether a video's bytes are embedded and to read
    an embedded model's blob. Both happen here, off the GUI thread.
    """

    def __init__(self, project_path):
        self.project_path = str(project_path)
        self.connection = sqlite3.connect(
            Path(str(project_path)).resolve().as_uri() + '?mode=ro', uri=True)

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def embedded(self, item_id):
        """True when the archive holds bytes for this item."""
        row = self.connection.execute(
            'SELECT length(data) FROM sqlar WHERE item_id=?',
            (item_id,)).fetchone()
        if row and row[0]:
            return True
        from prism.storage.managed import root_for, binding
        ref = binding(self.project_path, 'sqlar', item_id, item_id=True) if root_for(self.project_path) else None
        return bool(ref and ref[1])

    def embedded_ids(self, item_ids):
        """The ids of this batch whose bytes the archive really holds.

        One bounded query answers it for the whole page, so the page does
        not learn about a missing original by failing to read a header it
        does not have.
        """
        item_ids = tuple(item_ids)
        if not item_ids:
            return frozenset()
        from prism.storage.managed import root_for
        if root_for(self.project_path) is not None:
            rows = self.connection.execute(
                "SELECT s.item_id FROM sqlar s JOIN object_refs r ON r.owner='sqlar' AND r.entity=CAST(s.name AS TEXT) "
                'WHERE r.size>0 AND s.item_id IN (%s)' % ','.join('?' for _ in item_ids), item_ids)
            return frozenset(row[0] for row in rows)
        rows = self.connection.execute(
            'SELECT item_id FROM sqlar WHERE length(data)>0 AND item_id IN (%s)'
            % ','.join('?' for _ in item_ids), item_ids).fetchall()
        return frozenset(row[0] for row in rows)

    def blob(self, item_id):
        """The archive bytes of this item, or None."""
        from prism.storage.managed import root_for
        if root_for(self.project_path) is not None:
            from prism.source_store import read_archive
            return read_archive((self.project_path, item_id))
        row = self.connection.execute(
            'SELECT data FROM sqlar WHERE item_id=?', (item_id,)).fetchone()
        return row[0] if row else None


def _damaged_original(data, project_path):
    """The error placeholder for a material whose original bytes are gone.

    The full reader shows the same placeholder for a damaged original, and
    the partial save leaves such a row exactly as it is: the project keeps
    its record, the canvas says the truth about the bytes. One damaged
    material must never empty a whole canvas.
    """
    from prism.items import PrismErrorItem
    extra = dict(data.get('data') or {})
    clue = (extra.get('filename') or extra.get('title') or extra.get('notes')
            or _('no name stored'))
    damaged = dict(data)
    damaged['type'] = PrismErrorItem.TYPE
    # The metadata of the original row stays: the placeholder still belongs
    # to its canvas and still carries its notes, tags and title.
    extra['text'] = ('Image could not be loaded: %s\n(item %s)\n'
                     '工程里没有这张图的原始字节，无法显示；记录仍在工程里。'
                     % (clue, data['save_id']))
    extra['_asset_source'] = (str(project_path), data['save_id'])
    damaged['data'] = extra
    return damaged


class _Read(QtCore.QRunnable):
    def __init__(self, scope, canvas, after, known, epoch, cancel, signals):
        super().__init__()
        self.args = scope, canvas, after, known, epoch, cancel, signals

    def run(self):
        scope, canvas, after, known, epoch, cancel, signals = self.args
        try:
            # Every kind of the canvas, in one ascending id run. A pixmap
            # only page would leave the notes, strokes, videos and models of
            # the canvas invisible: the files are still there, the user just
            # cannot see them.
            summaries = scope.page(canvas, kind=None, after_id=after,
                                   limit=128, cancel=cancel.is_set)
            ids = [row['id'] for row in summaries if row['id'] not in known]
            baseline = read_baseline(scope.project_path,
                                     [(ITEM_TABLE, key) for key in ids])
            prepared = []
            with closing(_Archive(scope.project_path)) as archive:
                # One bounded lookup tells the page which originals really
                # exist. A material whose bytes are gone is found here,
                # instead of by failing to read a header it does not have.
                with_bytes = archive.embedded_ids(
                    key for _table, key, values in baseline.records
                    if values[0] == 'pixmap')
                for table, key, values in baseline.records:
                    if cancel.is_set():
                        return
                    data = dict(zip(ITEM_FIELDS, values))
                    data['save_id'] = key
                    extra = json.loads(data['data'] or '{}')
                    data['data'] = extra
                    kind = data['type']
                    if kind == 'pixmap':
                        if key not in with_bytes:
                            # The original bytes are gone. That is one
                            # damaged material, not a damaged page: the
                            # canvas keeps every other material and shows
                            # this one as the placeholder the full reader
                            # would show.
                            prepared.append(
                                _damaged_original(data, scope.project_path))
                            continue
                        # Header reads for old archives stay on this worker,
                        # never GUI.
                        if not (extra.get('imageWidth') and extra.get('imageHeight')):
                            from prism.source_store import read_archive
                            from prism.image_decode import native_reader
                            with native_reader(blob=read_archive((str(scope.project_path), key))) as reader:
                                size = reader.size()
                                if reader.transformation().value & 4:
                                    size.transpose()
                            if not size.isValid() or size.isEmpty():
                                # A corrupt original is a broken material,
                                # not a broken page.
                                prepared.append(
                                    _damaged_original(data, scope.project_path))
                                continue
                            extra['imageWidth'], extra['imageHeight'] = size.width(), size.height()
                    elif kind == 'video':
                        # An embedded video keeps its bytes in the archive;
                        # the item materializes them only when it plays.
                        data['_video_embedded'] = archive.embedded(key)
                    elif kind == 'glb':
                        data['_glb_blob'] = archive.blob(key)
                    elif kind not in ('text', 'path'):
                        # A kind nothing can install is a broken record, not
                        # a page that may be skipped silently. The registry
                        # is what can install: a row the project really
                        # holds (a saved error placeholder, for instance)
                        # must not empty the whole canvas.
                        from prism.items import item_registry
                        if kind not in item_registry:
                            raise ValueError(
                                'Canvas page contains an unsupported record type: %r'
                                % (kind,))
                    prepared.append(data)
            if not cancel.is_set():
                _deliver(signals, (epoch, baseline, prepared,
                                     summaries[-1]['id'] if summaries else after,
                                     len(summaries) == 128, None), cancel)
        except Exception as error:
            if not cancel.is_set():
                _deliver(signals, (epoch, None, (), after, False, error), cancel)


class PagedCanvasLoader(QtCore.QObject):
    """Cancellable page continuation with 32-record GUI installation turns.

    Residency intentionally retains canonical objects already loaded; this is
    not a byte-budget cache. Cancellation keeps committed batches and discards
    unread/uninstalled batches. Saving never sees a half-registered batch.
    """
    finished = QtCore.pyqtSignal(str)
    batch_ready = QtCore.pyqtSignal(str)
    failed = QtCore.pyqtSignal(str, str)

    def __init__(self, view):
        super().__init__(view)
        self.view = view
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.signals = _Signals(self)
        self.signals.result.connect(self._received)
        self.timer = QtCore.QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self._install)
        self.epoch = 0
        self.cancel_event = threading.Event()
        self.completed = set()
        self.active = False
        self.owner_gone = False
        view.destroyed.connect(self._owner_destroyed)

    def _owner_destroyed(self, *_):
        self.owner_gone = True
        self.cancel_event.set()
        self.epoch += 1
        self.active = False
        self.pending = None
        try:
            self.timer.stop()
            self.pool.clear()
        except RuntimeError:
            # A child may already have been deleted during application exit.
            pass

    def cancel(self, reset=False):
        self.epoch += 1
        self.cancel_event.set()
        self.pool.clear()
        self.timer.stop()
        self.active = False
        self.pending = None
        if reset:
            self.completed.clear()

    def request(self, canvas):
        if self.owner_gone:
            return False
        session = getattr(self.view.scene, '_partial_project_session', None)
        scope = getattr(self.view, '_open_scope', None)
        if session is None or scope is None:
            return False
        identity = (id(session), str(canvas))
        if identity in self.completed:
            if self.active and self.identity != identity:
                self.cancel()
            return True
        if self.active and self.identity == identity:
            return True
        try:
            if not scope.ready:
                from prism.project_open_scope import CatalogNotReady
                raise CatalogNotReady('Canvas catalog is missing, stale or incomplete')
        except Exception as error:
            self.canvas = str(canvas)
            self._fail(error)
            return False
        self.cancel()
        self.identity = identity
        self.session, self.scope, self.canvas = session, scope, str(canvas)
        self.project_id = self.view.scene.project_id
        self.cancel_event = threading.Event()
        self.active = True
        self.after = None
        self._next()
        return True

    def _current(self):
        return (not self.owner_gone and self.active and not self.cancel_event.is_set()
                and self.view.scene.project_id == self.project_id
                and getattr(self.view.scene, '_partial_project_session', None) is self.session
                and getattr(self.view, '_open_scope', None) is self.scope)

    def _next(self):
        if not self._current():
            return
        self.pool.start(_Read(self.scope, self.canvas, self.after,
                             set(self.session.loaded()), self.epoch,
                             self.cancel_event, self.signals))

    @QtCore.pyqtSlot(object)
    def _received(self, result):
        epoch, baseline, data, after, more, error = result
        if epoch != self.epoch or not self._current():
            return
        if error is not None:
            self._fail(error)
            return
        self.pending = [baseline, list(data), after, more]
        self.timer.start(0)

    def _fail(self, error):
        self.cancel()
        self.failed.emit(self.canvas, str(error))

    def _build_item(self, data):
        """Build the scene item for one prepared row.

        Images become asset records; every other kind goes through the
        registry path the full reader uses, so a text note, a brush stroke,
        a video and a 3D model are the items the project really holds
        instead of image records in disguise.
        """
        kind = data['type']
        if kind == 'pixmap':
            from prism.asset_records import ImageRecord
            item = ImageRecord(data, (str(self.scope.project_path),
                                      data['save_id']))
            item.setVisible(False)
            return item
        from prism.items import item_registry
        payload = dict(data)
        if kind in ('video', 'glb'):
            payload = build_native_item(
                payload, filename=self.scope.project_path,
                glb_blob=payload.get('_glb_blob'),
                video_embedded=bool(payload.get('_video_embedded')))
        cls = item_registry.get(payload['type'])
        if cls is None:
            raise ValueError(
                'Canvas page contains an unsupported record type: %r'
                % (payload['type'],))
        arguments = dict(payload)
        arguments.pop('type', None)
        item = cls.create_from_data(**arguments)
        item.update_from_data(**arguments)
        return item

    def _install(self):
        if not self._current() or self.pending is None:
            return
        from prism.asset_records import ImageRecord
        scene = self.view.scene
        baseline, remaining, after, more = self.pending
        # A search jump can install an item while this disk batch is in flight.
        # Reuse that canonical object rather than creating a duplicate copy.
        live_ids = {key_of(item) for item in scene.iter_assets()}
        chunk = [data for data in remaining[:32] if data['save_id'] not in live_ids]
        ids = {data['save_id'] for data in chunk}
        base = replace(baseline, records=tuple(row for row in baseline.records if row[1] in ids))
        installed = []
        blocked = scene.blockSignals(True)
        try:
            for data in chunk:
                key = data['save_id']
                item = self.session.retained_item(key)
                if item is None:
                    item = self._build_item(data)
                scene.addItem(item)
                installed.append(item)
                if isinstance(item, ImageRecord):
                    scene._prism_archive_items.add(item)
            if chunk:
                self.session.register_loaded(base, installed, scene)
        except Exception as error:
            for item in installed:
                scene.removeItem(item)
                scene._prism_archive_items.discard(item)
            self._fail(error)
            return
        finally:
            scene.blockSignals(blocked)
        if installed:
            loaded = set(getattr(scene, '_loaded_scope_ids', ()) or ())
            scene._loaded_scope_ids = tuple(loaded | ids)
            if not blocked:
                scene.assets_changed.emit()
                scene.changed.emit([QtCore.QRectF()])
        del remaining[:32]
        if remaining:
            self.timer.start(0)
            return
        self.after = after
        self.pending = None
        self.batch_ready.emit(self.canvas)
        if more:
            self._next()
        else:
            self.completed.add(self.identity)
            self.active = False
            self.finished.emit(self.canvas)


class _CatalogSignals(QtCore.QObject):
    """One channel for the build worker's events."""

    event = QtCore.pyqtSignal(object)


def _emit_event(signals, epoch, kind, payload):
    """Deliver one build event; a destroyed receiver is normal shutdown."""
    if sip.isdeleted(signals):
        return
    try:
        signals.event.emit((epoch, kind, payload))
    except RuntimeError:
        if not sip.isdeleted(signals):
            logger.exception('Cannot deliver a catalog build event')


class _CatalogBuild(QtCore.QRunnable):
    """Build a project catalog on a worker thread."""

    def __init__(self, scope, batch_size, cancel, signals, epoch):
        super().__init__()
        self.args = scope, batch_size, cancel, signals, epoch

    def run(self):
        scope, batch_size, cancel, signals, epoch = self.args
        if cancel.is_set():
            _emit_event(signals, epoch, 'cancelled', None)
            return

        def on_batch(indexed):
            # The project total is only known once the build ends, so it is
            # reported as unknown instead of being guessed from the count.
            _emit_event(signals, epoch, 'progress',
                        (int(indexed), CATALOG_TOTAL_UNKNOWN))

        try:
            result = scope.build(batch_size=batch_size, cancel=cancel.is_set,
                                 on_batch=on_batch)
        except Exception as error:
            _emit_event(signals, epoch, 'failed', str(error))
            return
        if not result.complete:
            # A stopped build keeps its checkpoint and stays incomplete.
            _emit_event(signals, epoch, 'cancelled', None)
            return
        _emit_event(signals, epoch, 'finished', result)


class CatalogBuildController(QtCore.QObject):
    """Cancellable, progress-reporting catalog build.

    The controller is the GUI's only handle on the build: it starts the work
    in a background thread, reports the indexed count while it runs, and
    stops it at the next checkpoint when the user cancels.

    A cancelled build leaves the cache **incomplete and resumable**: the
    catalog keeps its checkpoint and ``scope.ready`` stays False, so the
    next open resumes the build instead of trusting a half-built cache.

    The wiring contract, in full:

    * ``start(scope, *, batch_size=128) -> bool`` -- ``scope`` is a
      :class:`prism.project_open_scope.ProjectOpenScope`. It returns False
      while another build of this controller is still running; the caller
      has to wait for ``cancelled``/``finished`` before starting another.
    * ``progress(int, int)`` -- ``(processed, total)``. ``processed`` is the
      number of records the catalog has indexed so far. ``total`` is
      :data:`CATALOG_TOTAL_UNKNOWN` (0) while the build runs, because the
      project total is only known once the build ends; the last
      ``progress`` before ``finished`` carries the real total twice.
      A handler must therefore render ``total`` as "unknown" and never
      divide by it.
    * ``cancel() -> None`` -- asks the build to stop at its next
      checkpoint. It never blocks and never waits for the worker.
    * ``finished(object)`` -- the catalog ``BuildResult``
      (``complete``, ``total_records``, ``processed_records``, ``last_id``,
      ``seconds``, ``resumed``, ``json_characters``).
    * ``failed(str)`` -- the error message of a failed build.
    * ``cancelled()`` -- the build stopped on request; the cache is
      incomplete but resumable, so the next open continues it.
    """

    #: Indexed records so far and the project total
    #: (:data:`CATALOG_TOTAL_UNKNOWN` while the total is still unknown).
    progress = QtCore.pyqtSignal(int, int)
    #: The build finished; the payload is the catalog ``BuildResult``.
    finished = QtCore.pyqtSignal(object)
    #: The build failed; the payload is the error message.
    failed = QtCore.pyqtSignal(str)
    #: The build stopped on request; the cache is incomplete but resumable.
    cancelled = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = QtCore.QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.cancel_event = threading.Event()
        self.epoch = 0
        self.active = False
        self.build_id = None
        self._last_reported_processed = None
        self.signals = _CatalogSignals(self)
        self.signals.event.connect(self._received)

    def start(self, scope, *, batch_size=128):
        """Start building ``scope``'s catalog cache in the background.

        Returns False when a build of this controller is still running.
        """
        if self.active:
            return False
        self.epoch += 1
        self.cancel_event = threading.Event()
        self.active = True
        self.build_id = uuid.uuid4().hex[:16]
        self._last_reported_processed = None
        self.pool.start(_CatalogBuild(scope, batch_size, self.cancel_event,
                                      self.signals, self.epoch))
        return True

    def cancel(self):
        """Ask the running build to stop at its next checkpoint.

        The stop is cooperative: the catalog checks the token between rows
        and between batches, and SQLite interrupts the running statement
        through its progress handler. Nothing sleeps and nothing
        busy-waits, so the build stops as soon as the running batch ends.
        The call returns immediately; the ``cancelled`` signal says when
        the build actually stopped.
        """
        self.cancel_event.set()

    @QtCore.pyqtSlot(object)
    def _received(self, event):
        epoch, kind, payload = event
        if epoch != self.epoch:
            return
        if kind == 'progress':
            processed = int(payload[0])
            self._last_reported_processed = processed
            record_catalog_build_progress(
                build_id=self.build_id, processed=processed,
                processed_this_run=None, total_records=None,
                total_known=False, final=False)
            self.progress.emit(processed, payload[1])
        elif kind == 'finished':
            self.active = False
            record_catalog_build_progress(
                build_id=self.build_id, processed=payload.total_records,
                processed_this_run=payload.processed_records,
                total_records=payload.total_records, total_known=True, final=True)
            self._last_reported_processed = payload.total_records
            self.progress.emit(payload.total_records, payload.total_records)
            self.finished.emit(payload)
        elif kind == 'failed':
            self.active = False
            self.failed.emit(str(payload))
        elif kind == 'cancelled':
            self.active = False
            self.cancelled.emit()
