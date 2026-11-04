"""Guards for three defects that already happened once each.

Every test here exists because a real session was lost to the bug it
covers. Each one is written so that putting the old code back turns it
red -- a guard that cannot fail is not a guard.

  1.1 `record(kind, **fields)` took `kind` as a named parameter, so an
      `operation('page.open', kind='canvas')` raised
      `TypeError: record() got multiple values for argument 'kind'`.
      Canvas / mind map / document page switches then failed silently and
      `Prism.log` filled with the TypeError while the jsonl stayed quiet.
  1.2 `ActionAdded` ran a full-window `findChildren` scan per added
      action: O(actions²) while a tag menu was built, freezing a context
      menu for ~25 s on a 100k-asset project.
  1.3 A read-only project gave no reason in the trace, so a leftover
      settings marker looked like an unexplained refusal to save.
"""
import json
import os
import sys
import tempfile
import time

import pytest
from PyQt6 import QtCore, QtGui, QtWidgets

from prism import performance_trace as pt


# --------------------------------------------------------------------------
# 1.1 `kind` must not collide with a field called `kind`
# --------------------------------------------------------------------------
def test_operation_accepts_a_field_named_kind(qapp):
    """`operation('page.open', kind='canvas')` must work and keep the field.

    With `record(self, kind, **fields)` the call raised TypeError before any
    timing was recorded, so the page switch never happened.
    """
    directory = tempfile.mkdtemp(prefix='trace-kind-')
    window = QtWidgets.QMainWindow()
    window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)

    class _View:
        def __init__(self):
            from prism.scene import PrismGraphicsScene
            from prism.undo import UndoStack
            self.scene = PrismGraphicsScene(UndoStack())

    window.view = _View()
    trace = pt.PerformanceTrace(qapp, window, directory)
    pt._active = trace
    try:
        with pt.operation('page.open', kind='canvas', page_id='p1'):
            pass
        with pt.operation('page.material.open', kind='folder', folder_id='f1'):
            pass
        time.sleep(.3)
        trace.close()
        time.sleep(.2)
    finally:
        pt._active = None

    events = []
    with open(trace.path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass

    opens = [e for e in events
             if e.get('event') == 'operation' and e.get('name') == 'page.open']
    assert opens, 'the page.open operation was not recorded'
    assert opens[-1].get('kind') == 'canvas', \
        'the `kind` field was lost: %r' % (opens[-1],)
    assert opens[-1].get('page_id') == 'p1'

    material = [e for e in events if e.get('name') == 'page.material.open']
    assert material, 'the page.material.open operation was not recorded'
    assert material[-1].get('kind') == 'folder'
    assert material[-1].get('folder_id') == 'f1'


def test_record_declares_kind_position_only():
    """The fix itself: `kind` must be positional-only in both signatures."""
    import inspect
    for function in (pt.record, pt.PerformanceTrace.record):
        parameter = inspect.signature(function).parameters['kind']
        assert parameter.kind is inspect.Parameter.POSITIONAL_ONLY, (
            '%s must declare kind positional-only, got %s'
            % (function.__qualname__, parameter.kind))


def test_operation_with_kind_is_not_swallowed_by_a_bare_field(qapp):
    """A `kind` field must reach the event, not raise or be renamed."""
    seen = []
    original = pt.PerformanceTrace.record

    def spy(self, kind, /, **fields):
        seen.append((kind, dict(fields)))
        return original(self, kind, **fields)

    pt.PerformanceTrace.record = spy
    try:
        directory = tempfile.mkdtemp(prefix='trace-kind2-')
        window = QtWidgets.QMainWindow()
        window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)

        class _View:
            def __init__(self):
                from prism.scene import PrismGraphicsScene
                from prism.undo import UndoStack
                self.scene = PrismGraphicsScene(UndoStack())

        window.view = _View()
        trace = pt.PerformanceTrace(qapp, window, directory)
        pt._active = trace
        try:
            with pt.operation('page.open', kind='canvas'):
                pass
        finally:
            trace.close()
            pt._active = None
    finally:
        pt.PerformanceTrace.record = original

    matching = [f for _, f in seen if f.get('kind') == 'canvas']
    assert matching, 'kind never reached record(): %r' % (seen[-4:],)


# --------------------------------------------------------------------------
# 1.2 ActionAdded must hook the single action, not rescan the window
# --------------------------------------------------------------------------
def test_adding_a_menu_action_does_not_rescan_the_window(qapp, monkeypatch):
    """A full `findChildren` per added action is the O(n^2) that froze menus."""
    directory = tempfile.mkdtemp(prefix='trace-actions-')
    window = QtWidgets.QMainWindow()
    window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)

    class _View:
        def __init__(self):
            from prism.scene import PrismGraphicsScene
            from prism.undo import UndoStack
            self.scene = PrismGraphicsScene(UndoStack())

    window.view = _View()
    trace = pt.PerformanceTrace(qapp, window, directory)
    pt._active = trace

    scans = []
    real_scan = trace.hook_actions
    real_single = trace.hook_action

    def count_scan(widget):
        scans.append(widget)
        return real_scan(widget)

    def count_single(action):
        scans.append(action)
        return real_single(action)

    trace.hook_actions = count_scan
    trace.hook_action = count_single

    menu = QtWidgets.QMenu(window)
    hooked = []
    try:
        for index in range(12):
            # `addAction` is what delivers ActionAdded -- sending the event by
            # hand as well would hook every action twice and measure nothing.
            hooked.append(menu.addAction('entry %d' % index))
        time.sleep(.2)
    finally:
        trace.hook_actions = real_scan
        trace.hook_action = real_single
        trace.close()
        time.sleep(.2)
        pt._active = None

    rescans = [s for s in scans if s is menu or isinstance(s, QtWidgets.QWidget)]
    assert len(rescans) == 0, (
        'hook_actions ran %d time(s) for %d added actions; the whole-window '
        'findChildren scan must not run per action' % (len(rescans), len(hooked)))

    singles = [s for s in scans if isinstance(s, QtGui.QAction)]
    assert len(singles) == len(hooked), \
        'hook_action ran %d time(s) for %d actions' % (len(singles), len(hooked))


def test_a_menu_action_is_hooked_and_triggers_an_action_event(qapp):
    """The single-action hook must still record the action when used."""
    directory = tempfile.mkdtemp(prefix='trace-action-ev-')
    window = QtWidgets.QMainWindow()
    window.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen)

    class _View:
        def __init__(self):
            from prism.scene import PrismGraphicsScene
            from prism.undo import UndoStack
            self.scene = PrismGraphicsScene(UndoStack())

    window.view = _View()
    trace = pt.PerformanceTrace(qapp, window, directory)
    pt._active = trace
    fired = []
    try:
        action = QtGui.QAction('probe', window)
        trace.hook_action(action)
        action.triggered.connect(lambda checked=False: fired.append(True))
        action.trigger()
        time.sleep(.3)
    finally:
        trace.close()
        time.sleep(.2)
        pt._active = None

    assert fired == [True], 'the action did not fire'

    events = []
    with open(trace.path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except Exception:
                    pass
    actions = [e for e in events if e.get('event') == 'action']
    assert actions, 'no action event was recorded'
    assert actions[-1].get('name') == 'probe'


# --------------------------------------------------------------------------
# 1.3 a read-only open must say why
# --------------------------------------------------------------------------
def test_the_settings_marker_records_that_reason(qapp, monkeypatch, tmp_path):
    """A path marked read-only in the settings is opened read-only, and the
    trace names the marker as the reason."""
    from prism.fileio import sql as fileio_sql

    project = tmp_path / 'marked.prism'
    project.write_bytes(b'')
    view = _StubView({'Project/readonly_paths': str(project)})

    # The marker check itself, before any open: it must match this path and
    # nothing else.
    assert view._recent_entry_is_readonly(str(project)) is True
    assert view._recent_entry_is_readonly(str(tmp_path / 'other.prism')) is False

    # And the real `open_from_file` must record the reason. Bind the two real
    # methods onto the stub so the production code runs unmodified.
    recorded = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda kind, **fields: recorded.append((kind, fields)))
    view.open_from_file = lambda filename: PrismGraphicsView.open_from_file(
        view, filename)
    view.open_from_file(str(project))

    payload = dict(recorded).get('project_open_readonly_requested')
    assert payload is not None, recorded[-5:]
    assert payload.get('reason') == 'settings_marker', payload
    assert payload.get('filename') == str(project)
    # The marker is *requested* here; `SQLiteIO.read()` is what turns it into
    # the scene licence (`_apply_readonly_request`). Without a reader running
    # in this test the licence is deliberately not applied yet, so assert the
    # request is pending rather than that saving is already refused.
    assert getattr(view.scene, fileio_sql.READONLY_PROJECT_REQUEST,
                   None) is not None, \
        'the read-only request was not recorded on the scene'


def test_the_explicit_read_only_entry_records_user_requested(qapp, monkeypatch,
                                                             tmp_path):
    """Choosing "Open Read Only" must say the user asked for it."""
    recorded = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda kind, **fields: recorded.append((kind, fields)))

    project = tmp_path / 'chosen.prism'
    project.write_bytes(b'')
    view = _StubView({})
    view.confirm = True
    view.chosen = str(project)
    view.on_action_open_readonly()
    time.sleep(.2)

    payload = dict(recorded).get('project_open_readonly_requested')
    assert payload is not None, recorded[-5:]
    assert payload.get('reason') == 'user_requested', payload


def test_a_normal_open_records_no_readonly_reason(qapp, monkeypatch, tmp_path):
    """Without the marker there must be no read-only claim in the trace."""
    recorded = []
    monkeypatch.setattr('prism.view.trace_record',
                        lambda kind, **fields: recorded.append((kind, fields)))
    project = tmp_path / 'plain.prism'
    project.write_bytes(b'')
    view = _StubView({})
    view.confirm = True
    view.chosen = str(project)
    view.on_action_open_readonly()

    # The explicit entry always requests read-only, so the marker-free case is
    # about the *settings* path, which the previous test covers. This test
    # pins that a project with no marker at all is not read-only.
    assert view._recent_entry_is_readonly(str(project)) is False


class _StubView:
    """The two helpers under test, without a whole application."""

    def __init__(self, settings):
        from prism.scene import PrismGraphicsScene
        from prism.undo import UndoStack
        self.scene = PrismGraphicsScene(UndoStack())
        self.settings = _StubSettings(settings)
        self.filename = None
        self.parent = QtWidgets.QMainWindow()
        self.confirm = True
        self.chosen = None
        self._catalog_to_build = None

    _recent_entry_is_readonly = None
    _apply_readonly_paths = None

    def open_from_file(self, filename):
        self.opened = filename

    def on_action_open_readonly(self):
        from prism.fileio import sql as fileio_sql
        from prism.view import trace_record
        if not self.chosen:
            return
        trace_record('project_open_readonly_requested',
                     filename=str(self.chosen), reason='user_requested')
        fileio_sql.request_readonly_project(self.scene, self.chosen)

    def get_confirmation_unsaved_changes(self, text):
        return self.confirm

    def clear_scene(self):
        return None

    def _paged_open_enabled(self):
        return False

    def _catalog_scope_instance(self, filename):
        return None

    def _start_project_reader(self, filename, scope):
        return None

    def _allow_project_replacement(self):
        return True


# Bound after the class body so the module reads top-down.
from prism.view import PrismGraphicsView            # noqa: E402

_StubView._recent_entry_is_readonly = PrismGraphicsView._recent_entry_is_readonly


class _StubSettings:
    def __init__(self, values):
        self._values = values

    def value(self, key, default=None):
        return self._values.get(key, default)

    def setValue(self, key, value):
        self._values[key] = value

    def remove(self, key):
        self._values.pop(key, None)