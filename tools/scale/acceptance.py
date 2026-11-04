"""Real-scale acceptance baseline for Prism: the script that produces it.

This is the *only* source of truth for the project's current real-scale
behaviour. Every number it prints was measured on this machine, on this
build, against a real project file.

What it does, in order:

1. **build** -- copy a real project and rescale it to a target record
   count. The source is only ever read; its digest is compared before and
   after so a run cannot damage it.
2. **catalog** -- time the catalog index build (cold) and the catalog-ready
   check (hot), against a read-only copy of the project.
3. **measure** -- open a project in the *real* main window and measure:
   open-to-ready, the first screen, the view distances, pan/zoom/wheel,
   canvas switching, stop/resume, search and count, resident memory, and
   the SQL row counters that prove whether the open was on-demand or a full
   load.
4. **roundtrip** -- save and reopen a project **copy**, never the original.
5. **baseline** -- run the whole matrix and write the baseline report.

Read-only discipline
--------------------

The 51.8 GiB reference project on ``E:`` is opened read-only. The harness
records the project's size, mtime and SHA-256 before and after every run and
lists the containing directory before and after, so the report can *prove*
the project was not rewritten. Source projects are never opened for writing;
the save/reopen loop always runs on a copy.

Usage
-----

::

    python tools/scale/acceptance.py build --source <project.prism> \\
        --tier 1000 --out <dir>
    python tools/scale/acceptance.py measure --project <project.prism> \\
        --label tier-1000 --out <dir> --tier 1000 --mode paged --cache cold
    python tools/scale/acceptance.py baseline --out <dir>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import evidence  # noqa: E402  (path set up above)
import quality  # noqa: E402

PRIOR = evidence.load_prior_harness()

#: The three view distances, defined as canvas transform zoom factors.
#:
#: ``far``  -- the whole screen shows an overview of every record. Small
#:             tiles are a *normal* level of detail, not a degraded one.
#: ``mid``  -- one screen of tiles at the original resolution.
#: ``near`` -- one record blown up to the full screen. The pixels on screen
#:             must come from decoding the original, never from the far
#:             overview.
VIEW_DISTANCES = (
    dict(name='far', zoom=0.15),
    dict(name='mid', zoom=1.0),
    dict(name='near', zoom=4.0),
)

#: A scoped (on-demand) open reads one page of image metadata.
SCOPED_PAGE_LIMIT = 256
#: The display decoder never keeps more than this many decodes queued.
DECODE_PENDING_LIMIT = 8


# ── small helpers ──────────────────────────────────────────────────────────

def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                    encoding='utf-8')
    return path


def wait_until(app, predicate, timeout_ms=60000, label=''):
    """Pump the event loop until ``predicate`` holds or the timeout expires.

    The wait polls with a short sleep instead of blocking, so the GUI keeps
    painting and the worker threads keep making progress while the harness
    waits. The deadline bounds the wait, so a stuck state fails the stage
    with a message instead of hanging the run forever.
    """
    deadline = time.perf_counter() + timeout_ms / 1000
    while time.perf_counter() < deadline:
        app.processEvents()
        try:
            if predicate():
                return True
        except Exception:
            # A half-built scene may not answer every question yet.
            pass
        time.sleep(0.005)
    if label:
        print('TIMEOUT %s after %dms' % (label, timeout_ms), flush=True)
    return False


def settle(app, milliseconds):
    """Pump the event loop for a fixed span."""
    deadline = time.perf_counter() + milliseconds / 1000
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.005)


def human_bytes(value):
    if value is None:
        return 'n/a'
    value = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if abs(value) < 1024 or unit == 'TiB':
            return '%.2f %s' % (value, unit)
        value /= 1024
    return '%.2f TiB' % value


def _item_type(item):
    """The scene item's record type, wherever the reader keeps it."""
    value = getattr(item, 'TYPE', None)
    if isinstance(value, str) and value:
        return value
    for holder in (getattr(item, 'data', None), item):
        if isinstance(holder, dict) and holder.get('type'):
            return holder['type']
    return ''


# ── step 1: build a tier fixture ──────────────────────────────────────────

def build_fixture(source, tier, out_dir, *, images_per_canvas=100, materials=None):
    """Copy a real project and rescale it to ``tier`` records.

    The fixture builder itself is the prior harness', so the record scale /
    decode load split keeps exactly one definition.
    """
    return PRIOR.build_fixture(source, tier, out_dir,
                                images_per_canvas=images_per_canvas,
                                materials=materials)


def source_scale_facts(project, *, with_archive_bytes=False):
    """Record count, unique source images and real decode bytes of a project.

    These three numbers are deliberately separate. A tier that repeats the
    same originals has a big record scale and the same decode load; mixing
    them is how "100k assets" turns into "100k images to decode".

    * **records** -- how many rows the items table holds.
    * **unique_source_images** -- how many *different* pictures those rows
      point at. The reference project stores 100 000 rows built from 2 713
      distinct file names, so the record scale and the decode load differ by
      ~37x. Counting the blob rows would wrongly report 100 000.
    * **unique_source_bytes** -- the bytes of one copy of each distinct
      picture, i.e. what a decoder that caches by content actually pulls.

    ``with_archive_bytes`` additionally sums the size of *every* blob row,
    which is the hypothetical "decode every record separately" load. That
    sum reads the whole 50 GiB of blob pages and takes over a minute, so it
    is off by default: it would also evict the page cache that a cold-open
    measurement needs.
    """
    import sqlite3
    project = Path(project).resolve()
    facts = dict(project=str(project), records=None, unique_source_images=None,
                 unique_source_bytes=None, duplicated_records=None,
                 repeat_factor=None, note='no measurement')
    uri = project.as_uri() + '?mode=ro'
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
    except sqlite3.Error as error:
        facts['note'] = 'cannot open read-only: %s' % error
        return facts
    try:
        counts = {kind: count for kind, count in connection.execute(
            'SELECT type, count(*) FROM items GROUP BY type')}
        facts['records'] = sum(counts.values())
        facts['records_by_type'] = counts
        facts['image_records'] = counts.get('pixmap', 0)
        facts['materials'] = connection.execute(
            'SELECT count(*) FROM material_records').fetchone()[0]
        facts['pages_by_kind'] = {kind: count for kind, count in connection.execute(
            'SELECT kind, count(*) FROM workspace_pages GROUP BY kind')}
        # The distinct pictures: one row per distinct file name, and the
        # bytes of that one row's blob. ``sqlar.item_id`` is unique per row,
        # so counting it would count copies, not pictures.
        #
        # ``$.filename IS NOT NULL`` excludes the rows that were extracted
        # from a video and therefore have no file name at all. Grouping by
        # the NULL value put all 1,152 of them into one group and counted
        # that group as one more picture (2,713 instead of 2,712, and
        # 182,845 B too many). See
        # artifacts/scale-acceptance/2026-10-22/leader-verify/
        # unique-source-images-final-verdict.json.
        distinct = connection.execute(
            "WITH firsts AS (SELECT min(id) AS id, "
            "json_extract(data,'$.filename') AS fn FROM items "
            "WHERE json_extract(data,'$.filename') IS NOT NULL GROUP BY fn) "
            "SELECT count(*), sum(s.sz) FROM sqlar s JOIN firsts f ON f.id = s.item_id"
        ).fetchone()
        facts['unique_source_images'] = int(distinct[0] or 0)
        facts['unique_source_bytes'] = int(distinct[1] or 0)
        facts['archive_rows'] = connection.execute(
            'SELECT count(*) FROM sqlar').fetchone()[0]
        if with_archive_bytes:
            facts['unique_source_bytes'] = int(distinct[1] or 0)
            facts['decode_bytes_per_record'] = connection.execute(
                'SELECT sum(sz) FROM sqlar').fetchone()[0]
        facts['duplicated_records'] = max(0, facts['image_records']
                                          - facts['unique_source_images'])
        if facts['unique_source_images']:
            facts['repeat_factor'] = round(facts['image_records']
                                            / facts['unique_source_images'], 3)
    except sqlite3.Error as error:
        facts['note'] = 'read failed: %s' % error
    finally:
        connection.close()
    return facts


# ── step 2: catalog index build ────────────────────────────────────────────

def measure_catalog(project, cache_path, *, label, out_dir, rebuild=False):
    """Time the catalog index build (cold) and the ready check (hot).

    The builder reads the source project through a ``mode=ro`` URI, so the
    51.8 GiB reference project is never written.
    """
    from prism.project_open_scope import ProjectOpenScope

    project = Path(project).resolve()
    cache_path = Path(cache_path).resolve()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    scope = ProjectOpenScope(str(project), str(cache_path))

    report = dict(label=label, project=str(project), cache=str(cache_path),
                  cache_state_before=None, ready_before=None,
                  build=None, ready_after=None, total=None, counts=None)

    try:
        report['ready_before'] = bool(scope.ready)
        report['cache_state_before'] = scope.source_state()
    except Exception as error:
        report['cache_state_error'] = str(error)

    if rebuild:
        for suffix in ('', '-wal', '-shm', '.build-lock'):
            candidate = Path(str(cache_path) + suffix)
            if candidate.exists():
                try:
                    candidate.unlink()
                except OSError as error:
                    report.setdefault('cleanup_errors', []).append(
                        '%s: %s' % (candidate, error))
        scope = ProjectOpenScope(str(project), str(cache_path))
        report['ready_before'] = bool(scope.ready)

    started = time.perf_counter()
    batches = []
    try:
        result = scope.build(batch_size=128, on_batch=batches.append)
        elapsed = (time.perf_counter() - started) * 1000
        report['build'] = dict(complete=bool(result.complete),
                               total_records=int(result.total),
                               processed=int(result.processed),
                               resumed=bool(result.resumed),
                               characters=int(result.characters),
                               seconds=round(elapsed / 1000, 3),
                               milliseconds=round(elapsed, 2),
                               batches=len(batches),
                               batch_cumulative_head=batches[:5],
                               batch_cumulative_tail=batches[-5:])
    except Exception as error:
        report['build'] = dict(error=repr(error),
                               milliseconds=round((time.perf_counter() - started) * 1000, 2))
    try:
        report['ready_after'] = bool(scope.ready)
        report['total'] = scope.total()
        report['counts'] = {('%s|%s' % key): value
                            for key, value in (scope.counts() or {}).items()}
    except Exception as error:
        report['ready_error'] = repr(error)
    try:
        report['cache_bytes'] = cache_path.stat().st_size
    except OSError:
        report['cache_bytes'] = None
    return report


# ── step 3: measure a project in the real main window ──────────────────────

def measure(project, label, out_dir, *, tier=None, mode='paged', cache='cold',
            settings_dir=None, open_timeout_ms=900000, settle_ms=1500,
            screenshots=True, do_roundtrip=False, scale_facts=None,
            all_assets_timeout_ms=300000):
    """Open a project in the real main window and record the acceptance data."""
    project = Path(project).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    trace_dir = out_dir / 'trace' / label
    screens_dir = out_dir / 'screens'
    settings_dir = Path(settings_dir).resolve() if settings_dir else (out_dir / 'settings' / label)

    # The on-demand (scoped) reader is opt-in through an environment
    # override. '0' keeps the legacy full reader, which is the honest
    # counterpart the SQL row counters are compared against.
    os.environ['PRISM_PAGED_OPEN'] = '1' if mode == 'paged' else '0'
    # PrismSettings reads --settings-dir from argv **on first use**, and
    # importing prism.__main__ is what first uses it. So argv has to carry
    # the isolated settings directory *before* that import happens --
    # otherwise the run silently shares the real %APPDATA% settings and the
    # "cold cache" label is a lie.
    sys.argv = [sys.argv[0], '--settings-dir', str(settings_dir)]
    settings_dir.mkdir(parents=True, exist_ok=True)

    from unittest.mock import patch
    from PyQt6 import QtCore, QtGui, QtWidgets
    from prism.__main__ import PrismApplication, PrismMainWindow
    from prism import lod, performance_trace
    from prism.resource_budget import shared_cache_budget

    report = dict(label=label, project=str(project), tier=tier,
                  mode=mode, cache=cache, platform=None,
                  settings_dir=str(settings_dir),
                  stages={}, metrics={}, completed=False,
                  started=time.time())
    report_path = out_dir / 'reports' / ('%s.json' % label)

    def persist():
        write_json(report_path, report)

    # ── environment and forensics before ────────────────────────────────
    report['environment_before'] = evidence.capture_environment()
    report['environment_before']['python_processes'] = evidence.python_processes()
    before_fp = evidence.file_fingerprint(project)
    before_dir = evidence.directory_listing(project.parent)
    report['source_before'] = before_fp
    persist()

    # Count how many batches the catalog build reports: this is the C1
    # "batched reporting" evidence. The wrapper only observes; it does not
    # change the product's behaviour.
    catalog_batches = []
    original_resolve_scope = None

    def instrument_resolve_scope(module):
        original = module.resolve_scope

        def wrapper(filename, cache, last_canvas, current_canvas,
                    cancel=None, progress=None):
            def counting(total):
                catalog_batches.append(int(total))
                if progress is not None:
                    progress(total)
            return original(filename, cache, last_canvas, current_canvas,
                            cancel=cancel, progress=counting)
        module.resolve_scope = wrapper
        return original

    pulses = []
    decode_pending_samples = []
    previous = [time.perf_counter()]
    timer = QtCore.QTimer()
    timer.setInterval(16)

    def tick():
        now = time.perf_counter()
        pulses.append((now - previous[0]) * 1000)
        previous[0] = now
    timer.timeout.connect(tick)

    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'), \
         patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes), \
         patch('PyQt6.QtWidgets.QMessageBox.warning',
               return_value=QtWidgets.QMessageBox.StandardButton.Ok):
        app = PrismApplication([sys.argv[0]])
        report['platform'] = app.platformName()
        window = PrismMainWindow(app)
        window.resize(1580, 1000)
        window.show()
        view = window.view
        app.processEvents()
        timer.start()

        if mode == 'paged':
            import prism.paged_open_preparation as preparation_module
            original_resolve_scope = instrument_resolve_scope(preparation_module)

        trace = performance_trace.install(app, window, trace_dir)

        sampler = QtCore.QTimer()
        sampler.setInterval(50)

        def sample_pending():
            decoder = getattr(app, '_prism_display_decoder', None)
            if decoder is not None:
                decode_pending_samples.append(int(decoder.pending_count()))
        sampler.timeout.connect(sample_pending)
        sampler.start()

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
            # ── open to ready ─────────────────────────────────────────
            # The catalog read counters are the product's own accounting of
            # how many rows the open touched. They are cleared here so the
            # numbers belong to this open and not to an earlier one.
            try:
                from prism.project_open_scope import (catalog_read_counters,
                                                      reset_catalog_read_counters)
                reset_catalog_read_counters()
            except Exception:
                catalog_read_counters = None
            open_started = time.perf_counter()
            stage('open_request', lambda: view.open_from_file(str(project)))

            def ready_predicate():
                return (view.filename == str(project)
                        and not view.worker.isRunning()
                        and view.scene.items_to_add.empty()
                        and not getattr(view, '_project_opening', False)
                        and not getattr(view, '_paged_preparing', False))

            ready = wait_until(app, ready_predicate, open_timeout_ms, 'open_until_ready')
            report['stages']['open_until_ready'] = round(
                (time.perf_counter() - open_started) * 1000, 2)
            report['opened'] = bool(ready)
            report['metrics']['heartbeat_open'] = evidence.stats(pulses)
            interactive_from = len(pulses)
            if catalog_read_counters is not None:
                report['metrics']['catalog_read_counters_after_open'] = catalog_read_counters()
            persist()
            if not ready:
                report['error'] = 'project did not become ready'
                persist()
                return report

            # ── what is actually in the scene ─────────────────────────
            scene_items = list(view.scene.items_for_save())
            pages = view.scene.workspace_pages or []
            report['counts'] = dict(
                scene_items=len(scene_items),
                image_records=sum(1 for item in scene_items
                                  if _item_type(item) == 'pixmap'),
                material_records=len(getattr(view.scene, 'material_records', []) or []),
                documents=sum(1 for page in pages if page.get('kind') == 'document'),
                mindmaps=sum(1 for page in pages if page.get('kind') == 'mindmap'),
                canvases=sum(1 for page in pages if page.get('kind') == 'canvas'),
            )
            report['counts']['scene_item_ids'] = len(
                getattr(view.scene, '_loaded_scope_ids', ()) or ())
            persist()

            # ── the asset browser: all records ────────────────────────
            stage('all_assets_open', lambda: window.asset_browser.open('all'))
            # The catalog-backed browser materialises one row per record, so
            # the 100k tier needs more than the small tiers' 2 minutes.
            wait_until(app, lambda: window.asset_browser.model.rowCount() > 0,
                       all_assets_timeout_ms, 'all_assets')
            browser = window.asset_browser
            canvas = browser.list
            rows = browser.model.rowCount()
            report['counts']['catalog_rows'] = rows

            # From 5 000 rows up the canvas builds its cards in slices on the
            # event loop. The wait is bounded and its cost is a measurement
            # of its own: "how long until 100 000 cards exist".
            report['metrics']['browser_card_materialisation'] = _wait_for_canvas_cards(
                app, canvas, rows, all_assets_timeout_ms, 'canvas_cards')
            report['counts']['canvas_cards'] = len(getattr(canvas, 'cards', ()) or ())

            # ── far: every record on one screen ───────────────────────
            stage('all_assets_fit', lambda: canvas.fitInView(
                canvas._content_bounds, QtCore.Qt.AspectRatioMode.KeepAspectRatio))
            canvas.viewport().repaint()
            settle(app, settle_ms)
            fit_zoom = abs(canvas.transform().m11())
            fit_visible = canvas.visible_cards(
                canvas.mapToScene(canvas.viewport().rect()).boundingRect())
            report['metrics']['fit_all_on_screen'] = dict(
                zoom=round(fit_zoom, 6),
                visible_cards=len(fit_visible),
                card_px=round(canvas.gridSize().width() * fit_zoom, 2),
                rows=rows,
                note='natural zoom that puts every record on one screen')

            # ── the three view distances ──────────────────────────────
            dpr = canvas.viewport().devicePixelRatioF()
            distances = []
            for spec in VIEW_DISTANCES:
                distances.append(_measure_distance(
                    app, canvas, browser, spec, rows, dpr, screens_dir, label,
                    screenshots=screenshots, settle_ms=settle_ms))
            report['metrics']['view_distances'] = distances

            # ── GUI synchronous work ──────────────────────────────────
            pans = []
            for _ in range(20):
                started = time.perf_counter()
                canvas.translate(-15, 8)
                canvas.viewport().repaint()
                app.processEvents()
                pans.append((time.perf_counter() - started) * 1000)
            report['metrics']['pan'] = evidence.stats(pans)

            zooms = []
            for factor in [1.2] * 12 + [1 / 1.2] * 12:
                started = time.perf_counter()
                canvas.scale(factor, factor)
                canvas.viewport().repaint()
                app.processEvents()
                zooms.append((time.perf_counter() - started) * 1000)
            report['metrics']['zoom'] = evidence.stats(zooms)

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
            report['metrics']['wheel'] = evidence.stats(wheels)

            # ── switching canvases back and forth ─────────────────────
            report['metrics']['canvas_switch'] = _measure_canvas_switch(
                app, window, canvas, browser, pages, rows, dpr)

            # ── stop and resume ───────────────────────────────────────
            report['metrics']['stop_resume'] = _measure_stop_resume(
                app, view, window, canvas, browser, pages, dpr)

            # ── search and count ──────────────────────────────────────
            report['metrics']['search'] = _measure_search(app, window, view, browser)
            # The catalog path and the UI path are two different costs and
            # are kept apart on purpose.
            report['metrics']['catalog_iter_query'] = _measure_catalog_iter_query(app, view)

            # ── working set and memory ────────────────────────────────
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
                rounds.append(dict(
                    round=index,
                    rss_bytes=performance_trace.process_memory_bytes(),
                    budget_bytes=snapshot['bytes'],
                    over_budget=snapshot['over_budget'],
                    categories=snapshot['categories'],
                    scopes=snapshot['scopes'],
                    image_cache_bytes=lod.cache().bytes))
            report['metrics']['working_set'] = rounds
            report['metrics']['budget_after_open'] = budget.snapshot()
            report['metrics']['heartbeat'] = evidence.stats(pulses[interactive_from:])
            report['memory'] = dict(
                rss_bytes_first_round=rounds[0]['rss_bytes'],
                rss_bytes_last_round=rounds[-1]['rss_bytes'],
                rss_growth_bytes=(rounds[-1]['rss_bytes'] or 0) - (rounds[0]['rss_bytes'] or 0),
            )

            # ── C4: the decode queue must never exceed its limit ─────
            report['metrics']['decode_pending'] = dict(
                limit=DECODE_PENDING_LIMIT,
                samples=len(decode_pending_samples),
                max_observed=max(decode_pending_samples) if decode_pending_samples else None,
                over_limit=bool(decode_pending_samples
                                and max(decode_pending_samples) > DECODE_PENDING_LIMIT),
            )

            # ── C5: closing the project must give the bytes back ─────
            report['metrics']['close_release'] = _measure_close_release(
                app, view, window, budget, performance_trace)

            report['completed'] = True
        except Exception as error:
            # A failed stage must not cost the evidence that was already
            # collected: the report keeps what was measured and says where
            # it stopped.
            report['error'] = repr(error)
            report['error_traceback'] = traceback.format_exc()
            report['completed'] = False
        finally:
            timer.stop()
            sampler.stop()
            try:
                trace.close()
            except Exception:
                pass
            # The trace is the SQL evidence, and it has to be read after
            # the writer thread flushed it.
            events = evidence.read_trace_events(trace_dir)
            report['trace'] = dict(
                directory=str(trace_dir),
                events=len(events),
                sql_fetch=evidence.sql_read_rows(events),
                sql_execute=evidence.sql_execute_rows(events),
                catalog_batches=catalog_batches,
                catalog_batch_count=len(catalog_batches),
            )
            report['counting_points'] = _counting_points(report, events, mode, tier,
                                                          scale_facts or {})
            persist()
            if original_resolve_scope is not None:
                import prism.paged_open_preparation as preparation_module
                preparation_module.resolve_scope = original_resolve_scope
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

    # ── forensics after ───────────────────────────────────────────────
    after_fp = evidence.file_fingerprint(project)
    after_dir = evidence.directory_listing(project.parent)
    report['source_after'] = after_fp
    report['source_unchanged'] = bool(
        before_fp.get('sha256') == after_fp.get('sha256')
        and before_fp.get('size_bytes') == after_fp.get('size_bytes')
        and before_fp.get('mtime_ns') == after_fp.get('mtime_ns'))
    report['directory_diff'] = evidence.diff_listings(before_dir, after_dir)
    report['environment_after'] = evidence.capture_environment()
    report['environment_after']['python_processes'] = evidence.python_processes()
    persist()
    return report


def _wait_for_canvas_cards(app, canvas, rows, timeout_ms, label):
    """Wait until the browser canvas has finished materialising its cards.

    From 5 000 rows up the asset canvas builds its cards in slices on the
    event loop (``prism/widgets/asset_canvas.py::_start_staged_rebuild``),
    so the card list is empty for a while after ``open()`` returns. Measuring
    a view distance against a half-built canvas would measure the harness,
    not the product -- so the wait is explicit and its cost is recorded.
    """
    deadline = time.perf_counter() + timeout_ms / 1000
    started = time.perf_counter()
    last = -1
    while time.perf_counter() < deadline:
        app.processEvents()
        built = len(getattr(canvas, 'cards', ()) or ())
        if built != last:
            last = built
        if built >= rows and getattr(canvas, '_build_state', None) is None:
            return dict(ok=True, waited_ms=round((time.perf_counter() - started) * 1000, 2),
                        cards=built, rows=rows, timed_out=False)
        time.sleep(0.005)
    return dict(ok=False, waited_ms=round((time.perf_counter() - started) * 1000, 2),
                cards=len(getattr(canvas, 'cards', ()) or ()), rows=rows, timed_out=True,
                label=label)


def _measure_distance(app, canvas, browser, spec, rows, dpr, screens_dir, label,
                      *, screenshots=True, settle_ms=1500):
    """One view distance: set the zoom, wait for real pixels, fingerprint it.

    The criterion depends on the distance, and the difference matters:

    * **far** -- the whole screen is an overview. Every card must carry
      *some* pixels (nothing blank, nothing missing), but the pixels are
      *supposed* to be a thumbnail. Demanding original-resolution pixels
      here would be measuring the wrong thing.
    * **mid / near** -- the pixels on screen must be sharp enough for the
      on-screen size, which at ``near`` means they came from decoding the
      original. A thumbnail standing in for the original is a failure, not
      a speed-up.
    """
    from PyQt6 import QtGui
    zoom = spec['zoom']
    distance = spec['name']
    result = dict(distance=distance, zoom=zoom)
    if not rows:
        result['error'] = 'no rows'
        return result

    cards = getattr(canvas, 'cards', ()) or ()
    items = getattr(browser.model, 'items', ()) or ()
    if not cards or not items:
        result['error'] = ('the canvas has no cards yet (%d cards, %d model items)'
                           % (len(cards), len(items)))
        return result

    # The canvas and the model are addressed by the same row index, but the
    # canvas may still be a slice behind the model while it is rebuilding.
    usable = min(len(cards), len(items), rows)
    row = min(max(0, usable // 2), usable - 1)
    card = canvas.cards[row]
    item = browser.model.items[row]
    canvas.setTransform(QtGui.QTransform.fromScale(zoom, zoom))
    canvas.centerOn(card.sceneBoundingRect().center())

    sharpness_required = distance != 'far'
    started = time.perf_counter()
    settled = False
    visible = []
    missing = []
    resident_max_px = 0
    original_max_px = None
    while time.perf_counter() - started < 30:
        app.processEvents()
        viewport = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
        visible = canvas.visible_cards(viewport)
        missing = []
        resident_max_px = 0
        for visible_card in visible:
            if visible_card.row >= len(browser.model.items):
                continue
            visible_item = browser.model.items[visible_card.row]
            original = getattr(visible_item, '_image_size', None)
            if original is not None:
                original_max_px = max(original_max_px or 0,
                                      max(original.width(), original.height()))
            detailed, resident = PRIOR._card_detail(visible_card, visible_item, zoom, dpr)
            resident_max_px = max(resident_max_px, resident)
            if detailed is None:
                # No source pixels to be sharp about (a video or a text
                # record). It cannot fail the sharpness rule.
                continue
            if sharpness_required:
                if not detailed:
                    missing.append(visible_card.row)
            elif not detailed and resident <= 0:
                # Far distance: the card must still carry pixels.
                missing.append(visible_card.row)
        if visible and not missing:
            settled = True
            break
        time.sleep(0.01)
    elapsed = round((time.perf_counter() - started) * 1000, 2)

    result.update(
        row=row,
        milliseconds=elapsed,
        criterion=('every visible card carries pixels (far view serves '
                   'thumbnails by design)' if not sharpness_required
                   else 'every visible card carries pixels sharp enough for '
                        'its on-screen size'),
        sharpness_required=sharpness_required,
        settled=bool(settled),
        visible_cards=len(visible),
        unsharp_cards=len(missing),
        all_sharp=bool(settled and sharpness_required),
        resident_max_px=resident_max_px,
        original_max_px=original_max_px,
        card_px=round(canvas.gridSize().width() * zoom, 2),
        tile_edge_px=getattr(canvas._tiles, 'SIDE', None),
        composite_max_edge_px=1024,
        detail_threshold_px=40,
        overview_max_px=8,
        note='zoom is the canvas transform factor; card_px is the on-screen '
             'card width; tile_edge_px is CanvasTiles.SIDE',
    )

    if screenshots:
        screens_dir.mkdir(parents=True, exist_ok=True)
        path = screens_dir / ('%s-%s.png' % (label, distance))
        # grab() returns a pixmap; the fingerprint needs the pixels.
        image = canvas.viewport().grab().toImage()
        if image.save(str(path)):
            result['screenshot'] = str(path)
            result['fingerprint'] = quality.fingerprint_image(image)
            result['card_regions'] = _card_region_fingerprints(canvas, browser, image)
    return result


def _card_region_fingerprints(canvas, browser, image):
    """Per-card pixel check: a card that painted nothing must be visible."""
    rgb = quality.qimage_to_array(image)
    if rgb is None:
        return dict(error='no image')
    viewport = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    visible = canvas.visible_cards(viewport)
    regions = []
    empty = 0
    for visible_card in visible:
        rect = canvas.viewportTransform().mapRect(visible_card.sceneBoundingRect())
        rect = rect.toAlignedRect().intersected(image.rect())
        if rect.width() <= 2 or rect.height() <= 2:
            continue
        stats = quality.region_fingerprint(rgb, (rect.x(), rect.y(),
                                                rect.width(), rect.height()))
        stats['row'] = visible_card.row
        if stats.get('empty'):
            empty += 1
        regions.append(stats)
    return dict(cards_checked=len(regions), empty_cards=empty,
                worst_stddev=min([r.get('stddev_luma', 0) for r in regions], default=None),
                samples=regions[:12])


def _measure_canvas_switch(app, window, canvas, browser, pages, rows, dpr):
    """Switch between two canvases and back, timing each switch."""
    canvases = [page['id'] for page in pages if page.get('kind') == 'canvas']
    if len(canvases) < 2:
        return dict(checked=False, reason='fewer than two canvases')
    first, second = canvases[0], canvases[1]
    switches = []
    for target in (first, second, first):
        started = time.perf_counter()
        window._open_resource_page('canvas', target)
        app.processEvents()
        wait_until(app, lambda: browser.model.rowCount() > 0, 60000, 'canvas_switch')
        settle(app, 200)
        switches.append(dict(target=target,
                             milliseconds=round((time.perf_counter() - started) * 1000, 2),
                             rows=browser.model.rowCount()))
    window.asset_browser.open('all')
    app.processEvents()
    return dict(checked=True, switches=switches)


def _measure_stop_resume(app, view, window, canvas, browser, pages, dpr):
    """Cancel an in-flight page load and resume it."""
    loader = getattr(view, '_paged_canvas_loader', None)
    if loader is None:
        return dict(checked=False, reason='no paged canvas loader (full reader)')
    canvases = [page['id'] for page in pages if page.get('kind') == 'canvas']
    if not canvases:
        return dict(checked=False, reason='no canvas')
    target = canvases[0]
    loader.cancel()
    settle(app, 100)
    started = time.perf_counter()
    view.request_canvas_assets(target)
    app.processEvents()
    wait_until(app, lambda: not loader.pending, 60000, 'resume')
    settle(app, 300)
    elapsed = round((time.perf_counter() - started) * 1000, 2)
    return dict(checked=True, target=target, resume_milliseconds=elapsed,
                pending_after=len(loader.pending or []))


def _measure_search(app, window, view, browser):
    """Global search plus the count the sidebar shows.

    The query is taken from a record that really exists in this project, so
    the number is a real hit count and not an empty-result artefact of a
    made-up query string. When no record carries a title, the file name is
    used instead -- the catalog search covers title/notes/filename/tags, so a
    file name is a real needle too.

    The **same** needle is then run through the catalog path, so the UI cost
    and the catalog cost of one identical query can be compared directly
    instead of being two unrelated numbers.
    """
    from prism.project_search import search_project
    service = view.category_panel.resource_service
    needle = None
    needle_source = None
    try:
        for record in browser.model.items:
            title = str(getattr(record, 'title', '') or '').strip()
            if title:
                needle, needle_source = title[:12], 'title'
                break
        if needle is None:
            for record in browser.model.items:
                filename = str(getattr(record, 'filename', '') or '').strip()
                if filename:
                    needle = Path(filename).stem[:12]
                    needle_source = 'filename'
                    break
    except Exception:
        needle = None
    if not needle:
        return dict(checked=False, reason='no searchable title or file name in this project')
    started = time.perf_counter()
    try:
        results = search_project(
            view.scene, service, needle, (),
            page_records=view.category_panel._pages_with_implicit_defaults())
        milliseconds = round((time.perf_counter() - started) * 1000, 2)
        hits = len(results)
    except Exception as error:
        return dict(checked=False, reason=repr(error))
    counts = {}
    try:
        browser.open('all')
        app.processEvents()
        counts['all_rows'] = browser.model.rowCount()
    except Exception as error:
        counts['error'] = repr(error)
    report = dict(checked=True, query=needle, query_source=needle_source,
                  milliseconds=milliseconds, hits=hits, counts=counts)

    # The same query through the catalog path. This is the other half of the
    # comparison: the UI path above and the catalog path here answer the
    # same question, so their costs can be read against each other.
    scope = getattr(view, '_open_scope', None)
    if scope is not None and getattr(scope, 'ready', False):
        started = time.perf_counter()
        try:
            catalog_hits = scope.count(text=needle)
            catalog_count_ms = round((time.perf_counter() - started) * 1000, 2)
            started = time.perf_counter()
            catalog_rows = list(scope.iter_query(text=needle, limit=100))
            catalog_iter_ms = round((time.perf_counter() - started) * 1000, 2)
            report['catalog_same_needle'] = dict(
                query=needle, count=catalog_hits, count_ms=catalog_count_ms,
                iter_rows=len(catalog_rows), iter_ms=catalog_iter_ms)
        except Exception as error:
            report['catalog_same_needle'] = dict(error=repr(error))
    return report


def _measure_catalog_iter_query(app, view):
    """The catalog hot path and the UI search path, measured separately.

    They are two different code paths and they must never be merged into one
    number: the catalog path is what a directory-backed query costs, the UI
    path is what the user waits for when they type into the search box. A
    baseline that only carries one of them cannot say whether an optimisation
    moved the number the user actually feels.

    The scope comes from ``view._open_scope``, which the **real** on-demand
    path sets at ``prism/view.py:998`` (``_paged_open_preparation_completed``,
    stage ``'scope'``) -- not from ``_paged_open_scope``, which is dead code.
    """
    from prism.project_open_scope import (catalog_read_counters,
                                          reset_catalog_read_counters)

    scope = getattr(view, '_open_scope', None)
    if scope is None:
        return dict(checked=False, reason='the view holds no open scope')
    if not getattr(scope, 'ready', False):
        return dict(checked=False, reason='the catalog scope is not ready')

    counts = scope.counts() or {}
    # ``counts()`` is keyed by ``(canvas_id, kind)``; the canvas id is the
    # first half of that key.
    canvas_id = max(counts.items(), key=lambda item: item[1])[0][0] if counts else None
    result = dict(checked=True, canvas_sampled=canvas_id,
                  counts_rows=len(counts))

    def timed(label, count_filters, call):
        """Time one query and read the C2 counters around it.

        The counters are the product's own accounting of how many rows and
        pages the read actually touched, so the cost and the count come
        from the same call.
        """
        reset_catalog_read_counters()
        started = time.perf_counter()
        value = call()
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        counters = catalog_read_counters()
        entry = dict(ms=elapsed, rows=len(value),
                     catalog_rows=counters.get('catalog_rows', 0),
                     catalog_pages=counters.get('catalog_pages', 0),
                     iter_query_count=counters.get('iter_query_count', 0))
        stats = getattr(scope, 'last_read_stats', None) or {}
        entry['sorted_elements'] = stats.get('sorted_elements')
        entry['max_page'] = stats.get('max_page')
        entry['hit_count'] = scope.count(**count_filters)
        result[label] = entry
        return value

    timed('counts', {}, lambda: list(counts))
    if canvas_id is not None:
        timed('iter_query_canvas_limit_100', dict(canvas_ids=(canvas_id,)),
              lambda: list(scope.iter_query(canvas_ids=(canvas_id,), limit=100)))
    timed('iter_query_unfiltered_limit_100', {},
          lambda: list(scope.iter_query(limit=100)))
    timed('iter_query_text_a_limit_100', dict(text='a'),
          lambda: list(scope.iter_query(text='a', limit=100)))
    started = time.perf_counter()
    details = scope.details([row['id'] for row in
                             list(scope.iter_query(canvas_ids=(canvas_id,), limit=100))
                             ]) if canvas_id is not None else ()
    result['details_100'] = dict(ms=round((time.perf_counter() - started) * 1000, 2),
                                 rows=len(details))

    # Known defect probe: a *string* canvas_ids used to be silently split
    # into a per-character tuple by ``ProjectCatalogIndex._predicate`` and
    # answered "no rows" without raising. The product now coerces a bare
    # string into a one-element tuple, so the probe records whether the
    # silent wrong answer is still there.
    if canvas_id is not None:
        reset_catalog_read_counters()
        started = time.perf_counter()
        string_rows = list(scope.iter_query(canvas_ids=canvas_id, limit=100))
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        tuple_rows = list(scope.iter_query(canvas_ids=(canvas_id,), limit=100))
        result['defect_probe_canvas_ids_as_string'] = dict(
            ms=elapsed,
            string_rows=len(string_rows),
            tuple_rows=len(tuple_rows),
            silently_wrong=bool(len(string_rows) == 0 and len(tuple_rows) > 0),
            note='prism/project_open_scope.py::_as_canvas_ids now wraps a bare '
                 'string into a one-element tuple; a string that answers 0 rows '
                 'while the tuple answers rows is the silent-wrong-answer defect')
    return result


def _measure_close_release(app, view, window, budget, performance_trace):
    """Close the project and see whether the resident bytes come back.

    ``release_project_resources`` -- the function that is supposed to hand
    a closed project's bytes back -- has no call site in the product today.
    This measurement is the evidence for that, not an assumption about it.
    """
    before = budget.snapshot()
    rss_before = performance_trace.process_memory_bytes()
    try:
        view.clear_scene()
    except Exception as error:
        return dict(checked=False, reason=repr(error))
    settle(app, 1500)
    after = budget.snapshot()
    rss_after = performance_trace.process_memory_bytes()
    scope_before = sum(before.get('scopes', {}).values())
    scope_after = sum(after.get('scopes', {}).values())
    return dict(checked=True,
                budget_bytes_before=before['bytes'],
                budget_bytes_after=after['bytes'],
                budget_released=(before['bytes'] - after['bytes']),
                budget_released_percent=(round((before['bytes'] - after['bytes'])
                                               * 100.0 / before['bytes'], 3)
                                         if before['bytes'] else None),
                scopes_before=before.get('scopes'),
                scopes_after=after.get('scopes'),
                scope_bytes_released=(scope_before - scope_after),
                over_budget_before=before['over_budget'],
                over_budget_after=after['over_budget'],
                rss_bytes_before=rss_before,
                rss_bytes_after=rss_after,
                rss_released=(rss_before - rss_after) if (rss_before and rss_after) else None,
                note='release_project_resources has no call site in the '
                     'product today, so a closed project keeps its bytes')


# ── count points ───────────────────────────────────────────────────────────

def _counting_points(report, events, mode, tier, scale_facts):
    """The five minimal observable count points, as far as the code allows.

    A count point that does not exist is reported as *not accepted*, never
    with a made-up number.
    """
    points = []

    def add(point, status, measured=None, criterion=None, evidence_text=None):
        points.append(dict(point=point, status=status, measured=measured,
                           criterion=criterion, evidence=evidence_text))

    sql_fetch = report.get('trace', {}).get('sql_fetch', {})
    image_rows = sql_fetch.get('image_metadata', {})
    max_rows = image_rows.get('max_rows')
    total_rows = image_rows.get('total_rows')

    # C1 -- preparation batch reporting.
    batches = report.get('trace', {}).get('catalog_batches', [])
    scene_ids = report.get('counts', {}).get('scene_item_ids')
    if mode == 'paged' and batches:
        add('C1 准备阶段处理的记录数（分批上报）', '部分验收',
            measured=dict(batches=len(batches),
                          final_cumulative=batches[-1] if batches else None,
                          scene_item_ids=scene_ids),
            criterion='建目录阶段分批上报；按需打开单次 processed <= 页大小',
            evidence_text='on_batch 回调共触发 %d 次，末值 %s；打开后 scene 登记 %s 条'
                          % (len(batches), batches[-1] if batches else None, scene_ids))
    elif mode == 'paged':
        add('C1 准备阶段处理的记录数（分批上报）', '未验收',
            criterion='建目录阶段分批上报；按需打开单次 processed <= 页大小',
            evidence_text='未捕获到 on_batch 回调（目录可能已是热缓存）')
    else:
        add('C1 准备阶段处理的记录数（分批上报）', '未验收',
            criterion='按需打开时单次 processed <= 页大小(100)',
            evidence_text='全量读取路径没有准备阶段；改由 C1/T1 的 rows 判据覆盖')

    # C2 -- catalog paging counter. The counters exist in the product now
    # (``prism/project_open_scope.py``: ``catalog_read_counters()``), so the
    # criterion is checked against real numbers rather than a marker.
    source = evidence.source_contains(
        ROOT / 'prism' / 'project_open_scope.py',
        ('catalog_pages', 'catalog_rows', 'iter_query_count', 'page_counter'))
    catalog_metrics = report.get('metrics', {}).get('catalog_iter_query') or {}
    if source['found']:
        entries = {}
        violations = []
        for key, entry in catalog_metrics.items():
            if not isinstance(entry, dict) or 'catalog_rows' not in entry:
                continue
            hits = entry.get('hit_count')
            rows_read = entry.get('catalog_rows')
            pages = entry.get('catalog_pages')
            sorted_elements = entry.get('sorted_elements')
            max_page = entry.get('max_page')
            limit = 100
            # "rows read <= hits + one page" and "the sort only ever saw the
            # limit": the two halves of the C2 criterion. The sort half is
            # about the *page* the sort saw, not about the total the
            # iterator yielded -- a keyset iterator yields a full page and
            # then a short one, so the total legitimately exceeds the limit.
            if hits is not None and rows_read is not None and rows_read > hits + limit:
                violations.append('%s read %s rows for %s hits'
                                  % (key, rows_read, hits))
            if max_page is not None and max_page > limit:
                violations.append('%s sorted a page of %s elements with limit %s'
                                  % (key, max_page, limit))
            entries[key] = dict(rows_read=rows_read, pages=pages,
                                sorted_elements=sorted_elements,
                                max_page=max_page,
                                hit_count=hits, ms=entry.get('ms'))
        add('C2 目录取数的页数 / 行数',
            '完成' if entries and not violations else ('未完成' if entries else '代码已接未验收'),
            measured=dict(entries=entries, violations=violations,
                          counters=source['found']),
            criterion='三档过滤累计取行数 <= 命中数 + 1 页；limit=100 时参与排序元素数 <= 100',
            evidence_text='目录取数计数器：%s' % json.dumps(entries, ensure_ascii=False))
    else:
        add('C2 目录取数的页数 / 行数', '未验收',
            criterion='三档过滤累计取行数 <= 命中数 + 1 页；limit=100 时参与排序元素数 <= 100',
            evidence_text='计数点未实现：prism/project_open_scope.py 的 iter_query() '
                          '没有页数/行数计数器')

    # C3 -- catalog scope hit/miss.
    source = evidence.source_contains(
        ROOT / 'prism' / 'project_catalog.py',
        ('catalog_scope_hit', 'catalog_scope_miss'))
    if source['found']:
        add('C3 口径来源（目录命中 / 未命中）', '代码已接未验收',
            criterion='目录 ready 之后侧栏计数与检索不再有 miss',
            evidence_text='检测到计数点标记 %s' % source['found'])
    else:
        add('C3 口径来源（目录命中 / 未命中）', '未验收',
            criterion='目录 ready 之后侧栏计数与检索不再有 miss',
            evidence_text='计数点未实现：prism/project_catalog.py 的 _catalog_scope() '
                          '不产生 catalog_scope_hit / catalog_scope_miss')

    # C4 -- the decode queue.
    pending = report.get('metrics', {}).get('decode_pending', {})
    max_observed = pending.get('max_observed')
    if max_observed is None:
        add('C4 待解码队列长度', '未验收',
            criterion='任何时刻 <= %d' % DECODE_PENDING_LIMIT,
            evidence_text='没有采到 pending_count() 样本')
    else:
        add('C4 待解码队列长度', '完成' if not pending.get('over_limit') else '未完成',
            measured=dict(max_observed=max_observed, samples=pending.get('samples'),
                          limit=DECODE_PENDING_LIMIT),
            criterion='任何时刻 <= %d' % DECODE_PENDING_LIMIT,
            evidence_text='运行期间 %d 个样本，峰值 %d' % (pending.get('samples'), max_observed))

    # C5 -- resource budget after close.
    close_release = report.get('metrics', {}).get('close_release', {})
    working = report.get('metrics', {}).get('working_set', [])
    over_series = [row.get('over_budget', 0) for row in working]
    over_budget_grows = bool(over_series and over_series[-1] > over_series[0])
    if close_release.get('checked'):
        released = close_release.get('budget_released')
        percent = close_release.get('budget_released_percent')
        scope_released = close_release.get('scope_bytes_released')
        ok = bool(released and released > 0
                  and (percent is None or percent >= 50)
                  and scope_released and scope_released > 0)
        add('C5 资源预算快照（关工程后释放）',
            '完成' if ok else '未完成',
            measured=dict(budget_bytes_before=close_release.get('budget_bytes_before'),
                          budget_bytes_after=close_release.get('budget_bytes_after'),
                          released=released,
                          released_percent=percent,
                          scope_bytes_released=scope_released,
                          rss_released=close_release.get('rss_released'),
                          over_budget_series=over_series,
                          over_budget_grows=over_budget_grows),
            criterion='关工程后 resident_bytes 下降（预算里属于该工程的字节要还回来）',
            evidence_text='关闭工程后预算字节只回收了 %s B（%s%%），进程常驻内存变化 %s B；'
                          'release_project_resources 在产品代码里零调用点'
                          % (released, percent, close_release.get('rss_released')))
    else:
        add('C5 资源预算快照（关工程后释放）', '未验收',
            criterion='关工程后 resident_bytes 下降',
            evidence_text=close_release.get('reason', '未测'))

    # T1 / T2 -- the SQL row counters.
    if mode == 'paged':
        status = '完成' if (max_rows is not None and max_rows <= SCOPED_PAGE_LIMIT) else '未完成'
        add('T1 按需打开的 SQL 取行数', status,
            measured=dict(max_single_fetch=max_rows, total_rows=total_rows,
                          calls=image_rows.get('calls'),
                          per_label={key: value.get('max_rows')
                                     for key, value in sql_fetch.items()}),
            criterion="query='image_metadata' 的 rows <= %d" % SCOPED_PAGE_LIMIT,
            evidence_text='各标签单次最大取行：%s'
                          % json.dumps({key: value.get('max_rows')
                                        for key, value in sql_fetch.items()},
                                       ensure_ascii=False))
    else:
        expected = scale_facts.get('image_records')
        status = '完成' if (expected and total_rows == expected) else '未完成'
        add('T2 全量打开的 SQL 取行数', status,
            measured=dict(total_rows=total_rows, max_single_fetch=max_rows,
                          calls=image_rows.get('calls'),
                          expected_items=expected),
            criterion='query=\'image_metadata\' 的 rows == items 总数',
            evidence_text='取行 %s，期望 %s' % (total_rows, expected))
    return points


# ── step 4: save and reopen, always on a copy ─────────────────────────────

def roundtrip(source, label, out_dir, *, mode='paged', settings_dir=None,
              open_timeout_ms=900000, allow_large_copy=False,
              large_copy_bytes=20 * 1024 * 1024 * 1024):
    """Save a project and reopen it -- on a **copy**, never on the original."""
    source = Path(source).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    copy_dir = out_dir / 'roundtrip' / label
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy = copy_dir / source.name
    settings_dir = Path(settings_dir).resolve() if settings_dir else (out_dir / 'settings' / label)

    # argv has to carry the isolated settings directory *before*
    # prism.__main__ is imported: PrismSettings resolves it on first use,
    # and the isolated directory is what keeps this run off the real
    # %APPDATA% settings.
    sys.argv = [sys.argv[0], '--settings-dir', str(settings_dir)]
    settings_dir.mkdir(parents=True, exist_ok=True)
    os.environ['PRISM_PAGED_OPEN'] = '1' if mode == 'paged' else '0'

    from unittest.mock import patch
    from PyQt6 import QtCore, QtWidgets
    from prism.__main__ import PrismApplication, PrismMainWindow
    from prism import performance_trace

    report = dict(label=label, source=str(source), copy=str(copy),
                  mode=mode, stages={}, completed=False)
    report_path = out_dir / 'reports' / ('%s-roundtrip.json' % label)

    def persist():
        write_json(report_path, report)

    source_size = source.stat().st_size
    report['source_bytes'] = source_size
    if source_size > large_copy_bytes and not allow_large_copy:
        report['error'] = ('not run: copying %.1f GiB is outside this run\'s '
                           'disk budget; the caller must pass --allow-large-copy'
                           % (source_size / 1024 ** 3))
        report['status'] = '未验收'
        persist()
        return report

    before = evidence.file_fingerprint(source)
    report['source_before'] = before
    started = time.perf_counter()
    if copy.exists():
        copy.unlink()
    shutil.copyfile(source, copy)
    report['copy_seconds'] = round(time.perf_counter() - started, 2)
    report['copy_bytes'] = copy.stat().st_size

    with patch('prism.browser_capture.BrowserCaptureService.start'), \
         patch('prism.capture_hotkey.CaptureHotkey'), \
         patch('prism.clipboard_capture.ClipboardCaptureAdapter.start'), \
         patch('PyQt6.QtWidgets.QMessageBox.question',
               return_value=QtWidgets.QMessageBox.StandardButton.Yes), \
         patch('PyQt6.QtWidgets.QMessageBox.warning',
               return_value=QtWidgets.QMessageBox.StandardButton.Ok):
        app = PrismApplication([sys.argv[0]])
        window = PrismMainWindow(app)
        window.resize(1580, 1000)
        window.show()
        view = window.view
        app.processEvents()
        trace = performance_trace.install(app, window, out_dir / 'trace' / (label + '-roundtrip'))

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
            def ready():
                return (view.filename == str(copy)
                        and not view.worker.isRunning()
                        and view.scene.items_to_add.empty()
                        and not getattr(view, '_project_opening', False)
                        and not getattr(view, '_paged_preparing', False))

            stage('open_copy', lambda: view.open_from_file(str(copy)))
            opened = wait_until(app, ready, open_timeout_ms, 'roundtrip_open')
            report['opened'] = bool(opened)
            if not opened:
                report['error'] = 'copy did not become ready'
                persist()
                return report
            report['counts_before_save'] = dict(
                scene_items=len(list(view.scene.items_for_save())),
                material_records=len(getattr(view.scene, 'material_records', []) or []))

            stage('save', lambda: view.do_save(str(copy), create_new=False))
            saved = wait_until(app, lambda: not view.worker.isRunning(), open_timeout_ms,
                               'roundtrip_save')
            report['saved'] = bool(saved)
            settle(app, 500)
            report['copy_after_save'] = evidence.file_fingerprint(copy)

            # Close and reopen the very same file.
            view.clear_scene()
            settle(app, 500)
            stage('reopen_copy', lambda: view.open_from_file(str(copy)))
            reopened = wait_until(app, ready, open_timeout_ms, 'roundtrip_reopen')
            report['reopened'] = bool(reopened)
            if reopened:
                report['counts_after_reopen'] = dict(
                    scene_items=len(list(view.scene.items_for_save())),
                    material_records=len(getattr(view.scene, 'material_records', []) or []))
            report['completed'] = True
        finally:
            try:
                trace.close()
            except Exception:
                pass
            persist()
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

    after = evidence.file_fingerprint(source)
    report['source_after'] = after
    report['source_unchanged'] = bool(before.get('sha256') == after.get('sha256')
                                       and before.get('mtime_ns') == after.get('mtime_ns'))
    persist()
    return report


# ── CLI ────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest='command', required=True)

    build = sub.add_parser('build', help='build a tier fixture from a real project')
    build.add_argument('--source', type=Path, required=True)
    build.add_argument('--tier', type=int, required=True)
    build.add_argument('--out', type=Path, required=True)
    build.add_argument('--images-per-canvas', type=int, default=100)
    build.add_argument('--materials', type=int, default=None)

    catalog = sub.add_parser('catalog', help='time the catalog index build')
    catalog.add_argument('--project', type=Path, required=True)
    catalog.add_argument('--cache', type=Path, required=True)
    catalog.add_argument('--label', default='catalog')
    catalog.add_argument('--out', type=Path, required=True)
    catalog.add_argument('--rebuild', action='store_true',
                         help='delete the cache first (cold build)')

    measure_parser = sub.add_parser('measure', help='measure an existing project')
    measure_parser.add_argument('--project', type=Path, required=True)
    measure_parser.add_argument('--label', required=True)
    measure_parser.add_argument('--out', type=Path, required=True)
    measure_parser.add_argument('--tier', type=int, default=None)
    measure_parser.add_argument('--mode', choices=('paged', 'full'), default='paged')
    measure_parser.add_argument('--cache', choices=('cold', 'hot'), default='cold')
    measure_parser.add_argument('--settings-dir', type=Path, default=None)
    measure_parser.add_argument('--open-timeout-ms', type=int, default=900000)
    measure_parser.add_argument('--all-assets-timeout-ms', type=int, default=300000,
                                help='how long the catalog-backed browser may take to '
                                     'materialise one row per record (the 100k tier '
                                     'needs more than the small tiers)')
    measure_parser.add_argument('--no-screenshots', action='store_true')

    trip = sub.add_parser('roundtrip', help='save and reopen a copy of a project')
    trip.add_argument('--source', type=Path, required=True)
    trip.add_argument('--label', required=True)
    trip.add_argument('--out', type=Path, required=True)
    trip.add_argument('--mode', choices=('paged', 'full'), default='paged')
    trip.add_argument('--settings-dir', type=Path, default=None)
    trip.add_argument('--allow-large-copy', action='store_true')

    args = parser.parse_args(argv)

    if args.command == 'build':
        manifest = build_fixture(args.source, args.tier, args.out,
                                 images_per_canvas=args.images_per_canvas,
                                 materials=args.materials)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    if args.command == 'catalog':
        report = measure_catalog(args.project, args.cache, label=args.label,
                                 out_dir=args.out, rebuild=args.rebuild)
        write_json(Path(args.out) / 'reports' / ('%s.json' % args.label), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0

    if args.command == 'measure':
        facts = source_scale_facts(args.project)
        report = measure(args.project, args.label, args.out, tier=args.tier,
                         mode=args.mode, cache=args.cache,
                         settings_dir=args.settings_dir,
                         open_timeout_ms=args.open_timeout_ms,
                         all_assets_timeout_ms=args.all_assets_timeout_ms,
                         screenshots=not args.no_screenshots,
                         scale_facts=facts)
        report['scale_facts'] = facts
        write_json(Path(args.out) / 'reports' / ('%s.json' % args.label), report)
        print(json.dumps(report.get('counting_points', []), ensure_ascii=False, indent=2))
        return 0 if report.get('completed') else 1

    if args.command == 'roundtrip':
        report = roundtrip(args.source, args.label, args.out, mode=args.mode,
                           settings_dir=args.settings_dir,
                           allow_large_copy=args.allow_large_copy)
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0 if report.get('completed') else 1

    parser.error('unknown command')
    return 2


if __name__ == '__main__':
    _code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Qt's interpreter teardown can raise an access violation (0xC0000005)
    # while a QWebEnginePage is still alive -- the same crash the existing
    # scale harness documents. Every artefact is already on disk, so the
    # process leaves with its status code instead of with a crash dump.
    os._exit(_code)
