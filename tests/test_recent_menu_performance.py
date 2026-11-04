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
    assert original.qaction.text() == 'second'
    settings_factory.assert_not_called()


def test_project_title_and_recent_label_use_directory_project_name_without_io(main_window,tmp_path,monkeypatch):
    from prism.storage.entrypoint import display_project_name
    path=tmp_path/'City.prism.data'/'metadata.sqlite'
    assert display_project_name(path)=='City'
    assert display_project_name(tmp_path/'City.prism')=='City'
    assert display_project_name(path.parent)=='City'
    main_window.view.filename=str(path)
    monkeypatch.setattr('prism.storage.managed.root_for',Mock(side_effect=AssertionError('title probed disk')))
    main_window.view.update_window_title()
    assert main_window.windowTitle().startswith('City')
    assert 'metadata.sqlite' not in main_window.windowTitle()
