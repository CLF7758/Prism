"""Worker-safe rectangle layout preparation. No Qt or model objects allowed.

The caller owns source/model generations and GUI installation. Cancellation
raises LayoutCancelled; a partial result must never replace the active layout.
"""
from collections import OrderedDict
from dataclasses import dataclass
import math

from prism.compact_layout import LayoutCancelled, check_cancelled, pack_sizes


@dataclass(frozen=True)
class LayoutGroup:
    rows: tuple
    bounds: tuple


@dataclass(frozen=True)
class LayoutPreparation:
    positions: tuple
    bounds: tuple
    groups: tuple


def prepare_asset_layout(sizes, canvas_ids=None, **options):
    """Compatibility pair (positions, bounds); see result API for group data."""
    result = prepare_asset_layout_result(sizes, canvas_ids, **options)
    return result.positions, result.bounds


def _as_pairs(flat, expected):
    """Expand a flat ``[w0, h0, w1, h1, ...]`` buffer into pairs.

    The GUI collects sizes into a flat integer buffer instead of a list of
    tuples: at 100k rows the tuple per row is a real share of the resident
    object graph, and the collector has no use for pairs. The expansion
    happens HERE, in the worker, so the pairs are short-lived and never
    reach the GUI thread's retained state.
    """
    if len(flat) != expected*2:
        raise ValueError('Flat size buffer must hold exactly two values per row')
    return [(flat[index], flat[index+1]) for index in range(0, len(flat), 2)]


def prepare_asset_layout_result(sizes, canvas_ids=None, *, content_sizes=None,
                                visual=False, viewport_width=0, cancelled=None,
                                min_group_items=96, batch_size=128,
                                flat_sizes=False, flat_content=False):
    """Prepare the existing AssetCanvas layout using immutable scalar data.

    ``sizes`` are positive integer pack rectangles (including caller padding).
    ``content_sizes`` optionally supplies exact floating card dimensions for
    group bounds. All returned coordinates exclude the GUI's +12px origin.
    Passing ``canvas_ids=None`` disables grouping, as for mixed search results.
    The cancellation callable is checked between bounded native pack calls,
    and during validation, grouping, coordinate flattening and group bounds.

    ``flat_sizes``/``flat_content`` accept the compact buffers the canvas
    collects (two values per row, no per-row tuple).
    """
    check_cancelled(cancelled)
    if batch_size < 1 or min_group_items < 1:
        raise ValueError('Layout group sizes must be positive')
    if flat_sizes:
        sizes = _as_pairs(sizes, len(sizes)//2)
    else:
        sizes = tuple(sizes)
    if canvas_ids is not None:
        canvas_ids = tuple(canvas_ids)
        if len(canvas_ids) != len(sizes):
            raise ValueError('Canvas IDs must correspond exactly to sizes')
    if content_sizes is not None:
        if flat_content:
            content_sizes = _as_pairs(content_sizes, len(content_sizes)//2)
        else:
            content_sizes = tuple(content_sizes)
        if len(content_sizes) != len(sizes):
            raise ValueError('Content sizes must correspond exactly to sizes')
    for index, size in enumerate(sizes):
        if index % 128 == 0:
            check_cancelled(cancelled)
        if len(size) != 2 or any(not isinstance(value, int) or value <= 0 for value in size):
            raise ValueError('Pack sizes must be positive integer pairs')
        if content_sizes is not None:
            content = content_sizes[index]
            if len(content) != 2 or any(not math.isfinite(value) or value <= 0 for value in content):
                raise ValueError('Content sizes must be finite positive pairs')
    if visual:
        limit = max(400, viewport_width-24)
        x = y = height = width = 0
        positions = []
        for index, (w, h) in enumerate(sizes):
            if index % 128 == 0:
                check_cancelled(cancelled)
            if x and x+w > limit:
                x, y, height = 0, y+height, 0
            positions.append((x, y))
            x += w
            width, height = max(width, x), max(height, h)
        check_cancelled(cancelled)
        return LayoutPreparation(tuple(positions), (width, y+height), ())
    if canvas_ids is None or len(sizes) < min_group_items:
        positions, bounds = pack_sizes(sizes, cancelled=cancelled)
        return LayoutPreparation(tuple(positions), bounds, ())
    canvases = OrderedDict()
    for index, canvas_id in enumerate(canvas_ids):
        if index % 128 == 0:
            check_cancelled(cancelled)
        canvases.setdefault(canvas_id, []).append(index)
    batches, layouts = [], []
    for rows in canvases.values():
        for start in range(0, len(rows), batch_size):
            check_cancelled(cancelled)
            batch = tuple(rows[start:start+batch_size])
            batches.append(batch)
            layouts.append(pack_sizes(tuple(sizes[row] for row in batch), cancelled=cancelled))
    origins, bounds = pack_sizes(tuple((w+12, h+12) for _positions, (w,h) in layouts),
                                cancelled=cancelled)
    positions = [(0,0)]*len(sizes)
    groups = []
    content = content_sizes if content_sizes is not None else sizes
    for rows, (local, _bounds), (ox, oy) in zip(batches, layouts, origins):
        check_cancelled(cancelled)
        left = top = float('inf')
        right = bottom = float('-inf')
        for row, (x,y) in zip(rows, local):
            px, py = x+ox, y+oy
            positions[row] = px, py
            w, h = content[row]
            left, top = min(left, px), min(top, py)
            right, bottom = max(right, px+w), max(bottom, py+h)
        groups.append(LayoutGroup(rows, (left, top, right-left, bottom-top)))
    check_cancelled(cancelled)
    return LayoutPreparation(tuple(positions), bounds, tuple(groups))
