# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

import argparse
import atexit
import logging
import os
import os.path
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from PyQt6 import QtCore, QtGui

from prism import constants


logger = logging.getLogger(__name__)


parser = argparse.ArgumentParser(
    description=f'{constants.APPNAME_FULL} {constants.VERSION}')
parser.add_argument(
    'filenames',
    nargs='*',
    default=None,
    help=('Bee file or images to open. '
          'If the first file is a Prism project, it will be opened and all '
          'further files will be ignored. If the first argument isn\'t a '
          'Prism project, all files will be treated as images and inserted as '
          'if opened with "Insert -> Images".'))
parser.add_argument(
    '--settings-dir',
    help='settings directory to use instead of default location')
parser.add_argument(
    '-l', '--loglevel',
    default='INFO',
    choices=list(logging._nameToLevel.keys()),
    help='log level for console output')
parser.add_argument(
    '--debug-boundingrects',
    default=False,
    action='store_true',
    help='draw item\'s bounding rects for debugging')
parser.add_argument(
    '--debug-shapes',
    default=False,
    action='store_true',
    help='draw item\'s mouse event shapes for debugging')
parser.add_argument(
    '--debug-handles',
    default=False,
    action='store_true',
    help='draw item\'s transform handle areas for debugging')
parser.add_argument(
    '--debug-raise-error',
    default='',
    help='immediately exit with given error message')


class CommandlineArgs:
    """Wrapper around argument parsing.

    Checking for unknown arguments is configurable so that it can be
    deliberately enabled from the main() function while ignored for
    other imports so that unit tests won't fail.

    This is a singleton so that arguments are only parsed once, unless
    ``with_check`` is ``True``.
    """

    _instance = None

    def __new__(cls, *args, **kwargs):
        if not cls._instance or kwargs.get('with_check'):
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, with_check=False):
        if not hasattr(self, '_args'):
            if with_check:
                self._args = parser.parse_args()
            else:
                self._args = parser.parse_known_args()[0]

    def __getattribute__(self, name):
        if name == '_args':
            return super().__getattribute__(name)
        else:
            return getattr(self._args, name)


class PrismSettingsEvents(QtCore.QObject):
    restore_defaults = QtCore.pyqtSignal()
    restore_keyboard_defaults = QtCore.pyqtSignal()
    persistence_failed = QtCore.pyqtSignal(str)


# We want to send and receive settings events globally, not per
# PrismSettings instance. Since we can't instantiate PrismSettings
# globally on module level (because the Qt app doesn't exist yet), we
# use this events proxy
settings_events = PrismSettingsEvents()


# Distinct QSettings objects for the same filename share QConfFile's mutex.
# A worker sync (even its constructor) can therefore block GUI value/setValue.
# GUI instances share a private mirror instead; only the writer opens the real
# INI. Keep Qt's types, groups and arrays, and synchronous change callbacks.
_settings_sync_lock = threading.RLock()
_settings_sync_states = {}
_settings_sync_deadlines = {}
_settings_sync_snapshots = {}
_settings_persisted_snapshots = {}
_settings_sync_clear_revisions = {}
_settings_sync_status = {}
_settings_disk_snapshots = {}
_settings_mirrors = {}
_settings_mirror_readers = {}
_settings_mirror_directory = None
_settings_sync_condition = threading.Condition(_settings_sync_lock)
_settings_sync_futures = set()
_settings_sync_executor = None


def _settings_mirror(settings_dir, settings_format, settings_scope):
    """Initialize each real INI once, before any worker can own its Qt lock."""
    global _settings_mirror_directory
    key = (settings_dir, settings_format, settings_scope)
    with _settings_sync_lock:
        existing = _settings_mirrors.get(key)
        if existing is not None:
            return existing
        if settings_dir:
            QtCore.QSettings.setPath(settings_format, settings_scope, settings_dir)
        source = QtCore.QSettings(settings_format, settings_scope,
                                 constants.APPNAME, constants.APPNAME)
        path = os.path.normpath(source.fileName())
        values = {name: source.value(name) for name in source.allKeys()}
        status = source.status()
        del source
        if _settings_mirror_directory is None:
            _settings_mirror_directory = tempfile.TemporaryDirectory(prefix='prism-settings-')
        mirror = os.path.join(_settings_mirror_directory.name,
                              str(len(_settings_mirrors)) + '.ini')
        reader = QtCore.QSettings(mirror, settings_format)
        for name, value in values.items():
            reader.setValue(name, value)
        reader.sync()
        if reader.status() != QtCore.QSettings.Status.NoError:
            raise OSError('Cannot initialize private settings mirror')
        _settings_mirror_readers[(path, threading.get_ident())] = reader
        _settings_persisted_snapshots[path] = values
        _settings_sync_status[path] = status
        _settings_mirrors[key] = (path, mirror)
        return path, mirror


def _mirror_snapshot(path, mirror, settings_format):
    # This reader never enters caller-owned groups/arrays. In-memory changes
    # are immediately visible to distinct instances of this private Qt file.
    key = (path, threading.get_ident())
    reader = _settings_mirror_readers.get(key)
    if reader is None:
        reader = QtCore.QSettings(mirror, settings_format)
        _settings_mirror_readers[key] = reader
    return {name: reader.value(name) for name in reader.allKeys()}


def _notify_settings_failure(reason):
    try:
        settings_events.persistence_failed.emit(reason)
    except RuntimeError:
        # Application teardown may have deleted the signal proxy already.
        logger.error('Settings persistence failed during shutdown: %s', reason)


def _finish_settings_future(future):
    with _settings_sync_lock:
        _settings_sync_futures.discard(future)


def _write_settings(path, settings_format, snapshot, baseline, clear):
    """The only Qt object allowed to persist the real INI after startup."""
    if not _application_alive():
        raise _SettingsUnavailable('the application is already gone')
    writer = QtCore.QSettings(path, settings_format)
    try:
        if clear:
            writer.clear()
        else:
            for name in baseline.keys() - snapshot.keys():
                writer.remove(name)
        for name, value in snapshot.items():
            if clear or name not in baseline or baseline[name] != value:
                writer.setValue(name, value)
        writer.sync()
        status = writer.status()
        disk_values = ({name: writer.value(name) for name in writer.allKeys()}
                       if status == QtCore.QSettings.Status.NoError else None)
    except RuntimeError as error:
        # Qt was destroyed between the check above and this write.  The
        # revision must not be reported as a failed write: there is no
        # application left to write it into.
        raise _SettingsUnavailable(str(error)) from error
    finally:
        del writer
    return status, disk_values


def _persist_settings(path, settings_format):
    while True:
        with _settings_sync_condition:
            while True:
                remaining = _settings_sync_deadlines[path] - time.monotonic()
                if remaining <= 0:
                    break
                _settings_sync_condition.wait(remaining)
            revision = _settings_sync_states[path]
            snapshot = _settings_sync_snapshots[path]
            baseline = _settings_persisted_snapshots[path]
            clear_revision = _settings_sync_clear_revisions.get(path)
        started = time.perf_counter()
        status = None
        gone = False
        try:
            status, disk_values = _write_settings(path, settings_format, snapshot, baseline,
                                                  clear_revision is not None)
            if status != QtCore.QSettings.Status.NoError:
                logger.error('Settings background persistence failed: %s', status.name)
                _notify_settings_failure(status.name)
        except _SettingsUnavailable as unavailable:
            # The application was torn down while this write waited for its
            # turn.  There is nothing left to write into: this is not a disk
            # failure, and the revision must not be reported as one.
            gone = True
            logger.debug('Settings writer stopped: %s', unavailable)
        except Exception as error:
            status = QtCore.QSettings.Status.AccessError
            logger.exception('Settings background persistence failed')
            _notify_settings_failure(type(error).__name__)
        finally:
            try:
                from prism.performance_trace import record
                record('settings.sync_background', duration_ms=(time.perf_counter()-started)*1000,
                       status=(status.name if status is not None
                               else ('application_gone' if gone else 'exception')),
                       gui_thread=False, revision=revision,
                       basis='native_qsettings_sync_wall_clock')
            except Exception:
                pass  # diagnostics never change settings persistence
        if gone:
            with _settings_sync_lock:
                # Keep the GUI mirror and last durable baseline, but do not
                # leave a queue entry whose worker has exited. An explicit
                # sync can then retry rather than append to a dead job.
                _settings_sync_status[path] = QtCore.QSettings.Status.AccessError
                _settings_sync_states.pop(path, None)
                _settings_sync_deadlines.pop(path, None)
                _settings_sync_snapshots.pop(path, None)
            return
        with _settings_sync_lock:
            _settings_sync_status[path] = status
            if status == QtCore.QSettings.Status.NoError:
                _settings_persisted_snapshots[path] = snapshot
                _settings_disk_snapshots[path] = disk_values
                if _settings_sync_clear_revisions.get(path) == clear_revision:
                    _settings_sync_clear_revisions.pop(path, None)
            if _settings_sync_states[path] == revision:
                del _settings_sync_states[path]
                del _settings_sync_deadlines[path]
                del _settings_sync_snapshots[path]
                return
            # A newer edit arrived during sync. Persist again; never let an
            # old completion discard the newer revision.


def _request_settings_sync(path, settings_format, snapshot, *, clear=False):
    global _settings_sync_executor
    with _settings_sync_lock:
        if path in _settings_sync_states:
            _settings_sync_states[path] += 1
            _settings_sync_snapshots[path] = snapshot
            if clear:
                _settings_sync_clear_revisions[path] = _settings_sync_states[path]
            _settings_sync_deadlines[path] = time.monotonic() + .15
            _settings_sync_condition.notify_all()
            return
        _settings_sync_states[path] = 1
        _settings_sync_snapshots[path] = snapshot
        if clear:
            _settings_sync_clear_revisions[path] = 1
        _settings_sync_deadlines[path] = time.monotonic() + .15
        if _settings_sync_executor is None:
            _settings_sync_executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix='PrismSettingsWriter')
        try:
            future = _settings_sync_executor.submit(_persist_settings, path, settings_format)
        except Exception:
            del _settings_sync_states[path]
            del _settings_sync_deadlines[path]
            del _settings_sync_snapshots[path]
            raise
        _settings_sync_futures.add(future)
        future.add_done_callback(_finish_settings_future)


def _application_alive():
    """True while Qt can still create a QSettings.

    ``QCoreApplication.instance()`` is already ``None`` while the
    application's own ``destroyed`` signal is emitted, so this is false for
    every path that runs after the application is gone.
    """
    return QtCore.QCoreApplication.instance() is not None


class _SettingsUnavailable(RuntimeError):
    """The application is gone; the pending revision cannot be written.

    This is not a failed write.  The window state is flushed while Qt is
    still alive (``PrismMainWindow.closeEvent``), so a revision that reaches
    this point belongs to a session that is already tearing down.
    """


def _drain_settings_sync():
    # Explicit sync/normal exit preserve durability. This wait is intentionally
    # not in interactive UpdateRequest handling and is never labelled async.
    if not _application_alive():
        # Qt is gone: a QSettings can no longer be created, so the write would
        # fail and drop the revision on the floor.  The close path flushes
        # while Qt is still alive; this is only the interpreter-exit fallback.
        logger.debug('Settings drain skipped: the application is already gone')
        with _settings_sync_lock:
            for path in _settings_sync_states:
                _settings_sync_status[path] = QtCore.QSettings.Status.AccessError
        return
    with _settings_sync_lock:
        for path in _settings_sync_deadlines:
            _settings_sync_deadlines[path] = 0
        _settings_sync_condition.notify_all()
        futures = tuple(_settings_sync_futures)
    for future in futures:
        try:
            future.result()
        except Exception:
            logger.exception('Settings persistence worker failed during drain')
    # Finish private Qt dirty state before normal shutdown cleans temporary
    # files. Never sync a root reader belonging to a different Qt thread.
    for (_path, owner), reader in tuple(_settings_mirror_readers.items()):
        if owner == threading.get_ident():
            try:
                reader.sync()
            except RuntimeError:
                # Qt already destroyed the reader while the application was
                # closing.  There is nothing left to flush, and an exception
                # here would turn a normal exit into an abnormal one.
                logger.debug('Settings reader was already destroyed at exit')


# PyQt also cleans up native objects at interpreter exit. Drain first, while
# its QSettings objects are still alive; normal Qt exit uses the signals below.
atexit.register(_drain_settings_sync)


#: Default value of the on-demand ("paged") open switch, i.e. whether a
#: project opens only the page that is needed instead of the whole file.
#:
#: **This one line is the product decision.**  It is True (on) because the
#: owner decided on 2026-10-23 that paged open becomes the default for
#: every user: a 100k-asset project takes minutes to open in full mode,
#: and the audit (coordination/AUDIT-FULL-20261023.md) verified a hard
#: gate in prism/fileio/sql.py:971-972 that refuses a full save on a
#: partially loaded project instead of silently dropping unloaded rows.
#: See coordination/决策-20261023.md for the decision record.
PAGED_OPEN_DEFAULT = True


def normalize_bool(value):
    """Normalize a stored setting value into a real bool.

    ``bool('off')`` is True, so a plain ``bool`` cast is unsafe for any
    setting that may end up in a settings file as 'on'/'off' text: a
    disabled switch would read back as enabled.  This accepts every
    spelling that can realistically reach a QSettings INI file -- a real
    bool, 0/1, and the 'on'/'off'/'true'/'false'/'yes'/'no'/'1'/'0'
    texts -- and raises ValueError for anything else, which makes
    ``PrismSettings.valueOrDefault`` fall back to the registered default.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value in (0, 1):
            return bool(value)
    elif isinstance(value, str):
        text = value.strip().lower()
        if text in ('1', 'true', 'on', 'yes'):
            return True
        if text in ('0', 'false', 'off', 'no'):
            return False
    raise ValueError(f'Not a boolean setting value: {value!r}')


class PrismSettings(QtCore.QSettings):

    FIELDS = {
        'Interface/language': {
            'default': 'zh_CN',
            'validate': lambda x: x in ('en', 'zh_CN'),
        },
        'Save/confirm_close_unsaved': {
            'default': True,
            'cast': bool,
        },
        'Clipboard/auto_capture': {
            'default': True,
            'cast': bool,
        },
        'Clipboard/max_items': {
            'default': 50,
            'cast': int,
            'validate': lambda x: 1 <= x <= 500,
        },
        'Clipboard/download_image_urls': {
            'default': False,
            'cast': bool,
        },
        # Extra process names whose clipboard writes Prism ignores, on top
        # of the built-in password-manager list.  One comma-separated
        # string rather than a list: the INI backend writes lists in a
        # format that is easy to break when the file is edited by hand.
        'Clipboard/excluded_apps': {
            'default': '',
            'cast': str,
        },
        'Items/image_storage_format': {
            'default': 'best',
            'validate': lambda x: x in ('png', 'jpg', 'best'),
        },
        'Items/arrange_gap': {
            'default': 0,
            'cast': int,
            'validate': lambda x: 0 <= x <= 200,
        },
        'Items/arrange_default': {
            'default': 'optimal',
            'validate': lambda x: x in (
                'optimal', 'horizontal', 'vertical', 'square'),
        },
        'Items/image_allocation_limit': {
            'default': 256,
            'cast': int,
            'validate': lambda x: x >= 0,
            'post_save_callback': QtGui.QImageReader.setAllocationLimit,
        },
        'Items/video_embed_threshold_mb': {
            'default': 50,
            'cast': int,
            'validate': lambda x: x >= 0,
        },
        'Items/video_embed_mode': {
            'default': 'auto',
            'validate': lambda x: x in ('auto', 'embed', 'reference'),
        },
        'Items/glb_embed_threshold_mb': {
            'default': 50,
            'cast': int,
            'validate': lambda x: x >= 0,
        },
        'Items/glb_embed_mode': {
            'default': 'auto',
            'validate': lambda x: x in ('auto', 'embed', 'reference'),
        },
        'BrowserCapture/enabled': {
            'default': True,
            'cast': bool,
        },
        'BrowserCapture/port': {
            'default': 47653,
            'cast': int,
            'validate': lambda x: 1024 <= x <= 65535,
        },
        'Autosave/enabled': {
            'default': True,
            'cast': bool,
        },
        'Autosave/minutes': {
            'default': 2,
            'cast': int,
            'validate': lambda x: 0 <= x <= 240,
        },
        # On-demand ("paged") opening of a project: open only the page
        # that is needed instead of the whole file.  This is the switch
        # the settings dialog drives; prism/view.py reads it through
        # valueOrDefault().  The value is stored as a real bool, and the
        # cast normalizes 'on'/'off'/'1'/'0'/'true'/'false' texts too,
        # because bool('off') is True.
        'Project/paged_open': {
            'default': PAGED_OPEN_DEFAULT,
            'cast': normalize_bool,
        },
    }

    def __init__(self):
        settings_format = QtCore.QSettings.Format.IniFormat
        settings_scope = QtCore.QSettings.Scope.UserScope
        settings_dir = self.get_settings_dir()
        path, mirror = _settings_mirror(settings_dir, settings_format, settings_scope)
        super().__init__(mirror, settings_format)
        self._persistence_path = path
        self._mirror_path = mirror
        self._async_sync_ready = True
        self._settings_array_modes = []
        app = QtCore.QCoreApplication.instance()
        if app is not None:
            app.setProperty('_prism_settings_directory', os.path.dirname(path))
        if app is not None and not app.property('_prism_settings_drain_installed'):
            app.setProperty('_prism_settings_drain_installed', True)
            app.aboutToQuit.connect(_drain_settings_sync)
            app.destroyed.connect(_drain_settings_sync)

    def _queue_sync(self, *, clear=False):
        if QtCore.QCoreApplication.instance() is not None:
            try:
                snapshot = _mirror_snapshot(self.fileName(), self._mirror_path, self.format())
                _request_settings_sync(self.fileName(), self.format(), snapshot, clear=clear)
            except Exception:
                logger.exception('Cannot schedule settings persistence')
                _settings_sync_status[self.fileName()] = QtCore.QSettings.Status.AccessError
                _notify_settings_failure('AccessError')

    def event(self, event):
        if (event.type() == QtCore.QEvent.Type.UpdateRequest
                and getattr(self, '_async_sync_ready', False)
                and QtCore.QCoreApplication.instance() is not None):
            try:
                self._queue_sync()
                return True
            except Exception:
                logger.exception('Cannot schedule settings persistence; using native fallback')
        return super().event(event)

    def request_sync(self):
        """Queue the final snapshot without waiting on the GUI thread."""
        self._queue_sync()
        with _settings_sync_lock:
            path = self.fileName()
            if path in _settings_sync_deadlines:
                _settings_sync_deadlines[path] = 0
                _settings_sync_condition.notify_all()

    def sync_pending(self):
        with _settings_sync_lock:
            return self.fileName() in _settings_sync_states

    def sync(self):
        self._queue_sync()
        _drain_settings_sync()
        # Preserve QSettings.sync's external-reload behavior, using the real
        # writer's completed read rather than opening the real INI on the GUI.
        path = self.fileName()
        with _settings_sync_lock:
            disk = _settings_disk_snapshots.get(path)
            baseline = _settings_persisted_snapshots.get(path)
            current = _mirror_snapshot(path, self._mirror_path, self.format())
            if (disk is not None and path not in _settings_sync_states
                    and _settings_sync_status.get(path) == QtCore.QSettings.Status.NoError
                    and current == baseline):
                reader = _settings_mirror_readers[(path, threading.get_ident())]
                for name in current.keys() - disk.keys():
                    reader.remove(name)
                for name, value in disk.items():
                    if name not in current or current[name] != value:
                        reader.setValue(name, value)
                _settings_persisted_snapshots[path] = disk
        # Private mirrors can flush here without touching the real INI lock.
        super().sync()

    def status(self):
        return _settings_sync_status.get(self.fileName(), super().status())

    def beginReadArray(self, prefix):
        result = super().beginReadArray(prefix)
        self._settings_array_modes.append(False)
        return result

    def beginWriteArray(self, prefix, size=-1):
        super().beginWriteArray(prefix, size)
        self._settings_array_modes.append(True)

    def endArray(self):
        writing = self._settings_array_modes.pop() if self._settings_array_modes else False
        super().endArray()
        if writing:
            self._queue_sync()

    def clear(self):
        super().clear()
        self._queue_sync(clear=True)

    def on_startup(self):
        """Settings to be applied on application startup."""

        if os.environ.get('QT_IMAGEIO_MAXALLOC'):
            alloc = int(os.environ['QT_IMAGEIO_MAXALLOC'])
        else:
            alloc = self.valueOrDefault('Items/image_allocation_limit')
        QtGui.QImageReader.setAllocationLimit(alloc)

    def setValue(self, key, value):
        # Keep callbacks synchronous, but do not dirty/rewrite the INI for an
        # identical value (geometry, recent-file and sidebar repeats).
        if not self.contains(key) or self.value(key) != value:
            super().setValue(key, value)
            self._queue_sync()
        if key in self.FIELDS and 'post_save_callback' in self.FIELDS[key]:
            self.FIELDS[key]['post_save_callback'](value)

    def remove(self, key):
        super().remove(key)
        self._queue_sync()
        if key in self.FIELDS and 'post_save_callback' in self.FIELDS[key]:
            value = self.valueOrDefault(key)
            self.FIELDS[key]['post_save_callback'](value)

    def valueOrDefault(self, key):
        """Get the value for key, or the default value specified in FIELDS.

        This is the method to be used for configurable settings (as
        opposed to settings that Prism stores on its own.)

        This will validate and type cast the given value if 'cast' and
        'validate' are specified in the FIELDS entry for the given
        key. The default value will be returned if validation or type
        casting fails.
        """

        val = self.value(key)
        conf = self.FIELDS[key]
        if val is None:
            val = conf['default']
        if 'cast' in conf:
            try:
                val = conf['cast'](val)
            except (ValueError, TypeError):
                val = conf['default']
        if 'validate' in conf:
            if not conf['validate'](val):
                val = conf['default']
        return val

    def value_changed(self, key):
        """Whether the value for given key has changed from its default."""

        return self.valueOrDefault(key) != self.FIELDS[key]['default']

    def restore_defaults(self):
        """Restore all the values specified in FILEDS to their default values
        by removing them from the settings file.
        """

        logger.debug('Restoring settings to defaults')
        for key in self.FIELDS.keys():
            self.remove(key)
        settings_events.restore_defaults.emit()

    def fileName(self):
        return self.__dict__.get('_persistence_path', os.path.normpath(super().fileName()))

    def get_settings_dir(self):  # pragma: no cover
        args = CommandlineArgs()
        return args.settings_dir

    def update_recent_files(self, filename):
        filename = os.path.abspath(filename)
        values = self.get_recent_files()
        if values and values[0] == filename:
            return
        if filename in values:
            values.remove(filename)
        values.insert(0, filename)

        self.beginWriteArray('RecentFiles')
        for i, filename in enumerate(values[:10]):
            self.setArrayIndex(i)
            self.setValue('path', filename)
        self.endArray()

    def get_recent_files(self, existing_only=False):
        values = []
        size = self.beginReadArray('RecentFiles')
        for i in range(size):
            self.setArrayIndex(i)
            values.append(self.value('path'))
        self.endArray()

        if existing_only:
            values = [f for f in values if os.path.exists(f)]
        return values
