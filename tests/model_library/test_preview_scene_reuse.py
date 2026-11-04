"""Opt-in real hidden Windows/QtWebEngine/IPC verification, no desktop input."""
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest


@pytest.mark.skipif(os.environ.get('PRISM_TEST_SHARED_NATIVE') != '1',
                    reason='Explicit hidden native GPU verification')
def test_reopen_installs_live_scene_and_release_invalidates(tmp_path):
    from prism.model_library.native_jobs import NativeModelJobController, RuntimeConfig
    from prism.model_library.native_protocol import WORKER_NAME_PREVIEW
    from prism.model_library.contracts import ModelRequestIdentity, ModelJobRequest, ModelJobKind
    from prism.model_library.multimaterial.browser_scene import original_artifact
    source = Path('E:/Prism-stress-100k/Game/LZPV/Assets/Envs/Slay/Assets/SetDec/WoodCarving/SM_WoodCarving.FBX')
    if not source.is_file():
        pytest.skip('User-authorized WoodCarving fixture is not installed')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    payload = json.dumps(original_artifact(source, tmp_path), sort_keys=True).encode()
    artifact = tmp_path/(hashlib.sha256(payload).hexdigest()+'.render.json')
    artifact.write_bytes(payload)
    controller = NativeModelJobController(RuntimeConfig(cache_root=tmp_path/'worker',
        worker_target='prism.components.material_preview', worker_worker_args=('--headless',),
        worker_name=WORKER_NAME_PREVIEW, shutdown_timeout_s=3,
        env={'QT_QPA_PLATFORM':'windows',
             'QTWEBENGINE_CHROMIUM_FLAGS':'--no-sandbox --disable-gpu-shader-disk-cache'}))
    def request(action, **data):
        identity = ModelRequestIdentity('scene-reuse', 1, 'wood', uuid.uuid4().hex, 1, 1, 0)
        job = controller.submit(ModelJobRequest(identity, ModelJobKind.PREVIEW, str(source)),
            kind='preview', payload=dict(action=action, **data))
        result = controller.wait(job, 90)
        assert result and result['state'] == 'completed', result
        controller.forget(job)
        return result
    try:
        first = request('load', artifact_ref=str(artifact))
        second = request('open', artifact_ref=str(artifact))
        # A Python-side loaded flag cannot certify the browser's live scene.
        assert not second.get('scene_reused')
        assert first['bindings'] == second['bindings']
        assert first['statistics'] == second['statistics']
        request('release')
        third = request('load', artifact_ref=str(artifact))
        assert not third.get('scene_reused')
        assert third['bindings'] == first['bindings']
        assert hashlib.sha256(source.read_bytes()).hexdigest() == digest
        print('scene reopen real worker: first_ms=%s reopened_ms=%s after_release_ms=%s' % (
            first.get('elapsed_ms'), second.get('elapsed_ms'), third.get('elapsed_ms')))
    finally:
        controller.shutdown()
