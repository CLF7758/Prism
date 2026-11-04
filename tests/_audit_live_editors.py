"""Explicit native WebEngine editor -> module switch -> save/reopen audit."""
import json
import time
from pathlib import Path
from PyQt6 import QtCore

OUT=Path('artifacts/full-audit').resolve()

def test_live_editor_changes_survive_cross_module_switch_and_reopen(main_window,qtbot,tmp_path):
    window=main_window
    window.resize(1440,900);window.show()
    scene=window.view.scene
    scene.workspace_pages=[
        dict(id='live-doc',kind='document',title='Live document',content='<p>Initial</p>',tags=[]),
        dict(id='live-map',kind='mindmap',title='Live map',content={'data':{'text':'Initial root'},'children':[]},tags=[])]
    report=dict(completed=False,switches=[])
    def js(panel,source):
        values=[]
        panel._page.runJavaScript(source,values.append)
        qtbot.waitUntil(lambda:bool(values),timeout=15000)
        return values[0]
    def open_page(kind,page_id):
        started=time.perf_counter()
        window.open_workspace_page(kind,page_id)
        qtbot.waitUntil(lambda: window.current_page_id == page_id
                       and page_id in window._page_panels, timeout=15000)
        panel=window._page_panels[page_id]
        qtbot.waitUntil(lambda:getattr(panel,'_ready',False) and not getattr(panel,'_pending_push',False)
                       and not getattr(panel,'_tree_loading',False),timeout=20000)
        report['switches'].append(dict(kind=kind,ms=round((time.perf_counter()-started)*1000,2)))
        return panel
    doc=open_page('document','live-doc')
    assert js(doc,"window.__quill.insertText(0,'Live-document-edit ','user');true")
    mindmap=open_page('mindmap','live-map')
    assert 'Live-document-edit' in scene.workspace_pages[0]['content']
    edited_tree={'data':{'text':'Live root edit'},'children':[{'data':{'text':'Live child'},'children':[]}]}
    assert js(mindmap,'window.__mindMap.setData('+json.dumps(edited_tree)+');window.__mindMap.render();true')
    window.open_material_library()
    qtbot.waitUntil(lambda:window.tabs.currentWidget() is window.material_library,timeout=15000)
    qtbot.waitUntil(lambda:window.material_library.materials.ready,timeout=20000)
    assert scene.workspace_pages[1]['content']['data']['text']=='Live root edit'
    record=window.material_library.records.create(name='Switch audit material')
    window.material_library._selection_changed(record['id'])
    window.material_library.change_slot_options('roughness',{'value':.2})
    for _ in range(3):
        assert open_page('document','live-doc') is doc
        assert 'Live-document-edit' in js(doc,'window.__flush()')
        assert open_page('mindmap','live-map') is mindmap
        snapshot=js(mindmap,'window.__searchSnapshot()')
        assert 'Live root edit' in (snapshot if isinstance(snapshot,str) else json.dumps(snapshot))
        window.asset_browser.open('all')
        qtbot.waitUntil(lambda:window.tabs.currentWidget() is window.asset_browser,timeout=15000)
    assert 'slots' not in window.material_library.records.get(record['id'])['preview']['properties']
    assert window._store_current_page() is not False
    target=tmp_path/'live-editor-roundtrip.prism'
    window.view.do_save(str(target),create_new=True,silent=True)
    qtbot.waitUntil(lambda:not window.view.worker.isRunning(),timeout=30000)
    qtbot.waitUntil(lambda:not window.view.is_dirty(),timeout=15000)
    window.view.open_from_file(str(target))
    qtbot.waitUntil(lambda:not window.view.worker.isRunning() and not getattr(window.view,'_project_opening',False),timeout=30000)
    reopened=open_page('document','live-doc')
    assert 'Live-document-edit' in js(reopened,'window.__flush()')
    reopened_map=open_page('mindmap','live-map')
    snapshot=js(reopened_map,'window.__searchSnapshot()')
    assert 'Live root edit' in (snapshot if isinstance(snapshot,str) else json.dumps(snapshot))
    report['completed']=True
    OUT.mkdir(parents=True,exist_ok=True)
    (OUT/'live-editor-cross-module-20261021.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    window.view.undo_stack.setClean()
