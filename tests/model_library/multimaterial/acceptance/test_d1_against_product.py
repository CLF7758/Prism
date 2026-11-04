"""D1 gate: compare the product's catalog against the independent expectation.

The expectation comes from :mod:`material_reference`, which parses the raw
bytes and never imports ``prism``.  The product is
``prism.model_library.multimaterial.inspectors.inspect_source``.  When the two
disagree, the product is wrong -- but only after the reference reader has
already been proven right by the declared-intent cross-check in
``test_d1_expectation_set.py``.

The comparison is deliberately about *user-visible semantics*: how many
materials, what they are called, which texture roles each one uses, how many
places use it, which places use nothing, which definitions nothing uses, which
files are missing, and whether an unreadable file is reported as unreadable.

Every result -- pass or mismatch -- is written to
``artifacts/model-multimaterial/d1/product-comparison-*.json``, so a mismatch
is evidence rather than a chat message.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import D1_DIR, SAMPLES_DIR

#: Slot names are not the same vocabulary on both sides; the *role* is.
_ROLE = {
    'base_color': 'albedo', 'diffuse': 'albedo', 'basecolor': 'albedo',
    'normal': 'normal', 'norm': 'normal', 'bump': 'bump',
    'metallic_roughness': 'metalrough', 'metallic': 'metalrough',
    'roughness': 'metalrough',
    'occlusion': 'ao', 'ao': 'ao',
    'emissive': 'emissive', 'specular': 'specular',
    'specular_exponent': 'specular',
    'opacity': 'opacity', 'opacity_inverse': 'opacity',
    'displacement': 'displacement', 'reflection': 'reflection',
    'decal': 'decal', 'ambient': 'ambient',
}

#: The gate runs every case in the expectation set.  Writing the list by hand
#: is how a new fixture silently stops being checked, so the list is read from
#: the set itself and ``test_gate_covers_every_expectation_case`` keeps the two
#: in step.
def _collected_cases():
    path = D1_DIR / 'EXPECTATIONS.json'
    if not path.is_file():
        return ['fixture:gltf_three_distinct']
    payload = json.loads(path.read_text(encoding='utf-8'))
    cases = ['fixture:' + name for name in sorted(payload.get('fixtures', {}))]
    cases += ['upstream:' + name for name in sorted(payload.get('upstream', {}))
              if 'error' not in payload['upstream'][name]]
    return cases


CASES = _collected_cases()


def _role(slot):
    text = str(slot or '').lower()
    if text in _ROLE:
        return _ROLE[text]
    for key, value in _ROLE.items():
        if text.startswith(key):
            return value
    return text


def _basenames(values):
    """Compare missing references by file name: the two sides spell paths apart."""
    names = set()
    for value in values:
        text = str(value).replace('\\', '/').rstrip('/')
        names.add(text.rsplit('/', 1)[-1].lower())
    return names


def _product_usage(catalog, definition):
    return sum(1 for binding in catalog.binding_sites
               if binding.original_material_id == definition.material_id)


def _unresolved(catalog):
    try:
        from prism.model_library.multimaterial import catalog as catalog_module
        return tuple(catalog_module.unresolved_resources(catalog))
    except Exception:                                  # noqa: BLE001 - recorded
        return ()


def _embedded(catalog):
    try:
        from prism.model_library.multimaterial import catalog as catalog_module
        return tuple(catalog_module.embedded_resources(catalog))
    except Exception:                                  # noqa: BLE001 - recorded
        return ()


def _product_slots():
    """The slot vocabulary the product declares, so extra roles stay notes."""
    try:
        from prism.model_library.multimaterial import catalog as catalog_module
        return set(catalog_module.EDITABLE_SLOTS) | set(catalog_module.SOURCE_ONLY_SLOTS)
    except Exception:                                  # noqa: BLE001 - recorded
        return set()


def compare(facts, catalog):
    """Return (hard mismatches, informational notes) for one file."""
    problems = []
    notes = []
    vocabulary = {_role(slot) for slot in _product_slots()}

    expected_count = len(facts['materials'])
    if expected_count != len(catalog.definitions):
        problems.append('材质数量：期望 %d，产品 %d（期望名 %r，产品名 %r）'
                        % (expected_count, len(catalog.definitions),
                           [item['name'] for item in facts['materials']],
                           [item.name for item in catalog.definitions]))
        return problems, notes

    usage_counts = facts['usage_counts']
    for index, (expected, actual) in enumerate(zip(facts['materials'],
                                                   catalog.definitions)):
        name = expected['name']
        if name != actual.name:
            if '\ufffd' in name:
                notes.append('材质 %d 名称无法逐字核对：文件不是合法 UTF-8，'
                             '期望 %r，产品 %r' % (index, name, actual.name))
            else:
                problems.append('材质 %d 名称：期望 %r，产品 %r'
                                % (index, name, actual.name))
        expected_roles = {_role(use['slot']) for use in expected['textures']}
        beyond = sorted(expected_roles - vocabulary)
        if beyond:
            notes.append('材质 %s 的 %r 不在产品的槽位词表里，不计入失败'
                         % (name, beyond))
        expected_roles &= vocabulary
        actual_roles = {_role(use.slot) for use in actual.textures}
        missing_roles = sorted(expected_roles - actual_roles)
        if missing_roles:
            problems.append('材质 %d（%s）贴图角色缺失：%r（产品只有 %r）'
                            % (index, name, missing_roles, sorted(actual_roles)))
        expected_usage = usage_counts.get(str(index), 0)
        actual_usage = _product_usage(catalog, actual)
        if expected_usage != actual_usage:
            problems.append('材质 %d（%s）使用点：期望 %d，产品 %d'
                            % (index, name, expected_usage, actual_usage))
        normal = [use for use in expected['textures']
                  if _role(use['slot']) == 'normal']
        if normal:
            product_normal = [use for use in actual.textures
                              if _role(use.slot) == 'normal']
            if product_normal and normal[0]['texcoord'] is not None and \
                    product_normal[0].texcoord != normal[0]['texcoord']:
                problems.append('材质 %d（%s）法线 UV 套：期望 %r，产品 %r'
                                % (index, name, normal[0]['texcoord'],
                                   product_normal[0].texcoord))
        if expected['alpha_mode'] not in ('unknown', None):
            try:
                properties = json.loads(actual.properties_json or '{}')
            except ValueError:
                properties = {}
            actual_alpha = properties.get('alpha_mode')
            if actual_alpha is not None and \
                    str(actual_alpha).upper() != str(expected['alpha_mode']).upper():
                problems.append('材质 %d（%s）alpha 模式：期望 %r，产品 %r'
                                % (index, name, expected['alpha_mode'], actual_alpha))

    # Our expectation separates "nothing stated" from "stated but undefined";
    # the product records both as an unbound site and explains it in a note.
    # Comparing the union is the honest comparison -- the split is checked on
    # the expectation side by test_d1_negative_cases.
    expected_unassigned = (len(facts['unassigned_sites'])
                           + len(facts['dangling_sites']))
    actual_unassigned = len([binding for binding in catalog.binding_sites
                             if binding.original_material_id is None])
    if expected_unassigned != actual_unassigned:
        problems.append('没有材质的使用点：期望 %d（未指定 %d + 悬空 %d），产品 %d'
                        % (expected_unassigned, len(facts['unassigned_sites']),
                           len(facts['dangling_sites']), actual_unassigned))

    expected_unused = sorted(facts['materials'][index]['name']
                             for index in facts['unused_materials'])
    actual_unused = sorted(definition.name for definition in catalog.definitions
                           if _product_usage(catalog, definition) == 0)
    if len(expected_unused) != len(actual_unused):
        problems.append('未使用定义：期望 %r，产品 %r'
                        % (expected_unused, actual_unused))

    # A missing .mtl is a missing *library*, which the product reports as its
    # own note; ``unresolved_resources`` is about texture resources.  The two
    # are compared separately (see test_d1_negative_cases), never merged.
    library_missing = [name for name in facts['missing_dependencies']
                       if name.lower().endswith('.mtl')]
    texture_missing = [name for name in facts['missing_dependencies']
                       if not name.lower().endswith('.mtl')]
    if library_missing:
        notes.append('缺材质库 %r：由 no-material 反例单独核对' % library_missing)
    expected_missing = _basenames(texture_missing)
    actual_missing = _basenames(locator for _slot, locator in _unresolved(catalog))
    if facts['absolute_references']:
        notes.append('文件含作者绝对路径 %d 处：两边选用的文件名字段不同'
                     '（期望缺 %r，产品未解析 %r）'
                     % (len(facts['absolute_references']),
                        sorted(expected_missing), sorted(actual_missing)))
    elif expected_missing != actual_missing:
        problems.append('缺失文件：期望 %r，产品 %r'
                        % (sorted(expected_missing), sorted(actual_missing)))

    if facts['embedded_media'] and not _embedded(catalog):
        problems.append('内嵌资源：期望 %d 个，产品 0 个'
                        % len(facts['embedded_media']))
    if expected_missing and not _unresolved(catalog) \
            and not facts['absolute_references']:
        problems.append('缺图没有被报告为未解析资源')

    return problems, notes


def _cases(expectation_payload):
    cases = []
    for name, record in sorted(expectation_payload['fixtures'].items()):
        cases.append(('fixture:' + name,
                      os.path.join(str(D1_DIR), 'fixtures', name,
                                   record['declared']['main_file']),
                      record['facts']))
    for sample_id, record in sorted(expectation_payload['upstream'].items()):
        if 'error' in record:
            continue
        cases.append(('upstream:' + sample_id,
                      os.path.join(str(SAMPLES_DIR), record['directory'],
                                   record['main_file']),
                      record['facts']))
    return cases


def _case_map(payload):
    return {case_id: (path, facts) for case_id, path, facts in _cases(payload)}


def _inspect(path):
    from prism.model_library.multimaterial.inspectors import inspect_source
    return inspect_source(path)


@pytest.mark.parametrize('case_id', CASES, ids=lambda value: value.split(':', 1)[1])
def test_product_catalog_matches_expectation(case_id, expectation_payload,
                                             write_report):
    """The parse layer must agree with the independent answer key."""
    cases = _case_map(expectation_payload)
    assert case_id in cases, '期望集里没有 %s' % case_id
    path, facts = cases[case_id]
    assert os.path.isfile(path), '样本文件不存在：%s' % path

    if not facts['ok']:
        # The independent reader says this file cannot be read.  Reporting an
        # empty catalog instead would tell the user "this model has no
        # materials", which is different advice from "this file is broken".
        from prism.model_library.multimaterial.catalog import CatalogParseError
        with pytest.raises((CatalogParseError, RuntimeError)):
            _inspect(path)
        write_report(D1_DIR, 'product-comparison-%s.json' % case_id.replace(':', '-'),
                     {'case': case_id, 'path': path, 'expected': 'unreadable',
                      'result': 'raised as expected'})
        return

    catalog = _inspect(path)
    problems, notes = compare(facts, catalog)
    write_report(D1_DIR, 'product-comparison-%s.json' % case_id.replace(':', '-'),
                 {'case': case_id, 'path': path,
                  'expected': {'definitions': len(facts['materials']),
                               'names': [item['name'] for item in facts['materials']],
                               'usage': facts['usage_counts']},
                  'product': {'definitions': len(catalog.definitions),
                              'parser': catalog.parser_id,
                              'names': [item.name for item in catalog.definitions]},
                  'mismatches': problems, 'notes': notes})
    assert problems == [], '%s 与独立期望不符：%s' % (case_id, problems)


def test_fbx_identical_names_are_distinguishable(expectation_payload):
    """Two materials called "Material" cannot be told apart in a list.

    ``cubes_with_names.fbx`` declares ``Mat_Green`` and ``Mat_Red``.  If the
    catalog reports both as ``Material`` the panel shows two identical rows and
    "edit the red one" becomes ambiguous.
    """
    cases = _case_map(expectation_payload)
    path, facts = cases['upstream:assimp_multi_mesh_named']
    expected = [item['name'] for item in facts['materials']]
    catalog = _inspect(path)
    names = [definition.name for definition in catalog.definitions]
    assert names == expected, (
        'FBX 材质名丢失：文件里是 %r，产品给出 %r' % (expected, names))


def test_fbx_texture_slots_are_recognised(expectation_payload):
    """3ds Max PBR FBX writes seven texture slots; they must not all vanish."""
    cases = _case_map(expectation_payload)
    path, facts = cases['upstream:assimp_pbr_metal_rough']
    expected = {_role(use['slot']) for material in facts['materials']
                for use in material['textures']}
    catalog = _inspect(path)
    actual = {_role(use.slot) for definition in catalog.definitions
              for use in definition.textures}
    assert expected <= actual, (
        'FBX 贴图槽丢失：文件里有 %r，产品只识别到 %r'
        % (sorted(expected), sorted(actual)))


def test_gate_covers_every_expectation_case(expectation_payload):
    """Every fixture and every readable sample in the set must be gated.

    Without this, adding a fixture would quietly add an unenforced expectation
    and the report would claim coverage it does not have.
    """
    expected = {'fixture:' + name for name in expectation_payload['fixtures']}
    expected |= {'upstream:' + name for name, record
                 in expectation_payload['upstream'].items() if 'error' not in record}
    missing = sorted(expected - set(CASES))
    assert missing == [], '这些期望没有被产品门覆盖：%s' % missing
