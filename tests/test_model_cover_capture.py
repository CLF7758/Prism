"""Cover capture must not allocate a second model or screenshot render target."""
from concurrent.futures import Future
from dataclasses import replace
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import MaterialTarget


class ImmediatePool:
    def submit(self, function, *args):
        result = Future()
        try:
            result.set_result(function(*args))
        except Exception as error:
            result.set_exception(error)
        return result


@pytest.mark.parametrize('stale', [False, True])
def test_explicit_cover_reuses_visible_worker_and_checks_revision(tmp_path, stale):
    target = MaterialTarget('session', 1, 'model', 1, 1, 2)
    camera = {'renderer': 'babylon', 'alpha': 1.2}
    output = tmp_path / 'capture.png'
    from PIL import Image
    Image.new('RGB', (16, 16), (100, 80, 60)).save(output)
    requests, installed, published = [], [], []
    class Controller:
        def submit(self, request, *, kind, payload):
            requests.append(payload)
            assert payload['action'] == 'cover', 'Explicit cover must not reload geometry/textures'
            return 'capture'
        def wait(self, job, timeout):
            return {'state': 'completed', 'output_path': str(output)}
        def forget(self, job):
            pass
    service = object.__new__(ProjectMaterialService)
    service.controller = Controller()
    service.cover_controller = None  # Explicit capture must not start the background helper.
    service._closed = False
    service._covers, service._failed_covers = {}, {}
    service._cover_pending, service._native_traces = set(), {}
    service._cover_pool = ImmediatePool()
    service._root = tmp_path
    service._post = lambda callback: callback()
    service._remember_native_trace = lambda request: None
    service._load = lambda current: {'state': SimpleNamespace(target=target), 'path': 'model.fbx'}
    service.target_for = lambda record: replace(target, state_revision=3) if stale and requests else target
    repo = SimpleNamespace(cover_cache_root=tmp_path / 'covers',
        get_payload=lambda model: {'record_revision': 1},
        update_cache=lambda *args, **kwargs: installed.append(kwargs) or {})
    service.owner = SimpleNamespace(repository=repo, get=lambda model: object(),
        _can_write=lambda: True, _on_commit=lambda before, after: None,
        _publish=published.append)
    service.render_cover('model', camera)
    assert service._covers['model'].exception() is None
    assert not service._failed_covers
    assert len(requests) == 1
    assert requests[0]['expected_scene']['model_id'] == 'model'
    assert requests[0]['expected_scene']['appearance_revision'] == 2
    assert len(installed) == (0 if stale else 1)
    if not stale:
        assert installed[0]['thumbnail']['camera'] == camera
        assert published[-1]['state'] == 'completed'


@pytest.mark.parametrize('release,fail', [(False, False), (True, False), (False, True)])
def test_capture_reads_existing_framebuffer_without_extra_gpu_target(release, fail):
    source = Path('prism/assets/materials/model-scene.js').read_text(encoding='utf-8')
    capture = source[source.index('  async function capture(data){'):source.index('  async function thumbnail(data){')]
    script = r'''
const vm=require('vm');const calls=[];
const output={getContext:()=>({fillRect(){},drawImage(...args){calls.push(['draw',...args.slice(1)]);}}),toDataURL:()=> 'data:image/png;base64,test'};
const context={selection:['selected'],displayMode:'roughness',cameraState:()=>({alpha:1.2}),
  clearSelection:()=>calls.push('clear'),mode:value=>calls.push(['mode',value]),
  restoreCamera:value=>calls.push(['camera',value]),highlight:value=>calls.push(['highlight',value]),
  release:()=>calls.push('release'),compileVisible:async()=>{},canvas:{width:1600,height:900},
  scene:{whenReadyAsync:async()=>{},render(){calls.push('render');if(FAIL)throw Error('readback failed');}},
  engine:{isDisposed:false,_gl:{isContextLost:()=>false}},
  document:{createElement:()=>output}};
vm.createContext(context);vm.runInContext(SOURCE+';this.capture=capture',context);
context.capture({release_scene:RELEASE,width:99999,height:512}).then(result=>{
 console.log(JSON.stringify({result,calls,width:output.width,height:output.height}));
},error=>console.log(JSON.stringify({error:error.message,calls})));
'''.replace('SOURCE', json.dumps(capture)).replace('RELEASE', json.dumps(release)).replace('FAIL', json.dumps(fail))
    result = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert 'render' in actual['calls']
    if fail:
        assert actual['error'] == 'readback failed'
    else:
        assert actual['width'] == 1024 and actual['height'] == 512
        assert actual['result']['camera'] == {'alpha': 1.2}
        assert actual['result']['image'].startswith('data:image/png')
    if release:
        assert actual['calls'][-1] == 'release'
    else:
        assert actual['calls'][-2:] == [['mode', 'roughness'], ['highlight', ['selected']]]
