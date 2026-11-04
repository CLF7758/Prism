"""Duplicate the user's existing assets into an isolated, self-contained stress project."""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import time


def complete_materials(connection):
    """Keep broken source records visible in the report, out of valid fixtures."""
    verified = {}
    valid, rejected = [], []
    for (payload,) in connection.execute('SELECT payload FROM material_records'):
        record = json.loads(payload)
        if record.get('deletedAt') is not None or not record.get('files'):
            continue
        invalid = []
        for file in record['files']:
            key = (file['attachmentId'], file['sha256'])
            if key not in verified:
                row = connection.execute('SELECT blob FROM attachments WHERE id=?', (key[0],)).fetchone()
                verified[key] = bool(row and row[0] and hashlib.sha256(row[0]).hexdigest() == key[1])
            if not verified[key]:
                invalid.append(file['name'])
        if invalid:
            rejected.append(dict(id=record['id'], name=record['name'], invalid_files=invalid))
        else:
            valid.append(record)
    if not valid:
        raise ValueError('No complete material sources to duplicate')
    return valid, rejected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--pages-source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    source, output = args.source.resolve(), args.output.resolve()
    if source == output or output.exists():
        raise SystemExit('Output must be a new isolated project')
    output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    start = time.perf_counter()
    report = {'source': str(source), 'output': str(output), 'mode': 'duplicates-of-existing-real-assets', 'status': 'building'}
    def progress(**fields):
        report.update(fields)
        report['elapsed_seconds'] = round(time.perf_counter()-start, 2)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(fields, ensure_ascii=False), flush=True)
    def filehash(path):
        with path.open('rb') as handle:
            return hashlib.file_digest(handle, 'sha256').hexdigest()
    before = filehash(source)
    progress(source_sha256_before=before)
    src = sqlite3.connect(source.as_uri()+'?mode=ro', uri=True)
    db = sqlite3.connect(str(output), uri=True)
    src.backup(db, pages=16384)
    progress(stage='backup_complete')
    db.execute('PRAGMA journal_mode=OFF')  # Disposable fixture construction only.
    db.execute('PRAGMA synchronous=OFF')
    db.execute('PRAGMA cache_size=-262144')
    db.execute('ATTACH DATABASE ? AS original', (source.as_uri()+'?mode=ro',))
    rows = src.execute("SELECT id,data FROM items WHERE type='pixmap' ORDER BY id").fetchall()
    total_bytes = src.execute("SELECT sum(length(s.data)) FROM sqlar s JOIN items i ON i.id=s.item_id WHERE i.type='pixmap'").fetchone()[0]
    progress(original_images=len(rows), original_image_bytes=total_bytes)
    # Initially assemble one dense board. The grouped form below gives the
    # user's real scenario: 100 pictures per canvas, all visible in All assets.
    db.execute('DELETE FROM items')
    db.execute('DELETE FROM sqlar')
    bases = [(key, json.loads(data)) for key, data in rows]
    for index in range(100000):
        key, base = bases[index % len(bases)]
        data = copy.deepcopy(base)
        data.update(canvasId='default-canvas', groupId=None, groupNote='', title=f'Stress {index+1:06d}')
        data.pop('deletedAt', None)
        width, height = data['imageWidth'], data['imageHeight']
        scale = 460/max(width, height)
        x, y = (index % 400)*512, (index//400)*512
        db.execute('INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) VALUES (?,"pixmap",?,?,0,?,0,1,?)',
                   (index+1, x, y, scale, json.dumps(data, ensure_ascii=False)))
        db.execute('INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) SELECT ?,?,mode,mtime,sz,data FROM original.sqlar WHERE item_id=?',
                   (f'stress-{index+1:06d}', index+1, key))
        if (index+1) % 2000 == 0:
            db.commit()
            progress(images=index+1, project_bytes=output.stat().st_size)
    records, rejected = complete_materials(src)
    progress(incomplete_source_materials=rejected)
    db.execute('DELETE FROM material_records')
    project_id = src.execute("SELECT value FROM prism_metadata WHERE key='project_id'").fetchone()[0]
    for index in range(5000):
        record = copy.deepcopy(records[index % len(records)])
        record.update(id=f'stress-material-{index:05d}', folderId=None, deletedAt=None,
                      name=f'{record["name"][:125]} [{index+1:04d}]')
        db.execute('INSERT INTO material_records VALUES (?,?)', (record['id'], json.dumps(record, ensure_ascii=False)))
    db.execute("UPDATE prism_metadata SET value=? WHERE key='material_library_state'", (json.dumps({'folderId': None, 'views': {}, 'viewMode': 'grid', 'cardSize': 240}),))
    with sqlite3.connect(args.pages_source.resolve().as_uri()+'?mode=ro', uri=True) as pages_db:
        pages = pages_db.execute("SELECT kind,title,data FROM workspace_pages WHERE kind IN ('document','mindmap') ORDER BY sort_order").fetchall()
    # Prefer a real nonempty page per source; keep all embedded references
    # valid because source audit pages use the same attachments as this copy.
    db.execute('DELETE FROM workspace_pages')
    db.execute('DELETE FROM workspace_nodes')
    for group in range(1000):
        page_id = f'stress-canvas-{group:04d}'
        title = f'压力画布 {group+1:04d} · 100图'
        payload = {'__prism_page_payload__': True, 'content': None, 'tags': [], 'parent': None, 'permanent': False}
        db.execute('INSERT INTO workspace_pages VALUES (?,?,?,?,?)', (page_id,'canvas',title,json.dumps(payload),group))
        node = dict(id=f'node-{page_id}', projectId=project_id, section='canvas', nodeType='page', parentId=None,
                    title=title, order=(group+1)*1000, pageId=page_id, color=None, deletedAt=None)
        db.execute('INSERT INTO workspace_nodes VALUES (?,?)', (node['id'],json.dumps(node,ensure_ascii=False)))
        db.execute("UPDATE items SET data=json_set(data,'$.canvasId',?) WHERE id BETWEEN ? AND ?", (page_id,group*100+1,(group+1)*100))
    for kind in ('document', 'mindmap'):
        templates = [(title, json.loads(data)) for k,title,data in pages if k==kind]
        if not templates:
            raise RuntimeError(f'No existing {kind} page to duplicate')
        for index in range(500):
            title, payload = templates[index % len(templates)]
            payload = copy.deepcopy(payload)
            if isinstance(payload, dict) and payload.get('__prism_page_payload__'):
                payload.update(parent=None, permanent=False)
            page_id = f'stress-{kind}-{index:04d}'
            title = f'{title[:100]} [{index+1:03d}]'
            db.execute('INSERT INTO workspace_pages VALUES (?,?,?,?,?)', (page_id,kind,title,json.dumps(payload,ensure_ascii=False),index))
            node = dict(id=f'node-{page_id}', projectId=project_id, section=kind, nodeType='page', parentId=None, title=title,
                        order=(index+1)*1000, pageId=page_id, color=None, deletedAt=None)
            db.execute('INSERT INTO workspace_nodes VALUES (?,?)', (node['id'],json.dumps(node,ensure_ascii=False)))
    db.commit()
    db.execute('DETACH DATABASE original')
    db.execute('PRAGMA journal_mode=DELETE')
    check=db.execute('PRAGMA quick_check').fetchone()[0]
    counts={kind: count for kind,count in db.execute('SELECT kind,count(*) FROM workspace_pages GROUP BY kind')}
    assert db.execute('SELECT count(*) FROM items').fetchone()[0]==100000
    assert db.execute('SELECT count(*) FROM sqlar').fetchone()[0]==100000
    assert db.execute('SELECT count(*) FROM material_records').fetchone()[0]==5000
    assert counts=={'canvas':1000, 'document':500, 'mindmap':500}
    assert check=='ok'
    # Every duplicate owns a complete embedded source blob. Validate one full
    # cycle plus scattered copies against source SHA256, not just dimensions.
    hashes={key: hashlib.sha256(blob).hexdigest() for key,blob in src.execute("SELECT s.item_id,s.data FROM sqlar s JOIN items i ON i.id=s.item_id WHERE i.type='pixmap'")}
    verified=0
    for index in list(range(len(bases)))+list(range(3000,100000,997)):
        blob=db.execute('SELECT data FROM sqlar WHERE item_id=?',(index+1,)).fetchone()[0]
        assert hashlib.sha256(blob).hexdigest()==hashes[bases[index%len(bases)][0]]
        verified+=1
    db.close();src.close()
    after=filehash(source)
    assert before==after
    progress(status='ready', images=100000, canvases=1000, images_per_canvas=100, materials=5000, documents=500, mindmaps=500,
             material_source_records=len(records), source_sha256_after=after, source_unchanged=True,
             verified_duplicate_blobs=verified, quick_check=check, project_bytes=output.stat().st_size)


if __name__=='__main__':
    main()
