"""Tier-parameterised real-scale acceptance harness for Prism.

The 100k numbers in ``artifacts/scale-audit/`` were produced by
``tests/_stress_existing_scale.py``: one hard-coded project, one hard-coded
record count, and every report written straight into the existing audit
directory.  Three consequences follow:

* the 100 / 1,000 / 10,000 tiers were never measured,
* a tier other than the 100k fixture cannot be rebuilt from scratch,
* a run is not reproducible without editing a test file.

This script turns that one-off into a tool with two steps::

    python tools/scale_acceptance.py build   --source <project.prism> --tier 1000 --out <dir>
    python tools/scale_acceptance.py measure --project <dir>/project.prism --label tier-1000 --out <dir>
    python tools/scale_acceptance.py run     --source <project.prism> --tier 1000 --out <dir>

``build`` copies the source project into the output directory and rescales it
to the requested record count.  The source is only ever read, and its digest
is compared before and after, so a run cannot damage the user's project.
``measure`` opens the fixture in the real main window and records the five
acceptance metrics of the 2026-10-22 handoff (section 0.7), each next to the
threshold it is judged against.

Two numbers are reported separately on purpose:

* **record scale** -- how many rows the catalog, the scene and the sidebar
  have to carry, and
* **decode load** -- how many distinct original images exist and how many
  bytes they carry.

A tier that reuses the same originals at a higher record count has a larger
record scale and the same decode load.  Reporting one as the other is the
mistake the handoff explicitly calls out, so the fixture manifest keeps both
apart and the report repeats them side by side.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── helpers ──────────────────────────────────────────────────────────────

def sha256(path, chunk=4 * 1024 * 1024):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for block in iter(lambda: handle.read(chunk), b''):
            digest.update(block)
    return digest.hexdigest()


def stats(values):
    """The shape the existing scale-audit reports use."""
    values = sorted(values)
    if not values:
        return {}
    return dict(count=len(values),
                median_ms=round(statistics.median(values), 2),
                p95_ms=round(values[min(len(values) - 1, int(len(values) * .95))], 2),
                max_ms=round(max(values), 2))


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return path


def wait_until(app, predicate, timeout_ms=60000, label=''):
    """Pump the event loop until ``predicate`` holds or the timeout expires."""
    deadline = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except Exception:  # a half-built scene may not answer yet
            pass
        time.sleep(0.005)
    if label:
        print(f'TIMEOUT {label} after {timeout_ms}ms', flush=True)
    return False


def settle(app, milliseconds):
    """Pump the event loop for a fixed span."""
    deadline = time.perf_counter() + milliseconds / 1000
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)


def javascript(grid, expression, app, timeout_ms=20000):
    """Run one expression in the material grid's web view and return the value."""
    result = []
    try:
        grid.web.page().runJavaScript(expression, result.append)
    except Exception:
        return None
    deadline = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < deadline and not result:
        app.processEvents()
        time.sleep(0.01)
    return result[0] if result else None


# ── step 1: build a tier fixture ─────────────────────────────────────────

def build_fixture(source, tier, out_dir, *, images_per_canvas=100, materials=None):
    """Copy ``source`` into ``out_dir`` and rescale it to ``tier`` records.

    The copy keeps the real originals: every record points at a real embedded
    image, so decode cost is real.  When the tier is larger than the number
    of real assets, records repeat the real assets -- the manifest reports the
    repeat factor so the record scale is never confused with the decode load.
    """
    source = Path(source).resolve()
    out_dir = Path(out_dir).resolve()
    if not source.is_file():
        raise SystemExit(f'source project not found: {source}')
    out_dir.mkdir(parents=True, exist_ok=True)
    project = out_dir / 'project.prism'
    if project.exists():
        raise SystemExit(f'refusing to overwrite {project}')

    started = time.perf_counter()
    digest_before = sha256(source)
    shutil.copyfile(source, project)

    db = sqlite3.connect(project)
    try:
        db.execute('PRAGMA journal_mode=OFF')
        db.execute('PRAGMA synchronous=OFF')
        db.execute('PRAGMA cache_size=-262144')

        pixmaps = [row[0] for row in db.execute(
            "SELECT id FROM items WHERE type='pixmap' ORDER BY id")]
        if not pixmaps:
            raise SystemExit('source project has no pixmap items')
        unique = pixmaps[:min(tier, len(pixmaps))]

        surplus = pixmaps[len(unique):]
        if surplus:
            db.executemany('DELETE FROM sqlar WHERE item_id=?', [(i,) for i in surplus])
            db.executemany('DELETE FROM items WHERE id=?', [(i,) for i in surplus])

        next_id = max(row[0] for row in db.execute('SELECT id FROM items')) + 1
        duplicated = 0
        for index in range(len(unique), tier):
            base = unique[index % len(unique)]
            row = db.execute(
                'SELECT type,x,y,z,scale,rotation,flip,data FROM items WHERE id=?',
                (base,)).fetchone()
            db.execute('INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) '
                       'VALUES (?,?,?,?,?,?,?,?,?)', (next_id,) + row)
            db.execute('INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) '
                       'SELECT ?,?,mode,mtime,sz,data FROM sqlar WHERE item_id=?',
                       (f'accept-{next_id}', next_id, base))
            next_id += 1
            duplicated += 1

        # Canvas pages: one page per ``images_per_canvas`` records, so the
        # fixture has the same shape as the existing 100k stress project.
        canvases = max(1, (tier + images_per_canvas - 1) // images_per_canvas)
        project_id = db.execute(
            "SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()
        project_id = project_id[0] if project_id else 'acceptance'
        db.execute("DELETE FROM workspace_pages WHERE kind='canvas'")
        db.execute("DELETE FROM workspace_nodes")
        for index in range(canvases):
            page_id = f'accept-canvas-{index:04d}'
            title = f'验收画布 {index + 1:04d} · {images_per_canvas}图'
            payload = json.dumps({'__prism_page_payload__': True, 'content': None,
                                  'tags': [], 'parent': None, 'permanent': False},
                                 ensure_ascii=False)
            db.execute('INSERT INTO workspace_pages VALUES (?,?,?,?,?)',
                       (page_id, 'canvas', title, payload, index))
            node = dict(id=f'node-{page_id}', projectId=project_id, section='canvas',
                        nodeType='page', parentId=None, title=title,
                        order=(index + 1) * 1000, pageId=page_id, color=None,
                        deletedAt=None)
            db.execute('INSERT INTO workspace_nodes VALUES (?,?)',
                       (node['id'], json.dumps(node, ensure_ascii=False)))

        # Every record belongs to one canvas page.
        page_ids = [f'accept-canvas-{index:04d}' for index in range(canvases)]
        rows = db.execute('SELECT id FROM items WHERE type="pixmap" ORDER BY id').fetchall()
        for index, (item_id,) in enumerate(rows):
            page = page_ids[min(index // images_per_canvas, canvases - 1)]
            db.execute("UPDATE items SET data=json_set(data,'$.canvasId',?) WHERE id=?",
                       (page, item_id))

        if materials is not None:
            available = db.execute('SELECT count(*) FROM material_records').fetchone()[0]
            keep = min(materials, available)
            ids = [row[0] for row in db.execute(
                'SELECT id FROM material_records ORDER BY id')]
            db.executemany('DELETE FROM material_records WHERE id=?',
                           [(i,) for i in ids[keep:]])

        db.commit()
        db.execute('VACUUM')
        check = db.execute('PRAGMA quick_check').fetchone()[0]

        counts = {kind: count for kind, count in db.execute(
            'SELECT type, count(*) FROM items GROUP BY type')}
        material_count = db.execute('SELECT count(*) FROM material_records').fetchone()[0]
        record_bytes = db.execute('SELECT sum(length(data)) FROM sqlar').fetchone()[0] or 0
        unique_bytes = db.execute(
            'SELECT sum(length(data)) FROM sqlar WHERE item_id IN '
            f'({",".join(str(i) for i in unique)})').fetchone()[0] or 0
        canvas_counts = {kind: count for kind, count in db.execute(
            'SELECT kind, count(*) FROM workspace_pages GROUP BY kind')}
    finally:
        db.close()

    digest_after = sha256(source)
    manifest = {
        'source': str(source),
        'source_sha256_before': digest_before,
        'source_sha256_after': digest_after,
        'source_unchanged': digest_before == digest_after,
        'project': str(project),
        'project_bytes': project.stat().st_size,
        'tier': tier,
        'record_scale': {
            'items': counts,
            'pixmaps': counts.get('pixmap', 0),
            'canvases': canvas_counts.get('canvas', 0),
            'images_per_canvas': images_per_canvas,
            'materials': material_count,
            'record_bytes': record_bytes,
        },
        'decode_load': {
            'unique_real_assets': len(unique),
            'unique_real_bytes': unique_bytes,
            'duplicated_records': duplicated,
            'repeat_factor': round(tier / max(1, len(unique)), 3),
            'note': 'record scale and decode load are different numbers; '
                    'a repeated record adds a row, not a new image to decode',
        },
        'quick_check': check,
        'build_seconds': round(time.perf_counter() - started, 2),
    }
    write_json(out_dir / 'fixture.json', manifest)
    return manifest


# ── step 2: measure the acceptance metrics ───────────────────────────────

def _card_detail(card, item, scale, dpr=1.0):
    """Is one card already carrying the detail its on-screen size needs?

    The criterion is the one the existing scale audit uses: a card counts as
    sharp when a resident pixmap covers the pixels it occupies on screen.
    """
    from prism import lod
    original = getattr(item, '_image_size', None)
    if original is None:
        # Videos and text items have no source pixels to become sharp.
        return None, 0
    crop = getattr(item, 'crop', None)
    if crop is None:
        return None, 0
    pixels = max(card._size.width(), card._size.height()) * scale * dpr
    pixels *= max(original.width(), original.height()) / max(crop.width(), crop.height(), 1)
    needed = min(lod.level_for_pixels(pixels), max(original.width(), original.height()))
    candidates = [getattr(item, '_preview_pixmap', None)]
    detailed = any(pix is not None and not pix.isNull()
                    and max(pix.width(), pix.height()) >= needed for pix in candidates)
    if not detailed:
        candidates.extend(lod.cache().get(item, level, touch=False)
                          for level in lod.LEVELS if level >= needed)
        detailed = any(pix is not None and not pix.isNull()
                        and max(pix.width(), pix.height()) >= needed for pix in candidates)
    resident = max((max(pix.width(), pix.height())
                    for pix in candidates if pix is not None and not pix.isNull()),
                   default=0)
    return detailed, resident


def _detail_state(canvas, browser, scale):
    """How many cards in the viewport already carry source-derived detail."""
    viewport = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    visible = canvas.visible_cards(viewport)
    dpr = canvas.viewport().devicePixelRatioF()
    missing = []
    for card in visible:
        item = browser.model.items[card.row]
        detailed, _resident = _card_detail(card, item, scale, dpr)
        if detailed is None:
            continue
        if not detailed:
            missing.append(card.row)
    return (not missing), len(visible), missing


def measure(project, label, out_dir, *, open_timeout_ms=900000, settle_ms=1500):
    """Open the fixture in the real main window and record the metrics."""
    from unittest.mock import patch
    from PyQt6 import QtCore, QtGui, QtWidgets
    from prism.__main__ import PrismApplication, PrismMainWindow
    from prism import lod, performance_trace
    from prism.resource_budget import shared_cache_budget

    project = Path(project).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    app = PrismApplication([sys.argv[0] or 'prism'])
    report = {
        'project': str(project),
        'label': label,
        'platform': app.platformName(),
        'stages': {},
        'metrics': {},
        'completed': False,
    }
    report_path = out_dir / f'{label}.json'

    def persist():
        write_json(report_path, report)

    # The capture adapters bind the user's port and watch the real clipboard;
    # an acceptance run must not touch either.  Modal dialogs are answered
    # automatically so a warning can never wedge the run.
    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'), \
         patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes), \
         patch('PyQt6.QtWidgets.QMessageBox.warning',
               return_value=QtWidgets.QMessageBox.StandardButton.Ok):
        window = PrismMainWindow(app)
        window.resize(1580, 1000)
        window.show()
        view = window.view
        app.processEvents()

        # A 16 ms heartbeat measures how long the GUI thread was blocked.
        pulses = []
        previous = [time.perf_counter()]
        timer = QtCore.QTimer()
        timer.setInterval(16)

        def tick():
            now = time.perf_counter()
            pulses.append((now - previous[0]) * 1000)
            previous[0] = now
        timer.timeout.connect(tick)
        timer.start()

        trace = performance_trace.install(app, window, out_dir / 'trace')
        interactive_from = 0

        def stage(name, call):
            report['current_stage'] = name
            persist()
            print('STAGE', name, flush=True)
            started = time.perf_counter()
            result = call()
            report['stages'][name] = round((time.perf_counter() - started) * 1000, 2)
            persist()
            return result

        try:
            open_started = time.perf_counter()
            stage('open_request', lambda: view.open_from_file(str(project)))
            ready = wait_until(
                app,
                lambda: view.filename == str(project)
                and not view.worker.isRunning()
                and view.scene.items_to_add.empty()
                and not getattr(view, '_project_opening', False),
                open_timeout_ms, 'open_until_ready')
            # "Ready" means: the file name is set, the reader thread is done
            # and the scene queue is empty -- not merely that the call
            # returned.
            report['stages']['open_until_ready'] = round(
                (time.perf_counter() - open_started) * 1000, 2)
            report['opened'] = bool(ready)
            # The load itself is allowed to block; the interaction metrics are
            # not.  Split the heartbeat so a slow open is not reported as a
            # slow interface.
            report['metrics']['heartbeat_open'] = stats(pulses)
            interactive_from = len(pulses)
            if not ready:
                report['error'] = 'project did not become ready'
                persist()
                return report

            browser = window.asset_browser
            stage('all_assets_open', lambda: browser.open('all'))
            wait_until(app, lambda: browser.model.rowCount() > 0, 120000, 'all_assets')
            canvas = browser.list
            rows = browser.model.rowCount()
            report['counts'] = {'catalog_rows': rows}
            persist()

            stage('all_assets_fit', lambda: canvas.fitInView(
                canvas._content_bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
            canvas.viewport().repaint()
            app.processEvents()
            time.sleep(settle_ms / 1000)
            app.processEvents()

            fit_scale = abs(canvas.transform().m11())

            # ── GUI synchronous work ──────────────────────────────
            pans = []
            for _ in range(20):
                started = time.perf_counter()
                canvas.translate(-15, 8)
                canvas.viewport().repaint()
                app.processEvents()
                pans.append((time.perf_counter() - started) * 1000)
            report['metrics']['pan'] = stats(pans)

            zooms = []
            for factor in [1.2] * 12 + [1 / 1.2] * 12:
                started = time.perf_counter()
                canvas.scale(factor, factor)
                canvas.viewport().repaint()
                app.processEvents()
                zooms.append((time.perf_counter() - started) * 1000)
            report['metrics']['zoom'] = stats(zooms)

            wheels = []
            for delta in [360] * 8 + [-360] * 8:
                point = canvas.viewport().rect().center()
                event = QtGui.QWheelEvent(
                    QtCore.QPointF(point), QtCore.QPointF(canvas.viewport().mapToGlobal(point)),
                    QtCore.QPoint(), QtCore.QPoint(0, delta),
                    QtCore.Qt.MouseButton.NoButton, QtCore.Qt.KeyboardModifier.NoModifier,
                    QtCore.Qt.ScrollPhase.NoScrollPhase, False)
                started = time.perf_counter()
                QtWidgets.QApplication.sendEvent(canvas.viewport(), event)
                canvas.viewport().grab()
                app.processEvents()
                wheels.append((time.perf_counter() - started) * 1000)
            report['metrics']['wheel'] = stats(wheels)

            # ── whole-viewport clarity ───────────────────────────
            canvas.fitInView(canvas._content_bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            viewport_scale = abs(canvas.transform().m11())
            viewport_started = time.perf_counter()
            complete = False
            visible = 0
            missing = []
            while time.perf_counter() - viewport_started < 30:
                app.processEvents()
                complete, visible, missing = _detail_state(canvas, browser, viewport_scale)
                if complete:
                    break
                time.sleep(0.01)
            report['metrics']['viewport_clarity'] = {
                'scale': round(viewport_scale, 6),
                'visible_cards': visible,
                'unsharp_cards': len(missing),
                'ms': round((time.perf_counter() - viewport_started) * 1000, 2),
                'complete': bool(complete),
            }

            # ── focal sharpness ──────────────────────────────────
            # Near (3x) and mid (0.5x) distance, judged on the focused card
            # itself -- not on every card that happens to be on screen.
            focal = []
            dpr = canvas.viewport().devicePixelRatioF()
            for scale, distance in ((3, 'near'), (.5, 'mid')):
                for row in (0, max(0, rows // 2), max(0, rows - 1)):
                    card = canvas.cards[row]
                    item = browser.model.items[row]
                    canvas.setTransform(QtGui.QTransform.fromScale(scale, scale))
                    canvas.centerOn(card.sceneBoundingRect().center())
                    started = time.perf_counter()
                    sharp = False
                    while time.perf_counter() - started < 30:
                        app.processEvents()
                        detailed, _resident = _card_detail(card, item, scale, dpr)
                        if detailed:
                            sharp = True
                            break
                        time.sleep(0.01)
                    focal.append({'scale': scale, 'distance': distance, 'row': row,
                                  'sharp': sharp,
                                  'ms': round((time.perf_counter() - started) * 1000, 2)})
            report['metrics']['focal_clarity'] = focal

            # ── re-entering an unchanged scope must not rebuild ──
            tiles = getattr(canvas, '_tiles', None)
            composites = getattr(canvas, '_composites', None)
            arrange_calls = [0]
            original_arrange = canvas.arrange

            def counted_arrange(*args, **kwargs):
                arrange_calls[0] += 1
                return original_arrange(*args, **kwargs)
            canvas.arrange = counted_arrange
            before = {
                'tile_builds': tiles.builds if tiles is not None else None,
                'composite_builds': composites.builds if composites is not None else None,
                'arrange_calls': arrange_calls[0],
            }
            try:
                stage('reenter_unchanged_scope', lambda: browser.open('all'))
                wait_until(app, lambda: browser.model.rowCount() > 0, 120000, 'reenter')
                time.sleep(settle_ms / 1000)
                app.processEvents()
            finally:
                canvas.arrange = original_arrange
            after = {
                'tile_builds': tiles.builds if tiles is not None else None,
                'composite_builds': composites.builds if composites is not None else None,
                'arrange_calls': arrange_calls[0],
            }
            rebuild = {}
            for key in before:
                if before[key] is None or after[key] is None:
                    rebuild[key] = None
                else:
                    rebuild[key] = after[key] - before[key]
            report['metrics']['reenter_unchanged_scope'] = {
                'before': before, 'after': after, 'delta': rebuild}

            # ── byte budget and working set stability ────────────
            budget = shared_cache_budget()
            rounds = []
            for index in range(3):
                canvas.fitInView(canvas._content_bounds,
                                 QtCore.Qt.AspectRatioMode.KeepAspectRatio)
                canvas.viewport().repaint()
                app.processEvents()
                time.sleep(0.3)
                app.processEvents()
                snapshot = budget.snapshot()
                rounds.append({
                    'round': index,
                    'rss_bytes': performance_trace.process_memory_bytes(),
                    'budget_bytes': snapshot['bytes'],
                    'over_budget': snapshot['over_budget'],
                    'categories': snapshot['categories'],
                    'image_cache_bytes': lod.cache().bytes,
                })
            report['metrics']['working_set'] = rounds
            report['metrics']['budget'] = {
                'limit': rounds[-1].get('categories') and budget.snapshot()['limit'],
                'final': budget.snapshot(),
            }
            report['metrics']['heartbeat'] = stats(pulses[interactive_from:])
            report['memory'] = {
                'rss_bytes_first_round': rounds[0]['rss_bytes'],
                'rss_bytes_last_round': rounds[-1]['rss_bytes'],
                'rss_growth_bytes': (rounds[-1]['rss_bytes'] or 0) - (rounds[0]['rss_bytes'] or 0),
            }

            # ── material library: no regression at this scale ───
            # The material panel sits on the same catalog and byte budget the
            # performance work changed, so it is checked last: after the
            # memory rounds, so the working-set numbers above describe the
            # asset side alone.
            material = {'checked': False}
            manifest_path = out_dir / 'fixture.json'
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
                material['expected_records'] = \
                    manifest.get('record_scale', {}).get('materials')
            if hasattr(window, 'open_material_library'):
                stage('material_library_open', window.open_material_library)
                page = window.material_library
                ready = wait_until(
                    app, lambda: getattr(page.materials, 'ready', False),
                    60000, 'material_ready')
                material['checked'] = True
                material['ready'] = bool(ready)
                material['service_records'] = len(page.records.records)
                if ready:
                    material['dom_cards'] = javascript(
                        page.materials,
                        "document.querySelectorAll('.asset-card').length", app)
                    material['dom_card_images'] = javascript(
                        page.materials,
                        "document.querySelectorAll('.card-image img').length", app)
                    material['grid_count'] = page.materials.count() \
                        if hasattr(page.materials, 'count') else None
                    before = material.get('dom_cards')
                    try:
                        page.search.setText('a')
                        settle(app, 400)
                        material['filtered_cards'] = javascript(
                            page.materials,
                            "document.querySelectorAll('.asset-card').length", app)
                        page.search.setText('')
                        settle(app, 400)
                    except Exception as error:
                        material['filter_error'] = str(error)
                    if isinstance(before, int) and isinstance(material.get('filtered_cards'), int):
                        material['filter_narrows'] = material['filtered_cards'] <= before
            report['metrics']['material'] = material
            report['completed'] = True
        finally:
            timer.stop()
            try:
                trace.close()
            except Exception:
                pass
            persist()
            # Shut the window down the way the test suite does: the document
            # and mind map panels each own a QWebEnginePage, and leaving them
            # alive makes Qt exit with an access violation (0xC0000005).
            try:
                window.view.undo_stack.setClean()
                window.browser_capture.stop()
                window.hide()
            except Exception:
                pass
            for _ in range(3):
                QtCore.QCoreApplication.sendPostedEvents(
                    None, QtCore.QEvent.Type.DeferredDelete)
                app.processEvents()
    return report


# ── report rendering ─────────────────────────────────────────────────────

def verdicts(report):
    """Judge every metric against the thresholds of the 2026-10-22 handoff."""
    metrics = report.get('metrics', {})
    rows = []

    def add(name, value, threshold, ok, note=''):
        rows.append({'metric': name, 'value': value, 'threshold': threshold,
                     'verdict': '达标' if ok else ('未达标' if ok is False else '未测'),
                     'note': note})

    pan = metrics.get('pan', {})
    zoom = metrics.get('zoom', {})
    wheel = metrics.get('wheel', {})
    sync_max = max([pan.get('max_ms', 0), zoom.get('max_ms', 0), wheel.get('max_ms', 0)] or [0])
    add('GUI 同步工作（pan/zoom/wheel 最大值）', f'{sync_max} ms', '≤16 ms',
        sync_max <= 16 if sync_max else None,
        'pan/zoom/wheel 三组的最差值')
    heartbeat = metrics.get('heartbeat', {})
    heartbeat_open = metrics.get('heartbeat_open', {})
    add('GUI 心跳阻塞（交互阶段 16ms 定时器最大间隔）',
        f"{heartbeat.get('max_ms', '—')} ms", '≤16 ms 常态 / 无 >250ms 阻塞',
        heartbeat.get('max_ms', 1e9) <= 250 if heartbeat else None,
        f"最大间隔即界面线程最长阻塞；打开阶段最大 "
        f"{heartbeat_open.get('max_ms', '—')} ms（单独计）")

    viewport = metrics.get('viewport_clarity', {})
    add('全视口清晰', f"{viewport.get('ms', '—')} ms", '≤2 s（中景）/ ≤5 s',
        (viewport.get('ms', 1e9) <= 5000) if viewport else None,
        f"比例 {viewport.get('scale', '—')}，可见 {viewport.get('visible_cards', '—')} 张，"
        f"未清晰 {viewport.get('unsharp_cards', '—')} 张")

    focal = metrics.get('focal_clarity', [])
    near = [row.get('ms', 0) for row in focal if row.get('distance') == 'near']
    mid = [row.get('ms', 0) for row in focal if row.get('distance') == 'mid']
    if near:
        add('焦点清晰（近 3 倍）', f'{max(near)} ms', '≤500 ms', max(near) <= 500,
            '；'.join(f"row {row['row']}: {row['ms']}ms{'（未清晰）' if not row['sharp'] else ''}"
                      for row in focal if row.get('distance') == 'near'))
    if mid:
        add('焦点清晰（中 0.5 倍）', f'{max(mid)} ms', '≤2000 ms', max(mid) <= 2000,
            '；'.join(f"row {row['row']}: {row['ms']}ms{'（未清晰）' if not row['sharp'] else ''}"
                      for row in focal if row.get('distance') == 'mid'))
    if not focal:
        add('焦点清晰', '—', '≤500 ms（近）/ ≤2000 ms（中）', None, '未测')

    rebuild = metrics.get('reenter_unchanged_scope', {}).get('delta', {})
    if rebuild and any(value is not None for value in rebuild.values()):
        total = sum(value for value in rebuild.values() if value is not None)
        add('重复进入未变范围 → 重建/重排次数', total, '= 0',
            total == 0, json.dumps(rebuild, ensure_ascii=False))
    else:
        add('重复进入未变范围 → 重建/重排次数', '—', '= 0', None,
            '内部计数器不可用，未测')

    working = metrics.get('working_set', [])
    if working:
        over = max(row.get('over_budget', 0) for row in working)
        first = working[0].get('budget_bytes', 0)
        last = working[-1].get('budget_bytes', 0)
        growth = last - first
        stable = abs(growth) <= max(1024 * 1024, int(first * 0.1))
        add('每类字节预算', f'超预算 {over} B', 'over_budget = 0', over == 0,
            json.dumps(working[-1].get('categories', {}), ensure_ascii=False))
        add('固定工作集稳定（3 轮预算波动）', f'{growth} B', '≤10% 且无单调增长',
            stable, f'首轮 {first} B，末轮 {last} B')
    else:
        add('每类字节预算', '—', 'over_budget = 0', None, '未测')

    material = metrics.get('material', {})
    if material.get('checked'):
        expected = material.get('expected_records')
        service = material.get('service_records')
        add('材质库打开（规模下）',
            f"{service} 条记录（就绪 {material.get('ready')}）",
            f'记录数 = {expected}' if expected is not None else '能打开且就绪',
            (material.get('ready') and (expected is None or service == expected))
            if expected is not None else bool(material.get('ready')),
            '材质面板与目录/预算共用同一套机器，规模下必须照常打开')
        cards = material.get('dom_cards')
        add('材质卡片渲染', f'{cards} 张 DOM 卡片', '> 0',
            (cards > 0) if isinstance(cards, int) else None,
            f"卡片图片 {material.get('dom_card_images')}，面板计数 {material.get('grid_count')}")
        if 'filtered_cards' in material:
            add('材质筛选收窄结果集',
                f"{material.get('filtered_cards')} / {cards} 张", '筛选后不多于筛选前',
                material.get('filter_narrows'), '材质库搜索框过滤')
    else:
        add('材质库打开（规模下）', '—', '能打开且就绪', None, '本次运行未覆盖材质面板')
    return rows


def render_markdown(report, manifest=None):
    lines = []
    lines.append(f"# 规模验收报告 · {report.get('label', '?')}")
    lines.append('')
    lines.append(f"- 工程：`{report.get('project')}`")
    lines.append(f"- 平台：{report.get('platform')}")
    lines.append(f"- 完成：{'是' if report.get('completed') else '否'}")
    if not report.get('completed') and report.get('error'):
        lines.append(f"- 错误：{report['error']}")
    lines.append('')
    if manifest:
        lines.append('## 记录规模与解码负载（分开报告）')
        lines.append('')
        lines.append('| 项 | 值 |')
        lines.append('| --- | --- |')
        scale = manifest.get('record_scale', {})
        load = manifest.get('decode_load', {})
        lines.append(f"| 目标档位 | {manifest.get('tier')} |")
        lines.append(f"| 记录规模：图片记录 | {scale.get('pixmaps')} |")
        lines.append(f"| 记录规模：画布页 | {scale.get('canvases')} |")
        lines.append(f"| 记录规模：材质记录 | {scale.get('materials')} |")
        lines.append(f"| 记录规模：记录总字节 | {scale.get('record_bytes')} |")
        lines.append(f"| 解码负载：真实独立素材 | {load.get('unique_real_assets')} |")
        lines.append(f"| 解码负载：真实独立字节 | {load.get('unique_real_bytes')} |")
        lines.append(f"| 解码负载：重复系数 | {load.get('repeat_factor')} |")
        lines.append('')
        lines.append(f"> {load.get('note', '')}")
        lines.append('')
    lines.append('## 指标')
    lines.append('')
    lines.append('| 指标 | 实测 | 阈值 | 结论 | 说明 |')
    lines.append('| --- | --- | --- | --- | --- |')
    for row in verdicts(report):
        lines.append(f"| {row['metric']} | {row['value']} | {row['threshold']} | "
                     f"{row['verdict']} | {row['note']} |")
    lines.append('')
    lines.append('## 阶段耗时（ms）')
    lines.append('')
    lines.append('| 阶段 | 耗时 |')
    lines.append('| --- | --- |')
    for name, value in report.get('stages', {}).items():
        lines.append(f'| {name} | {value} |')
    lines.append('')
    return '\n'.join(lines) + '\n'


# ── CLI ───────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    build = sub.add_parser('build', help='build a tier fixture from a real project')
    build.add_argument('--source', type=Path, required=True)
    build.add_argument('--tier', type=int, required=True)
    build.add_argument('--out', type=Path, required=True)
    build.add_argument('--images-per-canvas', type=int, default=100)
    build.add_argument('--materials', type=int, default=None)

    run = sub.add_parser('run', help='build a fixture and measure it')
    run.add_argument('--source', type=Path, required=True)
    run.add_argument('--tier', type=int, required=True)
    run.add_argument('--out', type=Path, required=True)
    run.add_argument('--label', default=None)
    run.add_argument('--images-per-canvas', type=int, default=100)
    run.add_argument('--materials', type=int, default=None)
    run.add_argument('--open-timeout-ms', type=int, default=900000)

    measure_parser = sub.add_parser('measure', help='measure an existing fixture')
    measure_parser.add_argument('--project', type=Path, required=True)
    measure_parser.add_argument('--label', default='measurement')
    measure_parser.add_argument('--out', type=Path, required=True)
    measure_parser.add_argument('--open-timeout-ms', type=int, default=900000)

    args = parser.parse_args()

    if args.command == 'build':
        manifest = build_fixture(args.source, args.tier, args.out,
                                 images_per_canvas=args.images_per_canvas,
                                 materials=args.materials)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    if args.command == 'measure':
        report = measure(args.project, args.label, args.out,
                         open_timeout_ms=args.open_timeout_ms)
        manifest_path = Path(args.out) / 'fixture.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8')) \
            if manifest_path.exists() else None
        (Path(args.out) / f'{args.label}.md').write_text(
            render_markdown(report, manifest), encoding='utf-8')
        print(json.dumps(verdicts(report), ensure_ascii=False, indent=2))
        return 0 if report.get('completed') else 1

    label = args.label or f'tier-{args.tier}'
    manifest = build_fixture(args.source, args.tier, args.out,
                             images_per_canvas=args.images_per_canvas,
                             materials=args.materials)
    report = measure(Path(args.out) / 'project.prism', label, args.out,
                     open_timeout_ms=args.open_timeout_ms)
    (Path(args.out) / f'{label}.md').write_text(
        render_markdown(report, manifest), encoding='utf-8')
    print(json.dumps(verdicts(report), ensure_ascii=False, indent=2))
    return 0 if report.get('completed') else 1


if __name__ == '__main__':
    sys.exit(main())
