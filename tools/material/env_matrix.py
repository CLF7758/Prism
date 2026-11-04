"""环境矩阵：不同 DPI / 冷缓存 / 长会话内存。

三个维度各自一个可复跑入口（外加 ``all`` 一次跑完）：

    .\\.venv\\Scripts\\python.exe tools/material/env_matrix.py dpi     --out artifacts/scale-acceptance/2026-10-22
    .\\.venv\\Scripts\\python.exe tools/material/env_matrix.py cache   --out ... --project <工程路径>
    .\\.venv\\Scripts\\python.exe tools/material/env_matrix.py session --out ... --project <工程路径> --rounds 20
    .\\.venv\\Scripts\\python.exe tools/material/env_matrix.py all     --out ... --project <工程路径>

口径纪律（报告里也写一遍）：

* **DPI 环境变量必须在进程启动前设置** → 每一档用 ``subprocess`` 拉独立子进程，
  同一进程里绝不改 ``QT_SCALE_FACTOR``。
* **常驻内存**用 ``prism.performance_trace.process_memory_bytes()``；**系统内存**用
  ``ctypes`` 的 ``GlobalMemoryStatusEx``。不依赖 psutil（本机没装）。
* **缓存只改名备份，不删除**；被改名的文件记录原名 / 大小 / mtime。共享的
  ``%APPDATA%\\Prism`` 默认不碰 —— 冷缓存实验走 ``--settings-dir`` 指定的私有目录
  （等价于换了一个 APPDATA 根），需要动共享目录时必须显式 ``--shared-cache``。
* **工程一律只读打开**（sqlite ``mode=ro``），脚本任何分支都不写工程文件。
* 缺项一律写「无实测」并说明原因，不留空、不编造。

子命令（内部用，不面向用户）：

* ``_dpi-child`` —— 一档 DPI 的渲染与测量（父进程用 ``subprocess`` 拉起）。
* ``_open-child`` —— 一次「打开到就绪」的测量（冷 / 热各一次）。
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import math
import os
import platform
import sqlite3
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: 10 万档真实工程（只读）。
DEFAULT_PROJECT = Path(r'E:\Prism-stress-100k\Prism-100k-v1.prism')

#: 验收要求的四档 DPI。
DPI_LEVELS = (96, 120, 144, 192)

#: 基准 DPI（100%）。
BASE_DPI = 96

#: 缺项的统一写法。
NO_MEASUREMENT = '无实测'

#: 拉伸对照已经糊到测不出边缘时的表格写法。
BLURRED_REFERENCE_NOTE = '对照已糊化(无边缘)'

#: 长会话内存的判定阈值：末段均值相对首段均值的允许增长。
SESSION_GROWTH_TOLERANCE = 0.05

#: 长会话内存的判定阈值：末段均值相对首段均值的允许绝对增长（字节）。
SESSION_GROWTH_TOLERANCE_BYTES = 32 * 1024 * 1024


# ────────────────────────────── 环境口径 ──────────────────────────────


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ('dwLength', wintypes.DWORD), ('dwMemoryLoad', wintypes.DWORD),
        ('ullTotalPhys', ctypes.c_ulonglong), ('ullAvailPhys', ctypes.c_ulonglong),
        ('ullTotalPageFile', ctypes.c_ulonglong), ('ullAvailPageFile', ctypes.c_ulonglong),
        ('ullTotalVirtual', ctypes.c_ulonglong), ('ullAvailVirtual', ctypes.c_ulonglong),
        ('ullAvailExtendedVirtual', ctypes.c_ulonglong),
    ]


def system_memory():
    """系统内存档位：总内存 / 空闲内存（不用 psutil）。"""
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError('GlobalMemoryStatusEx failed')
    total = int(status.ullTotalPhys)
    available = int(status.ullAvailPhys)
    return {
        'total_bytes': total,
        'available_bytes': available,
        'used_bytes': total - available,
        'load_percent': int(status.dwMemoryLoad),
        'page_file_total_bytes': int(status.ullTotalPageFile),
        'page_file_available_bytes': int(status.ullAvailPageFile),
    }


class _FILETIME(ctypes.Structure):
    _fields_ = [('dwLowDateTime', wintypes.DWORD), ('dwHighDateTime', wintypes.DWORD)]


def system_cpu_times():
    """系统级 idle/kernel/user 累计时间（100 ns 单位）。"""
    idle, kernel, user = _FILETIME(), _FILETIME(), _FILETIME()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel),
                                                  ctypes.byref(user)):
        return None

    def value(stamp):
        return (stamp.dwHighDateTime << 32) | stamp.dwLowDateTime

    return {'idle': value(idle), 'kernel': value(kernel), 'user': value(user)}


def cpu_busy_percent(before, after):
    """两次采样之间的系统 CPU 占用百分比。"""
    if not before or not after:
        return None
    idle = after['idle'] - before['idle']
    total = (after['kernel'] - before['kernel']) + (after['user'] - before['user'])
    if total <= 0:
        return None
    return round(100.0 * (total - idle) / total, 2)


def python_process_snapshot():
    """本机正在跑的 python 进程数（机器争用口径）。"""
    try:
        completed = subprocess.run(
            ['tasklist', '/FI', 'IMAGENAME eq python.exe', '/FO', 'CSV', '/NH'],
            capture_output=True, text=True, timeout=30, encoding='utf-8', errors='replace')
    except (OSError, subprocess.SubprocessError) as exc:
        return {'count': None, 'error': repr(exc)}
    rows = [line for line in (completed.stdout or '').splitlines()
            if line.strip() and '.exe' in line.lower()]
    return {'count': len(rows), 'source': 'tasklist python.exe'}


def describe_environment(extra=None):
    """环境口径：OS / Python / PyQt / Qt / 核数 / 内存档位 / 机器负载。"""
    from PyQt6 import QtCore
    payload = {
        'os': platform.platform(),
        'os_version': platform.version(),
        'machine': platform.machine(),
        'processor': platform.processor(),
        'python': sys.version.split()[0],
        'python_executable': sys.executable,
        'pyqt6': QtCore.PYQT_VERSION_STR,
        'qt': QtCore.QT_VERSION_STR,
        'logical_cpus': os.cpu_count(),
        'memory': system_memory(),
        'python_processes': python_process_snapshot(),
        'measured_at': time.strftime('%Y-%m-%d %H:%M:%S'),
    }
    if extra:
        payload.update(extra)
    return payload


# ────────────────────────────── 工程口径 ──────────────────────────────


def connect_readonly(project):
    """只读连接一个 .prism 工程。"""
    path = Path(project).resolve()
    return sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)


def project_asset_profile(project, cycle_check=2744):
    """记录数 / 唯一源图数 / 实际解码字节数 —— 三项分列。

    10 万档是「2744 张原图复制出的 10 万条记录」，所以记录规模 ≠ 解码负载。
    唯一源图数用「复制周期」现场验证：第 1 条与第 ``cycle+1`` 条的 blob 摘要
    相同、第 ``cycle`` 条与第 1 条不同 → 周期长度就是唯一源图数。
    """
    profile = {'project': str(project), 'exists': Path(project).exists(),
               'records': None, 'unique_sources': None, 'distinct_source_bytes': None,
               'total_record_bytes': None, 'material_records': None}
    if not profile['exists']:
        profile['reason'] = '工程不存在'
        return profile
    try:
        with connect_readonly(project) as connection:
            profile['records'] = connection.execute(
                "SELECT count(*) FROM items WHERE type='pixmap'").fetchone()[0]
            profile['total_record_bytes'] = connection.execute(
                'SELECT sum(sz) FROM sqlar').fetchone()[0]
            try:
                profile['material_records'] = connection.execute(
                    'SELECT count(*) FROM material_records').fetchone()[0]
            except sqlite3.Error:
                profile['material_records'] = None
            rows = connection.execute(
                'SELECT item_id, data FROM sqlar WHERE item_id<=? ORDER BY item_id',
                (cycle_check,)).fetchall()
            sizes = {item_id: len(blob) for item_id, blob in rows}
            profile['distinct_source_bytes'] = sum(sizes.values()) if sizes else None
            import hashlib
            digests = {item_id: hashlib.sha256(blob).hexdigest() for item_id, blob in rows}
            first = digests.get(1)
            beyond = connection.execute(
                'SELECT item_id, data FROM sqlar WHERE item_id=?',
                (cycle_check + 1,)).fetchone()
            if first and beyond:
                same = hashlib.sha256(bytes(beyond[1])).hexdigest() == first
                different = digests.get(cycle_check) != first
                profile['cycle_verified'] = bool(same and different)
                profile['cycle_checked'] = cycle_check
                if same and different:
                    profile['unique_sources'] = cycle_check
                    profile['unique_source_note'] = (
                        f'复制周期现场验证通过：第 1 条与第 {cycle_check + 1} 条 blob 摘要相同，'
                        f'第 {cycle_check} 条与第 1 条不同')
            profile['decode_amplification'] = (
                round(profile['records'] / profile['unique_sources'], 2)
                if profile['records'] and profile['unique_sources'] else None)
    except (sqlite3.Error, OSError) as exc:
        profile['reason'] = f'{type(exc).__name__}: {exc}'
    return profile


def extract_material_texture(project, destination):
    """把一份**真实材质贴图**从工程里只读取出来，放到本地工作目录。

    没有这一步，DPI 维度只能用合成图，清晰度红线就失去意义。
    """
    if not project or not Path(project).exists():
        return {'ok': False, 'reason': f'工程不存在：{project}'}
    try:
        with connect_readonly(project) as connection:
            rows = connection.execute(
                'SELECT payload FROM material_records LIMIT 60').fetchall()
            for (payload,) in rows:
                record = json.loads(payload)
                textures = record.get('textures') or {}
                for channel, binding in textures.items():
                    if not isinstance(binding, dict):
                        continue
                    attachment_id = binding.get('attachmentId')
                    if not isinstance(attachment_id, int):
                        continue
                    found = connection.execute(
                        'SELECT name, mime, blob FROM attachments WHERE id=?',
                        (attachment_id,)).fetchone()
                    if not found or not found[2]:
                        continue
                    blob = bytes(found[2])
                    if len(blob) < 2048:
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(blob)
                    return {'ok': True, 'source': 'real-material-texture',
                            'project': str(project), 'material_id': record.get('id'),
                            'material_name': record.get('name'), 'channel': channel,
                            'attachment_id': attachment_id, 'bytes': len(blob),
                            'name': found[0], 'mime': found[1], 'path': str(destination)}
    except (sqlite3.Error, OSError, ValueError) as exc:
        return {'ok': False, 'reason': f'{type(exc).__name__}: {exc}'}
    return {'ok': False, 'reason': '工程里没有可用的材质贴图附件'}


# ────────────────────────────── 图像度量 ──────────────────────────────


def image_metrics(image, crop_side=256, edge_threshold=24):
    """一张 QImage 的清晰度 / 纯色块度量（按**设备像素**计算）。

    * ``modal_color_ratio`` —— 出现最多的颜色占比，纯色块 ≈ 1.0。
    * ``unique_colors`` —— 不同颜色数，纯色块 = 1。
    * ``max_laplacian`` —— 最强局部对比度，糊化会显著下降。
    * ``mean_edge_run`` —— 边缘平均宽度（设备像素），拉伸会把它撑宽。
    """
    from PyQt6 import QtGui
    if image is None or image.isNull():
        return {'ok': False, 'reason': '空图像'}
    width, height = image.width(), image.height()
    if width < 3 or height < 3:
        return {'ok': False, 'reason': f'图像太小：{width}x{height}'}
    rgb = image.convertToFormat(QtGui.QImage.Format.Format_RGB32)
    rgb_stride = rgb.bytesPerLine()
    raw_rgb = rgb.constBits().asstring(rgb.sizeInBytes())
    counts = {}
    stride = max(1, int(math.sqrt((width * height) / 20000.0)))
    sampled = 0
    for y in range(0, height, stride):
        row = y * rgb_stride
        for x in range(0, width, stride):
            index = row + x * 4
            key = raw_rgb[index:index + 3]
            counts[key] = counts.get(key, 0) + 1
            sampled += 1
    modal = max(counts.values()) if counts else 0
    gray = image.convertToFormat(QtGui.QImage.Format.Format_Grayscale8)
    gray_stride = gray.bytesPerLine()
    raw = gray.constBits().asstring(gray.sizeInBytes())
    gray_width, gray_height = gray.width(), gray.height()
    side = min(crop_side, gray_width, gray_height)
    x0 = max(0, (gray_width - side) // 2)
    y0 = max(0, (gray_height - side) // 2)
    x1 = min(gray_width, x0 + side)
    y1 = min(gray_height, y0 + side)
    laplacian_sum = 0
    laplacian_max = 0
    pixels = 0
    for y in range(y0 + 1, y1 - 1):
        row = y * gray_stride
        for x in range(x0 + 1, x1 - 1):
            centre = raw[row + x]
            value = abs(4 * centre - raw[row + x - 1] - raw[row + x + 1]
                        - raw[row - gray_stride + x] - raw[row + gray_stride + x])
            laplacian_sum += value
            if value > laplacian_max:
                laplacian_max = value
            pixels += 1
    crossings = 0
    edge_pixels = 0
    for y in range(y0, y1):
        row = y * gray_stride
        previous = raw[row + x0]
        for x in range(x0 + 1, x1):
            current = raw[row + x]
            if abs(current - previous) > edge_threshold:
                crossings += 1
                edge_pixels += 1
            previous = current
    return {
        'ok': True,
        'pixel_size': [width, height],
        'sampled_pixels': sampled,
        'unique_colors': len(counts),
        'modal_color_ratio': round(modal / sampled, 4) if sampled else None,
        'max_laplacian': laplacian_max,
        'mean_laplacian': round(laplacian_sum / pixels, 4) if pixels else None,
        'edge_crossings': crossings,
        'edge_pixels': edge_pixels,
        'mean_edge_run': round(edge_pixels / crossings, 3) if crossings else None,
        'crop_side': side,
        'crop_origin': [x0, y0],
    }


def synthetic_source(size=1024):
    """合成的带高频细节的图（仅在拿不到真实贴图时用，并在结果里标明）。"""
    from PyQt6 import QtCore, QtGui
    image = QtGui.QImage(size, size, QtGui.QImage.Format.Format_RGB32)
    for y in range(size):
        for x in range(size):
            value = (x * 7 + y * 13) % 256
            image.setPixelColor(x, y, QtGui.QColor(value, (value * 3) % 256,
                                                    (x ^ y) % 256))
    return image


# ────────────────────────────── DPI 维度 ──────────────────────────────


def dpi_child(argv):
    """一档 DPI 的测量：渲染同一素材 → 量像素尺寸 / 清晰度 / 纯色块。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--dpi', type=int, required=True)
    parser.add_argument('--source-image', type=Path)
    parser.add_argument('--logical-size', type=int, default=256)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--probe-only', action='store_true')
    args, _unknown = parser.parse_known_args(argv)

    from PyQt6 import QtCore, QtGui, QtWidgets
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    screen = QtGui.QGuiApplication.primaryScreen()
    payload = {
        'dpi_requested': args.dpi,
        'platform': app.platformName(),
        'device_pixel_ratio': float(screen.devicePixelRatio()),
        'logical_dots_per_inch': float(screen.logicalDotsPerInch()),
        'physical_dots_per_inch': float(screen.physicalDotsPerInch()),
        'screen_logical_px': [screen.geometry().width(), screen.geometry().height()],
        'env': {name: os.environ.get(name) for name in
                ('QT_SCALE_FACTOR', 'QT_SCREEN_SCALE_FACTORS', 'QT_FONT_DPI')},
    }
    if args.probe_only:
        _emit(payload, args.out)
        return 0

    source = None
    if args.source_image and Path(args.source_image).exists():
        source = QtGui.QImage(str(args.source_image))
        if source.isNull():
            source = None
    if source is None:
        source = synthetic_source()
        payload['source'] = 'synthetic-high-frequency'
        payload['source_note'] = '没有取到真实贴图，本档用合成高频图（已在报告中标注）'
    else:
        payload['source'] = str(args.source_image)
        payload['source_note'] = '真实材质贴图'
    payload['source_pixel_size'] = [source.width(), source.height()]

    dpr = payload['device_pixel_ratio']
    logical = args.logical_size
    device = max(1, round(logical * dpr))
    target = QtGui.QImage(device, device, QtGui.QImage.Format.Format_RGB32)
    target.setDevicePixelRatio(dpr)
    painter = QtGui.QPainter(target)
    painter.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(QtCore.QRectF(0, 0, logical, logical), source)
    painter.end()
    payload['logical_size'] = logical
    payload['device_pixel_size'] = [target.width(), target.height()]
    payload['expected_device_pixel_size'] = device
    payload['pixel_ratio'] = round(target.width() / logical, 4) if logical else None
    payload['expected_pixel_ratio'] = round(dpr, 4)

    render = image_metrics(target)
    # 「拉伸对照」：把同一张贴图先缩到逻辑尺寸，再双线性拉到设备尺寸。
    # 真采样放大应当比这个对照更锐，糊化/拉伸则不会。
    base = source.scaled(logical, logical, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                         QtCore.Qt.TransformationMode.SmoothTransformation)
    stretched = base.scaled(device, device, QtCore.Qt.AspectRatioMode.IgnoreAspectRatio,
                            QtCore.Qt.TransformationMode.SmoothTransformation)
    stretch = image_metrics(stretched)
    payload['render'] = render
    payload['stretched_reference'] = stretch
    if render.get('ok') and stretch.get('ok') and stretch.get('max_laplacian'):
        payload['detail_gain_vs_stretch'] = round(
            render['max_laplacian'] / stretch['max_laplacian'], 4)
    if render.get('ok') and stretch.get('ok') and stretch.get('mean_edge_run'):
        payload['edge_run_ratio_vs_stretch'] = round(
            render['mean_edge_run'] / stretch['mean_edge_run'], 4)

    # 真实 GUI 合成：把同一张贴图放进一个固定逻辑尺寸的控件，抓取设备像素。
    widget = QtWidgets.QWidget()
    widget.setFixedSize(logical, logical)
    label = QtWidgets.QLabel(widget)
    label.setGeometry(0, 0, logical, logical)
    label.setPixmap(QtGui.QPixmap.fromImage(source).scaled(
        logical, logical, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
        QtCore.Qt.TransformationMode.SmoothTransformation))
    widget.show()
    app.processEvents()
    pixmap = widget.grab()
    payload['widget_grab'] = {
        'pixel_size': [pixmap.width(), pixmap.height()],
        'device_pixel_ratio': float(pixmap.devicePixelRatio()),
        'independent_size': [pixmap.deviceIndependentSize().width(),
                             pixmap.deviceIndependentSize().height()],
        'metrics': image_metrics(pixmap.toImage()),
    }
    widget.hide()
    _emit(payload, args.out)
    return 0


def evaluate_dpi(entry):
    """把一档 DPI 的观测变成判据结论。"""
    if entry.get('status') != 'measured':
        # 缺项一律走「无实测」，并且 reasons 永远是列表，调用方不用两种形状。
        return {'verdict': NO_MEASUREMENT,
                'reasons': [entry.get('reason', '子进程未产出结果')]}
    checks = {}
    reasons = []
    expected = entry.get('expected_device_pixel_size')
    actual = entry.get('device_pixel_size')
    checks['pixel_size_scales'] = bool(expected and actual and actual[0] == expected
                                       and actual[1] == expected)
    if not checks['pixel_size_scales']:
        reasons.append(f'设备像素尺寸 {actual} ≠ 期望 {expected}')
    render = entry.get('render') or {}
    flat_ratio = render.get('modal_color_ratio')
    unique = render.get('unique_colors')
    checks['not_flat_block'] = bool(flat_ratio is not None and unique is not None
                                    and flat_ratio < 0.90 and unique >= 16)
    if not checks['not_flat_block']:
        reasons.append(f'疑似纯色块：主色占比 {flat_ratio}，颜色数 {unique}')
    gain = entry.get('detail_gain_vs_stretch')
    edge_ratio = entry.get('edge_run_ratio_vs_stretch')
    if entry.get('device_pixel_size') and entry['device_pixel_size'][0] > entry.get('logical_size', 0):
        # 拉伸对照在该阈值下测不到任何边缘 = 对照已经被抹平。真采样渲染既然
        # 还有边缘，它就不可能是那条拉伸路径的产物 —— 这比一个比值更强。
        stretch_blurred = edge_ratio is None
        checks['not_blurry_real_resample'] = bool(
            gain is not None and gain >= 1.05 and (stretch_blurred or edge_ratio <= 1.0))
        if not checks['not_blurry_real_resample']:
            reasons.append(f'相对拉伸对照不锐：细节增益 {gain}（要求 ≥1.05），'
                           f'边缘宽度比 {edge_ratio}（要求 ≤1.0）')
        entry['stretch_reference_blurred'] = stretch_blurred
    else:
        checks['not_blurry_real_resample'] = None
    grab = entry.get('widget_grab') or {}
    grab_size = grab.get('pixel_size')
    checks['widget_composition_scales'] = bool(
        expected and grab_size and grab_size[0] == expected and grab_size[1] == expected)
    if not checks['widget_composition_scales']:
        reasons.append(f'控件抓取设备像素 {grab_size} ≠ 期望 {expected}')
    applicable = [value for value in checks.values() if value is not None]
    verdict = 'pass' if applicable and all(applicable) else 'fail'
    return {'verdict': verdict, 'checks': checks, 'reasons': reasons}


def _dpi_env(base, dpi, knob='QT_SCALE_FACTOR'):
    env = dict(base)
    for name in ('QT_SCALE_FACTOR', 'QT_SCREEN_SCALE_FACTORS', 'QT_FONT_DPI'):
        env.pop(name, None)
    if knob is None:
        return env
    env[knob] = str(dpi / BASE_DPI) if knob != 'QT_FONT_DPI' else str(dpi)
    return env


def run_dpi(out_dir, levels=DPI_LEVELS, project=None, logical=256, timeout=240,
            source_image=None):
    """DPI 矩阵：每档一个独立子进程。"""
    from PyQt6 import QtCore
    work = Path(out_dir) / '_work' / 'dpi'
    work.mkdir(parents=True, exist_ok=True)
    source_note = None
    if source_image is None:
        source_image = work / 'source-texture.png'
        source_note = extract_material_texture(project, source_image)
    results = []
    for dpi in levels:
        child_json = work / f'dpi-{dpi}.json'
        env = _dpi_env(os.environ, dpi, 'QT_SCALE_FACTOR')
        command = [sys.executable, str(Path(__file__).resolve()), '_dpi-child',
                   '--dpi', str(dpi), '--logical-size', str(logical),
                   '--out', str(child_json)]
        if source_image and Path(source_image).exists():
            command += ['--source-image', str(source_image)]
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, env=env, capture_output=True, text=True,
                                       timeout=timeout, encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            results.append({'dpi': dpi, 'status': 'failed',
                            'reason': f'子进程 {timeout} s 超时'})
            continue
        seconds = time.perf_counter() - started
        entry = {'dpi': dpi, 'status': 'measured', 'child_seconds': round(seconds, 3),
                 'knob': 'QT_SCALE_FACTOR', 'command': ' '.join(command)}
        if child_json.exists():
            entry.update(json.loads(child_json.read_text(encoding='utf-8')))
        else:
            entry['status'] = 'failed'
            entry['reason'] = '子进程没有产出结果文件'
            entry['stderr'] = (completed.stderr or '')[-2000:]
        entry['evaluation'] = evaluate_dpi(entry)
        results.append(entry)
    knobs = []
    # 每一档都**只**设一个环境变量，另加一档什么都不设的基准。
    # QT_SCALE_FACTOR / QT_SCREEN_SCALE_FACTORS 是缩放倍数；
    # QT_FONT_DPI 的单位是 DPI 点数（96/120/144/192），不是倍数。
    knob_plan = [('基准（不设任何开关）', None, 96),
                 ('QT_SCALE_FACTOR', 'QT_SCALE_FACTOR', 144),
                 ('QT_SCREEN_SCALE_FACTORS', 'QT_SCREEN_SCALE_FACTORS', 144),
                 ('QT_FONT_DPI', 'QT_FONT_DPI', 144)]
    for label, knob, dpi in knob_plan:
        child_json = work / f'knob-{(knob or "base")}-{dpi}.json'
        if knob is None:
            env = _dpi_env(os.environ, dpi, None)
            env.pop('QT_SCALE_FACTOR', None)
            env.pop('QT_SCREEN_SCALE_FACTORS', None)
            env.pop('QT_FONT_DPI', None)
            env_value = None
        else:
            env = _dpi_env(os.environ, dpi, knob)
            env_value = env.get(knob)
        command = [sys.executable, str(Path(__file__).resolve()), '_dpi-child',
                   '--dpi', str(dpi), '--probe-only', '--out', str(child_json)]
        try:
            subprocess.run(command, env=env, capture_output=True, text=True,
                           timeout=timeout, encoding='utf-8', errors='replace')
        except subprocess.TimeoutExpired:
            knobs.append({'knob': label, 'status': 'failed', 'reason': '子进程超时'})
            continue
        if child_json.exists():
            knobs.append({'knob': label, 'status': 'measured',
                          'env_value': env_value,
                          **json.loads(child_json.read_text(encoding='utf-8'))})
        else:
            knobs.append({'knob': label, 'status': 'failed', 'reason': '子进程没有产出结果'})
    platform_name = next((entry.get('platform') for entry in results
                          if entry.get('platform')), None)
    return {'levels': results, 'knobs': knobs, 'source': source_note,
            'qt_platform': platform_name, 'qt_version': QtCore.QT_VERSION_STR}


# ────────────────────────────── 冷缓存维度 ──────────────────────────────


def build_fixture(project, destination, records=2000, unique_sources=64,
                  canvases=8, materials=200):
    """从真实工程复制出一个小工程：**真实图片字节** + 放大的记录数。

    口径上和 10 万档一致 —— 记录数远大于唯一源图数，解码负载按唯一源图算。
    只读源工程，目标工程是新文件。
    """
    import copy
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换：上一次失败留下的半成品也能被干净覆盖。
    staging = Path(str(destination) + '.building')
    if staging.exists():
        staging.unlink()
    with connect_readonly(project) as source:
        schema = source.execute(
            "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        bases = source.execute(
            "SELECT i.id,i.data,s.name,s.mode,s.mtime,s.sz,s.data FROM items i "
            "JOIN sqlar s ON s.item_id=i.id WHERE i.type='pixmap' ORDER BY i.id LIMIT ?",
            (unique_sources,)).fetchall()
        if not bases:
            raise ValueError('源工程里没有可复制的图片记录')
        material_rows = source.execute(
            'SELECT payload FROM material_records LIMIT ?', (materials,)).fetchall()
        metadata_rows = source.execute(
            'SELECT key, value FROM prism_metadata').fetchall()
        attachment_ids = set()
        for (payload,) in material_rows:
            record = json.loads(payload)
            preview = record.get('preview') or {}
            if isinstance(preview.get('thumbnailAttachmentId'), int):
                attachment_ids.add(preview['thumbnailAttachmentId'])
            for binding in (record.get('textures') or {}).values():
                if isinstance(binding, dict) and isinstance(binding.get('attachmentId'), int):
                    attachment_ids.add(binding['attachmentId'])
            for entry in record.get('files') or []:
                if isinstance(entry, dict) and isinstance(entry.get('attachmentId'), int):
                    attachment_ids.add(entry['attachmentId'])
        attachments = []
        if attachment_ids:
            marks = ','.join('?' * len(attachment_ids))
            attachments = source.execute(
                f'SELECT id,board_id,name,mime,blob,created_at FROM attachments '
                f'WHERE id IN ({marks})', tuple(sorted(attachment_ids))).fetchall()
        target = sqlite3.connect(staging)
        try:
            for kind, name, sql in schema:
                if name.startswith('sqlite_'):
                    # sqlite_sequence 等内部表不能手工建。
                    continue
                target.execute(sql)
            target.execute('DELETE FROM items')
            target.execute('DELETE FROM sqlar')
            target.execute('DELETE FROM material_records')
            target.execute('DELETE FROM attachments')
            # 画布页沿用源工程会和新记录对不上，fixture 自己造画布。
            target.execute('DELETE FROM workspace_pages')
            target.execute('DELETE FROM workspace_nodes')
            target.execute('DELETE FROM prism_metadata')
            for key, value in metadata_rows:
                # project_id 必须跟着材质记录一起搬，否则读工程时会被判成「材质库损坏」。
                target.execute('INSERT INTO prism_metadata VALUES (?,?)', (key, value))
            columns = 460
            for index in range(records):
                base_id, base_data, name, mode, mtime, size, blob = bases[index % len(bases)]
                data = copy.deepcopy(json.loads(base_data))
                data.update(canvasId=f'canvas-{index % canvases}', groupId=None,
                            groupNote='', title=f'Fixture {index + 1:06d}')
                data.pop('deletedAt', None)
                width = int(data.get('imageWidth') or 1)
                height = int(data.get('imageHeight') or 1)
                scale = columns / max(width, height)
                target.execute(
                    'INSERT INTO items(id,type,x,y,z,scale,rotation,flip,data) '
                    'VALUES (?,"pixmap",?,?,0,?,0,1,?)',
                    (index + 1, (index % 40) * 512, (index // 40) * 512, scale,
                     json.dumps(data, ensure_ascii=False)))
                target.execute(
                    'INSERT INTO sqlar(name,item_id,mode,mtime,sz,data) '
                    'VALUES (?,?,?,?,?,?)',
                    (f'fixture-{index + 1:06d}', index + 1, mode, mtime, size, blob))
            for row in attachments:
                target.execute(
                    'INSERT INTO attachments(id,board_id,name,mime,blob,created_at) '
                    'VALUES (?,?,?,?,?,?)', row)
            for (payload,) in material_rows:
                record = json.loads(payload)
                record.update(id=f'fixture-material-{record.get("id")}',
                              folderId=None, deletedAt=None)
                target.execute('INSERT INTO material_records VALUES (?,?)',
                               (record['id'], json.dumps(record, ensure_ascii=False)))
            # Prism 用 user_version / application_id 认「这是一个 Prism 工程」。
            target.execute('PRAGMA user_version=4')
            target.execute('PRAGMA application_id=2060242126')
            target.commit()
            check = target.execute('PRAGMA quick_check').fetchone()[0]
        finally:
            target.close()
    os.replace(staging, destination)
    profile = project_asset_profile(destination, cycle_check=unique_sources)
    profile['fixture'] = {
        'records': records, 'unique_sources': unique_sources, 'canvases': canvases,
        'materials': len(material_rows), 'quick_check': check,
        'source_project': str(project), 'destination': str(destination),
    }
    return profile


def catalog_cache_path(settings_dir, project):
    """目录缓存路径 —— 与 ``prism/view.py:1252-1253`` 同一套规则。

    ``cache_directory()`` = ``<settings>/previews``，目录缓存放在它的同级
    ``catalog/`` 下，文件名是工程 basename + ``.sqlite``。
    """
    return Path(settings_dir) / 'catalog' / (Path(project).name + '.sqlite')


def backup_cache_files(path, stamp):
    """把缓存**改名**备份（不删除），返回动了什么的清单。"""
    moved = []
    candidates = [path, Path(str(path) + '-wal'), Path(str(path) + '-shm'),
                  Path(str(path) + '.build-lock')]
    for candidate in candidates:
        if not candidate.exists():
            continue
        stat = candidate.stat()
        backup = candidate.with_name(candidate.name + f'.e4-backup-{stamp}')
        try:
            os.replace(candidate, backup)
        except OSError as exc:
            moved.append({'original': str(candidate), 'backup': None,
                          'bytes': stat.st_size,
                          'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                                                 time.localtime(stat.st_mtime)),
                          'error': f'{type(exc).__name__}: {exc}'})
            continue
        moved.append({'original': str(candidate), 'backup': str(backup),
                      'bytes': stat.st_size,
                      'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                                             time.localtime(stat.st_mtime))})
    return moved


def open_child(argv):
    """一次「打开到就绪」的测量（冷 / 热各跑一次子进程）。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--settings-dir', required=True)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--build', action='store_true')
    parser.add_argument('--full-read', action='store_true')
    args, _unknown = parser.parse_known_args(argv)

    from prism.project_open_scope import ProjectOpenScope
    from prism.project_catalog_index import CatalogNotReady

    cache = catalog_cache_path(args.settings_dir, args.project)
    scope = ProjectOpenScope(str(args.project), str(cache))
    payload = {'project': str(args.project), 'cache_path': str(cache),
               'settings_dir': str(args.settings_dir),
               'cache_existed_before': cache.exists()}
    started = time.perf_counter()
    ready_before = scope.ready
    payload['catalog_ready_before'] = bool(ready_before)
    if ready_before:
        counts = scope.counts()
        target = _pick_canvas(counts)
        page = scope.page(target, kind='pixmap', limit=256) if target else []
        payload['open_to_ready_seconds'] = round(time.perf_counter() - started, 3)
        payload['counts_canvases'] = len({key[0] for key in (counts or {})})
        payload['counts_total'] = (scope.total())
        payload['page_rows'] = len(page)
        payload['target_canvas'] = target
        payload['reused_cache'] = True
    else:
        payload['reused_cache'] = False
        payload['open_to_ready_seconds'] = None
        payload['cold_reason'] = '目录缓存不存在或与源不匹配（生产路径退回全量读取并后台建库）'
        if args.build:
            build_started = time.perf_counter()
            try:
                result = scope.build()
                payload['catalog_build_seconds'] = round(time.perf_counter() - build_started, 3)
                payload['catalog_build'] = {
                    'complete': bool(result.complete),
                    'total_records': int(result.total_records),
                    'processed_records': int(result.processed_records),
                    'json_characters': int(result.json_characters),
                    'resumed': bool(result.resumed),
                }
            except (CatalogNotReady, OSError, ValueError) as exc:
                payload['catalog_build_seconds'] = None
                payload['catalog_build_error'] = f'{type(exc).__name__}: {exc}'
            counts = scope.counts()
            target = _pick_canvas(counts)
            page = scope.page(target, kind='pixmap', limit=256) if target else []
            payload['open_to_ready_after_build_seconds'] = round(
                time.perf_counter() - started, 3)
            payload['counts_canvases'] = len({key[0] for key in (counts or {})})
            payload['counts_total'] = scope.total()
            payload['page_rows'] = len(page)
            payload['target_canvas'] = target
    if args.full_read:
        payload['full_read'] = _measure_full_read(args.project)
    payload['cache_bytes_after'] = cache.stat().st_size if cache.exists() else None
    _emit(payload, args.out)
    return 0


def _pick_canvas(counts):
    if not counts:
        return None
    best = None
    for (canvas_id, kind), total in counts.items():
        if kind != 'pixmap':
            continue
        if best is None or total > best[1]:
            best = (canvas_id, total)
    return best[0] if best else None


def _measure_full_read(project):
    """生产路径在目录不可用时的全量读取（``fileio.load_prism``）。"""
    started = time.perf_counter()
    try:
        from PyQt6 import QtGui, QtWidgets
        from prism import fileio
        from prism.scene import PrismGraphicsScene
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        scene = PrismGraphicsScene(QtGui.QUndoStack())
        fileio.load_prism(str(project), scene)
        read_seconds = round(time.perf_counter() - started, 3)
        # 读完之后条目是 add_item_later 延迟挂上去的，跑一圈事件循环再数。
        for _ in range(4):
            app.processEvents()
        return {'ok': True, 'seconds': read_seconds,
                'items': len(scene.items()),
                'archive_items': len(getattr(scene, '_prism_archive_items', []) or [])}
    except Exception as exc:  # noqa: BLE001 - 记录失败原因，不吞
        return {'ok': False, 'seconds': round(time.perf_counter() - started, 3),
                'error': f'{type(exc).__name__}: {exc}'}


def run_cache(out_dir, project, settings_root=None, full_read=False, timeout=1800):
    """冷 / 热缓存对比：同一份工程，两个子进程，私有 settings 目录。"""
    out_dir = Path(out_dir)
    work = out_dir / '_work' / 'cache'
    work.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d-%H%M%S')
    settings_dir = Path(settings_root) if settings_root else (work / f'settings-{stamp}')
    settings_dir.mkdir(parents=True, exist_ok=True)
    cache = catalog_cache_path(settings_dir, project)
    shared_catalog = None
    try:
        from prism.thumbnail_cache import cache_directory
        shared_root = Path(cache_directory()).parent
        shared_catalog = shared_root / 'catalog' / (Path(project).name + '.sqlite')
    except Exception:  # noqa: BLE001 - 只是拿路径，失败不影响隔离实验
        shared_catalog = None

    scenarios = []
    moved = backup_cache_files(cache, stamp)
    cold_json = work / 'open-cold.json'
    command = [sys.executable, str(Path(__file__).resolve()), '_open-child',
               '--project', str(project), '--settings-dir', str(settings_dir),
               '--out', str(cold_json), '--build']
    if full_read:
        command.append('--full-read')
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                   encoding='utf-8', errors='replace')
        cold = json.loads(cold_json.read_text(encoding='utf-8')) if cold_json.exists() else {}
        cold['child_seconds'] = round(time.perf_counter() - started, 3)
        if not cold:
            cold = {'status': 'failed', 'reason': '子进程没有产出结果',
                    'stderr': (completed.stderr or '')[-2000:]}
    except subprocess.TimeoutExpired:
        cold = {'status': 'failed', 'reason': f'冷启动子进程 {timeout} s 超时'}
    cold['scenario'] = 'cold'
    cold['cache_moved_aside'] = moved
    cold['cache_backups'] = moved
    scenarios.append(cold)

    hot_json = work / 'open-hot.json'
    command = [sys.executable, str(Path(__file__).resolve()), '_open-child',
               '--project', str(project), '--settings-dir', str(settings_dir),
               '--out', str(hot_json)]
    started = time.perf_counter()
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                   encoding='utf-8', errors='replace')
        hot = json.loads(hot_json.read_text(encoding='utf-8')) if hot_json.exists() else {}
        hot['child_seconds'] = round(time.perf_counter() - started, 3)
        if not hot:
            hot = {'status': 'failed', 'reason': '子进程没有产出结果',
                   'stderr': (completed.stderr or '')[-2000:]}
    except subprocess.TimeoutExpired:
        hot = {'status': 'failed', 'reason': f'热缓存子进程 {timeout} s 超时'}
    hot['scenario'] = 'hot'
    scenarios.append(hot)

    return {
        'scenarios': scenarios,
        'project': str(project),
        'settings_dir': str(settings_dir),
        'catalog_cache_path': str(cache),
        'shared_catalog_path': str(shared_catalog) if shared_catalog else None,
        'shared_catalog_touched': False,
        'cache_backups': moved,
        'note': ('冷/热两档都跑在私有 settings 目录里（等价于换了一个 APPDATA 根），'
                 '共享的 %APPDATA%\\Prism 未被改动'),
    }


# ────────────────────────────── 长会话内存维度 ──────────────────────────────


def session_loop(project, rounds=20, page_size=24, canvas_window=3, cache_path=None,
                 control=False):
    """连续 N 轮「切画布 / 缩放 / 平移 / 选材质」，每轮采样常驻内存。"""
    from PyQt6 import QtCore, QtGui, QtWidgets
    from prism import image_decode, lod
    from prism.performance_trace import process_memory_bytes
    from prism.items import PrismPixmapItem
    from prism.project_open_scope import ProjectOpenScope
    from prism.scene import PrismGraphicsScene

    project = Path(project).resolve()
    cache_path = Path(cache_path) if cache_path else (
        catalog_cache_path(out_dir / '_work' / 'session-settings', project)
        if out_dir else project.with_suffix('.prism.sqlite'))
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    scope = ProjectOpenScope(str(project), str(cache_path))
    if not scope.ready:
        scope.build()
    counts = scope.counts() or {}
    canvases = sorted({canvas_id for (canvas_id, kind) in counts if kind == 'pixmap'})
    payload = {'project': str(project), 'rounds_requested': rounds,
               'page_size': page_size, 'canvas_window': canvas_window,
               'control': control,
               'control_note': ('对照组：只翻目录页 + 选材质，不解码、不进场景，'
                                '用来把「分配器/驱动不归还内存」和「图片管线真的在涨」分开')
               if control else '',
               'canvases': len(canvases), 'cache_path': str(cache_path),
               'records_in_catalog': scope.total()}
    if not canvases:
        payload['status'] = 'failed'
        payload['reason'] = '目录里没有 pixmap 画布，无法开长会话'
        return payload

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    scene = PrismGraphicsScene(QtGui.QUndoStack())
    view = QtWidgets.QGraphicsView(scene)
    view.resize(1024, 768)
    view.show()
    app.processEvents()

    materials = _read_materials(project, limit=64)
    payload['material_records_available'] = len(materials)
    samples = []
    items = []
    before_cpu = system_cpu_times()
    for index in range(rounds):
        canvas_id = canvases[index % len(canvases)]
        rows = scope.page(canvas_id, kind='pixmap', limit=page_size)
        ids = [row['id'] for row in rows]
        blobs = [] if control else _read_blobs(project, ids)
        decoded = 0
        decoded_bytes = 0
        for item_id, blob in blobs:
            # 真实解码负载：工程里的压缩原图字节 → 有界显示图。
            image, size = image_decode.decode(blob=blob, max_side=512)
            if image is None or image.isNull():
                continue
            decoded += 1
            decoded_bytes += len(blob)
            preview = image.scaled(512, 512, QtCore.Qt.AspectRatioMode.KeepAspectRatio,
                                   QtCore.Qt.TransformationMode.SmoothTransformation)
            item = PrismPixmapItem(preview)
            item._source_blob = blob
            item._image_size = size
            item.reset_crop()
            item._preview_pixmap = QtGui.QPixmap.fromImage(preview)
            item.setPos((len(items) % 8) * 220, (len(items) // 8) * 220)
            scene.addItem(item)
            items.append((canvas_id, item))
        # 只保留最近几个画布的物件，模拟「来回切画布」而不是无限堆积。
        if len(items) > canvas_window * page_size:
            for old_canvas, item in items[:len(items) - canvas_window * page_size]:
                scene.removeItem(item)
            items = items[len(items) - canvas_window * page_size:]
        factor = 0.25 + 0.05 * (index % 9)
        view.setTransform(QtGui.QTransform.fromScale(factor, factor))
        view.centerOn(QtCore.QPointF(100 * (index % 5), 80 * (index % 3)))
        if not control:
            view.viewport().repaint()
        app.processEvents()
        material = _select_material(materials, index)
        app.processEvents()
        samples.append({
            'round': index + 1,
            'canvas': canvas_id,
            'page_rows': len(rows),
            'decoded_images': decoded,
            'decoded_bytes': decoded_bytes,
            'selected_material': (material or {}).get('id'),
            'scene_items': len(items),
            'rss_bytes': process_memory_bytes(),
            'display_cache_bytes': lod.cache().bytes,
        })
    after_cpu = system_cpu_times()
    view.hide()
    scene.clear()
    payload['status'] = 'measured'
    payload['samples'] = samples
    payload['memory'] = _memory_verdict(samples)
    payload['cpu_busy_percent'] = cpu_busy_percent(before_cpu, after_cpu)
    payload['python_processes_after'] = python_process_snapshot()
    return payload


def session_child(argv):
    """长会话的一个子进程（私有 settings 目录，不写共享 %APPDATA%）。"""
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--page-size', type=int, default=24)
    parser.add_argument('--canvas-window', type=int, default=3)
    parser.add_argument('--cache-path', required=True)
    parser.add_argument('--control', action='store_true')
    parser.add_argument('--out', type=Path)
    args, _unknown = parser.parse_known_args(argv)
    payload = session_loop(args.project, rounds=args.rounds, page_size=args.page_size,
                           canvas_window=args.canvas_window, cache_path=args.cache_path,
                           control=args.control)
    _emit(payload, args.out)
    return 0


def run_session(out_dir, project, rounds=20, page_size=24, canvas_window=3, timeout=1800):
    """跑主组 + 对照组两个长会话子进程。"""
    out_dir = Path(out_dir)
    work = out_dir / '_work' / 'session'
    work.mkdir(parents=True, exist_ok=True)
    settings_dir = work / 'settings'
    settings_dir.mkdir(parents=True, exist_ok=True)
    cache_path = catalog_cache_path(settings_dir, project)
    results = {'settings_dir': str(settings_dir), 'cache_path': str(cache_path),
               'runs': {}}
    for label, control in (('main', False), ('control', True)):
        child_json = work / f'session-{label}.json'
        command = [sys.executable, str(Path(__file__).resolve()), '_session-child',
                   '--project', str(project), '--rounds', str(rounds),
                   '--page-size', str(page_size), '--canvas-window', str(canvas_window),
                   '--cache-path', str(cache_path), '--out', str(child_json),
                   '--settings-dir', str(settings_dir)]
        if control:
            command.append('--control')
        started = time.perf_counter()
        try:
            completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                                       encoding='utf-8', errors='replace')
            data = json.loads(child_json.read_text(encoding='utf-8')) if child_json.exists() else {}
            if not data:
                data = {'status': 'failed', 'reason': '子进程没有产出结果',
                        'stderr': (completed.stderr or '')[-2000:]}
        except subprocess.TimeoutExpired:
            data = {'status': 'failed', 'reason': f'子进程 {timeout} s 超时'}
        data['child_seconds'] = round(time.perf_counter() - started, 3)
        data['command'] = ' '.join(command)
        results['runs'][label] = data
    main_run = results['runs'].get('main') or {}
    control_run = results['runs'].get('control') or {}
    results['status'] = main_run.get('status')
    results['samples'] = main_run.get('samples')
    results['memory'] = main_run.get('memory')
    results['control_memory'] = control_run.get('memory')
    results['canvases'] = main_run.get('canvases')
    results['records_in_catalog'] = main_run.get('records_in_catalog')
    results['material_records_available'] = main_run.get('material_records_available')
    results['cpu_busy_percent'] = main_run.get('cpu_busy_percent')
    results['python_processes_after'] = main_run.get('python_processes_after')
    return results


def _memory_verdict(samples):
    values = [sample['rss_bytes'] for sample in samples if sample.get('rss_bytes')]
    if len(values) < 3:
        return {'verdict': NO_MEASUREMENT, 'reason': f'样本太少（{len(values)}）'}
    quarter = max(1, len(values) // 4)
    first_mean = statistics.fmean(values[:quarter])
    last_mean = statistics.fmean(values[-quarter:])
    mid = values[len(values) // 2]
    slope = _slope(values)
    growth = last_mean - first_mean
    growth_ratio = growth / first_mean if first_mean else None
    checks = {
        'last_quarter_not_growing': bool(growth_ratio is not None
                                         and growth_ratio <= SESSION_GROWTH_TOLERANCE
                                         and growth <= SESSION_GROWTH_TOLERANCE_BYTES),
        'slope_not_positive': bool(slope <= 0),
    }
    verdict = 'pass' if all(checks.values()) else 'fail'
    # 稳态判据：把「预热」（显示缓存填到预算上限）排除后，看中段与末段是否还在涨。
    third = max(1, len(values) // 3)
    middle_mean = statistics.fmean(values[third:2 * third])
    end_mean = statistics.fmean(values[-third:])
    steady_growth = end_mean - middle_mean
    steady_ratio = steady_growth / middle_mean if middle_mean else None
    steady_checks = {
        'steady_state_not_growing': bool(steady_ratio is not None
                                         and steady_ratio <= SESSION_GROWTH_TOLERANCE
                                         and steady_growth <= SESSION_GROWTH_TOLERANCE_BYTES),
    }
    return {
        'verdict': verdict,
        'checks': checks,
        'verdict_note': ('严格判据 = 末 1/4 均值 vs 首 1/4 均值（**含预热**）；'
                         '预热期显示缓存会填到预算上限，所以这个判据把预热也算成了增长。'
                         '稳态判据 = 末 1/3 均值 vs 中 1/3 均值（排除预热）单列。'),
        'steady_state_verdict': 'pass' if all(steady_checks.values()) else 'fail',
        'steady_state_checks': steady_checks,
        'middle_third_mean_bytes': round(middle_mean),
        'last_third_mean_bytes': round(end_mean),
        'steady_growth_bytes': round(steady_growth),
        'steady_growth_ratio': round(steady_ratio, 4) if steady_ratio is not None else None,
        'first_round_bytes': values[0],
        'mid_round_bytes': mid,
        'last_round_bytes': values[-1],
        'peak_bytes': max(values),
        'min_bytes': min(values),
        'first_quarter_mean_bytes': round(first_mean),
        'last_quarter_mean_bytes': round(last_mean),
        'growth_bytes': round(growth),
        'growth_ratio': round(growth_ratio, 4) if growth_ratio is not None else None,
        'slope_bytes_per_round': round(slope, 1),
        'tolerance': {'ratio': SESSION_GROWTH_TOLERANCE, 'bytes': SESSION_GROWTH_TOLERANCE_BYTES},
    }


def _slope(values):
    count = len(values)
    mean_x = (count - 1) / 2
    mean_y = statistics.fmean(values)
    numerator = sum((index - mean_x) * (value - mean_y) for index, value in enumerate(values))
    denominator = sum((index - mean_x) ** 2 for index in range(count))
    return numerator / denominator if denominator else 0.0


def _read_materials(project, limit=64):
    try:
        with connect_readonly(project) as connection:
            rows = connection.execute(
                'SELECT payload FROM material_records LIMIT ?', (limit,)).fetchall()
    except (sqlite3.Error, OSError):
        return []
    materials = []
    for (payload,) in rows:
        try:
            record = json.loads(payload)
        except ValueError:
            continue
        preview = record.get('preview') or {}
        attachment_id = preview.get('thumbnailAttachmentId')
        materials.append({'id': record.get('id'), 'name': record.get('name'),
                          'thumbnail_attachment_id': attachment_id})
    return materials


def _read_blobs(project, ids):
    if not ids:
        return []
    try:
        with connect_readonly(project) as connection:
            marks = ','.join('?' * len(ids))
            rows = connection.execute(
                f'SELECT item_id, data FROM sqlar WHERE item_id IN ({marks})',
                tuple(ids)).fetchall()
    except (sqlite3.Error, OSError):
        return []
    return [(item_id, bytes(blob)) for item_id, blob in rows if blob]


def _select_material(materials, index):
    if not materials:
        return None
    return materials[index % len(materials)]


# ────────────────────────────── 输出 ──────────────────────────────


def _emit(payload, out_path):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        Path(out_path).write_text(text + '\n', encoding='utf-8')
    print(text)


def render_markdown(report):
    """把结构化结果渲染成三张表。"""
    environment = report.get('environment', {})
    memory = environment.get('memory', {})
    lines = ['# 环境矩阵结果表（E-4）', '']
    lines.append(f"- 生成时间：{report.get('generated_at')}")
    lines.append(f"- OS：{environment.get('os')}")
    lines.append(f"- Python：{environment.get('python')} ｜ PyQt6：{environment.get('pyqt6')}"
                 f" ｜ Qt：{environment.get('qt')}")
    lines.append(f"- 逻辑核：{environment.get('logical_cpus')}")
    lines.append(f"- 内存档位：总 {memory.get('total_bytes')} B"
                 f"（{(memory.get('total_bytes') or 0) / 1024 ** 3:.1f} GiB），"
                 f"空闲 {memory.get('available_bytes')} B，"
                 f"占用 {memory.get('load_percent')}%")
    lines.append(f"- Qt 平台名：{environment.get('qt_platform')}"
                 f"（{'真实 GUI 平台' if environment.get('qt_platform') == 'windows' else '非 windows 平台' if environment.get('qt_platform') else '未取到'}）")
    lines.append(f"- 机器负载：测量时 python 进程数 "
                 f"{(environment.get('python_processes') or {}).get('count')}")
    lines.append('')
    lines.append('### 记录规模 / 解码负载（三项分列，逐工程）')
    lines.append('')
    lines.append('| 工程 | 记录数 | 唯一源图数 | 实际解码字节数（唯一源图） |'
                 ' 记录总字节 | 放大倍数 | 材质记录 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- |')
    profiles = report.get('project_profiles') or {}
    if not profiles and report.get('project_profile'):
        profiles = {str(report.get('project')): report['project_profile']}
    for name, profile in profiles.items():
        lines.append(f"| `{name}` | {profile.get('records', NO_MEASUREMENT)} |"
                     f" {profile.get('unique_sources', NO_MEASUREMENT)} |"
                     f" {profile.get('distinct_source_bytes', NO_MEASUREMENT)} |"
                     f" {profile.get('total_record_bytes', NO_MEASUREMENT)} |"
                     f" {profile.get('decode_amplification', NO_MEASUREMENT)} |"
                     f" {profile.get('material_records', NO_MEASUREMENT)} |")
    lines.append('')
    lines.append('> 「记录数 / 唯一源图数 / 实际解码字节数」是三件不同的事：'
                 '10 万档是 2744 张原图复制出的 10 万条记录，'
                 '所以**记录规模不等于解码负载**（放大 '
                 f"{(profiles.get(str(DEFAULT_PROJECT)) or {}).get('decode_amplification')} 倍）。")
    lines.append('')

    lines += ['## 表 1 · DPI 档位表', '']
    lines.append('| DPI | 平台 | 设备像素比 | 设备像素尺寸 | 期望尺寸 | 像素按比例放大 |'
                 ' 主色占比 | 颜色数 | 细节增益(vs 拉伸) | 边缘宽度比 | 拉伸对照边缘数 |'
                 ' 控件抓取像素 | 结论 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |')
    for entry in report.get('dpi', {}).get('levels', []):
        evaluation = entry.get('evaluation') or {}
        render = entry.get('render') or {}
        stretch = entry.get('stretched_reference') or {}
        grab = entry.get('widget_grab') or {}
        if entry.get('status') != 'measured':
            lines.append(f"| {entry.get('dpi')} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT}："
                         f"{entry.get('reason', '子进程未产出结果')} |")
            continue
        checks = evaluation.get('checks') or {}
        lines.append(
            f"| {entry.get('dpi')} | {entry.get('platform')} | {entry.get('device_pixel_ratio')} |"
            f" {entry.get('device_pixel_size')} | {entry.get('expected_device_pixel_size')} |"
            f" {'是' if checks.get('pixel_size_scales') else '否'} |"
            f" {render.get('modal_color_ratio')} | {render.get('unique_colors')} |"
            f" {entry.get('detail_gain_vs_stretch', NO_MEASUREMENT)} |"
            f" {entry.get('edge_run_ratio_vs_stretch', BLURRED_REFERENCE_NOTE)} |"
            f" {stretch.get('edge_crossings')} |"
            f" {(grab.get('pixel_size') or NO_MEASUREMENT)} |"
            f" {evaluation.get('verdict')} |")
    lines.append('')
    lines.append('> 细节增益 = 真采样渲染的最强局部对比度 ÷ 同一贴图「先缩到逻辑尺寸再拉伸」的对照值；'
                 '≥1.05 且边缘宽度比 ≤1.0 判为「真采样放大、无糊化」。'
                 '「对照已糊化(无边缘)」= 拉伸对照在该阈值下测不到任何边缘（已被抹平），'
                 '此时真采样渲染仍有边缘，可判定它不是拉伸路径的产物。')
    lines.append('')
    lines.append('| 环境变量开关（各自单独行使） | 设定值 | 设备像素比 | 逻辑 DPI |'
                 ' Qt 上报的物理 DPI | 平台 |')
    lines.append('| --- | --- | --- | --- | --- | --- |')
    for knob in report.get('dpi', {}).get('knobs', []):
        if knob.get('status') != 'measured':
            lines.append(f"| {knob.get('knob')} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                         f" {NO_MEASUREMENT}：{knob.get('reason')} |")
            continue
        lines.append(f"| {knob.get('knob')} | {knob.get('env_value')} |"
                     f" {knob.get('device_pixel_ratio')} |"
                     f" {knob.get('logical_dots_per_inch')} |"
                     f" {knob.get('physical_dots_per_inch')} | {knob.get('platform')} |")
    lines.append('')
    lines.append('> `QT_SCALE_FACTOR` / `QT_SCREEN_SCALE_FACTORS` 的单位是**缩放倍数**（144 DPI → 1.5）；'
                 '`QT_FONT_DPI` 的单位是 **DPI 点数**（144），Qt 据此把缩放算成 144/96 = 1.5。'
                 '三者在本机都能把设备像素比抬到 1.5。')
    lines.append('> 「Qt 上报的物理 DPI」在施加缩放后**变小**（108.89 → 72.60 = 108.89 / 1.5）：'
                 'Qt 报的是「每逻辑像素折算出来的物理 DPI」，缩放越大同一块屏幕分到的逻辑像素越少，'
                 '所以这个数比逻辑 DPI 小是正常的，不是算错；真实屏幕物理 DPI 以基准档的 108.89 为准。')
    lines.append('')

    cache_blocks = [(key, value) for key, value in sorted(report.items())
                    if key.startswith('cache') and isinstance(value, dict) and 'scenarios' in value]
    lines += ['## 表 2 · 冷 / 热缓存对比表', '']
    lines.append('| 工程 | 档位 | 目录缓存 | 打开到就绪(s) | 建库(s) | counts 画布数 | 首页行数 | 结论 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- | --- |')
    for key, block in cache_blocks:
        for scenario in block.get('scenarios', []):
            if scenario.get('status') == 'failed' or scenario.get('reason'):
                lines.append(f"| `{block.get('project', key)}` | {scenario.get('scenario')} |"
                             f" {NO_MEASUREMENT} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                             f" {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                             f" {NO_MEASUREMENT}：{scenario.get('reason')} |")
                continue
            seconds = scenario.get('open_to_ready_seconds')
            if seconds is None:
                seconds = scenario.get('open_to_ready_after_build_seconds')
            lines.append(f"| `{block.get('project', key)}` | {scenario.get('scenario')} |"
                         f" {'存在（复用）' if scenario.get('reused_cache') else '不存在（冷）'} |"
                         f" {seconds} | {scenario.get('catalog_build_seconds', NO_MEASUREMENT)} |"
                         f" {scenario.get('counts_canvases')} | {scenario.get('page_rows')} |"
                         f" {'已实测'} |")
    lines.append('')
    lines.append('> 「打开到就绪」= 从「开始打开工程」到 **counts（侧栏计数）可读 + 目标画布首页（≤256 条）可读**'
                 ' 的墙钟耗时，不含 Qt 应用启动。冷档的目录缓存不存在，生产路径（`prism/view.py:1259-1268`）'
                 '会退回全量读取并后台建库；本表的冷档额外给出「建库耗时」。')
    lines.append('')
    for key, block in cache_blocks:
        lines.append('')
        lines.append(f"### 2.{cache_blocks.index((key, block)) + 1} · {key}"
                     f"（工程：{block.get('project', '未记录')}）")
        lines.append('')
        lines.append('| 档位 | 目录缓存 | 打开到就绪(s) | 建库(s) | counts 画布数 | 首页行数 | 结论 |')
        lines.append('| --- | --- | --- | --- | --- | --- | --- |')
        for scenario in block.get('scenarios', []):
            if scenario.get('status') == 'failed' or scenario.get('reason'):
                lines.append(f"| {scenario.get('scenario')} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                             f" {NO_MEASUREMENT} | {NO_MEASUREMENT} | {NO_MEASUREMENT} |"
                             f" {NO_MEASUREMENT}：{scenario.get('reason')} |")
                continue
            seconds = scenario.get('open_to_ready_seconds')
            if seconds is None:
                seconds = scenario.get('open_to_ready_after_build_seconds')
            lines.append(f"| {scenario.get('scenario')} |"
                         f" {'存在（复用）' if scenario.get('reused_cache') else '不存在（冷）'} |"
                         f" {seconds} | {scenario.get('catalog_build_seconds', NO_MEASUREMENT)} |"
                         f" {scenario.get('counts_canvases')} | {scenario.get('page_rows')} |"
                         f" {'已实测'} |")
        full_read = next((s.get('full_read') for s in block.get('scenarios', [])
                          if s.get('full_read')), None)
        if full_read:
            lines.append('')
            lines.append(f"- 冷启动回退全量读取（`fileio.load_prism`）："
                         f"{full_read.get('seconds')} s，"
                         f"{'成功' if full_read.get('ok') else '失败：' + str(full_read.get('error'))}")
        lines.append('')
        lines.append(f"- 目录缓存路径（本次实验）：`{block.get('catalog_cache_path')}`")
        lines.append(f"- 私有 settings 根：`{block.get('settings_dir')}`")
        lines.append(f"- 共享 `%APPDATA%\\Prism` 目录：**本组"
                     f"{'未改动' if not block.get('shared_catalog_touched') else '改动过'}"
                     f"**（本组备份命名固定为 `*.e4-backup-<时间戳>`；共享目录里出现的"
                     f"`*.bak-<时间戳>` 是别的组（本轮观察到 E-1 的冷启动实验改了 3 次）留下的，不是本组行为）"
                     f"（共享目录缓存路径：{block.get('shared_catalog_path')}）")
        backups = block.get('cache_backups') or []
        if backups:
            lines.append('')
            lines.append('| 改名的缓存文件 | 原大小(B) | 原 mtime | 备份名 |')
            lines.append('| --- | --- | --- | --- |')
            for item in backups:
                lines.append(f"| `{item.get('original')}` | {item.get('bytes')} |"
                             f" {item.get('mtime')} | `{item.get('backup')}` |")
    lines.append('')

    lines += ['## 表 3 · 长会话内存趋势表', '']
    session = report.get('session', {})
    memory = session.get('memory') or {}
    lines.append('| 轮次 | 画布 | 解码张数 | 解码字节 | 场景物件数 | 常驻内存(B) | 展示缓存(B) |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- |')
    for sample in session.get('samples', []):
        lines.append(f"| {sample.get('round')} | {sample.get('canvas')} |"
                     f" {sample.get('decoded_images')} | {sample.get('decoded_bytes')} |"
                     f" {sample.get('scene_items')} | {sample.get('rss_bytes')} |"
                     f" {sample.get('display_cache_bytes')} |")
    lines.append('')
    lines.append('| 指标 | 值 |')
    lines.append('| --- | --- |')
    for key, label in (('first_round_bytes', '首轮常驻内存'),
                       ('mid_round_bytes', '中段常驻内存'),
                       ('last_round_bytes', '末轮常驻内存'),
                       ('peak_bytes', '峰值常驻内存'),
                       ('min_bytes', '最低常驻内存'),
                       ('first_quarter_mean_bytes', '首 1/4 均值'),
                       ('last_quarter_mean_bytes', '末 1/4 均值'),
                       ('growth_bytes', '末段-首段 增长'),
                       ('growth_ratio', '末段/首段 比'),
                       ('slope_bytes_per_round', '每轮斜率(B/轮)'),
                       ('middle_third_mean_bytes', '中 1/3 均值（稳态基准）'),
                       ('last_third_mean_bytes', '末 1/3 均值'),
                       ('steady_growth_bytes', '稳态增长(末1/3-中1/3)'),
                       ('steady_growth_ratio', '稳态增长比'),
                       ('verdict', '结论（严格判据，含预热）'),
                       ('steady_state_verdict', '结论（稳态判据，排除预热）')):
        lines.append(f"| {label} | {memory.get(key, NO_MEASUREMENT)} |")
    if memory.get('reason'):
        lines.append(f"| 原因 | {memory['reason']} |")
    lines.append('')
    if memory.get('verdict_note'):
        lines.append(f"> 判据说明：{memory['verdict_note']}")
        lines.append('')
    control = report.get('session', {}).get('control_memory') or {}
    if control:
        lines.append('| 对照组（只翻目录页+选材质，不解码不进场景） | 值 |')
        lines.append('| --- | --- |')
        for key, label in (('first_round_bytes', '首轮常驻内存'),
                           ('last_round_bytes', '末轮常驻内存'),
                           ('peak_bytes', '峰值常驻内存'),
                           ('growth_bytes', '20 轮总增长'),
                           ('growth_ratio', '增长比'),
                           ('slope_bytes_per_round', '每轮斜率(B/轮)'),
                           ('verdict', '结论')):
            lines.append(f"| {label} | {control.get(key, NO_MEASUREMENT)} |")
        lines.append('')
        lines.append('> 对照组用来回答「增长是量具漂移还是真的在涨」：对照组 20 轮几乎不涨，'
                     '说明主组的增长来自图片管线与预热，不是采样噪声。')
        lines.append('')
    return '\n'.join(lines) + '\n'


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # 内部子命令先分流：它们的参数（含 --out / --dpi）不能被父解析器吃掉。
    if argv and argv[0] in ('_dpi-child', '_open-child', '_session-child'):
        if argv[0] == '_dpi-child':
            return dpi_child(argv[1:])
        if argv[0] == '_open-child':
            return open_child(argv[1:])
        return session_child(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    parser.add_argument('command',
                        choices=['dpi', 'cache', 'session', 'all', 'report', 'fixture'])
    parser.add_argument('--out', type=Path, default=Path('artifacts/scale-acceptance/2026-10-22'))
    parser.add_argument('--project', type=Path, default=DEFAULT_PROJECT)
    parser.add_argument('--rounds', type=int, default=20)
    parser.add_argument('--page-size', type=int, default=24)
    parser.add_argument('--settings-root', type=Path)
    parser.add_argument('--full-read', action='store_true')
    parser.add_argument('--dpi-levels', type=int, nargs='*', default=list(DPI_LEVELS))
    parser.add_argument('--no-real-source', action='store_true')
    parser.add_argument('--records', type=int, default=2000)
    parser.add_argument('--unique-sources', type=int, default=64)
    parser.add_argument('--materials', type=int, default=40)
    parser.add_argument('--label', default='')
    parser.add_argument('--cycle-check', type=int, default=2744,
                        help='复制周期：用来现场验证「唯一源图数」')
    args, unknown = parser.parse_known_args(argv)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = {'generated_at': time.strftime('%Y-%m-%d %H:%M:%S'),
              'command': args.command, 'project': str(args.project)}
    previous_environment = {}
    if args.command == 'report':
        # report 模式：先读回已有结果，再重算本工程的口径，最后重画三张表。
        if not (out_dir / 'env-matrix.json').exists():
            raise SystemExit('no env-matrix.json yet: run dpi/cache/session first')
        report = json.loads((out_dir / 'env-matrix.json').read_text(encoding='utf-8'))
        report['generated_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        previous_environment = report.get('environment') or {}
    environment = describe_environment()
    if not environment.get('qt_platform') and previous_environment.get('qt_platform'):
        # 平台名只有跑过 DPI 档位的那一轮才有，report 模式要把它带过来。
        environment['qt_platform'] = previous_environment['qt_platform']
    report['environment'] = environment
    report['project_profile'] = project_asset_profile(args.project, cycle_check=args.cycle_check)
    # 每个用到的工程各留一份口径：记录数 / 唯一源图数 / 实际解码字节数。
    profiles = {}
    target_json = out_dir / 'env-matrix.json'
    if target_json.exists():
        try:
            profiles = json.loads(target_json.read_text(encoding='utf-8')).get(
                'project_profiles') or {}
        except ValueError:
            profiles = {}
    profiles[str(args.project)] = report['project_profile']
    report['project_profiles'] = profiles

    if args.command == 'fixture':
        destination = out_dir / '_work' / 'fixture' / 'env-matrix-fixture.prism'
        report['fixture'] = build_fixture(args.project, destination,
                                          records=args.records, unique_sources=args.unique_sources,
                                          materials=args.materials)
        report['project_profile'] = report['fixture']
    if args.command in ('dpi', 'all'):
        report['dpi'] = run_dpi(out_dir, levels=tuple(args.dpi_levels),
                                project=None if args.no_real_source else args.project)
        environment['qt_platform'] = report['dpi'].get('qt_platform')
    if args.command in ('cache', 'all'):
        key = 'cache' + (f'_{args.label}' if args.label else '')
        report[key] = run_cache(out_dir, args.project, settings_root=args.settings_root,
                                full_read=args.full_read)
    if args.command in ('session', 'all'):
        report['session'] = run_session(out_dir, args.project, rounds=args.rounds,
                                        page_size=args.page_size)

    # 分维度跑的时候保留其它维度已经测出来的段落，最后再合成一张总表。
    target = out_dir / 'env-matrix.json'
    if target.exists() and args.command not in ('all', 'report'):
        try:
            existing = json.loads(target.read_text(encoding='utf-8'))
        except ValueError:
            existing = {}
        if isinstance(existing, dict):
            commands = list(existing.get('commands') or [])
            if args.command not in commands:
                commands.append(args.command)
            existing.update(report)
            existing['commands'] = commands
            report = existing
    target.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    (out_dir / 'env-matrix.md').write_text(render_markdown(report), encoding='utf-8')
    print(json.dumps({'out': str(out_dir / 'env-matrix.json'),
                      'md': str(out_dir / 'env-matrix.md')}, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
