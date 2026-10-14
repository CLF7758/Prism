"""Disposable, bounded duplicate fingerprints; never modify original projects."""
import json
import sqlite3
import threading
import time
from pathlib import Path


class FingerprintCache:
    def __init__(self, directory, limit=4096):
        self.path = Path(directory)/'duplicate-fingerprints-v1.sqlite'
        self.limit = limit
        self.connection = None
        self.lock = threading.RLock()
        self.disabled = False
        self.writes = 0

    def _open(self):
        if self.disabled:
            return None
        if self.connection is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # This cache is optional: another Prism process must not add a
            # blocking retry per image when it owns the SQLite write lock.
            self.connection = sqlite3.connect(self.path, timeout=0, check_same_thread=False)
            self.connection.execute('CREATE TABLE IF NOT EXISTS fingerprints '
                                    '(identity TEXT PRIMARY KEY, payload TEXT NOT NULL, touched INTEGER NOT NULL)')
        return self.connection

    def get(self, identity):
        with self.lock:
            try:
                connection = self._open()
                if connection is None:
                    return None
                row = connection.execute('SELECT payload FROM fingerprints WHERE identity=?', (identity,)).fetchone()
                if row is None:
                    return None
                value, color, aspect = json.loads(row[0])
                if (type(value) is not int or not 0 <= value < 2**64 or len(color) != 3
                        or any(type(c) is not int or not 0 <= c <= 255 for c in color)
                        or not 0 < aspect < 1e6):
                    return None
                return value, tuple(color), aspect
            except (OSError, sqlite3.Error, ValueError, TypeError):
                return None

    def put(self, identity, feature):
        with self.lock:
            try:
                connection = self._open()
                if connection is not None:
                    connection.execute('INSERT OR REPLACE INTO fingerprints VALUES (?,?,?)',
                                       (identity, json.dumps(feature), time.time_ns()))
                    self.writes += 1
                    if self.writes % 128 == 0:
                        connection.execute('DELETE FROM fingerprints WHERE identity NOT IN '
                                           '(SELECT identity FROM fingerprints ORDER BY touched DESC, rowid DESC LIMIT ?)', (self.limit,))
                        connection.commit()
            except (OSError, sqlite3.Error):
                pass

    def close(self):
        with self.lock:
            if self.connection is not None:
                try:
                    self.connection.execute('DELETE FROM fingerprints WHERE identity NOT IN '
                                            '(SELECT identity FROM fingerprints ORDER BY touched DESC, rowid DESC LIMIT ?)', (self.limit,))
                    self.connection.commit()
                except sqlite3.Error:
                    pass
                finally:
                    self.connection.close()
                    self.connection = None
