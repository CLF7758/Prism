"""环境矩阵脚本的定向回归（DPI / 冷缓存 / 长会话内存）。

纪律：
* 全部用 ``tmp_path`` 造临时工程，**不碰** ``%APPDATA%`` 里的真实缓存，
  也不碰 ``E:\\Prism-stress-100k``。
* 真实清缓存的实验只允许手工跑脚本，不进 pytest。
* 断言只覆盖脚本自己的契约；发现产品行为有问题时写进报告，不在这里固化。
"""
from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'tools' / 'material' / 'env_matrix.py'


def _load_module():
    spec = importlib.util.spec_from_file_location('prism_env_matrix', MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


env_matrix = _load_module()


# ────────────────────────── 环境口径 ──────────────────────────


def test_system_memory_reports_total_and_available():
    memory = env_matrix.system_memory()
    assert memory['total_bytes'] > 0
    assert 0 < memory['available_bytes'] <= memory['total_bytes']
    assert 0 <= memory['load_percent'] <= 100


def test_describe_environment_carries_the_required_口径():
    environment = env_matrix.describe_environment()
    for key in ('os', 'python', 'pyqt6', 'qt', 'logical_cpus', 'memory',
                'python_processes'):
        assert key in environment, key
    assert environment['logical_cpus'] >= 1
    assert environment['memory']['total_bytes'] > 0


def test_module_does_not_depend_on_psutil():
    """psutil 本机没装；脚本必须靠 ctypes 而不是它。"""
    import re
    source = MODULE_PATH.read_text(encoding='utf-8')
    assert not re.search(r'^\s*(import|from)\s+psutil', source, re.MULTILINE)


def test_process_memory_bytes_is_available():
    from prism.performance_trace import process_memory_bytes
    value = process_memory_bytes()
    assert value is None or value > 0


# ────────────────────────── 图像度量 ──────────────────────────


def _photo_like(size=512):
    """一张「照片式」的图：平滑渐变 + 确定性高频细节。"""
    from PyQt6 import QtGui
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            gradient = (x * 255) // size
            noise = (x * 7 + y * 13) % 37
            image.setPixelColor(x, y, QtGui.QColor(
                (gradient + noise) % 256, (y * 255) // size, (noise * 5) % 256))
    return image


def test_image_metrics_flags_a_solid_colour_block():
    from PyQt6 import QtGui
    flat = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
    flat.fill(QtGui.QColor('#404080'))
    metrics = env_matrix.image_metrics(flat)
    assert metrics['ok'] is True
    assert metrics['unique_colors'] == 1
    assert metrics['modal_color_ratio'] > 0.99


def test_image_metrics_accepts_a_detailed_image():
    metrics = env_matrix.image_metrics(_photo_like())
    assert metrics['ok'] is True
    assert metrics['unique_colors'] >= 16
    assert metrics['modal_color_ratio'] < 0.90
    assert metrics['max_laplacian'] > 0


def test_real_resample_beats_a_stretched_raster():
    """真采样放大必须比「先缩后拉」更锐 —— 这是清晰度红线的核心判据。"""
    from PyQt6 import QtCore, QtGui
    source = _photo_like(1024)
    logical, dpr = 128, 2
    device = logical * dpr
    target = QtGui.QImage(device, device, QtGui.QImage.Format.Format_RGB32)
    target.setDevicePixelRatio(dpr)
    painter = QtGui.QPainter(target)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QtCore.QRectF(0, 0, logical, logical), source)
    painter.end()
    base = source.scaled(logical, logical, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                         QtCore.Qt.TransformationMode.SmoothTransformation)
    stretched = base.scaled(device, device, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                            QtCore.Qt.TransformationMode.SmoothTransformation)
    render = env_matrix.image_metrics(target)
    stretch = env_matrix.image_metrics(stretched)
    assert render['max_laplacian'] > stretch['max_laplacian']
    assert render['max_laplacian'] / stretch['max_laplacian'] >= 1.05


# ────────────────────────── DPI 判据 ──────────────────────────


def _good_dpi_entry():
    return {
        'status': 'measured', 'dpi': 144, 'platform': 'windows',
        'logical_size': 256, 'device_pixel_size': [384, 384],
        'expected_device_pixel_size': 384, 'pixel_ratio': 1.5,
        'render': {'modal_color_ratio': 0.01, 'unique_colors': 3500},
        'detail_gain_vs_stretch': 5.47, 'edge_run_ratio_vs_stretch': 0.5,
        'widget_grab': {'pixel_size': [384, 384]},
    }


def test_evaluate_dpi_passes_a_correct_entry():
    verdict = env_matrix.evaluate_dpi(_good_dpi_entry())
    assert verdict['verdict'] == 'pass'
    assert verdict['reasons'] == []


def test_evaluate_dpi_fails_when_the_pixel_size_does_not_scale():
    entry = _good_dpi_entry()
    entry['device_pixel_size'] = [256, 256]
    verdict = env_matrix.evaluate_dpi(entry)
    assert verdict['verdict'] == 'fail'
    assert any('设备像素尺寸' in reason for reason in verdict['reasons'])


def test_evaluate_dpi_fails_a_flat_colour_block():
    entry = _good_dpi_entry()
    entry['render'] = {'modal_color_ratio': 0.99, 'unique_colors': 1}
    verdict = env_matrix.evaluate_dpi(entry)
    assert verdict['verdict'] == 'fail'
    assert any('纯色块' in reason for reason in verdict['reasons'])


def test_evaluate_dpi_fails_a_blurry_resample():
    entry = _good_dpi_entry()
    entry['detail_gain_vs_stretch'] = 0.8
    entry['edge_run_ratio_vs_stretch'] = 1.4
    verdict = env_matrix.evaluate_dpi(entry)
    assert verdict['verdict'] == 'fail'
    assert any('不锐' in reason for reason in verdict['reasons'])


def test_evaluate_dpi_reports_no_measurement_instead_of_a_guess():
    verdict = env_matrix.evaluate_dpi({'status': 'failed', 'reason': '子进程超时'})
    assert verdict['verdict'] == env_matrix.NO_MEASUREMENT
    assert '子进程超时' in verdict['reasons'][0]


def test_dpi_child_probe_runs_in_its_own_process():
    """DPI 档必须由独立子进程跑：环境变量在进程启动前设置。"""
    out = Path(os.environ.get('TEMP', '.')) / 'prism-env-matrix-probe.json'
    env = dict(os.environ)
    env['QT_SCALE_FACTOR'] = '1.25'
    command = [sys.executable, str(MODULE_PATH), '_dpi-child', '--dpi', '120',
               '--probe-only', '--out', str(out)]
    completed = subprocess.run(command, env=env, capture_output=True, text=True,
                               timeout=300, encoding='utf-8', errors='replace')
    assert completed.returncode == 0, completed.stderr[-2000:]
    payload = json.loads(out.read_text(encoding='utf-8'))
    assert payload['platform']
    assert payload['device_pixel_ratio'] > 0
    assert payload['env']['QT_SCALE_FACTOR'] == '1.25'


# ────────────────────────── 缓存维度 ──────────────────────────


def test_catalog_cache_path_follows_the_product_rule():
    """目录缓存路径规则来自 prism/view.py:1252-1253。"""
    path = env_matrix.catalog_cache_path(Path('/tmp/settings'), Path('/x/Project.prism'))
    assert path == Path('/tmp/settings') / 'catalog' / 'Project.prism.sqlite'


def test_backup_cache_files_renames_instead_of_deleting(tmp_path):
    cache = tmp_path / 'catalog' / 'demo.prism.sqlite'
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b'0123456789')
    moved = env_matrix.backup_cache_files(cache, 'stamp')
    assert not cache.exists(), '原文件必须被改名备份，不能直接消失'
    assert len(moved) == 1
    assert moved[0]['bytes'] == 10
    backup = Path(moved[0]['backup'])
    assert backup.exists()
    assert backup.read_bytes() == b'0123456789'
    assert moved[0]['mtime']


def _make_source_project(path, records=8, unique=2):
    """造一个最小可用的 Prism 工程（真实 PNG 字节）。"""
    from PyQt6 import QtCore, QtGui
    from prism.fileio.schema import APPLICATION_ID, SCHEMA, USER_VERSION
    connection = sqlite3.connect(path)
    for statement in SCHEMA:
        # SCHEMA 里的语句没有结尾分号，拼在一起会被 sqlite3 拒绝。
        connection.execute(statement)
    connection.executescript('''
        CREATE TABLE prism_metadata (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE material_records (id TEXT PRIMARY KEY, payload TEXT);
        CREATE TABLE workspace_nodes (id TEXT PRIMARY KEY, projectId TEXT, section TEXT,
            nodeType TEXT, parentId TEXT, title TEXT, "order" INTEGER, pageId TEXT, color TEXT,
            deletedAt TEXT);
    ''')
    connection.execute(f'PRAGMA user_version={USER_VERSION}')
    connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
    blobs = []
    for index in range(max(1, unique)):
        image = QtGui.QImage(64, 64, QtGui.QImage.Format.Format_RGB32)
        image.fill(QtGui.QColor(51 + index * 90, 102, 170 - index * 40))
        buffer = QtCore.QBuffer()
        buffer.open(QtCore.QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, 'PNG')
        blobs.append(bytes(buffer.data()))
    for index in range(records):
        payload = json.dumps({'filename': f'img-{index}.png', 'imageWidth': 64,
                              'imageHeight': 64, 'canvasId': 'canvas-0'})
        connection.execute(
            'INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) '
            'VALUES (?,"pixmap",?,?,0,1,0,1,?)', (index + 1, index * 100, 0, payload))
        blob = blobs[index % len(blobs)]
        connection.execute(
            'INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) VALUES (?,?,0,0,?,?)',
            (f'img-{index}.png', index + 1, len(blob), blob))
    connection.execute("INSERT INTO prism_metadata VALUES('project_id','fixture-project')")
    connection.commit()
    connection.close()
    return path


def test_fixture_builder_keeps_records_and_unique_sources_apart(tmp_path):
    source = _make_source_project(tmp_path / 'source.prism', records=8, unique=2)
    destination = tmp_path / 'work' / 'fixture.prism'
    profile = env_matrix.build_fixture(source, destination, records=20,
                                        unique_sources=2, canvases=4, materials=0)
    assert profile['records'] == 20
    assert profile['unique_sources'] == 2
    assert profile['distinct_source_bytes'] > 0
    assert profile['total_record_bytes'] > profile['distinct_source_bytes']
    assert destination.exists()


def test_open_child_measures_cold_then_hot(tmp_path):
    """冷：目录不存在 → 建库；热：目录存在 → 复用。"""
    source = _make_source_project(tmp_path / 'source.prism', records=8, unique=2)
    fixture = tmp_path / 'work' / 'fixture.prism'
    env_matrix.build_fixture(source, fixture, records=40, unique_sources=2,
                             canvases=2, materials=0)
    settings_dir = tmp_path / 'settings'
    cold_out = tmp_path / 'cold.json'
    hot_out = tmp_path / 'hot.json'
    for out, extra in ((cold_out, ['--build']), (hot_out, [])):
        command = [sys.executable, str(MODULE_PATH), '_open-child',
                   '--project', str(fixture), '--settings-dir', str(settings_dir),
                   '--out', str(out)] + extra
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600,
                                   encoding='utf-8', errors='replace')
        assert completed.returncode == 0, completed.stderr[-2000:]
    cold = json.loads(cold_out.read_text(encoding='utf-8'))
    hot = json.loads(hot_out.read_text(encoding='utf-8'))
    assert cold['reused_cache'] is False
    assert cold['catalog_build_seconds'] is not None
    assert cold['open_to_ready_after_build_seconds'] is not None
    assert cold['counts_total'] == 40
    assert hot['reused_cache'] is True
    assert hot['open_to_ready_seconds'] is not None
    assert hot['open_to_ready_seconds'] < cold['open_to_ready_after_build_seconds']
    # 缓存落在私有 settings 目录里，没有碰共享的 %APPDATA%
    assert str(settings_dir) in cold['cache_path']


# ────────────────────────── 长会话内存 ──────────────────────────


def test_memory_verdict_passes_a_flat_series():
    samples = [{'rss_bytes': 100 * 1024 * 1024} for _ in range(12)]
    verdict = env_matrix._memory_verdict(samples)
    assert verdict['verdict'] == 'pass'
    assert verdict['steady_state_verdict'] == 'pass'
    assert verdict['peak_bytes'] == 100 * 1024 * 1024


def test_memory_verdict_fails_an_unbounded_growth():
    samples = [{'rss_bytes': (100 + index * 20) * 1024 * 1024} for index in range(12)]
    verdict = env_matrix._memory_verdict(samples)
    assert verdict['verdict'] == 'fail'
    assert verdict['checks']['last_quarter_not_growing'] is False
    assert verdict['steady_state_verdict'] == 'fail'


def test_memory_verdict_needs_enough_samples():
    verdict = env_matrix._memory_verdict([{'rss_bytes': 1}, {'rss_bytes': 2}])
    assert verdict['verdict'] == env_matrix.NO_MEASUREMENT
    assert '样本太少' in verdict['reason']


# ────────────────────────── 报告渲染 ──────────────────────────


def test_render_markdown_contains_the_three_tables():
    report = {
        'generated_at': '2026-10-22 00:00:00',
        'environment': env_matrix.describe_environment({'qt_platform': 'windows'}),
        'project_profile': {'records': 100, 'unique_sources': 2,
                            'distinct_source_bytes': 1234},
        'dpi': {'levels': [], 'knobs': []},
        'cache_fixture': {'scenarios': [], 'catalog_cache_path': '/tmp/x.sqlite',
                          'settings_dir': '/tmp', 'shared_catalog_touched': False,
                          'shared_catalog_path': '/shared/x.sqlite'},
        'session': {'samples': [], 'memory': {}},
    }
    text = env_matrix.render_markdown(report)
    assert '表 1 · DPI 档位表' in text
    assert '表 2 · 冷 / 热缓存对比表' in text
    assert '表 3 · 长会话内存趋势表' in text
    assert env_matrix.NO_MEASUREMENT in text
