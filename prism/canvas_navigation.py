"""Canvas navigation and interaction quality policy; no project I/O."""
import logging
import time
from PyQt6 import QtCore

logger = logging.getLogger(__name__)

#: How long the mouse keeps the decode priority after the last zoom.
#:
#: While the user zooms, the region under the pointer is served first.  The
#: focus is not kept forever: after this pause the priority falls back to
#: the centre of the viewport, so the images around it advance as well and
#: none of them stays blurred for good.
POINTER_FOCUS_TTL = 1.2


class CanvasNavigationMixin:
    def centerOn(self, *args):
        from prism.item_lifecycle import is_record
        if len(args) == 1 and is_record(args[0]):
            if self._cached_scene_bounds is None:
                self.recalc_scene_rect()
            args = (args[0].center_scene_coords,)
        return super().centerOn(*args)

    @property
    def _import_in_progress(self):
        return getattr(self, '_import_progress_flag', False)

    @_import_in_progress.setter
    def _import_in_progress(self, value):
        self._import_progress_flag = bool(value)
        self.scene._import_in_progress = bool(value)

    def _begin_interaction(self):
        self.scene._interaction_in_progress = True
        self._interaction_timer.start()

    def _end_interaction(self):
        self.scene._interaction_in_progress = False
        self.viewport().update()

    def _content_version(self):
        """The content epoch this view is currently displaying.

        Combines the target canvas id with the scene's bounds revision so a
        cached bounding rect computed for an older canvas (or an earlier load
        of the same canvas) is detected instead of being reused for the wrong
        page. ``switch_context`` changes the canvas id without touching the
        scene, so the id alone already invalidates the cache across canvases.
        """
        return (
            getattr(self, 'current_canvas_id', None),
            getattr(self.scene, '_bounds_revision', 0),
        )

    def canvas_ready_for(self, page_id):
        """True only when this canvas is showing ``page_id``'s content.

        A tab having changed is not readiness: the overview tiles keep
        per-canvas caches and the scene loads lazily, so both the target id and
        the tile context must agree before a destination-dependent action may
        treat the canvas as ready.
        """
        if getattr(self, 'current_canvas_id', None) != page_id:
            return False
        renderer = getattr(self, '_overview_renderer', None)
        tiles = getattr(renderer, 'tiles', None)
        if tiles is not None and getattr(tiles, 'context', None) != page_id:
            return False
        return True

    def _content_bounds(self):
        version = self._content_version()
        if (self._cached_scene_bounds is None
                or getattr(self, '_bounds_context', None) != version):
            self._cached_scene_bounds = self._visible_content_bounds()
            self._bounds_context = version
        return self._cached_scene_bounds

    def _visible_content_bounds(self):
        """The bounding box of the content this canvas actually shows.

        Every asset belongs to one canvas and is shown only on that
        canvas (``media_library._apply_filter`` sets ``show = canvas ==
        current_canvas``), so the canvas shows a few dozen of the 100 000
        records the project holds.  ``itemsBoundingRect()`` without an
        argument looks at *every* item in the scene and maps four corners
        for each of them: measured 15 308 ms over 8 calls, about 1.9 s per
        canvas switch, all of it spent on items the user cannot see.

        The scene rectangle has to cover what the user can see, and that
        is what ``on_action_fit_scene`` already fits the view to (see
        ``view.py``: the visible items, not all of them).  Both now
        agree, and the walk is over the visible items only.

        ``iter_assets()`` enumerates the assets without sorting them --
        the z-order is not needed for a bounding box, and sorting 100 000
        records per switch would cost more than the walk it saves.
        """
        scene = self.scene
        visible = [item for item in scene.iter_assets()
                   if hasattr(item, 'save_id') and item.isVisible()]
        if not visible:
            return QtCore.QRectF(0, 0, 0, 0)
        return scene.itemsBoundingRect(items=visible)

    def recalc_scene_rect(self):
        """Resize the scene rectangle so that it is always one view width
        wider than all items' bounding box at each side and one view
        width higher on top and bottom. This gives the impression of
        an infinite canvas."""

        if self.previous_transform:
            return
        logger.trace('Recalculating scene rectangle...')
        try:
            # 包围盒只算一次。itemsBoundingRect() 要遍历场景里每一项，
            # 而剖析显示这个函数在打开工程期间被调 172 次、累计 0.62 秒
            # —— 其中一半是拿同一个包围盒去取第二个角点。
            bounds = self._content_bounds()
            topleft = self.mapFromScene(bounds.topLeft())
            topleft = self.mapToScene(QtCore.QPoint(
                topleft.x() - self.size().width(),
                topleft.y() - self.size().height()))
            bottomright = self.mapFromScene(bounds.bottomRight())
            bottomright = self.mapToScene(QtCore.QPoint(
                bottomright.x() + self.size().width(),
                bottomright.y() + self.size().height()))
            self.setSceneRect(QtCore.QRectF(topleft, bottomright))
        except OverflowError:
            logger.info('Maximum scene size reached')
        logger.trace('Done recalculating scene rectangle')

    def get_zoom_size(self, func):
        """Calculates the size of all items' bounding box in the view's
        coordinates.

        This helps ensure that we never zoom out too much (scene
        becomes so tiny that items become invisible) or zoom in too
        much (causing overflow errors).

        :param func: Function which takes the width and height as
            arguments and turns it into a number, for ex. ``min`` or ``max``.
        """

        bounds = self._content_bounds()
        topleft = self.mapFromScene(bounds.topLeft())
        bottomright = self.mapFromScene(bounds.bottomRight())
        return func(bottomright.x() - topleft.x(),
                    bottomright.y() - topleft.y())

    def scale(self, *args, **kwargs):
        super().scale(*args, **kwargs)
        self.scene.on_view_scale_change()
        self._recalc_timer.stop()
        self.recalc_scene_rect()

    def get_scale(self):
        return self.transform().m11()

    def needs_display(self, item):
        renderer = getattr(self, '_overview_renderer', None)
        rect = renderer.tiles.prefetch_rect() if renderer else self.mapToScene(self.viewport().rect()).boundingRect()
        return item.scene() is self.scene and item.isVisible() and item.sceneBoundingRect().intersects(rect)

    def _pointer_focus(self):
        """The scene point the pointer is on, or ``None``.

        The focus lives for :data:`POINTER_FOCUS_TTL` seconds after the
        last zoom.  While it lives, the image under the pointer is served
        first; after it expires the priority is the one of the viewport
        centre again, so the rest of the visible images advance too.
        """
        anchor = getattr(self, '_pointer_focus_anchor', None)
        if anchor is None:
            return None
        at = getattr(self, '_pointer_focus_at', None)
        if at is None or (time.monotonic()-at) > POINTER_FOCUS_TTL:
            return None
        return self.mapToScene(anchor)

    def _remember_pointer_focus(self, anchor):
        """Remember where the pointer is, in viewport coordinates.

        The anchor is the very point a zoom keeps fixed.  It is mapped
        with the same mapping the zoom uses, so the region that stays
        still while the user zooms is the region that gets the pixels
        first.
        """
        self._pointer_focus_anchor = anchor
        self._pointer_focus_at = time.monotonic()

    def display_priority(self, item):
        """How far the item is from the part of the canvas that matters now.

        The mouse wins while it points at something: the region under the
        pointer is decoded before the rest of the viewport.  Without a
        valid pointer focus the centre of the viewport is used, as before.
        """
        focus = self._pointer_focus()
        if focus is None:
            focus = self.mapToScene(self.viewport().rect().center())
        delta = item.sceneBoundingRect().center()-focus
        return delta.x()*delta.x()+delta.y()*delta.y()

    def pan(self, delta):
        self._begin_interaction()
        if not self.scene._item_count:
            logger.debug('No items in scene; ignore pan')
            return

        hscroll = self.horizontalScrollBar()
        hscroll.setValue(int(hscroll.value() + delta.x()))
        vscroll = self.verticalScrollBar()
        vscroll.setValue(int(vscroll.value() + delta.y()))

    def zoom(self, delta, anchor):
        self._begin_interaction()
        if not self.scene._item_count:
            logger.debug('No items in scene; ignore zoom')
            return

        # We calculate where the anchor is before and after the zoom
        # and then move the view accordingly to keep the anchor fixed
        # We can't use QGraphicsView's AnchorUnderMouse since it
        # uses the current cursor position while we need the initial mouse
        # press position for zooming with Ctrl + Middle Drag
        anchor = QtCore.QPoint(round(anchor.x()),
                               round(anchor.y()))
        ref_point = self.mapToScene(anchor)
        if delta == 0:
            return
        # The mouse region is served first while the user zooms.  The
        # anchor is the point the zoom keeps fixed, so it is the point the
        # pointer is really on.
        self._remember_pointer_focus(anchor)
        factor = 1 + abs(delta / 1000)
        if delta > 0:
            if self.get_zoom_size(max) < 10000000:
                self.scale(factor, factor)
            else:
                logger.debug('Maximum zoom size reached')
                return
        else:
            if self.get_zoom_size(min) > 50:
                self.scale(1/factor, 1/factor)
            else:
                logger.debug('Minimum zoom size reached')
                return

        self.pan(self.mapFromScene(ref_point) - anchor)
        self.reset_previous_transform()
        self.zoom_changed.emit(self.transform().m11() * 100.0)

    def wheelEvent(self, event):
        if event.modifiers() == QtCore.Qt.KeyboardModifier.ControlModifier:
            delta=event.angleDelta().y() or event.pixelDelta().y()
            if delta:self.zoom(delta,event.position())
            event.accept();return

        action, inverted\
            = self.keyboard_settings.mousewheel_action_for_event(event)

        delta = event.angleDelta().y()
        if inverted:
            delta = delta * -1

        if action == 'zoom':
            self.zoom(delta, event.position())
            event.accept()
            return
        if action == 'pan_horizontal':
            self.pan(QtCore.QPointF(0, 0.5 * delta))
            event.accept()
            return
        if action == 'pan_vertical':
            self.pan(QtCore.QPointF(0.5 * delta, 0))
            event.accept()
            return

