"""Counter-examples that a happy path never reaches.

Each one exists because a plausible implementation choice silently loses
something the user can see:

* collapsing two materials whose names decode to the same text;
* dropping a part that has no material instead of showing it;
* treating "the .mtl never arrived" as "the model has no materials";
* reporting one of the two texture paths an FBX declares as if it were the only
  one, or showing a preferred path string as if a file had been adopted.

The third group is deliberately split in two: keeping every usage site is one
claim (it can be signed off on its own), and telling "nothing stated" from
"stated but undefined" *per site* is a different claim that needs an explicit
state in the shared contract.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import D1_DIR, SAMPLES_DIR

FIXTURE_ROOT = D1_DIR / 'fixtures'

#: The structures the two sides must agree on.  The four-way tuple
#: ``(definitions, sites, sites_without_material, unused)`` is a *structural*
#: signature: two situations that produce the same tuple are not distinguishable
#: to a user, no matter what the messages say.
NO_MATERIAL_CASES = {
    'obj_no_usemtl': '文件里从未出现 usemtl',
    'obj_missing_mtl': 'mtllib 声明的文件缺失',
    'obj_unassigned_before_usemtl': '有 usemtl，但第一个之前的面块没有材质',
    'obj_dangling_material': 'usemtl 指向未定义材质',
}


def _payload():
    return json.loads((D1_DIR / 'EXPECTATIONS.json').read_text(encoding='utf-8'))


def _expectation(name):
    return _payload()['fixtures'][name]['facts']


def _inspect(path):
    from prism.model_library.multimaterial.inspectors import inspect_source
    return inspect_source(path)


def _product_signature(catalog):
    none_sites = sum(1 for binding in catalog.binding_sites
                     if binding.original_material_id is None)
    used = {binding.original_material_id for binding in catalog.binding_sites}
    unused = sum(1 for definition in catalog.definitions
                 if definition.material_id not in used)
    return (len(catalog.definitions), len(catalog.binding_sites),
            none_sites, unused)


def test_encoding_collision_keeps_three_identities(write_report):
    """Three materials whose names display alike must stay three.

    ``b'Wood\\xff'``, ``b'Wood\\xfe'`` and ``b'Wood\\xef\\xbf\\xbd'`` all show
    as ``Wood\\ufffd``.  If identity is the displayed name, editing "the wood
    one" becomes ambiguous and two of the three surfaces cannot be reached.
    """
    facts = _expectation('obj_encoding_collision')
    assert len(facts['materials']) == 3
    digests = {material['name_bytes_sha256'] for material in facts['materials']}
    assert len(digests) == 3, '参考读取器没有按原始字节区分这三个名称'
    assert len({material['name'] for material in facts['materials']}) == 1, \
        '这三个名称在显示层面本来就应当相同，否则这个夹具没有意义'

    catalog = _inspect(str(FIXTURE_ROOT / 'obj_encoding_collision' / 'model.obj'))
    write_report(D1_DIR, 'encoding-collision.json',
                 {'expected_definitions': 3,
                  'expected_byte_identities': sorted(digests),
                  'product_definitions': len(catalog.definitions),
                  'product_ids': [d.material_id for d in catalog.definitions],
                  'product_names': [d.name for d in catalog.definitions],
                  'product_signature': _product_signature(catalog)})
    assert len(catalog.definitions) == 3, (
        '三个不同字节的材质被合并成 %d 个：%r'
        % (len(catalog.definitions), [d.name for d in catalog.definitions]))
    identifiers = [definition.material_id for definition in catalog.definitions]
    assert len(set(identifiers)) == 3, '三个材质的身份不唯一：%r' % identifiers
    assert {binding.original_material_id for binding in catalog.binding_sites} \
        == set(identifiers), '三个使用点没有分别绑定到三个不同材质'


@pytest.mark.parametrize('case', list(NO_MATERIAL_CASES), ids=list(NO_MATERIAL_CASES))
def test_sites_survive_without_a_material(case, write_report):
    """A part with no material is still a part: its usage site must not vanish."""
    facts = _expectation(case)
    expected_sites = len(facts['sites'])
    expected_none = len(facts['unassigned_sites']) + len(facts['dangling_sites'])
    catalog = _inspect(str(FIXTURE_ROOT / case / 'model.obj'))
    signature = _product_signature(catalog)
    write_report(D1_DIR, 'no-material-%s.json' % case,
                 {'case': case, 'why': NO_MATERIAL_CASES[case],
                  'expected': {'definitions': facts['material_count'],
                               'sites': expected_sites,
                               'sites_without_material': expected_none,
                               'unassigned': facts['unassigned_sites'],
                               'dangling': facts['dangling_sites'],
                               'missing_libraries':
                                   facts['assignment']['missing_material_libraries']},
                  'product': {'signature': signature,
                              'notes': list(catalog.unresolved),
                              'ids': [d.material_id for d in catalog.definitions]}})
    assert len(catalog.binding_sites) == expected_sites, (
        '%s：期望 %d 个使用点，产品 %d 个（使用点消失了，用户看不到这部分）'
        % (case, expected_sites, len(catalog.binding_sites)))
    assert signature[2] == expected_none, (
        '%s：期望 %d 个"没有材质"的使用点，产品 %d 个'
        % (case, expected_none, signature[2]))


def test_the_four_no_material_situations_stay_distinct(write_report):
    """Four different situations must not collapse into one shape."""
    signatures = {}
    for case in NO_MATERIAL_CASES:
        catalog = _inspect(str(FIXTURE_ROOT / case / 'model.obj'))
        signatures[case] = _product_signature(catalog)
    write_report(D1_DIR, 'no-material-signatures.json',
                 {'signatures': {case: list(value)
                                 for case, value in signatures.items()},
                  'tuple_meaning': '(定义数, 使用点数, 无材质使用点数, 未使用定义数)',
                  'scope': '只证明四类情形形态不同；不证明同一文件内逐点区分'})
    assert len(set(signatures.values())) == len(signatures), (
        '四种"没有材质"的情形在产品里无法区分：%r' % signatures)
    assert signatures['obj_missing_mtl'][0] == 0, '缺 MTL 时不应有材质定义'
    assert signatures['obj_no_usemtl'][2] == 2


def test_missing_mtl_is_reported_as_a_missing_library(write_report):
    """The message has to name the missing library, not only "no material"."""
    catalog = _inspect(str(FIXTURE_ROOT / 'obj_missing_mtl' / 'model.obj'))
    text = ' '.join(str(item) for item in catalog.unresolved)
    write_report(D1_DIR, 'missing-mtl-notes.json',
                 {'unresolved': list(catalog.unresolved),
                  'definitions': len(catalog.definitions),
                  'sites': len(catalog.binding_sites)})
    assert 'materials.mtl' in text, (
        '缺材质库没有被指名：%r' % (list(catalog.unresolved),))


# ── mixed file: every site present, states told apart per site ───────────
def test_mixed_assignment_keeps_every_site(write_report):
    """One file with an unassigned part, a dangling name and a real binding.

    This is the claim that *can* be signed off today: no usage site disappears.
    """
    facts = _expectation('obj_mixed_assignment')
    catalog = _inspect(str(FIXTURE_ROOT / 'obj_mixed_assignment' / 'model.obj'))
    write_report(D1_DIR, 'mixed-assignment-sites.json',
                 {'expected_sites': facts['sites'],
                  'expected_states': facts['assignment'],
                  'product_sites': [{'id': b.binding_id,
                                     'material': b.original_material_id,
                                     'unknown': getattr(b, 'unknown_json', '')}
                                    for b in catalog.binding_sites]})
    assert len(catalog.binding_sites) == len(facts['sites']), (
        '期望 %d 个使用点，产品 %d 个' % (len(facts['sites']),
                                          len(catalog.binding_sites)))
    bound = [b for b in catalog.binding_sites if b.original_material_id]
    unbound = [b for b in catalog.binding_sites if not b.original_material_id]
    assert len(bound) == 1 and len(unbound) == 2, (
        '混合文件里应当 1 个已绑定、2 个无材质，产品给出 %d / %d'
        % (len(bound), len(unbound)))


def _product_site_state(binding):
    """Whatever the product exposes about *why* a site has no material."""
    try:
        payload = json.loads(getattr(binding, 'unknown_json', '') or '{}')
    except ValueError:
        payload = {}
    for key in ('state', 'assignment_state', 'material_state', 'dangling',
                'unresolved', 'missing_definition'):
        if key in payload:
            return payload[key]
    return None


def test_mixed_assignment_states_per_binding(write_report):
    """Unassigned and dangling must be told apart *on the same file*.

    Counting four separate fixtures is not this claim: a reader that records
    both as "no material" passes those counts.  This one needs a per-site state
    in the shared contract, so when the contract has none the item is recorded
    as "waiting for the contract" -- never as passed.
    """
    catalog = _inspect(str(FIXTURE_ROOT / 'obj_mixed_assignment' / 'model.obj'))
    declared = _payload()['fixtures']['obj_mixed_assignment']['declared']
    expected = [(item['site'], item['kind'], item['reference'])
                for item in declared['site_states']]
    product = []
    for binding in catalog.binding_sites:
        product.append({'id': binding.binding_id,
                        'material': binding.original_material_id,
                        'state': _product_site_state(binding),
                        'notes': list(getattr(catalog, 'unresolved', ()))})
    write_report(D1_DIR, 'mixed-assignment-states.json',
                 {'expected': [{'site': site, 'kind': kind, 'reference': ref}
                               for site, kind, ref in expected],
                  'product': product,
                  'contract_state_field': any(item['state'] is not None
                                              for item in product)})
    without_state = [item for item in product if item['state'] is None]
    if without_state:
        pytest.skip(
            '待契约贯通：产品的使用点没有可机读的状态字段（%d/%d 个 binding 只有 '
            'original_material_id=None），无法逐点区分"未指定"与"悬空"；'
            '不按并集签这一项通过。需 BindingSite 提供状态（如 dangling 标记）'
            % (len(without_state), len(product)))
    states = {item['state'] for item in product}
    assert len(states) >= 2, '所有使用点的状态相同，无法区分未指定与悬空：%r' % states


# ── FBX: declarations, candidates, adoption ──────────────────────────────
def _diffuse_use(facts):
    for material in facts['materials']:
        for use in material['textures']:
            if use['slot'] in ('diffuse', 'base_color'):
                return use
    raise AssertionError('期望集里没有漫反射槽')


def test_fbx_expectation_records_declarations_and_adoption(expectation_payload):
    """The expectation side must answer four questions about every texture.

    Which object and field declared it, what each candidate resolves to, which
    one was adopted and why, and -- when nothing exists -- that nothing was
    adopted.  A preferred path string is not an adopted file.
    """
    record = expectation_payload['upstream']['polyhaven_barrel_01']
    use = _diffuse_use(record['facts'])
    assert len(use['declarations']) == 2, (
        '应当记录两条声明（FileName 与 RelativeFilename），实际 %d'
        % len(use['declarations']))
    for declaration in use['declarations']:
        for key in ('object_kind', 'object_id', 'field', 'value', 'absolute'):
            assert key in declaration, '声明缺少 %s' % key
    assert {item['field'] for item in use['declarations']} == {
        'FileName', 'RelativeFilename'}
    assert len(use['candidates']) == 2
    for candidate in use['candidates']:
        for key in ('field', 'value', 'resolved', 'exists', 'digest'):
            assert key in candidate, '候选缺少 %s' % key
    assert use['resource_state'] == 'missing'
    assert use['selected_resource_id'] == '', (
        '本机没有这个文件，却"采用"了它：%r' % use['selected_resource_id'])
    assert use['selected_path'] == '', '没有找到资源时 selected 必须为空'
    assert use['selection_reason'], '没有写明为什么没有采用'


def test_fbx_product_records_declarations_and_adoption(write_report):
    """The product must be able to answer the same four questions."""
    record = _payload()['upstream']['polyhaven_barrel_01']
    use = _diffuse_use(record['facts'])
    other = [item for item in use['declarations']
             if item['field'] != 'RelativeFilename'][0]
    catalog = _inspect(str(SAMPLES_DIR / record['directory'] / record['main_file']))
    texts = []
    for definition in catalog.definitions:
        for texture in definition.textures:
            texts.append({'slot': texture.slot,
                          'locator': getattr(texture, 'source_locator', ''),
                          'resource': getattr(texture, 'resource_id', ''),
                          'extra': getattr(texture, 'unknown_json', '')})
    joined = ' | '.join('%s %s %s' % (item['locator'], item['resource'],
                                      item['extra']) for item in texts)
    write_report(D1_DIR, 'fbx-path-declarations.json',
                 {'expectation': {'declarations': use['declarations'],
                                  'candidates': use['candidates'],
                                  'selected': use['selected_resource_id'],
                                  'reason': use['selection_reason']},
                  'product': texts})
    # Claim A: the second declaration is visible somewhere in the record.
    assert (other['value'] in joined
            or other['field'].lower() in joined.lower()), (
        '产品只记录了一条声明：文件里 %s 的 %s=%r 在产品记录中完全看不到；'
        '「缺图」与「作者路径」都无法追溯。产品记录：%r'
        % (other['object_kind'], other['field'], other['value'], texts[:2]))
    # Claim B: nothing was adopted, so the resource id must be empty.  A
    # preferred path string is not an adopted file.
    assert all(item['resource'] == '' for item in texts), (
        '本机没有这些贴图，产品却给出了资源 id（等于把首选字符串当成已采用文件）：%r'
        % [item for item in texts if item['resource']][:2])


def test_fbx_conflicting_declarations_are_reported(write_report):
    """Two present candidates that differ: the conflict must be visible."""
    facts = _expectation('fbx_path_conflict')
    use = _diffuse_use(facts)
    assert use['candidate_conflict'] is True, '两条都存在的不同候选没有被标为冲突'
    assert use['selected_resource_id'], '存在可用文件却没有采用'
    catalog = _inspect(str(FIXTURE_ROOT / 'fbx_path_conflict' / 'model.fbx'))
    unresolved = ' | '.join(str(item) for item in catalog.unresolved)
    write_report(D1_DIR, 'fbx-path-conflict.json',
                 {'expectation': {'declarations': use['declarations'],
                                  'selected': use['selected_path'],
                                  'conflict': use['candidate_conflict'],
                                  'reason': use['selection_reason']},
                  'product_unresolved': list(catalog.unresolved),
                  'product_textures': [
                      {'slot': t.slot, 'locator': t.source_locator,
                       'resource': t.resource_id}
                      for d in catalog.definitions for t in d.textures]})
    # Claim A: both candidates are visible in the product's record.
    second = [item for item in use['declarations']
              if item['value'] != use['selected_path']]
    joined = ' | '.join(
        '%s %s' % (texture.source_locator, getattr(texture, 'unknown_json', ''))
        for definition in catalog.definitions for texture in definition.textures)
    assert all(item['value'] in joined or item['field'].lower() in joined.lower()
               for item in second), (
        '冲突的另一条候选在产品记录里看不到：%r（产品记录 %r）'
        % ([item['value'] for item in second], joined[:400]))
    # Claim B: the ambiguity itself is flagged.  Two present candidates with
    # different bytes is not the same situation as one present candidate.
    assert 'conflict' in joined.lower() or 'ambigu' in joined.lower(), (
        '两条候选都存在但内容不同，产品没有标记冲突/歧义，'
        '选择结果会随实现静默变化。产品记录：%r' % joined[:400])


def test_fbx_missing_candidates_adopt_nothing(write_report):
    """Both declarations are absent: the product must not pretend otherwise."""
    facts = _expectation('fbx_candidates_all_missing')
    use = _diffuse_use(facts)
    assert use['selected_resource_id'] == ''
    assert use['resource_state'] == 'missing'
    catalog = _inspect(str(FIXTURE_ROOT / 'fbx_candidates_all_missing' / 'model.fbx'))
    records = [{'slot': t.slot, 'locator': t.source_locator,
                'resource': t.resource_id}
               for d in catalog.definitions for t in d.textures]
    write_report(D1_DIR, 'fbx-candidates-missing.json',
                 {'expectation': {'selected': use['selected_resource_id'],
                                  'state': use['resource_state'],
                                  'reason': use['selection_reason']},
                  'product': records})
    diffuse = [item for item in records if item['slot'] == 'basecolor']
    assert diffuse, '产品的贴图记录里没有漫反射槽'
    assert all(item['resource'] == '' for item in diffuse), (
        '所有候选都不存在，产品却给出了资源 id：%r' % diffuse)
