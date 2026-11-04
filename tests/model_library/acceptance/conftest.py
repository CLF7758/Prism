"""Fixtures for the independent model-library acceptance suite.

The suite is split into two kinds of file:

* the **expectation set** under ``samples/`` -- real model files with their
  provenance, license and hashes, plus the facts an independent reference
  reader derived from them.
* the **checks** -- scripts that compare the product against that expectation
  set, and the log/diagnostic contract they have to satisfy.

Nothing in this package writes into the user's project, settings, clipboard
or capture service. Every test runs against a temporary project.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

ACCEPTANCE_DIR = Path(__file__).resolve().parent
PACKAGE_DIR = ACCEPTANCE_DIR.parent
TESTS_DIR = PACKAGE_DIR.parent
REPO_ROOT = TESTS_DIR.parent
SAMPLES_DIR = ACCEPTANCE_DIR / 'samples'
INVENTORY_PATH = SAMPLES_DIR / 'INVENTORY.json'
EXPECTATIONS_PATH = SAMPLES_DIR / 'EXPECTATIONS.json'
ARTIFACTS_DIR = REPO_ROOT / 'artifacts' / 'model-library-acceptance'

#: The D1 categories the expectation set has to cover.
REQUIRED_CATEGORIES = {
    'embedded_texture': '内嵌纹理',
    'external_texture': '外置纹理',
    'obj_with_mtl': 'OBJ+MTL',
    'gltf_with_bin': 'glTF+BIN',
    'same_name_texture_two_dirs': '多目录同名纹理',
    'space_or_chinese_path': '中文/空格路径',
    'multi_mesh': '多网格',
    'multi_material': '多材质',
    'instancing': '实例',
    'points_and_lines': '点线',
    'n_gon': '多边面',
    'transparency': '透明',
    'normal_orientation': '法线方向',
    'orm': 'ORM',
    'emissive': '发光',
    'fbx_skeleton_or_animation': 'FBX骨骼/动画',
    'missing_file': '缺文件',
    'corrupt_file': '坏文件',
}

#: How many real (upstream) samples each format has to provide.
MIN_SAMPLES_PER_FORMAT = {'gltf': 6, 'glb': 6, 'obj': 6, 'fbx': 6}
MIN_TOTAL_UPSTREAM_SAMPLES = 24


@pytest.fixture(scope='session')
def samples_dir():
    return SAMPLES_DIR


@pytest.fixture(scope='session')
def repo_root():
    return REPO_ROOT


@pytest.fixture(scope='session')
def artifacts_dir():
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    return ARTIFACTS_DIR


def _load_json(path, what):
    if not path.is_file():
        pytest.skip('%s 尚未生成：%s' % (what, path))
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError as error:
        pytest.fail('%s 不是合法 JSON：%s（%s）' % (what, path, error))


@pytest.fixture(scope='session')
def inventory():
    """The provenance manifest: real files, sources, licenses, hashes."""
    return _load_json(INVENTORY_PATH, '样本清单')


@pytest.fixture(scope='session')
def expectations():
    """The independent expectations, produced by the reference readers."""
    return _load_json(EXPECTATIONS_PATH, '独立期望集')


@pytest.fixture(scope='session')
def upstream_samples(inventory):
    return [sample for sample in inventory.get('samples', [])
            if sample.get('provenance_kind') == 'upstream']


@pytest.fixture(scope='session')
def derived_samples(inventory):
    return [sample for sample in inventory.get('samples', [])
            if sample.get('provenance_kind') == 'derived']


@pytest.fixture(scope='session')
def sample_by_id(inventory):
    return {sample['sample_id']: sample for sample in inventory.get('samples', [])}


def sample_path(sample):
    """The absolute directory of one sample."""
    return SAMPLES_DIR / sample['directory']


def main_file(sample):
    """The model file of one sample -- the one the user would import.

    The extension the sample declares wins: a directory that holds an FBX, an
    OBJ and a glTF of the same asset must not be read through whichever file
    happens to come first in the listing, or the expectation describes a
    different file than the one under test.
    """
    wanted = (sample.get('format') or '').lower()
    models = []
    for entry in sample.get('files', []):
        relative = entry['relpath']
        extension = relative.rsplit('.', 1)[-1].lower()
        if extension in ('gltf', 'glb', 'obj', 'fbx'):
            models.append((extension, relative))
    if not models:
        return None
    for extension, relative in models:
        if extension == wanted:
            return sample_path(sample) / relative
    if wanted == 'mixed':
        for extension, relative in models:
            if extension == 'gltf':
                return sample_path(sample) / relative
    return sample_path(sample) / models[0][1]


@pytest.fixture
def write_raw_result(artifacts_dir):
    """Store a raw measurement next to the report, not only in the chat."""
    def writer(name, payload):
        target = artifacts_dir / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                          encoding='utf-8')
        return target
    return writer
