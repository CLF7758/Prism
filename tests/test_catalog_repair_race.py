from types import SimpleNamespace

from prism.view import PrismGraphicsView


def test_queued_repair_does_not_override_save_delta(qapp, qtbot):
    calls = []
    scope = SimpleNamespace(ready=False, index=SimpleNamespace(source='source.prism'),
                            cache_path='cache.sqlite')
    view = SimpleNamespace(scene=object(), _open_scope=scope, _catalog_build_active=None,
        _catalog_to_build=None, _maybe_start_catalog_build=lambda *args, **kwargs: calls.append(args))
    assert PrismGraphicsView.current_catalog_scope(view) is scope
    receipt = dict(project='source.prism', cache='cache.sqlite', catalog_deltas=('save-proof',))
    view._catalog_to_build = receipt
    view._catalog_build_active = object()
    qtbot.wait(10)
    assert calls == []
    assert view._catalog_to_build is receipt


def test_cache_path_reuses_initialized_settings_profile(qapp, settings, monkeypatch):
    from prism.config import PrismSettings
    from prism import thumbnail_cache
    import os
    expected = os.path.join(os.path.dirname(settings.fileName()), 'previews')
    def unexpected(*args, **kwargs):
        raise AssertionError('reading a cache path must not reconstruct settings')
    monkeypatch.setattr(thumbnail_cache, 'PrismSettings', unexpected)
    assert thumbnail_cache.cache_directory() == expected


def test_queued_repair_ignores_destroyed_qt_owner(qapp, qtbot):
    from PyQt6 import QtCore, sip
    scope = SimpleNamespace(ready=False, index=SimpleNamespace(source='source.prism'),
                            cache_path='cache.sqlite')
    view = QtCore.QObject()
    view.scene = object()
    view._open_scope = scope
    view._catalog_build_active = None
    calls = []
    view._maybe_start_catalog_build = lambda *args, **kwargs: calls.append(args)
    PrismGraphicsView.current_catalog_scope(view)
    sip.delete(view)
    qtbot.wait(10)
    assert calls == []
