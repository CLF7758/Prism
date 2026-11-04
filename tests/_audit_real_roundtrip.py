"""Explicit real-project roundtrip on an isolated copy; not normal CI."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import time
import zlib
from concurrent.futures import ThreadPoolExecutor

OUT=Path('artifacts/full-audit').resolve()
SOURCE=Path('C:/Users/admin/Desktop/测试.prism')
TARGET=OUT/'Real-project-roundtrip-20261021.prism'

def sha(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(4*1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()

def manifest(path):
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        images={}
        for row, size, blob in db.execute('select item_id,sz,data from sqlar'):
            raw=blob or b''
            if len(raw)!=size and raw:
                raw=zlib.decompress(raw)
            images[row]=hashlib.sha256(raw).hexdigest()
        attachments={row:hashlib.sha256(blob or b'').hexdigest()
                     for row,blob in db.execute('select id,blob from attachments')}
        return dict(images=images,attachments=attachments)

def test_actual_project_save_reopen_preserves_all_source_bytes(main_window,qtbot):
    assert SOURCE.exists()
    OUT.mkdir(parents=True,exist_ok=True)
    result=dict(source=str(SOURCE),target=str(TARGET),completed=False)
    def persist():
        (OUT/'real-project-roundtrip-20261021.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    view=main_window.view
    def wait_open():
        qtbot.waitUntil(lambda:not view.worker.isRunning() and view.scene.items_to_add.empty()
                       and not getattr(view,'_project_opening',False),timeout=180000)
    with ThreadPoolExecutor(max_workers=1) as pool:
        def prepare():
            before=sha(SOURCE)
            shutil.copyfile(SOURCE,TARGET)
            return before,manifest(TARGET)
        future=pool.submit(prepare)
        qtbot.waitUntil(future.done,timeout=180000)
        original_hash,before=future.result()
        result['source_sha256_before']=original_hash
        result['images_verified']=len(before['images'])
        result['attachments_verified']=len(before['attachments'])
        persist()
        try:
            view.open_from_file(str(TARGET));wait_open()
            records=view.scene.material_records
            material=next(r for r in records if r.get('files'))
            material_id=material['id']
            source_bindings=copy.deepcopy(material.get('textures'))
            source_files=copy.deepcopy(material.get('files'))
            material['notes']='Roundtrip audit: source files preserved'
            material['tags']=list(material.get('tags',[]))+['roundtrip-audit']
            result['original_page_kinds']={kind:sum(p['kind']==kind for p in view.scene.workspace_pages)
                                           for kind in ['canvas','document','mindmap']}
            page=next((p for p in view.scene.workspace_pages if p['kind']=='document'),None)
            if page is None:
                page=dict(id='audit-roundtrip-document',kind='document',title='Audit document',content='',tags=['audit'])
                view.scene.workspace_pages.append(page)
            mindmap=dict(id='audit-roundtrip-mindmap',kind='mindmap',title='Audit mindmap',
                         content={'data':{'text':'Audit root'},'children':[{'data':{'text':'Audit child'},'children':[]}]},tags=['audit'])
            view.scene.workspace_pages.append(mindmap)
            expected_mindmap=copy.deepcopy(mindmap['content'])
            page_id=page['id']
            page['content']=str(page.get('content',''))+'<p>Roundtrip audit marker</p>'
            expected_content=page['content']
            view.mark_content_dirty()
            started=time.perf_counter()
            view.do_save(str(TARGET),create_new=False,silent=True)
            qtbot.waitUntil(lambda:not view.worker.isRunning(),timeout=180000)
            qtbot.waitUntil(lambda:not view.is_dirty(),timeout=15000)
            result['save_ms']=round((time.perf_counter()-started)*1000,2)
            future=pool.submit(manifest,TARGET)
            qtbot.waitUntil(future.done,timeout=180000)
            assert future.result()==before
            view.open_from_file(str(TARGET));wait_open()
            reopened=next(r for r in view.scene.material_records if r['id']==material_id)
            assert reopened['notes']==material['notes']
            assert 'roundtrip-audit' in reopened['tags']
            assert reopened.get('textures')==source_bindings
            assert reopened.get('files')==source_files
            assert next(p for p in view.scene.workspace_pages if p['id']==page_id)['content']==expected_content
            assert next(p for p in view.scene.workspace_pages if p['id']=='audit-roundtrip-mindmap')['content']==expected_mindmap
            with sqlite3.connect(TARGET) as db:
                assert db.execute('pragma quick_check').fetchone()[0]=='ok'
            result['completed']=True
        finally:
            future=pool.submit(sha,SOURCE)
            qtbot.waitUntil(future.done,timeout=180000)
            result['source_sha256_after']=future.result()
            persist()
            view.undo_stack.setClean()
            assert result['source_sha256_after']==original_hash
