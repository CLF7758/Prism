"""Helpers for the text frame (Prism 0.3.11 "text frame" hotfix).

The hotfix gave every text box an explicit frame: a width and a height of
its own.  The width feeds the Qt document layout, so a narrow box wraps the
text the way a word processor would; the height only clips the painting,
so the full text stays in the document and in the project file.

The rules live here so the item and the save/load path cannot drift apart.

**Units.**  The frame is measured in the item's *own* coordinates, so it
scales with the item's scale factor and with the view zoom.  Nothing in
this module is a device-pixel value, which is why the same numbers behave
at 100 % and at 800 % zoom, and on a 100 % or a 200 % display.

**Field names.**  ``frame_width`` / ``frame_height`` are the hotfix
release's own names: a project written by 0.3.11-text-frame loads here
unchanged, and a project written here loads in 0.3.11-text-frame.
"""
import math

#: The smallest frame a text box may be dragged to, in local units.
#:
#: 16 local units is the lower bound the hotfix release used.  It is a
#: *local* size -- it is not a screen size and not tied to any DPI -- and
#: the document margin (4 local units by default) is inside it, so there
#: is always room for at least one line of text.
MIN_FRAME_SIZE = 16.0

#: The saved field names (hotfix release compatible).
WIDTH_FIELD = 'frame_width'
HEIGHT_FIELD = 'frame_height'


def finite(value, fallback=None):
    """The value as a finite float, or ``fallback`` when it is not one."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def clamp_size(width, height):
    """Clamp a frame size to the usable range.

    A frame can never be zero, negative or infinite: a zero-width box
    would divide by zero while laying the text out, and a negative one
    would mirror the text.
    """
    return (max(MIN_FRAME_SIZE, finite(width, MIN_FRAME_SIZE)),
            max(MIN_FRAME_SIZE, finite(height, MIN_FRAME_SIZE)))


def read_size(data):
    """Read a saved frame size out of an item's data dict.

    ``None`` means "this item has **no** explicit frame" -- an old project
    keeps its adaptive, content-sized layout.  A broken value is treated
    the same way: the frame is dropped, the text is not.  A corrupt field
    must never cost the user their text.
    """
    if not isinstance(data, dict):
        return None
    width = finite(data.get(WIDTH_FIELD))
    height = finite(data.get(HEIGHT_FIELD))
    if width is None or height is None:
        return None
    if width <= 0 or height <= 0:
        return None
    return (width, height)
