"""Worker-only persistent image levels; disposable, bounded, source-versioned."""
import hashlib
import os
import threading
from collections import OrderedDict
from prism import thumbnail_cache

_cache = None
_lock = threading.Lock()
LEVELS = (32, 64, 192, 512, 1024)
_archive_versions = OrderedDict()
_version_lock = threading.Lock()


def acknowledge_archive_save(filename, receipt):
    """Keep unchanged row previews only across a proven in-place partial save.

    No proof/unknown external change means no reuse. Changed/deleted/new IDs
    are excluded conservatively, even for metadata-only edits. State is
    bounded and session-local, never a replacement for source validation.
    """
    from prism.source_fingerprint import file_fingerprint
    before = getattr(receipt, 'source_before', None)
    after = getattr(receipt, 'source_after', None)
    changed_ids = getattr(receipt, 'catalog_item_ids', None)
    if before is None or after is None or changed_ids is None:
        return False
    path = os.path.normcase(os.path.abspath(str(filename)))
    try:
        if file_fingerprint(filename) != after:
            return False
    except OSError:
        return False
    with _version_lock:
        entry = _archive_versions.get(path)
        if entry is None or entry['proof'] != before:
            return False
        excluded = entry['excluded'] | set(changed_ids)
        if len(excluded) > 32768:
            _archive_versions.pop(path, None)
            return False
        entry.update(proof=after, excluded=excluded)
        _archive_versions.move_to_end(path)
    return True


def cache():
    global _cache
    # The preview store already resolved the session's settings location.
    # Reopening QSettings for every image can contend with its GUI-thread
    # deferred INI sync and adds filesystem work even on a pyramid hit.
    directory = os.path.join(thumbnail_cache.cache().directory, 'pyramid')
    with _lock:
        if _cache is None or _cache.directory != directory:
            _cache = thumbnail_cache.ThumbnailCache(directory, max_bytes=256*1024*1024, max_side=1024)
            _cache._prune_after = 16
        return _cache


def source_key(blob, path, archive):
    if blob is not None:
        return hashlib.blake2b(blob, digest_size=16).hexdigest()
    if archive and not path:
        from prism.storage.managed import root_for, binding
        if root_for(archive[0]) is not None:
            ref=binding(archive[0],'sqlar',archive[1],item_id=True)
            if ref is not None:
                return hashlib.blake2b(('managed:'+ref[0]).encode(),digest_size=16).hexdigest()
    filename = path or (archive[0] if archive else None)
    if not filename:
        return None
    try:
        stat = os.stat(filename)
    except OSError:
        return None
    normalized = os.path.normcase(os.path.abspath(filename))
    base = (normalized, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    if archive and not path:
        from prism.source_fingerprint import file_fingerprint
        try:
            proof = file_fingerprint(filename)
        except OSError:
            return None
        with _version_lock:
            entry = _archive_versions.get(normalized)
            if entry is None or entry['proof'] != proof:
                entry = dict(proof=proof, base=base, excluded=set())
                _archive_versions[normalized] = entry
            elif archive[1] not in entry['excluded']:
                base = entry['base']
            _archive_versions.move_to_end(normalized)
            while len(_archive_versions) > 32:
                _archive_versions.popitem(last=False)
        # WAL mutations must invalidate untrusted previews too. For the normal
        # closed archive retain the existing cache-key format for disk hits.
        if proof.get('wal') is not None:
            base = (*base, tuple(proof['wal']))
    identity = (*base, archive if not path else None)
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
