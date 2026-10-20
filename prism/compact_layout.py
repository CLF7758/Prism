"""Shared compact rectangle packing with bounded work per group."""
import rpack
import math
from functools import lru_cache


def pack_sizes(sizes):
    positions, bounds = _pack_sizes(tuple(sizes))
    return list(positions), bounds


@lru_cache(maxsize=32)
def _pack_sizes(sizes):
    if not sizes:
        return [], (0, 0)
    if len(sizes) <= 16:
        side = max(max(max(w, h) for w, h in sizes), math.ceil(math.sqrt(sum(w*h for w, h in sizes))))
        while True:
            try:
                positions = rpack.pack(sizes, max_width=side, max_height=side)
                break
            except rpack.PackingImpossibleError:
                side = math.ceil(side*1.2)
        return tuple(positions), rpack.bbox_size(sizes, positions)
    # shortcut: pack groups hierarchically, rather than freezing the UI in a
    # global NP-hard search; move to a worker if real layouts still dominate.
    groups = [_pack_sizes(sizes[start:start+16]) for start in range(0, len(sizes), 16)]
    origins, bounds = _pack_sizes(tuple(group[1] for group in groups))
    positions = [(x+ox, y+oy) for (group, _), (ox, oy) in zip(groups, origins)
                 for x, y in group]
    return tuple(positions), bounds
