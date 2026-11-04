"""Project/session-bound metadata moves; never transfer or rename originals."""
import json
from PyQt6 import QtCore, QtGui
from PyQt6.QtCore import Qt

ASSET_MIME = 'application/x-prism-library-assets'

def start_library_drag(widget, folders, kind, ids, paths=()):
    ids = list(dict.fromkeys(ids))
    if not ids:
        return
    from prism.external_drag import file_mime
    mime = file_mime(paths) if paths else QtCore.QMimeData()
    mime.setData(ASSET_MIME, json.dumps(dict(project=folders.project_id,
        scope=str(id(folders)), kind=kind, ids=ids)).encode('utf-8'))
    drag = QtGui.QDrag(widget)
    drag.setMimeData(mime)
    return drag.exec(Qt.DropAction.MoveAction | Qt.DropAction.CopyAction,
                     Qt.DropAction.CopyAction) if paths else drag.exec(Qt.DropAction.MoveAction)

def read_library_drag(mime, folders):
    if not mime.hasFormat(ASSET_MIME):
        return None
    raw = bytes(mime.data(ASSET_MIME))
    if len(raw) > 1024 * 1024:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict) or value.get('project') != folders.project_id
            or value.get('scope') != str(id(folders))
            or value.get('kind') not in ('model', 'material')
            or not isinstance(value.get('ids'), list) or not value['ids']
            or not all(isinstance(i, str) and i for i in value['ids'])):
        return None
    value['ids'] = list(dict.fromkeys(value['ids']))
    return value
