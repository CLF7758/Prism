"""Native F3D job control: process lifetime, coalescing, cancellation, publish.

The controller is the only owner of the native helper processes. It never
imports F3D, never renders and never touches a QWidget: the main process may
not execute native ``add``/``render`` at all.

What this module guarantees, and the tests check one by one:

* one offscreen helper serves every cover job; a crash is contained and
  restarted a bounded number of times, then reported as unavailable;
* a newer revision of the same model supersedes the older one: the older
  request loses its right to publish before it finishes;
* ``cancel`` removes publish eligibility first and only then asks the helper,
  so a stuck native call cannot keep a cancelled result alive;
* a result is verified (job id, identity, file size, file hash) and only then
  moved into place with one atomic rename;
* project close stops that session's jobs and releases its outputs; it never
  touches another project's files;
* every failure carries a code, the child's exit status and a bounded log tail
  so a native crash is diagnosable instead of mysterious.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import native_protocol as wire


def _allow_preview_foreground(pid, owner_window_id):
    """Pass foreground permission to our preview only while Prism is active."""
    if sys.platform != 'win32' or not pid or not owner_window_id:
        return
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        if user32.GetForegroundWindow() == owner_window_id:
            user32.AllowSetForegroundWindow.argtypes = [ctypes.c_ulong]
            user32.AllowSetForegroundWindow(int(pid))
    except (AttributeError, OSError):
        pass  # Window ownership and Qt activation still apply.

#: Events published on the existing trace pipeline (see model_library.events).
EVENT_QUEUED = 'queued'
EVENT_STARTED = 'started'
EVENT_NATIVE_LOADED = 'native_loaded'
EVENT_RENDERED = 'rendered'
EVENT_OUTPUT_WRITTEN = 'output_written'
EVENT_INSTALLED = 'installed'
EVENT_COMPLETED = 'completed'
EVENT_FAILED = 'failed'
EVENT_CANCELLED = 'cancelled'
EVENT_STALE = 'stale'
EVENT_NATIVE_LOG = 'native_log'
EVENT_WORKER_STARTED = 'native_worker_started'
EVENT_WORKER_LOST = 'native_worker_lost'


@dataclass(frozen=True)
class RuntimeConfig:
    """Everything the controller needs to launch and supervise a helper.

    ``python`` defaults to the running interpreter: the helper must import the
    same F3D build that was verified against the pinned Qt/Python ABI.
    """

    cache_root: Path
    python: str = ''
    #: Module or script handed to the interpreter, e.g. ``prism.components.model_worker``.
    worker_target: str = 'prism.components.model_worker'
    worker_worker_args: tuple[str, ...] = ('--mode', 'render')
    #: Which helper identity the handshake must claim. Empty means "infer it
    #: from ``--mode``"; set it when the target is the interactive preview host.
    worker_name: str = ''
    cwd: str = ''
    env: Mapping[str, str] = field(default_factory=dict)
    startup_timeout_s: float = 45.0
    request_timeout_s: float = 120.0
    cancel_grace_s: float = 3.0
    shutdown_timeout_s: float = 8.0
    idle_exit_s: float = 90.0
    #: A helper that never served a job is released after this many seconds.
    #: Zero keeps it until the controller is shut down.  It bounds the cost
    #: of starting a helper early: a preview that is never opened gives its
    #: memory back on its own.
    unused_exit_s: float = 0.0
    restart_limit: int = 2
    restart_window_s: float = 120.0
    log_tail_lines: int = 200
    max_pending_per_key: int = 1
    cover_width: int = 512
    cover_height: int = 512

    def resolved_python(self) -> str:
        return self.python or sys.executable

    def scratch(self) -> Path:
        path = Path(self.cache_root) / 'native'
        path.mkdir(parents=True, exist_ok=True)
        return path


@dataclass
class _Job:
    job_id: str
    kind: str
    key: tuple
    identity: Mapping[str, Any]
    request_payload: Mapping[str, Any]
    output_ref: Optional[str]
    priority: int
    created_at: float
    state: str = 'queued'
    publish_eligible: bool = True
    sent_mono: float = 0.0
    deadline_mono: float = 0.0
    worker_pid: int = 0
    result: Optional[dict] = None
    read_finished: bool = False


class NativeModelJobController:
    """Owns the offscreen helper, the job queue and the publish rules."""

    def __init__(self, runtime_config: RuntimeConfig,
                 publish_event: Optional[Callable[..., None]] = None):
        self._config = runtime_config
        self._publish_event = publish_event
        self._lock = threading.RLock()
        self._jobs: dict[str, _Job] = {}
        self._queued: list[str] = []
        #: ``(project_session, model_id, kind)`` -> job id that may still publish.
        self._latest: dict[tuple, str] = {}
        self._counter = 0
        self._handshake: Optional[wire.Handshake] = None
        self._handshake_error = ''
        self._process: Optional[subprocess.Popen] = None
        self._channel: Optional[wire.Channel] = None
        self._listener: Optional[socket.socket] = None
        self._worker_pid = 0
        self._worker_started_mono = 0.0
        self._restarts: deque = deque(maxlen=64)
        self._log_tail: deque = deque(maxlen=max(20, int(runtime_config.log_tail_lines)))
        self._stdout_path: Optional[Path] = None
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._closed = False
        self._current: Optional[str] = None
        self._last_used_mono = time.monotonic()
        #: True once a job was handed to the helper. A helper that was only
        #: started and never used may be released on its own.
        self._served = False
        #: True once the controller itself asked the helper to stop, so an idle
        #: exit is never mistaken for a crash.
        self._shutdown_requested = False
        self._graceful_goodbye = False
        self._exit_reason = ''
        #: True while a launch thread is between spawn and handshake.
        self._starting = False
        #: Published outputs per project session, so a project close removes
        #: exactly its own derived files and no other project's package.
        self._session_outputs: dict[str, list[Path]] = {}

    # ------------------------------------------------------------------
    # Public port
    # ------------------------------------------------------------------
    @property
    def capabilities(self) -> Mapping[str, Any]:
        return self._capability_payload()

    def submit(self, request: Any, *, kind: Optional[str] = None,
               payload: Optional[Mapping[str, Any]] = None,
               output_ref: Optional[str] = None, priority: int = 5) -> str:
        """Queue one native request and return its job id.

        ``kind``/``payload`` let the adapter stay thin: the contracts request
        already carries the identity and the manifest reference.
        """
        identity = wire.identity_payload(request.identity)
        kind = str(kind or getattr(request.kind, 'value', request.kind))
        manifest = _as_text(getattr(request, 'manifest_ref', ''))
        payload = dict(payload or {})
        payload.setdefault('manifest_ref', manifest)
        output_ref = output_ref or getattr(request, 'output_ref', None)
        key = (identity['project_session'], identity['model_id'], kind)

        with self._lock:
            if self._closed:
                raise wire.ProtocolError('控制器已关闭')
            job_id = self._next_job_id(identity)
            superseded = self._latest.get(key)
            job = _Job(job_id=job_id, kind=kind, key=key, identity=identity,
                       request_payload=payload, output_ref=_as_text(output_ref) or None,
                       priority=int(priority), created_at=time.monotonic())
            self._jobs[job_id] = job
            self._latest[key] = job_id
            self._queued.append(job_id)
            self._queued.sort(key=lambda jid: (self._jobs[jid].priority,
                                               self._jobs[jid].created_at))
            self._trim_key(key, keep=job_id)
        if superseded and superseded != job_id:
            self._mark_stale(superseded)
        self._emit(EVENT_QUEUED, job_id=job_id, kind=kind,
                   model_id=identity['model_id'], request_id=identity['request_id'],
                   source_revision=identity['source_revision'],
                   appearance_revision=identity['appearance_revision'],
                   camera_revision=identity['camera_revision'])
        self._ensure_started()
        self._wake()
        return job_id

    def cancel(self, job_id: str) -> None:
        """Give up on a job. The native call never decides this."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in ('queued', 'running'):
                return
            job.publish_eligible = False
            job.state = 'cancelled'
            if job_id in self._queued:
                self._queued.remove(job_id)
            running = self._current == job_id
            self._drop_latest(job)
        self._emit(EVENT_CANCELLED, job_id=job_id, error_code=wire.ERR_CANCELLED)
        if running:
            self._send_soft(wire.message(wire.CANCEL, job_id=job_id))
            threading.Thread(target=self._escalate_cancel, args=(job_id,),
                             name='model-cancel', daemon=True).start()

    def close_project(self, project_session: str) -> None:
        """Stop every job of one project session and forget its outputs."""
        session = str(project_session or '')
        with self._lock:
            victims = [job.job_id for job in self._jobs.values()
                       if job.identity.get('project_session') == session
                       and job.state in ('queued', 'running')]
        for job_id in victims:
            self.cancel(job_id)
        self._send_soft(wire.message(wire.CLOSE_PROJECT, project_session=session))
        with self._lock:
            for key in [k for k in self._latest if k[0] == session]:
                self._latest.pop(key, None)
        self._sweep_session_outputs(session)

    def shutdown(self) -> None:
        """Close the helper and join the threads. Safe to call twice."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            victims = [job.job_id for job in self._jobs.values()
                       if job.state in ('queued', 'running')]
        for job_id in victims:
            self.cancel(job_id)
        self._stop.set()
        self._shutdown_requested = True
        self._send_soft(wire.message(wire.SHUTDOWN))
        deadline = time.monotonic() + max(1.0, self._config.shutdown_timeout_s)
        process = self._process
        while process is not None and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        if process is not None and process.poll() is None:
            self._kill('shutdown')
        for thread in list(self._threads):
            thread.join(timeout=2.0)
        self._threads.clear()
        self._close_channel()

    # ------------------------------------------------------------------
    # Handshake / capabilities
    # ------------------------------------------------------------------
    def handshake(self, timeout: Optional[float] = None) -> wire.Handshake:
        """Start the helper if needed and return its measured handshake.

        A handshake from a helper that has since died is not reused: reporting
        "available" for a dead process is exactly the kind of fake success this
        subsystem must never produce.
        """
        deadline = time.monotonic() + (timeout or self._config.startup_timeout_s)
        self._ensure_started()
        while time.monotonic() < deadline:
            with self._lock:
                alive = self._process is not None and self._process.poll() is None
                if self._handshake is not None and alive:
                    return self._handshake
                if self._handshake_error:
                    raise wire.HandshakeError(self._handshake_error)
            if not alive and self._restart_budget_exhausted():
                raise wire.HandshakeError(
                    self._handshake_error or '原生辅助进程不可用且已停止重启')
            time.sleep(0.02)
        raise wire.HandshakeError('原生辅助进程未在 %.1f 秒内完成握手' % (timeout or
                                                       self._config.startup_timeout_s))

    def probe(self, timeout: Optional[float] = None) -> Mapping[str, Any]:
        """Capability report that never raises; ``available`` says it all."""
        try:
            handshake = self.handshake(timeout)
        except Exception as error:  # noqa: BLE001 - reported, not raised
            return {'available': False, 'reason': str(error), 'handshake': None,
                    'worker_pid': 0, 'restarts': len(self._restarts),
                    'log_tail': tuple(self._log_tail)[-20:], 'capabilities': None}
        return {'available': True, 'reason': '', 'handshake': handshake,
                'worker_pid': handshake.pid, 'restarts': len(self._restarts),
                'log_tail': tuple(self._log_tail)[-20:],
                'capabilities': self._capability_payload()}

    # ------------------------------------------------------------------
    # Reading job outcomes
    # ------------------------------------------------------------------
    def warm(self) -> None:
        """Start the helper now, without waiting for it.

        The helper loads VTK and creates an OpenGL context before it answers
        the handshake, which costs about 2.3 seconds on a real project.  The
        call is asynchronous, so the caller can go on preparing the request
        while that happens.  It is idempotent and a no-op once the controller
        closed.
        """
        self._ensure_started()

    def state(self, job_id: str) -> str:
        """The job's current state, or ``''`` for an unknown job."""
        with self._lock:
            job = self._jobs.get(job_id)
            return job.state if job is not None else ''

    def result(self, job_id: str) -> Optional[Mapping[str, Any]]:
        """A validated completed/failed envelope, never a fabricated partial result.

        Transport failures without a worker envelope still return None. A
        shader/source failure retains its explanation for the service caller.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.state not in (wire.STATE_COMPLETED,wire.STATE_FAILED) or job.result is None:
                return None
            return dict(job.result)

    def wait(self, job_id: str, timeout: float = 120.0) -> Optional[Mapping[str, Any]]:
        """Block until the job leaves queued/running. Returns the result or None."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            if self.state(job_id) not in ('queued', 'running'):
                return self.result(job_id)
            time.sleep(0.01)
        return None

    def wait_for_read_release(self, job_id: str, timeout: float = 5.0) -> bool:
        """A cancelled publish is not proof that native file reads stopped."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                job = self._jobs.get(job_id)
                process = self._process
                if (job is None or not job.sent_mono or job.read_finished or
                        process is None or process.pid != job.worker_pid or process.poll() is not None):
                    return True
            if time.monotonic() >= deadline:
                self._kill('dependency-lease-release')
                return process.poll() is not None
            time.sleep(.02)

    def forget(self, job_id: str, *, discard_output: bool = False) -> None:
        """Drop one finished job from the bookkeeping (outputs stay on disk)."""
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is not None:
            self._drop_latest(job)
            if discard_output and job.result and job.result.get('output_path'):
                output = Path(job.result['output_path'])
                with self._lock:
                    paths = self._session_outputs.get(job.identity['project_session'], [])
                    if output in paths:
                        paths.remove(output)
                try:
                    output.unlink(missing_ok=True)
                except OSError:
                    pass

    def _capability_payload(self) -> dict:
        handshake = self._handshake
        if handshake is None:
            return {
                'available': False, 'backend_version': '', 'formats': {},
                'preview': False, 'wireframe': False, 'material_edges': False,
                'appearance': False, 'statistics': 'none', 'reason': '尚未握手',
            }
        return {
            'available': True,
            'backend_version': handshake.backend_version,
            'vtk_version': handshake.vtk_version,
            'formats': dict(handshake.formats),
            'preview': bool(handshake.formats),
            'wireframe': bool(handshake.wireframe),
            'material_edges': bool(handshake.material_edges),
            'appearance': bool(handshake.appearance),
            'statistics': handshake.statistics,
            'reason': '',
        }

    # ------------------------------------------------------------------
    # Worker lifecycle
    # ------------------------------------------------------------------
    def _ensure_started(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._process is not None and self._process.poll() is None:
                return
            if self._starting:
                return
            if self._restart_budget_exhausted():
                if not self._handshake_error:
                    self._handshake_error = ('原生辅助进程已达到重启上限（%d 次），已停止重启'
                                             % max(1, int(self._config.restart_limit)))
                reason = self._handshake_error
                exhausted = True
            else:
                exhausted = reason = None
                self._starting = True
                # A new helper is wanted, so the previous "stop it" request
                # no longer describes what should happen to this one.
                self._shutdown_requested = False
        if exhausted is not None:
            self._fail_all(wire.ERR_WORKER_UNAVAILABLE, reason)
            return
        thread = threading.Thread(target=self._run_worker, name='model-worker',
                                  daemon=True)
        with self._lock:
            self._threads.append(thread)
        thread.start()

    def _restart_budget_exhausted(self) -> bool:
        window = max(1.0, float(self._config.restart_window_s))
        now = time.monotonic()
        while self._restarts and now - self._restarts[0] > window:
            self._restarts.popleft()
        return len(self._restarts) >= max(1, int(self._config.restart_limit))

    def _run_worker(self) -> None:
        """One helper lifetime: launch, handshake, serve, then report the loss."""
        try:
            listener, port, token = self._open_listener()
        except OSError as error:
            self._fail_all(wire.ERR_WORKER_UNAVAILABLE, '无法建立本地通道：%s' % error)
            return
        process = None
        channel = None
        try:
            process = self._spawn(port, token)
            channel, hello = self._accept(listener, token)
            if wire.check_message(hello) != wire.HELLO:
                raise wire.HandshakeError('辅助进程首帧不是握手')
            handshake = wire.Handshake.from_payload(hello.get('handshake') or {})
            problems = handshake.problems(self._expected_worker())
            if problems:
                raise wire.HandshakeError('；'.join(problems))
            with self._lock:
                self._handshake = handshake
                self._handshake_error = ''
                self._process = process
                self._channel = channel
                self._worker_pid = handshake.pid or process.pid
                self._worker_started_mono = time.monotonic()
                self._last_used_mono = time.monotonic()
                self._current = None
                self._shutdown_requested = False
                self._graceful_goodbye = False
            self._worker_instance_id = uuid.uuid4().hex[:12]
            self._emit(EVENT_WORKER_STARTED, backend_version=handshake.backend_version,
                       worker_pid=self._worker_pid,
                       worker_instance_id=self._worker_instance_id,
                       process_id=os.getpid(),
                       vtk_version=handshake.vtk_version,
                       formats=wire.describe_formats(handshake.formats),
                       restarts=len(self._restarts))
            reason = self._serve(channel, handshake)
            with self._lock:
                self._exit_reason = reason
        except Exception as error:  # noqa: BLE001 - reported with a code
            self._note_log('controller: %s' % error)
            with self._lock:
                self._exit_reason = 'error'
                if self._handshake is None:
                    self._handshake_error = str(error)
                starting_error = self._handshake is None
            self._emit(EVENT_WORKER_LOST, exit_code=None,
                       worker_instance_id=getattr(self, '_worker_instance_id', ''),
                       process_id=os.getpid(),
                       error_message=str(error)[:300],
                       log_tail=list(self._log_tail)[-12:],
                       restarts=len(self._restarts))
            if starting_error:
                # Nothing can serve these jobs; failing them now is better than
                # leaving the UI waiting for a helper that will not appear.
                self._fail_all(wire.ERR_WORKER_UNAVAILABLE, str(error))
        finally:
            with self._lock:
                self._starting = False
            self._close_channel()
            if listener is not None:
                try:
                    listener.close()
                except OSError:
                    pass
            self._reap(process)

    def _expected_worker(self) -> str:
        if self._config.worker_name:
            return str(self._config.worker_name)
        if '--mode' in self._config.worker_worker_args:
            index = self._config.worker_worker_args.index('--mode')
            try:
                mode = self._config.worker_worker_args[index + 1]
            except IndexError:
                mode = 'render'
        else:
            mode = 'render'
        return {'preview': wire.WORKER_NAME_PREVIEW,
                'statistics': wire.WORKER_NAME_STATISTICS}.get(
                    mode, wire.WORKER_NAME_RENDER)

    def _open_listener(self) -> tuple[socket.socket, int, str]:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        listener.settimeout(max(5.0, self._config.startup_timeout_s))
        token = hashlib.sha256(os.urandom(32)).hexdigest()[:32]
        return listener, listener.getsockname()[1], token

    def _spawn(self, port: int, token: str) -> subprocess.Popen:
        scratch = self._config.scratch()
        self._stdout_path = scratch / 'worker-stdout.log'
        # One worker per session can log for hours. Rotate before it grows
        # without bound; the previous tail is all a restart analysis needs.
        try:
            if self._stdout_path.is_file() and self._stdout_path.stat().st_size > 4 * 1024 * 1024:
                rotated = self._stdout_path.with_suffix('.log.1')
                rotated.unlink(missing_ok=True)
                self._stdout_path.replace(rotated)
        except OSError:
            pass
        log_handle = open(self._stdout_path, 'ab', buffering=0)
        if getattr(sys, 'frozen', False):
            allowed = {'prism.components.model_worker', 'prism.components.model_preview',
                       'prism.components.material_preview'}
            if self._config.worker_target not in allowed:
                log_handle.close()
                raise ValueError('Unrecognised bundled worker')
            args = [sys.executable, '--prism-native-worker', self._config.worker_target]
        elif self._config.worker_target.endswith('.py'):
            args = [self._config.resolved_python(), '-I']
            args.append(str(Path(self._config.worker_target)))
        else:
            args = [self._config.resolved_python(), '-I']
            args.extend(['-m', self._config.worker_target])
        args.extend(['--ipc-port', str(port), '--ipc-token', token,
                     '--cache-root', str(Path(self._config.cache_root))])
        args.extend(self._config.worker_worker_args)
        env = dict(os.environ)
        env.setdefault('PYTHONIOENCODING', 'utf-8')
        env.setdefault('PYTHONUTF8', '1')
        env.update({str(k): str(v) for k, v in (self._config.env or {}).items()})
        creationflags = 0
        if os.name == 'nt':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
        try:
            return subprocess.Popen(
                args, cwd=self._config.cwd or None, env=env,
                stdin=subprocess.DEVNULL, stdout=log_handle, stderr=subprocess.STDOUT,
                close_fds=True, creationflags=creationflags)
        finally:
            # The child owns its inherited handle; the parent must not keep the
            # log file open or a later restart cannot rotate it on Windows.
            log_handle.close()

    def _accept(self, listener: socket.socket,
                token: str) -> tuple[wire.Channel, dict]:
        """Accept the helper's connection and return its first (hello) frame."""
        deadline = time.monotonic() + max(5.0, self._config.startup_timeout_s)
        while time.monotonic() < deadline:
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                break
            channel = wire.Channel(connection)
            hello = channel.recv(timeout=5.0)
            if hello is None:
                channel.close()
                continue
            if str(hello.get('token') or '') != token:
                # A stray local connection must not be mistaken for our helper.
                channel.close()
                continue
            return channel, hello
        raise wire.HandshakeError('原生辅助进程未在 %.0f 秒内连接'
                                  % self._config.startup_timeout_s)

    def _serve(self, channel: wire.Channel, handshake: wire.Handshake) -> str:
        """Read frames until the helper exits or shutdown was requested.

        Returns the reason the loop ended. A clean EOF is *not* a crash: the
        helper exits on its own after an idle shutdown, and the controller must
        not count that against the restart budget.
        """
        reason = 'stop'
        while not self._stop.is_set():
            self._dispatch_ready()
            try:
                envelope = channel.recv(timeout=0.1)
            except wire.ProtocolError as error:
                self._note_log('controller: %s' % error)
                self._fail_current(wire.ERR_BAD_PROTOCOL, str(error))
                reason = 'protocol'
                break
            if envelope is None:
                if channel.closed:
                    reason = 'eof'
                    break
                self._enforce_deadlines()
                self._maybe_idle_exit()
                continue
            self._handle_frame(envelope, handshake)
        if reason == 'stop':
            self._kill('serve-loop-stop')
        return reason

    def _handle_frame(self, envelope: Mapping[str, Any], handshake: wire.Handshake) -> None:
        try:
            kind = wire.check_message(envelope)
        except wire.ProtocolError as error:
            self._note_log('controller: %s' % error)
            return
        if kind == wire.LOG:
            self._note_log(str(envelope.get('message') or ''),
                           level=str(envelope.get('level') or 'info'))
        elif kind == wire.EVENT:
            self._handle_event(envelope)
        elif kind == wire.RESULT:
            self._handle_result(envelope)
        elif kind == wire.BYE:
            self._note_log('helper said goodbye')
            with self._lock:
                self._graceful_goodbye = True

    def _handle_event(self, envelope: Mapping[str, Any]) -> None:
        name = str(envelope.get('event') or '')
        fields = dict(envelope.get('fields') or {})
        job_id = str(fields.get('job_id') or envelope.get('job_id') or '')
        if job_id:
            fields.setdefault('job_id', job_id)
        self._emit(name or 'event', **fields)

    def _handle_result(self, envelope: Mapping[str, Any]) -> None:
        job_id = str(envelope.get('job_id') or '')
        with self._lock:
            job = self._jobs.get(job_id)
            current = self._current
            handshake = self._handshake
        if job is None:
            self._note_log('controller: 收到未知任务的结果 %r' % job_id, level='warning')
            return
        try:
            state, error_code, error_message = wire.check_result(
                envelope, expected_job=job_id, expected_identity=job.identity)
        except wire.ProtocolError as error:
            self._fail_job(job, wire.ERR_BAD_PROTOCOL, str(error))
            with self._lock:
                self._current = None
            return
        job.read_finished = True
        if not job.publish_eligible:
            # Cancelled, or superseded while the native call was running.
            with self._lock:
                cancelled = job.state == 'cancelled'
                job.state = 'cancelled' if cancelled else 'stale'
                if self._current == job_id:
                    self._current = None
            if not cancelled:
                self._emit(EVENT_STALE, job_id=job_id,
                           error_code=wire.ERR_STALE, reason='superseded')
            return
        if state == wire.STATE_COMPLETED:
            self._complete(job, envelope, handshake)
        elif state == wire.STATE_FAILED:
            # Preserve the validated worker failure for the service/UI caller.
            # Otherwise wait() returns None and the precise render error is lost.
            with self._lock:
                job.result = dict(envelope)
            self._fail_job(job, error_code or wire.ERR_BAD_FILE, error_message)
        elif state == wire.STATE_CANCELLED:
            with self._lock:
                job.state = 'cancelled'
            self._emit(EVENT_CANCELLED, job_id=job_id, error_code=wire.ERR_CANCELLED)
        else:
            with self._lock:
                job.state = 'stale'
            self._emit(EVENT_STALE, job_id=job_id, error_code=wire.ERR_STALE)
        with self._lock:
            if self._current == job_id:
                self._current = None
            self._last_used_mono = time.monotonic()

    def _complete(self, job: _Job, envelope: Mapping[str, Any],
                  handshake: Optional[wire.Handshake]) -> None:
        outputs = dict(envelope.get('outputs') or {})
        output = self._publish_output(job, outputs)
        if output is None and outputs:
            self._fail_job(job, wire.ERR_OUTPUT_INVALID, '结果文件未通过校验')
            return
        with self._lock:
            job.state = wire.STATE_COMPLETED
            job.result = dict(envelope)
            job.result['outputs'] = outputs
            if output is not None:
                job.result['output_path'] = output
            else:
                job.result['output_path'] = None
        statistics = envelope.get('statistics')
        if statistics:
            self._emit(EVENT_NATIVE_LOADED, job_id=job.job_id,
                       backend_version=(handshake.backend_version if handshake else ''),
                       statistics=json.dumps(statistics, ensure_ascii=False)[:1200])
        self._emit(EVENT_OUTPUT_WRITTEN, job_id=job.job_id,
                   bytes=int(outputs.get('bytes') or 0),
                   sha256=str(outputs.get('sha256') or '')[:32])
        self._emit(EVENT_INSTALLED, job_id=job.job_id,
                   output_path=str(output or ''))
        self._emit(EVENT_COMPLETED, job_id=job.job_id,
                   state=wire.STATE_COMPLETED,
                   timing=dict(envelope.get('timing') or {}))

    def _publish_output(self, job: _Job, outputs: Mapping[str, Any]) -> Optional[str]:
        """Verify a helper output and move it into place atomically."""
        if not outputs:
            return None
        temporary = _as_text(outputs.get('path'))
        if not temporary:
            return None
        source = Path(temporary)
        if not source.is_file():
            return None
        try:
            size = source.stat().st_size
            digest = _sha256_file(source)
        except OSError:
            return None
        if not wire.hashes_agree(size, digest, outputs):
            self._note_log('controller: 结果文件哈希不匹配 %s' % source.name, level='warning')
            return None
        destination = Path(job.output_ref or self._default_output(job))
        destination.parent.mkdir(parents=True, exist_ok=True)
        staged = destination.with_name(destination.name + '.part')
        try:
            shutil.move(str(source), str(staged))
            os.replace(str(staged), str(destination))
        except OSError as error:
            self._note_log('controller: 无法发布结果 %s' % error, level='warning')
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        session = str(job.identity.get('project_session') or '')
        if session:
            published = self._session_outputs.setdefault(session, [])
            published.append(destination)
        return str(destination)

    def _default_output(self, job: _Job) -> str:
        model = str(job.identity.get('model_id') or 'model').replace('/', '_')
        revision = job.identity.get('appearance_revision')
        return str(Path(self._config.cache_root) / 'covers'
                   / ('%s-%s-%s.png' % (model, job.identity.get('source_revision'), revision)))

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _dispatch_ready(self) -> None:
        with self._lock:
            if self._current is not None:
                return
            job = None
            while self._queued:
                candidate = self._jobs.get(self._queued[0])
                if candidate is None or candidate.state not in ('queued',):
                    self._queued.pop(0)
                    continue
                job = candidate
                break
            if job is None:
                return
            job.state = 'running'
            job.sent_mono = time.monotonic()
            job.deadline_mono = job.sent_mono + max(1.0, self._config.request_timeout_s)
            self._current = job.job_id
            # The helper is now doing the work it was started for; it is no
            # longer a helper that was started and never used.
            self._served = True
            self._queued.remove(job.job_id)
            channel = self._channel
            job.worker_pid = self._worker_pid
        if channel is None:
            self._fail_job(job, wire.ERR_WORKER_UNAVAILABLE, '原生辅助进程不可用')
            with self._lock:
                self._current = None
            return
        frame = wire.message(
            wire.REQUEST, job_id=job.job_id, kind=job.kind,
            identity=dict(job.identity), payload=dict(job.request_payload),
            output_ref=job.output_ref or '')
        try:
            if job.request_payload.get('action') == 'open' and job.request_payload.get('owner_window_id'):
                _allow_preview_foreground(job.worker_pid, int(job.request_payload['owner_window_id']))
            channel.send(frame)
        except (wire.ProtocolError, OSError) as error:
            self._fail_job(job, wire.ERR_WORKER_UNAVAILABLE, str(error))
            with self._lock:
                self._current = None
            self._kill('send-failed')
            return
        identity = job.identity
        self._emit(EVENT_STARTED, job_id=job.job_id, kind=job.kind,
                   model_id=identity.get('model_id'),
                   request_id=identity.get('request_id'),
                   source_revision=identity.get('source_revision'),
                   appearance_revision=identity.get('appearance_revision'),
                   camera_revision=identity.get('camera_revision'),
                   priority=job.priority)

    def _enforce_deadlines(self) -> None:
        with self._lock:
            job = self._jobs.get(self._current or '')
            if job is None:
                return
            expired = time.monotonic() > job.deadline_mono > 0
        if expired:
            self._fail_job(job, wire.ERR_TIMEOUT,
                           '原生调用超过 %.1f 秒未返回' % self._config.request_timeout_s)
            with self._lock:
                job.state = 'failed'
                self._current = None
            self._kill('timeout')

    def _maybe_idle_exit(self) -> None:
        config = self._config
        with self._lock:
            if config.idle_exit_s <= 0 and config.unused_exit_s <= 0:
                return
            idle_for = time.monotonic() - self._last_used_mono
            # A helper that was started early but never served a job has
            # nothing to show; it gives its memory back instead of waiting
            # for the controller to be shut down.
            unused = (config.unused_exit_s > 0 and not self._served
                      and idle_for > config.unused_exit_s)
            idle = (config.idle_exit_s > 0 and self._current is None and not self._queued
                    and idle_for > config.idle_exit_s)
            channel = self._channel
        if (idle or unused) and channel is not None:
            self._shutdown_requested = True
            self._send_soft(wire.message(wire.SHUTDOWN))

    def _escalate_cancel(self, job_id: str) -> None:
        deadline = time.monotonic() + max(0.2, self._config.cancel_grace_s)
        while time.monotonic() < deadline:
            with self._lock:
                if self._current != job_id:
                    return
            time.sleep(0.02)
        with self._lock:
            still_running = self._current == job_id
        if still_running:
            self._note_log('controller: 取消未在宽限期内生效，结束原生进程', level='warning')
            self._kill('cancel-timeout')

    # ------------------------------------------------------------------
    # Failure handling and (re)start bookkeeping
    # ------------------------------------------------------------------
    def _fail_current(self, code: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(self._current or '')
        if job is not None:
            self._fail_job(job, code, message)

    def _fail_job(self, job: _Job, code: str, detail: str) -> None:
        with self._lock:
            if job.state in ('cancelled', 'stale'):
                return
            job.state = 'failed'
            tail = list(self._log_tail)[-12:]
        self._emit(EVENT_FAILED, job_id=job.job_id, error_code=code,
                   error_message=str(detail)[:400], log_tail=tail,
                   worker_pid=job.worker_pid)

    def _fail_all(self, code: str, detail: str) -> None:
        with self._lock:
            job_ids = [job.job_id for job in self._jobs.values()
                       if job.state in ('queued', 'running')]
        for job_id in job_ids:
            with self._lock:
                job = self._jobs.get(job_id)
            if job is not None:
                self._fail_job(job, code, detail)
                with self._lock:
                    job.state = 'failed'
                    if self._current == job_id:
                        self._current = None

    def _reap(self, process: Optional[subprocess.Popen]) -> None:
        """Collect the exit status, record it, and let the scheduler continue."""
        code = None
        if process is not None:
            try:
                code = process.poll()
                if code is None:
                    # The channel can report EOF a moment before the process is
                    # reaped; waiting briefly keeps the real crash code (7, or a
                    # Windows exception code) instead of "terminated by us".
                    try:
                        code = process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        try:
                            code = process.wait(timeout=3.0)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            code = process.wait(timeout=3.0)
            except Exception:  # noqa: BLE001 - never raise from cleanup
                code = None
        with self._lock:
            if self._process is process:
                self._process = None
            self._channel = None
            crashed = code not in (0, None)
            expected = (self._stop.is_set() or self._closed
                        or self._shutdown_requested or self._exit_reason == 'stop'
                        or (self._graceful_goodbye and code == 0))
            # A helper we asked to stop is not a crash and must not burn the
            # restart budget; anything else is one, even when the exit code is 0.
            # A helper that never received a job cannot have broken one, and
            # its failure must not spend the attempts a real job still needs.
            if not expected and self._served:
                self._restarts.append(time.monotonic())
            pending = [job for job in self._jobs.values()
                       if job.state == 'running']
            self._current = None
            restarts = len(self._restarts)
        tail = self._read_tail()
        if pending:
            detail = '原生进程退出（code=%s）' % (code,)
            for job in pending:
                self._fail_job(job, wire.ERR_NATIVE_CRASH, detail)
                with self._lock:
                    job.state = 'failed'
        if not expected:
            # A helper we asked to stop is not reported as a loss, even when
            # the forced termination gives it a non-zero exit code.
            self._emit(EVENT_WORKER_LOST, exit_code=code, log_tail=tail,
                       restarts=restarts)
        if not self._stop.is_set() and not self._closed and not self._shutdown_requested:
            # A helper that said goodbye (the preview window closed) is
            # replaced so the next model finds it warm.  A helper that *we*
            # asked to stop is not: restarting it would only make it stop
            # again, forever.
            self._ensure_started()

    def _read_tail(self) -> list[str]:
        path = self._stdout_path
        if path is None or not path.is_file():
            return list(self._log_tail)[-20:]
        try:
            with open(path, 'rb') as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                window = min(size, 64 * 1024)
                handle.seek(size - window)
                text = handle.read().decode('utf-8', 'replace')
        except OSError:
            return list(self._log_tail)[-20:]
        lines = [line for line in text.splitlines() if line.strip()]
        return lines[-max(5, self._config.log_tail_lines):]

    def _kill(self, reason: str) -> None:
        with self._lock:
            process = self._process
            channel = self._channel
        self._note_log('controller: 结束原生进程（%s）' % reason, level='warning')
        if channel is not None:
            channel.close()
        if process is not None and process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass

    def _close_channel(self) -> None:
        with self._lock:
            channel = self._channel
            self._channel = None
        if channel is not None:
            channel.close()

    def _send_soft(self, frame: Mapping[str, Any]) -> None:
        with self._lock:
            channel = self._channel
        if channel is None:
            return
        try:
            channel.send(frame)
        except (wire.ProtocolError, OSError):
            pass

    def _note_log(self, text: str, level: str = 'info') -> None:
        line = str(text)[:2000]
        if not line:
            return
        with self._lock:
            self._log_tail.append(line)
        self._emit(EVENT_NATIVE_LOG, level=level, message=line)

    # ------------------------------------------------------------------
    # Small helpers
    # ------------------------------------------------------------------
    def _next_job_id(self, identity: Mapping[str, Any]) -> str:
        self._counter += 1
        base = '%s-%s' % (identity.get('model_id') or 'model', self._counter)
        return base.replace('/', '_').replace(' ', '_')

    def _trim_key(self, key: tuple, keep: str) -> None:
        """Keep at most N queued jobs per key so a slider drag cannot pile up."""
        same = [jid for jid in self._queued
                if self._jobs[jid].key == key and jid != keep]
        limit = max(0, int(self._config.max_pending_per_key) - 1)
        while len(same) > limit:
            victim = same.pop(0)
            job = self._jobs.get(victim)
            if job is None:
                continue
            job.publish_eligible = False
            job.state = 'stale'
            self._queued.remove(victim)
            self._emit(EVENT_STALE, job_id=victim, error_code=wire.ERR_STALE,
                       reason='superseded-queued')

    def _mark_stale(self, job_id: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            if job.state == 'running':
                job.publish_eligible = False
                return
        if job is not None and job.state in ('queued',):
            with self._lock:
                job.state = 'stale'
                job.publish_eligible = False
                if job_id in self._queued:
                    self._queued.remove(job_id)
            self._emit(EVENT_STALE, job_id=job_id, error_code=wire.ERR_STALE,
                       reason='superseded')

    def _drop_latest(self, job: _Job) -> None:
        if self._latest.get(job.key) == job.job_id:
            self._latest.pop(job.key, None)

    def _sweep_session_outputs(self, session: str) -> None:
        """Delete only this session's published covers.

        The list is what this controller published for that session, so no
        other project's package can be touched by a substring match.
        """
        if not session:
            return
        with self._lock:
            published = self._session_outputs.pop(session, [])
        for path in published:
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def _wake(self) -> None:
        # The serve loop polls with a short timeout; nothing else is needed.
        self._last_used_mono = time.monotonic()

    def _emit(self, event: str, **fields: Any) -> None:
        callback = self._publish_event
        if callback is None:
            return
        # A terminal result must remain attributable after the UI clears its
        # current request. The controller already owns the immutable identity.
        with self._lock:
            job = self._jobs.get(fields.get('job_id'))
            if job is not None:
                for key, value in job.identity.items():
                    fields.setdefault(key, value)
                fields.setdefault('backend_version', self._handshake.backend_version
                                  if self._handshake is not None else 'unknown')
        try:
            callback(event, **fields)
        except Exception:  # noqa: BLE001 - logging must never break the job
            pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _as_text(value: Any) -> str:
    if value is None:
        return ''
    return str(value)


def describe_controller(config: RuntimeConfig) -> str:
    """One-line inventory for logs and the handoff report."""
    return ('worker=%s python=%s mode=%s timeout=%.0fs restarts<=%d cache=%s'
            % (config.worker_target, config.resolved_python(),
               ' '.join(config.worker_worker_args), config.request_timeout_s,
               config.restart_limit, config.cache_root))
