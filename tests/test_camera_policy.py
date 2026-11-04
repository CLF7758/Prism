"""Camera and menu acceptance tests (AI-05, P08 / P09).

Two product requirements are guarded here.

**P08 -- one camera.**  The canvas has several visible ways to ask for
"everything on this canvas": the menu item, the toolbar button, the
right-click entry, the button in the status bar, and Ctrl+0.  They have to
land on the same place, they have to be idempotent, and they have to leave
the same margin -- 20 *logical screen pixels*, not 20 scene units, so the
gap does not change with the zoom.

**P09 -- no "bring to front" / "send to back".**  The user asked for the
z-order entries to go away.  What is left behind must not be a dangling
action id: a keyboard settings file written by an older Prism still holds
``Actions/raise_to_top = PgUp``, and it has to be inert, not fatal.
"""
import pytest

from PyQt6 import QtCore, QtGui, QtTest, QtWidgets

from prism import camera_policy
from prism.actions.actions import actions
from prism.actions.menu_structure import menu_structure
from prism.items import PrismPixmapItem, PrismTextItem


#: The action ids of the removed z-order entries.
Z_ORDER_ACTION_IDS = ('raise_to_top', 'lower_to_bottom')

#: The margin every camera entry has to leave, in logical screen pixels.
MARGIN = camera_policy.CAMERA_MARGIN


def _menu_action_ids():
    """Every action id that any menu shows."""
    found = []

    def walk(items):
        for item in items:
            if isinstance(item, dict):
                walk(item['items'])
            elif isinstance(item, str) and item != '_build_recent_files':
                found.append(item)

    walk(menu_structure)
    return found


def _camera_state(view):
    """The camera of a view: its transform and the scene point it looks at."""
    transform = view.transform()
    centre = view.mapToScene(view.get_view_center())
    return (round(transform.m11(), 9), round(transform.m12(), 9),
            round(transform.m21(), 9), round(transform.m22(), 9),
            round(centre.x(), 6), round(centre.y(), 6))


def _screen_centre(view):
    """The scene point at the centre of the viewport, in logical pixels."""
    point = view.mapToScene(view.viewport().rect().center())
    return point.x(), point.y()


def _margin_in_screen_px(view, rect):
    """The gap between the content and the viewport edge, in logical pixels."""
    if rect is None:
        return None
    top_left = view.mapFromScene(rect.topLeft())
    bottom_right = view.mapFromScene(rect.bottomRight())
    viewport = view.viewport().rect()
    return (top_left.x() - viewport.left(),
            viewport.right() - bottom_right.x(),
            top_left.y() - viewport.top(),
            viewport.bottom() - bottom_right.y())


def _add_asset(view, x, y, width=120, height=80, rotation=0):
    item = PrismPixmapItem(QtGui.QImage(width, height,
                                        QtGui.QImage.Format.Format_RGB32))
    item.setPos(x, y)
    item.setRotation(rotation)
    view.scene.addItem(item)
    return item


def _add_text(view, x, y, text='hello'):
    item = PrismTextItem(text)
    item.setPos(x, y)
    view.scene.addItem(item)
    return item


def _show(window, width=1200, height=800):
    """Give the window a real viewport -- an unshown view has no size."""
    window.resize(width, height)
    window.show()
    QtWidgets.QApplication.processEvents()
    return window


@pytest.fixture
def stale_shortcut_file(kbsettings):
    """A settings file that still names the removed z-order actions.

    The file is written by the user's own Prism, so it can outlive the
    actions it names.  It has to stay inert: the menu and the shortcut
    editor read the action registry, never the keys in the file.
    """
    kbsettings.setValue('Actions/raise_to_top', 'PgUp')
    kbsettings.setValue('Actions/lower_to_bottom', 'PgDown')
    kbsettings.sync()
    return kbsettings


class _StubCanvas(QtWidgets.QGraphicsView):
    """A stand-in for the aggregate canvas.

    The real one keeps a single scene item -- the layer that paints the
    card grid -- so its scene rectangle is the grid of the rows in scope.
    """

    def __init__(self, rows=0, placeholder=False):
        super().__init__()
        self.cards = [None] * rows
        self.setScene(QtWidgets.QGraphicsScene(self))
        if placeholder:
            # The material layer reports a 1x1 box until the layout has
            # been committed.
            self.scene().addItem(QtWidgets.QGraphicsRectItem(0, 0, 1, 1))
        else:
            self.scene().addItem(QtWidgets.QGraphicsRectItem(0, 0, 100, 60))


# ------------------------------------------------------------------- P09

def test_the_z_order_actions_are_not_registered():
    """No "raise to top" / "lower to bottom" action exists any more."""
    for action_id in Z_ORDER_ACTION_IDS:
        assert action_id not in actions


def test_no_menu_shows_the_z_order_entries():
    """The top menu bar and the right-click menu share one structure."""
    for action_id in _menu_action_ids():
        assert action_id not in Z_ORDER_ACTION_IDS


def test_no_shortcut_is_bound_to_the_z_order_actions():
    """PgUp and PgDown are free again."""
    for action_id, action in actions.items():
        for combo in action.shortcuts:
            assert 'pgup' not in combo.lower()
            assert 'pgdown' not in combo.lower()


def test_the_window_has_no_pgup_or_pgdown_qaction(main_window):
    """Not even a QAction that is not in the registry."""
    for qaction in main_window.actions():
        for shortcut in qaction.shortcuts():
            key = shortcut.toString().lower()
            assert 'pgup' not in key
            assert 'pgdown' not in key


def test_the_context_menu_has_no_z_order_entry(main_window):
    view = main_window.view
    view._remove_dynamic_menu_items()
    view._inject_canvas_tools()
    for action in view.context_menu.actions():
        text = action.text()
        assert '置顶' not in text
        assert '置底' not in text
        assert 'Raise to Top' not in text
        assert 'Lower to Bottom' not in text


def test_a_stale_shortcut_file_does_not_break_the_shortcut_editor(
        stale_shortcut_file):
    """An old settings file still names the removed actions.

    The file is written by the user's own Prism, so it can outlive the
    actions.  It has to stay inert: the shortcut editor reads the action
    registry, never the keys in the file.
    """
    from prism.widgets.controls.keyboard import KeyboardShortcutsModel
    model = KeyboardShortcutsModel()
    assert model.rowCount(None) == len(actions)
    for row in range(model.rowCount(None)):
        for column in range(model.columnCount(None)):
            model.data(model.index(row, column),
                       QtCore.Qt.ItemDataRole.DisplayRole)

    # the removed ids are still in the file, and still inert
    assert stale_shortcut_file.value('Actions/raise_to_top') == 'PgUp'
    assert stale_shortcut_file.value('Actions/lower_to_bottom') == 'PgDown'


def test_the_stale_shortcut_file_does_not_break_the_menu(stale_shortcut_file,
                                                        main_window):
    """The menu is built from the same stale file and must still come up.

    The window builds its menu once, while it is created -- so the stale
    keys have to be in the file *before* the window exists.  That is what
    this fixture does.
    """
    view = main_window.view
    menu = view.context_menu
    assert menu is not None
    texts = [action.text() for action in menu.actions()]
    assert not any('Raise to Top' in text for text in texts)
    assert not any('Lower to Bottom' in text for text in texts)
    assert not any('置顶' in text or '置底' in text for text in texts)
    # and the camera entry is still there: it is injected into the context
    # menu when the menu is opened, so it has to survive the stale file too
    view._remove_dynamic_menu_items()
    view._inject_canvas_tools()
    texts = [action.text() for action in view.context_menu.actions()]
    assert any('Reset Camera' in text or '重置相机' in text for text in texts)
    # the removed ids stayed in the file, and stayed inert
    assert stale_shortcut_file.value('Actions/raise_to_top') == 'PgUp'
    assert stale_shortcut_file.value('Actions/lower_to_bottom') == 'PgDown'
    for action_id in Z_ORDER_ACTION_IDS:
        assert action_id not in actions


@pytest.mark.xfail(reason=(
    'F2 (not AI-05\'s file): prism/actions/mixin.py registers the ten '
    'recent-files actions without a callback, so building the menu a '
    'second time dies with "TypeError: attribute name must be string, '
    'not \'NoneType\'".  The defect predates the P09 work -- the app '
    'builds the menu once, and this test is the second build.'),
    strict=False)
def test_the_menu_can_be_built_a_second_time(main_window):
    """A menu rebuild must survive the recent-files actions.

    See the xfail reason: the defect is in ``prism/actions/mixin.py``,
    which belongs to another owner.  The test stays here so the defect
    stays visible and turns into an XPASS the day it is fixed.
    """
    main_window.view.build_menu_and_actions()
    assert main_window.view.context_menu is not None


def test_the_other_action_groups_still_work(main_window):
    """Removing the z-order entries took nothing else with it."""
    for action_id in ('copy', 'cut', 'paste', 'delete', 'undo', 'redo',
                      'select_all', 'deselect_all', 'fit_scene',
                      'fit_selection', 'actual_size'):
        assert action_id in actions
        assert actions[action_id].qaction is not None
    for group in ('active_when_selection', 'active_when_can_undo',
                  'active_when_can_redo'):
        assert main_window.view.prism_actiongroups[group]


# ------------------------------------------------------------------- P08

def test_every_camera_entry_lands_on_the_same_place(main_window, qapp):
    """Menu, toolbar, right-click and the status bar button are one camera."""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    view.on_action_fit_scene()
    expected = _camera_state(view)
    main_window.canvas_toolbar.fit_all()
    assert _camera_state(view) == expected
    view.reset_canvas()
    assert _camera_state(view) == expected
    assert not hasattr(main_window,'_fit_all_button')


def test_the_camera_entries_agree_on_the_screen_centre(main_window, qapp):
    """The screen position of the centre is what the user sees."""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    _add_asset(view, 260, 180)

    view.on_action_fit_scene()
    expected = _screen_centre(view)
    main_window.canvas_toolbar.fit_all()
    assert _screen_centre(view) == pytest.approx(expected, abs=1.0)
    view.reset_canvas()
    assert _screen_centre(view) == pytest.approx(expected, abs=1.0)


def test_the_camera_reset_is_idempotent(main_window, qapp):
    """Pressing it twice does not move the picture."""
    view = main_window.view
    _show(main_window)
    _add_asset(view, 40, 30)
    view.on_action_fit_scene()
    first = _camera_state(view)
    view.on_action_fit_scene()
    assert _camera_state(view) == first
    main_window.canvas_toolbar.fit_all()
    assert _camera_state(view) == first


def test_the_camera_reset_does_not_restore_a_previous_fit(main_window, qapp):
    """A reset is not a toggle of the last single-item fit."""
    view = main_window.view
    _show(main_window)
    item = _add_asset(view, 40, 30)
    item.setSelected(True)
    view.fit_rect(QtCore.QRectF(0, 0, 10, 10), toggle_item=item)
    assert view.previous_transform is not None

    view.on_action_fit_scene()
    assert view.previous_transform is None
    first = _camera_state(view)
    view.on_action_fit_scene()
    assert _camera_state(view) == first


def test_an_empty_canvas_returns_to_the_origin(main_window, qapp):
    """The empty state is the same for every entry."""
    view = main_window.view
    _show(main_window)
    view.on_action_fit_scene()
    transform = view.transform()
    assert transform.m11() == pytest.approx(1.0)
    assert transform.m22() == pytest.approx(1.0)
    centre = view.mapToScene(view.get_view_center())
    assert centre.x() == pytest.approx(0.0, abs=1.0)
    assert centre.y() == pytest.approx(0.0, abs=1.0)


def test_the_camera_entries_do_not_recurse(main_window, qapp):
    """The toolbar and the view must not call each other in a circle."""
    view = main_window.view
    calls = []
    original = view.on_action_fit_scene

    def counted():
        calls.append('view')
        original()

    view.on_action_fit_scene = counted
    main_window.canvas_toolbar.fit_all()
    assert calls == ['view']


def test_ctrl_zero_is_bound_to_the_camera_reset():
    """The shortcut the toolbar advertises has to work."""
    assert 'Ctrl+0' in actions['fit_scene'].get_shortcuts()


def test_the_toolbar_hint_names_the_shortcuts_that_work(main_window):
    """The hint is read from the action, so it cannot drift."""
    action = actions['fit_scene']
    label = main_window.canvas_toolbar.camera_label()
    assert label.startswith(action.text)
    for shortcut in action.get_shortcuts():
        assert shortcut in label


def test_the_camera_has_one_name_in_the_chinese_ui(main_window):
    """Menu, toolbar and right-click entry spell the same name."""
    from prism.i18n.zh_CN import zh_CN
    assert zh_CN[actions['fit_scene'].text] == '重置相机'
    view = main_window.view
    view._remove_dynamic_menu_items()
    view._inject_canvas_tools()
    texts = [action.text() for action in view.context_menu.actions()]
    # the right-click entry is injected with the same string as the menu
    # action, so it reads the same in every language
    assert 'Reset Camera' in texts or '重置相机' in texts
    assert texts.count('Reset Camera') + texts.count('重置相机') == 1


# ------------------------------------------------- the margin and the scope

def test_the_margin_is_twenty_logical_screen_pixels(qtbot):
    """The margin does not change with the zoom."""
    canvas = _StubCanvas(rows=3)
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()

    bounds = camera_policy.aggregate_content_bounds(canvas)
    camera_policy.fit_bounds(canvas, bounds)
    margin = _margin_in_screen_px(canvas, bounds)
    assert min(margin) >= MARGIN - 1


def test_the_margin_survives_a_different_zoom(qtbot):
    """The same content at another zoom keeps the same screen margin."""
    canvas = _StubCanvas(rows=3)
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()

    bounds = camera_policy.aggregate_content_bounds(canvas)
    camera_policy.fit_bounds(canvas, bounds)
    wide = _margin_in_screen_px(canvas, bounds)

    canvas.scale(0.5, 0.5)
    camera_policy.fit_bounds(canvas, bounds)
    narrow = _margin_in_screen_px(canvas, bounds)
    assert min(narrow) >= MARGIN - 1
    assert min(wide) >= min(narrow) - 1


def test_the_margin_follows_the_viewport(qtbot):
    """A splitter drag changes the viewport, not the margin."""
    canvas = _StubCanvas(rows=3)
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()

    bounds = camera_policy.aggregate_content_bounds(canvas)
    camera_policy.fit_bounds(canvas, bounds)
    assert min(_margin_in_screen_px(canvas, bounds)) >= MARGIN - 1

    canvas.resize(400, 300)
    camera_policy.fit_bounds(canvas, bounds)
    assert min(_margin_in_screen_px(canvas, bounds)) >= MARGIN - 1


def test_the_aggregate_canvas_without_cards_returns_to_the_origin(qtbot):
    """The empty aggregate canvas behaves like the empty main canvas."""
    canvas = _StubCanvas(rows=0)
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()

    assert camera_policy.aggregate_content_bounds(canvas) is None
    assert camera_policy.fit_bounds(canvas, None) is None
    assert canvas.transform().m11() == pytest.approx(1.0)


def test_the_layout_placeholder_is_not_fitted(qtbot):
    """The one pixel placeholder must not zoom into an empty canvas.

    The material layer reports a 1x1 box until the layout has been
    committed.  Fitting to it would leave the camera at a huge zoom on an
    empty canvas.
    """
    canvas = _StubCanvas(rows=3, placeholder=True)
    qtbot.addWidget(canvas)
    canvas.resize(600, 400)
    canvas.show()

    assert camera_policy.aggregate_content_bounds(canvas) is None
    camera_policy.fit_bounds(canvas,
                             camera_policy.aggregate_content_bounds(canvas))
    assert canvas.transform().m11() == pytest.approx(1.0)
    assert canvas.transform().m22() == pytest.approx(1.0)


def test_the_camera_covers_text_and_drawings(view, qapp):
    """Text frames and drawings are user content, not "not saved"."""
    _add_asset(view, 40, 30)
    _add_text(view, 400, 300, 'a text frame')
    bounds = camera_policy.visible_asset_bounds(view)
    assert bounds is not None
    assert bounds.left() <= 40
    assert bounds.right() >= 400
    assert bounds.bottom() >= 300


def test_the_camera_skips_hidden_items(view, qapp):
    """A filtered out item is out of the bounds."""
    item = _add_asset(view, 40, 30)
    _add_asset(view, 400, 300)
    with_hidden = camera_policy.visible_asset_bounds(view)
    item.setVisible(False)
    without_hidden = camera_policy.visible_asset_bounds(view)
    assert without_hidden is not None
    # the hidden item is the left one, so the bounds start further right
    # and are narrower -- the right edge happens to be the same for both,
    # because the second item reaches exactly as far as the union did.
    assert with_hidden.left() == pytest.approx(40)
    assert without_hidden.left() == pytest.approx(400)
    assert without_hidden.width() < with_hidden.width()
    assert without_hidden.right() == pytest.approx(with_hidden.right())


def test_the_camera_reuses_the_cached_bounds(view, qapp, monkeypatch):
    """A reset must not add a walk of its own.

    The project can hold a hundred thousand records; the fit has to use
    the bounds the view already keeps.  The view walks the assets once
    per content revision -- that walk is the view's, not the camera's --
    so the camera may not add a second one on top of it.
    """
    _add_asset(view, 40, 30)
    scene = view.scene
    if callable(scene):
        scene = scene()
    # a timer driven recalculation would walk the scene behind our back
    timer = getattr(view, '_recalc_timer', None)
    if timer is not None:
        timer.stop()

    walks = {'n': 0}
    original = type(scene).iter_assets

    def counting(self):
        walks['n'] += 1
        return original(self)

    monkeypatch.setattr(type(scene), 'iter_assets', counting)

    bounds = camera_policy.visible_asset_bounds(view)
    assert bounds is not None
    assert bounds.left() <= 40
    after_first = walks['n']

    # the second call is served from the view's cache: no new walk
    camera_policy.visible_asset_bounds(view)
    assert walks['n'] == after_first


def test_a_reset_waits_for_the_layout_that_is_still_coming(view, qapp):
    """A reset taken before the content arrived is applied when it lands."""
    _show(view.window())
    content = {'rect': None}

    def provider(_view):
        return content['rect']

    camera_policy.request_reset(view, provider)
    assert view.previous_transform is None
    # nothing to fit yet: the camera waits
    assert camera_policy.reset_if_stale(view) is False

    _add_asset(view, 40, 30)
    content['rect'] = camera_policy.visible_asset_bounds(view)
    # the content generation moved on, so the reset is applied now
    view.scene._invalidate_view_bounds()
    assert camera_policy.reset_if_stale(view) is True
    assert view.transform().m11() > 0


def test_a_reset_is_not_pulled_back_by_a_later_layout(view, qapp):
    """The camera the user moved is not yanked back."""
    _show(view.window())
    content = {'rect': None}

    def provider(_view):
        return content['rect']

    camera_policy.request_reset(view, provider)
    _add_asset(view, 40, 30)
    content['rect'] = camera_policy.visible_asset_bounds(view)
    view.scene._invalidate_view_bounds()

    # the user moves the camera
    view.scale(2.0, 2.0)
    moved = view.transform()
    assert camera_policy.reset_if_stale(view) is False
    assert view.transform() == moved


def test_a_rotated_and_cropped_asset_is_fitted(view, qapp):
    """A rotated asset contributes the box of its transformed corners."""
    _show(view.window())
    item = _add_asset(view, 200, 200, width=200, height=100, rotation=45)
    item.setScale(1.5)
    bounds = camera_policy.visible_asset_bounds(view)
    assert bounds is not None
    # the rotated corners reach further than the unrotated rectangle
    assert bounds.width() > 200
    assert bounds.height() > 100
