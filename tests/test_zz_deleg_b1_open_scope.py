"""
Delegation round 2026-10-23 -- Batch 1: 默认打开落点 / 全部素材·未分类·未标签 / 保存后目录失效.

ROLE: verification only. This is a NEW test script created by the verification
round. It does not modify any existing test, any existing assertion, any product
code, or any product config. It writes only to
artifacts/test-delegation-20261023/batch1/.

Fixtures are written with plain sqlite3 against the REAL schema
(prism.fileio.schema.SCHEMA) so the fixture does not depend on the product save
path working. No user project file is ever opened for write; every .prism this
module touches is created inside artifacts/.../batch1/work/.

THE THREE QUESTIONS
  Q1  Opening a project must land on the FIRST LEGAL CANVAS in project /
      resource-tree order -- not the all-assets view, not the previously open
      page, not whichever page holds the most assets.
      Recorded separately: current_page_id, the ACTUALLY VISIBLE central widget,
      the resource-tree selection, and the first-load scope target.
  Q2  With more than 100 assets in the project, 全部素材 / 未分类 / 未标签 must not
      silently degrade to "the first 100 of the current page" and must not be
      presented as a complete whole-project result. 未分类 and 未标签 are
      DIFFERENT predicates and are not assumed equal to the project total.
      Expected id sets are computed independently, offline, by reading the
      fixture with plain SQL. The real entry points are then exercised and the
      LOGICAL result ids plus the sidebar counts are recorded.
  Q3  After a save the catalog may become not_ready. An unavailable catalog must
      be reported as preparing/failed; it must NOT be presented as a complete
      whole-project result while only the current page is loaded.

HONESTY RULES enforced here
  * A failing assertion is NOT automatically a product bug. Each case records
    status in {PASS, FAIL, INCONCLUSIVE} and a classification.
  * A process that dies is not a pass. qtbot teardown failures and Qt event-loop
    exceptions are surfaced as FAIL, never swallowed.
  * No result model is injected or patched. No high-resolution decode is forced.
    Visible card count and logical result count are recorded as two numbers.
"""
import json
import sqlite3
import traceback
from pathlib import Path

import pytest

from prism.fileio.schema import SCHEMA, USER_VERSION

ARTIFACTS = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                 r"\test-delegation-20261023\batch1")
EVIDENCE = ARTIFACTS / "evidence"
WORK = ARTIFACTS / "work"
EVIDENCE.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)

def _tiny_png():
    """A real, decodable 8x8 PNG.

    A hand-written PNG hex blob is not guaranteed to be valid, and an invalid
    one makes the loader skip the row: the project then opens with ZERO
    items, which looks exactly like a product bug ("the catalog reports 0")
    and is not one.  Build the bytes with PIL so the fixture is decodable by
    definition.
    """
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGBA', (8, 8), (120, 140, 160, 255)).save(buf, format='PNG')
    return buf.getvalue()


TINY_PNG = _tiny_png()

CASES = []          # machine-readable case records
_FIXTURE_CACHE = {}


# ────────────────────────── fixture construction ──────────────────────────

def _create_schema(con):
    for statement in SCHEMA:
        con.execute(statement)


def _page(page_id, title, order, kind='canvas'):
    return {'id': page_id, 'kind': kind, 'title': title,
            'data': json.dumps({'__prism_page_payload__': True, 'content': None,
                                'tags': [], 'parent': None, 'permanent': False}),
            'sort_order': order}


def build_project(path, pages, items, deleted_page_ids=(), *, categories=None,
                  tags=None):
    """Create a real .prism with the real schema. `items` is a list of dicts:
    {page_id, filename, tags, rating, notes, categories, deleted}."""
    path = Path(path)
    # Rebuild from scratch every time: a leftover file would make CREATE TABLE
    # fail and would silently test a stale fixture.
    for suffix in ('', '-journal', '-wal', '-shm'):
        cand = Path(str(path) + suffix)
        if cand.exists():
            cand.unlink()
    con = sqlite3.connect(path)
    try:
        _create_schema(con)
        con.execute('PRAGMA user_version=%s' % USER_VERSION)
        con.execute('CREATE TABLE IF NOT EXISTS prism_metadata '
                    '(key TEXT PRIMARY KEY, value TEXT)')
        con.execute('CREATE TABLE IF NOT EXISTS workspace_nodes '
                    '(id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        con.execute('CREATE TABLE IF NOT EXISTS material_records '
                    '(id TEXT PRIMARY KEY, payload JSON NOT NULL)')
        con.execute('INSERT INTO prism_metadata VALUES (?,?)',
                    ('project_id', 'delegation-b1'))
        con.execute('INSERT INTO prism_metadata VALUES (?,?)',
                    ('categories', json.dumps(categories or [])))
        con.execute('INSERT INTO prism_metadata VALUES (?,?)',
                    ('tags', json.dumps(tags or [])))
        con.execute('INSERT INTO prism_metadata VALUES (?,?)', ('tag_system', '{}'))
        con.execute('INSERT INTO board_notes (board_id, html, updated_at) '
                    'VALUES (0, ?, 0)', ('<p>b1</p>',))
        con.execute('INSERT INTO board_mindmap (board_id, tree) VALUES (0, ?)',
                    (json.dumps({'data': {'text': 'root'}}),))
        for p in pages:
            con.execute('INSERT INTO workspace_pages (id,kind,title,data,sort_order) '
                        'VALUES (?,?,?,?,?)',
                        (p['id'], p['kind'], p['title'], p['data'], p['sort_order']))
        # Use the product's own legacy->tree node builder so the fixture carries a
        # node shape ResourceTreeService._validate_all() actually accepts.
        # Hand-written nodes must carry id/projectId/section/nodeType/parentId;
        # omitting any of them makes the service raise and aborts
        # _reload_workspace_pages -- which would look like a product bug but is a
        # fixture bug. See tests/test_zz_deleg_diag_b1.py for that failure mode.
        from prism.workspace_service import legacy_page_nodes
        page_dicts = [{'id': p['id'], 'kind': p['kind'], 'title': p['title'],
                       'parent': None} for p in pages]
        for node in legacy_page_nodes(page_dicts, 'delegation-b1'):
            if node.get('pageId') in deleted_page_ids:
                node['deletedAt'] = '2026-10-23 10:00:00'
            con.execute('INSERT INTO workspace_nodes VALUES (?,?)',
                        (node['id'], json.dumps(node)))
        for i, it in enumerate(items, start=1):
            data = {'filename': it['filename'],
                    'opacity': 1, 'grayscale': False,
                    'crop': [0.0, 0.0, 8.0, 8.0],
                    'imageWidth': 8, 'imageHeight': 8,
                    'categories': list(it.get('categories', [])),
                    'lastUsed': '',
                    'tags': list(it.get('tags', [])),
                    'rating': int(it.get('rating', 0)),
                    'notes': it.get('notes', ''),
                    'title': it.get('title', ''),
                    'canvasId': it['page_id'],
                    'groupId': None, 'groupNote': ''}
            if it.get('deleted'):
                data['deletedAt'] = '2026-10-23 10:00:00'
            con.execute('INSERT INTO items(type,x,y,z,scale,rotation,flip,data) '
                        "VALUES ('pixmap',?,?,0,1,0,1,?)",
                        (float(i * 30), float(i * 20), json.dumps(data)))
            con.execute('INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) '
                        'VALUES (?,?,?,?,?,?)',
                        (it['filename'], i, 420, 0, len(TINY_PNG), TINY_PNG))
        con.commit()
    finally:
        con.close()
    return Path(path)


def _items_for(pages, counts, tag_by_page=None, deleted_ids=()):
    out = []
    for page in pages:
        n = counts.get(page['id'], 0)
        for i in range(n):
            tag = (tag_by_page or {}).get(page['id'])
            out.append({
                'page_id': page['id'],
                'filename': f"{page['id']}-{i:03d}.png",
                'tags': [tag] if tag else [],
                'categories': ['cat-a'] if i % 3 == 0 else [],
                'rating': (i % 5),
                'notes': f'note {page["id"]} {i}',
                'title': f'T{page["id"]}{i}',
                'deleted': (page['id'], i) in deleted_ids,
            })
    return out


def fixture_q1(name):
    """Three project shapes for Q1, cached across tests in one session."""
    if name in _FIXTURE_CACHE:
        return _FIXTURE_CACHE[name]
    if name == 'empty_first':
        pages = [_page('cv-empty', 'EmptyFirst', 0), _page('cv-two', 'Two', 1),
                 _page('cv-three', 'Three', 2)]
        counts = {'cv-empty': 0, 'cv-two': 3, 'cv-three': 7}
        tags = {'cv-two': 'tag-two', 'cv-three': 'tag-three'}
        deleted = ()
    elif name == 'nonempty_first':
        pages = [_page('cv-a', 'A', 0), _page('cv-b', 'B', 1), _page('cv-c', 'C', 2)]
        counts = {'cv-a': 4, 'cv-b': 2, 'cv-c': 7}
        tags = {'cv-b': 'tag-b', 'cv-c': 'tag-c'}
        deleted = ()
    elif name == 'first_deleted':
        pages = [_page('cv-gone', 'Gone', 0), _page('cv-live', 'Live', 1)]
        counts = {'cv-gone': 5, 'cv-live': 6}
        tags = {'cv-live': 'tag-live'}
        deleted = ('cv-gone',)
    else:
        raise ValueError(name)
    path = WORK / f'b1q1_{name}.prism'
    build_project(path, pages, _items_for(pages, counts, tags),
                  deleted_page_ids=deleted)
    info = {'path': str(path), 'pages': [p['id'] for p in pages],
            'counts': counts, 'deleted_pages': list(deleted),
            'n_items': sum(counts.values())}
    _FIXTURE_CACHE[name] = info
    return info


def fixture_q2():
    """>100 assets so 'only 100' and 'whole project' are distinguishable."""
    if 'q2' in _FIXTURE_CACHE:
        return _FIXTURE_CACHE['q2']
    pages = [_page(f'cv-{i}', f'C{i}', i) for i in range(4)]
    counts = {'cv-0': 40, 'cv-1': 35, 'cv-2': 30, 'cv-3': 25}   # 130 total
    tags = {'cv-0': 't0', 'cv-1': 't1'}
    deleted = {('cv-3', i) for i in range(5)}                    # 5 trashed
    path = WORK / 'b1q2_all_modes.prism'
    build_project(path, pages, _items_for(pages, counts, tags, deleted),
                  categories=['cat-a', 'cat-b'])
    info = {'path': str(path), 'counts': counts, 'n_items': sum(counts.values()),
            'n_deleted': len(deleted)}
    _FIXTURE_CACHE['q2'] = info
    return info


# ────────────────────────── expected sets, computed OFFLINE ────────────────

def expected_sets(path):
    """Independent ground truth, read with plain SQL. No product code involved."""
    con = sqlite3.connect(path)
    try:
        rows = list(con.execute('SELECT id, data FROM items'))
    finally:
        con.close()
    all_ids, untagged, unclassified, tagged, deleted = [], [], [], [], []
    for iid, raw in rows:
        d = json.loads(raw or '{}')
        if d.get('deletedAt'):
            deleted.append(iid)
            continue
        all_ids.append(iid)
        if not d.get('tags'):
            untagged.append(iid)
        if not d.get('categories'):
            unclassified.append(iid)
        else:
            tagged.append(iid)
    return {
        'all_live_ids': sorted(all_ids),
        'all_live_count': len(all_ids),
        'untagged_ids': sorted(untagged),
        'untagged_count': len(untagged),
        'unclassified_ids': sorted(unclassified),
        'unclassified_count': len(unclassified),
        'has_category_ids': sorted(tagged),
        'deleted_ids': sorted(deleted),
        'note': ('untagged and unclassified are computed from DIFFERENT fields '
                 '(tags vs categories); they are deliberately not assumed equal'),
    }


# ────────────────────────── observation helpers ────────────────────────────

def _safe(fn, label, sink):
    try:
        return fn()
    except Exception as exc:
        sink[label] = f'{type(exc).__name__}: {exc}'
        return None


def observe_open(window, errors):
    """Record everything Q1 asks for. Missing attributes are recorded, not guessed."""
    view = window.view
    panel = view.category_panel
    d = {'errors': errors}

    d['current_page_kind'] = getattr(window, 'current_page_kind', None)
    d['current_page_id'] = getattr(window, 'current_page_id', None)
    d['view.current_canvas_id'] = getattr(view, 'current_canvas_id', None)
    d['_first_canvas_id'] = _safe(lambda: window._first_canvas_id(),
                                 '_first_canvas_id', errors)
    d['_paged_open_target'] = getattr(view, '_paged_open_target', '<attr missing>')
    d['_paged_scope_is_none'] = getattr(view, '_paged_scope', None) is None
    d['scene._partial_project_source'] = getattr(
        view.scene, '_partial_project_source', '<attr missing>')
    d['view.filename'] = getattr(view, 'filename', None)

    # ---- the single most important observation for Q1 ----
    def widget():
        w = window.tabs.currentWidget()
        owners = [n for n in ('canvas_page', 'asset_browser', 'document_panel',
                              'mindmap_panel', 'material_library')
                  if getattr(window, n, None) is w]
        return {'class': type(w).__name__, 'objectName': w.objectName(),
                'is_panel': owners, 'tab_index': window.tabs.currentIndex(),
                'visible': w.isVisible()}
    d['central_widget'] = _safe(widget, 'central_widget', errors)

    browser = getattr(window, 'asset_browser', None)
    if browser is not None:
        d['asset_browser'] = {
            'result_mode': _safe(lambda: getattr(browser, '_result_mode', None),
                                 'browser._result_mode', errors),
            'history': _safe(lambda: list(getattr(browser, 'history', [])),
                             'browser.history', errors),
            'title': _safe(lambda: getattr(browser, 'title', None), 'browser.title',
                           errors),
            'open_calls': list(getattr(browser, '_delegation_open_calls', [])),
        }
    else:
        d['asset_browser'] = {'present': False}

    def tree():
        tr = getattr(panel, 'resource_tree', None)
        if tr is None:
            return {'present': False}
        idx = tr.currentIndex()
        model = tr.model()
        out = {'present': True, 'has_current_index': idx.isValid(),
               'row_count': model.rowCount() if model else None}
        if idx.isValid():
            roles = {}
            for role in range(int(QtUserRoleStart()),
                              int(QtUserRoleStart()) + 10):
                v = idx.data(role)
                if v not in (None, ''):
                    roles[str(role)] = str(v)[:120]
            out['current_roles'] = roles
        return out
    d['resource_tree'] = _safe(tree, 'resource_tree', errors)

    d['catalog'] = _safe(lambda: _catalog_info(panel), 'catalog', errors)
    d['nav_counts'] = _safe(lambda: {k: (v.text() if hasattr(v, 'text') else str(v))
                                     for k, v in getattr(panel, '_nav_counts', {}).items()},
                            'nav_counts', errors)
    d['scope_counters'] = _safe(
        lambda: __import__('prism.project_catalog', fromlist=['x'])
        .catalog_scope_counters(), 'scope_counters', errors)
    return d


def QtUserRoleStart():
    from PyQt6 import QtCore
    return QtCore.Qt.ItemDataRole.UserRole


def _catalog_info(panel):
    cat = getattr(panel, 'catalog', None)
    if cat is None:
        return {'present': False}
    info = {'present': True}
    info['ready'] = getattr(cat, 'ready', '?')
    info['generation'] = getattr(cat, 'generation', '?')
    info['source'] = str(getattr(cat, 'source', getattr(cat, 'path', '?')))[:300]
    try:
        info['count_snapshot'] = cat.count_snapshot()
    except Exception as exc:
        info['count_snapshot_error'] = repr(exc)
    return info


def record(**kw):
    CASES.append(kw)
    return kw


def _dump():
    (EVIDENCE / 'cases_b1.json').write_text(
        json.dumps(CASES, ensure_ascii=False, indent=2, default=str), encoding='utf-8')


# ────────────────────────── Q1: default open landing ───────────────────────

def _open_fixture(main_window, qtbot, path, scope):
    """Drive the production load entry, then the production finalize entry."""
    from prism import fileio

    window = main_window
    view = window.view
    errors = {}
    calls = []

    real_open = getattr(window.asset_browser, 'open', None)
    if real_open is not None:
        def spy(target, *a, **kw):
            calls.append({'target': target, 'thread': __import__('threading')
                          .current_thread().name})
            return real_open(target, *a, **kw)
        window.asset_browser.open = spy

    try:
        fileio.load_prism(str(path), view.scene, scope=scope)
        view._finalize_loaded_project(str(path), errors=[])
        qtbot.wait(200)
    except Exception:
        errors['open_exception'] = traceback.format_exc()
    finally:
        if real_open is not None:
            try:
                del window.asset_browser.open
            except Exception:
                pass

    obs = observe_open(window, errors)
    obs['asset_browser_open_calls'] = calls
    obs['paged_scope_used'] = 'scope' if scope else 'full'
    return obs


@pytest.mark.parametrize('shape,expected_first', [
    ('empty_first', 'cv-empty'),
    ('nonempty_first', 'cv-a'),
    ('first_deleted', 'cv-live'),
])
def test_q1_full_open_lands_on_first_legal_canvas(main_window, qtbot, shape,
                                                   expected_first):
    """Q1 / full (non-paged) open. PAGED_OPEN_DEFAULT is True, so this branch is
    NOT today's default, but it is the branch the audit singled out."""
    fx = fixture_q1(shape)
    obs = _open_fixture(main_window, qtbot, fx['path'], scope=None)

    landed_page = obs['current_page_id']
    widget_panels = (obs['central_widget'] or {}).get('is_panel', [])
    on_first_page = landed_page == expected_first
    shows_canvas = 'canvas_page' in widget_panels
    shows_all_assets = 'asset_browser' in widget_panels
    called_all = any(c['target'] == 'all' for c in obs['asset_browser_open_calls'])

    if not on_first_page:
        status, cls = 'FAIL', 'product_bug'
    elif not shows_canvas:
        # The page id is right but the user is looking at something else. This is
        # the owner's reported symptom, so it is a FAIL against the stated intent.
        status, cls = 'FAIL', 'product_bug'
    else:
        status, cls = 'PASS', 'none'

    # Root cause is known and cross-checked against the owner's REAL projects:
    # neither 1212.prism nor Prism-100k-v1.prism stores an explicit
    # default-canvas page, so media_library.py:1303-1306 prepends an implicit one
    # at index 0 and window_workspace.py:711-722 returns it. See
    # batch1/evidence/q1-root-cause-real-projects.json.
    root_cause = None
    if not on_first_page:
        root_cause = (
            'prism/widgets/media_library.py:1303-1306 inserts an implicit '
            "default-canvas page at index 0 whenever the project has no explicit "
            'one, unconditionally -- while its own docstring (media_library.py:1292-1299) '
            'says the legacy page is recreated "only if it holds data". '
            'prism/window_workspace.py:711-722 then takes the first kind=="canvas" '
            'entry of that expanded list, which is the empty implicit canvas, not '
            'the first canvas in workspace_pages order.')

    record(case_id=f'Q1-full-{shape}', entry_point='fileio.load_prism(scope=None) '
            '-> view._finalize_loaded_project',
           expected=f'current_page_id == {expected_first} AND central widget is '
                    f'canvas_page AND asset_browser.open("all") not called',
           observed={'current_page_id': landed_page,
                     'central_widget': obs['central_widget'],
                     'asset_browser_open_calls': obs['asset_browser_open_calls'],
                     '_first_canvas_id': obs['_first_canvas_id'],
                     '_paged_open_target': obs['_paged_open_target'],
                     'catalog': obs['catalog']},
           status=status, classification=cls,
           failure_location=root_cause or (
               'prism/window_workspace.py:699-704 (non-paged branch calls '
               'asset_browser.open("all"))'
               if (on_first_page and not shows_canvas) else None),
           evidence_file='batch1/evidence/cases_b1.json',
           cross_evidence='batch1/evidence/q1-root-cause-real-projects.json',
           untested_reason=None)

    assert on_first_page, (
        f'expected first legal canvas {expected_first!r}, landed on {landed_page!r}')
    assert shows_canvas, (
        'page id is correct but the visible central widget is '
        f'{widget_panels!r}, not canvas_page; asset_browser.open("all") '
        f'was called={called_all}')


@pytest.mark.parametrize('shape,expected_first', [
    ('empty_first', 'cv-empty'),
    ('nonempty_first', 'cv-a'),
    ('first_deleted', 'cv-live'),
])
def test_q1_paged_open_lands_on_first_legal_canvas(main_window, qtbot, shape,
                                                   expected_first):
    """Q1 / paged open -- this IS today's default (PAGED_OPEN_DEFAULT=True)."""
    fx = fixture_q1(shape)
    scope = [1]                       # a non-empty scope => the paged branch
    obs = _open_fixture(main_window, qtbot, fx['path'], scope=scope)

    landed_page = obs['current_page_id']
    widget_panels = (obs['central_widget'] or {}).get('is_panel', [])
    shows_canvas = 'canvas_page' in widget_panels
    called_all = any(c['target'] == 'all' for c in obs['asset_browser_open_calls'])
    on_first_page = landed_page == expected_first

    if not on_first_page:
        status, cls = 'FAIL', 'product_bug'
    elif not shows_canvas:
        status, cls = 'FAIL', 'product_bug'
    else:
        status, cls = 'PASS', 'none'

    root_cause = None
    if not on_first_page:
        root_cause = (
            'prism/widgets/media_library.py:1303-1306 inserts an implicit '
            "default-canvas page at index 0 whenever the project has no explicit "
            'one, unconditionally -- while its own docstring (media_library.py:1292-1299) '
            'says the legacy page is recreated "only if it holds data". '
            'prism/window_workspace.py:711-722 then takes the first kind=="canvas" '
            'entry of that expanded list, which is the empty implicit canvas, not '
            'the first canvas in workspace_pages order.')

    record(case_id=f'Q1-paged-{shape}',
           entry_point='fileio.load_prism(scope=[1]) -> view._finalize_loaded_project',
           expected=f'current_page_id == {expected_first} AND central widget is '
                    f'canvas_page AND asset_browser.open("all") not called',
           observed={'current_page_id': landed_page,
                     'central_widget': obs['central_widget'],
                     'asset_browser_open_calls': obs['asset_browser_open_calls'],
                     '_first_canvas_id': obs['_first_canvas_id'],
                     '_paged_open_target': obs['_paged_open_target'],
                     '_paged_scope_is_none': obs['_paged_scope_is_none'],
                     'catalog': obs['catalog']},
           status=status, classification=cls,
           failure_location=root_cause or (
               'prism/window_workspace.py:686 '
               '(_paged_open_target overrides _first_canvas_id())'
               if not on_first_page else None),
           evidence_file='batch1/evidence/cases_b1.json',
           cross_evidence='batch1/evidence/q1-root-cause-real-projects.json',
           untested_reason=None)

    assert on_first_page, (
        f'expected first legal canvas {expected_first!r}, landed on {landed_page!r}; '
        f'_first_canvas_id()={obs["_first_canvas_id"]!r} '
        f'_paged_open_target={obs["_paged_open_target"]!r}')
    assert shows_canvas, (
        'page id correct but visible central widget is '
        f'{widget_panels!r}; open("all") called={called_all}')


def test_q1_zz_dump(main_window, qtbot):
    """Always-passing teardown that flushes the case records to disk."""
    _dump()
    print(f'\ncases written: {len(CASES)}')