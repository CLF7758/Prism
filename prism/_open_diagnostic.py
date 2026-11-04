"""TEMPORARY diagnostic: what happens on screen while a project opens.

The user reports that opening a project makes the whole interface flash and
the resource tree lose its expansion -- the category that was expanded to
show the current canvas collapses, and the window looks like it crashed and
started again.

The operations trace knows *when* the GUI thread was busy, but not *what
changed on screen*.  This module watches the four things that can change the
visible interface and writes every change to the trace with a timestamp, the
thread and the Python stack of the caller:

* the resource tree's **model** -- a reset replaces the whole tree and loses
  the expansion.  The stack says who reset it;
* the tree's **top level expansion** -- which category is open;
* the workspace **tab** -- a page swap replaces the visible panel;
* the scene's **item count** -- a cleared canvas is a blank canvas.

A light sampler (100 ms) records the same state continuously, so a change
that happens between two events is still caught.

The sampler runs on the GUI thread (a QTimer), so reading the widgets is
safe.  It reads only the tree's *top level*: the tree can hold 100 000 rows
and a full walk would cost more than the flash it is meant to explain.

Remove this module once the flash is explained.
"""
import os
import time
import traceback

from PyQt6 import QtCore

from prism.performance_trace import record as trace_record

#: The probe is a diagnostic, not a permanent part of the application.
ENABLE_ENV = 'PRISM_UI_DIAG'

#: How often the visible state is sampled.
SAMPLE_MS = 100

#: The tree's top level is walked up to this many rows; a project with more
#: categories than that would cost more than the flash itself.
MAX_TOP_ROWS = 64

_installed = False
_state = {}


def _enabled():
    value = os.environ.get(ENABLE_ENV, '').strip().lower()
    return value not in ('0', 'false', 'no', 'off')


def _tree(window):
    view = getattr(window, 'view', None)
    panel = getattr(view, 'category_panel', None)
    return getattr(panel, 'resource_tree', None)


def _top_expansion(tree):
    """The expanded state of the tree's top level -- cheap by design."""
    model = tree.model() if tree is not None else None
    if model is None:
        return []
    rows = []
    try:
        count = min(model.rowCount(), MAX_TOP_ROWS)
    except Exception:
        return []
    for row in range(count):
        try:
            index = model.index(row, 0)
            rows.append((str(index.data() or '')[:48], bool(tree.isExpanded(index))))
        except Exception:
            break
    return rows


def _scene_items(window):
    view = getattr(window, 'view', None)
    scene = getattr(view, 'scene', None)
    try:
        return len(scene.items())
    except Exception:
        return -1


def _tab_name(window):
    tabs = getattr(window, 'tabs', None)
    if tabs is None:
        return ''
    try:
        current = tabs.currentWidget()
        return type(current).__name__ if current is not None else ''
    except Exception:
        return ''


def _stack():
    try:
        return ''.join(traceback.format_stack(limit=24))
    except Exception:
        return '<no stack>'


def _record(event, **fields):
    """Write one observation to the operations trace."""
    try:
        trace_record(event, **fields)
    except Exception:
        pass


def _on_model_reset(*_args):
    """``modelReset`` empties the tree: every expanded category collapses."""
    window = _state.get('window')
    tree = _tree(window)
    _record('resource_tree_model_reset',
            expanded_before=_top_expansion(tree),
            scene_items=_scene_items(window),
            tab=_tab_name(window),
            caller=_stack())


def _on_rows_about_to_be_reset(*_args):
    window = _state.get('window')
    tree = _tree(window)
    _record('resource_tree_rows_about_to_reset',
            expanded_before=_top_expansion(tree),
            caller=_stack())


def _on_rows_changed(*_args):
    window = _state.get('window')
    tree = _tree(window)
    _record('resource_tree_rows_changed',
            expanded_after=_top_expansion(tree),
            scene_items=_scene_items(window),
            caller=_stack())


def _on_tab_changed(*_args):
    window = _state.get('window')
    _record('workspace_tab_changed',
            tab=_tab_name(window),
            expanded=_top_expansion(_tree(window)),
            scene_items=_scene_items(window),
            caller=_stack())


def _watch_model(window):
    """Follow the tree's model: it is replaced when a project is installed."""
    tree = _tree(window)
    model = tree.model() if tree is not None else None
    if model is None or model is _state.get('model'):
        return
    _state['model'] = model
    for name, handler in (('modelReset', _on_model_reset),
                          ('rowsAboutToBeReset', _on_rows_about_to_be_reset),
                          ('rowsInserted', _on_rows_changed),
                          ('rowsRemoved', _on_rows_changed)):
        signal = getattr(model, name, None)
        if signal is None:
            continue
        try:
            signal.connect(handler)
        except Exception:
            pass


def _sample():
    """One sample of everything the user can see."""
    window = _state.get('window')
    if window is None:
        return
    _watch_model(window)
    tree = _tree(window)
    expanded = _top_expansion(tree)
    items = _scene_items(window)
    previous = _state.get('previous')
    current = (tuple(expanded), items, _tab_name(window))
    if previous != current:
        _state['previous'] = current
        _record('ui_state_changed',
                expanded=expanded,
                scene_items=items,
                tab=_tab_name(window),
                previous_expanded=list(previous[0]) if previous else None,
                previous_scene_items=previous[1] if previous else None,
                previous_tab=previous[2] if previous else None)
    counter = _state.get('repaints')
    if counter is not None:
        seen = (counter.update_requests, counter.exposes)
        if seen != counter.reported:
            counter.reported = seen
            _record('ui_repaint_counters',
                    update_requests=seen[0],
                    exposes=seen[1])


class _RepaintCounter(QtCore.QObject):
    """Counts the repaints of the whole window.

    A whole-window repaint is what the user sees as a flash: the window
    redraws itself, canvas and side panels included.  The operations trace
    only knows how long the *slow* ones took; this counts all of them, so
    a change in the prewarm can be measured instead of guessed.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self.update_requests = 0
        self.exposes = 0
        self.reported = (0, 0)

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QtCore.QEvent.Type.UpdateRequest:
            self.update_requests += 1
        elif kind == QtCore.QEvent.Type.Expose:
            self.exposes += 1
        return False


def install(window):
    """Watch the visible interface of this window."""
    global _installed
    if _installed:
        return
    _installed = True
    if not _enabled():
        return
    _state['window'] = window
    _watch_model(window)
    tabs = getattr(window, 'tabs', None)
    if tabs is not None:
        try:
            tabs.currentChanged.connect(_on_tab_changed)
        except Exception:
            pass
    counter = _RepaintCounter(window)
    window.installEventFilter(counter)
    _state['repaints'] = counter
    timer = QtCore.QTimer(window)
    timer.setInterval(SAMPLE_MS)
    timer.timeout.connect(_sample)
    timer.start()
    _state['timer'] = timer
    _record('ui_diagnostic_installed', sample_ms=SAMPLE_MS,
            at=time.strftime('%Y-%m-%d %H:%M:%S'))
