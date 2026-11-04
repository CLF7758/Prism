"""
Delegation round 2026-10-23 -- PROBE (discovery only, no assertions).

Purpose: discover the real attribute names and the real observed behaviour of the
"which page does a project open land on" path, so the real test module
(test_bzz_deleg_b1_open_scope.py) asserts against facts instead of guesses.

This file is a NEW test script created by the verification round. It does not
modify any existing test, any product code, or any existing assertion. It writes
only to artifacts/test-delegation-20261023/batch1/evidence/.

It contains NO assertions on purpose: its job is to dump state and tell us what
is true. Pass/fail judgement belongs in the follow-up module.
"""
import json
import sqlite3
import traceback
from pathlib import Path

import pytest

from PyQt6 import QtCore, QtGui

from prism.items import PrismPixmapItem

ARTIFACTS = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                 r"\test-delegation-20261023\batch1")
EVIDENCE = ARTIFACTS / "evidence"
WORK = ARTIFACTS / "work"
EVIDENCE.mkdir(parents=True, exist_ok=True)
WORK.mkdir(parents=True, exist_ok=True)

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000001b4944415478da63fcffff3f0326c8503084898d4889c8c0c0a00c00c00c0"
    "01c0d0a70100fc0b40b0f0f0000000049454e44ae426082"
)


def _canvas(page_id, title, order):
    return {'id': page_id, 'kind': 'canvas', 'title': title,
            'content': None, 'tags': [], 'parent': None, 'permanent': False}


def _tree_node(node_id, page_id, name, parent_id=None, deleted=False):
    payload = {'id': node_id, 'type': 'folder', 'name': name,
               'parentId': parent_id}
    if page_id:
        payload['pageId'] = page_id
    if deleted:
        payload['deletedAt'] = '2026-10-23 10:00:00'
    return payload


def build_project(path, *, pages, item_plan, deleted_first=False):
    """Create a real .prism through the product's own save path."""
    from PyQt6 import QtWidgets

    app = QtWidgets.QApplication.instance()
    from prism.__main__ import PrismMainWindow

    window = PrismMainWindow(app)
    try:
        view = window.view
        scene = view.scene
        scene.workspace_pages = list(pages)
        # resource tree nodes mirror the pages; a soft-deleted node carries deletedAt
        nodes = []
        for i, page in enumerate(pages):
            nodes.append(_tree_node(f'node-{page["id"]}', page['id'], page['title'],
                                    deleted=(deleted_first and i == 0)))
        scene.workspace_nodes = nodes
        window._sync_page_panels()
        view.category_panel.rebuild_workspace_tree()

        for canvas_id, count, tag in item_plan:
            window.open_workspace_page('canvas', canvas_id)
            for i in range(count):
                item = PrismPixmapItem(QtGui.QImage(8, 8,
                                                    QtGui.QImage.Format.Format_RGB32),
                                       f'{canvas_id}-{i:02d}.png')
                item._canvas_id = canvas_id
                if tag:
                    item._tags = [tag]
                item.setPos(i * 30.0, i * 20.0)
                scene.addItem(item)
        view.mark_content_dirty()
        view.do_save(str(path), create_new=True, silent=True)
        return str(path)
    finally:
        window.undo_stack.setClean()
        window.close()


def read_project_facts(path):
    """Independent read-back of what actually landed on disk."""
    out = {'exists': Path(path).is_file()}
    if not out['exists']:
        return out
    out['size_bytes'] = Path(path).stat().st_size
    con = sqlite3.connect(path)
    try:
        out['tables'] = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        pages = []
        for pid, kind, title, data, order in con.execute(
                'SELECT id, kind, title, data, sort_order FROM workspace_pages '
                'ORDER BY sort_order'):
            pages.append({'id': pid, 'kind': kind, 'title': title,
                          'sort_order': order,
                          'data_keys': sorted(json.loads(data or '{}').keys())})
        out['pages_in_stored_order'] = pages
        try:
            out['nodes'] = [json.loads(r[0]) if isinstance(r[0], str) else r[0]
                            for r in con.execute('SELECT payload FROM workspace_nodes')]
        except sqlite3.Error as exc:
            out['nodes_error'] = repr(exc)
        try:
            out['n_items'] = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
            out['items'] = []
            for iid, typ, data in con.execute('SELECT id, type, data FROM items ORDER BY id'):
                extra = json.loads(data or '{}')
                out['items'].append({'id': iid, 'type': typ,
                                     'canvasId': extra.get('canvasId'),
                                     'tags': extra.get('tags'),
                                     'filename': extra.get('filename'),
                                     'data_keys': sorted(extra.keys())})
        except sqlite3.Error as exc:
            out['items_error'] = repr(exc)
    finally:
        con.close()
    return out


def describe_open_state(window):
    """Dump everything Batch 1 question 1 asks for, tolerating missing attributes."""
    view = window.view
    panel = view.category_panel
    browser = getattr(window, 'asset_browser', None)
    d = {}

    def safe(fn, label):
        try:
            return fn()
        except Exception as exc:
            d.setdefault('errors', {})[label] = repr(exc)
            return f'ERROR: {exc!r}'

    d['current_page_kind'] = getattr(window, 'current_page_kind', None)
    d['current_page_id'] = getattr(window, 'current_page_id', None)
    d['view.current_canvas_id'] = getattr(view, 'current_canvas_id', None)
    d['_first_canvas_id'] = safe(lambda: window._first_canvas_id(), '_first_canvas_id')
    d['_paged_open_target'] = getattr(view, '_paged_open_target', '<missing>')
    d['_paged_scope'] = safe(lambda: getattr(view, '_paged_scope', '<missing>'),
                             '_paged_scope')
    d['_paged_scope_is_none'] = getattr(view, '_paged_scope', None) is None
    d['scene._partial_project_source'] = getattr(
        view.scene, '_partial_project_source', '<missing>')
    d['view.filename'] = getattr(view, 'filename', None)

    # --- the actually visible central widget: the key observation ---
    def widget_info():
        w = window.tabs.currentWidget()
        owner = []
        for name in ('canvas_page', 'asset_browser', 'document_panel',
                     'mindmap_panel', 'material_library'):
            if getattr(window, name, None) is w:
                owner.append(name)
        for name in ('canvas_page', 'asset_browser', 'document_panel',
                     'mindmap_panel', 'material_library'):
            if getattr(view, name, None) is w:
                owner.append('view.' + name)
        return {'class': type(w).__name__, 'objectName': w.objectName(),
                'is_panel': owner,
                'visible': w.isVisible()}
    d['central_widget'] = safe(widget_info, 'central_widget')

    if browser is not None:
        db = {}
        db['_result_mode'] = safe(lambda: getattr(browser, '_result_mode', '<missing>'),
                                 'browser._result_mode')
        db['history'] = safe(lambda: list(getattr(browser, 'history', [])), 'history')
        db['history_index'] = safe(lambda: getattr(browser, 'history_index', '?'),
                                   'history_index')
        db['scope_pages'] = safe(lambda: getattr(browser, 'scope_pages', '<missing>'),
                                 'scope_pages')
        db['title'] = safe(lambda: getattr(browser, 'title', '?'), 'title')
        db['row_count_attr'] = safe(lambda: len(getattr(browser, 'items', [])),
                                    'browser.items')
        db['open_all_seen'] = getattr(browser, '_delegation_open_all_calls', None)
        d['asset_browser'] = db

    # --- resource tree selection ---
    def tree_info():
        tree = getattr(panel, 'resource_tree', None)
        if tree is None:
            return 'no resource_tree'
        idx = tree.currentIndex()
        model = tree.model()
        out = {'has_index': idx.isValid()}
        if idx.isValid():
            src = model.data(idx, 0)
            out['display'] = str(src)[:200] if src is not None else None
            for role_name in ('pageId', 'nodeId'):
                for role in range(QtCore.Qt.ItemDataRole.UserRole,
                                  QtCore.Qt.ItemDataRole.UserRole + 8):
                    val = idx.data(role)
                    if val not in (None, ''):
                        out.setdefault('roles', {})[f'{role_name}@role{role}'] = str(val)[:120]
        rows = model.rowCount(QtCore.QModelIndex()) if model else 0
        out['row_count'] = rows
        return out
    d['resource_tree'] = safe(tree_info, 'resource_tree')

    # --- nav counts / catalog ---
    def catalog_info():
        cat = getattr(panel, 'catalog', None)
        if cat is None:
            return {'catalog': None}
        info = {'catalog_present': True,
                'ready': safe(lambda: getattr(cat, 'ready', '?'), 'cat.ready'),
                'generation': safe(lambda: getattr(cat, 'generation', '?'),
                                   'cat.generation')}
        snap = safe(lambda: cat.count_snapshot(), 'count_snapshot')
        info['count_snapshot'] = snap
        return info
    d['catalog'] = safe(catalog_info, 'catalog')

    def nav_counts():
        return {k: (v.text() if hasattr(v, 'text') else str(v))
                for k, v in getattr(panel, '_nav_counts', {}).items()}
    d['nav_counts'] = safe(nav_counts, 'nav_counts')

    def scope_counters():
        from prism.project_catalog import catalog_scope_counters
        return catalog_scope_counters()
    d['scope_counters'] = safe(scope_counters, 'scope_counters')

    return d


def test_probe_default_open_landing(qtbot, tmp_path):
    """SUPERSEDED -- kept only as a record of how the fixtures were discovered.

    Do NOT read a green result here as verification. This probe has no assertions
    on purpose, so it passes whatever happens; two known defects were found and
    fixed downstream in the real module:

      * build_project() referenced window.undo_stack, which lives on
        window.view -- and creating a second PrismMainWindow in one process dies
        in actions/mixin.py:113 because the global action registry is polluted.
        Fixtures are therefore built with plain sqlite3 against the real schema
        in tests/test_zz_deleg_b1_open_scope.py instead.
      * hand-written workspace_nodes payloads were missing
        projectId/section/nodeType, so ResourceTreeService._validate_all()
        raised and aborted _reload_workspace_pages. That looked exactly like a
        product defect and was in fact a fixture bug. The real module now uses
        prism.workspace_service.legacy_page_nodes().

    It is skipped so it cannot pollute a later full regression run with a green
    line that verifies nothing.
    """
    pytest.skip('superseded by tests/test_zz_deleg_b1_open_scope.py; no assertions')
    qtbot, tmp_path  # noqa: F841 - kept so the signature documents the old usage
    report = {'probe': 'default_open_landing', 'errors': []}

    shapes = {
        # first canvas EMPTY; most assets on the last page
        'empty_first': dict(
            pages=[_canvas('cv-empty', 'EmptyFirst', 0),
                   _canvas('cv-two', 'Two', 1),
                   _canvas('cv-three', 'Three', 2)],
            item_plan=[('cv-empty', 0, None), ('cv-two', 3, 'tag-two'),
                       ('cv-three', 7, 'tag-three')],
            deleted_first=False),
        # first canvas NOT empty; most assets on the LAST page
        'nonempty_first': dict(
            pages=[_canvas('cv-a', 'A', 0), _canvas('cv-b', 'B', 1),
                   _canvas('cv-c', 'C', 2)],
            item_plan=[('cv-a', 4, None), ('cv-b', 2, 'tag-b'),
                       ('cv-c', 7, 'tag-c')],
            deleted_first=False),
        # first canvas soft-deleted in the resource tree
        'first_deleted': dict(
            pages=[_canvas('cv-gone', 'Gone', 0), _canvas('cv-live', 'Live', 1)],
            item_plan=[('cv-gone', 5, None), ('cv-live', 6, 'tag-live')],
            deleted_first=True),
    }

    report['fixtures'] = {}
    paths = {}
    for name, spec in shapes.items():
        target = WORK / f'b1_{name}.prism'
        try:
            build_project(target, **spec)
            paths[name] = str(target)
            report['fixtures'][name] = read_project_facts(str(target))
        except Exception:
            report['fixtures'][name] = {'build_error': traceback.format_exc()}

    report['open_states'] = {}
    for name, path in paths.items():
        try:
            from prism.__main__ import PrismMainWindow
            from PyQt6 import QtWidgets
            app = QtWidgets.QApplication.instance()
            window = PrismMainWindow(app)
            try:
                view = window.view
                # Drive the production load entry.
                from prism import fileio
                fn = getattr(fileio, 'load_prism', None)
                report['open_states'][name] = {
                    'load_prism_present': fn is not None,
                    'view_load_attrs': [a for a in dir(view)
                                        if 'load' in a.lower() or 'open' in a.lower()][:40],
                }
            finally:
                window.undo_stack.setClean()
                window.close()
        except Exception:
            report['open_states'][name] = {'error': traceback.format_exc()}

    out = EVIDENCE / 'probe-default-open.json'
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\nprobe written: {out}')
    print(json.dumps(report, ensure_ascii=False, indent=2)[:6000])