"""Explicit real-asset stress run; never included in normal CI discovery."""
import cProfile
import pytest
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import statistics
import tempfile
import time

from PyQt6 import QtCore, QtGui, QtWidgets

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'artifacts/scale-audit'
PROJECT = Path('E:/Prism-stress-100k/Prism-100k-v1.prism')


def stats(values):
    values = sorted(values)
    return dict(count=len(values), median_ms=round(statistics.median(values),2),
                p95_ms=round(values[min(len(values)-1, int(len(values)*.95))],2), max_ms=round(max(values),2)) if values else {}


def test_existing_assets_at_requested_scale(main_window, qtbot, qapp):
    from prism import performance_trace, lod
    from tests.test_material_cards import javascript
    from prism.project_search import search_project
    assert PROJECT.exists()
    OUT.mkdir(exist_ok=True, parents=True)
    label = os.environ.get('PRISM_SCALE_LABEL','baseline')
    report = dict(project=str(PROJECT), platform=qapp.platformName(), stages={}, completed=False)
    pulses=[]; previous=[time.perf_counter()]
    timer=QtCore.QTimer(); timer.setInterval(16)
    def tick():
        now=time.perf_counter();pulses.append((now-previous[0])*1000);previous[0]=now
    timer.timeout.connect(tick)
    timer.start()
    trace=performance_trace.install(qapp, main_window, OUT/f'trace-{label}')
    profile=cProfile.Profile()
    if os.environ.get('PRISM_SCALE_PROFILE')=='1':profile.enable()
    def persist():
        report['parent_resident_bytes']=performance_trace.process_memory_bytes()
        (OUT/f'{label}.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def stage(name,call):
        report['current_stage']=name;persist()
        print('STAGE',name,flush=True)
        trace.record('scale_stage',name=name)
        start=time.perf_counter();result=call()
        report['stages'][name]=round((time.perf_counter()-start)*1000,2);persist()
        return result
    window=main_window;view=window.view
    window.resize(1580,1000); window.show()
    try:
        stage('open_request',lambda:view.open_from_file(str(PROJECT)))
        stage('open_until_ready',lambda:qtbot.waitUntil(lambda:view.filename==str(PROJECT) and not view.worker.isRunning() and view.scene.items_to_add.empty() and not getattr(view,'_project_opening',False),timeout=240000))
        items=stage('enumerate_items',lambda:list(view.scene.items_for_save()))
        assert len(items)==100000
        assert len(view.scene.material_records)==5000
        pages=view.scene.workspace_pages
        assert sum(p['kind']=='document' for p in pages)==500
        assert sum(p['kind']=='mindmap' for p in pages)==500
        report['counts']=dict(images=len(items),materials=5000,documents=500,mindmaps=500,canvases=sum(p['kind']=='canvas' for p in pages))
        browser=window.asset_browser
        if os.environ.get('PRISM_SCALE_COLD_PROFILE')=='1':
            original_render=browser.list._tiles.render
            def cold_render(*args,**kwargs):
                profiler=cProfile.Profile()
                stamp=time.perf_counter()
                profiler.enable()
                try:
                    return original_render(*args,**kwargs)
                finally:
                    profiler.disable()
                    if time.perf_counter()-stamp>.1:
                        profiler.dump_stats(str(OUT/f'{label}-cold.prof'))
                        browser.list._tiles.render=original_render
            browser.list._tiles.render=cold_render
        stage('all_assets_open',lambda:browser.open('all'))
        qtbot.wait(300)
        canvas=browser.list
        stage('all_assets_fit',lambda:canvas.fitInView(canvas._content_bounds,QtCore.Qt.AspectRatioMode.KeepAspectRatio))
        stage('all_assets_first_far_paint',lambda:canvas.viewport().repaint())
        qtbot.wait(1500)
        canvas.viewport().grab().save(str(OUT/f'{label}-all-100k.png'))
        pans=[]
        for _ in range(20):
            start=time.perf_counter();canvas.translate(-15,8);canvas.viewport().repaint();qtbot.wait(16)
            pans.append((time.perf_counter()-start)*1000)
        report['all_assets_pan']=stats(pans);persist()
        zoom=[]
        for factor in [1.2]*12+[1/1.2]*12:
            start=time.perf_counter();canvas.scale(factor,factor);canvas.viewport().repaint();qtbot.wait(16)
            zoom.append((time.perf_counter()-start)*1000)
        report['all_assets_zoom']=stats(zoom);persist()
        # Repaint() can be skipped by Windows for an obscured widget. Force
        # actual QWidget rendering for a same-camera in-process comparison.
        groups=canvas._composites
        canvas.fitInView(canvas._content_bounds,QtCore.Qt.AspectRatioMode.KeepAspectRatio)
        for _ in range(80):
            canvas.viewport().grab()
            qtbot.wait(20)
            if groups.builds and not groups.pending:
                break
        report['composites']=dict(groups=len(groups.groups),builds=groups.builds,bytes=groups.bytes,pending=len(groups.pending))
        forced=[]
        for _ in range(8):
            start=time.perf_counter();canvas.viewport().grab();forced.append((time.perf_counter()-start)*1000)
        report['forced_far_render']=stats(forced);persist()
        # Drive actual wheel events through all distances. The test observes
        # renderer output below without submitting its own decode requests.
        if os.environ.get('PRISM_SCALE_INVALIDATE_SPAN')=='1':
            canvas._invalidate_overview_span()
        wheel_times=[]
        report['wheel_samples']=[]
        original_build=canvas._tiles._build
        tile_samples=[]
        def observed_build(key):
            stamp=time.perf_counter()
            sample=dict(key=key,scale=canvas.transform().m11(),
                        tile_keys=list(canvas._tiles.tiles),
                        stale_tiles=list(canvas._tiles.stale),
                        cached_groups=len(canvas._composites.by_group),
                        total_groups=len(canvas._composites.groups),group_bytes=canvas._composites.bytes)
            result=original_build(key)
            sample['ms']=round((time.perf_counter()-stamp)*1000,2)
            tile_samples.append(sample)
            return result
        canvas._tiles._build=observed_build
        for delta in [360]*16+[-360]*16:
            point=canvas.viewport().rect().center()
            event=QtGui.QWheelEvent(QtCore.QPointF(point),QtCore.QPointF(canvas.viewport().mapToGlobal(point)),
                                   QtCore.QPoint(),QtCore.QPoint(0,delta),
                                   QtCore.Qt.MouseButton.NoButton,QtCore.Qt.KeyboardModifier.NoModifier,
                                   QtCore.Qt.ScrollPhase.NoScrollPhase,False)
            start=time.perf_counter()
            QtWidgets.QApplication.sendEvent(canvas.viewport(),event)
            dispatched=time.perf_counter()
            canvas.viewport().grab()
            rendered=time.perf_counter()
            qtbot.wait(1)
            ended=time.perf_counter()
            wheel_times.append((ended-start)*1000)
            report['wheel_samples'].append(dict(delta=delta,scale=canvas.transform().m11(),
                overview_span=getattr(canvas, '_overview_pixel_span', None),
                visible_detail_count=len(getattr(canvas, '_details_cards', [])),
                dispatch_ms=round((dispatched-start)*1000,2),render_ms=round((rendered-dispatched)*1000,2),
                callbacks_ms=round((ended-rendered)*1000,2)))
        canvas._tiles._build=original_build
        report['wheel_tile_builds']=tile_samples
        report['burst_wheel']=stats(wheel_times);persist()
        # Require source-derived detail at three distant viewport positions.
        quality=[]
        for row,scale in [(0,.15),(0,.35),(0,3),(50000,3),(99999,3)]:
            report['current_stage']=f'quality_{row}_{scale}';persist()
            card=canvas.cards[row];item=browser.model.items[row]
            canvas.setTransform(QtGui.QTransform.fromScale(scale,scale));canvas.centerOn(card.sceneBoundingRect().center())
            start=time.perf_counter()
            visible=canvas.visible_cards(canvas.mapToScene(canvas.viewport().rect()).boundingRect())
            last_detail_report=[0.0]
            screen_samples=[]
            screen_measure=os.environ.get('PRISM_SCALE_SCREEN_RECOVERY')=='1'
            def sample_screen(image):
                focus_rect=canvas.viewportTransform().mapRect(card.sceneBoundingRect()).toAlignedRect()
                focus_rect=focus_rect.intersected(image.rect())
                screen_samples.append(((time.perf_counter()-start)*1000,image.copy(focus_rect)))
            phase_profile=cProfile.Profile()
            phase_enabled=os.environ.get('PRISM_SCALE_PHASE_PROFILE')=='1' and scale==.15
            observer_cost=[]
            phase_patch=pytest.MonkeyPatch()
            phase_timings={}
            def watch(owner, name, label):
                original=getattr(owner,name)
                samples=phase_timings.setdefault(label,[])
                def timed(*args,**kwargs):
                    stamp=time.perf_counter()
                    try:
                        return original(*args,**kwargs)
                    finally:
                        samples.append((time.perf_counter()-stamp)*1000)
                phase_patch.setattr(owner,name,timed)
            if scale==.15 and os.environ.get('PRISM_SCALE_PHASE_TIMINGS')=='1':
                from prism import image_decode
                watch(image_decode,'decode_thumbnail','worker_thumbnail')
                watch(image_decode.DisplayDecoder,'_complete','gui_install')
                watch(canvas,'queue_visible_details','gui_queue_details')
                watch(canvas._tiles,'render','gui_tile_render')
                watch(canvas.delegate,'paint','gui_delegate_paint')
            last_observation=[0.0]
            if phase_enabled:phase_profile.enable()
            def ready():
                nonlocal visible
                observation_start=time.perf_counter()
                if observation_start-last_observation[0]<.1:
                    return False
                last_observation[0]=observation_start
                # Drive only the real renderer. Read cache state below without
                # asking the decoder for detail on the test's behalf.
                observed=canvas.viewport().grab()
                if screen_measure:
                    sample_screen(observed.toImage())
                # Showing scrollbars can resize the viewport after a wheel
                # gesture. Check today's viewport, not now-offscreen edge cards
                # captured before Qt completed that layout change.
                visible=canvas.visible_cards(canvas.mapToScene(canvas.viewport().rect()).boundingRect())
                assert visible, 'Quality checks must cover a nonempty viewport'
                complete=True
                missing=[]
                for visible_card in visible:
                    visible_item=browser.model.items[visible_card.row]
                    original=visible_item._image_size
                    crop=visible_item.crop
                    pixels=max(visible_card._size.width(),visible_card._size.height())*scale*canvas.viewport().devicePixelRatioF()
                    pixels*=max(original.width(),original.height())/max(crop.width(),crop.height(),1)
                    needed=min(lod.level_for_pixels(pixels),max(original.width(),original.height()))
                    candidates=[getattr(visible_item,'_preview_pixmap',None)]
                    detailed=any(pix is not None and not pix.isNull() and max(pix.width(),pix.height())>=needed for pix in candidates)
                    if not detailed:
                        candidates.extend(lod.cache().get(visible_item,level,touch=False)
                                          for level in lod.LEVELS if level>=needed)
                        detailed=any(pix is not None and not pix.isNull() and max(pix.width(),pix.height())>=needed for pix in candidates)
                    complete &= detailed
                    if not detailed:
                        missing.append(dict(row=visible_card.row,needed=needed,
                                            resident=max((max(p.width(),p.height()) for p in candidates if p is not None),default=0),
                                            request=(visible_item._display_decode_request[3] if visible_item._display_decode_request else None),
                                            failed=visible_item._display_decode_failed,
                                            needed_by_view=canvas.needs_display(visible_item),
                                            in_current_view=visible_card.sceneBoundingRect().intersects(canvas.mapToScene(canvas.viewport().rect()).boundingRect())))
                now=time.perf_counter()
                if now-last_detail_report[0]>.5 or complete:
                    last_detail_report[0]=now
                    decoder=getattr(qapp,'_prism_display_decoder',None)
                    report['quality_live']=dict(row=row,scale=scale,visible=len(visible),missing=len(missing),
                                                samples=missing[:12],queued=len(decoder.requests) if decoder else 0,
                                                pending_groups=len(canvas._composites.pending),
                                                detail_cursor=getattr(canvas,'_details_cursor',0),
                                                detail_total=len(getattr(canvas,'_details_cards',[])),
                                                live_visible=len(canvas.visible_cards(canvas.mapToScene(canvas.viewport().rect()).boundingRect())))
                    persist()
                observer_cost.append((time.perf_counter()-observation_start)*1000)
                return complete
            try:
                qtbot.waitUntil(ready,timeout=30000)
            finally:
                phase_patch.undo()
                if phase_timings:
                    report['quality_phase_timings']={name:dict(**stats(values),total_ms=round(sum(values),2))
                                                     for name,values in phase_timings.items()}
                if phase_enabled:
                    phase_profile.disable()
                    phase_profile.dump_stats(str(OUT/f'{label}-quality.prof'))
                report.setdefault('quality_observer',[]).append(dict(scale=scale,row=row,**stats(observer_cost),total_ms=round(sum(observer_cost),2)))
            quality.append(dict(row=row,scale=scale,visible_images=len(visible),ms=round((time.perf_counter()-start)*1000,2)))
            if screen_measure:
                from math import ceil,log2
                last_screen=[None,0.0]
                tail_patch=pytest.MonkeyPatch()
                tail_times={}
                tail_blocks={}
                tile_events=[]
                def time_tail(owner,name,label):
                    original=getattr(owner,name)
                    samples=tail_times.setdefault(label,[])
                    def timed(*args,**kwargs):
                        stamp=time.perf_counter()
                        if label=='tile_build':
                            key=args[0]
                            tile_events.append(dict(key=key,reason='new' if key not in owner.tiles else 'stale' if key in owner.stale else 'clean',
                                                    visible=key in owner.visible,bytes=owner.bytes,tiles=len(owner.tiles)))
                        try:return original(*args,**kwargs)
                        finally:samples.append((time.perf_counter()-stamp)*1000)
                    tail_patch.setattr(owner,name,timed)
                time_tail(canvas._tiles,'_build','tile_build')
                time_tail(canvas._tiles,'invalidate','invalidate')
                time_tail(canvas.delegate,'paint','delegate_paint')
                time_tail(canvas,'queue_visible_details','detail_queue')
                def blocked(reason):
                    tail_blocks[reason]=tail_blocks.get(reason,0)+1
                    return False
                def screen_settled():
                    image=canvas.viewport().grab().toImage()
                    sample_screen(image)
                    viewport=canvas.mapToScene(canvas.viewport().rect()).boundingRect()
                    groups=canvas._composites
                    group_mode=canvas.gridSize().width()*abs(canvas.transform().m11())<groups.DETAIL_PIXELS
                    if group_mode:
                        for group,(bounds,_cards) in enumerate(groups.groups):
                            if not bounds.intersects(viewport):
                                continue
                            edge=max(bounds.width(),bounds.height())*abs(canvas.transform().m11())*canvas.viewport().devicePixelRatioF()
                            wanted=max(32,2**ceil(log2(max(1,edge))))
                            if wanted<=groups.MAX_EDGE and ((group,wanted) not in groups.cache or (group,wanted) in groups.stale):
                                return blocked('groups')
                    tiled=len(canvas.cards)>=2000 and (group_mode or len(canvas.visible_cards(viewport))>=128)
                    if tiled and any(key not in canvas._tiles.tiles or key in canvas._tiles.stale for key in canvas._tiles.visible):
                        return blocked('tiles')
                    if last_screen[0] is None or image!=last_screen[0]:
                        last_screen[:]=[image,time.perf_counter()]
                        return False
                    return time.perf_counter()-last_screen[1]>=.2
                try:
                    qtbot.waitUntil(screen_settled,timeout=15000)
                finally:
                    tail_patch.undo()
                    report.setdefault('screen_tail',[]).append(dict(row=row,scale=scale,blocked=tail_blocks,tile_events=tile_events,
                        timings={name:dict(**stats(values),total_ms=round(sum(values),2)) for name,values in tail_times.items()}))
                final=screen_samples[-1][1]
                assert not final.isNull(), 'Focal pixel timing must sample a real visible image'
                first_match=next(ms for ms,image in screen_samples if image==final)
                report.setdefault('screen_recovery',[]).append(dict(row=row,scale=scale,
                    first_focal_final_pixels_ms=round(first_match,2),
                    whole_view_settled_ms=round((time.perf_counter()-start)*1000,2),
                    samples=len(screen_samples),proof='passive native pixels plus fresh visible group/tile cache'))
                assert canvas.viewport().grab().save(str(OUT/f'{label}-screen-{row}-{scale}.png'))
                persist()
        report['quality_recovery']=quality
        report['quality_check']='renderer-driven; cache observation only; no test decode requests'
        canvas.viewport().grab().save(str(OUT/f'{label}-zoom-detail.png'))
        hold=int(os.environ.get('PRISM_SCALE_REVIEW_HOLD','0'))
        if hold:
            stage('manual_review',lambda:qtbot.wait(min(hold,600)*1000))
        # Exercise the other high-volume entry points using this exact source
        # library. Reassigning fixture metadata is not counted as UI latency.
        report['scope_counts']={}
        stage('untagged_open',lambda:browser.open('untagged'))
        report['scope_counts']['untagged']=len(browser.model.items)
        assignments=[item._canvas_id for item in items]
        try:
            for item in items:
                item._canvas_id='default-canvas'
            stage('unclassified_100k_open',lambda:browser.open('unclassified'))
            assert len(browser.model.items)==100000
            report['scope_counts']['unclassified']=len(browser.model.items)
            stage('default_canvas_100k_open',lambda:window._open_resource_page('canvas','default-canvas'))
            assert len(browser.model.items)==100000
            assert window.tabs.currentWidget() is browser
            report['scope_counts']['default_canvas']=len(browser.model.items)
        finally:
            for item, canvas_id in zip(items,assignments):
                item._canvas_id=canvas_id
        persist()
        mats=window.material_library
        stage('material_library_open',window.open_material_library)
        qtbot.waitUntil(lambda:mats.materials.ready,timeout=20000)
        stage('material_records_in_browser',lambda:qtbot.waitUntil(lambda:javascript(mats.materials,'filtered.length===5000&&!applying',qtbot),timeout=30000))
        report['material_dom_cards']=javascript(mats.materials,"document.querySelectorAll('.asset-card').length",qtbot)
        stage('material_scroll_end',lambda:javascript(mats.materials,'window.scrollTo(0,document.documentElement.scrollHeight);true',qtbot))
        qtbot.wait(800)
        window.grab().save(str(OUT/f'{label}-materials-5000.png'))
        from prism.material_textures import export_originals
        originals=view.scene.material_records
        export_root = Path(tempfile.mkdtemp(prefix=f'{label}-', dir='E:/Prism-stress-100k'))
        report['export_directory']=str(export_root)
        def check_exports():
            count=0
            for index in [0,999,2499,4999]:
                record=originals[index]
                paths=export_originals(record,view.scene.attachments,
                                       export_root/str(index))
                assert {hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in paths}=={f['sha256'] for f in record['files']}
                count+=len(paths)
            return count
        with ThreadPoolExecutor(max_workers=1) as worker:
            future=worker.submit(check_exports)
            stage('export_exact_source_files',lambda:qtbot.waitUntil(future.done,timeout=30000))
            report['verified_export_files']=future.result()
        for kind in ['document','mindmap']:
            candidates=[p for p in pages if p['kind']==kind]
            for index in [0,249,499]:
                page=candidates[index]
                stage(f'{kind}_open_{index}',lambda p=page:window.open_workspace_page(kind,p['id']))
                panel=window._page_panels[page['id']]
                qtbot.waitUntil(lambda:getattr(panel,'_ready',False) and not getattr(panel,'_pending_push',False),timeout=25000)
        stage('global_search',lambda:search_project(view.scene,view.category_panel.resource_service,'Stress 099999',[],page_records=view.category_panel._pages_with_implicit_defaults()))
        timer.stop();report['heartbeat']=stats(pulses);report['display_cache_bytes']=lod.cache().bytes;report['shared_overview_bytes']=lod.cache().overview_bytes;report['shared_overview_entries']=len(lod.cache().overviews)
        report['completed']=True;persist()
    finally:
        timer.stop();profile.disable()
        if os.environ.get('PRISM_SCALE_PROFILE')=='1':profile.dump_stats(str(OUT/f'{label}.prof'))
        trace.close();persist()
        view.undo_stack.setClean()
