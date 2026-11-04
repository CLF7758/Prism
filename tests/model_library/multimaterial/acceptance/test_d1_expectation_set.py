"""D1: the independent expectation set, and the rules that keep it independent.

Every assertion here is about the *expectation*, not about the product.  The
point is that when the product is finally compared against this set, nobody has
to argue about whether the expected answer was derived from the implementation.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os

import pytest

from . import build_expectations
from .conftest import D1_DIR, FIXTURE_ROOT, REPO, SAMPLES_DIR


def _walk(node, path=''):
    """Yield (dotted path, value) for a nested expectation dict."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _walk(value, '%s.%s' % (path, key) if path else str(key))
    else:
        yield path, node


def test_expectation_set_builds_without_disagreement(expectations):
    """The author's declared structure and the raw bytes must agree.

    A disagreement means the reference reader -- the measuring instrument --
    is wrong, so the expectation set cannot be trusted until it is fixed.
    """
    payload, problems = expectations
    assert payload['fixtures'], '没有生成任何结构夹具'
    assert payload['upstream'], '没有纳入任何真实上游样本'
    assert problems == [], '声明与独立读取不一致：%s' % problems


def test_every_d1_requirement_has_cases(expectation_payload):
    fixtures_built = set(expectation_payload['fixtures'])
    upstream = set(expectation_payload['upstream'])
    covered = fixtures_built | upstream
    gaps = {}
    for requirement, cases in expectation_payload['coverage'].items():
        missing = [case for case in cases if case not in covered]
        if missing:
            gaps[requirement] = missing
        assert len(cases) >= 2, '%s 只有 %d 个用例' % (requirement, len(cases))
    assert gaps == {}, '覆盖缺口：%s' % gaps


def test_all_four_formats_have_real_samples(expectation_payload):
    """Each of the four formats has to be represented by a real asset.

    Synthetic structure is allowed for structures, but a format whose only
    evidence is hand-written is not evidence about that format's real files.
    """
    formats = {record['format']
               for record in expectation_payload['upstream'].values()
               if 'error' not in record}
    for required in ('gltf', 'glb', 'obj', 'fbx'):
        assert required in formats, '%s 没有真实上游样本' % required


def test_real_sample_hashes_still_match(expectation_payload):
    """A changed sample silently changes the expectation; it must not pass."""
    drift = []
    for sample_id, record in expectation_payload['upstream'].items():
        if 'error' in record:
            continue
        for relative, actual in record['files'].items():
            recorded = record['recorded_files'].get(relative)
            if recorded and actual != recorded:
                drift.append('%s/%s' % (sample_id, relative))
    assert drift == [], '样本与清单哈希不一致：%s' % drift


def test_no_expectation_comes_from_the_implementation():
    """The readers and the fixture writer must not import the product.

    This is the mechanical version of "don't use the parser under test as the
    answer key": if the module never imports ``prism``, its output cannot be a
    copy of the implementation's output.
    """
    forbidden = {'prism', 'f3d', 'vtk', 'assimp', 'trimesh'}
    offenders = {}
    for name in ('material_reference.py', 'fixtures.py', 'build_expectations.py'):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        with open(path, encoding='utf-8') as stream:
            tree = ast.parse(stream.read(), filename=name)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split('.')[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imported.add(node.module.split('.')[0])
        hit = sorted(imported & forbidden)
        if hit:
            offenders[name] = hit
    assert offenders == {}, '期望生成的依赖越界：%s' % offenders


def test_fixtures_are_reproducible(tmp_path):
    """Regenerating the fixtures must produce the same bytes.

    Without this, a "fixture changed" failure would be indistinguishable from
    "the library changed", and every hash in the evidence would be unstable.
    """
    from . import fixtures as fixtures_module
    from . import material_reference as reference
    first = fixtures_module.build(str(FIXTURE_ROOT))
    second = fixtures_module.build(str(tmp_path / 'rebuild'))
    differences = []
    for name, record in first.items():
        for relative in record['files']:
            left = os.path.join(FIXTURE_ROOT, name, relative.replace('/', os.sep))
            right = os.path.join(str(tmp_path / 'rebuild'), name,
                                 relative.replace('/', os.sep))
            if reference.sha256_file(left) != reference.sha256_file(right):
                differences.append('%s/%s' % (name, relative))
    assert differences == [], '夹具重建后不一致：%s' % differences
    assert len(first) == len(second)


@pytest.mark.parametrize('fixture_name,check', [
    ('gltf_three_distinct', 'three_distinct_values'),
    ('gltf_four_parts_share_one', 'one_material_four_sites'),
    ('gltf_one_mesh_multi_primitive', 'single_mesh_multi_primitive'),
    ('gltf_same_name_two_definitions', 'duplicate_name_two_definitions'),
    ('gltf_no_material_and_unused', 'unassigned_and_unused'),
    ('obj_dangling_and_unused', 'dangling_reference'),
    ('gltf_missing_texture', 'missing_texture_reported'),
    ('gltf_corrupt_json', 'corrupt_reported'),
    ('gltf_multi_uv_transform', 'uv_transform_and_orm'),
    ('gltf_alpha_mask_overlap', 'alpha_modes'),
    ('gltf_transmission_volume', 'transmission_is_not_scattering'),
    ('gltf_embedded_glb', 'embedded_media'),
    ('gltf_absolute_author_path', 'absolute_path'),
    ('obj_bump_vs_normal', 'bump_is_not_normal'),
    ('gltf_version_reorder', 'version_reorder'),
])
def test_required_structure_is_present(expectation_payload, fixture_name, check):
    """The expectation set names the structure the brief demands, by fact."""
    record = expectation_payload['fixtures'].get(fixture_name)
    assert record is not None, '缺少夹具 %s' % fixture_name
    facts = record['facts']
    materials = facts['materials']

    if check == 'three_distinct_values':
        assert len(materials) == 3
        values = {json.dumps([material['properties'],
                              [(texture['slot'], texture['reference'])
                               for texture in material['textures']]],
                             sort_keys=True, ensure_ascii=False)
                  for material in materials}
        assert len(values) == 3, '三个材质里有重复的定义'
    elif check == 'one_material_four_sites':
        assert facts['material_count'] == 1
        assert facts['usage_counts'] == {'0': 4}
    elif check == 'single_mesh_multi_primitive':
        geometry = {site['geometry_key'] for site in facts['sites']}
        assert len(facts['sites']) == 3 and len(geometry) == 1
    elif check == 'duplicate_name_two_definitions':
        assert facts['duplicate_material_names'] == ['Wood']
        assert len({material['name'] for material in materials}) == 1
        references = {texture['reference'] for material in materials
                      for texture in material['textures']}
        assert len(references) == 2, '同名两定义应当引用不同贴图'
    elif check == 'unassigned_and_unused':
        assert facts['unused_materials'] == [1]
        assert facts['unassigned_sites'], '没有记录"未指定材质"的使用点'
    elif check == 'dangling_reference':
        assert facts['dangling_material_references'] == ['MissingDefinition']
        assert any(site['dangling'] for site in facts['sites'])
    elif check == 'missing_texture_reported':
        assert facts['missing_dependencies'] == ['metal_orm.png']
    elif check == 'corrupt_reported':
        assert facts['ok'] is False and facts['error']
    elif check == 'uv_transform_and_orm':
        assert facts['uv_sets'] == 2
        slots = {texture['slot']: texture for material in materials
                 for texture in material['textures']}
        assert slots['normal']['texcoord'] == 1
        assert slots['normal']['uv_transform'], 'KHR_texture_transform 没被记录'
        assert slots['occlusion']['reference'] == slots['metallic_roughness']['reference']
        assert slots['base_color']['channels'] == 'rgba'
        assert materials[0]['double_sided'] is True
    elif check == 'alpha_modes':
        modes = {material['name']: material['alpha_mode'] for material in materials}
        assert modes == {'Cutout': 'MASK', 'Smoke': 'BLEND'}
        cutout = [material for material in materials if material['name'] == 'Cutout'][0]
        assert cutout['alpha_cutoff'] == 0.25
    elif check == 'transmission_is_not_scattering':
        by_name = {material['name']: material for material in materials}
        assert 'KHR_materials_transmission' in by_name['ThinTransmission']['extensions']
        assert 'KHR_materials_volume' in by_name['VolumeGlass']['extensions']
        assert by_name['VolumeGlass']['properties']['volume.thicknessFactor'] == 0.5
        assert by_name['PlainBlend']['extensions'] == [], \
            '普通半透明被标成散射，报告就会把 alpha 当 SSS'
    elif check == 'embedded_media':
        assert facts['embedded_media'], 'GLB 的内嵌贴图没有被记录'
        assert facts['dependencies'] == []
    elif check == 'absolute_path':
        assert facts['absolute_references'] == [
            'C:/Users/author/Documents/textures/wood.png']
        reference = materials[0]['textures'][0]
        assert reference['absolute'] is True and reference['exists'] is False
    elif check == 'bump_is_not_normal':
        by_name = {material['name']: material for material in materials}
        bump = [texture for texture in by_name['BumpedWood']['textures']]
        normal = [texture for texture in by_name['NormalledMetal']['textures']]
        assert [texture['slot'] for texture in bump] == ['bump']
        assert bump[0]['normal_convention'] == 'height'
        assert 'normal' in [texture['slot'] for texture in normal]
        assert bump[0]['extra']['bump_multiplier'] == ['0.5']
    elif check == 'version_reorder':
        names = [material['name'] for material in materials]
        assert names == ['Steel', 'Oak_Wood', 'Rubber']
        assert record['declared']['removed'] == ['Glass']
        assert record['declared']['ambiguous_match'], '重排/改名的歧义没有被记录'
    else:                                              # pragma: no cover - typo
        pytest.fail('未知检查项 %s' % check)


def test_corrupt_upstream_sample_is_called_corrupt(expectation_payload):
    """The assimp FBX whose container header is damaged must not read as empty.

    "0 materials" and "cannot open the file" lead to opposite user advice, so
    the expectation set has to keep them apart.
    """
    record = expectation_payload['upstream']['assimp_transparent']
    facts = record['facts']
    assert facts['ok'] is False
    assert 'malformed binary header' in (facts['error'] or '')
    assert facts['material_count'] == 0


def test_limitations_are_written_down(expectation_payload):
    """The gaps this set cannot cover have to be part of the artifact."""
    text = ' '.join(expectation_payload['limitations'])
    for topic in ('FBX', 'SSS', '置换', 'instancing'):
        assert topic in text, '局限里没有写明 %s 的边界' % topic


def test_expectation_records_carry_provenance(expectation_payload):
    """Every upstream case names its source, licence and hash."""
    missing = []
    for sample_id, record in expectation_payload['upstream'].items():
        if 'error' in record:
            missing.append('%s: %s' % (sample_id, record['error']))
            continue
        for key in ('source_repo', 'license', 'sha256'):
            if not record.get(key):
                missing.append('%s: 缺少 %s' % (sample_id, key))
    assert missing == []


def test_expectations_file_is_written(expectation_payload):
    """The set exists on disk as well, so other roles can read it."""
    path = D1_DIR / 'EXPECTATIONS.json'
    assert path.is_file(), '没有写出 %s' % path
    on_disk = json.loads(path.read_text(encoding='utf-8'))
    assert on_disk['coverage'] == build_expectations.COVERAGE
    assert on_disk['consistency']['problems'] == []
    assert on_disk['policy']
