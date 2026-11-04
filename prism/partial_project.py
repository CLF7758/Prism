"""Partially loaded projects: dirty tracking, capture and save receipts.

Companion to :mod:`prism.fileio.partial_save`, which owns the SQL. This module
decides *what* changed, under four rules:

* the page read at open time is the whole diff base. A record that was never
  loaded can therefore never be rewritten, and never looks deleted;
* a loaded row that disappeared is refused. Real deletion needs an explicit
  command plus source-lifecycle handling, not "it is not in the scene";
* only differing keys are written, so metadata this version never understood
  stays on disk untouched;
* an acknowledged save advances the base to exactly what the receipt covered,
  so edits made while the save was running stay dirty and are saved next time.

A page body is only read when the page can have changed.  The editor bumps a
page's modification version whenever it changes what the page stores, and a
version equal to the one the file holds proves that the page did not change
since the last acknowledged save -- so its body is not read, parsed or
written at all.  A page without a version has no reliable dirty record: it is
then compared with its baseline, which is slower but never loses an edit.

Hard deletion of an asset is refused rather than guessed: item rows share their
original bytes per project, so no partial snapshot may drop one. Clearing a mind
map that already has a tree, and permanently deleting a resource-tree node, are
supported -- but only through the explicit authorization below, so a purge can
never leave a project that refuses every later save. ``scene._partial_project_source``
is the marker the file writer already requires before it accepts a partial snapshot.

A session also owns the fact that *Save As* moved it to another file.  The
whole-project catalogue is a cache of one particular file, so after a Save As it
describes the previous project until the UI rebuilds it; the session reports
that through :meth:`PartialProjectSession.requires_catalog_rebuild` and
:meth:`PartialProjectSession.install_source_rebind_hook`.
"""
import json
import logging
from contextlib import closing
from pathlib import Path
import sqlite3
import time

from prism.fileio.partial_save import (PartialReadSnapshot, PartialSaveCancelled, PartialSaveConflict,
                                       PartialSaveSnapshot, RowInsert, RowPatch,
                                       capture_partial_save, item_save_columns,
                                       read_partial_records)
from prism.partial_residency import PartialAssetResidency, rebind_asset_sources

ITEM_TABLE = 'items'
PAGE_TABLE = 'workspace_pages'
NOTE_TABLE = 'board_notes'
MINDMAP_TABLE = 'board_mindmap'

#: Whole-project catalogue states a session can report.  ``stale`` means the
#: catalogue still describes the file this session came from.
CATALOG_SCOPE_CURRENT = 'current'
CATALOG_SCOPE_STALE = 'stale'

#: Status text for the window between "Save As" and "the new catalogue is
#: ready".  The UI shows it in the status bar.
CATALOG_REBUILDING_MESSAGE = 'Rebuilding the project index…'

logger = logging.getLogger(__name__)

#: ``items`` columns compared for change, in database order.
ITEM_FIELDS = ('type', 'x', 'y', 'z', 'scale', 'rotation', 'flip', 'data')
#: ``workspace_pages`` columns, in database order.
PAGE_FIELDS = ('kind', 'title', 'data', 'sort_order')

#: The body keys a workspace page stores.  Every other key of a stored page
#: payload belongs to a version of Prism that may not exist yet, so it is
#: carried over untouched.
PAGE_BODY_KEYS = ('content', 'tags', 'parent', 'permanent')
GLOBAL_METADATA_KEYS = ('tags', 'categories', 'library_queries', 'tag_system')

#: The envelope marker of a stored page payload.
PAGE_PAYLOAD_MARKER = '__prism_page_payload__'

#: Reserved key on a workspace page dict: its modification version.
#:
#: The editor bumps the version every time it changes what the page
#: stores -- an edit, a clear, an import, an undo or a redo.  The save
#: compares it with the version the file holds, so a page that did not
#: change since the last acknowledged save is skipped without its body
#: being read, parsed or written at all.
PAGE_VERSION_KEY = '__prism_page_version__'

#: Keys the metadata writer omits once they stop applying (trash -> restore).
#: Everything else is merged only, so an unloaded/unknown field is never dropped.
REMOVABLE_ITEM_KEYS = frozenset({'deletedAt'})

_MISSING = object()


def _attachment_ids(value, *, cancel=None):
    """Known attachment references, including packages and preview metadata.

    The same nested structure is often referenced from many places, so it is
    walked once; the walk also checks for cancellation between the entries,
    because a long traversal must not hold the save past a cancel.
    """
    result = set()
    seen = set()

    def walk(node):
        if cancel is not None and cancel():
            raise PartialSaveCancelled('Attachment scan cancelled')
        if isinstance(node, (dict, list)):
            # The same container reached twice carries the same references,
            # so walking it again can only repeat work, never add results.
            marker = id(node)
            if marker in seen:
                return
            seen.add(marker)
        if isinstance(node, dict):
            for key, child in node.items():
                if key in ('attachmentId', 'thumbnailAttachmentId') and child is not None:
                    if not isinstance(child, int) or isinstance(child, bool) or child < 1:
                        raise PartialSaveConflict('Invalid material attachment reference')
                    result.add(child)
                elif isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return result


def _merge_state(before, current):
    result = dict(before)
    for key, value in current.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge_state(result[key], value)
        else:
            result[key] = value
    return result


def _check_cancel(cancel):
    if cancel is not None and cancel():
        raise PartialSaveCancelled('Project baseline preparation cancelled')


def read_baseline(source, references, *, cancel=None):
    """Read the diff base for a bounded set of records, in batches of 1024."""
    _check_cancel(cancel)
    references = list(references)
    merged = None
    for start in range(0, len(references), 1024):
        _check_cancel(cancel)
        chunk = read_partial_records(source, references[start:start + 1024])
        _check_cancel(cancel)
        if merged is None:
            merged = chunk
            continue
        if (chunk.source != merged.source or chunk.source_identity != merged.source_identity
                or chunk.project_id != merged.project_id
                or chunk.schema_version != merged.schema_version):
            raise PartialSaveConflict('Project changed while the page baseline was read')
        merged = PartialReadSnapshot(merged.source, chunk.source_identity,
                                     merged.project_id, merged.schema_version,
                                     merged.records + chunk.records)
    if merged is None:
        # An empty page still needs identity, so a later save can verify it.
        merged = read_partial_records(source, [])
    _check_cancel(cancel)
    return merged


def item_values(item):
    """Current ``items`` columns for one item, with the metadata decoded."""
    columns = item_save_columns(item)
    return (*columns[:-1], _json_object(columns[-1]))


def _item_canvas_id(item):
    """The ``canvasId`` this item would be saved with, without the conversion.

    ``item_values`` serialises the whole extra save data -- the base64
    thumbnail included -- and decodes it again, only for the canvas guard to
    read one key out of it.  ``get_metadata_save_data`` is where that key
    comes from, so asking it there gives the same answer for a fraction of
    the cost.
    """
    metadata = getattr(item, 'get_metadata_save_data', None)
    if metadata is None:
        return item_values(item)[-1].get('canvasId', 'default-canvas')
    return metadata().get('canvasId', 'default-canvas')


def _json_object(value):
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    data = json.loads(value)
    if not isinstance(data, dict):
        raise PartialSaveConflict('Structured column is not an object')
    return data


def _changed_json(base, current):
    """Nested merge payload: only keys whose value actually differs.

    Removals never come from here. A loader that did not understand a field
    must not be able to delete it just because it is absent from memory.
    """
    updates = {}
    for key, value in current.items():
        before = base.get(key, _MISSING)
        if before is _MISSING:
            updates[key] = value
        elif isinstance(value, dict) and isinstance(before, dict):
            nested = _changed_json(before, value)
            if nested:
                updates[key] = nested
        elif value != before:
            updates[key] = value
    return updates


def _immutable(value):
    """True when a value cannot change after it was handed out.

    A string that is still the same object is still the same text, so the
    object itself is a reliable "did this change" token for it.  A mapping
    or a list can be edited in place, so for those the object proves
    nothing.
    """
    return isinstance(value, (str, bytes, int, float, bool, type(None)))


def page_version(page):
    """The modification version of one workspace page, or ``None``.

    A version is an integer the editor bumps every time it changes what the
    page stores.  ``None`` means the page carries no reliable dirty record:
    the caller must then compare the page with its baseline instead of
    trusting a version it did not write itself.
    """
    version = page.get(PAGE_VERSION_KEY)
    if isinstance(version, bool) or not isinstance(version, int):
        return None
    return version


def page_row(page, order):
    """``workspace_pages`` columns for one in-memory page.

    The body is a mapping, not JSON text.  The text is only needed for a
    page that really changed, and serialising the body of *every* page of
    the project on *every* save is what made saving a large project stall
    for a second.
    """
    payload = {PAGE_PAYLOAD_MARKER: True,
               'content': page.get('content'),
               'tags': page.get('tags', []),
               'parent': page.get('parent'),
               'permanent': bool(page.get('permanent'))}
    return (page.get('kind'), page.get('title'), payload, order)


def _save_id(item):
    return getattr(item, 'save_id', None) or getattr(item, 'original_save_id', None)


def _is_error_item(item):
    from prism.items import PrismErrorItem
    return getattr(item, 'TYPE', None) == PrismErrorItem.TYPE


def _assets(scene):
    """Every logical asset the scene would save, including the record layer."""
    iterator = getattr(scene, 'iter_assets', None)
    if iterator is not None:
        yield from iterator()
        return
    yield from getattr(scene, 'items_for_save', list)()


def _board_keys(source):
    """(note row id, mind map board id) that a partial save must cover.

    Both boards are singletons keyed by ``board_id = 0``. ``None`` means the row
    does not exist yet and a save would have to insert it.
    """
    path = Path(source).resolve(strict=True)
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=.25)) as connection:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        note = connection.execute(
            'SELECT id FROM board_notes WHERE board_id=0 LIMIT 1').fetchone() \
            if 'board_notes' in tables else None
        mindmap = connection.execute(
            'SELECT board_id FROM board_mindmap WHERE board_id=0').fetchone() \
            if 'board_mindmap' in tables else None
    return (note[0] if note else None), (mindmap[0] if mindmap else None)


class PartialProjectSession:
    """The GUI half of a partially loaded project.

    One session covers one open project and the records its pages loaded. It is
    deliberately small: no scene copy, no serialized document, only the rows it
    was told about and the ones the user touched.
    """

    def __init__(self, baseline, *, loaded_pages=(), pages_complete=False,
                 note_key=None, mindmap_key=None, attachments=False):
        self.baseline = baseline
        self.source = baseline.source
        self.source_identity = baseline.source_identity
        self.project_id = baseline.project_id
        self.schema_version = baseline.schema_version
        self._rows = {(table, key): row for table, key, row in baseline.records}
        self.loaded_pages = frozenset(loaded_pages)
        self.pages_complete = pages_complete
        self.note_key = note_key
        self.mindmap_key = mindmap_key
        self.attachments = attachments
        #: Set when the base can no longer be trusted; every later save refuses.
        self.invalid = None
        self.acknowledged = None
        self.revision = 0
        self._canvas_rows = {}
        self._canvas_moves = {}
        self._domains = None
        self._domain_deletions = set()
        self._library_state_before = _MISSING
        self._metadata_scope = False
        self._valid_domain_attachments = set()
        self._residency = PartialAssetResidency()
        self._mindmap_clear_authorized = False
        #: Whole-project catalogue state.  ``CATALOG_SCOPE_STALE`` means the
        #: catalogue still describes the file this session came from; see
        #: :meth:`requires_catalog_rebuild`.
        self._catalog_scope_state = CATALOG_SCOPE_CURRENT
        #: Optional ``hook(old_source, new_source)`` invoked after the session
        #: really moved to another file (Save As).
        self._source_rebind_hook = None
        #: Parsed page bodies: page id -> (stored data text, parsed body).
        #: The body of a page is parsed once and kept, so a save never
        #: re-parses the body of every page of the project.
        self._page_bodies = {}
        #: The body values a page held the last time it was compared.
        self._page_live = {}
        #: The modification version the file holds, per page id.
        self._page_bases = {}
        #: The versions a snapshot covered, per snapshot revision.
        self._captured_versions = {}
        self._version_counter = 0

    # ── 打开 ────────────────────────────────────────────────────

    @classmethod
    def open(cls, source, *, item_ids=(), page_ids=(), pages_complete=False,
             include_boards=True, attachments=False, include_domains=False, cancel=None):
        """Read the diff base for the records this page loads.

        Only the ids named here become writable. Everything else stays in the
        file, unopened and unverified -- which is why it cannot be overwritten.
        """
        _check_cancel(cancel)
        note_key = mindmap_key = None
        if include_boards:
            note_key, mindmap_key = _board_keys(source)
        references = [(ITEM_TABLE, key) for key in item_ids]
        references += [(PAGE_TABLE, key) for key in page_ids]
        if note_key is not None:
            references.append((NOTE_TABLE, note_key))
        if mindmap_key is not None:
            references.append((MINDMAP_TABLE, mindmap_key))
        baseline = read_baseline(source, references, cancel=cancel)
        session = cls(baseline, loaded_pages=page_ids, pages_complete=pages_complete,
                      note_key=note_key, mindmap_key=mindmap_key,
                      attachments=attachments or include_domains)
        # Only canvas metadata is needed to validate destinations; no target
        # assets or document contents are loaded to move a source item.
        _check_cancel(cancel)
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            canvas_ids = [row[0] for row in connection.execute(
                "SELECT id FROM workspace_pages WHERE kind='canvas' OR id='default-canvas'")]
        canvas_base = read_baseline(source, [(PAGE_TABLE, key) for key in canvas_ids], cancel=cancel)
        if (canvas_base.source_identity != baseline.source_identity or
                canvas_base.project_id != baseline.project_id or
                canvas_base.schema_version != baseline.schema_version):
            raise PartialSaveConflict('Project changed while canvas destinations were read')
        session._canvas_rows = {key: row for _table, key, row in canvas_base.records}
        # These four small rows are whole-project data even when only one
        # canvas is resident. Capture their read base at open, not at save.
        with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                  "AND name='prism_metadata'").fetchone():
                for key in GLOBAL_METADATA_KEYS:
                    row = connection.execute(
                        'SELECT value FROM prism_metadata WHERE key=?', (key,)).fetchone()
                    if row is not None:
                        session._rows[('prism_metadata', key)] = tuple(row)
                session._metadata_scope = True
        if include_domains:
            from prism.partial_domains import DomainTracker, read_domain_baseline
            _check_cancel(cancel)
            domain_base = read_domain_baseline(source, cancel=cancel)
            _check_cancel(cancel)
            if (domain_base.source_identity != baseline.source_identity or
                    domain_base.project_id != baseline.project_id or
                    domain_base.schema_version != baseline.schema_version):
                raise PartialSaveConflict('Project changed while domains were read')
            session._domains = DomainTracker(domain_base)
            session._rows.update({(table, key): row
                                  for table, key, row in domain_base.records})
            with closing(sqlite3.connect(Path(source).resolve().as_uri() + '?mode=ro', uri=True)) as connection:
                state = connection.execute(
                    "SELECT value FROM prism_metadata WHERE key='material_library_state'").fetchone()
                has_attachments = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attachments'").fetchone()
                if has_attachments:
                    session._valid_domain_attachments = {row[0] for row in connection.execute(
                        'SELECT id FROM attachments WHERE board_id=0 AND length(blob)>0')}
                    from prism.storage.managed import root_for
                    if root_for(source) is not None:
                        session._valid_domain_attachments.update(int(row[0]) for row in connection.execute(
                            "SELECT entity FROM object_refs WHERE owner='attachments' AND size>0"))
            session._library_state_before = state[0] if state else None
            if state:
                session._rows[('prism_metadata', 'material_library_state')] = tuple(state)
        _check_cancel(cancel)
        return session

    def attach(self, scene):
        """Mark the scene as partially loaded; the file writer requires this."""
        self._residency.bind_live(_assets(scene), set(self.loaded()))
        scene._partial_project_source = self.source
        scene._partial_project_session = self

    # ── 状态 ────────────────────────────────────────────────────

    def _require_valid(self):
        if self.invalid:
            raise PartialSaveConflict(self.invalid)

    def loaded(self, table=ITEM_TABLE):
        return tuple(key for name, key in self._rows if name == table)

    def register_loaded(self, baseline, items, scene):
        """Append a newly installed page without replacing earlier dirty bases.

        The caller reads its baseline before installation. Existing parked
        assets must be reused so undo commands cannot refer to a second copy.
        """
        self._require_valid()
        self._residency.require_stable()
        if not self._residency.initialized:
            raise PartialSaveConflict('Attach the initial page before registering more pages')
        if (not isinstance(baseline, PartialReadSnapshot) or baseline.source != self.source or
                baseline.source_identity != self.source_identity or
                baseline.project_id != self.project_id or baseline.schema_version != self.schema_version):
            raise PartialSaveConflict('Loaded page belongs to another project or source revision')
        additions, seen = {}, set()
        for table, key, row in baseline.records:
            if table not in (ITEM_TABLE, PAGE_TABLE) or (table, key) in seen:
                raise PartialSaveConflict('Register a unique item/page scope')
            seen.add((table, key))
            previous = self._rows.get((table, key))
            if previous is not None and previous != row:
                raise PartialSaveConflict('Registering a page cannot replace an existing edit baseline')
            additions[(table, key)] = row
        from prism.item_lifecycle import logical_item
        items = tuple(logical_item(item) for item in items)
        ids = [_save_id(item) for item in items]
        expected_ids = {key for table, key in seen if table == ITEM_TABLE}
        if len(ids) != len(set(ids)) or set(ids) != expected_ids:
            raise PartialSaveConflict('Installed assets do not match the loaded page baseline')
        self._residency.validate_restore(items, _assets(scene))
        self._residency.bind_live(_assets(scene), set(self.loaded()))
        # All validation precedes mutation: a failed load leaves prior dirty
        # records, residency and page registrations unchanged.
        self._rows.update(additions)
        self.loaded_pages = self.loaded_pages | {key for table, key in seen if table == PAGE_TABLE}
        for (table, key), row in additions.items():
            if table == PAGE_TABLE and row[0] == 'canvas':
                self._canvas_rows[key] = row
        self._residency.restored(items)

    def prepare_unload(self, items, scene):
        """Validate and pin a page before its caller removes the scene handles."""
        self._require_valid()
        return self._residency.prepare(items, _assets(scene), set(self.loaded()),
                                       lambda item: self._move_guards(item, scene))

    def commit_unload(self, token, scene):
        self._require_valid()
        self._residency.commit(token, _assets(scene))

    def cancel_unload(self, token, scene):
        self._require_valid()
        self._residency.cancel(token, _assets(scene))

    def retained_item(self, save_id):
        """Canonical unloaded item, including edits and undo ownership."""
        self._require_valid()
        return self._residency.parked.get(save_id)

    def mark_page_modified(self, page):
        """Record that a page changed, so the next save can skip its body.

        The editor calls this whenever it changes what a page stores: an
        edit, a clear, an import, an undo or a redo.  The version is
        compared with the version the file holds, so a page that did not
        change since the last acknowledged save is skipped without its
        body being read, parsed or written at all.

        The version lives on the page itself, so an undo that restores the
        page also restores the version the page had then: a page that was
        edited and undone is not saved again.

        A page that is never marked carries no reliable dirty record.  The
        save then compares the page with its baseline instead, which is
        slower but never loses an edit.

        The editor must call this for **every** change of a page, including
        a change that edits the stored body in place: a version that was
        not bumped does not prove that the body is unchanged.
        """
        if not isinstance(page, dict) or not page.get('id'):
            return
        self._version_counter += 1
        page[PAGE_VERSION_KEY] = self._version_counter

    def authorize_mindmap_clear(self):
        """An explicit editor command may clear the loaded singleton board."""
        self._require_valid()
        if self.mindmap_key is None or (MINDMAP_TABLE, self.mindmap_key) not in self._rows:
            raise PartialSaveConflict('Load the mind map before explicitly clearing it')
        self._mindmap_clear_authorized = True

    def mindmap_clear_needed(self):
        """True when clearing the mind map would be written as a real change.

        An unloaded board row has no patch to refuse, so only a loaded one needs
        the explicit authorization above.
        """
        return (self.mindmap_key is not None and
                (MINDMAP_TABLE, self.mindmap_key) in self._rows)

    def authorize_resource_purge(self, node_ids, page_ids, scene):
        """Admit one permanent delete from the recycle bin, before it happens.

        The tree purge removes rows in place, and a covered row that vanished is
        a conflict, never a delete -- so the session has to learn about the ids
        first. Refuses, without touching anything, the purges a partial save
        cannot write: a material folder that still holds materials, or pages
        whose loaded assets would have to go with them.
        """
        self._require_valid()
        node_ids = tuple(dict.fromkeys(node_ids))
        if not node_ids:
            return
        if self._domains is None:
            raise PartialSaveConflict(
                'The resource tree is outside this save scope; reopen the whole '
                'project before permanently deleting anything in it')
        present = {node.get('id'): node
                   for node in getattr(scene, 'workspace_nodes', ()) or ()}
        for key in node_ids:
            if ('workspace_nodes', key) not in self._rows:
                raise PartialSaveConflict(
                    f'Resource tree node {key} was not loaded, so this save cannot '
                    'verify deleting it. Reopen the project first.')
            node = present.get(key)
            if node is None:
                raise PartialSaveConflict(
                    f'Resource tree node {key} is no longer in the project')
            # Admitting an id the tree will then refuse would leave a deletion
            # authorized against a row that is still there, and every later save
            # would stop. Check what the tree itself requires, before admitting.
            if not node.get('deletedAt'):
                raise PartialSaveConflict(
                    'Permanently deleting outside the recycle bin needs the whole '
                    'project open')
            if node.get('pageId') == 'default-canvas':
                raise PartialSaveConflict('The default canvas cannot be permanently deleted')
        self._refuse_unwritable_purge(node_ids, page_ids, scene)
        self._domain_deletions.update(('workspace_nodes', key) for key in node_ids)

    def _refuse_unwritable_purge(self, node_ids, page_ids, scene):
        """Refuse a purge whose result no partial save can express."""
        purged = set(node_ids)
        for record in getattr(scene, 'material_records', ()) or ():
            if record.get('folderId') in purged:
                raise PartialSaveConflict(
                    'Materials still live in the folders being permanently deleted. '
                    'Move them out first: a partial save cannot write a material '
                    'without its folder, so this purge would block every later save.')
        if not page_ids:
            return
        from prism.item_lifecycle import logical_item
        gone = set(page_ids)
        stranded = [item for item
                    in list(_assets(scene)) + list(self._residency.parked.values())
                    if self._canvas_of(logical_item(item)) in gone]
        if stranded:
            raise PartialSaveConflict(
                f'{len(stranded)} asset(s) belong to the pages being permanently deleted. '
                'A partial save never deletes an item row -- the original bytes are '
                'shared per project -- so trash or move those assets first, or '
                'reopen the whole project before purging.')

    def _canvas_of(self, item):
        """Live canvas of one logical asset, falling back to its diff base."""
        canvas = getattr(item, '_canvas_id', None)
        if isinstance(canvas, str) and canvas:
            return canvas
        base = self._rows.get((ITEM_TABLE, _save_id(item)))
        if base is None:
            return 'default-canvas'
        return _json_object(base[-1]).get('canvasId', 'default-canvas')

    def authorize_canvas_move(self, items, page_id, scene):
        """Admit one explicit UI command, including its undo/redo destinations.

        Raw changes to canvasId are never interpreted as a move command. A
        destination may be unloaded; only its small page record is required.
        """
        self._require_valid()
        self._canvas_destination(page_id, scene)
        from prism.item_lifecycle import logical_item
        items = [logical_item(item) for item in items]
        entries = []
        for item in items:
            key = _save_id(item)
            if key is not None and (ITEM_TABLE, key) not in self._rows:
                raise PartialSaveConflict(f'Cannot move an unloaded item: {key}')
            current = _item_canvas_id(item)
            entries.append((item, current))
        for item, current in entries:
            entry = self._canvas_moves.setdefault(id(item), (item, set()))
            entry[1].update((current, page_id))

    def _canvas_destination(self, page_id, scene):
        if not isinstance(page_id, str) or not page_id:
            raise PartialSaveConflict('Choose an existing canvas as the move destination')
        row = self._canvas_rows.get(page_id)
        if row is not None and (row[0] != 'canvas' or _json_object(row[2]).get('deletedAt')):
            raise PartialSaveConflict('Move destination is not an active canvas')
        nodes = {node.get('id'): node for node in getattr(scene, 'workspace_nodes', ()) or ()}
        target_nodes = [node for node in nodes.values() if node.get('pageId') == page_id]
        for node in target_nodes:
            seen = set()
            while node:
                if node.get('id') in seen or node.get('deletedAt'):
                    raise PartialSaveConflict('Move destination is in the recycle bin or invalid')
                seen.add(node.get('id'))
                node = nodes.get(node.get('parentId'))
        current = {page.get('id'): page for page in getattr(scene, 'workspace_pages', ())}
        if page_id in current:
            page = current[page_id]
            if page.get('kind') != 'canvas' or page.get('deletedAt'):
                raise PartialSaveConflict('Move destination is not an active canvas')
            return
        row = self._canvas_rows.get(page_id)
        if row is not None and row[0] != 'canvas':
            raise PartialSaveConflict('Move destination is not a canvas')
        if row is not None and _json_object(row[2]).get('deletedAt'):
            raise PartialSaveConflict('Move destination is in the recycle bin')
        if page_id == 'default-canvas':
            return  # Legacy projects have an implicit default canvas.
        # ``_canvas_rows`` holds **every** canvas row of the file (see
        # ``open``), so a missing row proves the page is not a canvas of this
        # project at all.  ``pages_complete`` says how many pages this session
        # loaded, not how many canvases the project has: it must never reject
        # a canvas the file really contains -- that is the whole point of
        # moving an asset to a page this scope did not install.
        if row is None:
            raise PartialSaveConflict(f'Move destination does not exist: {page_id}')

    def _move_guards(self, item, scene):
        key = _save_id(item)
        base = self._rows.get((ITEM_TABLE, key))
        if base is None:
            return ()
        before = _json_object(base[-1]).get('canvasId', 'default-canvas')
        after = _item_canvas_id(item)
        if before == after:
            return ()
        admitted = self._canvas_moves.get(id(item))
        if admitted is None or admitted[0] is not item or after not in admitted[1]:
            raise PartialSaveConflict(
                'Canvas ownership changed without an explicit move command; save stopped')
        self._canvas_destination(after, scene)
        target = self._canvas_rows.get(after)
        if target is not None:
            return ((PAGE_TABLE, after, target),)
        if after == 'default-canvas':
            return ((PAGE_TABLE, after, None),)
        # A newly created canvas is inserted in the same snapshot; its row
        # conflict is checked by capture_partial_save.
        return ()

    # ── 捕获 ────────────────────────────────────────────────────

    def validate_capture(self, scene):
        """GUI preflight without JSON, document decoding or database reads."""
        self._require_valid()
        self._residency.require_stable()
        queue = getattr(scene, 'items_to_add', None)
        if queue is not None and not queue.empty():
            raise PartialSaveConflict(
                'This page is still installing items. Save again once it has settled.')
        uncovered = self._uncovered(scene)
        if uncovered:
            raise PartialSaveConflict(
                'This project is partly open and this save cannot verify: '
                + ', '.join(uncovered) + '. Reopen the whole project before saving.')
        live = list(_assets(scene))
        self._residency.bind_live(live, set(self.loaded()))
        seen = {_save_id(item) for item in self._residency.assets_for_save(live)}
        for key in self.loaded():
            if key not in seen:
                raise PartialSaveConflict(
                    f'Loaded item {key} is no longer on this page. Removing an asset needs '
                    'an explicit trash command, and this save refuses to infer one.')
        return live

    def capture(self, scene, *, cancel=None):
        """Snapshot only what changed since the last acknowledged save."""
        _check_cancel(cancel)
        self._require_valid()
        self._residency.require_stable()
        queue = getattr(scene, 'items_to_add', None)
        if queue is not None and not queue.empty():
            raise PartialSaveConflict(
                'This page is still installing items. Save again once it has settled.')
        uncovered = self._uncovered(scene)
        if uncovered:
            # These rows were loaded, so the user can edit them, but this session
            # has no baseline for them. Writing only the rows it knows would drop
            # those edits silently -- refusing is the only honest answer until the
            # scope cover them.
            raise PartialSaveConflict(
                'This project is partly open and this save cannot verify: '
                + ', '.join(uncovered) + '. Save with the whole project loaded, or '
                'reopen it before saving so no edit is dropped.')
        patches, inserts, new_items, guards = [], [], [], {}
        read_sets, references, required = [], set(), set()
        seen, tag = set(), 0
        live_items = list(_assets(scene))
        self._residency.bind_live(live_items, set(self.loaded()))
        for item in self._residency.assets_for_save(live_items):
            _check_cancel(cancel)
            save_id = _save_id(item)
            if save_id is None or save_id in seen:
                # Unknown id: either a fresh asset or a copy that kept the
                # original id. Both are inserts, exactly like the full writer.
                expected = item_save_columns(item)
                new_items.append(_new_item(item, expected, tag))
                tag += 1
                continue
            seen.add(save_id)
            if (ITEM_TABLE, save_id) not in self._rows:
                raise PartialSaveConflict(
                    f'Item {save_id} was not loaded with this page, so this save cannot '
                    f'verify it. Reopen the project before saving.')
            if _is_error_item(item):
                continue  # damaged rows are left exactly as they are
            for table, key, expected in self._move_guards(item, scene):
                guards[(table, key)] = expected
            patch = self._item_patch(save_id, item)
            if patch is not None:
                patches.append(patch)
        for (table, key) in self._rows:
            if table == ITEM_TABLE and key not in seen:
                raise PartialSaveConflict(
                    f'Loaded item {key} is no longer on this page. Removing an asset needs '
                    f'an explicit trash command, and this save refuses to infer one.')
        _check_cancel(cancel)
        self._global_metadata_patches(scene, patches, inserts, live_items)
        tag, versions = self._page_patches(scene, patches, inserts, tag, cancel=cancel)
        tag = self._board_patches(scene, patches, inserts, tag)
        _check_cancel(cancel)
        if self._domains is not None:
            domain_changes = self._domains.capture(
                getattr(scene, 'material_records', ()) or (),
                getattr(scene, 'workspace_nodes', ()) or (),
                getattr(scene, 'model_records', ()) or (),
                explicit_deletions=self._domain_deletions, cancel=cancel)
            patches.extend(domain_changes.patches)
            inserts.extend(domain_changes.inserts)
            for record in getattr(scene, 'material_records', ()) or ():
                current_refs = _attachment_ids(record, cancel=cancel)
                references.update(current_refs)
                old = self._rows.get(('material_records', record['id']))
                old_refs = _attachment_ids(_json_object(old[0]), cancel=cancel) if old else set()
                required.update(current_refs - old_refs)
            from prism.model_library.project_storage import model_attachment_ids
            for record in getattr(scene, 'model_records', ()) or ():
                current_refs = set(model_attachment_ids(record))
                references.update(current_refs)
                key = record.get('id') or record.get('model_id')
                old = self._rows.get(('model_records', key))
                old_refs = set(model_attachment_ids(_json_object(old[0]))) if old else set()
                required.update(current_refs - old_refs)
            if references and getattr(getattr(scene, 'attachments', None), '_meta', None) is None:
                raise PartialSaveConflict('Material sources need the complete attachment index')
            # Preserve pre-existing damaged sources, but do not let a source
            # known to be valid at open disappear during this editing session.
            required.update(references & self._valid_domain_attachments)
            # Validation of a material/tree uses the whole covered domain.
            # Protect that read set too, including concurrent phantom inserts.
            if domain_changes.patches or domain_changes.inserts or guards:
                for table, key, row in domain_changes.baseline.records:
                    guards[(table, key)] = row
                for table in ('material_records', 'workspace_nodes', 'model_records'):
                    read_sets.append((table, tuple(key for name, key, _row
                        in domain_changes.baseline.records if name == table)))
            self._library_state_patch(scene, patches, inserts)
        snapshot = capture_partial_save(
            self.source, patches, inserts=tuple(inserts), new_items=tuple(new_items),
            attachments=getattr(scene, 'attachments', None) if self.attachments else None,
            baseline=self._baseline(),
            read_guards=tuple((table, key, row) for (table, key), row in guards.items()),
            read_sets=read_sets, attachment_references=references,
            required_attachments=required, cancel=cancel)
        self._remember_versions(snapshot.revision, versions)
        return snapshot

    def acknowledge_model_commit(self, before, after):
        """Adopt exactly one independently committed model row, no other edits.

        This GUI-thread boundary is used by the model service's durable
        commands. It cannot acknowledge a canvas save or refresh a whole
        baseline, either of which would swallow newer editor changes.
        """
        if self._domains is None:
            raise PartialSaveConflict('Model metadata domain was not loaded')
        key = ('model_records', after['id'])
        previous = self._domains._decoded.get(key)
        if previous != before:
            raise PartialSaveConflict('Model command baseline changed')
        with closing(sqlite3.connect(Path(self.source).resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=.25)) as db:
            row = db.execute('SELECT payload FROM model_records WHERE id=?', (key[1],)).fetchone()
        if row is None or json.loads(row[0]) != after:
            raise PartialSaveConflict('Model command was not durably committed')
        self._domains._rows[key] = row
        self._domains._decoded[key] = json.loads(row[0])
        self._rows[key] = row
        from prism.model_library.project_storage import model_attachment_ids
        self._valid_domain_attachments.update(model_attachment_ids(after))

    def _remember_versions(self, revision, versions):
        """Remember the versions a snapshot covered.

        The receipt advances the base to exactly what the snapshot covered,
        so an edit made while the save was running stays newer than the
        base and the page is saved again.
        """
        self._captured_versions[revision] = versions
        while len(self._captured_versions) > 8:
            self._captured_versions.pop(next(iter(self._captured_versions)))

    def _uncovered(self, scene):
        """Boards that were loaded into the scene but are outside this scope.

        The material library and the resource tree are whole-table payloads with
        their own validation and trash rules; this session has no baseline for
        them, so an edit made there could not be written without guessing.
        """
        if self._domains is not None:
            return []
        return [name for name in ('material_records', 'workspace_nodes', 'model_records',
                                  'material_library_state')
                if getattr(scene, name, None)]

    def _global_metadata_patches(self, scene, patches, inserts, live_items):
        """Persist declared global edits without scanning unloaded assets."""
        from prism.tags import TagSystem
        for key, attr in (('tags', 'tag_names'), ('categories', 'category_names'),
                          ('library_queries', 'library_queries'),
                          ('tag_system', 'tag_system')):
            current = getattr(scene, attr, _MISSING)
            if current is _MISSING:
                continue  # This caller never loaded that domain.
            if key == 'tag_system':
                current = (current.to_dict() if isinstance(current, TagSystem)
                           else current)
                if not isinstance(current, dict):
                    raise PartialSaveConflict('Tag mappings must be an object')
                empty = not any(current.get(name) for name in ('siblings', 'parents'))
            else:
                if not isinstance(current, (list, tuple)):
                    raise PartialSaveConflict(f'Global {key} must be a list')
                current = list(current)
                if key in ('tags', 'categories'):
                    if not all(isinstance(name, str) for name in current):
                        raise PartialSaveConflict(f'Global {key} must contain names')
                    known = set(current)
                    field = '_tags' if key == 'tags' else '_categories'
                    for item in live_items:
                        for name in getattr(item, field, ()) or ():
                            if name not in known:
                                current.append(name)
                                known.add(name)
                empty = not current
            if not self._metadata_scope:
                if not empty:
                    raise PartialSaveConflict(
                        f'Global {key} was not loaded into this partial save baseline')
                continue
            before = self._rows.get(('prism_metadata', key))
            if before is None and empty:
                continue
            stored = json.loads(before[0]) if before and before[0] else None
            if key == 'tag_system' and isinstance(stored, dict):
                # Preserve extension keys unknown to TagSystem.from_dict().
                current = dict(stored, **current)
            if current == stored:
                continue
            encoded = json.dumps(current, ensure_ascii=False, allow_nan=False)
            if before is None:
                inserts.append(RowInsert('prism_metadata',
                    (('key', key), ('value', encoded)), tag=('prism_metadata', key)))
            else:
                patches.append(RowPatch('prism_metadata', key,
                                        fields=(('value', encoded),)))

    def _library_state_patch(self, scene, patches, inserts):
        current = getattr(scene, 'material_library_state', {}) or {}
        if not isinstance(current, dict):
            raise PartialSaveConflict('Material library view state must be an object')
        if not current:
            return  # A module that was never shown has no new view state.
        before = self._library_state_before
        stored = _json_object(before) if before else {}
        value = _merge_state(stored, current)
        if value == stored:
            return
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
        key = 'material_library_state'
        if ('prism_metadata', key) not in self._rows:
            inserts.append(RowInsert('prism_metadata', (('key', key), ('value', encoded)),
                                     tag=('prism_metadata', key)))
        else:
            patches.append(RowPatch('prism_metadata', key, fields=(('value', encoded),)))

    def _item_patch(self, save_id, item):
        base = self._rows[(ITEM_TABLE, save_id)]
        after = item_values(item)
        if not isinstance(after[-1], dict):
            raise PartialSaveConflict(f'Item {save_id} metadata is not an object')
        base_data = _json_object(base[-1])
        fields = tuple((name, value) for name, old, value
                       in zip(ITEM_FIELDS[:-1], base, after) if old != value)
        updates = _changed_json(base_data, after[-1])
        removals = tuple((key,) for key in REMOVABLE_ITEM_KEYS
                         if key in base_data and key not in after[-1])
        if not fields and not updates and not removals:
            return None
        return RowPatch(ITEM_TABLE, save_id, fields=fields,
                        json_updates=(('data', updates),) if updates else (),
                        json_removals=(('data', removals),) if removals else ())

    def _page_patches(self, scene, patches, inserts, tag, *, cancel=None):
        """Write only the pages that changed since the last acknowledged save.

        The body of a page is compared with the parsed baseline, so a page
        that did not change costs no JSON at all: the baseline is parsed
        once per session and the new body is only written -- and only
        serialised -- when it really differs.

        Returns the next insert tag and the modification version each page
        carried, so the receipt can advance the base to exactly what this
        snapshot covered.
        """
        present = set()
        versions = {}
        for order, page in enumerate(list(getattr(scene, 'workspace_pages', []) or [])):
            _check_cancel(cancel)
            page_id = page.get('id')
            if page_id is None:
                raise PartialSaveConflict('A workspace page has no id; this save cannot verify it')
            present.add(page_id)
            versions[page_id] = page_version(page)
            base = self._rows.get((PAGE_TABLE, page_id))
            if base is None:
                after = page_row(page, order)
                inserts.append(RowInsert(PAGE_TABLE, (
                    ('id', page_id), ('kind', after[0]), ('title', after[1]),
                    ('data', json.dumps(after[2], ensure_ascii=False)),
                    ('sort_order', after[3])), tag))
                tag += 1
                continue
            if self._page_settled(page_id, page):
                body = None
            else:
                stored = self._stored_body(page_id)
                body = self._page_body(page, page_id, stored)
            kind = page['kind'] if 'kind' in page else base[0]
            title = page['title'] if 'title' in page else base[1]
            # Page order belongs to a whole project, not to this page scope.
            sort_order = order if self.pages_complete else base[3]
            fields = tuple((name, value) for name, old, value in
                           (('kind', base[0], kind), ('title', base[1], title),
                            ('sort_order', base[3], sort_order)) if old != value)
            replacements = (('data', body),) if body is not None else ()
            if not fields and not replacements:
                continue
            patches.append(RowPatch(PAGE_TABLE, page_id, fields=fields,
                                    json_replacements=replacements))
        for (table, key) in self._rows:
            if table != PAGE_TABLE or key in present:
                continue
            if not self.pages_complete:
                raise PartialSaveConflict(
                    f'Loaded page {key} disappeared while saving a partial page scope')
            # Pages are a small, fully loaded list, so an absent page is a real
            # delete by an explicit command -- never an inference from a filter.
            patches.append(RowPatch(PAGE_TABLE, key, delete=True))
        return tag, versions

    def _stored_body(self, page_id):
        """The parsed ``data`` column of one page row.

        The body is parsed once and kept for the whole session, so a save
        never re-parses the body of every page of the project to find out
        that none of them changed.  A row the receipt rewrote is parsed
        again, because its stored body is a new one.
        """
        row = self._rows.get((PAGE_TABLE, page_id))
        if row is None:
            return {}
        cached = self._page_bodies.get(page_id)
        if cached is None or cached[0] != row[2]:
            cached = (row[2], _json_object(row[2]))
            self._page_bodies[page_id] = cached
        return cached[1]

    def _page_body(self, page, page_id, stored):
        """The body one page should store, or ``None`` when it is unchanged.

        The stored body is the diff base.  A key the page does not carry is
        a body that was never loaded here, so the stored value is kept: an
        unloaded body is never dropped, and an unknown key of the stored
        payload is never lost either.
        """
        live = {}
        for name in PAGE_BODY_KEYS:
            if name in page:
                value = page[name]
                live[name] = bool(value) if name == 'permanent' else value
        cached = self._page_live.get(page_id, {})
        settled = self._page_settled(page_id, page)
        values = {}
        for name in PAGE_BODY_KEYS:
            if name not in live:
                if name in stored:
                    values[name] = stored[name]
                continue
            value = live[name]
            untouched = cached.get(name, _MISSING) is value
            if untouched and (_immutable(value) or settled):
                # Provably the value the file already holds: an immutable
                # value cannot have changed under the same object, and a
                # settled version says the editor did not change the page.
                values[name] = stored[name] if name in stored else value
                continue
            if name in stored and value == stored[name]:
                values[name] = stored[name]
                continue
            values[name] = value
        self._page_live[page_id] = live
        merged = dict(stored)
        merged[PAGE_PAYLOAD_MARKER] = True
        merged.update(values)
        if merged == stored:
            return None
        return merged

    def _page_settled(self, page_id, page):
        """True when a reliable modification version proves the page is still.

        The editor bumps a page's version every time it changes what the
        page stores, so a version equal to the one the file holds proves
        that nothing changed since the last acknowledged save -- and the
        body of the page does not have to be compared at all.

        A page without a version has no reliable dirty record.  It is then
        compared with its baseline instead, which is slower but never loses
        an edit.
        """
        version = page_version(page)
        if version is None:
            return False
        base = self._page_bases.get(page_id)
        return base is not None and base == version

    def _board_patches(self, scene, patches, inserts, tag):
        now = int(time.time())
        html = getattr(scene, 'note_html', '') or ''
        base = self._rows.get((NOTE_TABLE, self.note_key)) if self.note_key is not None else None
        if base is not None:
            if base[1] != html:
                patches.append(RowPatch(NOTE_TABLE, self.note_key,
                                        fields=(('html', html), ('updated_at', now))))
        elif html:
            inserts.append(RowInsert(NOTE_TABLE, (
                ('board_id', 0), ('html', html), ('updated_at', now)), tag))
            tag += 1
        tree = getattr(scene, 'mindmap_tree', None)
        base = self._rows.get((MINDMAP_TABLE, self.mindmap_key)) if self.mindmap_key is not None else None
        if base is not None:
            stored = _json_object(base[0]) if base[0] else None
            if tree is None:
                if stored is not None:
                    if not self._mindmap_clear_authorized:
                        raise PartialSaveConflict(
                            'Clearing a saved mind map needs a full save or an explicit clear command')
                    patches.append(RowPatch(MINDMAP_TABLE, self.mindmap_key,
                        json_replacements=(('tree', None),), fields=(('updated_at', now),)))
            elif tree != stored:
                patches.append(RowPatch(MINDMAP_TABLE, self.mindmap_key,
                                        json_replacements=(('tree', tree),),
                                        fields=(('updated_at', now),)))
        elif tree is not None:
            inserts.append(RowInsert(MINDMAP_TABLE, (
                ('board_id', 0), ('tree', json.dumps(tree, ensure_ascii=False)),
                ('updated_at', now)), tag))
            tag += 1
        return tag

    def _baseline(self):
        """The current diff base, including rows already saved this session."""
        return PartialReadSnapshot(
            self.source, self.source_identity, self.project_id, self.schema_version,
            tuple((table, key, row) for (table, key), row in self._rows.items()))

    # ── 回执 ────────────────────────────────────────────────────

    def acknowledge(self, snapshot, receipt, *, scene=None, filename=None):
        """Advance the base to exactly what the receipt covered.

        Edits made while the save was running are newer than the snapshot, so
        they are still unsaved; only the acknowledged rows move forward.
        """
        if not isinstance(snapshot, PartialSaveSnapshot):
            raise TypeError('Acknowledging needs the captured snapshot')
        if receipt is None or receipt.revision != snapshot.revision:
            raise ValueError('Receipt does not belong to this snapshot')
        new_keys = dict(receipt.inserted)
        if self._domains is not None:
            self._domains.acknowledge(snapshot)
        # The base advances to the versions this snapshot covered, not to the
        # versions the pages carry now: an edit made while the save was
        # running is newer than the base and stays unsaved.
        for page_id, version in self._captured_versions.pop(snapshot.revision, {}).items():
            if version is not None:
                self._page_bases[page_id] = version
        for change in snapshot.changes:
            key = change.key if change.key is not None else new_keys.get(change.tag)
            if key is None:
                continue
            if change.after is None:
                self._rows.pop((change.table, key), None)
            else:
                self._rows[(change.table, key)] = change.after
            if change.table == NOTE_TABLE and change.before is None:
                self.note_key = key
            if change.table in ('material_records', 'workspace_nodes', 'model_records') \
                    and change.after is None:
                self._domain_deletions.discard((change.table, key))
            if change.table == PAGE_TABLE:
                if change.after is not None and (change.after[0] == 'canvas' or key == 'default-canvas'):
                    self._canvas_rows[key] = change.after
                else:
                    self._canvas_rows.pop(key, None)
            if change.table == 'prism_metadata' and key == 'material_library_state':
                self._library_state_before = change.after[0] if change.after else None
            if change.table == MINDMAP_TABLE:
                if change.before is None:
                    self.mindmap_key = key
                if change.after is not None and change.after[0] is None:
                    self._mindmap_clear_authorized = False
        for new in snapshot.new_items:
            key = new_keys.get(new.tag)
            if key is None:
                continue
            self._rows[(ITEM_TABLE, key)] = new.expected
            new.item.save_id = key
            archive = getattr(scene, '_prism_archive_items', None) if scene is not None else None
            if archive is not None:
                archive.add(new.item)
        origin = snapshot.attachment_origin
        if origin is not None and hasattr(origin, 'saved_to'):
            origin.saved_to(filename or receipt.filename, snapshot.attachment_versions)
        self._valid_domain_attachments.update(key for key, _name, _mime, blob
                                              in snapshot.attachment_writes if blob)
        self._valid_domain_attachments.difference_update(snapshot.attachment_removals)
        self.revision += 1
        self.acknowledged = receipt
        if filename is not None:
            self._rebind(filename, scene)

    def _rebind(self, filename, scene=None):
        """Point the session at the file the save actually wrote (Save As)."""
        target = Path(filename).resolve()
        if target == Path(self.source).resolve():
            return
        try:
            baseline = read_baseline(target, list(self._rows))
        except (OSError, sqlite3.DatabaseError, KeyError, PartialSaveConflict) as error:
            self.invalid = (f'The project was saved to {target}, but its new rows could not be '
                            f'read back ({error}). Reopen it before saving again.')
            return
        old_source = self.source
        items = list(_assets(scene)) if scene is not None else []
        # Includes pending unloads whose scene handles have already been
        # removed but whose two-phase token has not been committed yet.
        items.extend(self._residency.residents.values())
        rebind_asset_sources(items, old_source, baseline.source)
        self.source = baseline.source
        self.source_identity = baseline.source_identity
        self.project_id = baseline.project_id
        self.schema_version = baseline.schema_version
        self._rows = {(table, key): row for table, key, row in baseline.records}
        # The new file has its own stored bodies and its own versions.
        self._page_bodies.clear()
        self._page_live.clear()
        self._page_bases.clear()
        self._captured_versions.clear()
        if self._domains is not None:
            from prism.partial_domains import DomainTracker
            self._domains = DomainTracker(baseline)
        if scene is not None:
            scene._partial_project_source = self.source
        # The whole-project catalogue still describes the file we came from.
        # Save As leaves the old file untouched, so its fingerprint is still
        # valid and the sidebar counts / the project search would keep
        # reading the previous project.  Say so, and let whoever owns the
        # catalogue drop the stale scope and rebuild it for the new file.
        self._catalog_scope_state = CATALOG_SCOPE_STALE
        hook = self._source_rebind_hook
        if hook is not None:
            try:
                hook(old_source, self.source)
            except Exception:
                # The file is already written: a failing UI hook must not
                # invalidate a save that reached the disk.
                logger.exception('Catalogue rebind hook failed after saving to %s',
                                 self.source)

    # ── 目录 scope：另存为之后必须重建 ──────────────────────────
    def install_source_rebind_hook(self, hook):
        """Register ``hook(old_source, new_source)``, called after a Save As.

        The hook is how the session says: *my source changed, the
        whole-project catalogue no longer describes this session*.  It runs
        only when the session really moved to another file -- saving to the
        same file returns from :meth:`_rebind` before the hook is reached.

        The hook runs on the thread that acknowledged the save (the GUI
        thread).  A failing hook is logged, never propagated: the save
        already reached the disk.
        """
        self._source_rebind_hook = hook

    def requires_catalog_rebuild(self):
        """True when the whole-project catalogue no longer describes this session.

        A Save As leaves the **old** file untouched, so its catalogue
        fingerprint is still valid: the sidebar counts and the project
        search would keep reading the previous project.  The session knows
        its own source changed; the UI has to drop the stale scope and
        rebuild it for the new file.
        """
        return self._catalog_scope_state == CATALOG_SCOPE_STALE

    def catalog_rebuild_notice(self):
        """What the UI must show while the new catalogue is being built.

        Returns a dict:

        ``status``
            ``'stale'`` while the catalogue describes the previous file,
            ``'current'`` once it describes this session's source again.
        ``message``
            The status text for the status bar while rebuilding.
        ``counts``
            ``'stale'`` -- the last snapshot belongs to the previous file and
            must be marked as such, never presented as the new project's
            numbers.
        ``search``
            ``'fallback'`` -- the project search reads the in-memory scene
            until the new catalogue is ready.
        ``source``
            The file this session writes to.
        """
        stale = self.requires_catalog_rebuild()
        return {
            'status': self._catalog_scope_state,
            'message': CATALOG_REBUILDING_MESSAGE if stale else '',
            'counts': 'stale' if stale else 'current',
            'search': 'fallback' if stale else 'catalogue',
            'source': str(self.source),
        }

    def mark_catalog_rebuilt(self):
        """The UI rebuilt the catalogue for the current source."""
        self._catalog_scope_state = CATALOG_SCOPE_CURRENT


def _new_item(item, expected, tag):
    from prism.fileio.partial_save import NewItem
    return NewItem(item, expected, tag)
