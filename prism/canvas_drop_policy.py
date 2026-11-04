"""Canvas destination policy, independent of scene and event lifetimes."""


def canvas_drop_destination(*, travelled, threshold, has_selection, candidate, current):
    """Return an eligible cross-canvas destination, or None without side effects."""
    if travelled < threshold or not has_selection:
        return None
    if candidate is None or candidate == current:
        return None
    return candidate
