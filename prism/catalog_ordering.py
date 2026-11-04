"""Detached asset result ordering; no Qt, scene or query installation state."""

def order_catalog_items(items, *, mode, sort_key, name, key, visual_keys=(), random_seed=0):
    result = list(items)
    if mode == 'visual':
        rank = {identity: position for position, identity in enumerate(visual_keys)}
        result = [item for item in result if key(item) in rank]
        result.sort(key=lambda item: rank[key(item)])
        return result
    if sort_key == 'name':
        result.sort(key=lambda item: name(item).casefold())
    elif sort_key == 'rating':
        result.sort(key=lambda item: getattr(item, '_rating', 0), reverse=True)
    if mode == 'random':
        import random
        random.Random(random_seed).shuffle(result)
    elif mode == 'recent':
        result.sort(key=lambda item: getattr(item, '_last_used', ''), reverse=True)
    return result
