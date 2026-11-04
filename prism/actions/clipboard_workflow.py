"""Clipboard actions kept separate from the graphics view."""
import logging

from PyQt6 import QtCore, QtWidgets

from prism import commands, widgets
from prism.i18n import _
from prism.items import PrismPixmapItem, PrismTextItem


logger = logging.getLogger(__name__)


def paste(view):
    """Paste Prism items, an image, or text at the cursor position."""
    if not getattr(view, '_allow_import_during_open', lambda: True)():
        return
    view.cancel_active_modes()
    logger.debug('Pasting from clipboard...')
    clipboard = QtWidgets.QApplication.clipboard()
    position = view.mapToScene(view.mapFromGlobal(view.cursor().pos()))
    mime = clipboard.mimeData()
    data = mime.data('prism/items') if mime is not None else None
    logger.debug('Custom data in clipboard: %s', data)
    if data and view.scene.internal_clipboard:
        view.scene.paste_from_internal_clipboard(
            position, canvas_id=view._ensure_current_canvas())
        return
    image = clipboard.image()
    if not image.isNull():
        view._ensure_current_canvas()
        item = PrismPixmapItem(image)
        item._canvas_id = view.current_canvas_id
        view.undo_stack.push(commands.InsertItems(
            view.scene, [item], position))
        if len(view.scene.items()) == 1:
            view.on_action_fit_scene()
        return
    text = clipboard.text()
    if text:
        view._ensure_current_canvas()
        item = PrismTextItem(text)
        item._canvas_id = view.current_canvas_id
        item.setScale(1 / view.get_scale())
        view.undo_stack.push(commands.InsertItems(
            view.scene, [item], position))
        return
    message = _('No image data or text in clipboard or image too big')
    logger.info(message)
    widgets.PrismNotification(view, message)


def copy_selection(scene, clipboard):
    """Copy one scene selection through explicit scene/clipboard ports.

    No view, action state or application clipboard lookup is retained here.
    Return the copied count; an empty selection is a harmless no-op.
    """
    items = tuple(scene.selectedItems(user_only=True))
    if not items:
        return 0
    items[0].copy_to_clipboard(clipboard)
    scene.copy_selection_to_internal_clipboard()
    mime = clipboard.mimeData()
    if mime is not None:
        from prism.clipboard_capture import ClipboardCaptureAdapter
        ClipboardCaptureAdapter.mark_own_mime(mime)
        mime.setData('prism/items', QtCore.QByteArray.number(len(items)))
    return len(items)
