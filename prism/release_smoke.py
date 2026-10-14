"""Developer smoke check for the frozen duplicate worker bootstrap."""
import concurrent.futures
import json
import multiprocessing
from pathlib import Path


def run(output):
    from PyQt6 import QtCore, QtGui
    from prism.duplicate_worker import fingerprint_snapshot
    image = QtGui.QImage(1536, 1024, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('#354e7a'))
    painter = QtGui.QPainter(image)
    painter.fillRect(120, 100, 350, 800, QtGui.QColor('#dcb783'))
    painter.end()
    data = QtCore.QByteArray()
    buffer = QtCore.QBuffer(data)
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    snapshot = (bytes(data), None, None)
    expected = fingerprint_snapshot(snapshot)
    with concurrent.futures.ProcessPoolExecutor(max_workers=2,
            mp_context=multiprocessing.get_context('spawn')) as pool:
        results = list(pool.map(fingerprint_snapshot, [snapshot, snapshot]))
    assert expected[1] is not None and results == [expected, expected]
    Path(output).write_text(json.dumps({'ok': True, 'process_jobs': 2,
        'original_fingerprint_equal': True}), encoding='utf-8')
    return 0
