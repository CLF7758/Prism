"""D1: the expectation set is real, independent and covers the categories.

These checks are about the **fixture**, not about the product. A sample that
is not on disk, not hashed or not licensed cannot be an expectation, and a
sample that the product's own importer produced cannot be one either.
"""
from __future__ import annotations

import json

import pytest

from .conftest import (MIN_SAMPLES_PER_FORMAT, MIN_TOTAL_UPSTREAM_SAMPLES,
                       REQUIRED_CATEGORIES, main_file, sample_path)
from . import reference


def test_the_expectation_set_has_enough_real_samples(upstream_samples):
    assert len(upstream_samples) >= MIN_TOTAL_UPSTREAM_SAMPLES, (
        '真实样本不足：%d < %d' % (len(upstream_samples), MIN_TOTAL_UPSTREAM_SAMPLES))


def test_every_format_has_enough_real_samples(upstream_samples):
    counts = {}
    for sample in upstream_samples:
        counts[sample['format']] = counts.get(sample['format'], 0) + 1
    missing = []
    for fmt, floor in MIN_SAMPLES_PER_FORMAT.items():
        if counts.get(fmt, 0) < floor:
            missing.append('%s: %d < %d' % (fmt, counts.get(fmt, 0), floor))
    assert not missing, '每种格式的真实样本不足：' + '; '.join(missing)


def test_every_listed_file_is_on_disk_with_the_recorded_hash(upstream_samples,
                                                             derived_samples):
    """The manifest is not a promise: every hash is recomputed here."""
    problems = []
    for sample in upstream_samples + derived_samples:
        directory = sample_path(sample)
        for entry in sample.get('files', []):
            path = directory / entry['relpath']
            if not path.is_file():
                problems.append('%s: 缺少 %s' % (sample['sample_id'], entry['relpath']))
                continue
            size = path.stat().st_size
            if size != entry['bytes']:
                problems.append('%s/%s: 大小 %d != %d' % (
                    sample['sample_id'], entry['relpath'], size, entry['bytes']))
                continue
            digest = reference.sha256_file(path)
            if digest != entry['sha256']:
                problems.append('%s/%s: sha256 不一致' % (sample['sample_id'], entry['relpath']))
    assert not problems, '样本清单与磁盘不一致：\n' + '\n'.join(problems[:40])


def test_downloaded_bytes_match_the_upstream_git_blob(upstream_samples):
    """A download is proven, not assumed."""
    checked = 0
    problems = []
    for sample in upstream_samples:
        directory = sample_path(sample)
        for entry in sample.get('files', []):
            expected = entry.get('git_blob_sha1_expected')
            if not expected:
                continue
            path = directory / entry['relpath']
            if not path.is_file():
                continue
            actual = reference.git_blob_sha1(path)
            checked += 1
            if actual != expected:
                problems.append('%s/%s: git blob %s != %s' % (
                    sample['sample_id'], entry['relpath'], actual, expected))
    assert checked, '没有任何文件带有上游 git blob 校验值，完整性无法证明'
    assert not problems, '下载内容与上游不一致：\n' + '\n'.join(problems[:20])


def test_every_sample_states_a_license_with_evidence(inventory):
    problems = []
    for sample in inventory.get('samples', []):
        license_name = (sample.get('license') or '').strip()
        if not license_name:
            problems.append('%s: 没有许可' % sample['sample_id'])
            continue
        if not (sample.get('license_file') or sample.get('license_evidence')):
            problems.append('%s: 许可没有证据' % sample['sample_id'])
        if license_name == 'unverified-upstream-test-fixture' \
                and not (sample.get('notes') or '').strip():
            problems.append('%s: 许可未核实，必须写明后果' % sample['sample_id'])
    assert not problems, '许可记录不完整：\n' + '\n'.join(problems[:30])


def test_the_required_categories_are_covered(inventory):
    """D1 lists the categories the set has to contain."""
    covered = set()
    for sample in inventory.get('samples', []):
        covered.update(sample.get('categories', []))
    missing = [name for name in REQUIRED_CATEGORIES if name not in covered]
    assert not missing, '缺少类别：' + ', '.join(
        '%s(%s)' % (name, REQUIRED_CATEGORIES[name]) for name in missing)


def test_reference_readers_read_every_real_sample(upstream_samples):
    """The independent readers have to work on the real files.

    A sample the inventory marks as damaged is the inverse case: the reader
    must *refuse* it and say why. Silence on either side would let a broken
    fixture or a broken reader pass as normal.
    """
    problems = []
    unreadable = {'corrupt_file', 'unreadable_container'}
    for sample in upstream_samples:
        path = main_file(sample)
        if path is None:
            problems.append('%s: 没有可读的主文件' % sample['sample_id'])
            continue
        facts = reference.read_facts(path)
        if unreadable & set(sample.get('categories', [])):
            if facts.ok and not facts.missing:
                problems.append('%s: 样本标称损坏，参考读取器却读成了有效模型'
                                % sample['sample_id'])
            elif not facts.error:
                problems.append('%s: 判定损坏却没有给出原因' % sample['sample_id'])
            continue
        if not facts.ok:
            problems.append('%s: 参考读取失败 %s' % (sample['sample_id'], facts.error))
    assert not problems, '参考读取器与真实样本不一致：\n' + '\n'.join(problems[:30])


def test_sample_ids_are_unique(inventory):
    """One id, one directory. Duplicates make every comparison meaningless."""
    seen = {}
    for sample in inventory.get('samples', []):
        seen.setdefault(sample['sample_id'], []).append(sample['directory'])
    duplicated = {name: dirs for name, dirs in seen.items() if len(dirs) > 1}
    assert not duplicated, '样本 id 重复：%r' % duplicated


def test_expectations_match_a_fresh_independent_read(inventory, expectations):
    """The stored expectations must equal what the reference reader says now.

    A hand-edited expectation file would otherwise survive every re-run.
    """
    stored = expectations.get('samples', {})
    problems = []
    for sample in inventory.get('samples', []):
        sample_id = sample['sample_id']
        if sample_id not in stored:
            problems.append('%s: 期望集中没有' % sample_id)
            continue
        path = main_file(sample)
        if path is None:
            continue
        facts = reference.read_facts(path)
        expected = stored[sample_id]
        for field in ('dependencies', 'missing', 'embedded', 'vertices', 'triangles',
                      'polygons', 'meshes', 'materials', 'instances', 'has_skin',
                      'has_animation', 'has_instancing', 'has_embedded_media',
                      'has_external_media'):
            want = expected.get(field)
            got = getattr(facts, field)
            # JSON has no tuples: compare the *values*, never the container.
            want = list(want) if isinstance(want, (list, tuple)) else want
            got = list(got) if isinstance(got, (list, tuple)) else got
            if want != got:
                problems.append('%s.%s: 期望 %r 实际 %r' % (sample_id, field, want, got))
    assert not problems, '期望集与独立读取结果不一致：\n' + '\n'.join(problems[:40])


def test_the_missing_dependency_fixture_reports_the_missing_file(inventory,
                                                                sample_by_id):
    """A broken reference has to be reported as broken, not as complete."""
    candidates = [sample for sample in inventory.get('samples', [])
                  if 'missing_file' in sample.get('categories', [])
                  or 'missing_dependency' in sample.get('categories', [])]
    assert candidates, '没有缺文件样本'
    problems = []
    for sample in candidates:
        path = main_file(sample)
        facts = reference.read_facts(path)
        if not facts.missing:
            problems.append('%s: 参考读取器没有发现缺失（%s）'
                            % (sample['sample_id'], facts.error or 'no error'))
    assert not problems, '缺文件样本没有被参考读取器发现缺失：\n' + '\n'.join(problems)


def test_the_corrupt_fixture_is_reported_as_corrupt(inventory):
    """A truncated file must not read as a valid model."""
    candidates = [sample for sample in inventory.get('samples', [])
                  if 'corrupt_file' in sample.get('categories', [])]
    assert candidates, '没有坏文件样本'
    for sample in candidates:
        path = main_file(sample)
        facts = reference.read_facts(path)
        assert not facts.ok or facts.missing, (
            '%s: 坏文件被读成了有效模型（v=%s tri=%s）'
            % (sample['sample_id'], facts.vertices, facts.triangles))
        assert facts.error, (
            '%s: 判定损坏却没有给出原因；用户无法定位坏在哪一步' % sample['sample_id'])


def test_the_same_name_texture_fixture_is_ambiguous_by_name(inventory):
    """Two textures with one name must not be resolvable by name alone."""
    candidates = [sample for sample in inventory.get('samples', [])
                  if 'same_name_texture_two_dirs' in sample.get('categories', [])]
    assert candidates, '没有多目录同名纹理样本'
    sample = candidates[0]
    directory = sample_path(sample)
    names = {}
    for path in directory.rglob('*.png'):
        names.setdefault(path.name, []).append(path)
    repeated = {name: paths for name, paths in names.items() if len(paths) > 1}
    assert repeated, '样本里没有同名不同目录的贴图'
    digests = set()
    for paths in repeated.values():
        for path in paths:
            digests.add(reference.sha256_file(path))
    assert len(digests) > 1, '同名贴图内容相同，无法证明按名字解析会出错'


def test_the_manifest_records_its_own_generator_and_time(inventory):
    assert inventory.get('schema_version') == 1
    assert inventory.get('generated_utc')
    assert inventory.get('generator')
    assert inventory.get('sources'), '样本来源没有被记录'
    for source in inventory['sources']:
        assert source.get('repo')
        assert source.get('license')
