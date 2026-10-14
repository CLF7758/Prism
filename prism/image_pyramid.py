"""Worker-only persistent image levels; disposable, bounded, source-versioned."""
import hashlib
import os
import threading
from prism import thumbnail_cache

_cache = None
_lock = threading.Lock()
LEVELS = (32, 64, 192, 512, 1024)


def cache():
    global _cache
    directory = os.path.join(thumbnail_cache.cache_directory(), 'pyramid')
    with _lock:
        if _cache is None or _cache.directory != directory:
            _cache = thumbnail_cache.ThumbnailCache(directory, max_bytes=256*1024*1024, max_side=1024)
            _cache._prune_after = 16
        return _cache


def source_key(blob, path, archive):
    if blob is not None:
        return hashlib.blake2b(blob, digest_size=16).hexdigest()
    filename = path or (archive[0] if archive else None)
    if not filename:
        return None
    try:
        stat = os.stat(filename)
    except OSError:
        return None
    identity = (os.path.normcase(os.path.abspath(filename)), stat.st_size,
                stat.st_mtime_ns, stat.st_ctime_ns, archive if not path else None)
    return hashlib.blake2b(repr(identity).encode(), digest_size=16).hexdigest()


def decode_display(blob=None, path=None, archive=None, max_side=None):
    from prism import image_decode
    if not max_side or max_side > LEVELS[-1]:
        return image_decode.decode(blob, path, archive, max_side=max_side)
    identity = source_key(blob, path, archive)
    if identity:
        store = cache()
        for level in LEVELS:
            if level >= max_side:
                preview = store.load(f'{identity}:{level}')
                if preview is not None:
                    image = thumbnail_cache.make_preview(preview.image, max_side)
                    return image, preview.size
    result = image_decode.decode(blob, path, archive, max_side=max_side)
    if result and identity:
        image, size = result
        for level in LEVELS:
            if level <= max_side:
                store.store(f'{identity}:{level}', thumbnail_cache.make_preview(image, level),
                            size.width(), size.height())
    return result
