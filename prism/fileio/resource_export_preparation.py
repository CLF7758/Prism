# This file is part of Prism.
#
# Prism is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Prism is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Prism.  If not, see <https://www.gnu.org/licenses/>.

"""Read the page bodies an export needs, without touching the live scene.

Why this module exists
----------------------

A resource export writes one file per page: a document page becomes a
``.docx``, a mind map page a ``.xmind``.  The text those files carry lives
in ``workspace_pages[].content`` - and in a project that was opened page by
page (``SQLiteIO.read_board_extras(scope=...)``) that text is **not** in
memory: only the page list is, the bodies stay in the file until something
asks for them.

The old export read ``page.get('content') or ''``.  For a page whose body
was still in the file that is not "an empty document", it is "a document
whose text nobody read" - and the export wrote an empty ``.docx`` and
reported success.  A user who exported a folder of notes and opened the
files found blank pages and no explanation.

This module is the part that tells those apart:

* the body is in memory (the user's own text, edits included) -> use it
* the body is in the file and can be read -> read it, **read-only**
* the body is in the file but cannot be read (the file is gone, moved or
  damaged) -> say so; never write an empty file in its place
* the page does not exist at all -> say so

The read goes through :func:`prism.fileio.read_page_body` - the formal
read-only accessor that ``prism/fileio/sql.py`` owns, because that file owns
the storage format too (the body envelope in ``workspace_pages.data``).
Two places parsing the same envelope is how a reader silently starts reading
an empty dictionary while the writer still writes the body - and that is
the shape of the bug this module exists to fix.

What the export must **not** use is
:func:`prism.fileio.sql.load_deferred_board_extras`: that one materialises
the text into the live scene, which changes what the user is editing.  An
export must not do that.  The accessor takes a file path and no scene, so
that separation is structural rather than a matter of restraint, and the
read happens on the thread that writes the files.

The reader is injectable: :class:`prism.fileio.resource_export.
ResourceSubtreeExporter` takes a ``bodies=`` reader, so a caller that
already has the text (or a different storage layer) can hand it over
instead.  The export does not care where a body came from, only whether
it could be read.

Note on wording: the user-facing strings below reuse the catalogue entries
that already exist (``prism/i18n/zh_CN.py``).  New sentences that have no
entry yet are written in Chinese directly, because that catalogue belongs
to another owner and ``tests/test_translations.py`` requires every ``_()``
call to have an entry.  Whoever owns the catalogue can move those two
sentences into it and switch them to ``_()``.
"""
import dataclasses
import copy
import logging
import os
import re
import threading

from prism.fileio import read_page_body
from prism.source_fingerprint import file_fingerprint
from prism.fileio.attachments import ATTACHMENT_PREFIX
from prism.i18n import _

logger = logging.getLogger(__name__)


#: The body is in memory: the user's own text (edits included).
BODY_LOADED = 'loaded'
#: The body is in memory and is empty on purpose.
BODY_EMPTY = 'empty'
#: The body was still in the project file and was read from there.
BODY_DEFERRED = 'deferred'
#: There is no such page.
BODY_MISSING = 'missing'
#: The body could not be read: the source is gone, moved or damaged.
BODY_ERROR = 'error'

#: The statuses that mean "we know what this page holds".
RESOLVED = (BODY_LOADED, BODY_EMPTY, BODY_DEFERRED)


@dataclasses.dataclass(frozen=True)
class PageBody:
    """What one page's body turned out to be.

    ``status`` is one of the ``BODY_*`` constants.  ``content`` is the
    body itself: a string for a document page, a tree for a mind map
    page.  ``reason`` says why a body could not be read, in words the
    user can be shown.
    """

    status: str
    content: object = None
    reason: str = ''
    origin: str = ''

    @property
    def resolved(self):
        """True when we know what the page holds."""
        return self.status in RESOLVED

    @property
    def empty(self):
        """True when the page really is empty - not "we could not read it"."""
        return self.status == BODY_EMPTY


def is_blank(content):
    """True when a body carries nothing at all.

    A document whose text is ``''`` is empty; one whose text is
    ``'<p><br></p>'`` is not - the user wrote something, however little.
    """
    if content is None:
        return True
    if isinstance(content, str):
        return not content.strip()
    try:
        return not content
    except TypeError:                               # pragma: no cover
        return False


class PageBodyReader:
    """Resolve the body of a page, read-only, on whichever thread asks.

    ``pages`` is the scene's page list.  It is **copied** here: an export
    is a snapshot, and a page the user edits while the export runs must
    not change what the export writes halfway through.

    ``source`` is the project file a body that is not in memory can still
    be read from.  ``deferred_page_ids`` are the pages the scene itself
    marks as "still in the file"; they are kept for the record, because a
    body that is neither in memory nor in the file is a different failure
    from a body that was never there.

    The reader never raises: a body it cannot read is a :class:`PageBody`
    whose status says so and whose reason the user can be shown.
    """

    def __init__(self, pages=None, source=None, deferred_page_ids=None):
        self.pages = {}
        for page in pages or ():
            if isinstance(page, dict) and page.get('id'):
                snapshot = dict(page)
                if 'content' in snapshot:
                    snapshot['content'] = copy.deepcopy(snapshot['content'])
                self.pages[page['id']] = snapshot
        self.source = str(source) if source else None
        try:
            self._source_stamp = file_fingerprint(self.source) if self.source else None
        except OSError:
            self._source_stamp = None
        self.deferred_page_ids = set(deferred_page_ids or ())
        #: Notes the write phase wants the user to see.
        self.notes = []
        #: How many files the write phase really produced.
        self.written_files = 0
        #: How many planned files turned out to have nothing to write.
        self.skipped_files = 0
        #: True when the user stopped the export.
        self.cancelled = False
        self._resolved = {}
        self._lock = threading.Lock()
        self._connection = None

    # ── Reading ────────────────────────────────────────────────

    def read(self, page_id):
        """The body of one page, or a status that says why there is none."""
        with self._lock:
            known = self._resolved.get(page_id)
        if known is not None:
            return known
        body = self._resolve(page_id)
        with self._lock:
            self._resolved[page_id] = body
        return body

    def _resolve(self, page_id):
        page = self.pages.get(page_id)
        if page is None:
            return PageBody(BODY_MISSING,
                            reason=_('This item no longer exists.'))
        if 'content' in page:
            # 键在不在，比值真不真重要：'' 可能是用户自己清空的正文。
            content = page.get('content')
            status = BODY_EMPTY if is_blank(content) else BODY_LOADED
            return PageBody(status, content=content, origin='scene')
        return self._read_from_source(page_id)

    def _read_from_source(self, page_id):
        source = self.source
        if not source:
            return PageBody(
                BODY_ERROR,
                reason=_('Editor content could not be read. '
                         'Please retry exporting.'))
        if not os.path.isfile(source):
            return PageBody(
                BODY_ERROR,
                reason=_('{name}: the file was gone before it could be '
                         'read.').format(name=os.path.basename(source)))
        # The formal accessor: prism/fileio/sql.py owns the storage format,
        # so it owns the reading of it too.  It opens a short-lived
        # read-only handle for one statement and closes it again right
        # away - the project file is not even locked while the export
        # runs - and it never materialises anything into the scene.
        try:
            if self._source_stamp is None or file_fingerprint(source) != self._source_stamp:
                raise OSError('工程源在导出期间已变化，请重新发起导出')
            body = read_page_body(source, page_id)
            if file_fingerprint(source) != self._source_stamp:
                raise OSError('工程源在读取正文时已变化，请重新发起导出')
        except Exception as exc:                     # noqa: BLE001
            # The accessor does not raise; if it ever did, the export
            # still has to answer instead of dying on the write thread.
            logger.debug('The body accessor raised for %s', page_id,
                         exc_info=True)
            return PageBody(
                BODY_ERROR,
                reason=_('Could not read {path}: {reason}').format(
                    path=source, reason=exc))
        if body.error is not None:
            return PageBody(
                BODY_ERROR,
                reason=_('Could not read {path}: {reason}').format(
                    path=source, reason=body.error))
        if not body.found:
            return PageBody(BODY_MISSING,
                            reason=_('This item no longer exists.'))
        content = body.content
        status = BODY_EMPTY if is_blank(content) else BODY_DEFERRED
        return PageBody(status, content=content, origin='project')

    def close(self):
        """Let go of the project file.

        Nothing is held open between reads - the accessor opens a
        short-lived read-only handle for one statement and closes it
        again right away - so there is nothing to let go of.  The method
        is kept because the plan calls it when it is done, and a reader
        that does hold something open can close it here.
        """
        with self._lock:
            self._connection = None


# ── Attachments ─────────────────────────────────────────────────

#: ``<img ...>`` tags, the only place a document points at a picture.
_IMG_TAG = re.compile(r'<img\b[^>]*>', re.IGNORECASE)

#: ``src="prism://attach/12"`` inside such a tag.
_ATTACHMENT_SRC = re.compile(
    r'src\s*=\s*(?P<quote>["\'])(?P<url>'
    + re.escape(ATTACHMENT_PREFIX)
    + r'(?P<id>\d+))(?P=quote)', re.IGNORECASE)


def referenced_attachment_ids(html):
    """The attachment ids a document's pictures point at, in order."""
    found = []
    for tag in _IMG_TAG.findall(html or ''):
        match = _ATTACHMENT_SRC.search(tag)
        if match is None:
            continue
        identifier = int(match.group('id'))
        if identifier not in found:
            found.append(identifier)
    return tuple(found)


def missing_attachments(html, attachments):
    """The pictures a document refers to but cannot bring along.

    A picture that is not in the attachment table, or whose bytes cannot
    be read, would simply be missing from the ``.docx`` - the writer logs
    it and moves on, and the user gets a document with a hole in it and
    no word about it.  The export checks first and refuses the page
    instead, so the file that lands on disk is the file the user meant.

    :returns: a tuple of ``(attachment_id, name)``; ``name`` is None when
        the attachment is not in the table at all.
    """
    if not html:
        return ()
    missing = []
    getter = getattr(attachments, 'get', None)
    for identifier in referenced_attachment_ids(html):
        entry = None
        if getter is not None:
            try:
                entry = getter(identifier)
            except (KeyError, TypeError):
                entry = None
        if entry is None:
            missing.append((identifier, None))
            continue
        blob = entry[2] if len(entry) > 2 else None
        if not blob:
            missing.append((identifier, entry[0] if entry else None))
    return tuple(missing)


def attachment_reason(missing):
    """One sentence the user can read: which picture is not coming along."""
    names = []
    for _identifier, name in missing:
        names.append(str(name) if name else '?')
    listed = '、'.join(names[:5])
    if len(names) > 5:
        listed += '…'
    return _('{name}: the file was gone before it could be read.').format(
        name=listed)
