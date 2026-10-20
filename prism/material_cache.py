"""Small display-only LRU budgets; never owns original project attachments."""
from collections import OrderedDict
from collections.abc import MutableMapping


class MaterialCache(MutableMapping):
    def __init__(self, max_entries, max_bytes, cost=lambda key, value: len(value)):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.cost = cost
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = 0

    def __getitem__(self, key):
        try:
            value, size = self.entries[key]
        except KeyError:
            self.misses += 1
            raise
        self.hits += 1
        self.entries.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        if key in self.entries:
            del self[key]
        size = self.cost(key, value)
        if size > self.max_bytes:
            return
        self.entries[key] = (value, size)
        self.bytes += size
        while len(self) > self.max_entries or self.bytes > self.max_bytes:
            _, (_, old_size) = self.entries.popitem(last=False)
            self.bytes -= old_size

    def __delitem__(self, key):
        _, size = self.entries.pop(key)
        self.bytes -= size

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)


class MaterialThumbnailCache:
    """Project-local encoded thumbnails, invalidated by attachment byte identity.

    A preview attachment ID is reused on updates. Keeping the bytes in the key
    avoids stale cards without depending on timestamps or scanning originals.
    """
    def __init__(self):
        self.cache = MaterialCache(512, 32 * 1024 * 1024,
                                  lambda key, value: len(key[0]) + len(value) * 2)

    def get(self, record, attachments, loader, edge=480):
        attachment = attachments.get(record.get('preview', {}).get('thumbnailAttachmentId'))
        if not attachment or not isinstance(attachment[2], bytes):
            return None
        key = (attachment[2], edge)
        value = self.cache.get(key)
        if value is None:
            value = loader(record, attachments, edge)
            if value is not None:
                self.cache[key] = value
        return value
