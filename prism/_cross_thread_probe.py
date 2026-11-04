"""TEMPORARY diagnostic: name the object that dies on the wrong thread.

Qt refuses to unregister a running timer when the timer is stopped or
destroyed from a thread other than the one that created it:

    QObject::killTimer: Timers cannot be stopped from another thread
    QObject::~QObject: Timers cannot be stopped from another thread

Both messages leave the timer id registered in the owner thread's event
dispatcher.  The dispatcher later fires that id against freed memory, and the
process dies from the resulting use-after-free -- without a Windows crash
report, without a faulthandler dump and without the normal shutdown log.

The Qt message handler cannot say which object it was: the destructor runs in
C++ and the Python stack is empty.  This module therefore watches three
things:

* every cross-thread ``QTimer.start()`` / ``QTimer.stop()`` call, reported
  with the full Python stack of the calling thread;
* every background job -- ``QThreadPool`` runnables and ``QThread`` objects
  are wrapped so that the class that runs on a thread is written down;
* the receiver of every ``QTimer.singleShot`` -- Qt 6.7 implements it with an
  internal ``QSingleShotTimer``, a C++ object with a basic timer that no
  Python probe can see.

The job list matters most.  PyQt6 names the Python thread of every
``QThread.run()`` / ``QRunnable.run()`` with a global counter, so the
``Dummy-5`` in the log is the *fifth* background job of the process.  Once
the probe has written which class that was, the offending job is named.

**The probe must not change how objects are owned.**  An earlier version
replaced ``QtCore.QTimer`` with a Python subclass, and every timer in the
process became a Python-owned object: the interpreter then died with an
access violation while it shut down (measured with the A/B method: the same
test run exits with 0xC0000005 with the probe and cleanly without it).  The
probe therefore *shadows* the two methods instead of replacing the class, and
it connects one shared slot -- never one partial per object -- to the
``destroyed`` signal.  An even earlier version connected a partial to every
single QObject, and 70 000 objects turned that into 76 MB of log data and a
frozen GUI thread.

The probe is on unless ``PRISM_THREAD_PROBE=0`` is set.  The report goes to
its own file, because the process usually dies right after the report is
written and the regular log can lose its tail.

Remove this module once the owning object is fixed.
"""
import os
import sys
import threading
import time
import traceback

from PyQt6 import QtCore

#: The probe is a diagnostic, not a permanent part of the application.
ENABLE_ENV = 'PRISM_THREAD_PROBE'

#: A runaway report is worse than no report: it can freeze the GUI thread.
MAX_REPORTS = 2000

_real_timer = QtCore.QTimer
_log = None
_log_lock = threading.Lock()
_installed = False
_reports = 0


def _enabled():
    """On unless the environment explicitly turns the probe off.

    The probe adds one Python call to every timer and every background job,
    and nothing else.  The measured cost of this version is nil, so it stays
    on: without it a crash cannot be attributed to an object.
    """
    value = os.environ.get(ENABLE_ENV, '').strip().lower()
    return value not in ('0', 'false', 'no', 'off')


def _report_path():
    base = os.environ.get('APPDATA') or os.path.expanduser('~')
    return os.path.join(base, 'Prism', 'Prism-thread-probe.log')


def _write(text):
    global _log, _reports
    with _log_lock:
        if _reports >= MAX_REPORTS:
            return
        _reports += 1
        if _log is None:
            try:
                _log = open(_report_path(), 'a', encoding='utf-8', buffering=1)
            except OSError:
                return
        try:
            _log.write(text)
            _log.flush()
        except Exception:  # pragma: no cover - the report must never break the app
            pass


def _stamp():
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())


def _owner_label(parent):
    if parent is None:
        return '<no parent>'
    try:
        cls = type(parent)
        return '%s.%s' % (cls.__module__, cls.__name__)
    except Exception:
        return '<unknown owner>'


def _current_thread_label():
    try:
        return threading.current_thread().name
    except Exception:
        return '?'


def _stack():
    try:
        return ''.join(traceback.format_stack())
    except Exception:
        return '<no stack>'


def _report_cross_thread_call(timer, owner, operation):
    try:
        owner_thread = timer.thread()
        current = QtCore.QThread.currentThread()
        if owner_thread is current or owner_thread == current:
            return
    except Exception:
        return
    _write(
        '%s  CROSS-THREAD %s  timer=%s  owner=%s  thread=%s\n%s\n'
        % (_stamp(), operation, _real_timer.__name__, owner, _current_thread_label(),
           _stack()))


def _report_destroyed(obj):
    """``destroyed`` is emitted from ~QObject, in the destroying thread.

    One shared slot serves every connection: a partial per object would put
    tens of thousands of Python objects into the collector's way, and that
    is what froze the GUI thread in an earlier version of this probe.
    """
    if threading.current_thread() is threading.main_thread():
        return
    _write('%s  DESTROYED OFF GUI THREAD  owner=%s  thread=%s\n%s\n'
           % (_stamp(), _owner_label(obj), _current_thread_label(), _stack()))


def _qt_thread_label():
    """The Qt thread that is running right now.

    A QThreadPool worker reports ``Thread (pooled)``; a QThread reports the
    name it was given.  That is what tells the two apart when the warning
    arrives on a native thread that Python never created.
    """
    try:
        thread = QtCore.QThread.currentThread()
        return thread.objectName(), int(thread.currentThreadId())
    except Exception:
        return '?', 0


def _qt_message_handler(mode, context, message):
    """Chain Prism's handler and add the Qt thread that produced the message."""
    try:
        from prism.logging import qt_message_handler as previous
    except Exception:                                  # pragma: no cover
        previous = None
    if previous is not None:
        try:
            previous(mode, context, message)
        except Exception:                              # pragma: no cover
            pass
    if 'Timers cannot be' not in message:
        return
    name, ident = _qt_thread_label()
    _write('%s  QT THREAD WARNING  qt_thread_name=%r  qt_thread_id=%s'
           '  python_thread=%s\n%s\n'
           % (_stamp(), name, ident, _current_thread_label(), _stack()))


_real_timer_start = _real_timer.start
_real_timer_stop = _real_timer.stop
_real_single_shot = _real_timer.singleShot


def _timer_start(self, *args, **kwargs):
    # The default of ``start()`` keeps the current interval, so the
    # arguments are passed through unchanged.
    _report_cross_thread_call(self, _owner_label(self.parent()), 'start()')
    return _real_timer_start(self, *args, **kwargs)


def _timer_stop(self, *args, **kwargs):
    _report_cross_thread_call(self, _owner_label(self.parent()), 'stop()')
    return _real_timer_stop(self, *args, **kwargs)


class _LoggedRunnable(QtCore.QRunnable):
    """Wraps a runnable so the job's class is written down when it runs."""

    def __init__(self, original):
        super().__init__()
        self._original = original

    def run(self):
        try:
            name = '%s.%s' % (type(self._original).__module__,
                              type(self._original).__name__)
        except Exception:
            name = '<unknown>'
        _write('%s  JOB START  thread=%s  runnable=%s\n'
               % (_stamp(), _current_thread_label(), name))
        return self._original.run()


_real_pool_start = QtCore.QThreadPool.start
_real_thread_start = QtCore.QThread.start


def _single_shot_receiver(args):
    """The QObject a ``QTimer.singleShot`` call is aimed at.

    Qt 6.7 implements ``singleShot`` with an internal ``QSingleShotTimer``:
    a C++ object with a basic timer that no Python probe can see.  It lives
    as long as its receiver, and it dies with it -- on whatever thread
    destroys the receiver.  Remembering the receiver is therefore the only
    way to attribute the warning to a call site.
    """
    for arg in args:
        if isinstance(arg, QtCore.QObject):
            return arg
        owner = getattr(arg, '__self__', None)
        if isinstance(owner, QtCore.QObject):
            return owner
    return None


def _single_shot(*args, **kwargs):
    receiver = _single_shot_receiver(args)
    if receiver is not None:
        try:
            receiver.destroyed.connect(_report_destroyed)
        except Exception:
            pass
    return _real_single_shot(*args, **kwargs)


def _pool_start(self, runnable, *args, **kwargs):
    try:
        runnable = _LoggedRunnable(runnable)
    except Exception:
        pass
    return _real_pool_start(self, runnable, *args, **kwargs)


def _thread_start(self, *args, **kwargs):
    try:
        if not getattr(self, '_probe_run_wrapped', False):
            self._probe_run_wrapped = True
            original_run = self.run

            def run_wrapper(*run_args, **run_kwargs):
                name = '%s.%s' % (type(self).__module__, type(self).__name__)
                _write('%s  JOB START  thread=%s  qthread=%s\n'
                       % (_stamp(), _current_thread_label(), name))
                return original_run(*run_args, **run_kwargs)

            self.run = run_wrapper
    except Exception:
        pass
    return _real_thread_start(self, *args, **kwargs)


def install():
    """Watch the timers and the background jobs.

    Set ``PRISM_THREAD_PROBE=0`` to switch the probe off.  It adds a Python
    call to every timer and every background job, which is the price of
    being able to say *which* object died on the wrong thread.
    """
    global _installed
    if _installed:
        return
    _installed = True
    if not _enabled():
        return
    # Shadow the two methods instead of replacing the class: a Python
    # subclass would make every timer a Python-owned object, and the
    # interpreter then dies with an access violation while it shuts down.
    try:
        _real_timer.start = _timer_start
        _real_timer.stop = _timer_stop
    except Exception:                                  # pragma: no cover
        pass
    # singleShot is a static method: it has to stay one, or an instance
    # call (``timer.singleShot(...)``) would pass the timer as the delay.
    try:
        _real_timer.singleShot = staticmethod(_single_shot)
    except Exception:                                  # pragma: no cover
        pass
    # Every background job writes its class down when it starts.
    try:
        QtCore.QThreadPool.start = _pool_start
    except Exception:                                  # pragma: no cover
        pass
    try:
        QtCore.QThread.start = _thread_start
    except Exception:                                  # pragma: no cover
        pass
    # Chain Prism's own handler so the report keeps the Qt thread that
    # produced the warning.
    try:
        QtCore.qInstallMessageHandler(_qt_message_handler)
    except Exception:                                  # pragma: no cover
        pass
    _write('%s  probe installed  pid=%s  python=%s\n'
           % (_stamp(), os.getpid(), sys.version.split()[0]))


def report(message):
    """Write an arbitrary marker into the probe log."""
    _write('%s  %s\n' % (_stamp(), message))
