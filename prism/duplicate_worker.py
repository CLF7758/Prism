"""Process-isolated original image fingerprints; no widgets or scene objects."""
import hashlib
from pathlib import Path

_application = None
_reader = None


def fingerprint_snapshot(snapshot):
    global _application, _reader
    from PyQt6 import QtCore, QtGui
    from prism.source_store import ArchiveReadSession
    from prism.similarity import difference_hash, average_color
    if _application is None:
        _application = QtGui.QGuiApplication.instance()
        if _application is None:
            import os
            os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
            _application = QtGui.QGuiApplication([])
    if _reader is None:
        _reader = ArchiveReadSession()
    blob, path, archive = snapshot
    try:
        data = blob if blob is not None else (Path(path).read_bytes() if path else _reader.read(archive[:2]))
        if not data and archive and len(archive) > 2:
            data = Path(archive[2]).read_bytes()
    except (OSError, ValueError):
        data = b''
    data = data or b''
    digest = hashlib.sha256(data).hexdigest()
    # SVG text depends on the application's font environment. Keep its
    # fingerprint in the parent process rather than changing rendered glyphs.
    if b'<svg' in data[:4096].lower():
        return digest, None
    image = QtGui.QImage.fromData(data)
    if image.isNull():
        return digest, None
    value = difference_hash(image)
    if value is None:
        return digest, None
    return digest, (value, average_color(image), image.width()/max(image.height(), 1))
