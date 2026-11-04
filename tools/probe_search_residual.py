"""Scratch probe: current live search behaviour against the residual criteria.

Not a test file. Run manually with the venv python; deleted before delivery.
"""
import json
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, '.')

from prism.project_open_scope import ProjectOpenScope
from prism.project_search import search_catalog_assets


def scope_for(tmp, records):
    source = tmp / 'catalog-source.prism'
    with sqlite3.connect(source) as db:
        db.executescript("CREATE TABLE items(id INTEGER PRIMARY KEY,type TEXT,x,y,z,scale,rotation,flip,data);"
                         "CREATE TABLE sqlar(item_id INTEGER PRIMARY KEY,data BLOB);")
        for key, kind, extra in records:
            db.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?)',
                       (key, kind, 0, 0, 0, 1, 0, 0, json.dumps(extra, ensure_ascii=False)))
    scope = ProjectOpenScope(source, tmp / 'catalog.sqlite')
    assert scope.build().complete
    return scope


def main(tmp):
    records = [
        (1, 'pixmap', dict(canvasId='A', title='雨夜 unloaded', tags=[])),
        (2, 'pixmap', dict(canvasId='A', title='100%_literal', tags=[])),
        (3, 'pixmap', dict(canvasId='A', title='ÄPFEL', tags=[])),
        (4, 'pixmap', dict(canvasId='A', title='Σίσυφος', tags=[])),
        (5, 'pixmap', dict(canvasId='A', title='İSTANBUL', tags=[])),
    ]
    scope = scope_for(tmp, records)
    scene, service = SimpleNamespace(), SimpleNamespace(nodes=[])

    print('--- A: unloaded candidates ---')
    hits = search_catalog_assets(scope, scene, service, '雨夜')
    print('unloaded 雨夜 hits:', len(hits), [h['item_id'] for h in hits])

    print('--- C: literal % and _ ---')
    hits = search_catalog_assets(scope, scene, service, '%_')
    print('literal %_ hits:', len(hits), [h['item_id'] for h in hits])
    hits = search_catalog_assets(scope, scene, service, '100%')
    print('literal 100% hits:', len(hits), [h['item_id'] for h in hits])

    print('--- C: unicode casefold ---')
    for query in ('äpfel', 'ÄPFEL', 'σίσυ', 'ΣΊΣΥ', 'istanbul', 'İSTANBUL'):
        hits = search_catalog_assets(scope, scene, service, query)
        print(' ', query, '->', [h['item_id'] for h in hits])

    print('--- B: >1024 same-type matches ---')
    big = scope_for(tmp, [(key, 'pixmap', dict(canvasId='A', title=f'needle {key}'))
                          for key in range(1, 1200)])
    rows = list(big.iter_query(text='needle', limit=256))
    print('rows through keyset paging:', len(rows), 'ids head/tail', rows[0]['id'], rows[-1]['id'])
    hits = search_catalog_assets(big, scene, service, 'needle')
    print('catalog hits:', len(hits))

    print('--- candidate vs exact divergence probe ---')
    # Row whose tag text matches the SQL prefilter but whose python haystack may differ.
    div = scope_for(tmp, [(1, 'pixmap', dict(canvasId='A', title='x', tags=['a/b'])),
                          (2, 'pixmap', dict(canvasId='A', title='y', tags=['b']))])
    for query in ('a/b', 'b'):
        print(' ', query, '->', [h['item_id'] for h in search_catalog_assets(div, scene, service, query)])


if __name__ == '__main__':
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as name:
        main(Path(name))
