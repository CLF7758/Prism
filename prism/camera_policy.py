"""One camera policy for every canvas entry point.

Prism can be asked the same question in several ways -- "show me
everything on this canvas" -- and the answers used to differ:

* the menu action fitted the visible, materialised items of the main
  scene without any margin;
* the toolbar fitted every item of the aggregate canvas scene with a
  20 scene-unit margin and did nothing at all when that canvas was empty;
* the "back to centre" button measured the item rectangles itself, kept
  the current zoom, and used a different rectangle than the fit did.

Three answers to one question is what makes a camera feel broken: the same
button lands the picture in a different place depending on where it was
pressed from.

This module owns the parts that must not diverge:

``CAMERA_MARGIN``
    the single margin every fit applies, in logical screen pixels;
``fit_bounds(view, rect)``
    the single fit algorithm -- aspect ratio, margin, empty state, and the
    rule that a reset is not a toggle of the previous fit;
``visible_asset_bounds(view)`` / ``aggregate_content_bounds(canvas)``
    the boundary providers;
``request_reset(view, provider)`` / ``reset_if_stale(view)``
    the binding of a reset to the content generation it was asked for.

The providers deliberately do not know about each other.  A shared adapter
that asked the other canvas "what do you contain?" would have the two call
each other forever, so every provider answers for its own canvas only.

Rules the providers implement:

* the camera addresses the canvas that is on screen; the items of other
  canvases are hidden and therefore out of scope;
* only visible items count -- filtered out, deleted and hidden items stay
  out of the bounds;
* assets that are not materialised yet still count when their geometry is
  known: the camera shows the whole canvas, not the part that happens to
  be loaded.  Their extent comes from the stored crop and transform, never
  from decoding the original image;
* text frames and drawings count as well.  They are user content on the
  canvas; a canvas that holds nothing but a text frame is not an empty
  canvas.  Only the internal layers are left out: the selection frame,
  the control points, the rubber band, the record layer of the virtual
  asset index and the material layer of the aggregate canvas;
* a rotated asset contributes the bounding box of its four transformed
  corners, so rotating never pushes the camera off the content;
* an empty canvas returns to the identity transform centred on the origin.
"""
import logging

from PyQt6 import QtCore, QtGui

logger = logging.getLogger(__name__)

#: The uniform margin around the content, in **logical screen pixels**.
#:
#: It cannot be a scene quantity: 20 scene units are 20 pixels on a 1:1
#: canvas and a fraction of a pixel on a canvas shown at 5 %, so the
#: visible margin would change with the content.  The margin is taken off
#: the viewport's usable area instead; the device pixel ratio is Qt's
#: business and is not multiplied in here.
CAMERA_MARGIN = 20

#: The bounds of an aggregate canvas that has not been laid out yet are a
#: one pixel placeholder.  Fitting to that would zoom into empty space.
_PLACEHOLDER_SIZE = 2.0

#: Where a pending reset is kept on the view.
_PENDING_RESET = '_camera_pending_reset'


def clear_previous_transform(view):
    """A camera reset is not a toggle of the previous fit.

    ``fit_rect`` remembers the transform of a single item fit so that
    picking the same item again restores it.  A *reset* has to drop that
    memory, otherwise pressing "reset camera" twice would restore the
    camera of the first press instead of staying where it is.
    """
    if getattr(view, 'previous_transform', None) is not None:
        view.previous_transform = None


def fit_bounds(view, rect, margin=CAMERA_MARGIN):
    """Fit ``view`` to ``rect``.

    ``rect`` is what a boundary provider returned, in scene coordinates.
    ``None`` or an empty rectangle means "this canvas has nothing to
    show": the camera returns to the identity transform centred on the
    origin.

    Returns the rectangle that was fitted, or ``None`` for the empty case.
    """
    clear_previous_transform(view)
    if rect is None or rect.isEmpty():
        view.resetTransform()
        view.centerOn(0, 0)
        _announce_zoom(view)
        return None
    _fit_with_margin(view, rect, margin)
    _resize_scene_rect(view)
    # The scene rectangle can move the viewport while it is resized, so
    # fit once more to land on the same place every time.
    _fit_with_margin(view, rect, margin)
    _announce_zoom(view)
    return rect


def visible_asset_bounds(view):
    """The content bounds of the main canvas.

    The view already keeps the bounds of what it shows in
    ``_content_bounds()`` -- cached per canvas and per bounds revision --
    and the scene rectangle is derived from exactly those bounds.  Reusing
    them is what keeps the camera and the scene rectangle from disagreeing
    about which items are in scope, and it keeps the cost of a reset
    independent of how many assets the project holds.

    Views without that cache (plain graphics views, tests) fall back to
    the same walk over the scene's assets.
    """
    cached = getattr(view, '_content_bounds', None)
    if callable(cached):
        bounds = cached()
    else:
        bounds = _walk_assets(_scene_of(view))
    if bounds is None or bounds.isEmpty():
        return None
    return bounds


def _scene_of(view):
    """The scene of a view.

    ``QGraphicsView.scene`` is a method, but Prism's own view keeps the
    scene as an attribute -- ``view.scene`` is the scene object there.
    Both shapes have to work, because this module is called with the main
    view, with the aggregate canvas and with plain test views.
    """
    scene = getattr(view, 'scene', None)
    if callable(scene):
        scene = scene()
    return scene


def aggregate_content_bounds(canvas):
    """The content bounds of the aggregate/material canvas.

    That canvas paints its cards through a single scene item -- the
    material layer -- so the scene's bounding rectangle is the grid of the
    rows that are currently in scope.  The rows themselves are the cards,
    not every item in the scene.

    ``None`` means there is nothing to fit: either there are no rows at
    all, or the layout has not been committed yet and the layer still
    reports its one pixel placeholder.  Fitting to that placeholder would
    zoom into an empty canvas.
    """
    build=getattr(canvas,'_build_state',None)
    if build is not None and not build.get('keep_layout',False):
        return None
    cards = getattr(canvas, 'cards', None)
    if cards is not None and len(cards) == 0:
        return None
    scene = _scene_of(canvas)
    if scene is None:
        return None
    bounds = scene.itemsBoundingRect()
    if bounds.isEmpty():
        return None
    if (bounds.width() <= _PLACEHOLDER_SIZE
            and bounds.height() <= _PLACEHOLDER_SIZE):
        return None
    return bounds


def content_generation(view):
    """The revision of the content this view currently shows.

    A reset asked for while a query or a layout is still being prepared
    has to be re-applied once that work lands; this key is what tells the
    two apart.
    """
    scene = _scene_of(view)
    items = getattr(scene, '_viewport_items', None)
    model = getattr(view, 'model', None)
    rows = 0
    if callable(model):
        try:
            rows = len(model().items)
        except (AttributeError, TypeError):
            rows = 0
    return (getattr(scene, '_bounds_revision', 0),
            getattr(items, 'layout_revision', 0),
            getattr(view, 'current_canvas_id', None),
            getattr(view, '_render_layout_revision', 0),
            rows)


def request_reset(view, provider):
    """Bind a camera reset to the content generation it was asked for.

    ``provider`` is the boundary provider of this view -- it returns the
    bounds in scene coordinates, or ``None`` when the canvas is empty.

    The reset is applied right away.  When it was asked for before the
    content had arrived, the generation and the camera transform are
    remembered, so that :func:`reset_if_stale` can fit the new content
    once it lands.
    """
    bounds = provider(view)
    loader = getattr(view, '_paged_canvas_loader', None)
    canvas = getattr(view, 'current_canvas_id', None)
    pending = getattr(view, '_pending_canvas_demand', None)
    loading = ((pending is not None and pending == canvas) or
               (getattr(loader, 'active', False) and getattr(loader, 'canvas', None) == canvas))
    if bounds is None and loading:
        setattr(view, _PENDING_RESET, (content_generation(view), provider,
                                      QtGui.QTransform(view.transform()), True))
        logger.debug('Camera reset deferred until canvas %s finishes loading', canvas)
        return True
    fit_bounds(view, bounds)
    logger.info('Camera reset canvas=%s bounds=%s scale=%.6f',
                getattr(view,'current_canvas_id',None),bounds,view.transform().m11())
    setattr(view, _PENDING_RESET, (content_generation(view), provider,
                                   QtGui.QTransform(view.transform()),
                                   bounds is None))
    return True


def reset_if_stale(view):
    """Re-apply a pending reset when the content that was missing arrives.

    Returns ``True`` when a reset was applied.
    """
    pending = getattr(view, _PENDING_RESET, None)
    if pending is None:
        return False
    generation, provider, transform, was_empty = pending
    if not was_empty:
        # The reset had something to fit; it is not waiting for a layout.
        return False
    if provider(view) is None:
        return False
    if generation == content_generation(view):
        return False
    if view.transform() != transform:
        # The camera was moved since the reset; leave it alone.
        setattr(view, _PENDING_RESET, None)
        return False
    bounds = provider(view)
    fit_bounds(view, bounds)
    setattr(view, _PENDING_RESET, (content_generation(view), provider,
                                   QtGui.QTransform(view.transform()), False))
    return True


def layout_completed(view, previous_transform):
    """Honor a reset requested while layout was pending.

    Layout itself resets/restores a camera. Compare with the camera *before*
    that programmatic change, so it cannot masquerade as a user zoom.
    """
    pending=getattr(view,_PENDING_RESET,None)
    if pending is not None and pending[3] and previous_transform==pending[2]:
        request_reset(view,pending[1])


def _fit_with_margin(view, rect, margin):
    """Scale so that the content leaves ``margin`` pixels on every side.

    The margin belongs to the screen, so it is subtracted from the
    viewport's usable area -- the content has to fit into what is left.
    Adding it to the scene rectangle instead would make the visible margin
    depend on how far the canvas is zoomed out.
    """
    viewport = view.viewport().rect()
    usable = viewport.adjusted(margin, margin, -margin, -margin)
    if usable.width() <= 0 or usable.height() <= 0:
        usable = viewport
    scale = min(usable.width() / rect.width(),
                usable.height() / rect.height())
    transform = view.transform()
    transform.reset()
    transform.scale(scale, scale)
    view.setTransform(transform)
    view.centerOn(rect.center())


def _walk_assets(scene):
    """The bounds of the visible content of ``scene``.

    The walk covers the logical assets and the user content of the
    canvas -- the images, the text frames and the drawings -- so a record
    that is not materialised as a graphics item yet still contributes its
    extent.
    """
    visible = [item for item in scene.iter_assets()
               if hasattr(item, 'save_id') and item.isVisible()]
    if not visible:
        return QtCore.QRectF(0, 0, 0, 0)
    return scene.itemsBoundingRect(items=visible)


def _resize_scene_rect(view):
    """Let the view resize its scene rectangle after the camera moved."""
    timer = getattr(view, '_recalc_timer', None)
    if timer is not None:
        timer.stop()
    recalculate = getattr(view, 'recalc_scene_rect', None)
    if callable(recalculate):
        recalculate()


def _announce_zoom(view):
    """Publish the new zoom level, if this view publishes one."""
    signal = getattr(view, 'zoom_changed', None)
    if signal is not None:
        signal.emit(view.transform().m11() * 100.0)
