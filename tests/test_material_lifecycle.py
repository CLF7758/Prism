"""Preview ownership, hidden views and obsolete IO must not retain live work."""
import base64
import io
import json
from pathlib import Path
import shutil
import subprocess
from unittest.mock import MagicMock

import pytest
from PyQt6 import QtWidgets

from prism.widgets.material_render import MaterialRender, MaterialRenderQueue, _RenderBridge
from prism.widgets.material_task import MaterialTask


@pytest.fixture(autouse=True)
def stub_offscreen_webengine(monkeypatch, qapp):
    # Native WebGL remains a separate acceptance run; offscreen must not start
    # Chromium. This fixture also precedes the existing main_window fixture.
    if QtWidgets.QApplication.platformName() != 'offscreen':
        return
    def ensure(viewer):
        if viewer.web is None:
            viewer.web = MagicMock()
            viewer.bridge = _RenderBridge(viewer)
    monkeypatch.setattr(MaterialRender, '_ensure_web', ensure)
    from prism.widgets.material_cards import MaterialCards
    monkeypatch.setattr(MaterialCards, '_ensure_web', lambda self: None)


@pytest.fixture
def renderer(qtbot, monkeypatch):
    monkeypatch.setattr(MaterialRender, '_ensure_web', lambda self: None)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.web = MagicMock()
    viewer.bridge = _RenderBridge(viewer)
    viewer.ready = True
    viewer.show()
    return viewer


def _payload(key):
    return {'recordId': str(key), 'signature': 'sig-' + str(key), 'maps': {},
            'properties': {'controls': {}}}


def _loads(viewer):
    return [json.loads(call.args[0][len('prismRender.load('):-1])
            for call in viewer.web.page().runJavaScript.call_args_list
            if call.args[0].startswith('prismRender.load(')]


def _observe(viewer):
    events = []
    viewer.updated.connect(lambda signature: events.append(('updated', signature)))
    viewer.completed.connect(lambda signature, blob: events.append(('completed', signature, blob)))
    viewer.failed.connect(lambda message: events.append(('failed', message)))
    viewer.model_loaded.connect(lambda: events.append(('model_loaded',)))
    viewer.request_updated.connect(lambda token, signature: events.append(('request_updated', token, signature)))
    viewer.request_completed.connect(lambda token, signature, blob: events.append(('request_completed', token, signature, blob)))
    viewer.request_failed.connect(lambda token, message: events.append(('request_failed', token, message)))
    return events


def _png_url():
    from PIL import Image
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(stream, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode('ascii')


def test_native_release_and_reload_keep_environment_and_drop_old_textures(qtbot, tmp_path):
    import pytest
    from PIL import Image
    from PyQt6 import QtWidgets
    from prism.material_textures import prepare_texture, prepare_render
    if QtWidgets.QApplication.platformName() != 'windows':
        pytest.skip('native WebGL lifecycle')
    source = tmp_path / 'source.png'
    Image.new('RGB', (32, 32), (30, 110, 190)).save(source)
    info, blob = prepare_texture(source)
    payload = prepare_render({'id': 'a', 'textures': {'basecolor': dict(info, attachmentId=1,
                            channel='RGB')}, 'preview': {}},
                            {1: (info['name'], info['mime'], blob)}, tmp_path / 'inputs')
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.resize(320, 320)
    viewer.show()
    updates, errors = [], []
    viewer.updated.connect(updates.append)
    viewer.failed.connect(errors.append)
    viewer.load_material(payload, 'first')
    qtbot.waitUntil(lambda: bool(updates or errors), timeout=45000)
    assert not errors, errors
    def query(expression):
        result = []
        viewer.web.page().runJavaScript(expression, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=8000)
        return result[0]
    assert query("Object.keys(maps).length") == 1
    source_texture_ids = query('Object.values(maps).map(t=>t.uniqueId)')
    baseline_environment = query('scene.environmentTexture.uniqueId')
    viewer.release_material()
    assert query('current===null && sphere===null && material===null && Object.keys(maps).length===0')
    assert query('scene.environmentTexture.uniqueId') == baseline_environment
    remaining_texture_ids = query('scene.textures.map(t=>t.uniqueId)')
    # Babylon also owns its generated BRDF lookup texture. Test ownership,
    # rather than assuming the environment is the engine's only texture.
    assert not set(source_texture_ids) & set(remaining_texture_ids)
    assert baseline_environment in remaining_texture_ids
    assert query('engine._activeRenderLoops.length') == 0
    updates.clear()
    payload['recordId'] = 'b'
    viewer.load_material(payload, 'second')
    qtbot.waitUntil(lambda: bool(updates or errors), timeout=45000)
    assert not errors, errors
    assert query('current.token') == 'second'
    assert query('current.recordId') == 'b'
    assert query('Object.keys(maps).length') == 1
    assert query('scene.environmentTexture.uniqueId') == baseline_environment
    viewer.release_material()
    assert query('scene.textures.map(t=>t.uniqueId)') == remaining_texture_ids


@pytest.mark.parametrize('action', ['once', 'chain', 'release'])
def test_native_load_with_delayed_displacement_settles_latest_shape(qtbot, action):
    if QtWidgets.QApplication.platformName() != 'windows':
        pytest.skip('native WebGL shape lifecycle')
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.capture_enabled = False
    viewer.resize(320, 320)
    viewer.show()
    qtbot.waitUntil(lambda: viewer.ready, timeout=15000)
    updates, errors = [], []
    viewer.request_updated.connect(lambda token, signature: updates.append((token, signature)))
    viewer.failed.connect(errors.append)

    def query(expression):
        result = []
        viewer.web.page().runJavaScript(expression, result.append)
        qtbot.waitUntil(lambda: bool(result), timeout=8000)
        return result[0]

    # Delay only the native Image used by the real displacement function, not
    # Babylon's texture/environment IO. Neither displacement nor WebGL is mocked.
    assert query(r"""(()=>{
      window.__heightTest={images:[],writes:[],updates:[],creating:false,
        Image:window.Image,displacement,apply:B.Mesh.prototype.applyDisplacementMapFromBuffer,
        updated:bridge.updated};
      const test=window.__heightTest,src=Object.getOwnPropertyDescriptor(HTMLImageElement.prototype,'src');
      displacement=function(...args){test.creating=true;try{return test.displacement(...args);}finally{test.creating=false;}};
      window.Image=function(...args){
        const image=new test.Image(...args);if(!test.creating)return image;
        const entry={image,url:null,loaded:false};
        image.addEventListener('load',()=>{entry.loaded=true;});
        Object.defineProperty(image,'src',{get(){return src.get.call(image);},set(url){entry.url=url;test.images.push(entry);}});
        return image;
      };
      window.Image.prototype=test.Image.prototype;
      test.resolve=index=>{const entry=test.images[index];src.set.call(entry.image,entry.url);};
      B.Mesh.prototype.applyDisplacementMapFromBuffer=function(...args){test.writes.push(this.uniqueId);return test.apply.apply(this,args);};
      bridge.updated=function(...args){test.updates.push(args);return test.updated.apply(bridge,args);};
      return true;
    })()""")
    try:
        payload = _payload('delayed-shape')
        payload['maps']['displacement'] = _png_url()
        viewer.load_material(payload, 'delayed-shape')
        qtbot.waitUntil(lambda: query('__heightTest.images.length') == 1 or bool(errors), timeout=20000)
        assert not errors, errors
        original_request = viewer._active.wire_token
        obsolete_mesh = query('sphere.uniqueId')
        choices = ['sphere'] if action == 'once' else ['sphere', 'box', 'cylinder']
        for shape in choices:
            viewer.set_shape(shape)
        qtbot.waitUntil(lambda: query('__heightTest.images.length') == len(choices) + 1, timeout=8000)
        latest_mesh = query('sphere.uniqueId')
        assert latest_mesh != obsolete_mesh
        assert not updates and query('__heightTest.updates.length') == 0
        assert query('__heightTest.writes.length') == 0
        if action == 'release':
            viewer.release_material()
            assert query('current===null && sphere===null && material===null')
        else:
            # The newest shape completes while every obsolete Image is still
            # withheld. Completion must retain the original load's wire identity.
            query(f'__heightTest.resolve({len(choices)}); true')
            qtbot.waitUntil(lambda: bool(updates or errors), timeout=45000)
            assert not errors, errors
            assert updates == [('delayed-shape', 'sig-delayed-shape')]
            assert not viewer._render_deadline.isActive()
            assert viewer._active.phase == 'updated'
            assert viewer._active.wire_token == original_request
            assert query('__heightTest.writes') == [latest_mesh]
            assert query('__heightTest.updates') == [[original_request, 'sig-delayed-shape']]
            assert query('prismRender.diagnostics.shape') == choices[-1]
        withheld_count = len(choices) + (1 if action == 'release' else 0)
        query(f'for(let i=0;i<{withheld_count};i++)__heightTest.resolve(i); true')
        qtbot.waitUntil(lambda: query('__heightTest.images.every(entry=>entry.loaded)'), timeout=8000)
        # A round trip occurs after the Images' load handlers and microtasks.
        assert not errors, errors
        assert query('__heightTest.writes') == ([] if action == 'release' else [latest_mesh])
        assert query('__heightTest.updates.length') == (0 if action == 'release' else 1)
        assert len(updates) == (0 if action == 'release' else 1)
        if action == 'release':
            assert query('current===null && sphere===null && material===null')
            assert not viewer._render_deadline.isActive()
    finally:
        viewer.release_material()
        query("window.Image=__heightTest.Image; displacement=__heightTest.displacement; "
              "B.Mesh.prototype.applyDisplacementMapFromBuffer=__heightTest.apply; "
              "bridge.updated=__heightTest.updated; true")


def test_hidden_view_defers_webchannel_ready_and_capture(qtbot, monkeypatch):
    monkeypatch.setattr(MaterialRender, '_ensure_web', lambda self: None)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.web = MagicMock()
    viewer.ready = True
    viewer.load_material({'recordId': 'a', 'maps': {}}, 'a-token')
    viewer._capture()
    viewer.web.page().runJavaScript.assert_not_called()
    assert not viewer._render_deadline.isActive()
    assert viewer._push_pending
    viewer.show()
    calls = [call.args[0] for call in viewer.web.page().runJavaScript.call_args_list]
    assert any(call.startswith('prismRender.load(') for call in calls)
    assert not viewer._push_pending
    viewer._capture_timer.start()
    viewer.hide()
    assert not viewer._capture_timer.isActive()
    from prism.widgets.material_render import _RenderBridge
    _RenderBridge(viewer).updated('a-token', 'late-update')
    assert not viewer._capture_timer.isActive()


def test_release_invalidates_bridge_results_and_drops_payload(qtbot, monkeypatch):
    from prism.widgets.material_render import _RenderBridge
    monkeypatch.setattr(MaterialRender, '_ensure_web', lambda self: None)
    viewer = MaterialRender()
    qtbot.addWidget(viewer)
    viewer.web = MagicMock()
    viewer.ready = True
    viewer.payload = {'recordId': 'a', 'maps': {'roughness': 'original-path'}}
    viewer.token = 'old-token'
    viewer._capture_timer.start()
    viewer._render_deadline.start()
    updated = []
    viewer.updated.connect(updated.append)
    viewer.release_material()
    _RenderBridge(viewer).updated('old-token', 'stale-signature')
    assert not updated
    assert viewer.payload is None and viewer.token == ''
    assert not viewer._capture_timer.isActive()
    assert not viewer._render_deadline.isActive()
    viewer.web.page().runJavaScript.assert_called_with('prismRender.release()')


def test_obsolete_io_result_is_discarded_but_finished_is_emitted():
    obsolete = [False]
    def work():
        obsolete[0] = True
        return 'stale-payload'
    job = MaterialTask(work, lambda: obsolete[0])
    result, finished = [], []
    job.signals.result.connect(result.append)
    job.signals.finished.connect(lambda: finished.append(True))
    job.run()
    assert result == [] and finished == [True]


def test_hiding_library_releases_only_inline_and_preserves_records(main_window, monkeypatch, qtbot):
    page = main_window.material_library
    main_window.open_material_library()
    record = page.records.create(name='Preserved')
    page.records.update(record['id'], name='Preserved', notes='note')
    page.records.configure(record['id'], tags=['tag'])
    page._selection_changed(record['id'])
    page._auto_timer.stop()
    before = page.records.get(record['id'])
    inline = page.details.inline_render
    inline.payload = {'recordId': record['id'], 'maps': {}}
    inline.token = 'old-token'
    # A separately owned dialog remains live when the library leaves the stack.
    dialog = QtWidgets.QDialog()
    qtbot.addWidget(dialog)
    dialog.material_viewer = MagicMock()
    dialog.material_viewer.payload = None
    dialog.material_viewer._pending = None
    page._dialogs.append(dialog)
    page.hideEvent(None)
    assert inline.payload is None
    assert page._prepare_token is None and page._auto_key is None
    assert not page._auto_timer.isActive()
    assert page.records.get(record['id']) == before
    dialog.material_viewer.release_material.assert_not_called()
    page._dialogs.remove(dialog)


def test_same_viewer_100_updates_execute_only_active_and_latest(renderer):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload(0), 'token-0')
    first = _loads(viewer)[0]
    started = viewer._load_started
    for index in range(1, 100):
        viewer.load_material(_payload(index), 'token-' + str(index))
    assert len(_loads(viewer)) == 1
    assert viewer.token == 'token-0' and viewer.payload['signature'] == 'sig-0'
    assert viewer._load_started == started and viewer._render_deadline.isActive()
    assert viewer._pending.token == 'token-99'
    viewer.bridge.updated(first['requestId'], first['signature'])
    assert events == []
    assert [p['token'] for p in _loads(viewer)] == ['token-0', 'token-99']
    assert viewer.token == 'token-99' and viewer._pending is None
    assert not viewer._capture_timer.isActive()
    viewer.bridge.updated(first['requestId'], first['signature'])
    assert len(_loads(viewer)) == 2 and not events
    latest = _loads(viewer)[1]
    viewer.bridge.updated(latest['requestId'], latest['signature'])
    assert events == [('request_updated', 'token-99', 'sig-99'), ('updated', 'sig-99')]


@pytest.mark.parametrize('state', ['hidden', 'not_ready'])
def test_waiting_requests_keep_only_latest(renderer, state):
    viewer = renderer
    if state == 'hidden':
        viewer.hide()
    else:
        viewer.ready = False
    for index in range(100):
        viewer.load_material(_payload(index), str(index))
    assert not _loads(viewer) and viewer.token == '' and viewer._active is None
    assert viewer._pending.token == '99' and not viewer._render_deadline.isActive()
    viewer.bridge.ready()
    if state == 'hidden':
        assert not _loads(viewer)
        viewer.show()
    assert [p['token'] for p in _loads(viewer)] == ['99']
    viewer.bridge.ready()  # Duplicate ready must not launch it again.
    assert len(_loads(viewer)) == 1


def test_wrong_identity_signature_and_duplicate_callbacks_are_ignored(renderer):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'public-token')
    request = _loads(viewer)[-1]
    # Public tokens are not execution identities, even for their first use.
    for identity, signature in [('public-token', 'sig-a'), ('old', 'sig-a'),
                                (request['requestId'], 'wrong-signature')]:
        viewer.bridge.updated(identity, signature)
        viewer.bridge.rendered(identity, signature, _png_url())
        viewer.bridge.frame_submitted(identity, signature)
    viewer.bridge.error('old', 'obsolete')
    viewer.bridge.loaded(request['requestId'], 'sig-a')  # Not a model request.
    assert not events and viewer._active.phase == 'rendering'
    assert viewer._render_deadline.isActive()
    assert not viewer._active.frame_submitted
    viewer.bridge.frame_submitted(request['requestId'], 'sig-a')
    viewer.bridge.updated(request['requestId'], 'sig-a')
    viewer.bridge.updated(request['requestId'], 'sig-a')
    assert events == [('request_updated', 'public-token', 'sig-a'), ('updated', 'sig-a')]
    viewer._capture()
    assert viewer._active.phase == 'capturing'
    viewer.bridge.rendered(request['requestId'], 'wrong', _png_url())
    assert len(events) == 2
    viewer.bridge.rendered(request['requestId'], 'sig-a', _png_url())
    assert events[2][:3] == ('request_completed', 'public-token', 'sig-a')
    assert events[3][:2] == ('completed', 'sig-a')
    assert events[2][3] == events[3][2]
    assert not viewer._render_deadline.isActive()
    viewer.bridge.rendered(request['requestId'], 'sig-a', _png_url())
    viewer.bridge.updated(request['requestId'], 'sig-a')
    viewer.bridge.error(request['requestId'], 'too late')
    viewer._render_timeout()
    assert len(events) == 4


@pytest.mark.parametrize('settle', ['error', 'timeout'])
def test_obsolete_failure_settles_and_starts_latest_without_public_error(renderer, settle):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'a')
    first = _loads(viewer)[-1]
    viewer.load_material(_payload('b'), 'b')
    viewer.load_material(_payload('c'), 'c')
    if settle == 'error':
        viewer.bridge.error(first['requestId'], 'old failure')
    else:
        viewer._render_timeout()
    assert not events
    assert [p['token'] for p in _loads(viewer)] == ['a', 'c']
    assert viewer.token == 'c' and viewer._render_deadline.isActive()
    viewer.bridge.updated(first['requestId'], 'sig-a')
    viewer.bridge.error(first['requestId'], 'late failure')
    viewer.bridge.rendered(first['requestId'], 'sig-a', _png_url())
    assert not events
    latest = _loads(viewer)[-1]
    viewer.bridge.updated(latest['requestId'], latest['signature'])
    assert events == [('request_updated', 'c', 'sig-c'), ('updated', 'sig-c')]


@pytest.mark.parametrize('settle', ['error', 'timeout', 'invalid_image'])
def test_failure_rejects_all_late_results_and_same_token_retry(renderer, settle):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('same'), 'reused')
    first = _loads(viewer)[-1]
    if settle == 'invalid_image':
        viewer.bridge.updated(first['requestId'], 'sig-same')
        viewer._capture()
        events.clear()
        viewer.bridge.rendered(first['requestId'], 'sig-same', 'bad-image')
    elif settle == 'timeout':
        viewer._render_timeout()
    else:
        viewer.bridge.error(first['requestId'], 'failure')
    assert [event[0] for event in events] == ['request_failed', 'failed']
    assert events[0][1] == 'reused'
    assert not viewer._render_deadline.isActive() and not viewer._capture_timer.isActive()
    events.clear()
    viewer.bridge.error(first['requestId'], 'duplicate')
    viewer.bridge.updated(first['requestId'], 'sig-same')
    viewer.bridge.rendered(first['requestId'], 'sig-same', _png_url())
    viewer._render_timeout()
    assert not events
    viewer.load_material(_payload('same'), 'reused')
    retry = _loads(viewer)[-1]
    assert retry['requestId'] != first['requestId']
    viewer.bridge.updated(first['requestId'], 'sig-same')
    viewer.bridge.error(first['requestId'], 'previous incarnation')
    assert not events and viewer._active.phase == 'rendering'
    viewer.bridge.updated(retry['requestId'], 'sig-same')
    assert events == [('request_updated', 'reused', 'sig-same'), ('updated', 'sig-same')]


@pytest.mark.parametrize('pending', [False, True])
def test_hide_invalidates_all_callbacks_and_show_resumes_latest(renderer, pending):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'a')
    first = _loads(viewer)[-1]
    if pending:
        viewer.load_material(_payload('b'), 'b')
    viewer.hide()
    assert viewer.token == '' and viewer._active is None
    assert viewer._pending.token == ('b' if pending else 'a')
    assert not viewer._capture_timer.isActive() and not viewer._render_deadline.isActive()
    viewer.bridge.updated(first['requestId'], first['signature'])
    viewer.bridge.error(first['requestId'], 'hidden')
    viewer.bridge.rendered(first['requestId'], first['signature'], _png_url())
    viewer.bridge.loaded(first['requestId'], first['signature'])
    viewer._render_timeout()
    assert not events
    viewer.show()
    resumed = _loads(viewer)[-1]
    assert resumed['token'] == ('b' if pending else 'a')
    assert resumed['requestId'] != first['requestId']
    viewer.bridge.updated(first['requestId'], first['signature'])
    assert not events
    viewer.bridge.updated(resumed['requestId'], resumed['signature'])
    assert events[0] == ('request_updated', resumed['token'], resumed['signature'])


def test_release_clears_latest_and_capture_and_never_resumes(renderer):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'reused')
    old = _loads(viewer)[-1]
    viewer.bridge.updated(old['requestId'], old['signature'])
    viewer._capture()
    assert viewer._capture_request is viewer._active
    viewer.load_material(_payload('b'), 'b')
    viewer.load_material(_payload('c'), 'c')
    viewer.release_material()
    assert viewer._active is None and viewer._pending is None
    assert viewer._capture_request is None and viewer.token == '' and viewer.payload is None
    assert not viewer._render_deadline.isActive() and not viewer._capture_timer.isActive()
    events.clear()
    viewer.bridge.rendered(old['requestId'], old['signature'], _png_url())
    viewer.bridge.updated(old['requestId'], old['signature'])
    viewer.bridge.error(old['requestId'], 'released')
    before = len(_loads(viewer))
    viewer.hide()
    viewer.show()
    assert len(_loads(viewer)) == before and not events
    viewer.load_material(_payload('a'), 'reused')
    assert _loads(viewer)[-1]['requestId'] != old['requestId']
    viewer.bridge.rendered(old['requestId'], old['signature'], _png_url())
    assert not events


@pytest.mark.parametrize('signal', ['request_updated', 'updated'])
@pytest.mark.parametrize('action', ['load', 'release', 'hide'])
def test_updated_emit_reentry_never_arms_old_capture(renderer, signal, action):
    viewer = renderer
    viewer.load_material(_payload('a'), 'a')
    first = _loads(viewer)[-1]
    def reenter(*_):
        if action == 'load':
            viewer.load_material(_payload('b'), 'b')
        elif action == 'release':
            viewer.release_material()
        else:
            viewer.hide()
    getattr(viewer, signal).connect(reenter)
    viewer.bridge.updated(first['requestId'], first['signature'])
    assert not viewer._capture_timer.isActive() and viewer._capture_request is None
    if action == 'load':
        assert viewer.token == 'b' and viewer._active.phase == 'rendering'
        assert viewer._render_deadline.isActive()
    else:
        assert viewer.token == '' and not viewer._render_deadline.isActive()


@pytest.mark.parametrize('signal', ['request_failed', 'request_completed'])
def test_terminal_identity_emit_reentry_suppresses_old_legacy_signal(renderer, signal):
    viewer = renderer
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'a')
    old = _loads(viewer)[-1]
    getattr(viewer, signal).connect(lambda *_: viewer.load_material(_payload('b'), 'b'))
    if signal == 'request_completed':
        viewer.bridge.updated(old['requestId'], old['signature'])
        viewer._capture()
        events.clear()
        viewer.bridge.rendered(old['requestId'], old['signature'], _png_url())
    else:
        viewer.bridge.error(old['requestId'], 'failed a')
    assert [event[0] for event in events] == [signal]
    assert viewer.token == 'b' and viewer._render_deadline.isActive()
    assert not viewer._capture_timer.isActive()


def test_two_viewers_have_independent_execution_and_metric_identity(renderer, qtbot, monkeypatch):
    other = MaterialRender()
    qtbot.addWidget(other)
    other.web = MagicMock()
    other.bridge = _RenderBridge(other)
    other.ready = True
    other.show()
    first, second = _observe(renderer), _observe(other)
    records = []
    monkeypatch.setattr('prism.widgets.material_render.record', lambda event, **fields: records.append((event, fields)))
    renderer.load_material(_payload('a'), 'same-token')
    other.load_material(_payload('a'), 'same-token')
    a, b = _loads(renderer)[-1], _loads(other)[-1]
    renderer.load_material(_payload('new'), 'new')
    other.bridge.updated(a['requestId'], a['signature'])
    assert not first and not second
    other.bridge.frame_submitted(b['requestId'], b['signature'])
    other.bridge.frame_submitted(b['requestId'], b['signature'])
    other.bridge.updated(b['requestId'], b['signature'])
    assert second == [('request_updated', 'same-token', 'sig-a'), ('updated', 'sig-a')]
    assert renderer.token == 'same-token' and len(_loads(renderer)) == 1
    renderer.bridge.metrics('{"frames": 1}')
    other.bridge.metrics('{"frames": 2}')
    renderer.bridge.updated(a['requestId'], a['signature'])
    assert renderer.token == 'new' and len(_loads(renderer)) == 2
    assert len(_loads(other)) == 1
    assert renderer.viewer_id != other.viewer_id
    assert all(fields['viewer_id'] in (renderer.viewer_id, other.viewer_id) for _, fields in records)
    assert sum(event == 'material_frame_submitted' for event, _ in records) == 1
    assert {fields['viewer_id'] for event, fields in records if event == 'material_browser'} == {renderer.viewer_id, other.viewer_id}


def test_payload_snapshot_and_model_loaded_identity_settlement(renderer):
    viewer = renderer
    events = _observe(viewer)
    payload = dict(_payload('model'), model='file:///model.glb')
    viewer.load_material(payload, 'model-token')
    old = _loads(viewer)[-1]
    payload['signature'] = 'mutated'
    payload['properties']['controls']['new'] = {}
    assert viewer.payload['signature'] == 'sig-model'
    assert 'new' not in viewer.payload['properties']['controls']
    assert viewer.payload['properties']['controls']['basecolor']['linearColor'] == [1, 1, 1]
    viewer.bridge.loaded(old['requestId'], 'mutated')
    assert not events and viewer._render_deadline.isActive()
    viewer.load_material(_payload('next'), 'next')
    viewer.bridge.loaded(old['requestId'], 'sig-model')
    assert not events and viewer.token == 'next'
    viewer.bridge.loaded(old['requestId'], 'sig-model')
    assert len(_loads(viewer)) == 2
    viewer.release_material()
    viewer.load_material(dict(_payload('model'), model='file:///model.glb'), 'model-token')
    current = _loads(viewer)[-1]
    viewer.bridge.loaded(old['requestId'], 'sig-model')
    viewer.bridge.loaded(current['requestId'], 'sig-model')
    viewer.bridge.loaded(current['requestId'], 'sig-model')
    assert events == [('model_loaded',)] and not viewer._render_deadline.isActive()


@pytest.mark.parametrize('capture', [False, True])
def test_legacy_render_queue_still_completes(renderer, qtbot, capture):
    viewer = renderer
    queue = MaterialRenderQueue(viewer)
    queue.start([('a', _payload('a')), ('b', _payload('b'))], capture=capture)
    for index, key in enumerate(('a', 'b'), 1):
        qtbot.waitUntil(lambda: len(_loads(viewer)) == index)
        request = _loads(viewer)[-1]
        viewer.bridge.updated(request['requestId'], request['signature'])
        if capture:
            viewer._capture()
            viewer.bridge.rendered(request['requestId'], request['signature'], _png_url())
    qtbot.waitUntil(lambda: not queue.snapshot()['active'])
    assert queue.snapshot()['completed'] == 2 and queue.snapshot()['failed'] == 0
    assert set(queue.results()) == {'a', 'b'}
    assert all(bool(blob) == capture for _, blob in queue.results().values())


def test_real_deadline_event_starts_only_latest_and_never_emits_old_failure(renderer, qtbot):
    viewer = renderer
    viewer._render_deadline.setInterval(20)
    events = _observe(viewer)
    viewer.load_material(_payload('a'), 'a')
    old = _loads(viewer)[-1]
    for index in range(100):
        viewer.load_material(_payload(index), str(index))
    # Settle the new request synchronously when it is actually submitted, so
    # the test does not depend on beating its deliberately short deadline.
    def javascript(script):
        if script.startswith('prismRender.load('):
            request = json.loads(script[len('prismRender.load('):-1])
            viewer.bridge.updated(request['requestId'], request['signature'])
    viewer.capture_enabled = False
    viewer.web.page().runJavaScript.side_effect = javascript
    qtbot.waitUntil(lambda: viewer.token == '99')
    assert events == [('request_updated', '99', 'sig-99'), ('updated', 'sig-99')]
    viewer.bridge.updated(old['requestId'], old['signature'])
    assert not viewer._render_deadline.isActive() and len(_loads(viewer)) == 2


_JS_PREVIEW_PROFILE = json.loads((Path(__file__).resolve().parents[1] /
    'prism/assets/materials/preview-profile.json').read_text(encoding='utf-8'))

_JS_LIFECYCLE_HARNESS = r"""
const fs=require('fs'),vm=require('vm'),assert=require('assert');
const {source,stage,invalidate,profile}=JSON.parse(fs.readFileSync(0,'utf8'));
const deferred=()=>{let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return {promise,resolve,reject};};
const sceneReady=deferred(),assetReady=deferred(),screenshot=deferred();
const callbacks=[],rafs=[],images=[],events=[];
let submitted=0,displaced=0,canvasReads=0,containersDisposed=0,meshesCreated=0;
const vector=()=>({add(){return this;},scale(){return this;},subtractInPlace(){},scaleInPlace(){}});
const builtMeshes=[];
const mesh=(name='initial')=>({name,rotation:{},scaling:vector(),position:vector(),dispose(){this.disposed=true;},
  normalizeToUnitCube(){},computeWorldMatrix(){},getTotalVertices(){return 3;},
  getHierarchyBoundingVectors(){return {min:vector(),max:vector()};},
  applyDisplacementMapFromBuffer(){assert(!this.disposed);this.displaced=true;displaced++;}});
const build=name=>{const result=mesh(name);builtMeshes.push(result);return result;};
const pbr=()=>({dispose(){this.disposed=true;},isReady(){return true;},albedoColor:{asArray(){return [1,1,1];}},
  roughness:.5,metallic:0,alpha:1});
const container={meshes:[],materials:[],textures:[],dispose(){containersDisposed++;},
  addAllToScene(){},createRootMesh:mesh};
const bridge={ready(){},metrics(){},loaded(...args){events.push(['loaded',...args]);},
  updated(...args){events.push(['updated',...args]);},rendered(...args){events.push(['rendered',...args]);},
  frame_submitted(...args){events.push(['frame_submitted',...args]);},error(...args){events.push(['error',...args]);}};
const status={textContent:''};
const sandbox={console,performance:{now:()=>1},setTimeout,clearTimeout,
  XMLHttpRequest:class {
    open(method,path){assert.strictEqual(method,'GET');assert.strictEqual(path,'preview-profile.json');}
    send(){this.status=200;this.responseText=JSON.stringify(profile);
      Promise.resolve().then(()=>this.onload());}
  },
  requestAnimationFrame:cb=>rafs.push(cb),devicePixelRatio:1,
  prismPerformance:{count(){},span(){},connect(){}},
  document:{getElementById:id=>id==='status'?status:{},createElement:()=>({getContext:()=>({
    drawImage(){canvasReads++;},getImageData:()=>({data:[]})})})},
  Image:class {constructor(){this.width=this.height=1;images.push(this);}set src(value){}},
  qt:{webChannelTransport:{}},QWebChannel:class {constructor(transport,cb){cb({objects:{materialRender:bridge}});}},
  BABYLON:{MeshBuilder:{CreateSphere:name=>{meshesCreated++;return build(name);},CreateGround:build,
    CreateBox:build,CreateCylinder:build},PBRMaterial:class {constructor(){return pbr();}},
    Camera:{PERSPECTIVE_CAMERA:0},
    ArcRotateCamera:class {constructor(){this.dispose=()=>{};}},Vector3:{Zero:vector,Up:vector},
    SceneLoader:{LoadAssetContainerAsync:()=>assetReady.promise},
    Tools:{CreateScreenshotUsingRenderTargetAsync:()=>screenshot.promise}}};
sandbox.window=sandbox;vm.createContext(sandbox);vm.runInContext(source,sandbox);
sandbox.fakeEngine={renderWidth:256,runRenderLoop(){},stopRenderLoop(){},resize(){},webGLVersion:2};
sandbox.fakeScene={render(){submitted++;},whenReadyAsync:()=>sceneReady.promise,
  executeWhenReady:cb=>callbacks.push(cb)};
sandbox.fakeMesh=mesh();sandbox.fakeMaterial=pbr();
vm.runInContext(`engine=fakeEngine;scene=fakeScene;sphere=fakeMesh;material=fakeMaterial;
  camera={target:null};applySettings=()=>{};texture=async()=>({dispose(){}});
  current={token:'old',requestId:'old-id',signature:'old-sig',recordId:'old',capture:false,
    maps:{},properties:{controls:{}}};generation=1;`,sandbox);
const flush=async()=>{for(let i=0;i<16;i++)await Promise.resolve();};
function cancel(){
  if(invalidate==='release')sandbox.prismRender.release();
  else vm.runInContext(`++generation;current={token:'new',requestId:'new-id',signature:'new-sig',
    maps:{},properties:{controls:{}}};`,sandbox);
  sandbox.prismRender.diagnostics={owner:'new-or-released'};
  status.textContent='new-status';
}
const heightData=(identity='old-id')=>({token:identity,requestId:identity,signature:identity+'-sig',
  recordId:'same',shape:'plane',capture:false,maps:{displacement:'height.png'},properties:{controls:{}}});
async function finishFrames(){
  await flush();assert.strictEqual(callbacks.length,1,'Current load must compile the latest shape');
  callbacks.shift()();await flush();assert.strictEqual(rafs.length,1);
  rafs.shift()();rafs.shift()();await flush();
}
(async()=>{
  let work;
  if(stage.startsWith('shape_')){
    if(stage==='shape_init'){
      const initialization=deferred();sandbox.initialization=initialization;
      vm.runInContext('material=null;sphere=null;init=()=>initialization.promise;',sandbox);
      work=sandbox.prismRender.load(heightData());
      await sandbox.prismRender.shape('sphere');await sandbox.prismRender.shape('cylinder');
      initialization.resolve();await flush();
      assert.strictEqual(vm.runInContext('previewShape',sandbox),'cylinder','Init must preserve the newer user shape');
      assert.strictEqual(builtMeshes.at(-1).name,'material-cylinder');
      images[0].onload();await finishFrames();await work;
      assert.deepStrictEqual(events,[['frame_submitted','old-id','old-id-sig'],['updated','old-id','old-id-sig']]);
      process.stdout.write('ok');return;
    }
    work=sandbox.prismRender.load(heightData());await flush();assert.strictEqual(images.length,1);
    if(stage==='shape_compile'||stage==='shape_raf'){
      images[0].onload();await flush();assert.strictEqual(callbacks.length,1);
      if(stage==='shape_raf'){
        callbacks.shift()();await flush();rafs.shift()();
      }
      const shapeWork=sandbox.prismRender.shape('sphere');await flush();
      if(stage==='shape_compile')callbacks.shift()();else rafs.shift()();
      await flush();assert.deepStrictEqual(events,[],'Do not update while replacement displacement is pending');
      images[1].onload();await shapeWork;await finishFrames();await work;
      assert.strictEqual(displaced,2);
    }else{
      const shapes=[sandbox.prismRender.shape('sphere')];await flush();
      if(stage!=='shape_once'){
        shapes.push(sandbox.prismRender.shape('box'));shapes.push(sandbox.prismRender.shape('cylinder'));await flush();
      }
      const latest=images.length-1;
      assert.deepStrictEqual(events,[]);
      if(stage==='shape_release'){
        sandbox.prismRender.release();
        for(const image of images)image.onload();await Promise.all(shapes);await work;await flush();
        assert.deepStrictEqual(events,[]);assert.strictEqual(submitted,0);assert.strictEqual(displaced,0);
        assert.strictEqual(canvasReads,0);process.stdout.write('ok');return;
      }
      if(stage==='shape_new_load'||stage==='shape_fast_reuse'){
        const next=heightData('new-id');
        if(stage==='shape_new_load')next.maps={};
        work=Promise.all([work,sandbox.prismRender.load(next)]);await flush();
        if(stage==='shape_fast_reuse'){
          assert.strictEqual(images.length,latest+2,'A new ticket must not reuse an unfinished displaced mesh');
          images.at(-1).onload();
        }
        await finishFrames();await work;
        for(let i=0;i<=latest;i++)images[i].onload();await Promise.all(shapes);await flush();
        assert.deepStrictEqual(events,[['frame_submitted','new-id','new-id-sig'],['updated','new-id','new-id-sig']]);
        assert.strictEqual(displaced,stage==='shape_fast_reuse'?1:0);
        assert.strictEqual(canvasReads,displaced);process.stdout.write('ok');return;
      }
      if(stage==='shape_error'){
        images[latest].onerror();await flush();
        for(let i=0;i<latest;i++)images[i].onerror();await Promise.all(shapes);await work;
        assert.strictEqual(events.length,1);assert.strictEqual(events[0][0],'error');
        assert.strictEqual(events[0][1],'old-id');assert.strictEqual(submitted,0);
        assert.strictEqual(displaced,0);process.stdout.write('ok');return;
      }
      // Resolve only the newest Image: the superseded original load must not
      // strand completion, even if its Image never calls onload/onerror.
      images[latest].onload();await flush();
      assert.strictEqual(submitted,1,'The replacement shape draws once before load settlement');
      if(stage==='shape_once'){images[0].onload();await flush();}
      await finishFrames();await work;
      for(let i=latest-1;i>=0;i--)images[i].onload();await Promise.all(shapes);await flush();
      assert.strictEqual(displaced,1);assert.strictEqual(canvasReads,1);
      assert.strictEqual(builtMeshes.filter(mesh=>mesh.displaced).length,1);
      assert.strictEqual(builtMeshes.at(-1).displaced,true);
    }
    assert.deepStrictEqual(events,[['frame_submitted','old-id','old-id-sig'],['updated','old-id','old-id-sig']]);
    assert.strictEqual(sandbox.prismRender.diagnostics.shape,stage==='shape_chain'?'cylinder':'sphere');
    assert.strictEqual(callbacks.length,0);assert.strictEqual(rafs.length,0);
    process.stdout.write('ok');return;
  }else if(stage==='model_ready'||stage==='model_asset'){
    sandbox.modelData={model:'model.glb',token:'old',requestId:'old-id',signature:'old-sig'};
    work=sandbox.prismRender.load(sandbox.modelData);await flush();
    if(stage==='model_ready'){assetReady.resolve(container);await flush();}
    cancel();
    if(stage==='model_asset')assetReady.resolve(container);
    sceneReady.resolve();
  }else if(stage==='displacement'){
    sandbox.materialData={token:'old',requestId:'old-id',signature:'old-sig',capture:false,
      maps:{displacement:'height.png'},properties:{controls:{}}};
    work=sandbox.prismRender.load(sandbox.materialData);await flush();
    assert.strictEqual(images.length,1);cancel();images[0].onload();
  }else if(stage==='finish_compile'||stage==='finish_raf'){
    work=vm.runInContext('finish(current,generation,true)',sandbox);await flush();
    assert.strictEqual(callbacks.length,1);
    if(stage==='finish_raf'){
      callbacks.shift()();await flush();assert.strictEqual(rafs.length,1);
      rafs.shift()();assert.strictEqual(rafs.length,1);
    }
    cancel();
    if(stage==='finish_compile')callbacks.shift()();
    else rafs.shift()();
  }else if(stage==='capture'){
    work=sandbox.prismRender.capture('old','old-id','old-sig');await flush();
    assert.strictEqual(meshesCreated,1);cancel();screenshot.resolve('data:image/png;base64,old');
  }else if(stage==='capture_identity'){
    await sandbox.prismRender.capture('old','wrong-id','old-sig');
    await sandbox.prismRender.capture('old','old-id','wrong-sig');
    assert.strictEqual(meshesCreated,0);process.stdout.write('ok');return;
  }else if(stage==='fast_reuse'||stage==='full_prepare'){
    if(stage==='fast_reuse')vm.runInContext("loadedMapKey='{}'",sandbox);
    work=sandbox.prismRender.load({token:'same-token',requestId:'fresh-id',signature:'fresh-sig',
      recordId:'same',capture:false,maps:{},properties:{controls:{}}});
    await flush();
    if(stage==='full_prepare'){
      assert.strictEqual(callbacks.length,1);callbacks.shift()();await flush();
      assert.strictEqual(rafs.length,1);rafs.shift()();rafs.shift()();
    }
    await work;
    assert.strictEqual(callbacks.length,0);assert.strictEqual(images.length,0);
    assert.strictEqual(meshesCreated,0);assert.strictEqual(submitted,1);
    assert.deepStrictEqual(events,[['frame_submitted','fresh-id','fresh-sig'],['updated','fresh-id','fresh-sig']]);
    process.stdout.write('ok');return;
  }else throw Error('Unknown stage');
  await work;await flush();
  assert.deepStrictEqual(events,[],'No stale bridge publication');
  assert.strictEqual(submitted,0,'No stale scene submission');
  assert.strictEqual(displaced,0,'No stale mesh displacement');
  assert.strictEqual(canvasReads,0,'No stale displacement canvas read');
  assert.strictEqual(status.textContent,'new-status');
  assert.strictEqual(sandbox.prismRender.diagnostics.owner,'new-or-released');
  if(stage==='model_asset'||(stage==='model_ready'&&invalidate==='release'))assert.strictEqual(containersDisposed,1);
  process.stdout.write('ok');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""


@pytest.mark.parametrize('stage', ['model_asset', 'model_ready', 'displacement',
                                  'finish_compile', 'finish_raf', 'capture'])
@pytest.mark.parametrize('invalidate', ['release', 'next_generation'])
def test_render_js_async_continuations_reject_late_work(stage, invalidate):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required for browser-script lifecycle doubles')
    source = (Path(__file__).resolve().parents[1] / 'prism/assets/materials/render.js').read_text(encoding='utf-8')
    result = subprocess.run([node, '-e', _JS_LIFECYCLE_HARNESS],
                            input=json.dumps(dict(source=source, stage=stage, invalidate=invalidate, profile=_JS_PREVIEW_PROFILE)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'ok'


@pytest.mark.parametrize('stage', ['capture_identity', 'fast_reuse', 'full_prepare'])
def test_render_js_capture_identity_and_parameter_fast_path(stage):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required for browser-script lifecycle doubles')
    source = (Path(__file__).resolve().parents[1] / 'prism/assets/materials/render.js').read_text(encoding='utf-8')
    result = subprocess.run([node, '-e', _JS_LIFECYCLE_HARNESS],
                            input=json.dumps(dict(source=source, stage=stage, invalidate='release', profile=_JS_PREVIEW_PROFILE)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'ok'


@pytest.mark.parametrize('stage', ['shape_once', 'shape_chain', 'shape_compile', 'shape_raf',
                                  'shape_init', 'shape_release', 'shape_new_load',
                                  'shape_fast_reuse', 'shape_error'])
def test_render_js_load_settles_only_latest_stable_shape(stage):
    node = shutil.which('node')
    if node is None:
        pytest.skip('Node.js required for browser-script lifecycle doubles')
    source = (Path(__file__).resolve().parents[1] / 'prism/assets/materials/render.js').read_text(encoding='utf-8')
    result = subprocess.run([node, '-e', _JS_LIFECYCLE_HARNESS],
                            input=json.dumps(dict(source=source, stage=stage, invalidate='release', profile=_JS_PREVIEW_PROFILE)),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == 'ok'
