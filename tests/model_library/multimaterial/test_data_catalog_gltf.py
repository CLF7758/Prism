"""Catalog of a glTF/GLB source, compared against hand-written expectations.

The expectation in every test is written from the fixture's own JSON, not
produced by the reader under test: a value the reader also computes would prove
nothing.  Each test names the fact it proves in its docstring.
"""
import base64
import hashlib
import json
import struct

import pytest

from prism.model_library.multimaterial.catalog import (
    CatalogParseError,
    definitions_by_id,
    embedded_resources,
    texture_uses,
    unresolved_resources,
    unused_definitions,
    usage_counts,
)
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.inspectors.gltf import (
    PARSER_ID_GLTF,
    PARSER_ID_GLB,
)

PNG_BYTES = bytes.fromhex(
    '89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4'
    '890000000d49444154789c6360000002000155a2a5f40000000049454e44ae426082')
OTHER_PNG = PNG_BYTES + b'\x00'


def _write(path, document):
    path.write_text(json.dumps(document, indent=1), encoding='utf-8')
    return path


def _two_material_scene(tmp_path):
    """Four chair legs (two nodes) sharing one mesh, plus one unused material."""
    (tmp_path / 'wood.png').write_bytes(PNG_BYTES)
    (tmp_path / 'normal.png').write_bytes(OTHER_PNG)
    document = {
        'asset': {'version': '2.0', 'generator': 'handmade-fixture'},
        'scenes': [{'name': 'Chair', 'nodes': [3, 4]}],
        'nodes': [
            {'name': 'Leg', 'mesh': 0},
            {'name': 'Root'},
            {  # node 2 is not in the scene: it must not become a usage point
                'name': 'Orphan', 'mesh': 0},
            {'name': 'LeftFront', 'mesh': 0},
            {'name': 'LeftBack', 'mesh': 1},
        ],
        'meshes': [
            {'name': 'legMesh', 'primitives': [{'attributes': {}, 'material': 0}]},
            {'name': 'seatMesh', 'primitives': [
                {'attributes': {}, 'material': 1},
                {'attributes': {}},  # second primitive states no material
            ]},
        ],
        'materials': [
            {
                'name': 'wood',
                'pbrMetallicRoughness': {
                    'baseColorTexture': {'index': 0},
                    'baseColorFactor': [0.8, 0.6, 0.4, 1.0],
                    'metallicFactor': 0.0,
                    'roughnessFactor': 0.65,
                },
                'normalTexture': {'index': 1, 'scale': 0.75},
            },
            {
                'name': 'glass',
                'pbrMetallicRoughness': {'roughnessFactor': 0.1},
                'alphaMode': 'BLEND',
                'alphaCutoff': 0.5,
                'doubleSided': True,
            },
            {'name': 'unused', 'emissiveFactor': [1, 0, 0]},
        ],
        'textures': [
            {'source': 0, 'sampler': 0},
            {'source': 1, 'sampler': 0,
             'extensions': {}},
        ],
        'samplers': [{'magFilter': 9729, 'minFilter': 9987, 'wrapS': 33071}],
        'images': [
            {'uri': 'wood.png'},
            {'uri': 'normal.png'},
        ],
    }
    return _write(tmp_path / 'chair.gltf', document)


def test_gltf_reports_every_definition_with_its_source_locator(tmp_path):
    path = _two_material_scene(tmp_path)
    catalog = inspect_source(path)

    assert catalog.parser_id == PARSER_ID_GLTF
    assert catalog.schema_version == 1
    assert [item.material_id for item in catalog.definitions] == [
        'gltf:material:0', 'gltf:material:1', 'gltf:material:2']
    assert [item.name for item in catalog.definitions] == ['wood', 'glass', 'unused']
    assert [item.source_key for item in catalog.definitions] == [
        'materials[0]', 'materials[1]', 'materials[2]']
    assert catalog.definitions[0].workflow == 'gltf_pbr_metallic_roughness'
    assert catalog.source_digest.startswith('sha256:')


def test_gltf_texture_uses_carry_channel_colour_space_and_instance_count(tmp_path):
    path = _two_material_scene(tmp_path)
    catalog = inspect_source(path)
    wood = definitions_by_id(catalog)['gltf:material:0']
    uses = texture_uses(wood)

    assert sorted(uses) == ['basecolor', 'normal']
    assert uses['basecolor'].channels == 'RGB'
    assert uses['basecolor'].color_space == 'srgb'
    assert uses['basecolor'].texcoord == 0
    assert uses['basecolor'].resource_id == 'sha256:' + hashlib.sha256(PNG_BYTES).hexdigest()
    assert uses['normal'].channels == 'RGB'
    assert uses['normal'].color_space == 'linear'
    assert uses['normal'].normal_convention == 'opengl'
    # The sampler is retained verbatim, because it changes what the image means.
    sampler = json.loads(uses['basecolor'].unknown_json)['sampler']
    assert sampler == {'magFilter': 9729, 'minFilter': 9987, 'wrapS': 33071}


def test_gltf_binding_sites_are_per_instance_and_per_primitive(tmp_path):
    path = _two_material_scene(tmp_path)
    catalog = inspect_source(path)

    # Only nodes reachable from a scene become usage points: node 2 ("Orphan")
    # is in no scene, so it is not a place the material is used.
    paths = sorted(item.node_path for item in catalog.binding_sites)
    assert paths == ['Chair/LeftBack', 'Chair/LeftBack', 'Chair/LeftFront']
    front = [item for item in catalog.binding_sites if item.node_path == 'Chair/LeftFront']
    assert [item.original_material_id for item in front] == ['gltf:material:0']
    seat = [item for item in catalog.binding_sites if item.node_path == 'Chair/LeftBack']
    # Mesh 1 has two primitives: one names a material, one states none.
    assert [item.original_material_id for item in seat] == ['gltf:material:1', None]
    assert sorted(item.primitive_key for item in seat) == ['primitive:0', 'primitive:1']
    assert len({item.binding_id for item in catalog.binding_sites}) == 3


def test_gltf_shared_material_counts_usage_points_not_vertices(tmp_path):
    path = _two_material_scene(tmp_path)
    catalog = inspect_source(path)

    counts = usage_counts(catalog)
    # One wood material used by one instance, glass by one primitive, and the
    # primitive without a material is not counted as a material usage.
    assert counts == {'gltf:material:0': 1, 'gltf:material:1': 1}
    assert unused_definitions(catalog) == ('gltf:material:2',)


def test_gltf_keeps_numeric_only_material_and_makes_absence_explicit(tmp_path):
    path = _two_material_scene(tmp_path)
    catalog = inspect_source(path)
    glass = definitions_by_id(catalog)['gltf:material:1']
    properties = json.loads(glass.properties_json)

    # Only stated fields are numbers; nothing is filled in with a default that
    # would be indistinguishable from the author's own zero.
    assert properties['roughness_factor'] == 0.1
    assert 'metallic_factor' not in properties
    assert properties['alpha_mode'] == 'BLEND'
    assert properties['double_sided'] is True
    assert properties['present'] == ['alphaCutoff', 'alphaMode',
                                     'doubleSided', 'pbrMetallicRoughness.roughnessFactor']
    assert glass.textures == ()
    assert glass.unsupported == ()


def test_gltf_missing_and_embedded_images_are_reported_not_hidden(tmp_path):
    (tmp_path / 'normal.png').write_bytes(OTHER_PNG)
    document = {
        'asset': {'version': '2.0'},
        'materials': [{
            'name': 'mixed',
            'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}},
            'emissiveTexture': {'index': 1},
        }],
        'textures': [{'source': 0}, {'source': 1}],
        'images': [
            {'uri': 'missing.png'},
            {'uri': 'data:image/png;base64,'
                    + base64.b64encode(PNG_BYTES).decode('ascii')},
        ],
    }
    path = _write(tmp_path / 'mixed.gltf', document)
    catalog = inspect_source(path)
    material = definitions_by_id(catalog)['gltf:material:0']
    uses = texture_uses(material)

    assert uses['basecolor'].resource_id == ''
    assert uses['basecolor'].source_locator == 'missing.png'
    assert unresolved_resources(catalog) == (('basecolor', 'missing.png'),)
    assert uses['emissive'].resource_id == (
        'sha256:' + hashlib.sha256(PNG_BYTES).hexdigest())
    assert embedded_resources(catalog) == (('emissive', uses['emissive'].source_locator),)
    assert any('missing.png' in note for note in catalog.unresolved)


def test_gltf_texture_transform_and_unknown_extension_are_recorded(tmp_path):
    (tmp_path / 'wood.png').write_bytes(PNG_BYTES)
    document = {
        'asset': {'version': '2.0'},
        'extensionsUsed': ['KHR_texture_transform'],
        'materials': [{
            'name': 'tiled',
            'pbrMetallicRoughness': {
                'baseColorTexture': {
                    'index': 0, 'texCoord': 1,
                    'extensions': {'KHR_texture_transform': {
                        'offset': [0.25, 0.5], 'rotation': 1.5,
                        'scale': [2.0, 3.0], 'texCoord': 1}},
                },
            },
            'extensions': {'KHR_materials_clearcoat': {'clearcoatFactor': 1.0}},
        }],
        'textures': [{'source': 0}],
        'images': [{'uri': 'wood.png'}],
    }
    path = _write(tmp_path / 'tiled.gltf', document)
    catalog = inspect_source(path)
    material = definitions_by_id(catalog)['gltf:material:0']
    use = texture_uses(material)['basecolor']

    assert use.texcoord == 1
    assert use.uv_transform == (0.25, 0.5, 1.5, 2.0, 3.0)
    assert any('clearcoat' in item for item in material.unsupported)


def test_glb_hashes_images_from_the_binary_chunk(tmp_path):
    payload = {'asset': {'version': '2.0'},
               'materials': [{'name': 'inline',
                              'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}}}],
               'textures': [{'source': 0}],
               'images': [{'bufferView': 0, 'mimeType': 'image/png'}],
               'bufferViews': [{'buffer': 0, 'byteOffset': 0,
                                'byteLength': len(PNG_BYTES)}],
               'buffers': [{'byteLength': len(PNG_BYTES)}]}
    document = json.dumps(payload).encode('utf-8')
    document += b' ' * ((4 - len(document) % 4) % 4)
    binary = PNG_BYTES + b'\x00' * ((4 - len(PNG_BYTES) % 4) % 4)
    total = 12 + 8 + len(document) + 8 + len(binary)
    blob = (b'glTF' + struct.pack('<I', 2) + struct.pack('<I', total)
            + struct.pack('<I', len(document)) + b'JSON' + document
            + struct.pack('<I', len(binary)) + b'BIN\x00' + binary)
    path = tmp_path / 'inline.glb'
    path.write_bytes(blob)

    catalog = inspect_source(path)
    assert catalog.parser_id == PARSER_ID_GLB
    use = texture_uses(definitions_by_id(catalog)['gltf:material:0'])['basecolor']
    assert use.resource_id == 'sha256:' + hashlib.sha256(PNG_BYTES).hexdigest()
    assert 'bufferView' in use.source_locator


def test_gltf_without_any_material_is_an_empty_catalog_not_a_failure(tmp_path):
    document = {'asset': {'version': '2.0'},
                'scenes': [{'nodes': [0]}],
                'nodes': [{'name': 'plain', 'mesh': 0}],
                'meshes': [{'primitives': [{'attributes': {}}]}]}
    path = _write(tmp_path / 'plain.gltf', document)
    catalog = inspect_source(path)

    assert catalog.definitions == ()
    assert len(catalog.binding_sites) == 1
    assert catalog.binding_sites[0].original_material_id is None


def test_invalid_documents_fail_loudly(tmp_path):
    broken = tmp_path / 'broken.gltf'
    broken.write_text('{ not json', encoding='utf-8')
    with pytest.raises(CatalogParseError):
        inspect_source(broken)

    unsupported = tmp_path / 'model.stl'
    unsupported.write_text('solid', encoding='utf-8')
    with pytest.raises(CatalogParseError):
        inspect_source(unsupported)

    with pytest.raises(CatalogParseError):
        inspect_source(tmp_path / 'absent.gltf')
