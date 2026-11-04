"""One rule for "which canvas does a project open on".

This module is deliberately free of Qt imports: the same rule is used by the
GUI landing page (``prism/window_workspace.py``), by the background scope
preparation that runs off the GUI thread (``prism/paged_open_preparation.py``)
and by the page list that feeds the resource tree
(``prism/widgets/media_library.py``).  When those three answered the question
independently they could disagree -- the background picked "last opened" or
"the page with the most assets" while the interface picked the first page --
and the user then landed on a page that was not the one the scope had loaded.

The rule
--------
1. The canvases that are really stored in the project, in the order the
   project stores them (``workspace_pages`` ordered by ``sort_order, rowid``),
   are the candidates.  A stored canvas is a legal landing page **even when it
   holds no assets**: an empty first canvas is still the first canvas, and
   skipping it would land the user on some other page for no stated reason.
2. A canvas whose resource-tree node is soft-deleted is not legal.
3. The implicit ``default-canvas`` is the landing spot legacy projects used
   before canvases became first-class pages.  It is only a candidate when the
   project still carries content for it, and it never outranks a stored
   canvas.
4. "the project still carries content for it" is answered by the whole-project
   catalog summary when the catalog is ready.  **A catalog that is not ready
   is not an answer**: it means *unknown*, and unknown must never be read as
   *empty*, because that would make a legacy project's assets unreachable.
   The distinction between "the catalog says this canvas is empty" and "we do
   not know yet" is therefore carried explicitly.
"""

from __future__ import annotations

import json
import sqlite3

#: The legacy default canvas id.  Items that carry no ``canvasId`` land here.
DEFAULT_CANVAS_ID = 'default-canvas'

#: The kind of the implicit default page.
CANVAS_KIND = 'canvas'


def stored_canvas_ids(pages):
    """Canvas ids that are really stored in the project, in project order.

    ``pages`` is the page list as the project stores it (``scene.workspace_pages``
    or the rows of the ``workspace_pages`` table).  The order is the caller's
    responsibility: whoever produces the list must use the project's own
    ordering, ``sort_order, rowid``.
    """
    result = []
    for page in pages or ():
        if not isinstance(page, dict):
            continue
        if page.get('kind') != CANVAS_KIND:
            continue
        page_id = page.get('id')
        if page_id and page_id not in result:
            result.append(page_id)
    return result


def deleted_page_ids(nodes):
    """Page ids whose resource-tree node is soft-deleted."""
    deleted = set()
    for node in nodes or ():
        if not isinstance(node, dict):
            continue
        page_id = node.get('pageId')
        if page_id and node.get('deletedAt'):
            deleted.add(page_id)
    return deleted


def implicit_default_has_content(*, catalog_known, catalog_canvases):
    """Does the project still carry content for the implicit default canvas?

    ``catalog_known`` says whether the whole-project summary is authoritative.
    ``catalog_canvases`` maps a canvas id to its asset count, or is ``None``
    when no such summary is available.

    Returns ``True`` when the default canvas must stay reachable -- including
    the "we do not know yet" case, where dropping it could hide legacy assets.
    Returns ``False`` only when the catalog is ready and reports no content at
    all for it.
    """
    if not catalog_known or catalog_canvases is None:
        # Unknown is not empty.  Keep the legacy landing spot reachable.
        return True
    return bool(catalog_canvases.get(DEFAULT_CANVAS_ID, 0))


def first_legal_canvas(stored_ids, deleted, implicit_legal):
    """The first legal canvas, or ``None`` when the project has none.

    ``stored_ids`` is the project-ordered list of stored canvases, ``deleted``
    the soft-deleted page ids and ``implicit_legal`` whether the legacy
    default canvas may be used.
    """
    for page_id in stored_ids or ():
        if page_id not in deleted:
            return page_id
    if implicit_legal and DEFAULT_CANVAS_ID not in deleted:
        return DEFAULT_CANVAS_ID
    return None


def read_stored_canvas_order(filename):
    """Stored canvas ids of a ``.prism`` file, in the project's own order.

    Read-only: the file is opened with ``mode=ro`` so no write transaction is
    possible.  Returns ``[]`` for a file that cannot be read or that has no
    ``workspace_pages`` table -- an unreadable file is not a reason to invent
    a landing page.
    """
    if not filename:
        return []
    uri = 'file:' + str(filename).replace('\\', '/') + '?mode=ro'
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return []
    try:
        try:
            rows = connection.execute(
                'SELECT id, kind FROM workspace_pages ORDER BY sort_order, rowid'
            ).fetchall()
        except sqlite3.Error:
            return []
        pages = [{'id': row[0], 'kind': row[1]} for row in rows]
    finally:
        connection.close()
    return stored_canvas_ids(pages)


def read_deleted_page_ids(filename):
    """Soft-deleted page ids of a ``.prism`` file, read-only."""
    if not filename:
        return set()
    uri = 'file:' + str(filename).replace('\\', '/') + '?mode=ro'
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error:
        return set()
    try:
        try:
            rows = connection.execute('SELECT payload FROM workspace_nodes').fetchall()
        except sqlite3.Error:
            return set()
    finally:
        connection.close()
    nodes = []
    for (payload,) in rows:
        try:
            nodes.append(json.loads(payload) if isinstance(payload, str) else payload)
        except (TypeError, ValueError):
            continue
    return deleted_page_ids(nodes)
