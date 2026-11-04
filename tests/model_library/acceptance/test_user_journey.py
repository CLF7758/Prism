"""D4/D5: the real journeys, checked against the independent expectation.

The importer, the repository and the exporter belong to another work line.
This module does not touch their code; it **uses** them and compares what
they produced with what the independent reference readers say the file
should contain.

Each scenario records the operation, the observation, the expectation, the
evidence and a verdict. A capability that is not wired yet is recorded as
``blocked`` with the reason -- never as a pass, and never as a failure of
the product.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from . import reference
from .conftest import MIN_SAMPLES_PER_FORMAT, main_file, sample_path

JOB_TIMEOUT_S = 60.0


# ── helpers ───────────────────────────────────────────────────────────────
def wait_for_job(controller, job_id, timeout=JOB_TIMEOUT_S):
    """Wait for one background job and report how it ended."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = controller.job_state(job_id)
        if state in ('completed', 'failed', 'cancelled'):
            return state
        time.sleep(0.05)
    return controller.job_state(job_id) or 'timeout'


def expected_package(path):
    """The file set an importer has to bring in, with the bytes it must keep.

    The key is ``(name, sha256)``: the name alone cannot tell two files
    with the same name in different directories apart, and the hash alone
    cannot tell a dependency that was dropped from one that was kept.
    """
    closure, facts = reference.dependency_closure(path)
    base = os.path.dirname(os.path.abspath(path))
    expected = {}
    for relative in closure:
        resolved = base
        for part in relative.split('/'):
            resolved = os.path.join(resolved, part)
        if os.path.isfile(resolved):
            expected[os.path.basename(resolved)] = reference.sha256_file(resolved)
    return expected, facts


def actual_package(record):
    """The file set the importer actually stored, and the ones it left blank.

    A file record without a path is not a dependency: it is an entry the user
    cannot act on. It is returned separately so a defect of that shape is
    named instead of showing up as "an extra file called ''".
    """
    actual = {}
    anonymous = []
    for item in record.files:
        relative = (item.relative_path or '').strip()
        if not relative:
            anonymous.append({'role': item.role, 'sha256': item.sha256,
                              'missing': getattr(item, 'missing', None),
                              'original_reference': getattr(item, 'original_reference', None)})
            continue
        if getattr(item, 'missing', False):
            continue
        actual[os.path.basename(relative)] = item.sha256
    return actual, anonymous


def missing_entries(record):
    return [getattr(item, 'original_reference', '') or item.role
            for item in record.files if getattr(item, 'missing', False)]


@pytest.fixture
def project_context(tmp_path, qapp):
    """A real project, written by the application's own project writer.

    The GUI "save a new project" path is blocked by a defect in another work
    line (the model table's generated columns make the legacy->managed
    conversion refuse the project; see the D2 baseline). The model library
    itself binds to a project through ``open_model_context``, so the same
    import/export/resolve journey is exercised here against a project the
    application's writer produced. The UI entry point is recorded as
    *blocked* in the acceptance report, not as passed.
    """
    from PyQt6.QtGui import QUndoStack
    from prism.scene import PrismGraphicsScene
    from prism.fileio.sql import SQLiteIO
    from prism.model_library.project_storage import open_model_context
    scene = PrismGraphicsScene(QUndoStack())
    path = tmp_path / 'acceptance.prism'
    writer = SQLiteIO(str(path), scene, create_new=True)
    try:
        writer.write()
    finally:
        writer._close_connection()
    if not path.exists():
        pytest.skip('工程写入器没有产出工程文件；见基线报告')
    context = open_model_context(str(path))
    return context, tmp_path


# ── D4: the import journey ────────────────────────────────────────────────
def test_the_imported_package_matches_the_independent_expectation(project_context,
                                                                  inventory):
    """Every dependency is in, every byte is unchanged, nothing extra."""
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    results = []
    problems = []
    checked = 0
    for sample in inventory.get('samples', []):
        if sample.get('provenance_kind') != 'upstream':
            continue
        path = main_file(sample)
        if path is None or not path.is_file():
            continue
        expected, facts = expected_package(path)
        if not facts.ok:
            continue
        importer = ModelImporter(context, repository)
        events = []
        importer._publish = events.append  # the real listener is the UI's job
        job_id = importer.import_paths((str(path),))
        state = wait_for_job(importer, job_id)
        page = repository.query(_all_query())
        record = None
        for summary in page.items:
            candidate = repository.get(summary.model_id)
            if candidate and candidate.name == Path(path).name:
                record = candidate
        if state != 'completed' or record is None:
            problems.append('%s: 导入未完成（%s），没有产生记录' % (sample['sample_id'], state))
            results.append(dict(sample_id=sample['sample_id'], state=state,
                                files=[], expected=expected, actual={}))
            continue
        actual, anonymous = actual_package(record)
        checked += 1
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(name for name in set(expected) & set(actual)
                         if expected[name] != actual[name])
        if anonymous:
            problems.append('%s: %d 条文件记录没有 relative_path（用户无法定位）：%r'
                            % (sample['sample_id'], len(anonymous), anonymous[:4]))
        if missing or extra or changed:
            problems.append('%s: 缺少 %s / 多余 %s / 字节不同 %s' % (
                sample['sample_id'], missing, extra, changed))
        results.append(dict(sample_id=sample['sample_id'], state=state,
                            files=[{'relative_path': item.relative_path,
                                    'size_bytes': item.size_bytes,
                                    'sha256': item.sha256,
                                    'missing': bool(getattr(item, 'missing', False))}
                                   for item in record.files],
                            expected=expected, actual=actual,
                            missing_files=missing_entries(record)))
    assert checked, '没有任何样本走完导入流程'
    assert not problems, '导入的原件集合与独立期望不一致：\n' + '\n'.join(problems[:30])
    return results


def _all_query():
    from prism.model_library.contracts import ModelQuery
    return ModelQuery(limit=256)


def test_the_imported_record_keeps_every_dependency_of_a_textured_model(project_context,
                                                                         inventory):
    """The OBJ+MTL+texture case, checked file by file."""
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    sample = next((item for item in inventory.get('samples', [])
                   if item['sample_id'] == 'assimp_spider'), None)
    if sample is None:
        pytest.skip('缺少 assimp_spider 样本')
    path = main_file(sample)
    expected, facts = expected_package(path)
    assert 'SpiderTex.jpg' in expected, '参考读取器没有找到贴图，期望集本身有问题'
    importer = ModelImporter(context, repository)
    job_id = importer.import_paths((str(path),))
    assert wait_for_job(importer, job_id) == 'completed'
    page = repository.query(_all_query())
    record = None
    for summary in page.items:
        candidate = repository.get(summary.model_id)
        if candidate and candidate.name == 'spider.obj':
            record = candidate
    assert record is not None, '导入后没有记录'
    actual, anonymous = actual_package(record)
    assert not anonymous, '有 %d 条文件记录没有 relative_path：%r' % (
        len(anonymous), anonymous)
    assert set(actual) == set(expected), '贴图或 MTL 没有被一起导入：%s' % (
        sorted(set(expected) ^ set(actual)))
    for name, digest in expected.items():
        assert actual[name] == digest, '%s 的字节与原件不同' % name


# ── D5: the broken samples must not look complete ─────────────────────────
def test_a_missing_dependency_is_reported_and_not_hidden(project_context, inventory):
    """A broken reference may not become a "ready" model."""
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    sample = next((item for item in inventory.get('samples', [])
                   if item['sample_id'] == 'derived_missing_dependency'), None)
    if sample is None:
        pytest.skip('缺少缺文件样本')
    path = main_file(sample)
    importer = ModelImporter(context, repository)
    job_id = importer.import_paths((str(path),))
    state = wait_for_job(importer, job_id)
    page = repository.query(_all_query())
    record = None
    for summary in page.items:
        candidate = repository.get(summary.model_id)
        if candidate and candidate.name == 'spider.obj':
            record = candidate
    if record is None:
        assert state in ('failed', 'cancelled'), (
            '导入既没有产生记录，也没有报告失败（状态 %s）' % state)
        return
    missing = missing_entries(record)
    assert missing, '缺贴图的模型被当成完整模型存进了工程'
    assert 'SpiderTex.jpg' in ' '.join(missing), '没有指出缺的是哪一张贴图'


def test_a_corrupt_model_is_not_reported_as_successfully_imported(project_context,
                                                                 inventory):
    """A truncated payload may not become a usable model."""
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    sample = next((item for item in inventory.get('samples', [])
                   if item['sample_id'] == 'derived_corrupt_file'), None)
    if sample is None:
        pytest.skip('缺少坏文件样本')
    path = main_file(sample)
    importer = ModelImporter(context, repository)
    events = []
    importer._publish = events.append
    job_id = importer.import_paths((str(path),))
    state = wait_for_job(importer, job_id)
    failures = [event for event in events
                if type(event).__name__ == 'ModelImportFailedEvent']
    if state == 'completed':
        # The import may succeed as a *file copy*; that is only acceptable if
        # the broken payload is reported as broken instead of as a valid model.
        page = repository.query(_all_query())
        record = None
        for summary in page.items:
            candidate = repository.get(summary.model_id)
            if candidate and candidate.name == Path(path).name:
                record = candidate
        assert record is not None
        stats = record.statistics
        assert stats.triangles in (None, 0), (
            '损坏的模型被统计成了 %s 个三角形' % stats.triangles)
    assert state in ('completed', 'failed'), '导入状态异常：%s' % state


def test_every_file_record_says_which_file_it_is(project_context, inventory):
    """Every recorded file names itself, or is explicitly reported missing.

    A record with an empty path and an empty hash is not evidence of a
    dependency: the user cannot see what is missing and the export cannot
    decide what to copy.
    """
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    checked = 0
    problems = []
    for sample in inventory.get('samples', []):
        path = main_file(sample)
        if path is None or not path.is_file():
            continue
        facts = reference.read_facts(path)
        if not facts.ok:
            continue
        importer = ModelImporter(context, repository)
        job_id = importer.import_paths((str(path),))
        state = wait_for_job(importer, job_id)
        if state != 'completed':
            continue
        record = None
        for summary in repository.query(_all_query()).items:
            candidate = repository.get(summary.model_id)
            if candidate and candidate.name == Path(path).name:
                record = candidate
        if record is None:
            continue
        checked += 1
        for item in record.files:
            if getattr(item, 'missing', False):
                if not getattr(item, 'original_reference', ''):
                    problems.append('%s: 标记缺失却没有原始引用名（%r）'
                                    % (sample['sample_id'], item.relative_path))
                continue
            if not (item.relative_path or '').strip():
                problems.append('%s: 无名文件记录（role=%s, sha=%r, missing=%r）'
                                % (sample['sample_id'], item.role,
                                   item.sha256, getattr(item, 'missing', None)))
            elif not item.sha256:
                problems.append('%s: %r 没有 sha256' % (sample['sample_id'],
                                                        item.relative_path))
    assert checked, '没有任何样本被导入'
    assert not problems, '文件记录无法定位：\n' + '\n'.join(problems[:20])


# ── D4: export the originals back out ─────────────────────────────────────
def test_exported_originals_are_byte_identical(project_context, inventory):
    """The export has to reproduce the original bytes, not a preview."""
    context, _directory = project_context
    from prism.model_library.importer import ModelImporter
    from prism.model_library.exporter import ModelOriginalExporter
    from prism.model_library.repository import ModelRepositoryAdapter
    repository = ModelRepositoryAdapter(context)
    sample = next((item for item in inventory.get('samples', [])
                   if item['sample_id'] == 'assimp_spider'), None)
    if sample is None:
        pytest.skip('缺少 assimp_spider 样本')
    path = main_file(sample)
    expected, _facts = expected_package(path)
    importer = ModelImporter(context, repository)
    job_id = importer.import_paths((str(path),))
    assert wait_for_job(importer, job_id) == 'completed'
    model_id = None
    for summary in repository.query(_all_query()).items:
        candidate = repository.get(summary.model_id)
        if candidate and candidate.name == 'spider.obj':
            model_id = summary.model_id
    assert model_id, '找不到导入后的模型'
    destination = Path(context.project_path).parent / 'exported'
    exporter = ModelOriginalExporter(context, repository)
    export_job = exporter.export_originals((model_id,), str(destination))
    state = wait_for_job(exporter, export_job)
    assert state == 'completed', '导出没有完成：%s' % state
    exported = {}
    for base, _dirs, names in os.walk(destination):
        for name in names:
            path_value = Path(base) / name
            exported[name] = reference.sha256_file(path_value)
    for name, digest in expected.items():
        assert name in exported, '导出缺少 %s' % name
        assert exported[name] == digest, '导出的 %s 与原件字节不同' % name
