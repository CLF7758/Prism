"""Opt-in real-window acceptance: first screen while the catalog is held.

Run with PRISM_FIRST_SCREEN_SOURCE pointing at an existing project. The project
is opened read-only; derived caches and settings stay in the test directory.
"""
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, QtWidgets, sip


class PaintObserver(QtCore.QObject):
    def __init__(self, view, started):
        super().__init__(view)
        self.view, self.started, self.first_paint = view, started, None

    def eventFilter(self, watched, event):
        if (event.type() == QtCore.QEvent.Type.Paint
                and self.view.scene._item_count > 0 and self.first_paint is None):
            self.first_paint = time.perf_counter() - self.started
        return False


@pytest.mark.skipif(not os.environ.get('PRISM_FIRST_SCREEN_SOURCE'),
                    reason='Explicit real-project acceptance only')
def test_real_first_screen_does_not_wait_for_catalog(qapp, qtbot, monkeypatch, tmp_path):
    from prism.__main__ import PrismMainWindow
    from prism import view as view_module
    from prism.project_catalog_index import ProjectCatalogIndex

    source = Path(os.environ['PRISM_FIRST_SCREEN_SOURCE']).resolve(strict=True)
    output = Path(os.environ['PRISM_FIRST_SCREEN_OUTPUT'])
    output.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    entered, release = threading.Event(), threading.Event()
    builds = []
    original_build = ProjectCatalogIndex.build

    def held_build(index, *args, **kwargs):
        builds.append(str(index.cache))
        entered.set()
        if not release.wait(35):
            raise RuntimeError('Acceptance catalog hold expired')
        return original_build(index, *args, **kwargs)

    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    monkeypatch.setattr(view_module, '_catalog_cache_path',
                        lambda filename: str(tmp_path / 'cold-index.sqlite'))
    monkeypatch.setattr(ProjectCatalogIndex, 'build', held_build)
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
            patch('prism.capture_hotkey.CaptureHotkey'), \
            patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'):
        window = PrismMainWindow(qapp)
    view = window.view
    result = dict(source=str(source), readonly=True, platform=qapp.platformName(),
                  acceptance_passed=False, catalog_intentionally_held=True)
    try:
        window.resize(1280, 800)
        window.show()
        started = time.perf_counter()
        observer = PaintObserver(view, started)
        view.viewport().installEventFilter(observer)
        view.open_from_file(str(source), readonly=True)
        operation = view._opening_operation
        qtbot.waitUntil(lambda: operation.terminal and observer.first_paint is not None,
                        timeout=30000)
        assert operation.phase == 'ready'
        assert entered.is_set() and not release.is_set()
        assert not view._open_scope.ready
        assert view.filename == str(source)
        assert view.scene._item_count > 0
        assert view.scene._partial_project_session is not None
        result.update(first_paint_seconds=observer.first_paint,
                      ready_seconds=time.perf_counter()-started,
                      canvas=view.current_canvas_id,
                      installed_rows=view.scene._item_count,
                      source_scope_rows=len(view.scene._loaded_scope_ids),
                      catalog_ready=view._open_scope.ready,
                      operation_phase=operation.phase,
                      baseline_attached=True, build_calls=len(builds))
        assert view.viewport().grab().save(str(output / 'first-screen.png'))
        result['acceptance_passed'] = True
    finally:
        view._cancel_catalog_builds()
        release.set()
        qtbot.waitUntil(lambda: getattr(view, '_catalog_build_active', None) is None,
                        timeout=10000)
        after = source.stat()
        result['source_stat_unchanged'] = ((before.st_size, before.st_mtime_ns)
                                         == (after.st_size, after.st_mtime_ns))
        result['source_check'] = 'size_and_mtime_only_not_full_hash'
        (output / 'native-first-screen.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        if not sip.isdeleted(view.undo_stack):
            view.undo_stack.setClean()
        window.browser_capture.stop()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert result['source_stat_unchanged']
