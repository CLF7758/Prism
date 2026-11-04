"""Cheap file/WAL identity shared by save receipts and derived catalogs."""
from pathlib import Path


def file_fingerprint(path):
    path = Path(path)

    def stamp(candidate):
        try:
            value = candidate.stat()
        except FileNotFoundError:
            return None
        return [value.st_size, value.st_mtime_ns, value.st_dev, value.st_ino]

    wal = stamp(Path(str(path) + '-wal'))
    return dict(path=str(path), file=stamp(path), wal=wal if wal and wal[0] else None)
