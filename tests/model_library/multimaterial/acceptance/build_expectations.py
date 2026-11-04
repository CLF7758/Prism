"""Build the D1 independent expectation set.

Two sources are combined, and neither of them is the implementation under test:

1. **Structural fixtures** (:mod:`fixtures`) -- authored files for the
   structures real download corpora do not contain, each shipped with the
   author's declared intent.
2. **Real upstream samples** already vendored under
   ``tests/model_library/acceptance/samples`` -- provenance, licence and
   SHA-256 come from that corpus' inventory, so a changed sample cannot
   silently change the expectation.

Both are read back by :mod:`material_reference`, which parses raw bytes.  The
declared intent is then compared with what the reader derived: a disagreement
fails the build, because it means the reference reader -- not the product --
is wrong.

Output (``artifacts/model-multimaterial/d1/``):

* ``fixtures/``         -- the fixture files themselves
* ``EXPECTATIONS.json`` -- the expectation set the checks compare against
* ``REPORT.md``         -- coverage against the D1 requirement list

Run::

    .venv/Scripts/python.exe tests/model_library/multimaterial/acceptance/build_expectations.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time

try:                                            # imported as a package by pytest
    from . import fixtures
    from . import material_reference as reference
except ImportError:                             # run as a script
    import fixtures
    import material_reference as reference

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
ARTIFACTS = os.path.join(REPO, 'artifacts', 'model-multimaterial', 'd1')
FIXTURE_ROOT = os.path.join(ARTIFACTS, 'fixtures')
SAMPLES = os.path.join(REPO, 'tests', 'model_library', 'acceptance', 'samples')

#: Which upstream samples the expectation set uses, and why.  Choosing by hand
#: is deliberate: "the first file in the directory" is how an expectation ends
#: up describing a different file than the one that gets imported.
UPSTREAM_CASES = {
    'khronos_boxtextured_gltf': '外置贴图 + 外置 BIN 的最小可核对样本',
    'khronos_multiuvtest_gltf': '两个 UV 集，两张贴图分别绑 0/1 套',
    'khronos_alphablendmodetest_gltf': 'BLEND/MASK/OPAQUE 同文件；材质被多处共用',
    'khronos_simpleinstancing_gltf': 'EXT_mesh_gpu_instancing，且没有材质',
    'khronos_emissivestrengthtest_gltf': 'KHR_materials_emissive_strength',
    'khronos_meshprimitivemodes_gltf': '点/线/三角混合 primitive',
    'khronos_orientationtest_gltf': '法线朝向/双面边界',
    'khronos_damagedhelmet_glb': 'GLB 内嵌 ORM/法线/自发光，全内嵌',
    'assimp_multimaterial': 'OBJ 多材质 + 引用未定义材质名',
    'assimp_mtl_other_dir': 'MTL 在别的目录，贴图缺失',
    'assimp_mat_with_spaces': '材质名与文件路径含空格',
    'assimp_utf16': 'UTF-16 编码的 OBJ/MTL',
    'derived_same_name_texture_two_dirs': '同名贴图两个目录，按名解析会取错',
    'polyhaven_barrel_01': '二进制 FBX，贴图路径是作者机器上的绝对路径',
    'assimp_multi_mesh_named': 'ASCII FBX，两份材质被四个模型共用、含嵌套节点',
    'assimp_pbr_metal_rough': 'ASCII FBX（3ds Max PBR），七个贴图槽',
    'assimp_transparent': 'FBX 容器头损坏，必须报损坏而不是"无材质"',
}

#: The D1 requirement list from the role brief, item by item, mapped to the
#: cases that cover it.  Anything not covered here is visible as a gap.
COVERAGE = {
    '4.1 三个明确不同材质；同名不同定义': [
        'gltf_three_distinct', 'gltf_same_name_two_definitions',
        'obj_same_name_two_definitions', 'derived_same_name_texture_two_dirs',
        'khronos_alphablendmodetest_gltf',
        # 同名不同字节：三个名称解码后完全相同，身份必须靠原始字节
        'obj_encoding_collision'],
    '4.2 共享材质 4 处；同 mesh 多节点实例；单 mesh 多 primitive': [
        'gltf_four_parts_share_one', 'gltf_one_mesh_multi_primitive',
        'assimp_multi_mesh_named', 'khronos_simpleinstancing_gltf',
        'khronos_alphablendmodetest_gltf'],
    '4.3 无材质、未使用定义、纯数值、缺图与坏文件': [
        'gltf_no_material_and_unused', 'obj_dangling_and_unused',
        'gltf_missing_texture', 'obj_missing_texture', 'gltf_corrupt_json',
        'assimp_transparent', 'assimp_multimaterial', 'khronos_simpleinstancing_gltf',
        # 四种"没有材质"必须分开：无 usemtl / 缺 MTL / usemtl 之前的面 / 悬空
        'obj_no_usemtl', 'obj_missing_mtl', 'obj_unassigned_before_usemtl',
        'obj_dangling_material'],
    '4.4 多 UV、非默认变换、双面、RGB/A 共图、ORM、法线约定': [
        'gltf_multi_uv_transform', 'khronos_multiuvtest_gltf',
        'khronos_damagedhelmet_glb', 'polyhaven_barrel_01',
        'obj_bump_vs_normal', 'khronos_orientationtest_gltf'],
    '4.5 内嵌图片、外部 BIN/图、绝对作者路径、同名依赖冲突': [
        'gltf_embedded_glb', 'khronos_damagedhelmet_glb',
        'khronos_boxtextured_gltf', 'gltf_absolute_author_path',
        'obj_absolute_author_path', 'polyhaven_barrel_01',
        'derived_same_name_texture_two_dirs', 'assimp_mtl_other_dir'],
    '4.6 alpha mask/blend 与透明重叠；SSS/透射/薄透光分开；置换与 bump 分开': [
        'gltf_alpha_mask_overlap', 'gltf_transmission_volume',
        'khronos_alphablendmodetest_gltf', 'obj_bump_vs_normal'],
    '4.7 源新版本材质重排、增删、改名和无法匹配': [
        'gltf_version_reorder', 'gltf_version_unmatched'],
}


def _sha256(path):
    return reference.sha256_file(path)


def _file_hashes(directory, names):
    hashes = {}
    for name in names:
        path = os.path.join(directory, name.replace('/', os.sep))
        hashes[name] = _sha256(path) if os.path.isfile(path) else None
    return hashes


def _derived_missing_textures(facts):
    missing = set()
    for material in facts.materials:
        for use in material.textures:
            if use.exists is False:
                missing.add(use.reference or use.resolved)
    return sorted(missing)


def _derived_slots(facts):
    slots = {}
    for material in facts.materials:
        if material.textures:
            key = material.name
            if key in slots:
                key = '%s#%d' % (material.name, material.index)
            slots[key] = sorted({use.slot for use in material.textures})
    return slots


def _usage_by_name(facts):
    """Usage counts keyed so that duplicate names stay distinguishable."""
    counts = {}
    seen = {}
    for material in facts.materials:
        key = material.name
        if [other.name for other in facts.materials].count(material.name) > 1:
            key = '%s#%d' % (material.name, seen.get(material.name, 0))
            seen[material.name] = seen.get(material.name, 0) + 1
        counts[key] = facts.usage_counts().get(material.index, 0)
    return counts


def compare(declared, facts):
    """Compare the author's intent with what the independent reader derived.

    Returns ``(problems, checked_keys)``.  A key the harness does not know how
    to check is listed as informational rather than silently ignored.
    """
    problems = []
    checked = []

    def fail(message):
        problems.append(message)

    if 'readable' in declared and declared['readable'] is False:
        checked.append('readable')
        if facts.ok:
            fail('声明为不可读，参考读取器却读出了内容')
        return problems, checked

    if 'material_names' in declared:
        checked.append('material_names')
        names = [material.name for material in facts.materials]
        if names != declared['material_names']:
            fail('材质名不符：声明 %r，读取 %r' % (declared['material_names'], names))

    if 'usage' in declared:
        checked.append('usage')
        actual = _usage_by_name(facts)
        for key, expected in declared['usage'].items():
            if actual.get(key) != expected:
                fail('使用点数不符 %s：声明 %r，读取 %r（全部 %r）'
                     % (key, expected, actual.get(key), actual))

    if 'unused' in declared:
        checked.append('unused')
        unused = [facts.materials[index].name for index in facts.unused_materials]
        if sorted(unused) != sorted(declared['unused']):
            fail('未使用定义不符：声明 %r，读取 %r' % (declared['unused'], unused))

    if 'duplicate_material_names' in declared:
        checked.append('duplicate_material_names')
        if sorted(facts.duplicate_material_names) != sorted(
                declared['duplicate_material_names']):
            fail('同名材质不符：声明 %r，读取 %r'
                 % (declared['duplicate_material_names'],
                    facts.duplicate_material_names))

    if 'dangling_material_references' in declared:
        checked.append('dangling_material_references')
        if sorted(facts.dangling_material_references) != sorted(
                declared['dangling_material_references']):
            fail('悬空材质引用不符：声明 %r，读取 %r'
                 % (declared['dangling_material_references'],
                    facts.dangling_material_references))

    if 'sites_without_material' in declared:
        checked.append('sites_without_material')
        unassigned = [site for site in facts.sites
                      if site.material_index is None and not site.dangling]
        if len(unassigned) != declared['sites_without_material']:
            fail('无材质使用点数不符：声明 %r，读取 %r'
                 % (declared['sites_without_material'], len(unassigned)))

    if 'unassigned_sites' in declared:
        checked.append('unassigned_sites')
        unassigned = [site for site in facts.sites
                      if site.material_index is None and not site.dangling]
        if len(unassigned) != declared['unassigned_sites']:
            fail('无材质使用点数不符：声明 %r，读取 %r'
                 % (declared['unassigned_sites'], len(unassigned)))

    if 'missing_textures' in declared:
        checked.append('missing_textures')
        actual = _derived_missing_textures(facts)
        if sorted(actual) != sorted(declared['missing_textures']):
            fail('缺图清单不符：声明 %r，读取 %r'
                 % (declared['missing_textures'], actual))

    if 'present_textures' in declared:
        checked.append('present_textures')
        present = sorted({use.reference for material in facts.materials
                          for use in material.textures if use.exists})
        for name in declared['present_textures']:
            if name not in present:
                fail('声明存在的贴图缺失：%r（读取到 %r）' % (name, present))

    if 'absolute_references' in declared:
        checked.append('absolute_references')
        if sorted(facts.absolute_references) != sorted(
                declared['absolute_references']):
            fail('绝对路径清单不符：声明 %r，读取 %r'
                 % (declared['absolute_references'],
                    list(facts.absolute_references)))

    if 'uv_sets' in declared:
        checked.append('uv_sets')
        if facts.uv_sets != declared['uv_sets']:
            fail('UV 套数不符：声明 %r，读取 %r' % (declared['uv_sets'], facts.uv_sets))

    if 'alpha' in declared:
        checked.append('alpha')
        for name, mode in declared['alpha'].items():
            match = [material for material in facts.materials if material.name == name]
            if not match:
                fail('声明了 alpha 模式的材质不存在：%r' % name)
                continue
            actual = match[0].alpha_mode
            if actual != mode:
                fail('alpha 模式不符 %s：声明 %r，读取 %r' % (name, mode, actual))

    if 'alpha_cutoff' in declared:
        checked.append('alpha_cutoff')
        for name, cutoff in declared['alpha_cutoff'].items():
            match = [material for material in facts.materials if material.name == name]
            if not match or match[0].alpha_cutoff != cutoff:
                fail('alphaCutoff 不符 %s：声明 %r，读取 %r'
                     % (name, cutoff, match[0].alpha_cutoff if match else None))

    if 'double_sided' in declared:
        checked.append('double_sided')
        for name, value in declared['double_sided'].items():
            match = [material for material in facts.materials if material.name == name]
            if not match or match[0].double_sided != value:
                fail('双面标志不符 %s：声明 %r，读取 %r'
                     % (name, value, match[0].double_sided if match else None))

    if 'slots' in declared:
        checked.append('slots')
        actual = _derived_slots(facts)
        for name, slots in declared['slots'].items():
            if actual.get(name) != sorted(slots):
                fail('槽位不符 %s：声明 %r，读取 %r'
                     % (name, sorted(slots), actual.get(name)))

    if 'texture_slots' in declared:
        checked.append('texture_slots')
        total = sum(len(material.textures) for material in facts.materials)
        if total != declared['texture_slots']:
            fail('贴图槽总数不符：声明 %r，读取 %r'
                 % (declared['texture_slots'], total))

    if 'bump_multiplier' in declared:
        checked.append('bump_multiplier')
        found = [use.extra.get('bump_multiplier') for material in facts.materials
                 for use in material.textures if use.slot == 'bump']
        values = {float(item[0]) for item in found if item}
        if declared['bump_multiplier'] not in values:
            fail('bump 强度不符：声明 %r，读取 %r'
                 % (declared['bump_multiplier'], sorted(values)))

    if 'face_counts' in declared:
        checked.append('face_counts')
        actual = {}
        for site in facts.sites:
            if site.material_name:
                actual[site.material_name] = actual.get(site.material_name, 0) + (
                    site.face_count or 0)
        for name, count in declared['face_counts'].items():
            if actual.get(name) != count:
                fail('面数不符 %s：声明 %r，读取 %r' % (name, count, actual.get(name)))

    if 'extensions' in declared:
        checked.append('extensions')
        for name, expected in declared['extensions'].items():
            match = [material for material in facts.materials if material.name == name]
            if not match:
                fail('声明了扩展的材质不存在：%r' % name)
                continue
            if sorted(match[0].extensions) != sorted(expected):
                fail('扩展不符 %s：声明 %r，读取 %r'
                     % (name, sorted(expected), list(match[0].extensions)))

    if 'volume_thickness' in declared:
        checked.append('volume_thickness')
        values = [material.properties.get('volume.thicknessFactor')
                  for material in facts.materials]
        values = [value for value in values if value is not None]
        if not values or float(values[0]) != declared['volume_thickness']:
            fail('体积厚度不符：声明 %r，读取 %r'
                 % (declared['volume_thickness'], values))

    if 'not_sss' in declared:
        checked.append('not_sss')
        for name in declared['not_sss']:
            match = [material for material in facts.materials if material.name == name]
            if not match:
                fail('声明为"非散射"的材质不存在：%r' % name)
                continue
            if any(item in ('KHR_materials_volume', 'KHR_materials_transmission')
                   for item in match[0].extensions):
                fail('%s 声明为普通半透明，却带散射/体积扩展' % name)

    if 'has_material_statement' in declared:
        checked.append('has_material_statement')
        actual = facts.assignment_summary()['has_material_statement']
        if actual != declared['has_material_statement']:
            fail('"是否声明过材质"不符：声明 %r，读取 %r'
                 % (declared['has_material_statement'], actual))

    if 'missing_material_libraries' in declared:
        checked.append('missing_material_libraries')
        actual = facts.assignment_summary()['missing_material_libraries']
        if sorted(actual) != sorted(declared['missing_material_libraries']):
            fail('缺失材质库不符：声明 %r，读取 %r'
                 % (declared['missing_material_libraries'], actual))

    if 'distinct_byte_identities' in declared:
        checked.append('distinct_byte_identities')
        actual = facts.assignment_summary()['distinct_byte_identities']
        if actual != declared['distinct_byte_identities']:
            fail('原始字节身份数不符：声明 %r，读取 %r'
                 % (declared['distinct_byte_identities'], actual))

    if 'numeric_only_material' in declared:
        checked.append('numeric_only_material')
        match = [material for material in facts.materials
                 if material.name == declared['numeric_only_material']]
        if not match:
            fail('纯数值材质不存在：%r' % declared['numeric_only_material'])
        elif match[0].textures:
            fail('%s 声明为纯数值材质，却读出了贴图'
                 % declared['numeric_only_material'])
        elif not match[0].properties:
            fail('%s 声明为纯数值材质，却没有任何数值' % declared['numeric_only_material'])

    if 'normal_texcoord' in declared or 'normal_scale' in declared:
        checked.append('normal_texcoord/normal_scale')
        found = [use for material in facts.materials for use in material.textures
                 if use.slot == 'normal']
        if not found:
            fail('声明了法线槽，读取器没有找到法线贴图')
        else:
            if 'normal_texcoord' in declared and found[0].texcoord != declared['normal_texcoord']:
                fail('法线 UV 套不符：声明 %r，读取 %r'
                     % (declared['normal_texcoord'], found[0].texcoord))
            if 'normal_scale' in declared and \
                    abs(float(found[0].extra.get('scale', 1.0))
                        - declared['normal_scale']) > 1e-6:
                fail('法线强度不符：声明 %r，读取 %r'
                     % (declared['normal_scale'], found[0].extra.get('scale')))

    if 'texture_transform' in declared:
        checked.append('texture_transform')
        found = [use for material in facts.materials for use in material.textures
                 if use.uv_transform]
        if not found:
            fail('声明了 UV 变换，读取器没有读到')
        else:
            transform = found[0].uv_transform
            for key, value in declared['texture_transform'].items():
                if [round(item, 6) for item in value] != [
                        round(item, 6) for item in transform.get(key, [])]:
                    fail('UV 变换 %s 不符：声明 %r，读取 %r'
                         % (key, value, transform.get(key)))

    if 'shared_mesh_instances' in declared:
        checked.append('shared_mesh_instances')
        geometry_keys = {site.geometry_key for site in facts.sites}
        if len(facts.sites) != declared['shared_mesh_instances']:
            fail('实例使用点数不符：声明 %r，读取 %r'
                 % (declared['shared_mesh_instances'], len(facts.sites)))
        if declared.get('distinct_geometry') is not None and \
                len(geometry_keys) != declared['distinct_geometry']:
            fail('不同几何数不符：声明 %r，读取 %r'
                 % (declared['distinct_geometry'], len(geometry_keys)))

    if 'distinct_geometry' in declared and 'shared_mesh_instances' not in declared:
        checked.append('distinct_geometry')
        geometry_keys = {site.geometry_key for site in facts.sites}
        if len(geometry_keys) != declared['distinct_geometry']:
            fail('不同几何数不符：声明 %r，读取 %r'
                 % (declared['distinct_geometry'], len(geometry_keys)))

    if 'primitives_per_mesh' in declared:
        checked.append('primitives_per_mesh')
        per_mesh = {}
        for site in facts.sites:
            per_mesh[site.geometry_key] = per_mesh.get(site.geometry_key, 0) + 1
        if declared['primitives_per_mesh'] not in per_mesh.values():
            fail('单 mesh primitive 数不符：声明 %r，读取 %r'
                 % (declared['primitives_per_mesh'], list(per_mesh.values())))

    if 'orm_shared_image' in declared:
        checked.append('orm_shared_image')
        by_slot = {use.slot: use.reference for material in facts.materials
                   for use in material.textures}
        if by_slot.get('metallic_roughness') != by_slot.get('occlusion'):
            fail('声明 ORM 共用一张图，读取器看到 %r / %r'
                 % (by_slot.get('metallic_roughness'), by_slot.get('occlusion')))

    if 'shared_rgb_a_image' in declared:
        checked.append('shared_rgb_a_image')
        by_slot = {use.slot: (use.reference, use.channels)
                   for material in facts.materials for use in material.textures}
        base = by_slot.get('base_color')
        if not base or base[1] != 'rgba':
            fail('声明 RGB/A 共图，读取到的 base_color 通道是 %r' % (base,))

    if 'embedded_media' in declared:
        checked.append('embedded_media')
        if len(facts.embedded_media) != declared['embedded_media']:
            fail('内嵌媒体数不符：声明 %r，读取 %r'
                 % (declared['embedded_media'], len(facts.embedded_media)))

    if 'external_dependencies' in declared:
        checked.append('external_dependencies')
        if len(facts.dependencies) != declared['external_dependencies']:
            fail('外部依赖数不符：声明 %r，读取 %r'
                 % (declared['external_dependencies'], list(facts.dependencies)))

    if 'same_name_different_texture' in declared and declared['same_name_different_texture']:
        checked.append('same_name_different_texture')
        same = [material for material in facts.materials if material.name == 'Wood']
        references = {use.reference for material in same
                      for use in material.textures if use.slot == 'base_color'}
        if len(same) < 2 or len(references) < 2:
            fail('同名不同定义未被区分：%r 份同名材质、贴图 %r'
                 % (len(same), references))

    known = set(checked)
    informational = sorted(set(declared) - known)
    return problems, informational


def _spot_material_count(path):
    """A second, dumber count of the material definitions in a file.

    The reference reader is the expectation; this is the check on the checker.
    It deliberately shares no code with the reader.
    """
    extension = os.path.splitext(path)[1].lower()
    if extension in ('.gltf', '.glb'):
        if extension == '.gltf':
            document = json.loads(open(path, 'rb').read().decode('utf-8-sig'))
        else:
            # Read the JSON chunk out of the container with nothing but struct
            # and json: sharing no code with the reader keeps this a real check.
            import struct
            raw = open(path, 'rb').read()
            position = 12
            document = None
            while position + 8 <= len(raw):
                length, kind = struct.unpack('<II', raw[position:position + 8])
                position += 8
                if kind == 0x4E4F534A:
                    document = json.loads(raw[position:position + length]
                                          .decode('utf-8').rstrip('\x00 '))
                position += length
            if document is None:
                return None, 'GLB 没有 JSON 块'
        return len(document.get('materials') or []), 'json 直接计数'
    if extension == '.obj':
        text = reference._read_text(path)
        libraries = re.findall(r'^\s*mtllib\s+(.+)$', text, re.M)
        names = set()
        base = os.path.dirname(os.path.abspath(path))
        for library in libraries:
            candidate = os.path.join(base, library.strip())
            if os.path.isfile(candidate):
                names.update(re.findall(r'^\s*newmtl\s+(.+)$',
                                        reference._read_text(candidate), re.M | re.I))
        return len(names), 'newmtl 正则计数'
    if extension == '.fbx':
        with open(path, 'rb') as stream:
            raw = stream.read()
        if raw.startswith(reference._FBX_BINARY_MAGIC):
            return raw.count(b'\x08Material'), '二进制节点名原始计数（上界）'
        text = reference._decode_text(raw)
        return len(re.findall(r'^\s*Material:\s*\d+', text, re.M)), '文本 Material 块计数'
    return None, '无交叉核对方法'


def _read_upstream(inventory):
    samples = {sample['sample_id']: sample for sample in inventory.get('samples', [])}
    results = {}
    for sample_id, why in UPSTREAM_CASES.items():
        sample = samples.get(sample_id)
        if sample is None:
            results[sample_id] = {'error': '样本清单里没有这个样本', 'why': why}
            continue
        directory = os.path.join(SAMPLES, sample['directory'])
        wanted = (sample.get('format') or '').lower()
        candidates = []
        for entry in sample.get('files', []):
            extension = entry['relpath'].rsplit('.', 1)[-1].lower()
            if extension in ('gltf', 'glb', 'obj', 'fbx'):
                candidates.append((extension, entry['relpath']))
        main = None
        for extension, relative in candidates:
            if extension == wanted:
                main = relative
                break
        if main is None and candidates:
            main = candidates[0][1]
        if main is None:
            results[sample_id] = {'error': '目录里没有模型文件', 'why': why}
            continue
        path = os.path.join(directory, main.replace('/', os.sep))
        facts = reference.read_material_facts(path)
        count, method = _spot_material_count(path)
        record = {
            'why': why,
            'sample_id': sample_id,
            'format': sample.get('format'),
            'main_file': main,
            'directory': sample['directory'],
            'provenance_kind': sample.get('provenance_kind'),
            'source_repo': sample.get('source_repo'),
            'source_url_template': sample.get('source_url_template'),
            'license': sample.get('license'),
            'license_file': sample.get('license_file'),
            'license_note': sample.get('notes'),
            'sha256': facts.sha256,
            'files': _file_hashes(directory, [entry['relpath']
                                              for entry in sample.get('files', [])]),
            'recorded_files': {entry['relpath']: entry.get('sha256')
                               for entry in sample.get('files', [])},
            'facts': facts.as_dict(),
            'cross_check': {'count': count, 'method': method,
                            'reader_count': len(facts.materials)},
        }
        if count is not None:
            record['cross_check']['agrees'] = (
                count == len(facts.materials)
                if method != '二进制节点名原始计数（上界）'
                else len(facts.materials) <= count)
        results[sample_id] = record
    return results


def build():
    os.makedirs(FIXTURE_ROOT, exist_ok=True)
    generated = time.strftime('%Y-%m-%dT%H:%M:%S')
    built = fixtures.build(FIXTURE_ROOT)

    fixture_records = {}
    problems = []
    for name, record in built.items():
        declared = record['declared']
        directory = os.path.join(FIXTURE_ROOT, name)
        main = os.path.join(directory, declared['main_file'].replace('/', os.sep))
        facts = reference.read_material_facts(main)
        offset, informational = compare(declared, facts)
        if offset:
            problems.extend('%s: %s' % (name, item) for item in offset)
        fixture_records[name] = {
            'directory': name,
            'declared': declared,
            'declared_unchecked_keys': informational,
            'files': _file_hashes(directory, record['files']),
            'facts': facts.as_dict(),
        }

    inventory_path = os.path.join(SAMPLES, 'INVENTORY.json')
    inventory = (json.loads(open(inventory_path, encoding='utf-8').read())
                 if os.path.isfile(inventory_path) else {'samples': []})
    upstream = _read_upstream(inventory)
    for sample_id, record in upstream.items():
        check = record.get('cross_check') or {}
        if check.get('agrees') is False:
            problems.append('%s: 交叉计数不一致（%s 得 %r，参考读取器 %r）'
                            % (sample_id, check.get('method'), check.get('count'),
                               check.get('reader_count')))

    payload = {
        'schema_version': 1,
        'generated': generated,
        'generator': 'tests/model_library/multimaterial/acceptance/build_expectations.py',
        'policy': ('期望只来自原始文件的独立解析与作者节点/夹具声明；'
                   '被测实现（prism.model_library.multimaterial）从未参与生成。'),
        'coverage': COVERAGE,
        'fixtures': fixture_records,
        'upstream': upstream,
        'consistency': {'problems': problems},
        'limitations': [
            'FBX 没有合成夹具：手写 FBX 不能证明 DCC 输出的行为，FBX 覆盖只来自真实样本。',
            '没有真实 SSS（次表面散射）样本：glTF 的透射/体积不等于 SSS，'
            '本期望集只记录它们的区别，不宣称散射已覆盖。',
            '没有真实置换贴图样本：bump 与 normal 已分开，displacement 记为未覆盖。',
            'EXT_mesh_gpu_instancing 的实例数在扩展里，按节点路径枚举的使用点数低于真实实例数。',
        ],
    }
    expectations_path = os.path.join(ARTIFACTS, 'EXPECTATIONS.json')
    with open(expectations_path, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)

    _write_report(payload, fixture_records, upstream, problems, generated)
    return payload, problems


def _write_report(payload, fixtures_built, upstream, problems, generated):
    lines = ['# D1 独立期望集', '', '生成：%s' % generated, '',
             '生成器：`%s`' % payload['generator'], '',
             '## 覆盖对照（任务书第 4 节）', '']
    for requirement, cases in COVERAGE.items():
        existing = [case for case in cases
                    if case in fixtures_built or case in upstream]
        missing = [case for case in cases if case not in existing]
        lines.append('- **%s**' % requirement)
        lines.append('  - 覆盖：%s' % ('、'.join('`%s`' % case for case in existing)
                                     or '无'))
        if missing:
            lines.append('  - 缺口：%s' % '、'.join('`%s`' % case for case in missing))
    lines += ['', '## 结构夹具', '']
    for name, record in sorted(fixtures_built.items()):
        facts = record['facts']
        state = '可读' if facts['ok'] else '不可读（这是期望）'
        lines.append('- `%s`（%s）：材质 %d，使用点 %d，未使用 %s，缺图 %s，'
                     '绝对路径 %d，内嵌 %d，UV 套 %s，未核对键 %s'
                     % (name, state, facts['material_count'], len(facts['sites']),
                        facts['unused_materials'] or '无',
                        list(facts['missing_dependencies']) or '无',
                        len(facts['absolute_references']),
                        len(facts['embedded_media']),
                        facts['uv_sets'],
                        record['declared_unchecked_keys'] or '无'))
    lines += ['', '## 真实上游样本', '']
    for sample_id, record in sorted(upstream.items()):
        if 'error' in record:
            lines.append('- `%s`：%s' % (sample_id, record['error']))
            continue
        facts = record['facts']
        check = record['cross_check']
        verdict = {True: '一致', False: '不一致'}.get(check.get('agrees'), '跳过')
        lines.append('- `%s`（%s，%s）：材质 %d，使用点 %d，缺图 %d，'
                     '交叉计数 %s（%s）%s'
                     % (sample_id, record['format'], record['license'],
                        facts['material_count'], len(facts['sites']),
                        len(facts['missing_dependencies']), verdict,
                        check.get('method', '无'),
                        '' if facts['ok'] else '，读取器拒绝该文件：%s'
                        % (facts.get('error') or '')[:80]))
    lines += ['', '## 一致性', '']
    if problems:
        lines.append('声明与独立读取结果不一致 **%d** 项：' % len(problems))
        lines += ['- %s' % item for item in problems]
    else:
        lines.append('全部夹具的声明与独立读取结果一致（0 项不一致）。')
    lines += ['', '## 已知局限', '']
    lines += ['- %s' % item for item in payload['limitations']]
    with open(os.path.join(ARTIFACTS, 'REPORT.md'), 'w', encoding='utf-8') as stream:
        stream.write('\n'.join(lines) + '\n')


def main():
    payload, problems = build()
    print('fixtures :', len(payload['fixtures']))
    print('upstream :', len(payload['upstream']))
    print('problems :', len(problems))
    for item in problems:
        print('  -', item)
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
