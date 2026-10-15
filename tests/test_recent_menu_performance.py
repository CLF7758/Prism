"""A file-name update must not rebuild keyboard settings ten times."""
from unittest.mock import Mock

from prism.actions.actions import actions


def test_recent_menu_reuses_definitions_and_retargets_files(main_window, tmp_path, monkeypatch):
    view = main_window.view
    original = actions['recent_files_0']
    first, second = tmp_path/'first.prism', tmp_path/'second.prism'
    first.touch()
    second.touch()
    settings_factory = Mock(side_effect=AssertionError('recreated keyboard settings'))
    monkeypatch.setattr('prism.actions.actions.KeyboardSettings', settings_factory)
    monkeypatch.setattr(view.settings, 'get_recent_files', lambda **kw: [str(first)])
    view.update_menu_and_actions()
    monkeypatch.setattr(view.settings, 'get_recent_files', lambda **kw: [str(second)])
    view.update_menu_and_actions()
    assert actions['recent_files_0'] is original
    assert original.qaction.text() == 'second.prism'
    settings_factory.assert_not_called()
