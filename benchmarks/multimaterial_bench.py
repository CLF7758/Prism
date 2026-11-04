"""Measure the stages of the multimaterial pipeline that exist, honestly.

The rule this script follows is the one from the brief: a number is only worth
recording when the thing being measured is the thing the user waits for.  So
each stage is reported as ``measured`` or ``blocked``, and a blocked stage
prints the reason instead of a placeholder number.  No threshold is invented
here; the thresholds live in ``benchmarks/multimaterial_budget.md`` and were
frozen before this script produced anything.

Today only the *parse* stage is implemented
(``prism.model_library.multimaterial.inspectors.inspect_source``).  It runs on
a background thread in the real design, so a wall-clock measurement of it is a
parse cost, not a user-perceived latency -- that distinction is written into
the output rather than glossed over.

Usage::

    .venv/Scripts/python.exe benchmarks/multimaterial_bench.py
    .venv/Scripts/python.exe benchmarks/multimaterial_bench.py --repeat 50
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTIFACTS = os.path.join(REPO, 'artifacts', 'model-multimaterial', 'd5')
FIXTURES = os.path.join(REPO, 'artifacts', 'model-multimaterial', 'd1', 'fixtures')
SAMPLES = os.path.join(REPO, 'tests', 'model_library', 'acceptance', 'samples')

#: Inputs the parse stage is measured on.  A tiny fixture, a real texture-less
#: glTF, a real single-material GLB and a real multi-material glTF with five
#: external textures: the spread matters more than one model's number.
INPUTS = [
    ('fixture_3_materials', os.path.join(FIXTURES, 'gltf_three_distinct', 'model.gltf')),
    ('fixture_shared_4_sites', os.path.join(FIXTURES, 'gltf_four_parts_share_one',
                                           'model.gltf')),
    ('real_damagedhelmet_glb', os.path.join(SAMPLES, 'khronos_damagedhelmet_glb',
                                            'DamagedHelmet.glb')),
    ('real_alphablend_6_materials', os.path.join(
        SAMPLES, 'khronos_alphablendmodetest_gltf', 'AlphaBlendModeTest.gltf')),
    ('real_fbx_binary', os.path.join(SAMPLES, 'polyhaven_barrel_01',
                                     'fbx_1k_Barrel_01_Barrel_01_1k.fbx')),
]

#: Stages the brief requires.  ``blocked`` until the service reports them.
BLOCKED_STAGES = {
    'select_material': 'services.capabilities().edit_material 为 False：没有暖选材质路径',
    'draft_prepare': 'services.capabilities().edit_material 为 False：草稿准备未接入',
    'native_install': 'services.capabilities().edit_material 为 False：原生局部材质安装未实现',
    'first_frame': '原生安装不存在，首帧无从测量',
    'commit': 'MultiMaterialService.commit 抛 ModelFeatureUnavailable',
    'cover': '正式封面只接受已提交状态，目前没有提交',
    'save': '没有状态可保存',
    'exit': '没有状态可退出',
}


def _percentiles(values):
    ordered = sorted(values)
    if not ordered:
        return {}
    def pick(fraction):
        index = min(len(ordered) - 1, int(round(fraction * (len(ordered) - 1))))
        return ordered[index]
    return {'p50_ms': round(pick(0.50) * 1000, 3),
            'p95_ms': round(pick(0.95) * 1000, 3),
            'p99_ms': round(pick(0.99) * 1000, 3),
            'max_ms': round(ordered[-1] * 1000, 3),
            'min_ms': round(ordered[0] * 1000, 3)}


def measure_parse(path, repeat):
    """Wall clock of one parse call, cold (first) and warm (rest)."""
    from prism.model_library.multimaterial.inspectors import inspect_source
    if not os.path.isfile(path):
        return {'status': 'blocked', 'reason': '样本不存在：%s' % path}
    timings = []
    definitions = None
    error = None
    for _ in range(repeat):
        started = time.perf_counter()
        try:
            catalog = inspect_source(path)
            definitions = len(catalog.definitions)
        except Exception as failure:                     # noqa: BLE001 - recorded
            error = '%s: %s' % (type(failure).__name__, failure)
            break
        timings.append(time.perf_counter() - started)
    if error:
        return {'status': 'blocked', 'reason': error}
    return {
        'status': 'measured',
        'basis': 'wall clock of inspect_source on the calling thread; '
                 'in the product this runs on a background worker, so this is '
                 'a parse cost, not user-perceived latency',
        'samples': len(timings),
        'definitions': definitions,
        'cold': _percentiles([timings[0]]),
        'warm': _percentiles(timings[1:]),
        'bytes': os.path.getsize(path),
    }


#: The synthetic catalogue set.
#:
#: What it measures is only ``catalog_to_payload`` -> JSON encode/decode ->
#: ``catalog_from_payload``.  It touches **no** database: no index, no query,
#: no write, no recovery.  So it is a *catalogue codec* benchmark and must not
#: be used to judge storage layout or 100k-scale index performance.  The
#: numbers stay as they are; only the name is corrected.
SYNTHETIC_SIZES = (1, 10, 100, 1000)


def _synthetic_catalog(materials):
    """A catalogue with ``materials`` definitions, each used exactly once."""
    from prism.model_library.multimaterial import contracts
    definitions = []
    bindings = []
    for index in range(materials):
        material_id = 'gltf:material:%d' % index
        definitions.append(contracts.MaterialDefinition(
            material_id=material_id, source_key='src:%d' % index,
            name='M%d' % index, workflow='pbr_metallic_roughness',
            textures=(contracts.TextureUse(
                slot='basecolor', resource_id='sha256:%064x' % index,
                channels='RGB', texcoord=0, color_space='srgb'),),
            properties_json='{"metallic":0.0,"roughness":0.8}'))
        bindings.append(contracts.BindingSite(
            binding_id='binding:%d' % index, source_key='site:%d' % index,
            node_path='/part-%d' % index, geometry_key='mesh:%d' % index,
            primitive_key='primitive:0', original_material_id=material_id))
    return contracts.MaterialCatalog(
        schema_version=1, revision=1, source_digest='sha256:synthetic',
        parser_id='synthetic-index', definitions=tuple(definitions),
        binding_sites=tuple(bindings))


def measure_synthetic_catalog_codec(repeat):
    """Catalogue payload codec cost per size.  Not a storage measurement."""
    import json
    from prism.model_library.multimaterial import catalog as catalog_module
    results = {}
    for materials in SYNTHETIC_SIZES:
        catalog = _synthetic_catalog(materials)
        timings = []
        size = 0
        for _ in range(repeat):
            started = time.perf_counter()
            payload = catalog_module.catalog_to_payload(catalog)
            text = json.dumps(payload, ensure_ascii=False)
            reloaded = catalog_module.catalog_from_payload(json.loads(text))
            timings.append(time.perf_counter() - started)
            size = len(text.encode('utf-8'))
            if len(reloaded.definitions) != materials:      # pragma: no cover
                return {'status': 'failed',
                        'reason': '往返后定义数从 %d 变成 %d'
                                  % (materials, len(reloaded.definitions))}
        results[str(materials)] = {
            'status': 'measured',
            'basis': 'synthetic-catalog-codec',
            'is_storage_measurement': False,
            'materials': materials,
            'runs': repeat,
            'payload_bytes': size,
            'bytes_per_material': round(size / materials, 1),
            'percentiles': _percentiles(timings),
            'cannot_replace': ('真实混合数据：合成集的分配完全均衡、无缺图、'
                              '无共享贴图、无历史长度'),
            'does_not_cover': ('数据库索引、查询、写入、恢复、WAL、'
                              '单槽读写放大、工程增长'),
        }
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repeat', type=int, default=20)
    arguments = parser.parse_args(argv)
    sys.path.insert(0, REPO)

    results = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'machine': {'platform': platform.platform(),
                    'python': sys.version.split()[0],
                    'budget': 'benchmarks/multimaterial_budget.md',
                    'load': 'single process, no other measurement running'},
        'stages': {},
    }
    results['stages']['parse'] = {
        name: measure_parse(path, arguments.repeat) for name, path in INPUTS}
    for stage, reason in BLOCKED_STAGES.items():
        results['stages'][stage] = {'status': 'blocked', 'reason': reason}
    # 合成清单编解码成绩单独一节，绝不与真实样本混在一起。
    results['synthetic_catalog_codec'] = {
        'name': 'synthetic-catalog-codec（合成清单编解码）',
        'measures': 'catalog_to_payload → JSON 编解码 → catalog_from_payload',
        'is_storage_measurement': False,
        'does_not_cover': ('数据库索引、查询、写入、恢复、WAL、单槽读写放大、'
                          '工程增长、十万级索引性能'),
        'cannot_replace': ('真实混合集：分配不均、缺图、共享贴图、历史长度、'
                          '十万级真实模型'),
        'sizes': measure_synthetic_catalog_codec(max(3, arguments.repeat // 4)),
    }

    os.makedirs(ARTIFACTS, exist_ok=True)
    target = os.path.join(ARTIFACTS, 'parse-baseline.json')
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(results, stream, ensure_ascii=False, indent=2)

    for name, record in results['stages']['parse'].items():
        if record['status'] == 'measured':
            print('%-30s cold p50 %7.3f ms   warm p50 %7.3f ms  p95 %7.3f ms  (%d runs)'
                  % (name, record['cold']['p50_ms'], record['warm']['p50_ms'],
                     record['warm']['p95_ms'], record['samples']))
        else:
            print('%-30s blocked: %s' % (name, record['reason']))
    print('blocked stages: %s' % ', '.join(BLOCKED_STAGES))
    print('-- synthetic-catalog-codec（合成清单编解码，NOT a storage measurement）--')
    for size, record in results['synthetic_catalog_codec']['sizes'].items():
        if record['status'] != 'measured':
            print('  %-6s materials  %s' % (size, record['reason']))
            continue
        print('  %-6s materials  payload %8d bytes (%6.1f B/material)  '
              'round-trip p50 %6.3f ms'
              % (size, record['payload_bytes'], record['bytes_per_material'],
                 record['percentiles']['p50_ms']))
    print('written:', target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
