"""Local upgrade hints, never authority to open an unverified target."""
import hashlib
import json
import sqlite3
from pathlib import Path

from prism.source_fingerprint import file_fingerprint
from prism.storage.contracts import StorageError


def _key(source):
    normalized = str(Path(source).resolve()).casefold()
    return 'Storage/upgrade/' + hashlib.sha256(normalized.encode('utf-8')).hexdigest()


def hint(settings, source):
    if settings is None:
        return None
    try:
        raw = settings.value(_key(source))
        if not isinstance(raw, str) or len(raw) > 32768:
            return None
        value = json.loads(raw)
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('target'), str)
                or not isinstance(value.get('instance'), str)):
            return None
        return value
    except (ValueError, OSError, TypeError):
        return None


def validate(source, value):
    """Run off the GUI. Check source stat/WAL and published target binding.

    This reuses a prior conversion, not a fresh content hash certification.
    Editing the converted project is allowed; a different instance is not.
    """
    from prism.storage.entrypoint import is_entry, resolve
    from prism.storage.migration import read_manifest
    try:
        source = Path(source).resolve()
        before = file_fingerprint(source)
        if before['file'] is None or before != value.get('source'):
            return None
        target = Path(value['target']).resolve()
        if target == source or not is_entry(target):
            return None
        folder = resolve(target)
        manifest = read_manifest(folder)
        if (manifest.get('source') != before
                or manifest.get('storage_instance_id') != value['instance']
                or file_fingerprint(source) != before):
            return None
        return str(target)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error, StorageError):
        return None


def remember(settings, source, target):
    """Only call after the verified target has actually opened successfully."""
    if settings is None:
        return
    from prism.storage.entrypoint import resolve
    from prism.storage.migration import read_manifest
    source = Path(source).resolve()
    manifest = read_manifest(resolve(target))
    fingerprint = file_fingerprint(source)
    if manifest.get('source') != fingerprint:
        return
    settings.setValue(_key(source), json.dumps(dict(version=1, source=fingerprint,
        target=str(Path(target).resolve()), instance=manifest['storage_instance_id'])))
