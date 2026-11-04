"""Recovery capture yields to Qt and never rewrites an unchanged revision."""
from pathlib import Path
import pytest
from prism.recovery import RecoveryStore


@pytest.fixture(autouse=True)
def isolated_recovery(main_window, tmp_path, qtbot):
    qtbot.waitUntil(lambda: main_window._recovery_worker is None, timeout=10000)
    main_window._recovery_store = RecoveryStore(tmp_path/'recovery')


def checkpoint(window, qtbot):
    assert window._write_recovery_snapshot()
    qtbot.waitUntil(lambda: getattr(window, '_recovery_capture', None) is None
                   and window._recovery_worker is None, timeout=15000)


def test_unchanged_revision_does_no_io(main_window, tmp_path, qtbot, monkeypatch):
    window = main_window
    qtbot.waitUntil(lambda: window._recovery_worker is None, timeout=10000)
    monkeypatch.setattr(window, '_store_current_page', lambda: True)
    window.view.filename = str(tmp_path/'source.prism')
    window.view.scene.workspace_pages = [dict(id=str(i), kind='document', title=str(i), content='body')
                                         for i in range(20000)]
    window.view.mark_content_dirty()
    checkpoint(window, qtbot)
    records = window._recovery_store.records()
    assert len(records) == 1
    path = Path(records[0]['_path'])
    before = path.stat().st_mtime_ns
    for _ in range(3):
        assert window._write_recovery_snapshot()
    assert len(window._recovery_store.records()) == 1
    assert path.stat().st_mtime_ns == before


def test_changed_revision_adds_a_checkpoint(main_window, tmp_path, qtbot, monkeypatch):
    window = main_window
    qtbot.waitUntil(lambda: window._recovery_worker is None, timeout=10000)
    monkeypatch.setattr(window, '_store_current_page', lambda: True)
    window.view.filename = str(tmp_path/'source.prism')
    window.view.mark_content_dirty()
    checkpoint(window, qtbot)
    window.view.scene.note_html = 'edited'
    window.view.mark_content_dirty()
    checkpoint(window, qtbot)
    records = window._recovery_store.records()
    assert len(records) == 2
    assert records[0]['payload']['note_html'] == 'edited'


def test_clean_project_does_not_write(main_window, qtbot):
    qtbot.waitUntil(lambda: main_window._recovery_worker is None, timeout=10000)
    main_window.view.undo_stack.setClean()
    main_window.view._content_dirty = False
    assert not main_window._write_recovery_snapshot()
    assert main_window._recovery_store.records() == []
