"""D9+10: real 100000-row browser, sampled Qt paints, NOT full traversal/display proof.

Run this file with the project venv Python to launch an isolated offscreen pytest
subprocess. Only generated synthetic thumbnails/source and workspace outputs are
used. Native renderers/installers are not replaced; observation hooks call through.
"""
import base64
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import uuid

WORK = Path(r'C:\Users\admin\.qoderwork\workspace\mvknb4088p9yzu4a')


def run_offscreen():
    """Launch before importing app code; all app imports happen under pytest."""
    root = Path(__file__).resolve().parents[1]
    assert (root / '.venv/Scripts/python.exe').is_file()
    output_parent = WORK / 'outputs'
    assert output_parent.is_dir()
    output = output_parent / ('d-large-browser-' + time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    output.mkdir()
    env = dict(os.environ, QT_QPA_PLATFORM='offscreen',
               QTWEBENGINE_CHROMIUM_FLAGS='--no-sandbox',
               PYTHONDONTWRITEBYTECODE='1', PRISM_PRESENTATION_DIAGNOSTIC='1',
               PRISM_PRESENTATION_ID_BUDGET='150000', PRISM_D_LARGE_OUTPUT_DIR=str(output))
    command = [str(root / '.venv/Scripts/python.exe'), '-B', '-m', 'pytest',
               str(Path(__file__).resolve()), '-q', '-s', '--tb=short', '-p', 'no:cacheprovider',
               '--basetemp=' + str(output / 'pytest-temp'),
               '--junitxml=' + str(output / 'result.xml')]
    metadata = dict(command=command, cwd=str(root), qt_platform='offscreen',
                    environment_passed_via_subprocess=True, timeout_seconds=480,
                    output_dir=str(output))
    try:
        result = subprocess.run(command, cwd=str(root), env=env, capture_output=True, timeout=480)
        log = result.stdout + result.stderr
        metadata['returncode'] = result.returncode
    except subprocess.TimeoutExpired as error:
        log = (error.stdout or b'') + (error.stderr or b'')
        metadata.update(returncode=124, timeout=True)
    (output / 'run.log').write_bytes(log)
    (output / 'run.json').write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding='utf-8')
    sys.stdout.buffer.write(log)
    print('\nD_LARGE_OUTPUT ' + str(output))
    return metadata['returncode']


if __name__ == '__main__':
    raise SystemExit(run_offscreen())

import pytest
from PyQt6 import QtCore, QtGui

from prism import asset_paint_evidence as proof
from prism.presentation_evidence import (QueryEvidence, add_presentation_observer,
                                         remove_presentation_observer)
from tests.test_catalog_browser_partial import prepare, wait_complete

COUNT = 100000
COLOURS = ('#ed2020', '#20d040', '#2040ed')
SAMPLES = (('front', 0), ('middle', COUNT // 2), ('tail', COUNT - 1))
TEST_BUDGET_SECONDS = 420


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def colour_band(row):
    return min(2, row * 3 // COUNT)


def thumbnail(colour):
    image = QtGui.QImage(32, 32, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor(colour))
    # A contrasting corner also makes this actual image content, not UI text.
    painter = QtGui.QPainter(image)
    painter.fillRect(0, 0, 4, 4, QtGui.QColor('#ffe020'))
    painter.end()
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, 'PNG')
    return [32, 32, base64.b64encode(bytes(buffer.data())).decode('ascii')]


@pytest.fixture
def large_browser(main_window, tmp_path, monkeypatch):
    """Independent real SQLite catalog, with actual saved thumbs on all rows."""
    monkeypatch.setenv('PRISM_PRESENTATION_DIAGNOSTIC', '1')
    monkeypatch.setenv('PRISM_PRESENTATION_ID_BUDGET', '150000')
    thumbs = tuple(thumbnail(colour) for colour in COLOURS)
    scope, scene, browser = prepare(main_window, tmp_path,
        ((row, 'pixmap', dict(canvasId='A' if row < COUNT // 2 else 'B',
                             title=f'D-large-{row:06d}', imageWidth=32, imageHeight=32,
                             displayThumbnail=thumbs[colour_band(row)], opacity=1.0,
                             tags=[])) for row in range(COUNT)))
    # These are tripwires, not paint mocks. Display summaries and saved-thumb
    # decoding remain real, while any original BLOB/full-detail access fails.
    reads = dict(original_blob=0, full_details=0)

    def forbidden_original(*args, **kwargs):
        reads['original_blob'] += 1
        raise AssertionError('Original BLOB read is forbidden for this thumb fixture')

    def forbidden_details(*args, **kwargs):
        reads['full_details'] += 1
        raise AssertionError('Full detail read is forbidden for this catalog fixture')

    monkeypatch.setattr('prism.source_store.read_archive', forbidden_original)
    monkeypatch.setattr(scope.index, 'details', forbidden_details)
    main_window.resize(1000, 700)
    main_window.show()
    yield scope, scene, browser, reads
    browser.list._tiles.reset()
    browser.list._composites.reset()
    browser.list._display_timer.stop()


def test_real_100000_browser_sampled_direct_tile_and_composite(
        large_browser, qtbot, monkeypatch):
    scope, scene, browser, source_reads = large_browser
    host = browser.list
    output = Path(os.environ.get('PRISM_D_LARGE_OUTPUT_DIR', str(WORK / 'outputs')))
    assert output.is_dir()
    stem = 'D-large-browser-' + uuid.uuid4().hex[:12]
    facts_path = output / (stem + '-facts.json')
    trace_path = output / (stem + '-events.jsonl')
    started = time.monotonic()
    deadline = started + TEST_BUDGET_SECONDS
    source_before = sha256(scope.project_path)
    failures, captures = [], []
    latest = {}
    native_frames = OrderedDict()
    lock = threading.RLock()
    phase = dict(sample='installation', branch=None)
    event_counts = {}
    trace = trace_path.open('x', encoding='utf-8')
    observer_errors = []
    facts = dict(scope='sampled_viewports_only', model_rows_expected=COUNT,
                 all_100000_unique_painted_claim=False, physical_display_confirmed=False,
                 capture_boundary='QWidget.grab completed; matching paint/visibility frame',
                 qt_platform=QtGui.QGuiApplication.platformName(),
                 fixture_source=str(scope.project_path), source_sha256_before=source_before,
                 budgets=dict(test_seconds=TEST_BUDGET_SECONDS, query_id_budget=150000,
                              maximum_viewport_candidates=1000, retained_native_frames=24,
                              tile_bytes=host._tiles.BUDGET,
                              composite_bytes=host._composites.BUDGET),
                 samples=captures, failures=failures, events_path=str(trace_path))

    def observe(kind, **fields):
        try:
            with lock:
                event_counts[kind] = event_counts.get(kind, 0) + 1
                latest[kind, fields['query_id'], fields.get('stage')] = fields
                if kind in ('asset_resource_generated', 'asset_cache_installed'):
                    if fields.get('viewport_drawn') != 0:
                        failures.append(kind + ': offscreen event added viewport coverage')
                    if (fields.get('resource_query_id') != fields['query_id'] or
                            fields.get('resource_source_revision') != fields['source_revision']):
                        failures.append(kind + ': generated/installed resource identity mismatch')
                record = dict(fields, event=kind, monotonic=time.monotonic(), **phase)
                # Full bounded diagnostic regions are retained for capture frames
                # in facts, not redundantly serialized on every timer repaint.
                record.pop('diagnostic_regions', None)
                record.pop('diagnostic_ids', None)
                trace.write(json.dumps(record, ensure_ascii=False) + '\n')
        except Exception as error:
            observer_errors.append(repr(error))

    def native_frame(sink):
        if not isinstance(sink, proof.Frame):
            return None  # Offscreen generation is NEVER viewport observation.
        frame = native_frames.setdefault(sink.frame_id, dict(
            direct_draws=0, cache_draws=0, native_cache_categories={},
            direct_rows=[], direct_thumb_rgb={}, cache_resources=[], overflow=False))
        while len(native_frames) > 24:
            native_frames.popitem(last=False)
        return frame

    original_pixmap = proof.observe_pixmap
    original_image = proof.observe_image

    def observe_actual_pixmap(owner, painter, item, target, pm, source, pixels,
                              actual_source_revision=None):
        # The production LOD hook runs AFTER its real painter.drawPixmap.
        original_pixmap(owner, painter, item, target, pm, source, pixels,
                        actual_source_revision)
        if owner is not host:
            return
        sink = getattr(owner, '_asset_paint_sink', None)
        frame = native_frame(sink)
        if frame is None:
            return
        path, _ = proof._geometry(painter, target, sink)
        if path is None or path.isEmpty() or painter.opacity() <= 0:
            return
        row = getattr(owner, '_asset_paint_row', None)
        frame['direct_draws'] += 1
        if len(frame['direct_rows']) >= 1000:
            frame['overflow'] = True
            return
        frame['direct_rows'].append(row)
        if row in {value for _, value in SAMPLES}:
            frame['direct_thumb_rgb'][str(row)] = list(pm.toImage().pixelColor(
                pm.width() // 2, pm.height() // 2).getRgb()[:3])

    def observe_actual_image(owner, painter, target, image, source=None, **kwargs):
        # CanvasTiles/CanvasComposites call this AFTER real painter.drawImage.
        original_image(owner, painter, target, image, source, **kwargs)
        if owner is not host:
            return
        sink = getattr(owner, '_asset_paint_sink', None)
        frame = native_frame(sink)
        if frame is None:
            return
        path, _ = proof._geometry(painter, target, sink)
        if path is None or path.isEmpty() or painter.opacity() <= 0:
            return
        category = kwargs.get('category', 'tile')
        frame['cache_draws'] += 1
        counts = frame['native_cache_categories']
        counts[category] = counts.get(category, 0) + 1
        resource = getattr(image, '_asset_paint_resource', None)
        if len(frame['cache_resources']) >= 48:
            frame['overflow'] = True
            return
        frame['cache_resources'].append(dict(
            native_category=category, painter_active=painter.isActive(),
            resource_id=resource.resource_id if resource else None,
            resource_kind=resource.kind if resource else None,
            resource_query_id=resource.identity.get('query_id') if resource else None,
            resource_source_revision=resource.identity.get('source_revision') if resource else None,
            inherited_categories=sorted({entry.get('category') for _, entry in
                                          resource.entries}) if resource else [],
            resource_complete=resource.complete if resource else None,
            resource_incomplete=resource.incomplete if resource else None,
            resource_manifest_entries=len(resource.entries) if resource else 0,
            # Resource membership is NOT a viewport row count. Screenshot and
            # clipped production observations separately test the anchor.
            resource_anchor_rows=sorted({entry.get('row') for _, entry in resource.entries
                if entry.get('kind') == 'content' and entry.get('row') in
                {anchor for _, anchor in SAMPLES}}) if resource else [],
            opacity=painter.opacity(),
            image_size=[image.width(), image.height()], target=proof.rect_values(target)))

    monkeypatch.setattr(proof, 'observe_pixmap', observe_actual_pixmap)
    monkeypatch.setattr(proof, 'observe_image', observe_actual_image)

    def remaining_ms(limit):
        remaining = deadline - time.monotonic()
        assert remaining > 0, 'Overall sampled-paint test budget exhausted'
        return max(1, min(limit, int(remaining * 1000)))

    def event(kind, evidence, stage=None):
        with lock:
            return latest.get((kind, evidence.query_id, stage))

    def check(condition, message):
        if not condition:
            failures.append(message)

    def navigate(row, scale):
        # Normal public camera navigation; no card moves, model substitutions,
        # direct scene.render calls or installation shortcuts.
        host.setTransform(QtGui.QTransform.fromScale(scale, scale))
        host.centerOn(host.cards[row].sceneBoundingRect().center())
        host.viewport().update()

    def capture(label, row, branch, evidence):
        expected_category = dict(direct='direct', tile='tile', composite='group')[branch]
        phase.update(sample=label, branch=branch)
        composite_threshold = 1 if branch == 'tile' else 40
        monkeypatch.setattr(host._composites, 'DETAIL_PIXELS', composite_threshold)
        scale = dict(direct=1.0, tile=.16, composite=.23)[branch]
        navigate(row, scale)
        if branch == 'tile':
            # Pure tile needs the production >=128-visible-card branch.
            for _ in range(5):
                camera = host.mapToScene(host.viewport().rect()).boundingRect()
                if len(host.visible_cards(camera)) >= 128:
                    break
                scale *= .85
                navigate(row, scale)
        camera = host.mapToScene(host.viewport().rect()).boundingRect()
        candidates = host.visible_cards(camera)
        check(len(candidates) <= 1000, f'{label}/{branch}: viewport budget exceeded')
        if len(candidates) > 1000:
            raise AssertionError(f'{label}/{branch}: unbounded sampling camera')
        if branch == 'tile':
            check(len(candidates) >= 128, f'{label}/tile: production tile branch not entered')
        check(row in {card.row for card in candidates}, f'{label}/{branch}: anchor outside camera')

        def anchor_observed():
            value = event('asset_visibility_snapshot', evidence)
            if value is None or value['viewport_transform'] != proof.transform_values(host.viewportTransform()):
                return False
            # Wait for actual operations, NOT quality success. Provenance is
            # asserted below, so deterministic missing revision stamps do not
            # spend 45 seconds per viewport pretending they are still loading.
            if any(entry.get('kind') == 'content' and entry.get('row') == row
                   and entry.get('category') == expected_category
                   and entry.get('opacity', 0) >= 1.0 - 1e-6
                   and (branch == 'direct' or entry.get('resource_complete'))
                   for entry in value.get('diagnostic_regions', [])):
                return True
            if branch == 'direct':
                return False
            native = native_frames.get(value['frame_id'], {})
            return any(row in resource.get('resource_anchor_rows', [])
                       and resource.get('resource_complete')
                       and resource.get('opacity', 0) >= 1.0 - 1e-6
                       and resource['native_category'] == 'tile'
                       and (branch != 'composite' or 'group' in resource['inherited_categories'])
                       for resource in native.get('cache_resources', []))

        try:
            qtbot.waitUntil(anchor_observed, timeout=remaining_ms(20000))
        except Exception as error:
            failures.append(f'{label}/{branch}: anchor draw wait failed: {error}')
        # grab synchronously paints the widget. Do not process events between
        # this capture and pairing its frame/snapshot/native observations.
        previous_capture_id = getattr(host, '_asset_paint_capture_id', None)
        image = host.viewport().grab().toImage()
        capture_id = getattr(host, '_asset_paint_capture_id', None)
        frame = event('asset_paint_frame', evidence)
        visibility = event('asset_visibility_snapshot', evidence)
        native = native_frames.get(capture_id, {})
        image_path = output / f'{stem}-{label}-{branch}.png'
        assert image.save(str(image_path), 'PNG')
        colour = QtGui.QColor(COLOURS[colour_band(row)])
        expected = colour.getRgb()[:3]
        matching_pixels = sum(all(abs(component - wanted) <= 30
                                  for component, wanted in zip(image.pixelColor(x, y).getRgb()[:3], expected))
                              for y in range(0, image.height(), 3)
                              for x in range(0, image.width(), 3))
        anchor = host.mapFromScene(host.cards[row].sceneBoundingRect().center())
        dpr = host.viewport().devicePixelRatioF()
        ax, ay = round(anchor.x() * dpr), round(anchor.y() * dpr)
        anchor_rgb = list(image.pixelColor(ax, ay).getRgb()[:3]) if (
            0 <= ax < image.width() and 0 <= ay < image.height()) else None
        sample = dict(sample=label, anchor_row=row, anchor_asset_id=browser.model.items[row].save_id,
                      branch=branch, scale=scale, composite_detail_pixels=composite_threshold,
                      camera=proof.rect_values(camera), camera_api='setTransform + centerOn',
                      viewport_candidates=len(candidates), screenshot=str(image_path),
                      capture_id=capture_id, image_size=[image.width(), image.height()],
                      expected_thumb_rgb=list(expected), matching_sampled_pixels=matching_pixels,
                      anchor_device_pixel=[ax, ay], anchor_rgb=anchor_rgb,
                      paint_frame=frame, visibility_snapshot=visibility, native_draws=native,
                      tile_builds=host._tiles.builds, composite_builds=host._composites.builds,
                      tile_bytes=host._tiles.bytes, composite_bytes=host._composites.bytes)
        captures.append(sample)
        prefix = f'{label}/{branch}'
        check(frame is not None and visibility is not None, prefix + ': missing frame facts')
        if frame is None or visibility is None:
            return
        check(capture_id and capture_id != previous_capture_id,
              prefix + ': grab did not produce a fresh observed paint frame')
        check(capture_id == frame['capture_id'] == frame['frame_id'] == visibility['frame_id'],
              prefix + ': screenshot/frame identity mismatch')
        check(frame['viewport_transform'] == proof.transform_values(host.viewportTransform()) and
              frame['display_confirmed'] is False and visibility['display_confirmed'] is False,
              prefix + ': camera/display-boundary evidence mismatch')
        for value in (frame, visibility):
            check(value['query_id'] == evidence.query_id and
                  value['source_revision'] == evidence.source_revision and
                  value['session_id'] == evidence.session_id and value['model_rows'] == COUNT,
                  prefix + ': query/source/model identity mismatch')
        check(frame['full_viewport_repaint'], prefix + ': capture is not a full paint')
        check(frame['viewport_drawn'] > 0, prefix + ': no observed content draws')
        check(matching_pixels > 10, prefix + ': screenshot lacks real thumb colour')
        check(anchor_rgb is not None and all(abs(a - b) <= 30 for a, b in zip(anchor_rgb, expected)),
              prefix + ': anchor screenshot pixel is not real thumb content')
        regions = [entry for entry in visibility.get('diagnostic_regions', [])
                   if entry.get('kind') == 'content' and entry.get('row') == row
                   and entry.get('category') == expected_category]
        check(bool(regions), prefix + ': anchor lacks real content branch evidence')
        sample['anchor_provenance'] = [dict(actual=entry.get('actual_source_revision'),
            expected=entry.get('expected_item_revision'), current=entry.get('revision_current'),
            category=entry.get('category')) for entry in regions]
        check(all(entry.get('query_id') == evidence.query_id and
                  entry.get('source_revision') == evidence.source_revision and
                  entry.get('revision_current') and entry.get('actual_source_revision') is not None
                  for entry in regions), prefix + ': anchor pixel provenance is invalid: ' +
                  json.dumps(sample['anchor_provenance']))
        check(not native.get('overflow', False), prefix + ': native observation budget overflow')
        if branch == 'direct':
            check(row in native.get('direct_rows', []), prefix + ': no native direct draw')
            check(native.get('direct_thumb_rgb', {}).get(str(row)) == list(expected),
                  prefix + ': native pixmap is not saved thumbnail')
            check(frame.get('cache_draw_calls', 0) == 0, prefix + ': direct branch drew a cache instead')
        else:
            check(native.get('cache_draws', 0) > 0 and frame.get('cache_draw_calls', 0) > 0,
                  prefix + ': no native cache draw into viewport')
            resources = native.get('cache_resources', [])
            check(any(value['native_category'] in ('tile', 'incremental') and
                      value['resource_query_id'] == evidence.query_id and
                      value['resource_source_revision'] == evidence.source_revision and
                      (branch != 'composite' or 'group' in value['inherited_categories'])
                      for value in resources), prefix + ': no current real tile/composite replay')
            check(all(entry.get('resource_query_id') == evidence.query_id and
                      entry.get('resource_source_revision') == evidence.source_revision
                      for entry in regions), prefix + ': cache resource identity mismatch')
            if branch == 'composite':
                # Overview content is not a detail/quality-ready claim.
                check(all(not entry.get('ready') for entry in regions),
                      prefix + ': group composite falsely claims detail readiness')
        print('D_LARGE_SAMPLE ' + json.dumps(dict(sample=label, branch=branch, anchor=row,
              model_rows=frame['model_rows'], viewport_candidates=len(candidates),
              draw_operations=frame['viewport_drawn'], unique_sample_rows=visibility.get('viewport_drawn'),
              categories=frame['categories'], native_cache_draws=native.get('cache_draws', 0),
              unknown=frame['unknown'], anchor_provenance=sample['anchor_provenance'],
              matching_pixels=matching_pixels, capture_id=capture_id)))
        trace.flush()

    add_presentation_observer(observe)
    try:
        assert facts['qt_platform'] == 'offscreen', 'Launch via the explicit-env subprocess runner'
        browser.open('all')
        qtbot.waitUntil(lambda: browser._catalog_pending is None and
                       browser.model.rowCount() == COUNT and
                       browser.model.presentation_evidence is not None,
                       timeout=remaining_ms(240000))
        wait_complete(qtbot, browser, COUNT)
        evidence = browser.model.presentation_evidence
        assert isinstance(evidence, QueryEvidence) and evidence.active
        assert evidence.query_id and evidence.source_revision not in (None, '', 'unknown')
        facts.update(query_id=evidence.query_id, source_revision=evidence.source_revision,
                     session_id=evidence.session_id)
        read = event('catalog_rows_read_evidence', evidence)
        delivery = event('catalog_rows_delivered', evidence, 'consumer_consumed')
        published = event('asset_model_published', evidence)
        facts['installation'] = dict(read=read, delivery=delivery, published=published)
        assert read and delivery and published
        assert read['unique_rows'] == delivery['unique_rows'] == published['model_rows'] == COUNT
        assert delivery['logical_rows'] == COUNT and published['model_unique_ids'] == COUNT
        assert all(value['complete'] and value['query_id'] == evidence.query_id and
                   value['source_revision'] == evidence.source_revision and
                   value['session_id'] == evidence.session_id for value in (read, delivery, published))
        qtbot.waitUntil(lambda: host._build_state is None and len(host.cards) == COUNT
                       and bool(host._spatial) and bool(host._composites.groups),
                       timeout=remaining_ms(90000))
        facts['layout'] = dict(cards=len(host.cards), groups=len(host._composites.groups),
                              content_bounds=proof.rect_values(host._content_bounds))
        for label, row in SAMPLES:
            assert browser.model.items[row].save_id == row, 'Fixture/model sample order changed'
            for branch in ('direct', 'tile', 'composite'):
                capture(label, row, branch, evidence)
        check(len(captures) == 9, 'All nine branch/viewpoint captures are required')
        offscreen_events = [value for (kind, query, stage), value in latest.items()
                            if query == evidence.query_id and kind in
                            ('asset_resource_generated', 'asset_cache_installed')]
        check(event_counts.get('asset_resource_generated', 0) > 0 and
              event_counts.get('asset_cache_installed', 0) > 0, 'No real offscreen resources observed')
        check(all(value['viewport_drawn'] == 0 for value in offscreen_events),
              'Offscreen generation/cache installation was counted as viewport drawing')
        drawn = {row for sample in captures for row in
                 (sample.get('visibility_snapshot') or {}).get('diagnostic_ids', [])}
        facts['unique_rows_in_nine_captures'] = len(drawn)
        check(3 <= len(drawn) < COUNT, 'Sample coverage must not be a 100000-unique-paint claim')
        check(not list(scene.iter_assets()), 'Catalog proxies leaked into source scene')
        check(all(getattr(type(item), '_catalog_proxy', False) for item in browser.model.items),
              'Real catalog proxies were replaced')
        check(source_reads == dict(original_blob=0, full_details=0), 'Original/full-detail access occurred')
        check(not observer_errors, 'Evidence observer failed: ' + repr(observer_errors))
        assert not failures, '\n'.join(failures)
    except BaseException as error:
        facts['exception'] = repr(error)
        raise
    finally:
        remove_presentation_observer(observe)
        trace.flush()
        trace.close()
        facts.update(elapsed_seconds=time.monotonic() - started, event_counts=event_counts,
                     source_reads=source_reads, observer_errors=observer_errors,
                     source_sha256_after=sha256(scope.project_path))
        facts['source_sha256_unchanged'] = facts['source_sha256_before'] == facts['source_sha256_after']
        facts['passed'] = not failures and 'exception' not in facts and facts['source_sha256_unchanged']
        facts_path.write_text(json.dumps(facts, ensure_ascii=False, indent=2), encoding='utf-8')
        print('D_LARGE_FACTS ' + str(facts_path))
        print('D_LARGE_COVERAGE sampled viewports only; NOT 100000 unique paints; NOT physical display')
        assert facts['source_sha256_unchanged'], 'Synthetic source SHA256 changed'
