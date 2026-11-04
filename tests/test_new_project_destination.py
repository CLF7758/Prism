from PyQt6 import QtWidgets


def test_cancel_new_project_location_keeps_current_project(view, monkeypatch):
    calls = []
    monkeypatch.setattr(view, 'get_confirmation_unsaved_changes', lambda *_: True)
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', lambda **_: ('', ''))
    monkeypatch.setattr(view, 'clear_scene', lambda: calls.append('cleared'))
    view.on_action_new_scene()
    assert calls == []


def test_new_project_selects_location_before_clearing_and_uses_managed_save(view, tmp_path, monkeypatch):
    calls = []
    target = tmp_path / 'My Project.prism'
    monkeypatch.setattr(view, 'get_confirmation_unsaved_changes', lambda *_: True)
    def choose(**kwargs):
        assert not calls
        assert '新建项目' in kwargs['caption']
        return str(target), ''
    monkeypatch.setattr(QtWidgets.QFileDialog, 'getSaveFileName', choose)
    monkeypatch.setattr(view, 'clear_scene', lambda: calls.append('cleared'))
    monkeypatch.setattr(view, 'do_save', lambda path, **kw: calls.append((path, kw)))
    view.on_action_new_scene()
    assert calls == ['cleared', (str(target) + '.data', {'create_new': True})]
