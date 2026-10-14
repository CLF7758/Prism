"""The stable logical asset survives recycling its Qt display object."""
from PyQt6 import sip


def logical_item(item):
    return getattr(item, '__dict__', {}).get('_asset_record') or item


def is_record(item):
    return getattr(type(item), '_is_asset_record', False)


def is_deleted(item):
    return item._disposed if is_record(item) else sip.isdeleted(item)


def is_pixmap_item(item):
    return getattr(item, 'TYPE', None) in ('pixmap', 'glb')


def native_pixmap(item):
    from PyQt6 import QtGui, QtWidgets
    if is_record(item):
        graphics = item._graphics
        if graphics is not None:
            return QtWidgets.QGraphicsPixmapItem.pixmap(graphics)
        return item._derived_pixmap or QtGui.QPixmap()
    if isinstance(item, QtWidgets.QGraphicsPixmapItem):
        return QtWidgets.QGraphicsPixmapItem.pixmap(item)
    return None
