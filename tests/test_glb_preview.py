"""GLB/glTF parsing and previews.

The guiding rule is that a broken model must never take anything down: it
should surface as a :class:`GLBError`, and the canvas item built from it has
to stay usable with a placeholder tile.
"""
import base64
import json
import struct

import pytest

from prism.glb_preview import (GLBError, Mesh, is_glb_file, load_mesh,
                               parse_glb, parse_gltf, render_mesh)

GLB_MAGIC = 0x46546C67
JSON_CHUNK_TYPE = 0x4E4F534A
BIN_CHUNK_TYPE = 0x004E4942

TRIANGLE_POSITIONS = (0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
TRIANGLE_INDICES = (0, 1, 2)


def build_glb(positions=None, indices=None, **document_overrides):
    """Assemble a minimal but valid single-mesh GLB container."""
    positions = TRIANGLE_POSITIONS if positions is None else positions
    indices = TRIANGLE_INDICES if indices is None else indices

    vertex_bytes = struct.pack(f'<{len(positions)}f', *positions)
    index_bytes = struct.pack(f'<{len(indices)}H', *indices)
    while len(index_bytes) % 4:
        index_bytes += b'\x00'
    blob = vertex_bytes + index_bytes

    document = {
        'asset': {'version': '2.0'},
        'buffers': [{'byteLength': len(blob)}],
        'bufferViews': [
            {'buffer': 0, 'byteOffset': 0,
             'byteLength': len(vertex_bytes)},
            {'buffer': 0, 'byteOffset': len(vertex_bytes),
             'byteLength': len(index_bytes)},
        ],
        'accessors': [
            {'bufferView': 0, 'componentType': 5126,
             'count': len(positions) // 3, 'type': 'VEC3'},
            {'bufferView': 1, 'componentType': 5123,
             'count': len(indices), 'type': 'SCALAR'},
        ],
        'meshes': [{'primitives': [
            {'attributes': {'POSITION': 0}, 'indices': 1}]}],
    }
    document.update(document_overrides)

    json_bytes = json.dumps(document).encode('utf-8')
    while len(json_bytes) % 4:
        json_bytes += b' '
    total = 12 + 8 + len(json_bytes) + 8 + len(blob)

    out = struct.pack('<III', GLB_MAGIC, 2, total)
    out += struct.pack('<II', len(json_bytes), JSON_CHUNK_TYPE) + json_bytes
    out += struct.pack('<II', len(blob), BIN_CHUNK_TYPE) + blob
    return out


def as_standalone_gltf(**document_overrides):
    """Return the sample geometry as a standalone ``.gltf`` document.

    A real ``.gltf`` carries its buffer outside the container, so the
    sample's binary chunk is turned into a base64 data URI here.
    """
    container = build_glb(**document_overrides)
    json_length = struct.unpack_from('<I', container, 12)[0]
    document = json.loads(container[20:20 + json_length])
    binary = container[20 + json_length + 8:]
    document['buffers'] = [{
        'byteLength': len(binary),
        'uri': 'data:application/octet-stream;base64,'
               + base64.b64encode(binary).decode('ascii'),
    }]
    return document


# ── Extension detection ──────────────────────────────────────────────


def test_is_glb_file_recognises_models():
    assert is_glb_file('scene.glb')
    assert is_glb_file('SCENE.GLTF')
    assert not is_glb_file('photo.png')
    assert not is_glb_file('clip.mp4')


# ── Successful parsing ───────────────────────────────────────────────


def test_parse_minimal_glb():
    mesh = parse_glb(build_glb())
    assert mesh.vertex_count == 3
    assert mesh.triangle_count == 1
    assert list(mesh.indices) == [0, 1, 2]


def test_parsed_mesh_reports_bounds_and_center():
    mesh = parse_glb(build_glb())
    bounds = mesh.bounds()
    assert bounds[0] == pytest.approx(0.0)
    assert bounds[3] == pytest.approx(1.0)
    assert bounds[4] == pytest.approx(1.0)
    assert mesh.center()[0] == pytest.approx(0.5)
    assert mesh.radius() > 0


def test_parse_handles_geometry_in_a_json_gltf_document():
    """The same geometry parses from a standalone .gltf document."""
    document = as_standalone_gltf()
    mesh = parse_gltf(json.dumps(document).encode('utf-8'))
    assert mesh.triangle_count == 1


def test_out_of_range_indices_are_clamped():
    mesh = parse_glb(build_glb(indices=(0, 1, 42)))
    assert mesh.triangle_count == 1
    assert all(0 <= index < mesh.vertex_count for index in mesh.indices)


def test_non_triangle_primitives_are_skipped():
    document = as_standalone_gltf()
    document['meshes'][0]['primitives'][0]['mode'] = 0     # POINTS
    with pytest.raises(GLBError):
        parse_gltf(json.dumps(document).encode('utf-8'))


# ── Broken input must raise, never crash ─────────────────────────────


@pytest.mark.parametrize('payload, reason', [
    (b'', 'empty file'),
    (b'glTF', 'shorter than a header'),
    (b'\x00' * 12, 'wrong magic'),
    (struct.pack('<III', GLB_MAGIC, 1, 12), 'unsupported version'),
    (struct.pack('<III', GLB_MAGIC, 2, 12), 'no chunks at all'),
    (struct.pack('<III', GLB_MAGIC, 2, 0xFFFFFF), 'declared length lies'),
])
def test_broken_containers_raise_glb_error(payload, reason):
    with pytest.raises(GLBError):
        parse_glb(payload)


def test_truncated_container_raises():
    with pytest.raises(GLBError):
        parse_glb(build_glb()[:20])


def test_invalid_json_chunk_raises():
    payload = struct.pack('<III', GLB_MAGIC, 2, 12 + 8 + 5)
    payload += struct.pack('<II', 5, JSON_CHUNK_TYPE) + b'{oops'
    with pytest.raises(GLBError):
        parse_glb(payload)


def test_dangling_accessor_raises():
    payload = build_glb(meshes=[{'primitives': [
        {'attributes': {'POSITION': 99}}]}])
    with pytest.raises(GLBError):
        parse_glb(payload)


def test_document_without_meshes_raises():
    with pytest.raises(GLBError):
        parse_glb(build_glb(meshes=[]))


def test_document_without_buffers_raises():
    with pytest.raises(GLBError):
        parse_glb(build_glb(buffers=[]))


def test_load_mesh_rejects_missing_file(tmp_path):
    with pytest.raises(GLBError):
        load_mesh(str(tmp_path / 'nope.glb'))


def test_load_mesh_rejects_other_extensions(tmp_path):
    path = tmp_path / 'photo.png'
    path.write_bytes(b'\x89PNG')
    with pytest.raises(GLBError):
        load_mesh(str(path))


def test_load_mesh_reads_a_real_file(tmp_path):
    path = tmp_path / 'triangle.glb'
    path.write_bytes(build_glb())
    mesh = load_mesh(str(path))
    assert mesh.triangle_count == 1


# ── Software rendering ───────────────────────────────────────────────


def test_render_mesh_returns_the_requested_size(qapp):
    image = render_mesh(parse_glb(build_glb()), (64, 48))
    assert image.width() == 64
    assert image.height() == 48
    assert not image.isNull()


def test_render_mesh_accepts_a_qsize(qapp):
    from PyQt6 import QtCore
    image = render_mesh(parse_glb(build_glb()), QtCore.QSize(32, 32))
    assert image.width() == 32


def test_render_mesh_survives_missing_mesh(qapp):
    assert not render_mesh(None, (32, 32)).isNull()


def test_render_mesh_survives_empty_mesh(qapp):
    assert not render_mesh(Mesh([], []), (32, 32)).isNull()


def test_render_mesh_survives_degenerate_geometry(qapp):
    # Every vertex identical: no triangle has a usable normal.
    mesh = parse_glb(build_glb(positions=(1.0, 1.0, 1.0) * 3))
    assert not render_mesh(mesh, (32, 32)).isNull()


# ── Canvas item ──────────────────────────────────────────────────────


def test_glb_item_falls_back_to_a_placeholder(qapp):
    """A model that cannot be parsed still yields a usable canvas item."""
    from prism.items import PrismGlbItem

    item = PrismGlbItem(filename='broken.glb', glb_blob=b'not a glb at all')
    assert item.TYPE == 'glb'
    assert item._ensure_mesh() is None
    assert item._load_error
    assert not item.pixmap().isNull()


def test_glb_item_renders_a_thumbnail(qapp):
    from prism.items import PrismGlbItem

    item = PrismGlbItem(filename='triangle.glb', glb_blob=build_glb())
    assert item._ensure_mesh() is not None
    assert not item.pixmap().isNull()
    assert item.pixmap().width() > 0


def test_glb_item_is_registered_for_loading(qapp):
    from prism.items import item_registry
    assert item_registry.get('glb') is not None


def test_glb_item_save_data_for_an_embedded_model(qapp):
    from prism.items import PrismGlbItem

    item = PrismGlbItem(filename='triangle.glb', glb_blob=build_glb())
    data = item.get_extra_save_data()
    assert data['filename'] == 'triangle.glb'
    assert data['isReference'] is False
    assert 'modelPath' not in data


def test_glb_item_save_data_for_a_referenced_model(qapp, tmp_path):
    from prism.items import PrismGlbItem

    path = tmp_path / 'triangle.glb'
    path.write_bytes(build_glb())
    item = PrismGlbItem(filename=str(path), glb_path=str(path))
    item._is_reference = True
    data = item.get_extra_save_data()
    assert data['isReference'] is True
    assert data['modelPath'] == str(path)


def test_glb_item_restores_from_save_data(qapp):
    from prism.items import PrismGlbItem

    fresh = PrismGlbItem()
    restored = PrismGlbItem.create_from_data(
        item=fresh, data={'filename': 'restored.glb', 'isReference': True,
                          'modelPath': 'C:/models/restored.glb'})
    assert restored.filename == 'restored.glb'
    assert restored._is_reference is True
    assert restored._glb_path == 'C:/models/restored.glb'


def test_glb_item_export_filename_keeps_the_extension(qapp, tmp_path):
    from prism.items import PrismGlbItem

    item = PrismGlbItem(filename=str(tmp_path / 'scene.glb'))
    item.save_id = 7
    assert item.get_filename_for_export('glb') == '0007-scene.glb'
