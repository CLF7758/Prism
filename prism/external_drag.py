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
