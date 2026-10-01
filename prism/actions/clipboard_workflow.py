"""Clipboard actions kept separate from the graphics view."""
import logging

from PyQt6 import QtWidgets

from prism import commands, widgets
from prism.i18n import _
from prism.items import PrismPixmapItem, PrismTextItem


logger = logging.getLogger(__name__)


def paste(view):
    """Paste Prism items, an image, or text at the cursor position."""
    view.cancel_active_modes()
    logger.debug('Pasting from clipboard...')
    clipboard = QtWidgets.QApplication.clipboard()
    position = view.mapToScene(view.mapFromGlobal(view.cursor().pos()))
    mime = clipboard.mimeData()
    data = mime.data('prism/items') if mime is not None else None
    logger.debug('Custom data in clipboard: %s', data)
    if data and view.scene.internal_clipboard:
        view.scene.paste_from_internal_clipboard(position)
        return
    image = clipboard.image()
    if not image.isNull():
        item = PrismPixmapItem(image)
        item._canvas_id = view.current_canvas_id
        view.undo_stack.push(commands.InsertItems(
            view.scene, [item], position))
        if len(view.scene.items()) == 1:
            view.on_action_fit_scene()
        return
    text = clipboard.text()
    if text:
        item = PrismTextItem(text)
        item._canvas_id = view.current_canvas_id
        item.setScale(1 / view.get_scale())
        view.undo_stack.push(commands.InsertItems(
            view.scene, [item], position))
        return
    message = _('No image data or text in clipboard or image too big')
    logger.info(message)
    widgets.PrismNotification(view, message)
