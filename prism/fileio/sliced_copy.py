"""Cooperative deep copies of plain board-save data."""
from copy import deepcopy


_ATOMIC_TYPES = (type(None), bool, int, float, complex, str, bytes)


def sliced_copy(value):
    """Yield None between elements and return the detached copy.

    Use ``copied = yield from sliced_copy(value)`` inside a generator, or
    consume it with ``next()`` and read ``StopIteration.value``. Yielded
    values are scheduling checkpoints, not portions of the copied data.

    Built-in dict/list/tuple/set/frozenset values are copied incrementally;
    a per-call memo preserves aliases and cycles. Immutable scalar values,
    including strings and bytes, are shared. Other types (including container
    subclasses) use deepcopy with the same memo, which may block for one
    step. The caller must keep the source stable while consuming the copy.
    """
    return (yield from _copy_steps(value, {}))


def _copy_steps(value, memo):
    value_type = type(value)
    if value_type in _ATOMIC_TYPES:
        return value

    identity = id(value)
    if identity in memo:
        return memo[identity]

    if value_type is dict:
        copied = {}
        memo[identity] = copied
        for key, item in value.items():
            copied_key = yield from _copy_steps(key, memo)
            copied_item = yield from _copy_steps(item, memo)
            copied[copied_key] = copied_item
            yield None
        return copied

    if value_type is list:
        copied = []
        memo[identity] = copied
        for item in value:
            copied.append((yield from _copy_steps(item, memo)))
            yield None
        return copied

    if value_type is tuple:
        items = []
        for item in value:
            items.append((yield from _copy_steps(item, memo)))
            yield None
        # A recursive tuple -> mutable container -> tuple copy may already
        # have completed this tuple while copying one of its children.
        if identity in memo:
            return memo[identity]
        copied = tuple(items)
        memo[identity] = copied
        return copied

    if value_type is set:
        copied = set()
        memo[identity] = copied
        for item in value:
            copied.add((yield from _copy_steps(item, memo)))
            yield None
        return copied

    if value_type is frozenset:
        items = set()
        for item in value:
            items.add((yield from _copy_steps(item, memo)))
            yield None
        if identity in memo:
            return memo[identity]
        copied = frozenset(items)
        memo[identity] = copied
        return copied

    copied = deepcopy(value, memo)
    yield None
    return copied
