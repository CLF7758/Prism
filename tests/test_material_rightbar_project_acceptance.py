"""Opt-in native right-sidebar acceptance against a read-only real project."""
import hashlib
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import patch

import pytest
from PyQt6 import QtCore, sip


@pytest.mark.skipif(not os.environ.get('PRISM_RIGHTBAR_SOURCE'),
                    reason='Explicit real-project Windows acceptance only')
def test_real_project_rightbar_all_intensity_sliders(qapp, qtbot, monkeypatch, tmp_path):
    from prism.__main__ import PrismMainWindow
    from prism import view as view_module
    from prism.material_textures import render_signature
    from prism.project_catalog_index import ProjectCatalogIndex

    assert qapp.platformName() == 'windows'
    source = Path(os.environ['PRISM_RIGHTBAR_SOURCE']).resolve(strict=True)
    output = Path(os.environ['PRISM_RIGHTBAR_OUTPUT'])
    output.mkdir(parents=True, exist_ok=True)
    before = source.stat()
    original_build = ProjectCatalogIndex.build
    release = threading.Event()

    def hold_catalog(index, *args, **kwargs):
        if not release.wait(30):
            raise RuntimeError('Acceptance catalog hold expired')
        return original_build(index, *args, **kwargs)

    monkeypatch.setenv('PRISM_PAGED_OPEN', '1')
    monkeypatch.setattr(view_module, '_catalog_cache_path',
                        lambda filename: str(tmp_path / 'index.sqlite'))
    monkeypatch.setattr(ProjectCatalogIndex, 'build', hold_catalog)
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
            patch('prism.capture_hotkey.CaptureHotkey'), \
            patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'):
        window = PrismMainWindow(qapp)
    view, page = window.view, window.material_library
    result = dict(source=str(source), readonly=True, acceptance_passed=False,
                  platform=qapp.platformName(), scale=os.environ.get('QT_SCALE_FACTOR', '1'))
    samples = []
    timer = QtCore.QTimer()
    timer.setInterval(5)
    try:
        window.resize(1500, 960)
        window.show()
        view.open_from_file(str(source), readonly=True)
        qtbot.waitUntil(lambda: view._opening_operation.terminal, timeout=20000)
        assert view._opening_operation.phase == 'ready'
        # Directory construction is unrelated to this acceptance; stop the
        # held task before starting material work, leaving material IO real.
        view._cancel_catalog_builds()
        release.set()
        qtbot.waitUntil(lambda: view._catalog_build_active is None, timeout=10000)
        window.open_material_library()
        qtbot.waitUntil(lambda: window.tabs.currentWidget() is page, timeout=15000)
        candidates = [record for record in view.scene.material_records
                      if not record.get('deletedAt') and record.get('textures', {}).get('roughness')]
        assert candidates, 'The real project has no roughness-bound material'
        complete = []
        excluded = []
        for candidate in candidates:
            try:
                valid = all(view.scene.attachments[entry['attachmentId']][2]
                            for entry in candidate['textures'].values())
            except (KeyError, TypeError):
                valid = False
            if valid:
                complete.append(candidate)
                break
            excluded.append(candidate['id'])
        result['excluded_materials_with_missing_originals'] = excluded
        assert complete, 'No complete original texture set in the real project'
        page._selection_changed(complete[0]['id'])
        details = page.details

        def rendered():
            record = page.records.get(page.selected_id)
            return (page._preview_state == 'ready'
                    and record['preview'].get('signature') == render_signature(record)
                    and not record['preview'].get('stale', True))

        qtbot.waitUntil(rendered, timeout=30000)
        qtbot.waitUntil(lambda: not page._jobs, timeout=10000)
        original_record = page.records.get(page.selected_id)
        originals = {key: hashlib.sha256(view.scene.attachments[entry['attachmentId']][2]).hexdigest()
                     for key, entry in original_record['textures'].items()}
        for slot in details.slots.values():
            slot.fold.setChecked(True)
            if not slot.slider.isEnabled():
                slot.enabled.setChecked(True)
        qtbot.wait(100)

        def sample():
            point = details.sections.mapTo(details, QtCore.QPoint())
            samples.append((point.x(), point.y(), details.sections.width()))

        sample()
        baseline = samples[0]
        timer.timeout.connect(sample)
        timer.start()
        adjusted = []
        for key, slot in details.slots.items():
            assert slot.slider.isEnabled(), key
            slot.slider.sliderPressed.emit()
            for value in (800, 150, 650, 375):
                slot.slider.setValue(value)
                qtbot.wait(40)
            slot.slider.sliderReleased.emit()
            adjusted.append(key)
        qtbot.waitUntil(rendered, timeout=30000)
        qtbot.waitUntil(lambda: not page._jobs, timeout=10000)
        qtbot.wait(150)
        timer.stop()
        record = page.records.get(page.selected_id)
        assert len(samples) >= 20
        assert all(position == baseline for position in samples), set(samples)
        for key in adjusted:
            assert details.slots[key].slider.value() == 375
        assert originals == {key: hashlib.sha256(view.scene.attachments[entry['attachmentId']][2]).hexdigest()
                             for key, entry in record['textures'].items()}
        result.update(acceptance_passed=True, material_id=record['id'],
                      materials_in_project=len(view.scene.material_records),
                      adjusted_slots=adjusted, final_slider_value=375,
                      sample_count=len(samples), unique_positions=sorted(set(samples)),
                      sidebar_displacement_logical_pixels=0,
                      native_preview_state=page._preview_state,
                      final_signature=record['preview']['signature'],
                      source_texture_sha256=originals, texture_bytes_unchanged=True)
        details.grab().save(str(output / 'real-project-rightbar.png'))
    finally:
        timer.stop()
        operation = getattr(view, '_opening_operation', None)
        result['opening_phase'] = getattr(operation, 'phase', None)
        result['opening_terminal'] = getattr(operation, 'terminal', None)
        result['opening_error'] = str(getattr(operation, 'error', None))
        result['installed_scene_rows'] = view.scene._item_count
        result['preparation_failure'] = getattr(view, '_paged_open_failed_reason', None)
        window.grab().save(str(output / 'whole-window.png'))
        view._cancel_catalog_builds()
        release.set()
        qtbot.waitUntil(lambda: getattr(view, '_catalog_build_active', None) is None,
                        timeout=10000)
        result['sampled_positions'] = samples
        after = source.stat()
        result['source_stat_unchanged'] = ((before.st_size, before.st_mtime_ns)
                                         == (after.st_size, after.st_mtime_ns))
        result['source_check'] = 'size_and_mtime_only_not_full_hash'
        (output / 'real-project-rightbar.json').write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        page._auto_timer.stop()
        page._invalidate_inline_preview()
        if not sip.isdeleted(view.undo_stack):
            view.undo_stack.setClean()
        view._content_dirty = False
        window.browser_capture.stop()
        window.close()
        window.deleteLater()
        qapp.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)
    assert result['source_stat_unchanged']
