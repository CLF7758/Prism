"""Job-control tests: coalescing, cancellation, timeouts, crashes, publishing.

Most tests drive a scripted helper that speaks the real protocol, so a failure
points at the controller rather than at the GPU. The last test runs the real
F3D helper and is skipped when the runtime is not installed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

from prism.model_library import native_protocol as wire
from prism.model_library.contracts import (ModelJobKind, ModelJobRequest,
                                           ModelRequestIdentity)
from prism.model_library.native_jobs import (EVENT_COMPLETED, EVENT_FAILED,
                                             EVENT_INSTALLED, EVENT_OUTPUT_WRITTEN,
                                             EVENT_STALE, EVENT_WORKER_LOST,
                                             NativeModelJobController, RuntimeConfig)

ROOT = Path(__file__).resolve().parents[2]

#: A helper that follows the protocol and misbehaves exactly as told.
FAKE_WORKER = r'''
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, os.environ.get('PRISM_FAKE_PATH', ''))
from prism.model_library import native_protocol as wire

def main():
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    port = int(args['--ipc-port'])
    token = args['--ipc-token']
    channel = wire.Channel.connect('127.0.0.1', port, timeout=10.0)
    handshake = wire.Handshake(
        worker=wire.WORKER_NAME_RENDER, backend_version='fake-1.0', vtk_version='x',
        python_version='3', pid=os.getpid(),
        formats={'.fbx': 'FBX', '.obj': 'OBJ', '.glb': 'GLB', '.gltf': 'GLTF'},
        readers=('OBJ',), wireframe=False, material_edges=True, appearance=True,
        statistics='fake')
    channel.send(wire.message(wire.HELLO, token=token, handshake=handshake.to_payload()))
    while True:
        envelope = channel.recv(timeout=0.5)
        if envelope is None:
            if channel.closed:
                return 0
            continue
        kind = wire.check_message(envelope)
        if kind == wire.SHUTDOWN:
            channel.send(wire.message(wire.BYE))
            return 0
        if kind == wire.CANCEL:
            continue
        if kind != wire.REQUEST:
            continue
        identity = envelope.get('identity') or {}
        payload = envelope.get('payload') or {}
        behaviour = str(payload.get('behaviour') or 'ok')
        job_id = str(envelope.get('job_id'))
        if behaviour == 'crash':
            os._exit(7)
        if behaviour == 'hang':
            while True:
                time.sleep(0.05)
        if behaviour == 'slow':
            time.sleep(float(payload.get('delay') or 0.3))
        if behaviour == 'error':
            channel.send(wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_FAILED, identity=identity,
                                      error_code=wire.ERR_BAD_FILE,
                                      error_message='不行'))
            continue
        if behaviour == 'other-identity':
            identity = dict(identity)
            identity['appearance_revision'] = 999
        path = str(payload.get('output') or '')
        if not path:
            channel.send(wire.message(wire.RESULT, job_id=job_id,
                                      state=wire.STATE_FAILED, identity=identity,
                                      error_code=wire.ERR_OUTPUT_INVALID,
                                      error_message='no output'))
            continue
        os.makedirs(os.path.dirname(path), exist_ok=True)
        staged = path + '.part'
        with open(staged, 'wb') as handle:
            handle.write(b'cover-bytes')
        os.replace(staged, path)
        data = open(path, 'rb').read()
        digest = hashlib.sha256(data).hexdigest()
        if behaviour == 'bad-hash':
            digest = '0' * 64
        channel.send(wire.message(
            wire.RESULT, job_id=job_id, state=wire.STATE_COMPLETED, identity=identity,
            outputs={'path': path, 'sha256': digest, 'bytes': len(data),
                     'width': 4, 'height': 4},
            statistics={'basis': 'fake'}))
        if behaviour == 'goodbye':
            channel.send(wire.message(wire.BYE))
            return 0

sys.exit(main())
'''


def _write_fake_worker(directory: Path) -> Path:
    path = directory / 'fake_worker.py'
    path.write_text(FAKE_WORKER, encoding='utf-8')
    return path


def _config(tmp_path: Path, script: Path, **overrides) -> RuntimeConfig:
    options = dict(cache_root=tmp_path / 'cache', worker_target=str(script),
                   worker_worker_args=(), startup_timeout_s=25.0,
                   request_timeout_s=6.0, cancel_grace_s=0.4, shutdown_timeout_s=3.0,
                   idle_exit_s=0.0, restart_limit=2, restart_window_s=60.0,
                   env={'PRISM_FAKE_PATH': str(ROOT)})
    options.update(overrides)
    return RuntimeConfig(**options)


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event, **fields):
        self.events.append((event, fields))

    def names(self):
        return [name for name, _ in self.events]

    def payloads(self, name):
        return [fields for event, fields in self.events if event == name]


def _identity(**overrides):
    base = dict(project_session='ps-1', open_epoch=1, model_id='model-1',
                request_id='req-1', source_revision=1, appearance_revision=1,
                camera_revision=0)
    base.update(overrides)
    return ModelRequestIdentity(**base)


def _request(identity=None, *, kind=ModelJobKind.THUMBNAIL, output=None,
             behaviour='ok', delay=None, manifest='model.obj'):
    payload = {'behaviour': behaviour, 'output': str(output or '')}
    if delay is not None:
        payload['delay'] = delay
    request = ModelJobRequest(identity=identity or _identity(), kind=kind,
                              manifest_ref=str(manifest), output_ref=None)
    return request, payload


@pytest.fixture()
def helper(tmp_path):
    script = _write_fake_worker(tmp_path)
    controllers = []

    def factory(**overrides):
        config = _config(tmp_path, script, **overrides)
        recorder = _Recorder()
        controller = NativeModelJobController(config, publish_event=recorder)
        controllers.append(controller)
        return controller, recorder

    yield factory
    for controller in controllers:
        controller.shutdown()


# ── happy path ────────────────────────────────────────────────────────────
def test_submit_publishes_verified_output(helper, tmp_path):
    controller, recorder = helper()
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png')
    job = controller.submit(request, payload=payload, output_ref=str(destination))

    assert controller.wait(job, timeout=30.0) is not None
    assert controller.state(job) == wire.STATE_COMPLETED
    assert destination.is_file()
    assert destination.read_bytes() == b'cover-bytes'
    assert not (tmp_path / 'scratch' / 'cover.png').exists(), '临时文件应被移走'
    assert controller.result(job)['output_path'] == str(destination)

    names = recorder.names()
    assert names.index(EVENT_OUTPUT_WRITTEN) < names.index(EVENT_INSTALLED)
    assert EVENT_COMPLETED in names
    completed = recorder.payloads(EVENT_COMPLETED)[0]
    assert completed['state'] == wire.STATE_COMPLETED


def test_capabilities_are_unavailable_before_handshake(helper):
    controller, _ = helper()
    payload = controller.capabilities
    assert payload['available'] is False
    assert payload['preview'] is False


def test_probe_returns_the_measured_handshake(helper):
    controller, _ = helper()
    report = controller.probe(timeout=30.0)
    assert report['available'] is True
    handshake = report['handshake']
    assert handshake.backend_version == 'fake-1.0'
    assert handshake.formats['.obj'] == 'OBJ'
    assert controller.capabilities['preview'] is True
    assert controller.capabilities['wireframe'] is False


# ── publish guards ────────────────────────────────────────────────────────
def test_bad_hash_is_never_published(helper, tmp_path):
    controller, recorder = helper()
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png',
                                behaviour='bad-hash')
    job = controller.submit(request, payload=payload, output_ref=str(destination))
    controller.wait(job, timeout=30.0)
    assert controller.state(job) == 'failed'
    assert not destination.exists()
    failures = recorder.payloads(EVENT_FAILED)
    assert any(item['error_code'] == wire.ERR_OUTPUT_INVALID for item in failures)


def test_result_with_other_identity_is_rejected(helper, tmp_path):
    controller, recorder = helper()
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png',
                                behaviour='other-identity')
    job = controller.submit(request, payload=payload, output_ref=str(destination))
    controller.wait(job, timeout=30.0)
    assert controller.state(job) == 'failed'
    assert not destination.exists()
    failures = recorder.payloads(EVENT_FAILED)
    assert any(item['error_code'] == wire.ERR_BAD_PROTOCOL for item in failures)


def test_newer_revision_supersedes_the_pending_one(helper, tmp_path):
    controller, recorder = helper()
    first_destination = tmp_path / 'published' / 'first.png'
    second_destination = tmp_path / 'published' / 'second.png'
    slow, slow_payload = _request(identity=_identity(request_id='req-1',
                                                     appearance_revision=1),
                                  output=tmp_path / 'scratch' / 'first.png',
                                  behaviour='slow', delay=0.4)
    first = controller.submit(slow, payload=slow_payload,
                              output_ref=str(first_destination))
    new, new_payload = _request(identity=_identity(request_id='req-2',
                                                   appearance_revision=2),
                                output=tmp_path / 'scratch' / 'second.png')
    second = controller.submit(new, payload=new_payload,
                               output_ref=str(second_destination))

    controller.wait(second, timeout=30.0)
    assert controller.state(second) == wire.STATE_COMPLETED
    assert second_destination.is_file()
    assert not first_destination.exists(), '旧修订的结果不能落盘'
    assert controller.state(first) in ('stale', 'cancelled')
    assert recorder.names().count(EVENT_STALE) >= 1


def test_cancel_of_a_queued_job_never_publishes(helper, tmp_path):
    controller, recorder = helper()
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png',
                                behaviour='slow', delay=0.5)
    job = controller.submit(request, payload=payload, output_ref=str(destination))
    controller.cancel(job)
    controller.wait(job, timeout=10.0)
    assert controller.state(job) == 'cancelled'
    assert not destination.exists()


def test_cancel_of_a_running_job_ends_the_helper(helper, tmp_path):
    controller, recorder = helper()
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png',
                                behaviour='hang')
    job = controller.submit(request, payload=payload, output_ref=str(destination))
    time.sleep(0.5)
    first_pid = controller.probe(timeout=10.0)['worker_pid']
    controller.cancel(job)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline and controller.probe(timeout=10.0)['worker_pid'] == first_pid:
        time.sleep(0.1)
    assert controller.probe(timeout=10.0)['worker_pid'] != first_pid
    assert not destination.exists()
    assert controller.state(job) == 'cancelled'


def test_timeout_fails_the_job_and_restarts_the_helper(helper, tmp_path):
    controller, recorder = helper(request_timeout_s=0.6)
    destination = tmp_path / 'published' / 'cover.png'
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png',
                                behaviour='hang')
    job = controller.submit(request, payload=payload, output_ref=str(destination))
    controller.wait(job, timeout=20.0)
    assert controller.state(job) == 'failed'
    failures = recorder.payloads(EVENT_FAILED)
    assert any(item['error_code'] == wire.ERR_TIMEOUT for item in failures)
    assert not destination.exists()

    # the helper must come back for the next job
    good, good_payload = _request(identity=_identity(request_id='req-2'),
                                  output=tmp_path / 'scratch' / 'good.png')
    second = controller.submit(good, payload=good_payload,
                               output_ref=str(tmp_path / 'published' / 'good.png'))
    controller.wait(second, timeout=30.0)
    assert controller.state(second) == wire.STATE_COMPLETED


# ── crash isolation ───────────────────────────────────────────────────────
def test_normal_goodbye_does_not_consume_restart_budget(helper, tmp_path):
    controller, recorder = helper()
    request, payload = _request(behaviour='goodbye', output=tmp_path/'scratch'/'closed.png')
    job = controller.submit(request, payload=payload, output_ref=str(tmp_path/'published'/'closed.png'))
    assert controller.wait(job,timeout=30)['state']==wire.STATE_COMPLETED
    deadline=time.monotonic()+10
    while time.monotonic()<deadline and sum(name=='native_worker_started' for name,_ in recorder.events)<2:
        time.sleep(.02)
    assert not recorder.payloads(EVENT_WORKER_LOST)
    assert not controller._restarts
    next_request, next_payload = _request(identity=_identity(request_id='after-close'),
        output=tmp_path/'scratch'/'next.png')
    next_job=controller.submit(next_request,payload=next_payload,output_ref=str(tmp_path/'published'/'next.png'))
    assert controller.wait(next_job,timeout=30)['state']==wire.STATE_COMPLETED


def test_crash_reports_exit_code_and_log_tail(helper, tmp_path):
    controller, recorder = helper()
    request, payload = _request(behaviour='crash')
    job = controller.submit(request, payload=payload,
                            output_ref=str(tmp_path / 'published' / 'x.png'))
    controller.wait(job, timeout=30.0)
    assert controller.state(job) == 'failed'
    # The failed job is reported first, the reaped helper right after it.
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and not recorder.payloads(EVENT_WORKER_LOST):
        time.sleep(0.05)
    losses = recorder.payloads(EVENT_WORKER_LOST)
    assert losses, '原生进程的退出必须被记录，不能只有任务失败'
    assert losses[0].get('exit_code') == 7
    assert isinstance(losses[0].get('log_tail'), list)


def test_restart_budget_stops_an_infinite_crash_loop(helper, tmp_path):
    controller, recorder = helper(restart_limit=1, restart_window_s=600.0)
    for index in range(3):
        request, payload = _request(identity=_identity(request_id='req-%d' % index),
                                    behaviour='crash')
        controller.submit(request, payload=payload,
                          output_ref=str(tmp_path / 'published' / ('%d.png' % index)))
        time.sleep(0.4)
    deadline = time.monotonic() + 25.0
    while time.monotonic() < deadline:
        report = controller.probe(timeout=5.0)
        if not report['available']:
            break
        time.sleep(0.2)
    assert controller.probe(timeout=5.0)['available'] is False
    # The budget is spent: a new submit fails fast instead of waiting forever.
    request, payload = _request(behaviour='ok')
    job = controller.submit(request, payload=payload,
                            output_ref=str(tmp_path / 'published' / 'late.png'))
    controller.wait(job, timeout=10.0)
    assert controller.state(job) in ('failed', 'queued')
    if controller.state(job) == 'failed':
        assert any(item['error_code'] == wire.ERR_WORKER_UNAVAILABLE
                   for item in recorder.payloads(EVENT_FAILED))


def test_worker_error_is_reported_with_its_code(helper, tmp_path):
    controller, recorder = helper()
    request, payload = _request(behaviour='error')
    job = controller.submit(request, payload=payload,
                            output_ref=str(tmp_path / 'published' / 'x.png'))
    controller.wait(job, timeout=30.0)
    assert controller.state(job) == 'failed'
    assert controller.result(job)['error_code'] == wire.ERR_BAD_FILE
    assert controller.result(job)['error_message']
    failures = [item for item in recorder.payloads(EVENT_FAILED)
                if item['job_id'] == job]
    assert failures and failures[0]['error_code'] == wire.ERR_BAD_FILE


# ── project close and shutdown ────────────────────────────────────────────
def test_close_project_removes_only_its_own_outputs(helper, tmp_path):
    controller, _ = helper()
    kept = tmp_path / 'published' / 'kept.png'
    dropped = tmp_path / 'published' / 'dropped.png'
    request_a, payload_a = _request(identity=_identity(project_session='session-a'),
                                    output=tmp_path / 'scratch' / 'a.png')
    request_b, payload_b = _request(identity=_identity(project_session='session-b',
                                                       request_id='req-b'),
                                    output=tmp_path / 'scratch' / 'b.png')
    job_a = controller.submit(request_a, payload=payload_a, output_ref=str(dropped))
    job_b = controller.submit(request_b, payload=payload_b, output_ref=str(kept))
    controller.wait(job_a, timeout=30.0)
    controller.wait(job_b, timeout=30.0)
    assert dropped.is_file() and kept.is_file()

    controller.close_project('session-a')
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and dropped.exists():
        time.sleep(0.05)
    assert not dropped.exists()
    assert kept.is_file(), '其他工程的输出不能被清理'


def test_shutdown_is_idempotent_and_stops_the_helper(helper, tmp_path):
    controller, _ = helper()
    request, payload = _request(output=tmp_path / 'scratch' / 'cover.png')
    job = controller.submit(request, payload=payload,
                            output_ref=str(tmp_path / 'published' / 'cover.png'))
    controller.wait(job, timeout=30.0)
    pid = controller.probe(timeout=10.0)['worker_pid']
    controller.shutdown()
    controller.shutdown()
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline and _alive(pid):
        time.sleep(0.05)
    assert not _alive(pid)


def _alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    except Exception:  # noqa: BLE001 - platform specific
        return False
    return True


# ── the real helper ───────────────────────────────────────────────────────
def _f3d_available() -> bool:
    """Ask a child process, never this one.

    The pytest process already has Qt loaded through other suites, and loading
    pyf3d after Qt fails on this machine, so the check must not import f3d here.
    Prism's main process has the same rule for the same reason.
    """
    import subprocess

    try:
        probe = subprocess.run([sys.executable, '-c', 'import f3d; print(f3d.__version__)'],
                              capture_output=True, text=True, timeout=120,
                              encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and bool(probe.stdout.strip())


requires_native = pytest.mark.skipif(not _f3d_available(),
                                    reason='这台机器没有可用的 F3D 原生运行时')


def _real_config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(cache_root=tmp_path / 'cache', startup_timeout_s=90.0,
                         request_timeout_s=180.0, idle_exit_s=0.0,
                         worker_worker_args=('--mode', 'render'))


@requires_native
def test_real_helper_renders_a_cover_and_reuses_the_model(tmp_path):
    sample = ROOT / 'third_party' / 'model-library' / 'samples' / 'obj_multimaterial.obj'
    if not sample.is_file():
        pytest.skip('样本文件不存在')
    recorder = _Recorder()
    controller = NativeModelJobController(_real_config(tmp_path), publish_event=recorder)
    try:
        destination = tmp_path / 'published' / 'cover.png'
        request, payload = _request(output=tmp_path / 'scratch' / 'cover.png')
        payload['manifest_ref'] = str(sample)
        payload['width'] = 256
        payload['height'] = 256
        job = controller.submit(request, payload=payload, output_ref=str(destination))
        result = controller.wait(job, timeout=180.0)
        assert result is not None, recorder.events[-5:]
        assert controller.state(job) == wire.STATE_COMPLETED
        assert destination.is_file() and destination.stat().st_size > 500
        assert result['statistics']['basis'] == 'f3d-loaded'
        assert result['statistics']['points'] == 24
        assert result['scene']['actors'] == 7

        second, second_payload = _request(identity=_identity(request_id='req-2',
                                                             appearance_revision=2),
                                          output=tmp_path / 'scratch' / 'cover2.png')
        second_payload.update({'manifest_ref': str(sample), 'width': 256,
                               'height': 256})
        second_job = controller.submit(second, payload=second_payload,
                                       output_ref=str(tmp_path / 'published' / 'c2.png'))
        second_result = controller.wait(second_job, timeout=120.0)
        assert second_result is not None
        assert second_result['load']['reused'] is True, '同一模型不应重新读取'
    finally:
        controller.shutdown()
