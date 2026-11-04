"""Transient scalar metadata adapter for the existing browser predicate.

No image source, Qt geometry, scene ownership or editable state is allocated.
Keeping the same attribute protocol lets live edits and disk summaries use
exactly the same tag-expression and text predicates.
"""
import json


class CatalogMatchRecord:
    __slots__ = ('TYPE', '_canvas_id', '_tags', '_rating', '_title', 'filename',
                 '_notes', '_deleted_at', '_last_used')

    def __init__(self, row):
        self.TYPE = row.get('kind') or 'pixmap'
        self._canvas_id = row.get('canvas_id')
        self._tags = tuple(json.loads(row.get('tags') or '[]'))
        self._rating = max(0, min(5, int(row.get('rating') or 0)))
        self._title = row.get('title') or ''
        self.filename = row.get('filename') or ''
        self._notes = row.get('notes') or ''
        self._deleted_at = row.get('deleted_at')
        self._last_used = row.get('last_used', '')
