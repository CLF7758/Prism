"""Opening releases its reader before returning or native QThread.finished."""

from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock, call
import sqlite3
import threading

import pytest
from PyQt6 import QtCore

from prism import fileio
from prism.fileio.sql import SQLiteIO, USER_VERSION


@pytest.mark.parametrize('scope', [None, [3, 5]])
@pytest.mark.parametrize('canceled', [False, True])
def test_load_prism_closes_reader_after_return(monkeypatch, tmp_path,
                                              scope, canceled):
    filename = str(tmp_path / 'mock.prism')
    scene = object()
    worker = SimpleNamespace(canceled=canceled)
    result = None if canceled else object()
    reader = Mock(spec=SQLiteIO)
    reader.read.return_value = result
    factory = Mock(return_value=reader)
    monkeypatch.setattr(fileio, 'SQLiteIO', factory)

    assert fileio.load_prism(filename, scene, worker=worker, scope=scope) is result

    factory.assert_called_once_with(filename, scene, readonly=True, worker=worker)
    assert reader.mock_calls == [call.read(scope=scope), call._close_connection()]


@pytest.mark.parametrize('error', [RuntimeError('read failed'),
                                   KeyboardInterrupt('read interrupted')])
def test_load_prism_closes_reader_after_exception(monkeypatch, tmp_path, error):
    filename = str(tmp_path / 'mock.prism')
    scene = object()
    scope = [7]
    reader = Mock(spec=SQLiteIO)
    reader.read.side_effect = error
    factory = Mock(return_value=reader)
    monkeypatch.setattr(fileio, 'SQLiteIO', factory)

    with pytest.raises(type(error)) as caught:
        fileio.load_prism(filename, scene, scope=scope)

    assert caught.value is error
    factory.assert_called_once_with(filename, scene, readonly=True, worker=None)
    assert reader.mock_calls == [call.read(scope=scope), call._close_connection()]


@pytest.mark.parametrize('outcome', ['success', 'canceled', 'exception'])
def test_sqlite_is_closed_before_native_finished(monkeypatch, tmp_path,
                                                 qapp, outcome):
    filename = str(tmp_path / 'temporary.prism')
    with closing(sqlite3.connect(filename)) as connection:
        connection.execute(f'PRAGMA user_version = {USER_VERSION}')
    before = (tmp_path / 'temporary.prism').read_bytes()
    scope = [11]
    error = RuntimeError('read failed')
    result = object() if outcome == 'success' else None
    retained = []
    events = []
    closed_at_finished = []

    def make_reader(filename, scene, readonly, worker):
        reader = SQLiteIO(filename, scene, readonly=readonly, worker=worker)
        connection = reader.connection
        # Keep the reader alive past finished: __del__ cannot provide cleanup.
        retained.extend([reader, connection])

        def read(*, scope):
            events.append(('read', threading.get_ident()))
            connection.execute('SELECT 1').fetchone()
            if outcome == 'exception':
                raise error
            assert worker.canceled == (outcome == 'canceled')
            return result

        reader.read = Mock(side_effect=read)
        close_connection = reader._close_connection

        def close_reader():
            close_connection()
            events.append(('close', threading.get_ident()))

        reader._close_connection = Mock(side_effect=close_reader)
        return reader

    monkeypatch.setattr(fileio, 'SQLiteIO', Mock(side_effect=make_reader))

    class OpenThread(QtCore.QThread):
        def __init__(self):
            super().__init__()
            self.canceled = outcome == 'canceled'
            self.result = None
            self.error = None

        def run(self):
            try:
                self.result = fileio.load_prism(
                    filename, object(), worker=self, scope=scope)
            except Exception as exc:
                self.error = exc

    def on_native_finished():
        events.append(('finished', threading.get_ident()))
        try:
            retained[1].execute('SELECT 1')
        except sqlite3.ProgrammingError as exc:
            closed_at_finished.append('closed' in str(exc))
        else:
            closed_at_finished.append(False)

    thread = OpenThread()
    thread.finished.connect(on_native_finished,
                            QtCore.Qt.ConnectionType.DirectConnection)
    thread.start()
    assert thread.wait(5000), 'Temporary SQLite reader thread did not finish'

    assert closed_at_finished == [True]
    assert [event for event, _ in events] == ['read', 'close', 'finished']
    assert len({thread_id for _, thread_id in events}) == 1
    assert events[0][1] != threading.get_ident()
    assert thread.error is (error if outcome == 'exception' else None)
    assert thread.result is result
    retained[0].read.assert_called_once_with(scope=scope)
    retained[0]._close_connection.assert_called_once_with()
    assert retained[0]._connection_closed
    assert not hasattr(retained[0], '_connection')
    assert not hasattr(retained[0], '_cursor')
    assert (tmp_path / 'temporary.prism').read_bytes() == before
