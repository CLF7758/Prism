"""Protocol tests: framing, handshake, identity and result validation.

These are pure unit tests. They never start a native process, so a failure here
means the boundary is broken, not that the machine has no GPU.
"""
from __future__ import annotations

import json

import pytest

from prism.model_library import native_protocol as wire


def _identity(**overrides):
    base = dict(project_session='s-1', open_epoch=3, model_id='m-1',
                request_id='r-1', source_revision=7, appearance_revision=2,
                camera_revision=1, protocol_version=wire.NATIVE_PROTOCOL_VERSION)
    base.update(overrides)
    return base


# ── framing ───────────────────────────────────────────────────────────────
def test_encode_is_one_utf8_line():
    data = wire.encode({'type': wire.REQUEST, 'v': 1, 'note': '中文'})
    assert data.endswith(b'\n')
    assert data.count(b'\n') == 1
    assert json.loads(data.decode('utf-8'))['note'] == '中文'


def test_encode_rejects_oversized_frame():
    with pytest.raises(wire.ProtocolError):
        wire.encode({'type': wire.LOG, 'v': 1, 'message': 'x' * (wire.MAX_FRAME_BYTES + 10)})


def test_decoder_reassembles_split_frames():
    decoder = wire.FrameDecoder()
    payload = wire.encode({'type': wire.LOG, 'v': 1, 'message': 'hello'})
    assert decoder.feed(payload[:5]) == []
    frames = decoder.feed(payload[5:])
    assert len(frames) == 1 and frames[0]['message'] == 'hello'


def test_decoder_handles_several_frames_in_one_chunk():
    chunk = (wire.encode({'type': wire.LOG, 'v': 1, 'message': 'a'})
             + wire.encode({'type': wire.LOG, 'v': 1, 'message': 'b'}))
    frames = wire.FrameDecoder().feed(chunk)
    assert [frame['message'] for frame in frames] == ['a', 'b']


def test_decoder_skips_blank_lines():
    assert wire.FrameDecoder().feed(b'\n \n') == []


def test_decoder_rejects_invalid_json():
    with pytest.raises(wire.ProtocolError):
        wire.FrameDecoder().feed(b'{not json}\n')


def test_decoder_rejects_non_object_frame():
    with pytest.raises(wire.ProtocolError):
        wire.FrameDecoder().feed(b'[1,2,3]\n')


def test_decoder_rejects_oversized_unterminated_buffer():
    decoder = wire.FrameDecoder(max_frame_bytes=64)
    with pytest.raises(wire.ProtocolError):
        decoder.feed(b'x' * 100)


# ── envelope checks ───────────────────────────────────────────────────────
def test_message_stamps_version_and_rejects_unknown_type():
    assert wire.message(wire.LOG)['v'] == wire.NATIVE_PROTOCOL_VERSION
    with pytest.raises(wire.ProtocolError):
        wire.message('not-a-type')


def test_message_keeps_a_kind_field_of_its_own():
    # A request carries a ``kind`` of its own; it must not collide with the type.
    frame = wire.message(wire.REQUEST, kind='thumbnail', job_id='j-1')
    assert frame['type'] == wire.REQUEST and frame['kind'] == 'thumbnail'


def test_check_message_rejects_other_protocol_version():
    with pytest.raises(wire.ProtocolError):
        wire.check_message({'type': wire.LOG, 'v': wire.NATIVE_PROTOCOL_VERSION + 1})


def test_check_message_rejects_unknown_type():
    with pytest.raises(wire.ProtocolError):
        wire.check_message({'type': 'mystery', 'v': wire.NATIVE_PROTOCOL_VERSION})


def test_check_message_rejects_non_mapping():
    with pytest.raises(wire.ProtocolError):
        wire.check_message(['not', 'a', 'frame'])


# ── identity ──────────────────────────────────────────────────────────────
def test_identity_payload_copies_contract_fields():
    from prism.model_library.contracts import ModelRequestIdentity

    identity = ModelRequestIdentity(project_session='s', open_epoch=1, model_id='m',
                                    request_id='r', source_revision=2,
                                    appearance_revision=3, camera_revision=4)
    payload = wire.identity_payload(identity)
    assert set(payload) == set(wire.IDENTITY_KEYS)
    assert payload['camera_revision'] == 4
    assert payload['protocol_version'] == identity.protocol_version


def test_identity_mismatch_names_the_field():
    sent = _identity()
    assert wire.identity_mismatch(sent, dict(sent)) is None
    assert wire.identity_mismatch(sent, _identity(request_id='other')) == 'request_id'
    assert wire.identity_mismatch(sent, _identity(appearance_revision=99)) == \
        'appearance_revision'
    assert wire.identity_mismatch(sent, None) == 'identity'


# ── results ───────────────────────────────────────────────────────────────
def _result(**overrides):
    frame = wire.message(wire.RESULT, job_id='j-1', state=wire.STATE_COMPLETED,
                         identity=_identity())
    frame.update(overrides)
    return frame


def test_check_result_accepts_matching_frame():
    state, code, message = wire.check_result(_result(), expected_job='j-1',
                                             expected_identity=_identity())
    assert state == wire.STATE_COMPLETED and code == '' and message == ''


def test_check_result_rejects_other_job():
    with pytest.raises(wire.ProtocolError):
        wire.check_result(_result(), expected_job='j-2', expected_identity=_identity())


def test_check_result_rejects_stale_identity():
    with pytest.raises(wire.ProtocolError):
        wire.check_result(_result(), expected_job='j-1',
                          expected_identity=_identity(source_revision=999))


def test_check_result_rejects_unknown_state():
    with pytest.raises(wire.ProtocolError):
        wire.check_result(_result(state='almost-done'), expected_job='j-1',
                          expected_identity=_identity())


def test_check_result_reports_failure_details():
    frame = _result(state=wire.STATE_FAILED, error_code=wire.ERR_TIMEOUT,
                    error_message='超时')
    state, code, message = wire.check_result(frame, expected_job='j-1',
                                             expected_identity=_identity())
    assert (state, code, message) == (wire.STATE_FAILED, wire.ERR_TIMEOUT, '超时')


def test_hashes_agree_needs_size_and_digest():
    assert wire.hashes_agree(10, 'ABC', {'bytes': 10, 'sha256': 'abc'})
    assert not wire.hashes_agree(10, 'abc', {'bytes': 11, 'sha256': 'abc'})
    assert not wire.hashes_agree(10, 'abc', {'bytes': 10, 'sha256': 'def'})
    assert not wire.hashes_agree(10, 'abc', {'bytes': 10})


# ── handshake ─────────────────────────────────────────────────────────────
def _handshake(**overrides):
    base = dict(worker=wire.WORKER_NAME_RENDER, backend_version='4.0.0',
                vtk_version='9.7.1', python_version='3.12.8', pid=4242,
                formats={'.fbx': 'FBX', '.obj': 'OBJ', '.glb': 'GLB', '.gltf': 'GLTF'},
                readers=('FBX', 'OBJ', 'GLB', 'GLTF'), wireframe=False,
                material_edges=True, appearance=True, statistics='f3d-loaded')
    base.update(overrides)
    return wire.Handshake(**base)


def test_handshake_round_trips_through_json():
    original = _handshake()
    restored = wire.Handshake.from_payload(json.loads(json.dumps(original.to_payload())))
    assert restored == original


def test_handshake_accepts_a_complete_helper():
    assert _handshake().problems(wire.WORKER_NAME_RENDER) == ()


def test_handshake_reports_a_missing_reader():
    problems = _handshake(formats={'.obj': 'OBJ'}).problems(wire.WORKER_NAME_RENDER)
    assert any('缺少格式读取器' in problem for problem in problems)


def test_handshake_reports_wrong_helper_and_version():
    problems = _handshake(worker='other', protocol_version=99,
                          backend_version='').problems(wire.WORKER_NAME_RENDER)
    assert any('辅助进程不匹配' in problem for problem in problems)
    assert any('协议版本不匹配' in problem for problem in problems)
    assert any('后端版本' in problem for problem in problems)


def test_handshake_rejects_malformed_payload():
    with pytest.raises(wire.ProtocolError):
        wire.Handshake.from_payload({'protocol_version': 'x'})
    with pytest.raises(wire.ProtocolError):
        wire.Handshake.from_payload({'formats': 'obj'})


def test_handshake_defaults_are_conservative():
    handshake = wire.Handshake.from_payload({'worker': wire.WORKER_NAME_RENDER})
    assert handshake.wireframe is False
    assert handshake.material_edges is False
    assert handshake.statistics == 'none'


def test_describe_formats_covers_all_supported_formats():
    text = wire.describe_formats({'.obj': 'OBJ'})
    for extension in wire.SUPPORTED_FORMATS:
        assert extension.lstrip('.') in text
