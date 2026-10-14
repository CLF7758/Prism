"""Shared compact rectangle packing with bounded work per group."""
import rpack
import math


def pack_sizes(sizes):
    if not sizes:
        return [], (0, 0)
    if len(sizes) <= 64:
        side = max(max(max(w, h) for w, h in sizes), math.ceil(math.sqrt(sum(w*h for w, h in sizes))))
        while True:
            try:
                positions = rpack.pack(sizes, max_width=side, max_height=side)
                break
            except rpack.PackingImpossibleError:
                side = math.ceil(side*1.2)
        return positions, rpack.bbox_size(sizes, positions)
    # shortcut: pack groups hierarchically, rather than freezing the UI in a
    # global NP-hard search; move to a worker if real layouts still dominate.
    groups = [pack_sizes(sizes[start:start+64]) for start in range(0, len(sizes), 64)]
    origins, bounds = pack_sizes([group[1] for group in groups])
    positions = [(x+ox, y+oy) for (group, _), (ox, oy) in zip(groups, origins)
                 for x, y in group]
    return positions, bounds
