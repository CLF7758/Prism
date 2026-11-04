"""Partition the isolated 100k fixture into 1000 canvases of 100 pictures."""
import json
from pathlib import Path
import sqlite3

root=Path(__file__).resolve().parents[1]
report_path=root/'artifacts/scale-audit/fixture.json'
report=json.loads(report_path.read_text(encoding='utf-8'))
assert report['status']=='ready'
path=Path(report['output'])
assert path.parent==Path('E:/Prism-stress-100k')
with sqlite3.connect(path) as db:
    db.execute('PRAGMA journal_mode=OFF')
    db.execute('PRAGMA synchronous=OFF')
    project_id=db.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()[0]
    for group in range(1000):
        page_id=f'stress-canvas-{group:04d}'
        title=f'压力画布 {group+1:04d} · 100图'
        payload={'__prism_page_payload__':True,'content':None,'tags':[],'parent':None,'permanent':False}
        db.execute('INSERT OR REPLACE INTO workspace_pages VALUES (?,?,?,?,?)',(page_id,'canvas',title,json.dumps(payload),group))
        node=dict(id='node-'+page_id,projectId=project_id,section='canvas',nodeType='page',parentId=None,
                  title=title,order=(group+1)*1000,pageId=page_id,color=None,deletedAt=None)
        db.execute('INSERT OR REPLACE INTO workspace_nodes VALUES (?,?)',(node['id'],json.dumps(node,ensure_ascii=False)))
        db.execute("UPDATE items SET data=json_set(data,'$.canvasId',?) WHERE id BETWEEN ? AND ?",(page_id,group*100+1,(group+1)*100))
        if group%100==99:
            db.commit();print('grouped',group+1,flush=True)
    db.commit()
    db.execute('PRAGMA journal_mode=DELETE')
    assert db.execute('PRAGMA quick_check').fetchone()[0]=='ok'
report.update(canvases=1000,images_per_canvas=100,layout='1000 canvases; All assets contains 100000 images')
report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
