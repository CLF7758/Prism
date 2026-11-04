"""Candidate budget measurement for the image-search path (D-2, P10).

Run it explicitly::

    $env:PRISM_D2_BENCHMARK='1'
    .\\.venv\\Scripts\\python.exe -m pytest tests/test_visual_search_benchmark.py -q

It writes ``artifacts/group-d/W-D2-candidate-budget.json``.

The three numbers are reported **separately**, because they answer different
questions:

``records``
    how many rows the project holds (the old code embedded all of them);
``unique_sources``
    how many distinct source images those rows point at (a project may
    hold many rows per image - this one does not);
``decoded_bytes``
    how many source bytes the worker would actually have to read and
    decode for the round.  This is the number the trimming moves.

The environment is stated in the artifact: this is a synthetic project on
the local disk, not the 51.8 GiB stress project.  The ML worker itself is
not installed in this environment, so "decoded bytes" is measured from the
source payloads the round would hand it, not from a CLIP run.
"""
import json
import os
import platform
import random
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from PyQt6 import QtGui

from prism.project_catalog import ProjectAssetCatalog
from prism.project_open_scope import ProjectOpenScope
from prism.project_search import item_key


ARTIFACT = Path(__file__).resolve().parents[1] / 'artifacts' / 'group-d' / 'W-D2-candidate-budget.json'

RECORDS = 2000
BUDGET = 200
SIDE = 64

pytestmark = pytest.mark.skipif(
    os.environ.get('PRISM_D2_BENCHMARK') != '1',
    reason='Set PRISM_D2_BENCHMARK=1 to run the candidate budget benchmark')


def build_project(tmp_path, count, side=SIDE):
    """A project file with `count` rows, each with its own PNG payload.

    The payload is noise, not a flat colour: a flat colour compresses to a
    few hundred bytes and the byte numbers would be meaningless.
    """
    source = tmp_path / 'benchmark.prism'
    sizes = {}
    with sqlite3.connect(source) as db:
        db.executescript(
            "CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);"
            "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key in range(1, count + 1):
            image = QtGui.QImage(side, side, QtGui.QImage.Format.Format_RGB32)
            generator = random.Random(key)
            for y in range(side):
                for x in range(side):
                    image.setPixel(x, y, generator.randint(0, 0xFFFFFF))
            path = tmp_path / f'src-{key:05d}.png'
            assert image.save(str(path))
            payload = path.read_bytes()
            sizes[key] = len(payload)
            extra = dict(canvasId='canvas-a', title=f'Asset {key:05d}',
                         imageWidth=side, imageHeight=side, tags=[], rating=0, notes='',
                         filename=path.name)
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, 'pixmap', 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
            db.execute('INSERT INTO sqlar VALUES(?,?)', (key, payload))
    scope = ProjectOpenScope(source, tmp_path / 'benchmark.sqlite')
    assert scope.build().complete
    return scope, sizes


def install(main_window, scope):
    scene = main_window.view.scene
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(id='canvas-a', kind='canvas', title='A')]
    panel = main_window.view.category_panel
    panel.catalog = ProjectAssetCatalog(
        scene, panel.resource_service, scope_provider=(lambda: scope))
    return panel


def test_candidate_budget_moves_records_sources_and_decoded_bytes(main_window, tmp_path, monkeypatch):
    scope, sizes = build_project(tmp_path, RECORDS)
    panel = install(main_window, scope)
    controller = main_window.search_controller.visual_search
    controller.candidate_limit = BUDGET

    # ── the old behaviour: every matching row ──────────────────────────────
    started = time.perf_counter()
    untrimmed = panel.catalog.query(limit=None)
    untrimmed_seconds = time.perf_counter() - started
    untrimmed_ids = [int(row.item.save_id) for row in untrimmed]

    # ── the new behaviour: the controller trims before the worker ──────────
    sent = []

    def fake_search(query, assets):
        sent.append(list(assets))
        return 'benchmark-round'

    monkeypatch.setattr(controller.service, 'search', fake_search)
    monkeypatch.setattr(controller.service, 'stop', lambda: None)
    monkeypatch.setattr(main_window.asset_browser, 'open', lambda *a, **k: None)

    started = time.perf_counter()
    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()
    trimmed_seconds = time.perf_counter() - started

    trimmed_ids = [int(key) for key in controller.item_map]
    untrimmed_bytes = sum(sizes[key] for key in untrimmed_ids)
    trimmed_bytes = sum(sizes[key] for key in trimmed_ids)

    artifact = dict(
        environment=dict(
            note='synthetic project on the local disk; NOT the 51.8 GiB stress project',
            logical_cpus=os.cpu_count(),
            python=sys.version.split()[0],
            sqlite=sqlite3.sqlite_version,
            platform=platform.platform(),
            ml_worker_installed=False),
        scale=dict(records=RECORDS, unique_sources=len(set(sizes)),
                   payload_side_px=SIDE,
                   payload_bytes_each_min=min(sizes.values()),
                   payload_bytes_each_max=max(sizes.values())),
        budget=BUDGET,
        decoded_bytes=dict(
            untrimmed=untrimmed_bytes,
            trimmed=trimmed_bytes,
            saved=untrimmed_bytes - trimmed_bytes,
            ratio=(untrimmed_bytes / trimmed_bytes) if trimmed_bytes else None),
        candidate_rows=dict(untrimmed=len(untrimmed_ids), trimmed=len(trimmed_ids),
                            handed_to_worker=len(sent[0]) if sent else 0),
        query_seconds=dict(untrimmed=round(untrimmed_seconds, 4),
                           trimmed_round=round(trimmed_seconds, 4)),
        source_project=str(scope.project_path),
        source_project_bytes=scope.project_path.stat().st_size,
        ui_text=controller.search.info.text(),
        candidate_truncated=controller.candidate_truncated,
        candidate_total=controller.candidate_total,
    )
    ARTIFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTIFACT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding='utf-8')

    # The trimming really moved the bytes, and it really is the same data.
    assert artifact['scale']['records'] == RECORDS
    assert len(trimmed_ids) == BUDGET < RECORDS
    assert len(sent) == 1 and len(sent[0]) == BUDGET
    assert trimmed_bytes < untrimmed_bytes
    assert set(trimmed_ids) <= set(untrimmed_ids)
    assert artifact['environment']['logical_cpus']
