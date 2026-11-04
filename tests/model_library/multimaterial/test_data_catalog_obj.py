"""Catalog of an OBJ/MTL source, compared against hand-written expectations.

The fixtures state facts that real files contain and viewers get wrong: two
libraries on one ``mtllib`` line, one name declared twice, a group without
``usemtl``, a ``usemtl`` naming nothing, ``bump`` next to ``norm``, ``map_Ks``
next to ``map_Kd``, a slot declared twice, and a texture file that is not there.
"""
import hashlib
import json

import pytest

from prism.model_library.multimaterial.catalog import (
    CatalogParseError,
    binding_resolutions,
    bindings_by_resolution,
    dangling_bindings,
    definitions_by_id,
    texture_uses,
    unused_definitions,
    usage_counts,
)
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.inspectors.obj import PARSER_ID_OBJ

TEXTURE = b'\x89PNG\r\n\x1a\nfaketexture'

OBJ = """\
# handmade fixture
mtllib libA.mtl libB.mtl
o chair
mtllib libA.mtl
usemtl wood
v 0 0 0
f 1 2 3
f 1 3 4
g seat
usemtl wood
f 1 2 3
g trim
usemtl metal
f 1 2 3
g leftover
f 1 2 3
g phantom
usemtl nothing
f 1 2 3
o table
usemtl shared
f 1 2 3
"""

LIB_A = """\
newmtl wood
Ka 0.1 0.1 0.1
Kd 0.8 0.6 0.4
Ks 0.2 0.2 0.2
Ns 62.5
d 0.9
illum 2
map_d -imfchan l mask.png
norm normal.png
bump bump.png
map_Ks specular.png
disp height.png
map_Kd absent.png
newmtl metal
Kd 0.5 0.5 0.5
illum 1
map_Kd -s 2 3 1 -o 0.1 0.2 metal.png
newmtl shared
Kd 0 0 1
"""

LIB_B = """\
newmtl shared
Kd 1 0 0
newmtl old wood
Tr 0.25
map_Kd \"wood texture.png\"
newmtl dup
map_Kd a.png
map_Kd b.png
"""

PRESENT_FILES = ('mask.png', 'normal.png', 'bump.png', 'specular.png',
                 'height.png', 'metal.png', 'wood texture.png', 'a.png', 'b.png')


@pytest.fixture()
def obj_fixture(tmp_path):
    (tmp_path / 'libA.mtl').write_text(LIB_A, encoding='utf-8')
    (tmp_path / 'libB.mtl').write_text(LIB_B, encoding='utf-8')
    for name in PRESENT_FILES:
        (tmp_path / name).write_bytes(TEXTURE + name.encode('ascii'))
    path = tmp_path / 'chair.obj'
    path.write_text(OBJ, encoding='utf-8')
    return path


def test_obj_definitions_are_located_by_library_and_name(obj_fixture):
    catalog = inspect_source(obj_fixture)

    assert catalog.parser_id == PARSER_ID_OBJ
    # Two declarations of one name are two definitions: the same name in one
    # library, or in two libraries, is not the same material.
    assert [item.material_id for item in catalog.definitions] == [
        'obj:mtl:libA.mtl#wood',
        'obj:mtl:libA.mtl#metal',
        'obj:mtl:libA.mtl#shared',
        'obj:mtl:libB.mtl#shared',
        'obj:mtl:libB.mtl#old wood',
        'obj:mtl:libB.mtl#dup',
    ]
    # ``shared`` is declared by both libraries: usemtl can only bind by name, so
    # it binds to the first declaration and the ambiguity is reported.
    assert any('材质名重复' in note and 'shared' in note for note in catalog.unresolved)
    assert usage_counts(catalog)['obj:mtl:libA.mtl#shared'] == 1
    assert 'obj:mtl:libB.mtl#shared' in unused_definitions(catalog)


def test_obj_bump_is_not_a_normal_map_and_specular_is_not_roughness(obj_fixture):
    catalog = inspect_source(obj_fixture)
    wood = definitions_by_id(catalog)['obj:mtl:libA.mtl#wood']
    uses = texture_uses(wood)

    # A source role Prism cannot edit keeps its own slot: bump is its own role,
    # never a normal map, and map_Ks is specular, never roughness.
    assert sorted(uses) == ['basecolor', 'bump', 'displacement', 'normal',
                            'opacity', 'specular']
    assert uses['normal'].source_locator == 'normal.png'
    assert uses['normal'].normal_convention == 'unspecified'
    assert uses['normal'].color_space == 'linear'
    assert uses['bump'].source_locator == 'bump.png'
    assert uses['specular'].source_locator == 'specular.png'
    assert uses['displacement'].channels == 'R'

    joined = ' '.join(wood.unsupported)
    assert 'bump.png' in joined and 'normal' in joined
    assert 'specular.png' in joined and '粗糙度' in joined
    assert wood.workflow == 'mtl_illum_2'
    # The map points at a file that is not in the package: no digest, no
    # pretence that the texture is fine.
    assert uses['basecolor'].resource_id == ''
    assert uses['basecolor'].color_space == 'srgb'


def test_obj_map_options_become_uv_transform_channels_and_digest(obj_fixture):
    catalog = inspect_source(obj_fixture)
    wood = definitions_by_id(catalog)['obj:mtl:libA.mtl#wood']
    metal = definitions_by_id(catalog)['obj:mtl:libA.mtl#metal']

    assert json.loads(wood.properties_json)['kd_color'] == [0.8, 0.6, 0.4]
    opacity = texture_uses(wood)['opacity']
    assert opacity.channels == 'R'          # -imfchan l -> luminance
    assert opacity.color_space == 'linear'
    assert opacity.resource_id == 'sha256:' + hashlib.sha256(
        TEXTURE + b'mask.png').hexdigest()

    basecolor = texture_uses(metal)['basecolor']
    assert basecolor.resource_id == 'sha256:' + hashlib.sha256(
        TEXTURE + b'metal.png').hexdigest()
    assert basecolor.uv_transform == (0.1, 0.2, 0.0, 2.0, 3.0)


def test_obj_usage_points_are_object_group_and_usemtl_runs(obj_fixture):
    catalog = inspect_source(obj_fixture)

    # "wood" is used by o=chair (no group) and by o=chair/g=seat: two places,
    # one definition -- the "affects two places" number the UI must show.
    counts = usage_counts(catalog)
    assert counts['obj:mtl:libA.mtl#wood'] == 2
    # "metal" is used by g=trim and by g=leftover, whose faces inherit the
    # material that was active when they were read.
    assert counts['obj:mtl:libA.mtl#metal'] == 2
    assert counts['obj:mtl:libA.mtl#shared'] == 1
    assert unused_definitions(catalog) == ('obj:mtl:libB.mtl#shared',
                                           'obj:mtl:libB.mtl#old wood',
                                           'obj:mtl:libB.mtl#dup')

    face_counts = json.loads(catalog.unknown_json)['face_counts']
    # Every face belongs to exactly one usage point, including the one whose
    # material name is dangling: the file still assigns that range of faces.
    assert sum(face_counts.values()) == 7
    seat = [item for item in catalog.binding_sites
            if item.binding_id == 'obj|object:chair|group:seat|usemtl:wood']
    assert len(seat) == 1
    assert seat[0].node_path == 'chair/seat'
    assert seat[0].original_material_id == 'obj:mtl:libA.mtl#wood'
    assert face_counts[seat[0].binding_id] == 1


def test_obj_dangling_usemtl_keeps_its_usage_point_with_a_state(obj_fixture):
    catalog = inspect_source(obj_fixture)
    binding_id = 'obj|object:chair|group:phantom|usemtl:nothing'

    # A usemtl that names nothing is a broken reference, not "no material
    # assigned".  The usage point stays (dropping it would under-report where a
    # surface expects a material) and its state is `dangling`.
    site = [item for item in catalog.binding_sites if item.binding_id == binding_id]
    assert len(site) == 1
    assert site[0].original_material_id is None
    assert site[0].source_key == 'usemtl:nothing'      # the original reference
    assert binding_resolutions(catalog)[binding_id] == 'dangling'
    assert len(bindings_by_resolution(catalog, 'dangling')) == 1
    assert len(bindings_by_resolution(catalog, 'unassigned')) == 0
    dangling = dangling_bindings(catalog)
    assert dangling[binding_id] == {
        'node_path': 'chair/phantom', 'material_name': 'nothing',
        'material_key': b'nothing'.hex(), 'faces': 1}
    assert any('nothing' in note and 'phantom' in note for note in catalog.unresolved)


def test_obj_group_without_usemtl_is_unassigned_not_dangling(tmp_path):
    (tmp_path / 'plain.obj').write_text(
        'o box\ng bare\nv 0 0 0\nf 1 2 3\n', encoding='utf-8')
    catalog = inspect_source(tmp_path / 'plain.obj')
    binding_id = 'obj|object:box|group:bare|usemtl:'

    site = [item for item in catalog.binding_sites if item.binding_id == binding_id]
    assert site and site[0].original_material_id is None
    assert binding_resolutions(catalog)[binding_id] == 'unassigned'
    assert dangling_bindings(catalog) == {}


def test_obj_duplicate_slot_is_reported_and_last_wins(obj_fixture):
    catalog = inspect_source(obj_fixture)
    dup = definitions_by_id(catalog)['obj:mtl:libB.mtl#dup']

    assert [use.source_locator for use in dup.textures] == ['b.png']
    assert any('重复声明' in note for note in catalog.unresolved)


def test_obj_missing_library_and_missing_file_are_reported(tmp_path):
    (tmp_path / 'chair.obj').write_text(
        'mtllib gone.mtl\nusemtl wood\nf 1 2 3\n', encoding='utf-8')
    catalog = inspect_source(tmp_path / 'chair.obj')

    assert catalog.definitions == ()
    assert any('gone.mtl' in note for note in catalog.unresolved)
    assert any('未定义' in note for note in catalog.unresolved)


def test_obj_without_any_material_library_still_reports_usage_points(tmp_path):
    (tmp_path / 'plain.obj').write_text(
        'o box\ng sides\nv 0 0 0\nf 1 2 3\n', encoding='utf-8')
    catalog = inspect_source(tmp_path / 'plain.obj')

    assert catalog.definitions == ()
    assert len(catalog.binding_sites) == 1
    assert catalog.binding_sites[0].original_material_id is None
    assert any('没有 mtllib' in note for note in catalog.unresolved)


def test_obj_tr_becomes_opacity_and_is_labelled(tmp_path):
    (tmp_path / 'chair.obj').write_text('mtllib only.mtl\nusemtl glass\nf 1 2 3\n',
                                        encoding='utf-8')
    (tmp_path / 'only.mtl').write_text('newmtl glass\nTr 0.25\nillum 4\n',
                                       encoding='utf-8')
    catalog = inspect_source(tmp_path / 'chair.obj')
    glass = definitions_by_id(catalog)['obj:mtl:only.mtl#glass']
    properties = json.loads(glass.properties_json)

    assert properties['opacity'] == [0.75]
    assert properties['opacity_source'] == 'Tr'
    assert glass.workflow == 'mtl_illum_4'


def test_obj_continuation_lines_are_joined(tmp_path):
    (tmp_path / 'chair.obj').write_text(
        'mtllib only.mtl\nusemtl wood\nf 1 2 \\\n 3\n', encoding='utf-8')
    (tmp_path / 'only.mtl').write_text('newmtl wood\nKd 1 0 0\n', encoding='utf-8')
    catalog = inspect_source(tmp_path / 'chair.obj')

    assert json.loads(catalog.unknown_json)['face_count'] == 1


def test_obj_missing_file_fails_loudly(tmp_path):
    with pytest.raises(CatalogParseError):
        inspect_source(tmp_path / 'nothing.obj')
