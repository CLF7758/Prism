"""Proof for reusing a displayed asset projection after a confirmed save.

This is not source storage or a second dirty tracker. Unknown chains always
fall back to the normal complete catalog query.
"""


def changed_ids_since(before, after, receipts, *, limit=256):
    if not before or not after:
        return None
    current = before
    active = False
    ids = set()
    for receipt in receipts:
        if not active:
            if receipt.source_before != before:
                continue
            active = True
        if receipt.source_before != current or receipt.source_after is None:
            return None
        current = receipt.source_after
        if any(type(key) is not int for key in receipt.catalog_item_ids):
            return None
        ids.update(receipt.catalog_item_ids)
        if len(ids) > limit:
            return None
    return tuple(sorted(ids)) if active and current == after else None
