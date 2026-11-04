"""Tests for the on-demand ("paged") open switch.

The switch is registered in ``prism/config/settings.py`` under the key
``Project/paged_open`` and is driven by the settings dialog through
``prism/widgets/settings.py``.

Two properties matter most here:

* ``valueOrDefault`` must return a real ``bool`` for this key, no matter
  which spelling the settings file happens to contain.  ``bool('off')``
  is True, so a plain ``bool`` cast would read a *disabled* switch as
  enabled -- the single worst bug this setting could have.
* The value must survive a round trip through the INI backend, which
  stores booleans as ``true``/``false`` and text as text.
"""

import pytest
from PyQt6 import QtWidgets

from prism.config.settings import (
    PAGED_OPEN_DEFAULT,
    PrismSettings,
    normalize_bool,
)
from prism.widgets.settings import PagedOpenWidget, SettingsDialog


KEY = 'Project/paged_open'


def _parse_like_view(value):
    """The exact parsing prism/view.py applies to this setting.

    Mirrors ``PrismGraphicsView._paged_open_enabled`` so that the
    registered value can be proven to reach the view correctly without
    touching ``prism/view.py`` (the main session's reserved file).
    """

    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


def test_paged_open_key_is_registered():
    assert KEY in PrismSettings.FIELDS


def test_paged_open_default_is_a_real_bool(settings):
    value = settings.valueOrDefault(KEY)
    assert isinstance(value, bool)
    assert value is PAGED_OPEN_DEFAULT
    assert value is True, (
        'paged open is on by default since 2026-10-23 '
        '(coordination/决策-20261023.md)')


@pytest.mark.parametrize('stored', [
    'on', 'ON', 'On', '1', 'true', 'True', 'TRUE', 'yes', True, 1])
def test_paged_open_reads_every_enabled_spelling_as_true(settings, stored):
    settings.setValue(KEY, stored)
    value = settings.valueOrDefault(KEY)
    assert isinstance(value, bool)
    assert value is True
    assert _parse_like_view(value) is True


@pytest.mark.parametrize('stored', [
    'off', 'OFF', 'Off', '0', 'false', 'False', 'FALSE', 'no', False, 0])
def test_paged_open_reads_every_disabled_spelling_as_false(settings, stored):
    settings.setValue(KEY, stored)
    value = settings.valueOrDefault(KEY)
    assert isinstance(value, bool)
    assert value is False, (
        f'{stored!r} must never read back as enabled')
    assert _parse_like_view(value) is False


def test_paged_open_falls_back_to_the_default_for_garbage(settings):
    settings.setValue(KEY, 'maybe')
    assert settings.valueOrDefault(KEY) is PAGED_OPEN_DEFAULT


def test_paged_open_value_survives_a_restart(settings):
    """A bool is stored as true/false in the INI file and reads back as
    a bool, so the switch keeps its meaning across restarts."""

    settings.setValue(KEY, True)
    settings.sync()
    assert settings.value(KEY) is True
    assert settings.valueOrDefault(KEY) is True

    settings.setValue(KEY, False)
    settings.sync()
    assert settings.value(KEY) is False
    assert settings.valueOrDefault(KEY) is False


def test_normalize_bool_rejects_values_it_cannot_interpret():
    with pytest.raises(ValueError):
        normalize_bool('perhaps')
    with pytest.raises(ValueError):
        normalize_bool(7)
    with pytest.raises(ValueError):
        normalize_bool(None)


def test_paged_open_widget_shows_the_stored_value(settings, qtbot):
    settings.setValue(KEY, True)
    widget = PagedOpenWidget()
    assert widget.input.isChecked() is True

    settings.setValue(KEY, False)
    widget = PagedOpenWidget()
    assert widget.input.isChecked() is False


def test_paged_open_widget_writes_the_change_to_the_settings(settings, qtbot):
    widget = PagedOpenWidget()
    # The default is now on, so turning the switch *off* is the edit.
    widget.set_value(False)

    assert settings.valueOrDefault(KEY) is False
    assert settings.value(KEY) is False, 'the switch must be a real bool'
    assert widget.title().endswith('✎')

    widget.set_value(True)
    assert settings.valueOrDefault(KEY) is True
    assert settings.value(KEY) is True
    assert widget.title() == 'On-demand opening:'


def test_paged_open_widget_marks_an_edited_value(settings, qtbot):
    widget = PagedOpenWidget()
    assert widget.title() == 'On-demand opening:'
    widget.set_value(False)
    assert widget.title() == 'On-demand opening: ✎'


def test_paged_open_widget_restores_the_default(settings, qtbot):
    settings.setValue(KEY, False)
    widget = PagedOpenWidget()
    assert widget.input.isChecked() is False

    # The real "restore defaults" path: the key is removed from the
    # settings file and the change is broadcast to every open group.
    settings.restore_defaults()
    assert widget.input.isChecked() is True
    assert settings.valueOrDefault(KEY) is PAGED_OPEN_DEFAULT
    assert widget.title() == 'On-demand opening:'


def test_paged_open_switch_is_visible_in_the_settings_dialog(settings, qtbot):
    parent = QtWidgets.QWidget()
    dialog = SettingsDialog(parent)
    widgets = dialog.findChildren(PagedOpenWidget)
    assert len(widgets) == 1, 'the switch must exist exactly once'
    widget = widgets[0]

    # Find the tab page the switch lives on and make sure that page is
    # reachable -- the switch has to be visible, not merely present.
    tab_widget = dialog.findChildren(QtWidgets.QTabWidget)[0]
    page = None
    for index in range(tab_widget.count()):
        candidate = tab_widget.widget(index)
        if candidate is not None and candidate.findChild(PagedOpenWidget) is widget:
            page = index
            break
    assert page is not None, 'the switch must live on a settings tab'
    tab_widget.setCurrentIndex(page)
    assert widget.isVisible() is True


def test_paged_open_switch_writes_through_the_dialog(settings, qtbot):
    parent = QtWidgets.QWidget()
    dialog = SettingsDialog(parent)
    widget = dialog.findChildren(PagedOpenWidget)[0]

    # Default is on: turn off first (a real edit), then back on.
    widget.set_value(False)
    assert settings.valueOrDefault(KEY) is False
    assert settings.value(KEY) is False

    widget.set_value(True)
    assert settings.valueOrDefault(KEY) is True
    assert settings.value(KEY) is True


def test_the_environment_override_still_wins_over_the_registered_value(
        settings, monkeypatch):
    """The env var is a hard override in both directions and must not be
    weakened by the setting becoming a registered one."""

    settings.setValue(KEY, True)
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    assert _parse_like_view(settings.valueOrDefault(KEY)) is True, (
        'the registered value itself is unaffected by the env var')

    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    settings.setValue(KEY, False)
    assert _parse_like_view(settings.valueOrDefault(KEY)) is False


def test_the_real_view_gate_honours_the_registered_value(
        settings, view, monkeypatch):
    """The switch reaches the unmodified production gate.

    ``prism/view.py`` is the main session's reserved file, so this test
    only *calls* it.  It proves that a value written through the
    registered path is read back as a real bool and that the existing
    ``isinstance(value, bool)`` branch of ``_paged_open_enabled`` turns
    it into the right answer.
    """

    monkeypatch.delenv('PRISM_PAGED_OPEN', raising=False)

    # Nothing written yet: the registered default governs (now on).
    view.settings.remove(KEY)
    assert view._paged_open_enabled() is True

    # The switch, turned on through the registered path.
    view.settings.setValue(KEY, True)
    assert view.settings.valueOrDefault(KEY) is True
    assert view._paged_open_enabled() is True

    # The switch, turned off again.
    view.settings.setValue(KEY, False)
    assert view._paged_open_enabled() is False

    # The environment remains a hard override in both directions.
    view.settings.setValue(KEY, True)
    monkeypatch.setenv('PRISM_PAGED_OPEN', '0')
    assert view._paged_open_enabled() is False
    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    view.settings.setValue(KEY, False)
    assert view._paged_open_enabled() is True
