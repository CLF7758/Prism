"""Keep destination-dependent work behind the window's editor commit barrier."""
from PyQt6 import QtCore
from prism.item_lifecycle import is_deleted


class NavigationSequence:
    """Monotonic token source used to discard superseded navigation results.

    Every ``next()`` invalidates all tokens handed out earlier, so a callback
    (or a late asynchronous reply) that captured an older token can drop its
    result instead of writing it into a newer project or selection. Instances
    hold only an integer counter -- never callbacks or results -- so anything
    guarded by a stale token is released by simply not running it.

    ``current`` is ``0`` until the first ``next()``.
    """

    __slots__ = ('_token',)

    def __init__(self):
        self._token = 0

    @property
    def current(self):
        """The most recent token; still valid for an in-flight result."""
        return self._token

    def next(self):
        """Invalidate every previous token and return the fresh one."""
        self._token += 1
        return self._token

    def is_current(self, token):
        """True when ``token`` is the most recent one (``None`` never is)."""
        return token is not None and token == self._token


def navigation_ready(window, *, page_id=None, widget=None, content_version=None):
    """Return True only when the navigation target is demonstrably in place.

    Every non-None check must pass; any mismatch fails closed and returns
    False. A tab having changed -- or a fixed delay having elapsed -- is never
    treated as readiness.

    * ``widget`` -- ``window.tabs.currentWidget()`` must be exactly this widget
      (identity, not equality) and it must not be disposed.
    * ``page_id`` -- ``window.current_page_id`` must equal this value.
    * ``content_version`` -- ``window._navigation_content_version`` must equal
      this value. Windows that do not maintain a content version fail this
      check (fail closed) rather than claiming readiness.
    """
    if widget is not None:
        tabs = getattr(window, 'tabs', None)
        current = tabs.currentWidget() if tabs is not None else None
        if current is not widget:
            return False
        if isinstance(widget, QtCore.QObject) and is_deleted(widget):
            return False
    if page_id is not None:
        if getattr(window, 'current_page_id', None) != page_id:
            return False
    if content_version is not None:
        if getattr(window, '_navigation_content_version', None) != content_version:
            return False
    return True


def defer_navigation(window, callback, *, owner=None, ready=None, generation=None):
    """Return True when the complete action was handed to navigation.

    Call at the action boundary, not between opening a page and using its
    panel. The callback runs with the window's committed-navigation token.
    Lightweight adapters without a coordinator retain their synchronous
    behavior.

    ``ready`` is an optional zero-argument predicate run immediately before
    ``callback``; a False result drops the action instead of executing it.
    ``generation`` is an optional :class:`NavigationSequence`: the token in
    effect when this call was made must still be current, otherwise the
    callback is discarded. Together these drop superseded requests (A then B
    then C keeps only C) without touching the window's existing latest-intent
    barrier.
    """
    request = getattr(window, 'request_navigation', None)
    committed = getattr(window, '_navigation_is_committed', None)
    if request is None or committed is None or committed():
        return False

    token = generation.current if generation is not None else None

    def execute():
        if isinstance(owner, QtCore.QObject) and is_deleted(owner):
            return
        if isinstance(window, QtCore.QObject) and is_deleted(window):
            return
        if generation is not None and not generation.is_current(token):
            return
        if ready is not None and not ready():
            return
        callback()

    request(execute)
    return True
