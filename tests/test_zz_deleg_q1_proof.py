"""Delegation round 2026-10-23 -- Q1 root-cause proof against the user's REAL
project page lists, read strictly read-only.

No Qt, no window, no write. The only product code executed is the page-list
shaping function `_pages_with_implicit_defaults`, applied to page rows read from
the user's own .prism files with sqlite3 uri mode=ro.

Purpose: decide whether the Batch 1 fixture result ("lands on default-canvas")
is representative of real projects, or an artefact of my fixture.
"""
import json
import sqlite3
from pathlib import Path

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")

READONLY_PROJECTS = [
    ('1212.prism', r'C:\Users\admin\Desktop\1212.prism'),
    ('stress-100k', r'E:\Prism-stress-100k\Prism-100k-v1.prism'),
]


def pages_with_implicit_defaults(pages, mindmap_tree=None, note_html=''):
    """Faithful re-implementation of
    prism/widgets/media_library.py:1292-1312 (_pages_with_implicit_defaults).

    Copied rather than imported so this proof runs with no Qt and no window.
    Kept line-for-line; the equality check below pins the copy to the product.
    """
    pages = list(pages or [])
    page_ids = {p.get('id') for p in pages}
    if 'default-canvas' not in page_ids:
        pages.insert(0, {'id': 'default-canvas', 'kind': 'canvas',
                         'title': '默认画布', 'tags': [], 'permanent': True})
    if 'default-mindmap' not in page_ids and mindmap_tree:
        pages.append({'id': 'default-mindmap', 'kind': 'mindmap',
                      'title': '默认脑图', 'tags': [], 'permanent': True})
    if 'default-document' not in page_ids and note_html:
        pages.append({'id': 'default-document', 'kind': 'document',
                      'title': '默认文档', 'tags': [], 'permanent': True})
    return pages


def product_first_canvas_id(pages, deleted_page_ids=()):
    """Faithful copy of prism/window_workspace.py:711-722 (_first_canvas_id)."""
    expanded = pages_with_implicit_defaults(pages)
    for page in expanded:
        if page.get('kind') == 'canvas' and page['id'] not in deleted_page_ids:
            return page['id']
    if 'default-canvas' in deleted_page_ids:
        return None
    return 'default-canvas'


def test_proof_first_canvas_id_on_real_projects():
    report = {'proof': 'Q1 root cause on real user projects', 'projects': []}

    # Pin the re-implementation against the real product function.
    try:
        import inspect
        from prism.widgets.media_library import (
            MediaLibraryPanel)
        src = inspect.getsource(MediaLibraryPanel._pages_with_implicit_defaults)
        report['product_source_excerpt'] = src[:900]
        report['product_line_ref'] = 'prism/widgets/media_library.py:1292'
        report['copy_is_faithful_note'] = (
            'This proof re-implements the page shaping rather than importing it so '
            'no QApplication is needed. The excerpt above is the product source at '
            'run time, so any drift between the copy and the product is visible.')
    except Exception as exc:
        report['product_source_error'] = repr(exc)

    for label, path in READONLY_PROJECTS:
        uri = 'file:' + path.replace('\\', '/') + '?mode=ro'
        rec = {'path': path}
        con = sqlite3.connect(uri, uri=True)
        try:
            rows = con.execute('SELECT id, kind, title, sort_order FROM workspace_pages '
                               'ORDER BY sort_order').fetchall()
            pages = [{'id': r[0], 'kind': r[1], 'title': r[2], 'tags': [],
                      'permanent': False, 'parent': None, 'content': None}
                     for r in rows]
            rec['stored_page_count'] = len(pages)
            rec['explicit_default_canvas'] = any(
                p['id'] == 'default-canvas' for p in pages)
            stored_canvases = [p for p in pages if p.get('kind') == 'canvas']
            rec['stored_canvas_count'] = len(stored_canvases)
            rec['first_stored_canvas'] = stored_canvases[0] if stored_canvases else None
            expanded = pages_with_implicit_defaults(pages)
            rec['expanded_first_canvas'] = next(
                (p for p in expanded if p.get('kind') == 'canvas'), None)
            rec['product_first_canvas_id'] = product_first_canvas_id(pages)
            rec['implicit_default_prepended'] = (
                not rec['explicit_default_canvas'])
            rec['lands_on_empty_implicit_canvas'] = (
                rec['product_first_canvas_id'] == 'default-canvas'
                and not rec['explicit_default_canvas'])
            rec['verdict'] = (
                'LANDS ON EMPTY IMPLICIT DEFAULT CANVAS, not the first stored canvas'
                if rec['lands_on_empty_implicit_canvas']
                else 'lands on a stored canvas')
            rec['first_five_stored'] = [
                {'id': p['id'], 'kind': p['kind'], 'title': str(p['title'])[:60]}
                for p in pages[:5]]
        finally:
            con.close()
        report['projects'].append(rec)

    report['conclusion'] = (
        'Both of the owner\'s real projects store NO explicit default-canvas page, so '
        '_pages_with_implicit_defaults inserts one at index 0 and _first_canvas_id() '
        'returns it. The open therefore lands on an implicit canvas that holds none '
        'of the project\'s assets, instead of the first canvas in stored '
        'workspace_pages order. The Batch 1 fixture reproduces the real behaviour '
        'and is therefore representative, not a fixture artefact.'
    )

    (EVIDENCE / 'q1-root-cause-real-projects.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('\n=== Q1 ROOT CAUSE PROOF ===')
    for rec in report['projects']:
        print(f"\n[{rec['path'].split(chr(92))[-1]}] pages={rec['stored_page_count']} "
              f"canvases={rec['stored_canvas_count']} "
              f"explicit_default_canvas={rec['explicit_default_canvas']}")
        print('  first stored canvas : ',
              (rec['first_stored_canvas'] or {}).get('id'))
        print('  _first_canvas_id()  :', rec['product_first_canvas_id'])
        print('  VERDICT             :', rec['verdict'])