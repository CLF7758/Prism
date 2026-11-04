"""Delegation round 2026-10-23 -- diagnostic for the Batch 1 fixture path.

Answers one question: why does a project built straight from SCHEMA not appear
in the resource panel's page list? Prints the real state after a load attempt.

Read-only with respect to the product. Creates no product file.
"""
import json
import sqlite3
import traceback
from pathlib import Path

WORK = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
            r"\test-delegation-20261023\batch1\work")
EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")

from prism.fileio.schema import SCHEMA, USER_VERSION

TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000080000000808020000004b6d29dc"
    "0000001b4944415478da63fcffff3f0326c8503084898d4889c8c0c0a00c00c00c0"
    "01c0d0a70100fc0b40b0f0f0000000049454e44ae426082")


def _build(path):
    # Always rebuild from scratch: a leftover file from an earlier run would make
    # CREATE TABLE fail and hide the real diagnostic.
    path = Path(path)
    for suffix in ('', '-journal', '-wal', '-shm'):
        cand = Path(str(path) + suffix)
        if cand.exists():
            cand.unlink()
    con = sqlite3.connect(path)
    for st in SCHEMA:
        con.execute(st)
    con.execute('PRAGMA user_version=%s' % USER_VERSION)
    con.execute('CREATE TABLE IF NOT EXISTS prism_metadata (key TEXT PRIMARY KEY, value TEXT)')
    con.execute('CREATE TABLE IF NOT EXISTS workspace_nodes (id TEXT PRIMARY KEY, payload JSON NOT NULL)')
    con.execute('CREATE TABLE IF NOT EXISTS material_records (id TEXT PRIMARY KEY, payload JSON NOT NULL)')
    for k, v in (('project_id', 'diag'), ('categories', '[]'), ('tags', '[]'),
                 ('tag_system', '{}')):
        con.execute('INSERT INTO prism_metadata VALUES (?,?)', (k, v))
    con.execute('INSERT INTO board_notes (board_id, html, updated_at) VALUES (0,?,0)', ('<p>x</p>',))
    con.execute('INSERT INTO board_mindmap (board_id, tree) VALUES (0,?)',
                (json.dumps({'data': {'text': 'r'}}),))
    pages = [('cv-a', 'A', 0), ('cv-b', 'B', 1)]
    for pid, title, order in pages:
        con.execute('INSERT INTO workspace_pages (id,kind,title,data,sort_order) '
                    'VALUES (?,?,?,?,?)',
                    (pid, 'canvas', title,
                     json.dumps({'__prism_page_payload__': True, 'content': None,
                                 'tags': [], 'parent': None, 'permanent': False}),
                     order))
        con.execute('INSERT INTO workspace_nodes VALUES (?,?)',
                    (f'node-{pid}',
                     json.dumps({'id': f'node-{pid}', 'type': 'folder', 'name': title,
                                 'parentId': None, 'pageId': pid})))
    for i, (pid, _title, _order) in enumerate(pages, start=1):
        d = {'filename': f'{pid}-{i}.png', 'opacity': 1, 'grayscale': False,
             'crop': [0.0, 0.0, 8.0, 8.0], 'imageWidth': 8, 'imageHeight': 8,
             'categories': [], 'lastUsed': '', 'tags': [], 'rating': 0,
             'notes': '', 'title': '', 'canvasId': pid, 'groupId': None,
             'groupNote': ''}
        con.execute("INSERT INTO items(type,x,y,z,scale,rotation,flip,data) "
                    "VALUES ('pixmap',?,?,0,1,0,1,?)", (float(i), float(i), json.dumps(d)))
        con.execute('INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) VALUES (?,?,?,?,?,?)',
                    (f'{pid}-{i}.png', i, 420, 0, len(TINY_PNG), TINY_PNG))
    con.commit()
    con.close()


def test_diag_load(main_window, tmp_path):
    out = {}
    p = WORK / 'diag.prism'
    _build(p)
    out['fixture'] = str(p)

    con = sqlite3.connect(p)
    out['db_pages'] = [list(r) for r in con.execute(
        'SELECT id, kind, title, sort_order FROM workspace_pages ORDER BY sort_order')]
    out['db_nodes'] = [list(r) for r in con.execute('SELECT id FROM workspace_nodes')]
    out['db_items'] = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    con.close()

    view = main_window.view
    out['scene_before'] = {'pages': list(getattr(view.scene, 'workspace_pages', [])),
                           'nodes': len(getattr(view.scene, 'workspace_nodes', []) or [])}

    from prism import fileio
    try:
        fileio.load_prism(str(p), view.scene, scope=None)
        out['load_prism'] = 'ok'
    except Exception:
        out['load_prism'] = traceback.format_exc()

    out['scene_after_load'] = {'pages': list(getattr(view.scene, 'workspace_pages', [])),
                               'nodes': list(getattr(view.scene, 'workspace_nodes', []) or []),
                               'filename': getattr(view, 'filename', None),
                               'partial_source': getattr(view.scene,
                                                        '_partial_project_source', '<missing>')}

    panel = view.category_panel
    try:
        out['panel_pages_with_implicit_defaults'] = [
            dict(p) for p in panel._pages_with_implicit_defaults()]
    except Exception:
        out['panel_pages_with_implicit_defaults'] = traceback.format_exc()

    try:
        out['resource_service_nodes'] = list(getattr(panel.resource_service, 'nodes', []))
    except Exception:
        out['resource_service_nodes'] = traceback.format_exc()

    try:
        out['_first_canvas_id'] = main_window._first_canvas_id()
    except Exception:
        out['_first_canvas_id'] = traceback.format_exc()

    # now the finalize step
    try:
        view._finalize_loaded_project(str(p), errors=[])
        out['finalize'] = 'ok'
    except Exception:
        out['finalize'] = traceback.format_exc()

    out['after_finalize'] = {
        'filename': getattr(view, 'filename', None),
        'current_page_id': getattr(main_window, 'current_page_id', None),
        'current_page_kind': getattr(main_window, 'current_page_kind', None),
        'scene_pages': [dict(p) for p in (getattr(view.scene, 'workspace_pages', []) or [])],
        'first_canvas_id': getattr(main_window, '_first_canvas_id', '?')() if callable(
            getattr(main_window, '_first_canvas_id', None)) else None,
        'central_widget_class': type(main_window.tabs.currentWidget()).__name__,
        'is_canvas_page': getattr(main_window, 'canvas_page', None)
        is main_window.tabs.currentWidget(),
    }

    (EVIDENCE / 'diag-load.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('\n=== DIAG ===')
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:5000])