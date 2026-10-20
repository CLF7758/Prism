"""Deep audit of the user's SQLite snapshot, using the real Qt/WebEngine UI.

Run explicitly; this fixture contains personal assets and is not part of CI.
"""
import hashlib
import html as html_module
import json
import sqlite3
import time
import zipfile
import uuid
from pathlib import Path

from PyQt6 import QtCore, QtGui

from prism import performance_trace
from prism.fileio.sql import SQLiteIO
from prism.fileio import document_export, mindmap_export
from prism.fileio.attachments import attachment_url
from prism.widgets.mindmap_panel import content_signature
from tests.test_search_editors import javascript
from tests.test_unified_project_search import page as create_page

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'artifacts/full-audit'
PROJECT = OUT/'Test-project-audit.prism'


def blob_hashes(path, table, id_column, blob_column):
    result = {}
    with sqlite3.connect(path) as db:
        for key, blob in db.execute(f'SELECT {id_column},{blob_column} FROM {table}'):
            result[key] = hashlib.sha256(blob).hexdigest() if blob is not None else None
    return result


def test_actual_project_across_all_surfaces(main_window, qtbot, qapp):
    assert PROJECT.exists(), 'Create the audit snapshot first'
    assert qapp.platformName() == 'windows'
    report = {'project': str(PROJECT), 'stages': {}}
    trace = None
    timer = QtCore.QTimer()
    timer.setInterval(16)
    gaps = []
    previous = [time.perf_counter()]
    def tick():
        now = time.perf_counter()
        gaps.append((now-previous[0])*1000)
        previous[0] = now
    timer.timeout.connect(tick)
    def stage(name, function):
        if trace is not None:
            trace.record('audit_stage', name=name)
        started = time.perf_counter()
        result = function()
        report['stages'][name] = round((time.perf_counter()-started)*1000, 2)
        (OUT/'deep-report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
        return result
    try:
        before_sources = stage('hash_original_canvas_sources', lambda: blob_hashes(PROJECT,'sqlar','item_id','data'))
        before_attachments = stage('hash_original_attachments', lambda: blob_hashes(PROJECT,'attachments','id','blob'))
        trace = performance_trace.install(qapp, main_window, OUT/'deep-trace')
        previous[0] = time.perf_counter()  # Exclude test-owned hashing from UI timing.
        timer.start()
        view = main_window.view
        main_window.resize(1580,1000)
        main_window.show()
        stage('open_real_project', lambda: view.open_from_file(str(PROJECT)))
        qtbot.waitUntil(lambda: view.filename == str(PROJECT) and not view.worker.isRunning(), timeout=120000)
        qtbot.waitUntil(lambda: view.scene.items_to_add.empty(), timeout=30000)
        items = list(view.scene.items_for_save())
        report['objects'] = len(items)
        assert len(items) == 2766
        canvases = ['default-canvas'] + [p['id'] for p in view.scene.workspace_pages if p['kind']=='canvas']
        canvas_switch = []
        for canvas in canvases:
            started = time.perf_counter()
            main_window.open_workspace_page('canvas',canvas)
            qtbot.wait(25)
            canvas_switch.append((time.perf_counter()-started)*1000)
        report['canvas_switch_ms'] = canvas_switch
        main_window.open_workspace_page('canvas','default-canvas')
        # Let actual preview jobs settle, then rapidly revisit two canvases.
        # Presence alone is insufficient: an 8px fallback is still a mosaic.
        from prism import lod
        from prism.item_lifecycle import is_pixmap_item
        quality = []
        for canvas in canvases[:2]:
            main_window.open_workspace_page('canvas', canvas)
            started = time.perf_counter()
            def details_ready():
                rect = view.mapToScene(view.viewport().rect()).boundingRect()
                scale = view.get_scale()*view.viewport().devicePixelRatioF()
                visible = [i for i in view.scene.items(rect) if i.isVisible() and is_pixmap_item(i)]
                return all(lod.base_preview_ready(i, max(i._image_size.width(), i._image_size.height())*i.scale()*scale, view)
                           for i in visible)
            qtbot.waitUntil(details_ready, timeout=30000)
            qtbot.wait(250)
            quality.append({'canvas': canvas, 'details_ready_ms': round((time.perf_counter()-started)*1000,2)})
        for _ in range(5):
            for canvas in canvases[:2]:
                main_window.open_workspace_page('canvas',canvas)
                qtbot.wait(30)
                assert details_ready(), 'Returning to a canvas must retain readable previews'
        report['preview_quality'] = quality
        main_window.grab().save(str(OUT/'rapid-switch-preview.png'))
        # A readable 192px preview alone does not prove zooming recovers
        # source detail. Evict one large image's memory levels, then revisit
        # at magnification and require the actual requested display level.
        from prism.item_lifecycle import logical_item
        zoom_checks = []
        for canvas in canvases[:2]:
            main_window.open_workspace_page('canvas', canvas)
            candidates = [logical_item(i) for i in view.scene.items_for_save()
                          if is_pixmap_item(i) and i.isVisible()]
            item = max(candidates, key=lambda i: i._image_size.width()*i._image_size.height())
            assert max(item._image_size.width(), item._image_size.height()) > 512
            lod.cache().invalidate(item)
            item._preview_pixmap = None
            view.fitInView(item.sceneBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)
            view.scale(2, 2)
            view.centerOn(item.sceneBoundingRect().center())
            pixels = max(item._image_size.width(), item._image_size.height())*item.scale()*view.get_scale()*view.viewport().devicePixelRatioF()
            needed = min(lod.level_for_pixels(pixels), max(item._image_size.width(), item._image_size.height()))
            assert needed > 192
            started = time.perf_counter()
            def zoom_ready():
                preview = lod.display_pixmap(item, pixels, view)
                return preview is not None and max(preview.width(), preview.height()) >= needed
            qtbot.waitUntil(zoom_ready, timeout=30000)
            qtbot.wait(250)
            zoom_checks.append(dict(canvas=canvas, required=needed,
                                    recovered_ms=round((time.perf_counter()-started)*1000, 2)))
            assert lod.cache().bytes <= lod.cache().budget
            view.viewport().grab().save(str(OUT/f'zoom-recovery-{len(zoom_checks)}.png'))
        report['zoom_recovery'] = zoom_checks
        main_window.open_workspace_page('canvas','default-canvas')
        stage('canvas_fit', view.on_action_fit_scene)
        pans = []
        for _ in range(15):
            started = time.perf_counter()
            view.pan(QtCore.QPointF(12,8))
            view.viewport().repaint()
            qtbot.wait(16)
            pans.append((time.perf_counter()-started)*1000)
        report['canvas_pan_ms'] = pans
        # Exercise a real image edit and its undo through the existing commands.
        image = next(i for i in items if getattr(i,'TYPE','')=='pixmap' and i.isVisible())
        image = image.graphics_item() if hasattr(type(image),'graphics_item') else image
        image.setSelected(True)
        position = image.pos()
        from prism.commands import MoveItemsBy
        command = MoveItemsBy([image], QtCore.QPointF(10,10))
        view.undo_stack.push(command)
        view.undo_stack.undo()
        assert image.pos() == position
        view.scene.clearSelection()

        material = next(r for r in view.scene.material_records if 'Poly Haven' in r.get('tags',[]))
        attachment_id = material['files'][0]['attachmentId']
        html = '<h1>Audit document marker</h1>' + ''.join(
            f'<p>Paragraph {i}: search marker, bold <strong>text</strong>, 中文保存测试。</p>' for i in range(200))
        html += f'<p><img src="{attachment_url(attachment_id)}" width="240"></p>'
        run_id = uuid.uuid4().hex[:8]
        document = create_page(main_window,'document','Audit document '+run_id,html)
        tree = {'data':{'text':'Audit mind map marker','uid':'audit-root','expand':True},'children':[
            {'data':{'text':f'Branch {i}','uid':f'branch-{i}','expand':True},'children':[
                {'data':{'text':f'Node {i}-{j} 中文','uid':f'node-{i}-{j}'},'children':[]} for j in range(9)
            ]} for i in range(20)]}
        tree['children'][0]['data'].update(image=attachment_url(attachment_id),
                                           imageSize={'width':128,'height':128})
        mindmap = create_page(main_window,'mindmap','Audit mind map '+run_id,tree)
        stage('open_document_first',lambda:main_window.open_workspace_page('document',document['id']))
        doc = main_window._page_panels[document['id']]
        qtbot.waitUntil(lambda:doc._ready and not doc._pending_push,timeout=20000)
        assert javascript(qtbot,doc._page,'window.__quill.getText().includes("Paragraph 199")')
        javascript(qtbot,doc._page,'window.__quill.insertText(0,"Saved live edit\\n");true')
        doc.store_to_scene()
        assert 'Saved live edit' in html_module.unescape(document['content']).replace('\u00a0', ' ')
        stage('open_mindmap_first',lambda:main_window.open_workspace_page('mindmap',mindmap['id']))
        mind = main_window._page_panels[mindmap['id']]
        qtbot.waitUntil(lambda:mind._ready and javascript(qtbot,mind.web_view.page(),
            'Boolean(window.__mindMap && window.__mindMap.renderer.root)'),timeout=25000)
        qtbot.wait(350)
        mind.store_to_scene()
        map_signature = content_signature(mindmap['content'])
        switches = []
        for _ in range(5):
            for kind, page_id in [('document',document['id']),('mindmap',mindmap['id']),('canvas','default-canvas')]:
                started = time.perf_counter()
                main_window.open_workspace_page(kind,page_id)
                qtbot.wait(20)
                switches.append(dict(kind=kind,ms=round((time.perf_counter()-started)*1000,2)))
            stage('open_material_library_warm',main_window.open_material_library)
            qtbot.wait(50)
        report['surface_switches'] = switches
        assert 'Saved live edit' in html_module.unescape(document['content']).replace('\u00a0', ' ')
        assert content_signature(mindmap['content']) == map_signature

        from prism.project_search import search_project
        hits = stage('global_search',lambda: search_project(view.scene,view.category_panel.resource_service,'marker',
            [doc,mind],page_records=view.category_panel._pages_with_implicit_defaults()))
        assert {document['id'],mindmap['id']} <= {h.get('page_id') for h in hits}
        stage('document_html_export',lambda:document_export.export_note_to_html(document['content'],view.scene.attachments,str(OUT/'audit-document.html')))
        stage('document_docx_export',lambda:document_export.export_note_to_word(document['content'],view.scene.attachments,str(OUT/'audit-document.docx')))
        stage('mindmap_json_export',lambda:mindmap_export.export_mindmap(mindmap['content'],str(OUT/'audit-mindmap.json'),attachments=view.scene.attachments))
        stage('mindmap_xmind_export',lambda:mindmap_export.export_mindmap(mindmap['content'],str(OUT/'audit-mindmap.xmind'),attachments=view.scene.attachments))
        original_image_hash = material['files'][0]['sha256']
        for export in ('audit-document.docx','audit-mindmap.xmind'):
            with zipfile.ZipFile(OUT/export) as archive:
                assert any(hashlib.sha256(archive.read(name)).hexdigest()==original_image_hash
                           for name in archive.namelist() if not name.endswith('/'))
        assert 'prism://attach/' not in (OUT/'audit-document.html').read_text(encoding='utf-8')
        main_window._store_current_page()
        stage('start_full_project_save',lambda:view.do_save(str(PROJECT),create_new=False))
        saved_started = time.perf_counter()
        qtbot.waitUntil(lambda:not view.worker.isRunning(),timeout=120000)
        qtbot.waitUntil(lambda:not view.is_dirty(),timeout=10000)
        report['full_save_ms'] = round((time.perf_counter()-saved_started)*1000,2)
        timer.stop()  # The remaining synchronous checks belong to the harness.
        trace.close()
        after_sources = stage('verify_canvas_originals_after_save',lambda:blob_hashes(PROJECT,'sqlar','item_id','data'))
        after_attachments = stage('verify_attachments_after_save',lambda:blob_hashes(PROJECT,'attachments','id','blob'))
        assert after_sources == before_sources
        assert all(after_attachments.get(k)==v for k,v in before_attachments.items())
        report['canvas_originals_verified'] = len(before_sources)
        report['attachments_verified'] = len(before_attachments)
        with sqlite3.connect(PROJECT) as db:
            assert db.execute('pragma quick_check').fetchone()[0]=='ok'
            saved_pages = dict(db.execute('select id,data from workspace_pages'))
            assert 'Saved live edit' in html_module.unescape(saved_pages[document['id']]).replace('\u00a0', ' ')
            assert len(list(db.execute('select id from material_records'))) == 332
        main_window.grab().save(str(OUT/'deep-final.png'))
        report['status'] = 'passed'
    finally:
        timer.stop()
        report['heartbeat_p95_ms'] = sorted(gaps)[int(len(gaps)*.95)] if gaps else None
        report['heartbeat_max_ms'] = max(gaps,default=0)
        if trace is not None:
            trace.close()
        (OUT/'deep-report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
