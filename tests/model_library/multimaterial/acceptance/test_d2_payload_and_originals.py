"""D2/D3: the two parts of the journey that *can* be judged today.

The full journey (edit a material, replace from the library, undo, save,
reopen, move the project, export the original) needs a committed state, and
the service has none.  But two pieces of it do not need the renderer:

* **原件哈希不变** -- parsing must not rewrite, rewrite-in-place or "normalise"
  an original file.  That is checkable now and it is a hard requirement.
* **清单载荷往返无损** -- the catalog payload that will be stored in the
  project must survive ``catalog_to_payload`` -> ``catalog_from_payload``
  without losing definitions, bindings, texture uses or unknown fields.  A
  lossy payload cannot be made lossless later by the save path.

Both are recorded as *partial* evidence: they say nothing about editing.
"""
from __future__ import annotations

import json
import os

import pytest

from .conftest import D1_DIR, SAMPLES_DIR


def _all_inputs():
    """Every file the parse gate reads: fixtures plus the upstream samples."""
    paths = []
    if D1_DIR.is_dir():
        for entry in sorted(os.listdir(str(D1_DIR / 'fixtures'))):
            directory = D1_DIR / 'fixtures' / entry
            for name in sorted(os.listdir(str(directory))):
                paths.append(str(directory / name))
    if SAMPLES_DIR.is_dir():
        for entry in sorted(os.listdir(str(SAMPLES_DIR))):
            directory = SAMPLES_DIR / entry
            if not directory.is_dir():
                continue
            for name in sorted(os.listdir(str(directory))):
                paths.append(str(directory / name))
    return [path for path in paths if os.path.isfile(path)]


def _hash_tree(paths):
    from hashlib import sha256
    digests = {}
    for path in paths:
        with open(path, 'rb') as stream:
            digests[path] = sha256(stream.read()).hexdigest()
    return digests


def test_parsing_never_changes_an_original_file(write_report):
    """The original bytes are the user's asset: parsing must leave them alone."""
    from prism.model_library.multimaterial.inspectors import inspect_source
    paths = _all_inputs()
    assert paths, '没有可核对的样本'
    before = _hash_tree(paths)
    inspected = 0
    for path in paths:
        if os.path.splitext(path)[1].lower() not in ('.gltf', '.glb', '.obj', '.fbx'):
            continue
        try:
            inspect_source(path)
            inspected += 1
        except Exception:                              # noqa: BLE001 - reported
            pass                                       # a broken file may refuse
    after = _hash_tree(paths)
    changed = sorted(path for path in before if before[path] != after[path])
    write_report(D1_DIR, 'original-integrity.json',
                 {'files': len(paths), 'inspected': inspected,
                  'changed': changed,
                  'verdict': '原件未被改写' if not changed else '原件被改写'})
    assert inspected > 0, '一次解析都没有发生，这个检查没有意义'
    assert changed == [], '解析改动了原件：%s' % changed


def _catalog_cases():
    from prism.model_library.multimaterial.inspectors import inspect_source
    cases = []
    for name in sorted(os.listdir(str(D1_DIR / 'fixtures'))):
        directory = D1_DIR / 'fixtures' / name
        for candidate in ('model.gltf', 'model.glb', 'model.obj'):
            path = directory / candidate
            if path.is_file():
                cases.append((name, str(path)))
                break
    return cases


@pytest.mark.parametrize('label,path', _catalog_cases(),
                         ids=lambda value: os.path.basename(str(value)))
def test_catalog_payload_round_trip_is_lossless(label, path, write_report):
    """What will be stored must survive a store/load cycle without loss.

    This is the payload layer only -- it does not prove the project save path
    calls it, and it does not prove a reopen.  It proves the shape is not
    lossy by construction.
    """
    from prism.model_library.multimaterial import catalog as catalog_module
    from prism.model_library.multimaterial.inspectors import inspect_source
    try:
        catalog = inspect_source(path)
    except Exception as error:                         # noqa: BLE001 - expected
        pytest.skip('该夹具按设计不可读：%s: %s' % (type(error).__name__, error))

    payload = catalog_module.catalog_to_payload(catalog)
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    reloaded = catalog_module.catalog_from_payload(json.loads(text))

    problems = []
    if len(reloaded.definitions) != len(catalog.definitions):
        problems.append('定义数 %d -> %d' % (len(catalog.definitions),
                                            len(reloaded.definitions)))
    if len(reloaded.binding_sites) != len(catalog.binding_sites):
        problems.append('使用点数 %d -> %d' % (len(catalog.binding_sites),
                                              len(reloaded.binding_sites)))
    for left, right in zip(catalog.definitions, reloaded.definitions):
        for attribute in ('material_id', 'name', 'workflow', 'properties_json',
                          'unknown_json'):
            if getattr(left, attribute, None) != getattr(right, attribute, None):
                problems.append('定义 %s 的 %s 丢失' % (left.material_id, attribute))
        if len(left.textures) != len(right.textures):
            problems.append('定义 %s 的贴图数 %d -> %d'
                            % (left.material_id, len(left.textures),
                               len(right.textures)))
            continue
        for first, second in zip(left.textures, right.textures):
            for attribute in ('slot', 'resource_id', 'channels', 'texcoord',
                              'color_space', 'normal_convention', 'unknown_json'):
                if getattr(first, attribute, None) != getattr(second, attribute, None):
                    problems.append('定义 %s 的 %s.%s 丢失'
                                    % (left.material_id, first.slot, attribute))
    for left, right in zip(catalog.binding_sites, reloaded.binding_sites):
        for attribute in ('binding_id', 'node_path', 'geometry_key',
                          'primitive_key', 'original_material_id'):
            if getattr(left, attribute, None) != getattr(right, attribute, None):
                problems.append('使用点 %s 的 %s 丢失'
                                % (left.binding_id, attribute))
    write_report(D1_DIR, 'payload-round-trip-%s.json' % label,
                 {'fixture': label, 'problems': problems,
                  'definitions': len(catalog.definitions),
                  'bindings': len(catalog.binding_sites)})
    assert problems == [], '%s 载荷往返有损：%s' % (label, problems)
