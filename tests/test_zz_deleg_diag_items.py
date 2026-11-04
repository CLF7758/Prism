"""Do the fixture's items actually load into the scene? And why not?"""
import json
import sqlite3
import traceback
from pathlib import Path

EVIDENCE = Path(r"C:\Users\admin\Desktop\Prism-main\Prism-main\artifacts"
                r"\test-delegation-20261023\batch1\evidence")


def test_diag_items_load(main_window, qtbot):
    from tests.test_zz_deleg_b1_open_scope import fixture_q2
    fx = fixture_q2()
    out = {'fixture': fx['path']}

    con = sqlite3.connect(fx['path'])
    out['db_items'] = con.execute('SELECT COUNT(*) FROM items').fetchone()[0]
    out['db_sqlar'] = con.execute('SELECT COUNT(*) FROM sqlar').fetchone()[0]
    row = con.execute('SELECT id, type, data FROM items LIMIT 1').fetchone()
    out['sample_item'] = [row[0], row[1], (row[2] or '')[:300]]
    con.close()

    window = main_window
    view = window.view
    from prism import fileio
    try:
        fileio.load_prism(fx['path'], view.scene, scope=None)
        out['load'] = 'ok'
    except Exception:
        out['load'] = traceback.format_exc()
    out['items_right_after_load'] = len(view.scene.items())
    out['scene_asset_records'] = len(getattr(view.scene, 'material_records', []) or [])

    qtbot.wait(1500)
    out['items_after_wait'] = len(view.scene.items())
    out['items_after_wait_iter_assets'] = len(list(view.scene.iter_assets()))
    out['loaded_scope_ids'] = getattr(view.scene, '_loaded_scope_ids', None)

    (EVIDENCE / 'diag-items-load.json').write_text(
        json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
    print('\n=== ITEMS LOAD ===')
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str)[:2500])