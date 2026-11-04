"""Catalog of an FBX source, compared against hand-written expectations.

Two containers are covered because two shapes exist in the wild: a binary 7.4
container (written byte by byte here, so the expectation is independent of the
reader) and an ASCII container.  The facts under test are the ones a viewer
usually gets wrong:

* which material a **polygon** uses (``LayerElementMaterial`` integer array),
* that the same geometry used by two models is two usage points,
* that a texture is attached to a *named* material property, so ``Bump`` stays a
  bump and never becomes a normal map,
* that embedded base64 media is hashed from its decoded bytes.
"""
import base64
import hashlib
import json
import struct

import pytest

from prism.model_library.multimaterial.catalog import (
    CatalogParseError,
    definitions_by_id,
    texture_uses,
    unused_definitions,
    usage_counts,
)
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.inspectors.fbx import PARSER_ID_FBX

MAGIC = b'Kaydara FBX Binary  \x00'
TEXTURE = b'\x89PNG\r\n\x1a\nfbxtexture'

GEOMETRY = 1001
CHAIR = 2001
LEG_A = 2002
CHAIR_B = 2003
LEG_B = 2004
WOOD = 3001
GLASS = 3002
WOOD_TEXTURE = 4001
BUMP_TEXTURE = 4002
WOOD_VIDEO = 5001


# ---------------------------------------------------------------------------
# an independent binary writer (the reader under test is the only reader used)
# ---------------------------------------------------------------------------
def _property(value) -> bytes:
    if isinstance(value, bool):
        return b'C' + bytes([1 if value else 0])
    if isinstance(value, int):
        return b'L' + struct.pack('<q', value)
    if isinstance(value, float):
        return b'D' + struct.pack('<d', value)
    if isinstance(value, str):
        data = value.encode('utf-8')
        return b'S' + struct.pack('<I', len(data)) + data
    if isinstance(value, tuple) and value[0] == 'array':
        kind, items = value[1], tuple(value[2])
        width = {'i': ('i', 4), 'l': ('q', 8), 'd': ('d', 8)}[kind]
        payload = struct.pack('<' + width[0] * len(items), *items)
        return (kind.encode('ascii')
                + struct.pack('<III', len(items), 0, 0) + payload)
    raise TypeError(f'cannot encode {value!r}')


def _record(name, properties, children, start) -> tuple:
    name_bytes = name.encode('utf-8')
    property_bytes = b''.join(_property(item) for item in properties)
    body_start = start + 13
    cursor = body_start + len(name_bytes) + len(property_bytes)
    child_bytes = b''
    for child in children:
        chunk, cursor = _record(child[0], child[1], child[2], cursor)
        child_bytes += chunk
    terminator = b'\x00' * 13 if children else b''
    end = cursor + len(terminator)
    header = (struct.pack('<III', end, len(properties), len(property_bytes))
              + bytes([len(name_bytes)]))
    record = header + name_bytes + property_bytes + child_bytes + terminator
    assert len(record) == end - start, (name, len(record), end - start)
    return record, end


def build_fbx(records) -> bytes:
    """A binary FBX 7.4 container carrying ``records``."""
    header = MAGIC + b'\x1a\x00' + struct.pack('<I', 7400)
    body = b''
    offset = len(header)
    for record in records:
        chunk, offset = _record(record[0], record[1], record[2], offset)
        body += chunk
    return header + body + b'\x00' * 13


def test_binary_7300_encoded_material_indices_are_not_dropped(tmp_path,monkeypatch):
    import zlib
    original=_property
    def compressed_property(value):
        if isinstance(value,tuple) and value[0]=='array':
            raw=struct.pack('<'+'i'*len(value[2]),*value[2]);packed=zlib.compress(raw)
            return b'i'+struct.pack('<III',len(value[2]),1,len(packed))+packed
        return original(value)
    monkeypatch.setitem(globals(),'_property',compressed_property)
    data=bytearray(build_fbx(scene_records()))
    struct.pack_into('<I',data,23,7300)
    path=tmp_path/'encoded-7300.fbx';path.write_bytes(data)
    catalog=inspect_source(path)
    assert len(catalog.binding_sites)==4
    assert {b.original_material_id for b in catalog.binding_sites}=={f'fbx:material:{WOOD}',f'fbx:material:{GLASS}'}


def scene_records(*, materials_on_model=(WOOD, GLASS), by_polygon=(0, 1, 0, 1),
                  embedded=False, uv_set='UVChannel_1'):
    texture_properties = [uv_set] if uv_set else []
    video_children = [] if embedded else [('RelativeFilename', ['wood.png'], [])]
    if embedded:
        video_children = [('Content', [base64.b64encode(TEXTURE).decode('ascii')], [])]
    return [
        ['Objects', [], [
            ['Geometry', [GEOMETRY, 'Cube\x00\x01Geometry', 'Mesh'], [
                ['LayerElementMaterial', [0], [
                    ['MappingInformationType', ['ByPolygon'], []],
                    ['ReferenceInformationType', ['IndexToDirect'], []],
                    ['Materials', [('array', 'i', by_polygon)], []],
                ]],
            ]],
            ['Material', [WOOD, 'wood\x00\x01Material', ''], [
                ['ShadingModel', ['phong'], []],
                ['Properties70', [], [
                    ['P', ['DiffuseColor', 'Color', '', 'A', 0.8, 0.6, 0.4], []],
                    ['P', ['Opacity', 'double', 'Number', '', 1.0], []],
                    ['P', ['ShininessExponent', 'Number', '', 'A', 62.5], []],
                ]],
            ]],
            ['Material', [GLASS, 'glass\x00\x01Material', ''], [
                ['ShadingModel', ['lambert'], []],
            ]],
            ['Texture', [WOOD_TEXTURE, 'wood_tex\x00\x01Texture', ''],
             [['UVSet', texture_properties, []]] if texture_properties else []],
            ['Texture', [BUMP_TEXTURE, 'bump_tex\x00\x01Texture', ''], []],
            ['Video', [WOOD_VIDEO, 'wood.png\x00\x01Video', 'Clip'], video_children],
            ['Model', [CHAIR, 'Chair\x00\x01Model', 'Mesh'], []],
            ['Model', [LEG_A, 'LegA\x00\x01Model', 'Null'], []],
            ['Model', [CHAIR_B, 'Chair2\x00\x01Model', 'Mesh'], []],
            ['Model', [LEG_B, 'LegB\x00\x01Model', 'Null'], []],
        ]],
        ['Connections', [], [
            ['C', ['OO', GEOMETRY, CHAIR], []],
            ['C', ['OO', WOOD, CHAIR], []],
            ['C', ['OO', GLASS, CHAIR], []],
            ['C', ['OO', CHAIR, LEG_A], []],
            ['C', ['OO', GEOMETRY, CHAIR_B], []],
            ['C', ['OO', WOOD, CHAIR_B], []],
            ['C', ['OO', GLASS, CHAIR_B], []],
            ['C', ['OO', CHAIR_B, LEG_B], []],
            ['C', ['OP', WOOD_TEXTURE, WOOD, 'DiffuseColor'], []],
            ['C', ['OP', BUMP_TEXTURE, WOOD, 'Bump'], []],
            ['C', ['OO', WOOD_TEXTURE, WOOD_VIDEO], []],
            ['C', ['OO', BUMP_TEXTURE, WOOD_VIDEO], []],
        ]],
    ]


@pytest.fixture()
def fbx_file(tmp_path):
    (tmp_path / 'wood.png').write_bytes(TEXTURE)
    path = tmp_path / 'chair.fbx'
    path.write_bytes(build_fbx(scene_records()))
    return path


# ---------------------------------------------------------------------------
# binary container
# ---------------------------------------------------------------------------
def test_fbx_binary_definitions_come_from_objects_and_shading_model(fbx_file):
    catalog = inspect_source(fbx_file)

    assert catalog.parser_id == PARSER_ID_FBX
    assert json.loads(catalog.unknown_json)['fbx_version'] == '7400'
    by_id = definitions_by_id(catalog)
    assert sorted(by_id) == [f'fbx:material:{WOOD}', f'fbx:material:{GLASS}']
    assert by_id[f'fbx:material:{WOOD}'].name == 'wood'
    assert by_id[f'fbx:material:{WOOD}'].workflow == 'fbx_phong'
    assert by_id[f'fbx:material:{GLASS}'].workflow == 'fbx_lambert'
    properties = json.loads(by_id[f'fbx:material:{WOOD}'].properties_json)
    assert properties['basecolor_factor'] == [0.8, 0.6, 0.4]
    assert properties['opacity'] == [1.0]
    assert properties['specular_exponent'] == [62.5]


def test_fbx_texture_is_attached_through_its_property_name(fbx_file):
    catalog = inspect_source(fbx_file)
    wood = definitions_by_id(catalog)[f'fbx:material:{WOOD}']
    uses = texture_uses(wood)

    # The Bump texture keeps its own role: it is neither dropped nor turned
    # into a normal map.
    assert sorted(uses) == ['basecolor', 'bump']
    assert uses['basecolor'].resource_id == 'sha256:' + hashlib.sha256(TEXTURE).hexdigest()
    assert uses['basecolor'].channels == 'RGB'
    assert uses['basecolor'].color_space == 'srgb'
    assert uses['basecolor'].texcoord == 1          # UVSet "UVChannel_1"
    assert any('Bump' in item for item in wood.unsupported)
    assert all(use.slot != 'normal' for use in wood.textures)


def test_fbx_polygon_material_indices_become_separate_usage_points(fbx_file):
    catalog = inspect_source(fbx_file)

    # Two models use one geometry; each gets its own instance path, and the
    # per-polygon array (0,1,0,1) yields one usage point per material index.
    paths = sorted(item.node_path for item in catalog.binding_sites)
    assert paths == ['LegA/Chair', 'LegA/Chair', 'LegB/Chair2', 'LegB/Chair2']
    by_index = {}
    for item in catalog.binding_sites:
        by_index.setdefault((item.node_path, item.primitive_key),
                            item.original_material_id)
    assert by_index[('LegA/Chair', 'fbx|layer:0|material_index:0')] == f'fbx:material:{WOOD}'
    assert by_index[('LegA/Chair', 'fbx|layer:0|material_index:1')] == f'fbx:material:{GLASS}'
    counts = json.loads(catalog.unknown_json)['face_counts']
    assert sorted(counts.values()) == [2, 2, 2, 2]
    # One wood material, two usage points: the count is places, not polygons.
    assert usage_counts(catalog) == {f'fbx:material:{WOOD}': 2,
                                     f'fbx:material:{GLASS}': 2}
    assert unused_definitions(catalog) == ()


def test_fbx_embedded_media_is_hashed_from_its_decoded_bytes(tmp_path):
    path = tmp_path / 'embedded.fbx'
    path.write_bytes(build_fbx(scene_records(embedded=True)))
    catalog = inspect_source(path)
    wood = definitions_by_id(catalog)[f'fbx:material:{WOOD}']
    use = texture_uses(wood)['basecolor']

    # No sibling file exists, so the embedded base64 payload is decoded first.
    assert use.resource_id == 'sha256:' + hashlib.sha256(TEXTURE).hexdigest()
    assert use.source_locator.startswith('fbx:embedded:')
    assert not (tmp_path / 'wood.png').exists()


def test_fbx_all_same_mapping_states_one_usage_point(tmp_path):
    records = scene_records(by_polygon=(0, 0, 0, 0))
    # Rewrite the layer mapping to AllSame for this fixture.
    objects = records[0][2]
    geometry = objects[0]
    layer = geometry[2][0]
    layer[2][0] = ('MappingInformationType', ['AllSame'], [])
    path = tmp_path / 'allsame.fbx'
    path.write_bytes(build_fbx(records))
    catalog = inspect_source(path)

    indices = {item.primitive_key for item in catalog.binding_sites}
    assert indices == {'fbx|layer:0|material_index:0'}
    assert usage_counts(catalog) == {f'fbx:material:{WOOD}': 2}


def test_fbx_geometry_without_material_is_reported(tmp_path):
    records = scene_records()
    # Drop the material links so the geometry stays unassigned.
    records[1][2] = [item for item in records[1][2]
                     if not (item[0] == 'C' and item[1][0] == 'OO'
                             and item[1][1] in (WOOD, GLASS))]
    path = tmp_path / 'unassigned.fbx'
    path.write_bytes(build_fbx(records))
    catalog = inspect_source(path)

    # The definitions are still declared; what is missing is the assignment.
    assert len(catalog.definitions) == 2
    assert catalog.binding_sites
    assert all(item.original_material_id is None for item in catalog.binding_sites)
    assert any('没有连接任何材质' in note for note in catalog.unresolved)


def test_unreadable_fbx_fails_loudly(tmp_path):
    truncated = tmp_path / 'truncated.fbx'
    truncated.write_bytes(MAGIC + b'\x1a\x00' + struct.pack('<I', 7400))
    with pytest.raises(CatalogParseError):
        inspect_source(truncated)

    short = tmp_path / 'short.fbx'
    short.write_bytes(b'Kaydara')
    with pytest.raises(CatalogParseError):
        inspect_source(short)


# ---------------------------------------------------------------------------
# ASCII container
# ---------------------------------------------------------------------------
ASCII_SCENE = """\
; handmade ASCII fixture
FBXHeaderExtension:  {
\tFBXHeaderVersion: 1003
}
Objects:  {
\tGeometry: 1001, "Geometry::Cube", "Mesh" {
\t\tLayerElementMaterial: 0 {
\t\t\tMappingInformationType: "ByPolygon"
\t\t\tReferenceInformationType: "IndexToDirect"
\t\t\tMaterials: *4 {
\t\t\t\ta: 0,1,1,0
\t\t\t}
\t\t}
\t}
\tMaterial: 3001, "Material::stone", "" {
\t\tShadingModel: "phong"
\t\tProperties70:  {
\t\t\tP: "DiffuseColor", "Color", "", "A",0.25,0.5,0.75
\t\t\tP: "ShininessExponent", "Number", "", "A",12
\t\t}
\t}
\tMaterial: 3002, "Material::spare", "" {
\t\tShadingModel: "lambert"
\t}
\tModel: 2001, "Model::Table", "Mesh" {
\t}
}
Connections:  {
\tC: "OO",1001,2001
\tC: "OO",3001,2001
\tC: "OO",3002,2001
}
"""


def test_ascii_container_is_read_with_the_same_rules(tmp_path):
    path = tmp_path / 'table.fbx'
    path.write_text(ASCII_SCENE, encoding='utf-8')
    catalog = inspect_source(path)

    assert json.loads(catalog.unknown_json)['fbx_version'] == 'ascii'
    by_id = definitions_by_id(catalog)
    assert sorted(by_id) == ['fbx:material:3001', 'fbx:material:3002']
    stone = by_id['fbx:material:3001']
    assert stone.name == 'stone'
    assert stone.workflow == 'fbx_phong'
    properties = json.loads(stone.properties_json)
    assert properties['basecolor_factor'] == [0.25, 0.5, 0.75]
    assert properties['specular_exponent'] == [12.0]

    # The integer array (0,1,1,0) yields two usage points on one model.
    assert sorted(item.primitive_key for item in catalog.binding_sites) == [
        'fbx|layer:0|material_index:0', 'fbx|layer:0|material_index:1']
    paths = {item.node_path for item in catalog.binding_sites}
    assert paths == {'Table'}
    counts = json.loads(catalog.unknown_json)['face_counts']
    assert sorted(counts.values()) == [2, 2]
    # "spare" is declared and assigned, so it is used; nothing is unused here.
    assert unused_definitions(catalog) == ()


def test_ascii_definition_without_assignment_is_listed_as_unused(tmp_path):
    text = ASCII_SCENE.replace('\tC: "OO",3002,2001\n', '')
    path = tmp_path / 'table2.fbx'
    path.write_text(text, encoding='utf-8')
    catalog = inspect_source(path)

    assert unused_definitions(catalog) == ('fbx:material:3002',)
