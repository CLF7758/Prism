"""Write the independent expectation set.

For every sample the reference readers say what the file contains: which
files it refers to, which of those are missing, and what geometry it
declares. The result is stored next to the samples so a test can compare
the product against it -- and the test re-computes the same numbers, so a
hand-edited expectation file cannot survive a re-run.

    .venv/Scripts/python.exe tests/model_library/acceptance/build_expectations.py
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(HERE, '..', '..', '..')))

from tests.model_library.acceptance import reference  # noqa: E402
from tests.model_library.acceptance.conftest import main_file  # noqa: E402

SAMPLES = os.path.join(HERE, 'samples')
INVENTORY = os.path.join(SAMPLES, 'INVENTORY.json')
EXPECTATIONS = os.path.join(SAMPLES, 'EXPECTATIONS.json')

#: The fields a reader is expected to report. A reader that cannot answer
#: one of them says ``None`` -- it never guesses.
REPORTED = ('vertices', 'triangles', 'polygons', 'lines', 'points', 'meshes',
            'materials', 'instances', 'texture_count', 'has_skin',
            'has_animation', 'has_instancing', 'has_embedded_media',
            'has_external_media', 'primitive_modes', 'uv_layers')


def main():
    with open(INVENTORY, 'r', encoding='utf-8') as stream:
        inventory = json.load(stream)
    samples = {}
    for sample in inventory.get('samples', []):
        path = main_file(sample)
        if path is None:
            continue
        closure, facts = reference.dependency_closure(path)
        entry = {
            'main_file': os.path.relpath(path, os.path.join(SAMPLES, sample['directory'])
                                         ).replace('\\', '/'),
            'format': facts.format,
            'ok': facts.ok,
            'error': facts.error,
            'basis': facts.basis,
            # ``dependencies`` is what the file *refers to*; ``closure`` adds
            # the file itself, which is what an importer has to copy.
            'dependencies': list(facts.dependencies),
            'closure': sorted(closure),
            'missing': list(facts.missing),
            'embedded': list(facts.embedded),
        }
        for name in REPORTED:
            entry[name] = getattr(facts, name)
            if isinstance(entry[name], tuple):
                entry[name] = list(entry[name])
        entry['extra'] = facts.extra
        samples[sample['sample_id']] = entry
    payload = {
        'schema': 'model_expectations/1',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'generator': 'tests/model_library/acceptance/build_expectations.py',
        'reader': 'tests/model_library/acceptance/reference.py',
        'independence': ('The readers parse the file bytes themselves. They do not '
                         'call the Prism importer, the exporter or F3D, and they never '
                         'read a byte of a model the product produced.'),
        'basis_note': ('vertices/triangles/polygons are what the FILE declares '
                       '(basis=file); a loader may report different numbers after '
                       'it applies transforms, splits or instance expansion.'),
        'samples': samples,
    }
    with open(EXPECTATIONS, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    readable = sum(1 for entry in samples.values() if entry['ok'])
    write_summary(inventory, samples)
    print('expectations for %d samples (%d readable) -> %s'
          % (len(samples), readable, EXPECTATIONS))
    return 0


def write_summary(inventory, samples):
    """D1 deliverable: what the expectation set is, and what it does not cover.

    Written next to the raw measurements so a reader of the report can check
    the coverage claim without re-running anything, and so an uncovered
    category is stated instead of being left to inference.
    """
    artifacts = os.path.abspath(os.path.join(HERE, '..', '..', '..',
                                             'artifacts', 'model-library-acceptance'))
    os.makedirs(artifacts, exist_ok=True)
    by_format = {}
    by_kind = {}
    categories = {}
    uncovered_required = []
    for sample in inventory.get('samples', []):
        by_format[sample['format']] = by_format.get(sample['format'], 0) + 1
        by_kind[sample['provenance_kind']] = by_kind.get(sample['provenance_kind'], 0) + 1
        for category in sample.get('categories', []):
            categories.setdefault(category, []).append(sample['sample_id'])
    try:
        from tests.model_library.acceptance.conftest import REQUIRED_CATEGORIES
        uncovered_required = [name for name in REQUIRED_CATEGORIES
                              if name not in categories]
    except Exception:
        uncovered_required = ['not-checked']
    unreadable = [name for name, entry in samples.items() if not entry['ok']]
    files = []
    for sample in inventory.get('samples', []):
        for entry in sample.get('files', []):
            files.append({'sample_id': sample['sample_id'],
                          'relpath': entry['relpath'],
                          'bytes': entry['bytes'],
                          'sha256': entry['sha256'],
                          'integrity': entry.get('integrity')})
    payload = {
        'schema': 'model_library_d1/1',
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'inventory': os.path.relpath(INVENTORY, os.path.dirname(artifacts)),
        'expectations': os.path.relpath(EXPECTATIONS, os.path.dirname(artifacts)),
        'reference_reader': 'tests/model_library/acceptance/reference.py',
        'independence': ('读取器只解析文件字节，不调用导入器/导出器/F3D，'
                         '也不读产品产出的任何字节。'),
        'sample_count': len(inventory.get('samples', [])),
        'expected_count': len(samples),
        'readable': len(samples) - len(unreadable),
        'unreadable': unreadable,
        'by_format': by_format,
        'by_provenance': by_kind,
        'categories': {name: len(ids) for name, ids in sorted(categories.items())},
        'required_categories_uncovered': uncovered_required,
        'total_bytes': sum(entry['bytes'] for entry in files),
        'file_count': len(files),
        'files': files,
    }
    target = os.path.join(artifacts, 'd1-expectation-set.json')
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
    return target


if __name__ == '__main__':
    sys.exit(main())
