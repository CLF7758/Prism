"""Shared native file drag transport for canvas assets and material originals."""
from pathlib import Path
from PyQt6 import QtCore, QtGui


def file_mime(paths):
    paths = [str(Path(path).resolve()) for path in paths]
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(path) for path in paths])
    mime.setText('\n'.join(paths))
    return mime


def start_file_drag(widget, paths, pixmap=None):
    drag = QtGui.QDrag(widget)
    drag.setMimeData(file_mime(paths))
    if pixmap is not None and not pixmap.isNull():
        drag.setPixmap(pixmap.scaled(160,120,QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                    QtCore.Qt.TransformationMode.SmoothTransformation))
    return drag.exec(QtCore.Qt.DropAction.CopyAction)


class DragFileStore:
    """Own temporary PNG exports until the canvas view is released.

    Referenced originals are returned as-is; temporary files are removed by
    this store's TemporaryDirectory, never by the native drag transport.
    """
    def __init__(self):
        import tempfile
        self.directory = tempfile.TemporaryDirectory(prefix='prism-external-drag-')

    def path_for(self, item, index):
        import os
        import re
        import tempfile
        filename = getattr(item, 'filename', '') or ''
        if os.path.isfile(filename):
            return os.path.abspath(filename)
        video_url = getattr(item, '_video_url', None)
        if video_url is not None and video_url.isLocalFile():
            path = video_url.toLocalFile()
            if os.path.isfile(path):
                return os.path.abspath(path)
        pixmap = getattr(item, 'pixmap', lambda: None)()
        if pixmap is None or pixmap.isNull():
            return None
        stem = os.path.splitext(os.path.basename(filename))[0] or f'image-{index}'
        stem = re.sub(r'[^A-Za-z0-9._-]+', '-', stem).strip('-') or f'image-{index}'
        fd, path = tempfile.mkstemp(prefix=stem[:80] + '-', suffix='.png', dir=self.directory.name)
        os.close(fd)
        if not pixmap.save(path, 'PNG'):
            os.unlink(path)
            return None
        return path
