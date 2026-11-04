"""以图搜图：先裁候选再 embedding，以及索引不可用时的诚实口径。

These are the D-2 regressions for P10:

* the worker must not receive the whole project any more - the candidate
  set is trimmed to the scope and to a budget before it is embedded;
* the UI must say how many candidates were used, and that they were
  trimmed; an unknown candidate set must never be rendered as "0 found";
* a cancelled or replaced round must not write its result back;
* a scope id must never reach the catalog as a bare string: the catalog
  index splits it into characters and answers "nothing found" without an
  error.

Every assertion below is written as a fact about the production path
(``VisualSearchController.submit`` -> ``VisualSearchService.search``), not
about a helper that only the test calls.
"""
import json
import sqlite3

import pytest
from PyQt6 import QtGui

from prism.project_catalog import ProjectAssetCatalog
from prism.project_open_scope import ProjectOpenScope
from prism.project_search import item_key
from tests.test_asset_browser import photo


CANVASES = [dict(id='canvas-a', kind='canvas', title='A'),
            dict(id='canvas-b', kind='canvas', title='B')]


def image_payload(tmp_path, key, side=16):
    """A real PNG payload, so the byte counts below are real bytes."""
    image = QtGui.QImage(side, side, QtGui.QImage.Format.Format_RGB32)
    image.fill(QtGui.QColor('blue'))
    path = tmp_path / f'src-{key:05d}.png'
    assert image.save(str(path))
    return path.read_bytes()


def records(count, tmp_path, *, canvases=('canvas-a', 'canvas-b'), side=16):
    """`count` catalog records, each with its own source image."""
    out = []
    for key in range(1, count + 1):
        out.append((key, 'pixmap',
                    dict(canvasId=canvases[key % len(canvases)], title=f'Asset {key:05d}',
                         imageWidth=side, imageHeight=side, tags=[], rating=0, notes='',
                         filename=f'src-{key:05d}.png'),
                    image_payload(tmp_path, key, side)))
    return out


def project(main_window, tmp_path, rows, *, directory=True):
    """A partially opened project: one live page, whole-project directory."""
    source = tmp_path / 'visual-candidates.prism'
    with sqlite3.connect(source) as db:
        db.executescript(
            "CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);"
            "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key, kind, extra, payload in rows:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
            db.execute('INSERT INTO sqlar VALUES(?,?)', (key, payload))
    scope = ProjectOpenScope(source, tmp_path / 'visual-candidates.sqlite')
    assert scope.build().complete
    scene = main_window.view.scene
    scene._partial_project_source = str(scope.project_path)
    scene.workspace_pages = [dict(page) for page in CANVASES]
    panel = main_window.view.category_panel
    panel.catalog = ProjectAssetCatalog(
        scene, panel.resource_service,
        scope_provider=(lambda: scope) if directory else (lambda: None))
    return scope, scene, panel


def spy_query(panel, monkeypatch):
    """Record the keyword arguments the catalog query was called with."""
    calls = []
    original = panel.catalog.query

    def spy(**kwargs):
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(panel.catalog, 'query', spy)
    return calls


def fake_service(monkeypatch, controller, names=('round-1',)):
    """Replace the worker call; return the list of captured asset lists."""
    sent = []
    identifiers = iter(names)

    def search(query, assets):
        sent.append(list(assets))
        return next(identifiers, 'round-extra')

    monkeypatch.setattr(controller.service, 'search', search)
    monkeypatch.setattr(controller.service, 'stop', lambda: None)
    return sent


def result_payload(keys, **extra):
    value = dict(results=[dict(key=key, score=0.5) for key in keys],
                 indexed=len(keys), total=len(keys), device='cpu', cached=0, failed=0)
    value.update(extra)
    return value


# ── (1) 先裁候选再 embedding ────────────────────────────────────────────────

def test_candidate_budget_trims_what_reaches_the_worker(main_window, tmp_path, monkeypatch):
    """The worker gets the trimmed set, not the whole project.

    600 catalog records, a budget of 50: the worker must see 50 assets.
    Before the trimming the same round embedded all 600.
    """
    scope, scene, panel = project(main_window, tmp_path, records(600, tmp_path))
    controller = main_window.search_controller.visual_search
    controller.candidate_limit = 50
    sent = fake_service(monkeypatch, controller)
    calls = spy_query(panel, monkeypatch)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()

    assert len(sent) == 1
    assert len(sent[0]) == 50
    assert 50 < 600
    assert controller.candidate_count == 50
    assert controller.candidate_truncated is True
    # The catalog was asked for one row more than the budget: that extra row
    # is the evidence that the candidate set was cut.
    assert calls[0]['limit'] == 51


def test_scope_pushdown_reaches_the_catalog(main_window, tmp_path, monkeypatch):
    """A page scope is pushed down, so only that canvas' rows are read."""
    scope, scene, panel = project(main_window, tmp_path, records(6, tmp_path))
    controller = main_window.search_controller.visual_search
    calls = spy_query(panel, monkeypatch)

    controller.active = True
    controller.search.scope = ('page', ('canvas', 'canvas-a'))
    candidates = controller.candidates()

    assert candidates is not None
    assert calls[0]['canvas_scope'] == ('canvas-a',)
    # Three of the six records live on canvas-a.
    assert candidates.count == 3


def test_the_candidate_set_is_not_cached_between_rounds(main_window, tmp_path, monkeypatch):
    """Every round re-queries: a save that adds an asset is visible at once."""
    scope, scene, panel = project(main_window, tmp_path, records(4, tmp_path))
    controller = main_window.search_controller.visual_search
    calls = spy_query(panel, monkeypatch)

    controller.active = True
    first = controller.candidates()
    second = controller.candidates()

    assert len(calls) == 2
    assert first.count == second.count == 4


# ── (2) UI 明示裁剪后的候选数 ───────────────────────────────────────────────

def test_ui_states_the_trimmed_candidate_count(main_window, tmp_path, monkeypatch):
    """The candidate count is on screen - before and after the round ends."""
    scope, scene, panel = project(main_window, tmp_path, records(600, tmp_path))
    controller = main_window.search_controller.visual_search
    controller.candidate_limit = 50
    sent = fake_service(monkeypatch, controller)
    monkeypatch.setattr(main_window.asset_browser, 'open', lambda *a, **k: None)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()

    running = controller.search.info.text()
    assert '50' in running
    assert '已裁剪到' in running

    controller.finished('round-1', result_payload([]))
    finished_text = controller.search.info.text()
    assert '50' in finished_text
    assert '已裁剪到' in finished_text
    # The tooltip says what was trimmed away: a trimmed round must never read
    # like "the whole library has no match".
    tooltip = controller.search.info.toolTip()
    assert '50' in tooltip
    assert '不代表全库' in tooltip


def test_an_untrimmed_round_says_how_many_candidates_were_searched(main_window, tmp_path, monkeypatch):
    scope, scene, panel = project(main_window, tmp_path, records(4, tmp_path))
    controller = main_window.search_controller.visual_search
    fake_service(monkeypatch, controller)
    monkeypatch.setattr(main_window.asset_browser, 'open', lambda *a, **k: None)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()
    controller.finished('round-1', result_payload([]))

    assert controller.candidate_truncated is False
    assert controller.candidate_total == 4
    assert '候选 4 张' in controller.search.info.text()


# ── (3) 索引不可用时必须诚实 ───────────────────────────────────────────────

def test_unknown_candidate_set_is_reported_not_rendered_as_zero(main_window, tmp_path):
    """No directory: the candidate set is unknown, not empty."""
    scope, scene, panel = project(main_window, tmp_path, records(2, tmp_path), directory=False)
    controller = main_window.search_controller.visual_search

    assert controller.candidates() is None
    text = controller.search.info.text()
    assert '索引准备中' in text
    assert '候选 0' not in text
    assert '0 张' not in text
    assert '未知' in text


def test_a_broken_catalog_is_reported_as_unavailable(main_window, tmp_path, monkeypatch):
    """A failing query must not read as "no matching assets"."""
    scope, scene, panel = project(main_window, tmp_path, records(2, tmp_path))

    def boom(**kwargs):
        raise RuntimeError('catalog exploded')

    monkeypatch.setattr(panel.catalog, 'query', boom)
    controller = main_window.search_controller.visual_search

    assert controller.candidates() is None
    assert '索引不可用' in controller.search.info.text()
    assert 'catalog exploded' in controller.search.info.toolTip()
    assert '0 张' not in controller.search.info.text()


def test_an_unknown_candidate_set_never_reaches_the_worker(main_window, tmp_path, monkeypatch):
    """Unknown set -> nothing is embedded, and no stale round is accepted."""
    scope, scene, panel = project(main_window, tmp_path, records(2, tmp_path), directory=False)
    controller = main_window.search_controller.visual_search
    sent = fake_service(monkeypatch, controller)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()

    assert sent == []
    assert controller.request is None


# ── (4) 点击未加载目标按 ID 加载并定位 ──────────────────────────────────────

def test_clicking_an_unloaded_result_loads_that_id(main_window, tmp_path, monkeypatch, qtbot):
    """An unloaded hit is loaded by id, not by loading the whole canvas.

    ``view.ensure_asset_loaded`` is the main session's hook; the visual
    result path must go through it with the asset's own id.
    """
    scope, scene, panel = project(main_window, tmp_path, records(3, tmp_path))
    controller = main_window.search_controller.visual_search
    controller.active = True
    candidates = controller.candidates()
    assert candidates is not None and candidates.count == 3

    proxy = candidates.items[0]
    assert getattr(proxy, '_catalog_proxy', False)
    assert proxy.save_id is not None

    loaded = []
    monkeypatch.setattr(main_window.view, 'ensure_asset_loaded',
                        lambda item_id: loaded.append(item_id))
    browser = main_window.asset_browser
    browser.visual_results = [(proxy, 0.9)]
    browser.visual_project = scene.project_id
    browser.open('visual', title='以图搜图', record=False)
    qtbot.waitUntil(lambda: browser.model.rowCount() == 1, timeout=20000)

    browser.locate(browser.model.index(0))

    qtbot.waitUntil(lambda: bool(loaded), timeout=5000)
    assert loaded == [str(proxy.save_id)]


# ── (5) 取消 / 被取代后旧结果不回写 ─────────────────────────────────────────

def test_a_cancelled_round_does_not_write_back_its_result(main_window, tmp_path, monkeypatch):
    scope, scene, panel = project(main_window, tmp_path, records(3, tmp_path))
    controller = main_window.search_controller.visual_search
    sent = fake_service(monkeypatch, controller)
    monkeypatch.setattr(main_window.asset_browser, 'open', lambda *a, **k: None)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()
    assert controller.request == 'round-1'

    controller.cancel()
    stale_key = list(controller.item_map)[0]
    controller.finished('round-1', result_payload([stale_key]))

    assert getattr(main_window.asset_browser, 'visual_results', []) == []
    assert '可检索' not in controller.search.info.text()


def test_a_replaced_round_does_not_write_back_its_result(main_window, tmp_path, monkeypatch):
    """The second round wins; the first round's result is dropped."""
    scope, scene, panel = project(main_window, tmp_path, records(3, tmp_path))
    controller = main_window.search_controller.visual_search
    sent = fake_service(monkeypatch, controller, names=('round-1', 'round-2'))
    monkeypatch.setattr(main_window.asset_browser, 'open', lambda *a, **k: None)

    controller.start(str(tmp_path / 'src-00001.png'))
    controller.timer.stop()
    controller.submit()
    assert controller.request == 'round-1'
    controller.submit()
    assert controller.request == 'round-2'

    stale_key = list(controller.item_map)[0]
    controller.finished('round-1', result_payload([stale_key]))
    assert getattr(main_window.asset_browser, 'visual_results', []) == []

    controller.finished('round-2', result_payload([stale_key]))
    assert len(main_window.asset_browser.visual_results) == 1


# ── (6) 保存前后结果集合一致 ───────────────────────────────────────────────

def test_candidate_set_is_the_same_before_and_after_a_save(main_window, tmp_path):
    """Saving must not change the candidate set.

    A save turns the assets from "rows on disk" into "loaded objects"; the
    live overlay replaces the disk rows and the set of candidates stays the
    same.  Added, deleted and renamed assets are the only allowed changes.
    """
    scope, scene, panel = project(main_window, tmp_path, records(6, tmp_path))
    controller = main_window.search_controller.visual_search

    before = {item_key(item) for item in controller.candidates().items}
    assert len(before) == 6

    # The saved state: the same six assets, now loaded into the scene.
    for key in range(1, 7):
        item = photo(main_window, CANVASES[key % 2]['id'], f'Asset {key:05d}')
        item.save_id = key

    after = {item_key(item) for item in controller.candidates().items}
    assert after == before


def test_the_candidate_set_survives_a_catalog_rebuild(main_window, tmp_path):
    """A rewritten project file rebuilds the catalog; the set is unchanged."""
    scope, scene, panel = project(main_window, tmp_path, records(4, tmp_path))
    controller = main_window.search_controller.visual_search

    before = {item_key(item) for item in controller.candidates().items}
    assert len(before) == 4

    with sqlite3.connect(scope.project_path) as db:
        for key in range(1, 5):
            row = db.execute('SELECT data FROM items WHERE id=?', (key,)).fetchone()
            extra = json.loads(row[0])
            extra['title'] = f'Asset {key:05d} saved'
            db.execute('UPDATE items SET data=? WHERE id=?',
                       (json.dumps(extra, ensure_ascii=False), key))
    assert scope.build().complete

    after = {item_key(item) for item in controller.candidates().items}
    assert after == before


# ── (7) 作用域参数传字符串时不得静默查空 ───────────────────────────────────

def test_a_bare_string_scope_selects_that_one_canvas(main_window, tmp_path):
    """A bare string is ONE canvas id -- in all three layers.

    ``project_catalog_index._predicate`` does ``tuple(canvas_ids)``: a bare
    string used to become a tuple of characters, the SQL matched no row, and
    the query answered "no assets" without raising anything -- the silent
    wrong answer. The scope layer (``project_open_scope._as_canvas_ids``)
    and the index layer (``project_catalog_index._canvas_id_tuple``) already
    wrapped a bare string into a one element tuple; **A-1 (round 2) closed
    the third leg**, the scene-side service
    (``project_catalog._as_canvas_ids``), so the three layers now agree:
    a bare string is one canvas id.
    """
    scope, scene, panel = project(main_window, tmp_path, records(4, tmp_path))

    from_string = panel.catalog.query(canvas_scope='canvas-a')
    from_tuple = panel.catalog.query(canvas_scope=('canvas-a',))

    # It does not answer "nothing" any more -- that was the silent wrong
    # answer, and it is what this case used to assert.
    assert from_string
    assert len(from_string) == 2
    # A bare string and the equivalent one element tuple are the same query.
    assert len(from_tuple) == 2
    assert [(row.canvas_id, row.title) for row in from_string] == \
           [(row.canvas_id, row.title) for row in from_tuple]


def test_the_scope_reaches_the_catalog_as_a_tuple(main_window, tmp_path, monkeypatch):
    """The controller never hands a bare string to the catalog."""
    scope, scene, panel = project(main_window, tmp_path, records(6, tmp_path))
    controller = main_window.search_controller.visual_search
    calls = spy_query(panel, monkeypatch)

    controller.active = True
    controller.search.scope = ('page', ('canvas', 'canvas-a'))
    candidates = controller.candidates()

    assert candidates is not None
    scope_argument = calls[0]['canvas_scope']
    assert not isinstance(scope_argument, (str, bytes))
    assert scope_argument == ('canvas-a',)
    # And the answer is the scoped one, not an empty set.
    assert candidates.count == 3
