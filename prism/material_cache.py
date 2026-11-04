"""Small display-only LRU budgets; never owns original project attachments."""
import threading
from collections import OrderedDict
from collections.abc import MutableMapping


class MaterialCache(MutableMapping):
    """A bounded LRU that the GUI thread reads while a worker fills it.

    Encoded thumbnails are produced by the background loader and consumed by
    the GUI thread, so both sides touch the same mapping.  Without a lock an
    eviction between a lookup and its ``move_to_end`` raises ``KeyError`` on
    the GUI thread, which is the one place that must never fail.
    """

    def __init__(self, max_entries, max_bytes, cost=lambda key, value: len(value)):
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.cost = cost
        self.entries = OrderedDict()
        self.bytes = 0
        self.hits = self.misses = 0
        self._lock = threading.RLock()
        from prism.resource_budget import shared_cache_budget
        self._cache_budget = shared_cache_budget()
        self._cache_budget.register(self, category='material_encoded', priority=30)

    def _trim_cache_to(self, target):
        with self._lock:
            while self.entries and self.bytes > target:
                _, (_, size) = self.entries.popitem(last=False)
                self.bytes -= size

    def __getitem__(self, key):
        with self._lock:
            try:
                value, size = self.entries[key]
            except KeyError:
                self.misses += 1
                raise
            self.hits += 1
            self.entries.move_to_end(key)
        self._cache_budget.touch(self)
        return value

    def __setitem__(self, key, value):
        with self._lock:
            if key in self.entries:
                self._drop(key)
            size = self.cost(key, value)
            if size > self.max_bytes:
                return
            self.entries[key] = (value, size)
            self.bytes += size
            while len(self.entries) > self.max_entries or self.bytes > self.max_bytes:
                _, (_, old_size) = self.entries.popitem(last=False)
                self.bytes -= old_size
        self._cache_budget.changed(self)

    def _drop(self, key):
        _, size = self.entries.pop(key)
        self.bytes -= size

    def __delitem__(self, key):
        with self._lock:
            self._drop(key)

    def __iter__(self):
        with self._lock:
            return iter(list(self.entries))

    def __len__(self):
        with self._lock:
            return len(self.entries)


class MaterialThumbnailCache:
    """Project-local encoded thumbnails, invalidated by attachment byte identity.

    A preview attachment ID is reused on updates. Keeping the bytes in the key
    avoids stale cards without depending on timestamps or scanning originals.
    """
    def __init__(self):
        self.cache = MaterialCache(512, 32 * 1024 * 1024,
                                  lambda key, value: len(key[0]) + len(value) * 2)

    def peek(self, record, attachments, edge=480):
        """The encoded thumbnail if it is already cached, else None.

        Nothing is loaded and nothing is decoded here. The GUI thread only
        asks whether a picture is already there; an uncached one stays
        pending and the card surface asks for it, which loads it in the
        background.  A thumbnail that is not resident yet cannot be keyed,
        because the key is the attachment's own bytes.
        """
        attachment_id = record.get('preview', {}).get('thumbnailAttachmentId')
        peek = getattr(attachments, 'peek', None)
        if peek is None:
            # A plain mapping has no resident/non-resident distinction: its
            # bytes are already in hand, so only the cache can be missing.
            attachment = attachments.get(attachment_id)
        else:
            attachment = peek(attachment_id)
        if not attachment or len(attachment) < 3 or not isinstance(attachment[2], bytes):
            return None
        return self.cache.get((attachment[2], edge))

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
