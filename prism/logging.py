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

import logging
import logging.handlers
import os.path

from PyQt6 import QtCore


logging.TRACE = 5
logging.addLevelName(logging.TRACE, 'TRACE')


def _trace(self, msg, *args, **kwargs):
    self.log(logging.TRACE, msg, *args, **kwargs)


# Attached to the Logger class itself rather than only to PrismLogger:
# setLoggerClass() only affects loggers created after it runs, so a module
# that grabbed its logger before prism.logging was imported ends up with a
# plain Logger and raises AttributeError on the first logger.trace() call.
logging.Logger.trace = _trace


class PrismLogger(logging.Logger):

    def __init__(self, name, level=logging.NOTSET):
        super().__init__(name, level)
        logging.addLevelName(logging.TRACE, 'TRACE')

    def trace(self, msg, *args, **kwargs):
        self.log(logging.TRACE, msg, *args, **kwargs)


logging.setLoggerClass(PrismLogger)


#: Rotation failures are counted rather than swallowed.  A bounded log that
#: cannot rotate is still usable, but the bundle has to be able to say so.
_ROTATION = {'failures': 0, 'last_error': '', 'truncations': 0}


def rotation_failures():
    """How many rotations failed, and what the last error looked like."""
    return dict(_ROTATION)


class PrismRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that survives more than one process at once.

    Windows refuses to rename a file another process still has open, so when
    several Prism instances run side by side the rotation raises
    PermissionError and the logging module dumps the whole traceback to
    stderr for every single record - which buries the real errors.

    Failed rotation retains a bounded active file and reopens our stream,
    since the base class closes it before trying to rename.
    """

    def __init__(self, filename, **kwargs):
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        super().__init__(filename, **kwargs)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError as error:
            # A second process/native crash descriptor can lock the filename.
            # Retain a hard cap rather than letting failed rotation grow forever.
            _ROTATION['failures'] += 1
            _ROTATION['last_error'] = type(error).__name__
            try:
                with open(self.baseFilename, 'r+b') as bounded:
                    bounded.seek(0, 2)
                    if self.maxBytes > 0 and bounded.tell() >= self.maxBytes*2:
                        bounded.seek(0)
                        bounded.truncate()
                        _ROTATION['truncations'] += 1
            except OSError as truncation_error:
                _ROTATION['last_error'] = type(truncation_error).__name__
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass


qtlogger = logging.getLogger('Qt')
_lifecycle_diagnostics = {}


def _main_thread_stack():
    """The stack of the GUI thread, as text.

    A Qt warning that arrives on a worker thread has no Python frames of
    its own: the destructor runs inside C++.  The GUI thread is the one
    that dies from it, though -- it is the thread that owns the timer whose
    id stays registered in its dispatcher -- so its stack is the useful one.
    """
    import sys
    import threading
    try:
        frames = sys._current_frames()
        frame = frames.get(threading.main_thread().ident)
        if frame is None:
            return '<no main thread frame>'
        import traceback
        return ''.join(traceback.format_stack(frame))
    except Exception:                                  # pragma: no cover
        return '<no main thread stack>'


def _qt_thread_identity():
    """The Qt thread that produced the message, as ``(name, id)``."""
    try:
        thread = QtCore.QThread.currentThread()
        return thread.objectName(), int(thread.currentThreadId())
    except Exception:                                  # pragma: no cover
        return '?', 0


def qt_message_handler(mode, context, message):
    logfuncs = {
        QtCore.QtMsgType.QtDebugMsg: qtlogger.debug,
        QtCore.QtMsgType.QtInfoMsg: qtlogger.info,
        QtCore.QtMsgType.QtWarningMsg: qtlogger.warning,
        QtCore.QtMsgType.QtCriticalMsg: qtlogger.critical,
        QtCore.QtMsgType.QtFatalMsg: qtlogger.fatal,
    }
    if context and (context.file or context.line or context.function):
        message = (f'{message}: File {context.file}, line {context.line}, '
                   f'in {context.function}')

    logfuncs[mode](message)
    # Rare native lifecycle failures need the Python entry/thread, not just
    # Qt's warning. Bound diagnostics by category to avoid warning storms.
    category = next((key for key in ('Failed to create platform window',
                                    'QEventDispatcherWin32::wakeUp',
                                    'Timers cannot be stopped from another thread')
                     if key in message), None)
    if not category:
        return
    import threading
    import traceback
    seen = _lifecycle_diagnostics.get(category, 0) + 1
    _lifecycle_diagnostics[category] = seen
    if seen > 3:
        # The details are bounded, but the count must not be: a warning that
        # keeps coming is a different story than one that happened once,
        # and a silent cap hides exactly that difference.
        if seen == 4 or seen % 25 == 0:
            qtlogger.error('Qt lifecycle repeats: %s; category=%s; seen=%d',
                           message.split(': File ')[0], category, seen)
        return
    qt_name, qt_id = _qt_thread_identity()
    qtlogger.error(
        'Qt lifecycle context: thread=%s; qt_thread=%s/%s; category=%s; seen=%d\n'
        'offending thread stack:\n%s'
        'GUI thread stack:\n%s',
        threading.current_thread().name, qt_name, qt_id, category, seen,
        ''.join(traceback.format_stack(limit=18)), _main_thread_stack())
