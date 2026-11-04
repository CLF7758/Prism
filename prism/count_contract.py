"""The immutable count contract of the application.

Prism shows a lot of numbers: the whole library, the current result set,
what is visible in the viewport, what is selected, how many assets carry a
tag, how many are painted on the aggregate canvas.  They used to travel as
loose dictionaries, so every consumer re-derived its own answer and the
numbers drifted apart - the sidebar said one total, the status bar another,
and an unknown count was rendered as ``0``.

This module is the one place that says what a count **is**.

``CountSnapshot``
    One counter.  Frozen.  It carries the revision it was computed against
    and the token of the publication it belongs to.

``CountPublication``
    One notification: every counter that was computed together, sharing one
    request token.

The rules the contract fixes:

* ``value is None`` means **unknown**.  Unknown is never rendered as ``0``;
  the caller shows "Preparing…" or the last confirmed value instead.
* ``complete`` is ``False`` when the answer is partial - for example the
  directory is not ready, so only the loaded page could be counted.
* Every counter carries a **basis**: what it was counted over.  The whole
  library, the current result rows, the resident scene, the painted canvas
  and one tag are five different bases; a number without its basis is a
  number nobody can compare.
* A **pending** kind is a value carried over from the last confirmed
  publication: it is still the best known number, but it does not answer
  the current revision yet.  The UI has to say so.
* ``overlay_revision`` is the session's edit revision, ``source_revision``
  the directory's state, ``open_epoch`` the generation of the open project.
  A publication is stale as soon as any of them moved on.
* A consumer drops a publication that is older than the one it already
  applied, or that belongs to another project / open / query / revision.
  That is what keeps a slow A -> B -> A switch from writing the old answer
  back over the new one.
* The module imports nothing from Qt, so it can be used by background code
  and by tests without a GUI.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional

#: The whole library: active assets in the project (directory + overlay).
LIBRARY_TOTAL = 'library.total'
#: Active assets without a usable tag.
LIBRARY_UNTAGGED = 'library.untagged'
#: Active assets on the default canvas or without a canvas.
LIBRARY_UNCLASSIFIED = 'library.unclassified'
#: Deleted assets plus deleted top level pages.
LIBRARY_TRASH = 'library.trash'
#: Loaded assets that are currently visible in the scene.
LIBRARY_VISIBLE = 'library.visible'
#: Assets the user has selected.
SELECTION_COUNT = 'selection.count'
#: Rows of the current result set.
RESULT_COUNT = 'result.count'
#: Assets actually painted on the canvas.
CANVAS_DRAWN = 'canvas.drawn'
#: Assets the canvas holds (model rows, not necessarily painted).
CANVAS_TOTAL = 'canvas.total'
#: Assets that carry one tag (aliases and implied parents folded in).
TAG_MATCH = 'tag.match'
#: Assets in one colour group of the current canvas.
COLOR_GROUP = 'color.group'
#: Assets on one canvas of the resource tree.
CANVAS_ASSETS = 'canvas.assets'

#: Every kind this contract knows about.
COUNT_KINDS = (
    LIBRARY_TOTAL, LIBRARY_UNTAGGED, LIBRARY_UNCLASSIFIED, LIBRARY_TRASH,
    LIBRARY_VISIBLE, SELECTION_COUNT, RESULT_COUNT, CANVAS_DRAWN,
    CANVAS_TOTAL, TAG_MATCH, COLOR_GROUP, CANVAS_ASSETS,
)

# ── the bases ────────────────────────────────────────────────────────────
#: The whole project: the read-only directory plus this session's edits.
BASIS_CATALOG = 'catalog+overlay'
#: Only what is loaded in this scene.
BASIS_RESIDENT = 'resident'
#: The rows of the current result set.
BASIS_RESULT_ROWS = 'result-rows'
#: What is really painted on the canvas (one painted id per asset).
BASIS_PAINTED = 'painted'
#: A synthetic far scene group: one drawn object can cover many assets.
BASIS_COMPOSITE = 'composite'
#: Model rows - **not** a painted count.
BASIS_MODEL_ROWS = 'model-rows'
#: Tags: what the count was counted over.
BASIS_TAG_ASSET = 'asset'
BASIS_TAG_MATERIAL = 'material'
BASIS_TAG_PAGE = 'page'

_TOKEN_LOCK = threading.Lock()
_TOKEN = 0


def next_request_token() -> int:
    """A process wide, strictly increasing publication token."""
    global _TOKEN
    with _TOKEN_LOCK:
        _TOKEN += 1
        return _TOKEN


@dataclass(frozen=True)
class CountSnapshot:
    """One counter, with the revision it belongs to.

    ``value is None`` means unknown.  ``complete`` is ``False`` when the
    answer is partial.  ``request_token`` names the publication this
    snapshot was part of.  ``basis`` says what the number was counted
    over.  ``pending`` means the value is the last confirmed one and does
    not answer the current revision yet.
    """

    count_kind: str
    value: Optional[int] = None
    project: Optional[str] = None
    query: Optional[str] = None
    source_revision: Optional[str] = None
    overlay_revision: Optional[int] = None
    open_epoch: Optional[str] = None
    complete: bool = True
    request_token: int = 0
    label: Optional[str] = None
    basis: str = ''
    pending: bool = False

    @property
    def known(self) -> bool:
        """Whether this counter has a value at all."""
        return self.value is not None

    def is_stale(self, *, project=None, overlay_revision=None,
                 source_revision=None, open_epoch=None, query=None) -> bool:
        """Whether the state this was computed against has moved on."""
        if project is not None and self.project is not None \
                and project != self.project:
            return True
        if overlay_revision is not None and self.overlay_revision is not None \
                and overlay_revision != self.overlay_revision:
            return True
        if source_revision is not None and self.source_revision is not None \
                and source_revision != self.source_revision:
            return True
        if open_epoch is not None and self.open_epoch is not None \
                and str(open_epoch) != str(self.open_epoch):
            return True
        if query is not None and self.query is not None and query != self.query:
            return True
        return False

    def text(self, unknown: str = 'Preparing…') -> str:
        """The value as it is shown to the user."""
        if self.value is None:
            return unknown
        return f'{self.value:,}'


@dataclass(frozen=True)
class CountPublication:
    """Every counter that was computed together, under one request token.

    The sidebar, the status bar, the colour bar and the toolbar all consume
    this one object, so one computation answers all of them and no consumer
    has to ask the directory a second time.
    """

    request_token: int = 0
    project: Optional[str] = None
    query: Optional[str] = None
    source_revision: Optional[str] = None
    overlay_revision: Optional[int] = None
    open_epoch: Optional[str] = None
    complete: bool = True
    counts: Mapping[str, Optional[int]] = field(default_factory=dict)
    tag_counts: Mapping[str, Optional[int]] = field(default_factory=dict)
    #: What each counter was counted over (see the BASIS_* constants).
    bases: Mapping[str, str] = field(default_factory=dict)
    #: The kinds whose value is carried over from the last confirmation.
    pending: frozenset = frozenset()
    #: Tag counts split by what carries the tag: asset / material / page.
    tag_counts_by_basis: Mapping[str, Mapping[str, Optional[int]]] = \
        field(default_factory=dict)
    #: The generation each externally published counter belongs to (the
    #: query generation of a painted set, for example).  A publication of
    #: an older generation must not overwrite a newer one.
    generations: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, 'counts',
                           MappingProxyType(dict(self.counts or {})))
        object.__setattr__(self, 'tag_counts',
                           MappingProxyType(dict(self.tag_counts or {})))
        object.__setattr__(self, 'bases',
                           MappingProxyType(dict(self.bases or {})))
        object.__setattr__(self, 'pending', frozenset(self.pending or ()))
        object.__setattr__(
            self, 'tag_counts_by_basis',
            MappingProxyType({basis: MappingProxyType(dict(values or {}))
                              for basis, values in
                              (self.tag_counts_by_basis or {}).items()}))
        object.__setattr__(self, 'generations',
                           MappingProxyType(dict(self.generations or {})))

    def value(self, count_kind: str) -> Optional[int]:
        """The value of one counter, or ``None`` when it is unknown."""
        return self.counts.get(count_kind)

    def basis_of(self, count_kind: str) -> str:
        """What one counter was counted over."""
        return self.bases.get(count_kind, '')

    def is_pending(self, count_kind: str) -> bool:
        """Whether the value is the last confirmed one, not the current one."""
        return count_kind in self.pending

    def tag_value(self, name: str, basis: Optional[str] = None) -> Optional[int]:
        """How many things carry one tag, or ``None`` when it is unknown.

        With ``basis`` the answer is restricted to one kind of carrier
        (asset / material / page).
        """
        if basis is None:
            return self.tag_counts.get(name)
        return (self.tag_counts_by_basis.get(basis) or {}).get(name)

    def snapshot(self, count_kind: str, *, label: Optional[str] = None) -> CountSnapshot:
        """One counter of this publication as a :class:`CountSnapshot`."""
        return CountSnapshot(
            count_kind=count_kind,
            value=self.counts.get(count_kind),
            project=self.project,
            query=self.query,
            source_revision=self.source_revision,
            overlay_revision=self.overlay_revision,
            open_epoch=self.open_epoch,
            complete=self.complete,
            request_token=self.request_token,
            label=label,
            basis=self.basis_of(count_kind),
            pending=self.is_pending(count_kind),
        )

    def is_stale(self, *, project=None, overlay_revision=None,
                 source_revision=None, open_epoch=None, query=None) -> bool:
        """Whether the state this was computed against has moved on."""
        return self.snapshot(LIBRARY_TOTAL).is_stale(
            project=project, overlay_revision=overlay_revision,
            source_revision=source_revision, open_epoch=open_epoch,
            query=query)

    def newer_than(self, other: Optional['CountPublication']) -> bool:
        """Whether this publication is newer than ``other``.

        Consumers apply a publication only when it is newer, so a slow
        result can never write an old answer over a new one.
        """
        if other is None:
            return True
        return self.request_token > other.request_token

    def rejection_reason(self, *, project=None, open_epoch=None, query=None,
                          source_revision=None, overlay_revision=None,
                          current: Optional['CountPublication'] = None
                          ) -> Optional[str]:
        """Why this publication must not replace ``current``.

        ``None`` means it may be applied.  The token alone is not enough:
        a newer token can still belong to another project, another open of
        the same project, another query or an older edit revision.
        """
        if current is not None and not self.newer_than(current):
            return 'older_token'
        if project is not None and self.project is not None \
                and str(project) != str(self.project):
            return 'other_project'
        if open_epoch is not None and self.open_epoch is not None \
                and str(open_epoch) != str(self.open_epoch):
            return 'other_open_epoch'
        if query is not None and self.query is not None and query != self.query:
            return 'other_query'
        if source_revision is not None and self.source_revision is not None \
                and source_revision != self.source_revision:
            return 'other_source_revision'
        if overlay_revision is not None and self.overlay_revision is not None \
                and self.overlay_revision < overlay_revision:
            return 'older_overlay_revision'
        if overlay_revision is not None and self.overlay_revision is not None \
                and self.overlay_revision > overlay_revision:
            return 'other_overlay_revision'
        return None


def publication_from_catalog(snapshot: Optional[Mapping], *, request_token: int,
                             project=None, query=None, source_revision=None,
                             overlay_revision=None, open_epoch=None,
                             tag_counts=None, tag_counts_by_basis=None,
                             bases=None, pending=()):
    """Build a :class:`CountPublication` from a catalog count snapshot.

    ``snapshot`` is the dictionary ``ProjectAssetCatalog.count_snapshot()``
    returns.  ``None`` values stay ``None``: a directory that is not ready
    means *unknown*, not zero.
    """
    counts = {}
    complete = True
    if snapshot is None:
        complete = False
    else:
        for kind, key in (
            (LIBRARY_TOTAL, 'total'),
            (LIBRARY_UNTAGGED, 'untagged'),
            (LIBRARY_UNCLASSIFIED, 'unclassified'),
            (LIBRARY_TRASH, 'trash'),
            (LIBRARY_VISIBLE, 'visible'),
        ):
            value = snapshot.get(key)
            counts[kind] = value if isinstance(value, int) else None
            if value is None:
                complete = False
    return CountPublication(
        request_token=request_token,
        project=project,
        query=query,
        source_revision=source_revision,
        overlay_revision=overlay_revision,
        open_epoch=open_epoch,
        complete=complete,
        counts=counts,
        tag_counts=tag_counts or {},
        bases=bases or {},
        pending=frozenset(pending or ()),
        tag_counts_by_basis=tag_counts_by_basis or {},
    )
