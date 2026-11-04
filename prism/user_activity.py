"""When did the user last touch the app?

Background work has to share the GUI thread with the person using it.  The
library panels are built ahead of the click that needs them (see
``prism.widgets.widget_construction``), so that build needs to know whether
the user is currently working: while they are, the window belongs to them
and the panel waits.

This module keeps exactly one fact, and nothing else: the moment of the
last real user input.  It deliberately has no Qt dependency, so the
construction job can read it from any thread.
"""
from __future__ import annotations

import time


class UserActivity:
    """The last moment the user touched the app."""

    def __init__(self):
        self._last_input = 0.0

    def note_input(self, when=None):
        """Record user input.  Called from the application's event dispatch."""
        self._last_input = time.monotonic() if when is None else when

    def seconds_since_input(self):
        """Seconds since the last input; infinite when there was none."""
        if not self._last_input:
            return float('inf')
        return time.monotonic() - self._last_input

    def is_idle(self, seconds):
        """Whether the user has been quiet for at least ``seconds``."""
        return self.seconds_since_input() >= seconds

    def reset(self):
        """Forget the last input (used by tests that fake a quiet user)."""
        self._last_input = 0.0


#: One process, one user.
USER_ACTIVITY = UserActivity()
