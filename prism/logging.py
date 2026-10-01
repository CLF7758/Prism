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


class PrismRotatingFileHandler(logging.handlers.RotatingFileHandler):
    """RotatingFileHandler that survives more than one process at once.

    Windows refuses to rename a file another process still has open, so when
    several Prism instances run side by side the rotation raises
    PermissionError and the logging module dumps the whole traceback to
    stderr for every single record - which buries the real errors.

    A failed rotation is harmless: the log just keeps growing until a later
    attempt succeeds.  The important part is reopening our own stream, since
    the base class closes it before it tries to rename.
    """

    def __init__(self, filename, **kwargs):
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        super().__init__(filename, **kwargs)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            if self.stream is None:
                try:
                    self.stream = self._open()
                except OSError:
                    pass


qtlogger = logging.getLogger('Qt')


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
