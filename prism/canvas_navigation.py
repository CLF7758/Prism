"""Canvas navigation and interaction quality policy; no project I/O."""
import logging
from PyQt6 import QtCore

logger = logging.getLogger(__name__)


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

    def _content_bounds(self):
        if self._cached_scene_bounds is None:
            self._cached_scene_bounds = self.scene.itemsBoundingRect()
        return self._cached_scene_bounds

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

    def display_priority(self, item):
        delta = item.sceneBoundingRect().center()-self.mapToScene(self.viewport().rect().center())
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

