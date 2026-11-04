"""Local protocol between Prism and the native F3D helper processes.

Why this module exists: the helper writes F3D progress bars and VTK error text
to its own stdout/stderr, so stdout can never be a message channel. Prism and
the helper therefore talk over a private loopback socket with explicit frames,
a version handshake and a result identity that is re-checked before anything is
published.

Nothing here imports Qt, F3D or VTK: the main process must stay free of the
native runtime, and this module must be importable from both sides.

Frame format: one JSON object per line (UTF-8), terminated by ``\\n``. A frame
larger than :data:`MAX_FRAME_BYTES` is a protocol violation, not a large model:
model bytes travel as files, never inside a control frame.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional, Sequence

#: Bumped whenever a message shape or a required field changes.
NATIVE_PROTOCOL_VERSION = 1

#: The helper's product name in the handshake. Prism refuses a different helper.
WORKER_NAME_RENDER = 'prism.model.f3d.offscreen'
WORKER_NAME_PREVIEW = 'prism.model.f3d.preview'
WORKER_NAME_STATISTICS = 'prism.model.vtk.statistics'

#: Control frames stay small; images and models are files on disk.
MAX_FRAME_BYTES = 1 << 20

#: The formats this subsystem reads directly, without an intermediate GLB.
SUPPORTED_FORMATS = ('.fbx', '.obj', '.glb', '.gltf')

# ── message types ─────────────────────────────────────────────────────────
HELLO = 'hello'
WELCOME = 'welcome'
REQUEST = 'request'
RESULT = 'result'
EVENT = 'event'
LOG = 'log'
CANCEL = 'cancel'
CLOSE_PROJECT = 'close_project'
SHUTDOWN = 'shutdown'
BYE = 'bye'

MESSAGE_TYPES = frozenset({
    HELLO, WELCOME, REQUEST, RESULT, EVENT, LOG, CANCEL, CLOSE_PROJECT,
    SHUTDOWN, BYE,
})

# ── result states ─────────────────────────────────────────────────────────
STATE_COMPLETED = 'completed'
STATE_FAILED = 'failed'
STATE_CANCELLED = 'cancelled'
STATE_STALE = 'stale'
STATES = frozenset({STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED, STATE_STALE})

# ── error codes ───────────────────────────────────────────────────────────
ERR_UNSUPPORTED_FORMAT = 'unsupported_format'
ERR_UNSUPPORTED_CAPABILITY = 'unsupported_capability'
ERR_MISSING_DEPENDENCY = 'missing_dependency'
ERR_BAD_FILE = 'bad_file'
ERR_NATIVE_CRASH = 'native_crash'
ERR_TIMEOUT = 'timeout'
ERR_CANCELLED = 'cancelled'
ERR_STALE = 'stale'
ERR_BAD_PROTOCOL = 'bad_protocol'
ERR_WORKER_UNAVAILABLE = 'worker_unavailable'
ERR_OUTPUT_INVALID = 'output_invalid'
ERR_WORKER_BUSY = 'worker_busy'
ERR_SHUTDOWN = 'shutting_down'

ERROR_CODES = frozenset({
    ERR_UNSUPPORTED_FORMAT, ERR_UNSUPPORTED_CAPABILITY, ERR_MISSING_DEPENDENCY,
    ERR_BAD_FILE, ERR_NATIVE_CRASH, ERR_TIMEOUT, ERR_CANCELLED, ERR_STALE,
    ERR_BAD_PROTOCOL, ERR_WORKER_UNAVAILABLE, ERR_OUTPUT_INVALID,
    ERR_WORKER_BUSY, ERR_SHUTDOWN,
})

#: Identity keys a result must repeat exactly. They mirror
#: :class:`prism.model_library.contracts.ModelRequestIdentity`.
IDENTITY_KEYS = (
    'project_session', 'open_epoch', 'model_id', 'request_id',
    'source_revision', 'appearance_revision', 'camera_revision',
    'protocol_version',
)

#: A published image must carry these keys; anything less is not verifiable.
OUTPUT_KEYS = ('path', 'sha256', 'bytes', 'width', 'height')


class ProtocolError(RuntimeError):
    """The other side sent something this side must not accept."""


class PeerDisconnected(ProtocolError):
    """The transport closed abnormally; distinct from an invalid frame."""


class HandshakeError(ProtocolError):
    """Version or capability handshake failed. Never softened into success."""


# ── framing ───────────────────────────────────────────────────────────────
def encode(message: Mapping[str, Any]) -> bytes:
    """Serialize one message as a single UTF-8 line."""
    if not isinstance(message, Mapping):
        raise ProtocolError('消息必须是映射')
    body = json.dumps(dict(message), ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False)
    data = body.encode('utf-8')
    if len(data) > MAX_FRAME_BYTES:
        raise ProtocolError('控制帧超过上限 %d 字节' % MAX_FRAME_BYTES)
    return data + b'\n'


class FrameDecoder:
    """Reassembles line frames out of arbitrary socket chunks."""

    def __init__(self, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._buffer = bytearray()
        self._max = int(max_frame_bytes)

    def feed(self, chunk: bytes) -> list[dict]:
        if chunk:
            self._buffer.extend(chunk)
        if len(self._buffer) > self._max and b'\n' not in self._buffer:
            raise ProtocolError('控制帧超过上限 %d 字节' % self._max)
        frames: list[dict] = []
        while True:
            index = self._buffer.find(b'\n')
            if index < 0:
                break
            raw = bytes(self._buffer[:index])
            del self._buffer[:index + 1]
            if not raw.strip():
                continue
            if len(raw) > self._max:
                raise ProtocolError('控制帧超过上限 %d 字节' % self._max)
            try:
                message = json.loads(raw.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ProtocolError('无法解析控制帧：%s' % error) from error
            if not isinstance(message, dict):
                raise ProtocolError('控制帧必须是 JSON 对象')
            frames.append(message)
        return frames

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)


class Channel:
    """Blocking, framed channel over a connected socket.

    Both sides use the same class. ``recv`` returns ``None`` on a clean EOF so
    the caller can distinguish "the helper exited" from "the helper sent
    rubbish", which have different error codes and different UI text.
    """

    def __init__(self, sock: socket.socket, max_frame_bytes: int = MAX_FRAME_BYTES):
        self._socket = sock
        self._decoder = FrameDecoder(max_frame_bytes)
        self._pending: list[dict] = []
        self._closed = False

    @classmethod
    def connect(cls, host: str, port: int, timeout: Optional[float] = 10.0) -> 'Channel':
        sock = socket.create_connection((host, int(port)), timeout=timeout)
        sock.settimeout(None)
        return cls(sock)

    def send(self, message: Mapping[str, Any]) -> None:
        if self._closed:
            raise ProtocolError('通道已关闭')
        self._socket.sendall(encode(message))

    def recv(self, timeout: Optional[float] = None) -> Optional[dict]:
        """Return the next frame, or ``None`` when there is nothing to read.

        ``None`` means "try again later" (timeout, non-blocking socket) *and*
        "the peer is gone" (EOF). Callers tell them apart with
        :attr:`closed` — a distinction the Qt event loop needs, because it
        polls with ``timeout=0`` and a non-blocking socket raises
        ``BlockingIOError`` instead of ``socket.timeout``.
        """
        if self._pending:
            return self._pending.pop(0)
        if self._closed:
            return None
        self._socket.settimeout(timeout)
        while True:
            try:
                chunk = self._socket.recv(65536)
            except socket.timeout:
                return None
            except (BlockingIOError, InterruptedError):
                return None
            except OSError as error:
                if getattr(error, 'winerror', None) == 10035:  # WSAEWOULDBLOCK
                    return None
                import errno
                if (getattr(error, 'winerror', None) in (10054, 10053)
                        or error.errno in (errno.ECONNRESET, errno.ECONNABORTED)):
                    self._closed = True
                    raise PeerDisconnected('peer_reset') from error
                raise ProtocolError('读取控制帧失败：%s' % error) from error
            if not chunk:
                self._closed = True
                return None
            frames = self._decoder.feed(chunk)
            if frames:
                self._pending.extend(frames[1:])
                return frames[0]

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._socket.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self._socket.close()
        except OSError:
            pass

    @property
    def closed(self) -> bool:
        return self._closed


def message(message_type: str, **fields: Any) -> dict:
    """Build one message with the protocol version stamped in.

    The parameter is not named ``kind`` on purpose: request payloads carry a
    ``kind`` field of their own and must not collide with the frame type.
    """
    if message_type not in MESSAGE_TYPES:
        raise ProtocolError('未知的消息类型：%r' % (message_type,))
    payload = {'type': message_type, 'v': NATIVE_PROTOCOL_VERSION}
    payload.update(fields)
    return payload


def check_message(envelope: Mapping[str, Any]) -> str:
    """Return the message type, or raise when the envelope is unusable."""
    if not isinstance(envelope, Mapping):
        raise ProtocolError('控制帧必须是 JSON 对象')
    kind = envelope.get('type')
    if kind not in MESSAGE_TYPES:
        raise ProtocolError('未知的消息类型：%r' % (kind,))
    version = envelope.get('v')
    if version != NATIVE_PROTOCOL_VERSION:
        raise ProtocolError('协议版本不匹配：收到 %r，期望 %d'
                            % (version, NATIVE_PROTOCOL_VERSION))
    return kind


# ── handshake ─────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Handshake:
    """What the helper really is, measured at startup.

    Every capability here is a measured claim. ``wireframe`` stays ``False``
    until a helper build can draw surface-free edges, and ``statistics``
    names the provenance so a reader can tell a loaded count from a
    file-declared count.
    """

    worker: str
    protocol_version: int = NATIVE_PROTOCOL_VERSION
    backend_version: str = ''
    vtk_version: str = ''
    python_version: str = ''
    pid: int = 0
    formats: Mapping[str, str] = field(default_factory=dict)
    readers: tuple[str, ...] = ()
    wireframe: bool = False
    material_edges: bool = False
    appearance: bool = False
    statistics: str = 'none'
    features: Mapping[str, Any] = field(default_factory=dict)
    limits: Mapping[str, Any] = field(default_factory=dict)

    def to_payload(self) -> dict:
        return {
            'worker': self.worker,
            'protocol_version': self.protocol_version,
            'backend_version': self.backend_version,
            'vtk_version': self.vtk_version,
            'python_version': self.python_version,
            'pid': self.pid,
            'formats': dict(self.formats),
            'readers': list(self.readers),
            'wireframe': bool(self.wireframe),
            'material_edges': bool(self.material_edges),
            'appearance': bool(self.appearance),
            'statistics': self.statistics,
            'features': dict(self.features),
            'limits': dict(self.limits),
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> 'Handshake':
        if not isinstance(payload, Mapping):
            raise ProtocolError('握手内容必须是映射')
        formats = payload.get('formats') or {}
        if not isinstance(formats, Mapping):
            raise ProtocolError('握手 formats 必须是映射')
        readers = payload.get('readers') or ()
        if isinstance(readers, str) or not isinstance(readers, Iterable):
            raise ProtocolError('握手 readers 必须是列表')
        try:
            protocol_version = int(payload.get('protocol_version') or 0)
            pid = int(payload.get('pid') or 0)
        except (TypeError, ValueError) as error:
            raise ProtocolError('握手版本号无效：%s' % error) from error
        return cls(
            worker=str(payload.get('worker') or ''),
            protocol_version=protocol_version,
            backend_version=str(payload.get('backend_version') or ''),
            vtk_version=str(payload.get('vtk_version') or ''),
            python_version=str(payload.get('python_version') or ''),
            pid=pid,
            formats={str(k): str(v) for k, v in formats.items()},
            readers=tuple(str(item) for item in readers),
            wireframe=bool(payload.get('wireframe')),
            material_edges=bool(payload.get('material_edges')),
            appearance=bool(payload.get('appearance')),
            statistics=str(payload.get('statistics') or 'none'),
            features=dict(payload.get('features') or {}),
            limits=dict(payload.get('limits') or {}),
        )

    def problems(self, expected_worker: str) -> tuple[str, ...]:
        """Everything that makes this helper unusable, in report order.

        A render or preview helper must read all four formats, because the user
        drops any of them on the library. A statistics helper is a different
        contract: it is allowed to lack FBX (there is no independent FBX reader)
        as long as it says so and reports a real basis.
        """
        reasons = []
        if self.worker != expected_worker:
            reasons.append('辅助进程不匹配：%r != %r' % (self.worker, expected_worker))
        if self.protocol_version != NATIVE_PROTOCOL_VERSION:
            reasons.append('协议版本不匹配：%d != %d'
                           % (self.protocol_version, NATIVE_PROTOCOL_VERSION))
        if not self.backend_version:
            reasons.append('辅助进程没有报告后端版本')
        if expected_worker == WORKER_NAME_STATISTICS:
            if self.statistics in ('', 'none'):
                reasons.append('统计辅助进程没有声明统计口径')
            if not self.formats:
                reasons.append('统计辅助进程没有可用的几何读取器')
            return tuple(reasons)
        missing = [ext for ext in SUPPORTED_FORMATS if not self.formats.get(ext)]
        if missing:
            reasons.append('缺少格式读取器：%s' % ', '.join(missing))
        return tuple(reasons)

    @property
    def backend_version_label(self) -> str:
        return self.backend_version or 'unknown'


# ── jobs and results ──────────────────────────────────────────────────────
def identity_payload(identity: Any) -> dict:
    """Copy the identity fields of a contract identity into a plain dict."""
    payload = {}
    for key in IDENTITY_KEYS:
        value = getattr(identity, key, None)
        payload[key] = int(value) if key in (
            'open_epoch', 'source_revision', 'appearance_revision',
            'camera_revision', 'protocol_version') else value
    return payload


def identity_mismatch(sent: Mapping[str, Any], received: Mapping[str, Any]) -> Optional[str]:
    """Return the first identity field that does not agree, or ``None``."""
    if not isinstance(received, Mapping):
        return 'identity'
    for key in IDENTITY_KEYS:
        if sent.get(key) != received.get(key):
            return key
    return None


def check_result(envelope: Mapping[str, Any], *, expected_job: str,
                 expected_identity: Mapping[str, Any]) -> tuple[str, str, str]:
    """Validate one result frame. Returns ``(state, error_code, error_message)``.

    A frame that fails validation is reported as a protocol failure: it must
    never be turned into a successful publish.
    """
    kind = check_message(envelope)
    if kind != RESULT:
        raise ProtocolError('期望结果帧，收到 %r' % kind)
    job_id = envelope.get('job_id')
    if expected_job and job_id != expected_job:
        raise ProtocolError('结果属于其他任务：%r != %r' % (job_id, expected_job))
    state = envelope.get('state')
    if state not in STATES:
        raise ProtocolError('未知的结果状态：%r' % (state,))
    mismatch = identity_mismatch(expected_identity, envelope.get('identity'))
    if mismatch is not None:
        raise ProtocolError('结果身份不匹配：%s' % mismatch)
    return str(state), str(envelope.get('error_code') or ''), str(envelope.get('error_message') or '')


def hashes_agree(path_size: int, path_hash: str, envelope: Mapping[str, Any]) -> bool:
    """Compare the metadata the helper reported with what is really on disk."""
    try:
        size = int(envelope.get('bytes'))
    except (TypeError, ValueError):
        return False
    if size != int(path_size):
        return False
    reported = str(envelope.get('sha256') or '').lower()
    return bool(reported) and reported == str(path_hash).lower()


def describe_formats(formats: Mapping[str, str]) -> str:
    parts = []
    for ext in SUPPORTED_FORMATS:
        parts.append('%s=%s' % (ext.lstrip('.'), formats.get(ext, 'unknown')))
    return ' '.join(parts)


def sequence(value: Any) -> Sequence[Any]:
    """Small helper so callers never assume a JSON list where a tuple came."""
    return value if isinstance(value, (list, tuple)) else ()
