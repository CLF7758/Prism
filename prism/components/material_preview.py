"""Isolated model/cover host for the material library's shared Babylon renderer."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import sys
import time


def main():
    from PyQt6 import QtCore, QtGui, QtWidgets, QtWebEngineCore
    from prism.model_library import native_protocol as wire
    from prism.model_library.contracts import ModelCapabilities
    from prism.model_library.multimaterial.browser_scene import original_artifact, prepare_scene
    from prism.material_preview_profile import preview_profile
    from prism.widgets.material_render import MaterialRender
    from prism.widgets.model_preview import ModelPreviewWindow, PreviewWindowKeeper
    from prism.widgets.preview_theme import apply_preview_theme
    from prism.library_observability import exception_fields
    from prism.performance_trace import process_memory_bytes

    parser = argparse.ArgumentParser()
    parser.add_argument('--ipc-port', type=int, required=True)
    parser.add_argument('--ipc-token', required=True)
    parser.add_argument('--cache-root', required=True)
    parser.add_argument('--headless', action='store_true')
    args = parser.parse_args()
    QtCore.QCoreApplication.setAttribute(QtCore.Qt.ApplicationAttribute.AA_ShareOpenGLContexts)
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv[:1])
    apply_preview_theme(app)
    app.setQuitOnLastWindowClosed(False)
    channel = wire.Channel.connect('127.0.0.1', args.ipc_port, timeout=30)
    cache = Path(args.cache_root); cache.mkdir(parents=True, exist_ok=True)
    shell = ModelPreviewWindow()
    shell.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, False)
    shell.setAttribute(QtCore.Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    viewer = MaterialRender(shell)
    viewer.capture_enabled = False
    shell.install_viewport(viewer)
    shell.set_capabilities(ModelCapabilities(preview=True, wireframe=True, backend_version='Babylon'))
    shell.set_available_channels({key:'' for key in ('basecolor','normal','roughness','metallic','ao','opacity','emissive','sss','displacement')})
    lod_choice = QtWidgets.QComboBox(shell);lod_choice.hide()
    shell.layout().itemAt(0).layout().insertWidget(1, lod_choice)
    headless = args.headless or app.platformName() == 'offscreen'
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='model-browser-inputs')
    state = dict(closed=False, busy=None, future=None, identity={}, hello=False, ready=False,
                 loaded=False, owner=None, shown=False, sequence=0, pending=[], bootstrap_started=time.monotonic())

    def event(name, **fields):
        if not state['closed']:
            channel.send(wire.message(wire.EVENT, event=name, fields=dict(identity=state['identity'], **fields)))

    def send(action, data, callback):
        state['sequence'] += 1
        token = str(state['sequence'])
        state['callback'] = (token, callback)
        missing = json.dumps(json.dumps(dict(ok=False,error='共享模型渲染脚本未加载')))
        viewer.web.page().runJavaScript("typeof prismModel==='undefined'?bridge.model_result(" + json.dumps(token)
            + ',' + missing + '):prismModel.command(' + ','.join(json.dumps(v) for v in (token, action, data)) + ')')

    def reply(result):
        active = state['busy']
        if active is None:return
        envelope, started = active
        state['busy'] = None
        identity = envelope.get('identity') or {}
        job_id = envelope.get('job_id', '')
        payload = envelope.get('payload') or {}
        if result.get('stale'):
            channel.send(wire.message(wire.RESULT, job_id=job_id, identity=identity, state=wire.STATE_STALE))
            return
        if result.get('ok') is False:
            message = result.get('error', '模型预览失败')
            shell.set_status('预览失败：' + message)
            event('material.native.failed', job_id=job_id, action=payload.get('action', 'load'),
                  error_type=result.get('error_type') or 'ModelPreviewError',
                  error_code=wire.ERR_BAD_FILE, reason=message[-600:])
            channel.send(wire.message(wire.RESULT, job_id=job_id, identity=identity,
                state=wire.STATE_FAILED, error_code=wire.ERR_BAD_FILE, error_message=message[-600:]))
            return
        outcome = dict(result);outcome.pop('ok', None)
        image = outcome.pop('image', None)
        if image:
            try:
                blob = base64.b64decode(image.split(',', 1)[1], validate=True)
                decoded = QtGui.QImage.fromData(blob)
                if decoded.isNull() or len(blob) > 4*1024*1024:raise ValueError('封面图像无效或超过预算')
                destination = Path(payload.get('output') or cache/(hashlib.sha256(job_id.encode()).hexdigest()+'.png'))
                from prism.model_library.multimaterial.presentation import _publish_cache_file
                _publish_cache_file(destination, lambda staged: staged.write_bytes(blob))
                info=dict(path=str(destination),bytes=len(blob),sha256=hashlib.sha256(blob).hexdigest())
                # Existing thumbnail callers own their immutable row-cache path;
                # covers go through the controller's verified publication step.
                outcome['thumbnail' if payload.get('action')=='thumbnail' else 'outputs']=info
            except Exception as error:
                state['busy'] = active;reply(dict(ok=False,error=str(error)));return
        if state.get('statistics') and payload.get('action') != 'thumbnail':outcome['statistics'] = state['statistics']
        if payload.get('action')=='mode':shell.show_mode(payload['mode'])
        if payload.get('action')=='lod':
            event('material.lod_selected', level=payload.get('level'),
                  bindings=outcome.get('bindings', 'unknown'))
        elapsed_ms=(time.perf_counter()-started)*1000
        event('material.native.timing', job_id=job_id, action=payload.get('action', 'load'),
              elapsed_ms=elapsed_ms, timing=dict(request_wall_ms=elapsed_ms), renderer='Babylon',
              basis='request wall time; includes preparation and browser rendering')
        resources = dict(outcome.get('resources') or {})
        resources.setdefault('actors', 'unknown')
        event('material.native.resources', process_rss_bytes=process_memory_bytes(),
              decoded_texture_bytes='unknown', gpu_bytes='unknown',
              memory_basis='process RSS measured; decoded/GPU allocations not instrumented',
              **resources, renderer='Babylon')
        channel.send(wire.message(wire.RESULT, job_id=job_id, identity=identity,
            state=wire.STATE_COMPLETED, elapsed_ms=(time.perf_counter()-started)*1000, **outcome))

    def received(token, result):
        pending = state.get('callback')
        if pending is None or token != pending[0] or state['closed']:return
        state['callback'] = None
        try:pending[1](result)
        except Exception as error:
            if state['busy'] is not None:reply(dict(ok=False,error=str(error)))
            else:
                event('material.native.failed', **exception_fields(error))
                shutdown()
    viewer.scene_result.connect(received)
    def scene_event(name, fields):
        event(name, **fields)
        if name=='material.graphics_lost':
            state['graphics_failed']=True
            state['loaded']=False
            state['callback']=None
            message='图形设备已断开，已停止渲染，请重启程序后再打开预览'
            shell.set_status(message)
            if state['busy']:reply(dict(ok=False,error=message))
    viewer.scene_event.connect(scene_event)

    def bootstrapped(result):
        if not result.get('ok'):
            event('material.native.environment', state='unavailable', renderer='Babylon', reason=result.get('error'))
            shutdown();return
        state.update(ready=True, hello=True)
        hello = wire.Handshake(worker=wire.WORKER_NAME_PREVIEW, backend_version='Babylon-shared/1',
            python_version=platform.python_version(), pid=os.getpid(),
            formats={ext:'prism-source+assimp' for ext in wire.SUPPORTED_FORMATS},
            wireframe=True, material_edges=True, appearance=True, statistics='source-binding+ear-clipping',
            features={'bundled':True,'material_editing':True,'renderer':'Babylon'},
            limits={'max_frame_bytes':wire.MAX_FRAME_BYTES})
        channel.send(wire.message(wire.HELLO, token=args.ipc_token, handshake=hello.to_payload()))
        graphics = result.get('gpu', {})
        event('material.native.environment', state='available', renderer='Babylon',
              gpu_vendor=graphics.get('vendor'),gpu_renderer=graphics.get('renderer'),opengl_version=graphics.get('version'))

    def browser_ready():
        print('Prism Babylon: browser page ready', flush=True)
        QtCore.QTimer.singleShot(0, lambda:send('init',dict(profile=preview_profile('model')),bootstrapped))
    viewer.browser_ready.connect(browser_ready)

    def present(payload):
        if headless:return
        owner = payload.get('owner_window_id')
        from prism.qt_window_owner import present_owned_preview
        state['owner'], visibility = present_owned_preview(
            shell, payload.get('model_name', '模型') + ' — 模型预览', owner)
        state['shown'] = visibility['qt_visible'] and visibility['native_visible'] is not False
        event('material.preview_presented', frontmost='unknown', owner_hint=bool(owner),
              renderer='Babylon', headless=False, **visibility)
        if not state['shown']:
            raise RuntimeError('模型已准备，但预览窗口未能显示，请重试')

    def prepared(future):
        try:
            scene_input = future.result()
            if state['closed'] or state['busy'] is None:return
            if state.pop('cancelled', False):
                cancelled();return
            envelope = state['busy'][0];payload = envelope.get('payload') or {}
            state['statistics'] = scene_input['geometry']['statistics']
            def installed(result):
                if not result.get('ok'):reply(result);return
                state['loaded'] = True
                state['loaded_identity']=dict(envelope.get('identity') or {})
                with QtCore.QSignalBlocker(lod_choice):
                    lod_choice.clear()
                    for level in result.get('lod_levels', []):lod_choice.addItem('LOD '+str(level), level)
                    lod_choice.addItem('全部 LOD（对照）', None)
                lod_choice.setVisible(bool(result.get('lod_levels')))
                def finish(value):
                    if state['shown']:shell.set_status('拖动旋转、滚轮缩放；Ctrl＋单击选择部位')
                    if state.get('first_frame_key') != scene_input['geometry']['key']:
                        state['first_frame_key'] = scene_input['geometry']['key']
                        event('material.first_frame', bindings=len(value.get('bindings', [])), renderer='Babylon',
                              headless=headless, shown=state['shown'])
                    if envelope.get('kind') == 'thumbnail' and not payload.get('action'):
                        send('cover',dict(camera=payload.get('camera'),width=payload.get('width',512),height=payload.get('height',512)),reply)
                    else:reply(value)
                if payload.get('camera'):
                    send('camera',dict(camera=payload['camera']),finish)
                else:finish(result)
            send('load', scene_input, installed)
        except Exception as error:
            event('material.native.failed', **exception_fields(error))
            reply(dict(ok=False,error=str(error)))

    def prepare(payload):
        artifact = json.loads(Path(payload['artifact_ref']).read_text(encoding='utf-8')) if payload.get('artifact_ref') else original_artifact(
            payload['model_path'],cache,payload.get('parameter_snapshot'))
        geometry_root = Path(payload['artifact_ref']).parent/'geometry' if payload.get('artifact_ref') else cache
        return prepare_scene(artifact,geometry_root)

    def start(envelope):
        payload = envelope.get('payload') or {};action = payload.get('action', 'load')
        state['identity'] = envelope.get('identity') or {}
        state['busy'] = (envelope, time.perf_counter())
        if state.get('graphics_failed'):
            reply(dict(ok=False,error='图形设备已断开，请重启程序后再打开预览'))
            return
        if action in ('load', 'open'):
            if payload.get('show') or action == 'open':
                present(payload);shell.set_status('正在准备模型…')
            # Showing the window may reset the browser's scene independently
            # of our loaded flag. Always install through the existing load
            # path, which already caches geometry and prepared materials.
            state['loaded'] = False
            state['future'] = pool.submit(prepare, payload)
        elif action == 'thumbnail':
            state['loaded'] = False
            send(action,dict(material=payload['material'],profile=preview_profile('model')),reply)
        else:
            expected=payload.get('expected_scene')
            if action=='cover' and expected and (not state['loaded'] or any(
                    state.get('loaded_identity',{}).get(key)!=value for key,value in expected.items())):
                reply(dict(ok=False,error='模型预览已切换，请在当前模型加载完成后重新设置封面'))
                return
            if action == 'release' or payload.get('release_scene'):
                state['loaded'] = False
            data = dict(payload)
            if action == 'highlight':
                deadline = data.pop('highlight_deadline', None)
                if deadline is not None:data['duration_ms'] = max(0,(deadline-time.monotonic())*1000)
            send(action, data, reply)

    def local(action, data=None, callback=None):
        if state.get('graphics_failed'):return
        if state['busy'] or state.get('callback') or not state['ready']:return
        send(action,data or {},callback or (lambda result:shell.set_status(result.get('error',''))))
    def change_mode(value):
        def finished(result):
            if result.get('ok'):shell.show_mode(value)
            else:shell.set_status(result.get('error','切换显示模式失败'))
        local('mode',dict(mode=value),finished)
    shell.view_mode_requested.connect(change_mode)
    shell.reset_requested.connect(lambda:local('reset'))
    shell.cover_requested.connect(lambda:local('state',callback=lambda result:event('material.cover_requested',camera=result['camera'])
        if result.get('ok') else shell.set_status(result.get('error','封面失败'))))
    lod_choice.currentIndexChanged.connect(lambda:local('lod',dict(level=lod_choice.currentData())))

    def closed_window():
        state['shown'] = False;state['loaded'] = False
        shell.show_mode('material')
        state['first_frame_key'] = None
        event('material.preview_closed',reason='window_closed')
        if state['busy']:
            if state['future'] is not None and not state['future'].done():state['cancelled'] = True
            else:cancelled()
        local('release')
    keeper = PreviewWindowKeeper(shell, closed_window)

    def shutdown():
        if state['closed']:return
        state['closed'] = True
        timer.stop();pool.shutdown(wait=False, cancel_futures=True)
        try:channel.send(wire.message(wire.BYE))
        except Exception:pass
        viewer.release_material();channel.close();app.quit()

    shell.close_requested.connect(shutdown)
    def cancelled():
        active = state['busy']
        if active is None:return
        state['busy'] = None;state['callback'] = None
        viewer.web.page().runJavaScript("prismModel.execute('cancel',{})")
        channel.send(wire.message(wire.RESULT, job_id=active[0]['job_id'], identity=active[0]['identity'],
                                  state=wire.STATE_CANCELLED))
    def poll():
        if state['closed']:return
        try:
            message = channel.recv(timeout=0)
            if message is None and channel.closed:shutdown();return
            if message and message.get('type') in (wire.SHUTDOWN,wire.CLOSE_PROJECT):shutdown();return
            if message and message.get('type') == wire.CANCEL:
                if state['busy'] and state['busy'][0]['job_id'] == message.get('job_id'):
                    if state['future'] is not None and not state['future'].done():state['cancelled'] = True
                    else:cancelled()
                state['pending'] = [m for m in state['pending'] if m['job_id'] != message.get('job_id')]
            if message and message.get('type') == wire.REQUEST:state['pending'].append(message)
            future = state.get('future')
            if future is not None and future.done():
                state['future'] = None;prepared(future)
            if state['ready'] and not state['busy'] and not state.get('callback') and state['pending']:
                start(state['pending'].pop(0))
            if not state['hello'] and time.monotonic()-state['bootstrap_started']>40:shutdown()
        except Exception as error:
            try:event('material.native.failed', **exception_fields(error))
            finally:shutdown()
    timer = QtCore.QTimer(app);timer.timeout.connect(poll);timer.start(20)
    viewer._ensure_web()  # Covers also load while the shell has no exposed surface.
    viewer.web.loadFinished.connect(lambda ok:print('Prism Babylon: page loaded='+str(ok),flush=True))
    shell.show()  # A hidden native surface; headless mode never presents it to the user.
    return app.exec()


if __name__ == '__main__':raise SystemExit(main())
