"""Cross-page selection and query identity for the model library.

Qt-free on purpose: the selection is business state, not a widget state, and
it has to be unit-testable without a running application.

Why this module exists
----------------------
The shared card surface keeps ONE selection set and intersects it with the
records it currently displays. That is correct for the material library,
which holds a single client-side list. It is wrong for the model library:
the user selects two models on page 1, walks to page 3 and selects a third,
then exports all three. If the card surface owned the selection, loading page
3 would silently drop the two earlier picks.

So the authoritative selection lives here. The card surface is only a mirror
of the part of the selection that is currently on screen.

The controller also owns the *query identity*: the text, folder, sort order
and revision that produced the current result set. An export names the
identity it was created under, so a later list refresh cannot change what a
running export job covers.
"""
from dataclasses import dataclass, replace
from typing import Iterable, Optional, Tuple


@dataclass(frozen=True)
class QueryIdentity:
    """Immutable identity of one query revision.

    Two result sets are interchangeable only when every field matches. The
    revision counter alone is not enough: the same text can be re-issued
    after the underlying data changed, and a stale page must not be mistaken
    for a fresh one.
    """

    text: str = ''
    folder_id: Optional[str] = None
    sort: str = 'name'
    revision: int = 0

    def with_revision(self, revision: int) -> 'QueryIdentity':
        return replace(self, revision=revision)

    def matches(self, other: Optional['QueryIdentity']) -> bool:
        return other is not None and self == other

    def signature(self) -> Tuple[str, Optional[str], str, int]:
        return (self.text, self.folder_id, self.sort, self.revision)

    def describe(self) -> str:
        parts = [self.text or '全部模型']
        if self.folder_id:
            parts.append('目录 ' + str(self.folder_id))
        parts.append('排序 ' + str(self.sort))
        return ' · '.join(parts)


@dataclass(frozen=True)
class SelectionSnapshot:
    """A frozen export scope.

    The ids and the scope token are captured once, when the user starts the
    operation. A later list refresh, page change or project switch must not
    change what this operation covers.
    """

    model_ids: Tuple[str, ...]
    scope_token: Optional[str] = None
    query: Optional[QueryIdentity] = None

    def __len__(self) -> int:
        return len(self.model_ids)

    def __bool__(self) -> bool:
        return bool(self.model_ids)

    def describe(self) -> str:
        return '%d 个模型 · %s' % (len(self.model_ids),
                                   self.query.describe() if self.query else '当前查询')


class ModelSelection:
    """Selection that survives page changes, refreshes and filter edits.

    Rules that the model library depends on:

    * A selection is a set of *model ids*, not of positions. Changing the page
      or the filter never removes an id from it.
    * A selection belongs to one project session. Binding a different session
      clears it, so an export can never mix two projects.
    * Exporting always uses a frozen snapshot, never the live set.
    """

    def __init__(self, scope_token: Optional[str] = None):
        self._scope_token = scope_token
        self._ids: list = []
        self._id_set = set()
        self._current: Optional[str] = None
        self._query: Optional[QueryIdentity] = None

    # ------------------------------------------------------------------
    # Scope
    # ------------------------------------------------------------------
    @property
    def scope_token(self) -> Optional[str]:
        return self._scope_token

    def bind_scope(self, scope_token: Optional[str]) -> bool:
        """Bind a project session/epoch token.

        Returns ``True`` when the scope actually changed. A changed scope
        invalidates every selection: the ids belonged to the old project.
        """
        if scope_token == self._scope_token:
            return False
        self._scope_token = scope_token
        self.clear()
        return True

    # ------------------------------------------------------------------
    # Query identity
    # ------------------------------------------------------------------
    @property
    def query(self) -> Optional[QueryIdentity]:
        return self._query

    def set_query(self, identity: Optional[QueryIdentity]) -> None:
        """Record which query the current selection was made under.

        The query identity does NOT clear the selection. The user may keep a
        pick while narrowing the filter; the export then names both the ids
        and the query they came from.
        """
        self._query = identity

    # ------------------------------------------------------------------
    # Membership
    # ------------------------------------------------------------------
    def __len__(self) -> int:
        return len(self._ids)

    def __contains__(self, model_id: object) -> bool:
        return model_id in self._id_set

    def __iter__(self):
        return iter(tuple(self._ids))

    def is_empty(self) -> bool:
        return not self._ids

    @property
    def count(self) -> int:
        return len(self._ids)

    @property
    def current(self) -> Optional[str]:
        """The focused model. Drives the detail panel, not the export scope."""
        return self._current

    def ids(self) -> Tuple[str, ...]:
        """Selected ids in selection order (stable, so exports are stable)."""
        return tuple(self._ids)

    def contains(self, model_id: str) -> bool:
        return model_id in self._id_set

    # ------------------------------------------------------------------
    # Mutation
    # ------------------------------------------------------------------
    def set_single(self, model_id: Optional[str]) -> None:
        """Replace the whole selection with one id (plain click)."""
        self._ids = []
        self._id_set = set()
        if model_id:
            self._ids.append(model_id)
            self._id_set.add(model_id)
        self._current = model_id

    def toggle(self, model_id: str) -> bool:
        """Add or remove one id. Returns the new membership state."""
        if model_id in self._id_set:
            self._id_set.discard(model_id)
            self._ids = [item for item in self._ids if item != model_id]
            if self._current == model_id:
                self._current = self._ids[0] if self._ids else None
            return False
        self._ids.append(model_id)
        self._id_set.add(model_id)
        self._current = model_id
        return True

    def add_many(self, model_ids: Iterable[str]) -> None:
        for model_id in model_ids:
            if model_id and model_id not in self._id_set:
                self._ids.append(model_id)
                self._id_set.add(model_id)

    def remove_many(self, model_ids: Iterable[str]) -> None:
        drop = {model_id for model_id in model_ids if model_id}
        if not drop:
            return
        self._id_set -= drop
        self._ids = [item for item in self._ids if item not in drop]
        if self._current in drop:
            self._current = self._ids[0] if self._ids else None

    def clear(self) -> None:
        self._ids = []
        self._id_set = set()
        self._current = None

    def set_current(self, model_id: Optional[str]) -> None:
        """Move the focus without changing the membership."""
        self._current = model_id

    def reconcile(self, known_ids: Iterable[str]) -> Tuple[str, ...]:
        """Drop ids that no longer exist.

        Only call this when the full id set is known (a complete re-query),
        never with the ids of the visible page. Returns the dropped ids.
        """
        known = set(known_ids)
        dropped = tuple(model_id for model_id in self._ids if model_id not in known)
        if dropped:
            self.remove_many(dropped)
        return dropped

    # ------------------------------------------------------------------
    # Export scope
    # ------------------------------------------------------------------
    def freeze(self) -> SelectionSnapshot:
        """Capture the export scope. Later changes do not affect the snapshot."""
        return SelectionSnapshot(tuple(self._ids), self._scope_token, self._query)

    def visible_subset(self, page_ids: Iterable[str]) -> Tuple[str, ...]:
        """The part of the selection that is on the given page.

        The card surface can only paint what it displays, so it mirrors this
        subset. The rest of the selection stays in the controller.
        """
        page = set(page_ids)
        return tuple(model_id for model_id in self._ids if model_id in page)
