import logging
import os.path
from types import SimpleNamespace
from unittest.mock import patch

from PyQt6 import QtCore

from prism.logging import (
    PrismLogger,
    PrismRotatingFileHandler,
    qt_message_handler,
)


def test_sets_new_loglevel():
    assert logging.getLevelName(5) == 'TRACE'


@patch('prism.logging.PrismLogger.log')
def test_beelogger(log_mock):
    logger = PrismLogger('mylogger', logging.TRACE)
    logger.trace('blah: %s', 'spam', extra={'foo': 'bar'})
    log_mock.assert_called_once_with(
        logging.TRACE, 'blah: %s', 'spam', extra={'foo': 'bar'})


def test_rotating_file_handler_creates_new_dir(tmpdir):
    logfile = os.path.join(tmpdir, 'foo', 'bar.log')
    handler = PrismRotatingFileHandler(logfile)
    handler.emit(logging.LogRecord(
        'foo', logging.INFO, 'bar', 66, 'baz', [], None))
    handler.close()
    assert os.path.exists(logfile)


def testrotating_file_handler_uses_existing_dir(tmpdir):
    logfile = os.path.join(tmpdir, 'bar.log')
    handler = PrismRotatingFileHandler(logfile)
    handler.emit(logging.LogRecord(
        'foo', logging.INFO, 'bar', 66, 'baz', [], None))
    handler.close()
    assert os.path.exists(logfile)


def test_locked_rotation_does_not_grow_without_bound(tmp_path):
    logfile = tmp_path / 'locked.log'
    handler = PrismRotatingFileHandler(str(logfile), maxBytes=128, backupCount=1)
    try:
        with patch('os.rename', side_effect=PermissionError('locked')):
            for _ in range(100):
                handler.emit(logging.LogRecord('test', logging.INFO, '', 0, 'x'*80, (), None))
        assert logfile.stat().st_size <= 128*2+81
    finally:
        handler.close()


@patch('prism.logging.qtlogger.info')
def test_qt_message_handler_without(log_mock, qapp):
    qt_message_handler(QtCore.QtMsgType.QtInfoMsg, None, 'foo')
    log_mock.assert_called_once_with('foo')


@patch('prism.logging.qtlogger.warning')
def test_qt_message_handler_with_context(log_mock, qapp):
    ctx = SimpleNamespace(file='bla.txt', line='1', function='myfunc')
    qt_message_handler(QtCore.QtMsgType.QtWarningMsg, ctx, 'foo')
    log_mock.assert_called_once_with('foo: File bla.txt, line 1, in myfunc')
