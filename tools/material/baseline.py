"""材质功能逐项不退化基线（E-3 组）。

给一组材质记录，**逐项**回答"这一项材质能力还在不在"：每项能力一条独立结论
（通过 / 失败 / 未验收），并给出证据位置。整体没报错不算结论。

三层证据，从便宜到贵：

1. **契约层** —— 记录里的绑定 / 通道 / 槽位设置能不能走到渲染载荷
   （``prism.material_textures.prepare_render``）。这一层不需要 GPU。
2. **像素层** —— 渲染出来的 PNG 是不是还在传递真实信息：平均亮度、标准差、
   唯一颜色数、非纯色块比例。纯色块 = 退化。
3. **GPU 层** —— 真的走一遍 Babylon.js（``prism/widgets/material_render.py``
   的生产路径），抓产品自己那张 256x256 预览图，并读回
   ``prismRender.diagnostics``。

**尺寸口径**：喂给 GPU 的贴图是**真实尺寸**的真实贴图，脚本里不做任何降采样。
产品自己的预览截图是 256x256（``prism/assets/materials/render.js`` 里的
``CreateScreenshotUsingRenderTargetAsync``），分析只作用在这张产品输出上 ——
降采样的是**分析对象**，不是**渲染输入**。

**规模口径**：记录数、唯一源图数、实际解码字节数三项分开列。这份 10 万条工程
是 2744 张原图复制出来的，记录规模不等于解码负载。

用法::

    python tools/material/baseline.py --project E:/Prism-stress-100k/Prism-100k-v1.prism \\
        --out artifacts/scale-acceptance/2026-10-22 --limit 24
    python tools/material/baseline.py --capabilities-only --out <dir>

只读打开工程：SQLite 以 ``mode=ro`` 打开，绝不写回。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PIL import Image, ImageChops, ImageDraw, ImageStat


# ── 口径常量 ────────────────────────────────────────────────────────────

#: 渲染预览截图的产品分辨率（prism/assets/materials/render.js）。
CAPTURE_SIZE = 256

#: 一个块被判为"纯色"的亮度标准差上限。
FLAT_BLOCK_LUMA_STD = 0.5

#: 退化判据：非纯色块比例、唯一颜色数、整体亮度标准差。
MIN_NON_FLAT_BLOCK_RATIO = 0.05
MIN_UNIQUE_COLORS = 32
MIN_LUMA_STD = 1.0

#: 贴图边长。能力探针用真实尺寸（与真实工程同级的 1024），不缩图。
PROBE_TEXTURE_SIZE = 1024

#: 状态词只有六个。
STATUS_PENDING = '未开始'
STATUS_RUNNING = '开发中'
STATUS_LANDED = '代码已接未验收'
STATUS_PARTIAL = '部分验收'
STATUS_DONE = '完成'
STATUS_BLOCKED = '阻塞'

#: 逐项结论的三个取值。
VERDICT_PASS = '通过'
VERDICT_FAIL = '失败'
VERDICT_UNVERIFIED = '未验收'

#: 落盘重试（Windows 文件锁）。上限固定：最多 5 次重试，
#: 退避 50 / 100 / 200 / 400 / 800 ms，总退避上限 1.55 s。
#: 只在 ``os.replace`` 撞到瞬时文件锁时发生，是**写盘 I/O 重试**，
#: 不是渲染路径的 sleep、不是忙等、不是拿小图替代高清。
WRITE_RETRY_LIMIT = 5
WRITE_RETRY_BACKOFF_MS = (50, 100, 200, 400, 800)

#: 落盘统计：写了几次、靠重试成功了几次、重试了几次、最终失败了几次。
#: 如实写进最终产物，不藏。
_write_stats = {'writes': 0, 'retries': 0, 'retry_successes': 0,
                 'failures': 0, 'last_error': None}


# ── 通用工具 ────────────────────────────────────────────────────────────

def stats(values):
    """The shape the existing scale-audit reports use (tools/scale_acceptance.py)."""
    values = sorted(values)
    if not values:
        return {}
    return dict(count=len(values),
                median_ms=round(statistics.median(values), 2),
                p95_ms=round(values[min(len(values) - 1, int(len(values) * .95))], 2),
                max_ms=round(max(values), 2))


def _luma(pixel):
    r, g, b = pixel[:3]
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _fingerprint(image, grid=16):
    """Structural fingerprint: a grid of block means, hashed.

    Two renders of the same texture under different settings differ in their
    block means, and two different textures differ as well -- so the
    fingerprint separates "same image, different render" from "different
    image" without pretending to be perceptually lossless.
    """
    width, height = image.size
    cells = []
    for gy in range(grid):
        for gx in range(grid):
            left, top = gx * width // grid, gy * height // grid
            right = max(left + 1, (gx + 1) * width // grid)
            bottom = max(top + 1, (gy + 1) * height // grid)
            region = image.crop((left, top, right, bottom))
            cells.append(tuple(int(value) for value in ImageStat.Stat(region).mean))
    return hashlib.sha256(repr(cells).encode('utf-8')).hexdigest()


def _rgb_pixels(image):
    """Per-pixel RGB tuples without Pillow's deprecated getdata()."""
    data = image.tobytes()
    return list(zip(data[0::3], data[1::3], data[2::3]))


def analyze_png(blob, block=8):
    """Pixel facts about a captured render.

    ``non_flat_block_ratio`` is the "纯色块" detector: the capture is cut into
    ``block`` x ``block`` tiles and a tile counts as flat when it is a single
    colour.  A degraded render (solid placeholder, missing texture) drives
    that ratio to zero.
    """
    with Image.open(io.BytesIO(blob)) as raw:
        image = raw.convert('RGB')
        width, height = image.size
        pixels = _rgb_pixels(image)
    count = len(pixels)
    luma = [_luma(pixel) for pixel in pixels]
    mean = sum(luma) / count if count else 0.0
    variance = sum((value - mean) ** 2 for value in luma) / count if count else 0.0
    std = math.sqrt(variance)
    unique = len(set(pixels))

    tile_width = max(1, width // block)
    tile_height = max(1, height // block)
    flat = 0
    tiles = 0
    for ty in range(block):
        for tx in range(block):
            box = (tx * tile_width, ty * tile_height,
                   min((tx + 1) * tile_width, width), min((ty + 1) * tile_height, height))
            region = _rgb_pixels(image.crop(box))
            if not region:
                continue
            tiles += 1
            region_luma = [_luma(pixel) for pixel in region]
            region_mean = sum(region_luma) / len(region_luma)
            region_var = sum((value - region_mean) ** 2 for value in region_luma) / len(region_luma)
            if len(set(region)) == 1 and math.sqrt(region_var) < FLAT_BLOCK_LUMA_STD:
                flat += 1
    non_flat = 1.0 - (flat / tiles if tiles else 1.0)
    degraded = (non_flat < MIN_NON_FLAT_BLOCK_RATIO
                or unique < MIN_UNIQUE_COLORS
                or std < MIN_LUMA_STD)
    return dict(
        width=width,
        height=height,
        pixel_sha256=hashlib.sha256(image.tobytes()).hexdigest(),
        fingerprint=_fingerprint(image),
        mean_luma=round(mean, 4),
        std_luma=round(std, 4),
        unique_colors=unique,
        blocks=tiles,
        flat_blocks=flat,
        non_flat_block_ratio=round(non_flat, 6),
        degraded=bool(degraded),
    )


def _atomic_write(path, text):
    """Write through a temp file and replace it in one step.

    The report is rewritten after every rendered record, so a run that is
    killed half-way through a write must not leave a half-written file
    behind: a truncated JSON would read as "no records" and the shard would
    be lost.

    **Windows 上的文件锁**：``os.replace()`` 在目标文件被瞬时占用时
    （杀软 / 索引器 / 另一个读者短暂持有）会抛 ``PermissionError
    [WinError 5]``。报告每渲染一条就整份重写一次，一次失败的 replace
    如果直接往上抛，**整片会死掉**，而这一片已经渲染出来的成果全都留在
    那个 shard 目录里 —— 白跑。所以这里做**有限次退避重试**：

    * 最多重试 ``WRITE_RETRY_LIMIT`` 次，退避 50 / 100 / 200 / 400 / 800 ms；
    * 重试前确认临时文件还在，不在就重写一份（避免撞上别人留下的同名 tmp）；
    * 重试仍失败就**放弃这一次落盘**、删掉临时文件、记一条 warning，
      **不往上抛** —— 快照只是落后一次，下一次 ``persist()`` 会把
      最新的行重新写上去。

    > **这里的 sleep 是文件落盘的 I/O 重试退避**，只在写盘失败时发生、
    > 次数与总时长都有固定上限。**不是**拿 sleep 充数、**不是**渲染路径里的
    > 忙等、**不是**用小图替代高清掩盖卡顿（渲染路径里没有任何 sleep）。
    """
    path = Path(path)
    temp = path.with_name(path.name + '.tmp')
    temp.write_text(text, encoding='utf-8')
    _write_stats['writes'] += 1
    last_error = None
    for attempt in range(WRITE_RETRY_LIMIT + 1):
        try:
            os.replace(temp, path)
            if attempt:
                _write_stats['retries'] += attempt
                _write_stats['retry_successes'] += 1
            _write_stats['last_error'] = None
            return path
        except OSError as error:
            last_error = error
            if attempt >= WRITE_RETRY_LIMIT:
                break
            delay = WRITE_RETRY_BACKOFF_MS[min(attempt, len(WRITE_RETRY_BACKOFF_MS) - 1)]
            time.sleep(delay / 1000)
            if not temp.exists():
                # 临时文件被别人动过（上次失败留下的同名 tmp 被换掉了）——
                # 重新写一份再试，别拿半个文件去 replace。
                temp.write_text(text, encoding='utf-8')
    # 重试仍失败：记下来，不让整片崩掉。
    _write_stats['failures'] += 1
    _write_stats['last_error'] = repr(last_error)
    try:
        if temp.exists():
            temp.unlink()
    except OSError:
        pass
    print(f'[warn] 落盘失败（已重试 {WRITE_RETRY_LIMIT} 次，放弃本次落盘）：'
          f'{path} -> {last_error!r}', flush=True)
    return None


def write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2))
    return path


# ── 只读工程读取 ────────────────────────────────────────────────────────

class MaterialProject:
    """Read-only view of a .prism project's material records and originals."""

    def __init__(self, path):
        self.path = Path(path)
        if not self.path.is_file():
            raise SystemExit(f'project not found: {self.path}')
        self.connection = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)

    def close(self):
        self.connection.close()

    def records(self):
        cursor = self.connection.execute(
            'SELECT payload FROM material_records ORDER BY rowid')
        for (payload,) in cursor:
            yield json.loads(payload)

    def attachment(self, attachment_id):
        row = self.connection.execute(
            'SELECT name, mime, blob FROM attachments WHERE id = ?',
            (attachment_id,)).fetchone()
        if not row:
            return None
        return row[0], row[1], row[2]

    def attachment_count(self):
        return self.connection.execute('SELECT count(*) FROM attachments').fetchone()[0]


# ── 能力探针贴图（真实尺寸，不降采样）──────────────────────────────────

def _structure(size, seed=0):
    """A deterministic, non-flat grey field: noise times a gradient."""
    noise = Image.effect_noise((size, size), 42).convert('L')
    gradient = Image.linear_gradient('L').resize((size, size))
    field = ImageChops.multiply(noise, gradient.point(lambda value: 128 + value // 2))
    if seed:
        draw = ImageDraw.Draw(field)
        for index in range(6):
            offset = (seed * 37 + index * 53) % size
            draw.rectangle([offset, offset // 2, offset + size // 5, offset // 2 + size // 7],
                           outline=min(255, offset % 255), width=3)
    return field


def probe_texture(kind, size=PROBE_TEXTURE_SIZE):
    """Build one real-size probe texture.  Never downsampled."""
    red = _structure(size, 1)
    green = _structure(size, 2)
    blue = _structure(size, 3)
    if kind == 'base':
        return Image.merge('RGB', (red, green, blue))
    if kind == 'base_rgba':
        alpha = Image.new('L', (size, size), 77)
        return Image.merge('RGBA', (red, green, blue, alpha))
    if kind == 'base_binary_alpha':
        image = Image.merge('RGBA', (red, green, blue, Image.new('L', (size, size), 255)))
        draw = ImageDraw.Draw(image)
        draw.ellipse([size * .18, size * .18, size * .82, size * .82], fill=(0, 0, 0, 0))
        return image
    if kind == 'base_gradient_alpha':
        alpha = Image.linear_gradient('L').resize((size, size))
        return Image.merge('RGBA', (red, green, blue, alpha))
    if kind == 'gray':
        return _structure(size, 4)
    if kind == 'normal':
        flat = Image.new('L', (size, size), 128)
        draw = ImageDraw.Draw(flat)
        for index in range(24):
            y = index * size // 24
            draw.line([0, y, size, y + size // 40], fill=90 + index * 6, width=5)
        return Image.merge('RGB', (flat, _structure(size, 5), flat))
    if kind == 'emissive':
        field = _structure(size, 6)
        return Image.merge('RGB', (field.point(lambda v: min(255, v + 90)),
                                   field.point(lambda v: min(255, v // 2)),
                                   field.point(lambda v: min(255, v // 3))))
    raise ValueError('unknown probe texture: ' + kind)


def _slot_binding(texture, attachment_id, channel, name, mime='image/png'):
    return dict(name=name, width=texture.width, height=texture.height,
                size=len(texture.tobytes()), sha256='', mime=mime,
                grayscale=False, hasAlpha='A' in texture.getbands(),
                extrema=None, attachmentId=attachment_id, channel=channel,
                colorSpace='sRGB' if channel == 'RGB' else 'linear')


class ProbeSet:
    """Textures plus the attachments dict the render path expects."""

    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.attachments = {}
        self.blobs = {}
        self._next_id = 1

    def add(self, kind, name, channel):
        texture = probe_texture(kind)
        path = self.directory / name
        texture.save(path)
        blob = path.read_bytes()
        attachment_id = self._next_id
        self._next_id += 1
        self.attachments[attachment_id] = (name, 'image/png', blob)
        self.blobs[attachment_id] = blob
        binding = _slot_binding(texture, attachment_id, channel, name)
        binding['sha256'] = hashlib.sha256(blob).hexdigest()
        return binding


# ── 能力定义：每一项一条独立结论 ───────────────────────────────────────

def _record(textures, properties=None):
    return dict(id='probe', projectId='probe', schemaVersion=1, name='probe',
                notes='', folderId=None, tags=[], textures=textures,
                preview={'properties': dict(properties or {})},
                nativePackages=[], status='draft',
                createdAt='2026-10-22T00:00:00.000000+00:00',
                updatedAt='2026-10-22T00:00:00.000000+00:00', deletedAt=None)


#: capability id -> the pytest case that pins the same contract.
CAPABILITY_TESTS = {
    'opaque': 'test_capability_opaque_reaches_the_render_payload',
    'masked': 'test_capability_masked_with_alpha_cutoff_reaches_the_render_payload',
    'translucent': 'test_capability_translucent_reaches_the_render_payload',
    'metallic': 'test_capability_metallic_lands_in_the_packed_blue_channel',
    'roughness': 'test_capability_roughness_lands_in_the_packed_green_channel',
    'normal': 'test_capability_normal_slot_and_convention_reach_the_render_payload',
    'ao': 'test_capability_ao_reaches_the_render_payload',
    'emissive': 'test_capability_emissive_reaches_the_render_payload',
    'sss': 'test_capability_sss_reaches_the_render_payload',
    'channel_rgb': 'test_capability_channel_rgb_picks_the_whole_colour_image',
    'channel_r': 'test_capability_channel_r_picks_the_red_channel',
    'channel_g': 'test_capability_channel_g_picks_the_green_channel',
    'channel_b': 'test_capability_channel_b_picks_the_blue_channel',
    'channel_a': 'test_capability_channel_a_picks_the_alpha_channel',
}

#: The ten items the handoff asks for.  The fifth slot item -- custom channel --
#: is expanded into one verdict per channel.
CAPABILITY_GROUPS = {
    'opaque': ('1', '不透明'),
    'masked': ('2', '透明裁切'),
    'translucent': ('3', '半透明'),
    'metallic': ('4', '金属'),
    'roughness': ('5', '粗糙'),
    'normal': ('6', '法线'),
    'ao': ('7', 'AO'),
    'emissive': ('8', '自发光'),
    'sss': ('9', 'SSS'),
    'channel_rgb': ('10', '自定义通道'),
    'channel_r': ('10', '自定义通道'),
    'channel_g': ('10', '自定义通道'),
    'channel_b': ('10', '自定义通道'),
    'channel_a': ('10', '自定义通道'),
}


def build_probes(probe_dir):
    """Every probe is a real material record plus its real original files."""
    probes = ProbeSet(probe_dir)
    definitions = []

    def add(capability_id, name, textures, properties, evidence_note, extra=None):
        record = _record(textures, properties)
        record['files'] = [dict(binding) for binding in textures.values()]
        for binding in record['files']:
            binding.pop('channel', None)
            binding.pop('colorSpace', None)
        number, group = CAPABILITY_GROUPS.get(capability_id, ('-', name))
        item = dict(id=capability_id, capability=name, record=record,
                    attachments=probes.attachments, properties=properties,
                    note=evidence_note, group_number=number, group=group,
                    role='pair' if extra and extra.get('pair') else 'primary')
        if extra:
            item.update(extra)
        definitions.append(item)
        return item

    base = probes.add('base', 'probe_base.png', 'RGB')
    base_alpha_mask = probes.add('base_binary_alpha', 'probe_mask.png', 'RGB')
    base_alpha_grad = probes.add('base_gradient_alpha', 'probe_glass.png', 'RGB')
    gray = probes.add('gray', 'probe_gray.png', 'R')
    normal = probes.add('normal', 'probe_normal.png', 'RGB')
    emissive = probes.add('emissive', 'probe_emissive.png', 'RGB')

    add('opaque', '不透明',
        {'basecolor': dict(base)}, {'blendMode': 'opaque'},
        'basecolor 不带 alpha -> 透明输入完全不透明')
    add('masked', '透明裁切',
        {'basecolor': dict(base_alpha_mask)}, {'blendMode': 'masked', 'alphaCutoff': 0.5},
        '黑白遮罩 -> masked；alphaCutoff 落在 properties.alphaCutoff')
    add('translucent', '半透明',
        {'basecolor': dict(base_alpha_grad)}, {'blendMode': 'translucent'},
        '连续灰阶 alpha -> translucent')
    add('metallic', '金属',
        {'basecolor': dict(base), 'metallic': dict(gray)}, {},
        '金属度贴图进 Babylon 打包贴图的蓝通道')
    add('roughness', '粗糙',
        {'basecolor': dict(base), 'roughness': dict(gray)}, {},
        '粗糙度贴图进 Babylon 打包贴图的绿通道')
    add('normal', '法线',
        {'basecolor': dict(base), 'normal': dict(normal)},
        {'normalConvention': 'DirectX'},
        '法线必须是整张 RGB；DirectX / OpenGL 决定绿通道是否翻转')
    add('ao', 'AO',
        {'basecolor': dict(base), 'ao': dict(gray)}, {},
        'AO 贴图走 ambientTexture')
    add('emissive', '自发光',
        {'basecolor': dict(base), 'emissive': dict(emissive)}, {},
        '自发光贴图走 emissiveTexture')
    add('sss', 'SSS',
        {'basecolor': dict(base), 'sss': dict(gray)}, {},
        'SSS 贴图打开 Babylon 的薄表面透射')

    for channel in ('RGB', 'R', 'G', 'B', 'A'):
        binding = probes.add('base_rgba', f'probe_channel_{channel}.png', channel)
        add(f'channel_{channel.lower()}', f'自定义通道 {channel}',
            {'basecolor': dict(binding)}, {},
            f'通道 {channel} 的像素必须真的被选中')

    # 法线约定：同一张法线贴图，DirectX 与 OpenGL 必须给出不同的渲染。
    add('normal_convention', '法线约定（DirectX / OpenGL）',
        {'basecolor': dict(base), 'normal': dict(normal)},
        {'normalConvention': 'OpenGL'},
        '与 normal 项配对：同一张法线贴图、相反的约定', {'pair': 'normal'})

    # 裁切阈值：同一张遮罩，两个阈值必须给出不同的渲染。
    add('alpha_cutoff', '裁切阈值（0.2）',
        {'basecolor': dict(base_alpha_mask)}, {'blendMode': 'masked', 'alphaCutoff': 0.2},
        '与 masked 项配对：同一张遮罩、相反的阈值', {'pair': 'masked'})
    add('alpha_cutoff_high', '裁切阈值（0.8）',
        {'basecolor': dict(base_alpha_mask)}, {'blendMode': 'masked', 'alphaCutoff': 0.8},
        '与 alpha_cutoff 项配对：同一张遮罩、相反的阈值', {'pair': 'masked'})
    return definitions


# ── GPU 渲染（走产品自己的预览控件）────────────────────────────────────

class BabylonRenderer:
    """Drive prism.widgets.material_render.MaterialRender and capture its PNG.

    The widget is the production preview path; the script only drives it.
    Waiting is done with a QEventLoop -- no processEvents busy-wait, no sleep.
    """

    def __init__(self, size=CAPTURE_SIZE):
        from PyQt6 import QtWidgets
        self.app = QtWidgets.QApplication.instance()
        if self.app is None:
            # The production app class: it is what the preview widget expects
            # (WebEngine, palette, settings dir).  argv must not be empty --
            # see tests/conftest.py for the crash an empty argv causes.
            from prism.__main__ import PrismApplication
            self.app = PrismApplication([sys.argv[0] or 'prism'])
        from prism.widgets.material_render import MaterialRender
        self.viewer = MaterialRender()
        self.viewer.resize(size, size)
        self.viewer.show()
        self.size = size
        self.available = True
        self.reason = ''
        self.webgl = ''

    def close(self):
        try:
            self.viewer.release_material()
            self.viewer.close()
        except Exception:
            pass

    def _wait(self, predicate, timeout_ms):
        from PyQt6 import QtCore
        loop = QtCore.QEventLoop()
        state = {'done': False, 'error': ''}

        def finish(error=''):
            state['done'] = True
            state['error'] = error
            loop.quit()

        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: finish('timeout'))
        timer.start(timeout_ms)
        try:
            if not predicate(finish):
                loop.exec()
        finally:
            timer.stop()
        return state

    def render(self, payload, token, timeout_ms=60000):
        """Return {'blob': bytes} or {'error': str}."""
        from PyQt6 import QtCore
        result = {}

        def on_completed(_signature, blob):
            result['blob'] = blob
            loop.quit()

        def on_failed(message):
            result['error'] = message
            loop.quit()

        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: (result.setdefault('error', 'timeout'), loop.quit()))
        self.viewer.completed.connect(on_completed)
        self.viewer.failed.connect(on_failed)
        timer.start(timeout_ms)
        try:
            self.viewer.load_material(payload, token)
            loop.exec()
        finally:
            timer.stop()
            self.viewer.completed.disconnect(on_completed)
            self.viewer.failed.disconnect(on_failed)
        return result

    def diagnostics(self, timeout_ms=8000):
        """Read back the material state Babylon actually built.

        The JavaScript answer arrives on the GUI thread, so the wait is a
        QEventLoop -- not a processEvents busy-wait and not a sleep.
        """
        from PyQt6 import QtCore
        values = []
        loop = QtCore.QEventLoop()
        timer = QtCore.QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        timer.start(timeout_ms)
        try:
            self.viewer.web.page().runJavaScript(
                'window.prismRender && window.prismRender.diagnostics ? '
                'window.prismRender.diagnostics : null',
                lambda value: (values.append(value), loop.quit()))
            if not values:
                loop.exec()
        except Exception as error:
            return {'error': str(error)}
        finally:
            timer.stop()
        return values[0] if values else None


# ── 逐项结论 ────────────────────────────────────────────────────────────

def _contract_check(probe, payload):
    """Contract layer: did the capability reach the render payload?"""
    capability = probe['id']
    properties = payload['properties']
    maps = payload['maps']
    checks = []
    if capability == 'opaque':
        checks.append(('blendMode == opaque', properties.get('blendMode') == 'opaque'))
    elif capability == 'masked':
        checks.append(('blendMode == masked', properties.get('blendMode') == 'masked'))
        checks.append(('alphaCutoff 落在 0..1',
                       0 <= properties.get('alphaCutoff', -1) <= 1))
        checks.append(('opacity 贴图进入渲染载荷', 'opacity' in maps))
    elif capability == 'translucent':
        checks.append(('blendMode == translucent',
                       properties.get('blendMode') == 'translucent'))
    elif capability == 'metallic':
        checks.append(('metallicRoughness 打包贴图存在', 'metallicRoughness' in maps))
    elif capability == 'roughness':
        checks.append(('metallicRoughness 打包贴图存在', 'metallicRoughness' in maps))
    elif capability == 'normal':
        checks.append(('normal 贴图进入渲染载荷', 'normal' in maps))
        checks.append(('normalConvention 已携带',
                       properties.get('normalConvention') in ('DirectX', 'OpenGL')))
    elif capability == 'ao':
        checks.append(('ao 贴图进入渲染载荷', 'ao' in maps))
    elif capability == 'emissive':
        checks.append(('emissive 贴图进入渲染载荷', 'emissive' in maps))
    elif capability == 'sss':
        checks.append(('sss 贴图进入渲染载荷', 'sss' in maps))
    elif capability.startswith('channel_'):
        checks.append(('通道贴图进入渲染载荷', 'basecolor' in maps))
    elif capability in ('normal_convention', 'alpha_cutoff', 'alpha_cutoff_high'):
        checks.append(('blendMode 已携带', 'blendMode' in properties))
    passed = all(ok for _, ok in checks)
    return passed, [f'{name}: {"是" if ok else "否"}' for name, ok in checks]


def _expected_channel_value(channel, source):
    """What the prepared map must contain for a given channel selection."""
    if channel == 'RGB':
        return tuple(source.getpixel((0, 0))[:3])
    index = {'R': 0, 'G': 1, 'B': 2, 'A': 3}[channel]
    value = source.getpixel((0, 0))[index]
    return (value, value, value)


def run_capability_pass(probe_dir, work_dir, renderer, timeout_ms, persist,
                        renderer_reason=''):
    """One independent verdict per capability."""
    from prism.material_textures import prepare_render, selected_image

    definitions = build_probes(probe_dir)
    results = []
    for definition in definitions:
        capability = definition['id']
        entry = dict(id=capability, capability=definition['capability'],
                     group_number=definition.get('group_number', '-'),
                     group=definition.get('group', definition['capability']),
                     role=definition.get('role', 'primary'),
                     layers={}, verdict=VERDICT_UNVERIFIED, notes=[],
                     evidence=[])
        started = time.perf_counter()
        try:
            render_dir = Path(work_dir) / capability
            payload = prepare_render(definition['record'], definition['attachments'],
                                     render_dir)
            contract_ok, contract_notes = _contract_check(definition, payload)
            entry['layers']['contract'] = dict(
                verdict=VERDICT_PASS if contract_ok else VERDICT_FAIL,
                notes=contract_notes,
                maps=sorted(payload['maps']),
                blendMode=payload['properties'].get('blendMode'),
                alphaCutoff=payload['properties'].get('alphaCutoff'),
                normalConvention=payload['properties'].get('normalConvention'),
            )
            entry['evidence'].append(
                f'material-baseline.json#capabilities.{capability}.layers.contract')
            test_name = CAPABILITY_TESTS.get(capability)
            if test_name:
                entry['evidence'].append(
                    f'tests/test_material_baseline.py::{test_name}')

            # 通道项再核一次：准备出来的贴图里，选中的通道像素必须真的在那儿。
            if capability.startswith('channel_'):
                channel = capability.split('_', 1)[1].upper()
                binding = definition['record']['textures']['basecolor']
                blob = definition['attachments'][binding['attachmentId']][2]
                with Image.open(io.BytesIO(blob)) as raw:
                    expected = _expected_channel_value(channel, raw)
                with Image.open(render_dir / 'basecolor.png') as prepared:
                    actual = prepared.convert('RGB').getpixel((0, 0))
                ok = actual == expected
                entry['layers']['contract']['notes'].append(
                    f'选中通道像素 {channel}: 期望 {expected} 实际 {actual} -> {"是" if ok else "否"}')
                entry['layers']['contract']['verdict'] = (
                    VERDICT_PASS if ok and contract_ok else VERDICT_FAIL)

            # GPU 层
            if renderer is None:
                entry['layers']['gpu'] = dict(
                    verdict=VERDICT_UNVERIFIED,
                    notes=['GPU 像素层无实测：' + (renderer_reason or '预览不可用')])
            else:
                gpu = dict(verdict=VERDICT_UNVERIFIED, notes=[])
                try:
                    result = renderer.render(payload, 'baseline-' + capability,
                                             timeout_ms=timeout_ms)
                    if 'error' in result:
                        gpu['verdict'] = VERDICT_FAIL
                        gpu['notes'].append('渲染失败：' + str(result['error']))
                    else:
                        analysis = analyze_png(result['blob'])
                        gpu['analysis'] = analysis
                        gpu['notes'].append(
                            f"截图 {analysis['width']}x{analysis['height']}，"
                            f"平均亮度 {analysis['mean_luma']}，标准差 {analysis['std_luma']}，"
                            f"唯一颜色数 {analysis['unique_colors']}，"
                            f"非纯色块比例 {analysis['non_flat_block_ratio']}")
                        gpu['notes'].append('像素指纹 ' + analysis['fingerprint'])
                        if analysis['degraded']:
                            gpu['verdict'] = VERDICT_FAIL
                            gpu['notes'].append('像素层判定为退化（纯色块 / 颜色数不足）')
                        else:
                            gpu['verdict'] = VERDICT_PASS
                        diagnostics = renderer.diagnostics()
                        gpu['diagnostics'] = diagnostics
                        if isinstance(diagnostics, dict):
                            if capability in ('opaque', 'masked', 'translucent'):
                                gpu['notes'].append(
                                    'Babylon 模式 ' + str(diagnostics.get('mode')))
                            if capability == 'sss':
                                gpu['notes'].append('Babylon sss ' + str(diagnostics.get('sss')))
                            gpu['maps'] = diagnostics.get('maps')
                except Exception as error:
                    gpu['verdict'] = VERDICT_FAIL
                    gpu['notes'].append('渲染异常：' + repr(error))
                entry['layers']['gpu'] = gpu
                entry['evidence'].append(
                    f'material-baseline.json#capabilities.{capability}.layers.gpu')
        except Exception as error:
            entry['layers']['contract'] = dict(verdict=VERDICT_FAIL,
                                               notes=[repr(error)])
            entry['notes'].append(traceback.format_exc(limit=3))
        entry['ms'] = round((time.perf_counter() - started) * 1000, 2)
        verdicts = [layer.get('verdict') for layer in entry['layers'].values()]
        if VERDICT_FAIL in verdicts:
            entry['verdict'] = VERDICT_FAIL
        elif verdicts and all(verdict == VERDICT_PASS for verdict in verdicts):
            entry['verdict'] = VERDICT_PASS
        else:
            entry['verdict'] = VERDICT_UNVERIFIED
        results.append(entry)
        persist(results)

    _apply_pair_checks(results)
    return results


def _apply_pair_checks(results):
    """Pair probes: the same texture under two settings must render differently.

    A pair that renders *identically* means the setting never reached the GPU
    -- exactly the kind of silent regression a per-item verdict has to catch.
    """
    by_id = {entry['id']: entry for entry in results}
    pairs = [('normal', 'normal_convention', '法线约定'),
             ('alpha_cutoff', 'alpha_cutoff_high', '裁切阈值')]
    for first, second, label in pairs:
        left, right = by_id.get(first), by_id.get(second)
        if not left or not right:
            continue
        layer = dict(verdict=VERDICT_UNVERIFIED, notes=[])
        left_fp = left.get('layers', {}).get('gpu', {}).get('analysis', {}).get('fingerprint')
        right_fp = right.get('layers', {}).get('gpu', {}).get('analysis', {}).get('fingerprint')
        if left_fp and right_fp:
            if left_fp == right_fp:
                layer['verdict'] = VERDICT_FAIL
                layer['notes'].append(
                    f'{label}：两种设置渲染出完全相同的像素指纹 {left_fp} -> 设置没有生效')
            else:
                layer['verdict'] = VERDICT_PASS
                layer['notes'].append(
                    f'{label}：两种设置的像素指纹不同（{left_fp[:12]} / {right_fp[:12]}）-> 设置生效')
        else:
            layer['notes'].append(f'{label}：无实测（缺 GPU 像素指纹）')
        for entry in (left, right):
            entry['layers']['pair'] = layer
            verdicts = [value.get('verdict') for value in entry['layers'].values()]
            if VERDICT_FAIL in verdicts:
                entry['verdict'] = VERDICT_FAIL
            elif verdicts and all(value == VERDICT_PASS for value in verdicts):
                entry['verdict'] = VERDICT_PASS
            else:
                entry['verdict'] = VERDICT_UNVERIFIED
            entry['evidence'].append(
                f'material-baseline.json#capabilities.{entry["id"]}.layers.pair')


# ── 真实记录批次 ────────────────────────────────────────────────────────

def run_record_pass(project, limit, offset, work_root, renderer, timeout_ms, persist):
    """Render a sample of the project's real materials at full size."""
    from prism.material_textures import prepare_render, render_signature

    records = []
    for index, record in enumerate(project.records()):
        if index < offset:
            continue
        if limit and len(records) >= limit:
            break
        records.append(record)
    rows = []
    unique_attachments = {}
    decoded_bytes = 0
    decoded_slots = 0
    for record in records:
        attachments = {}
        read_bytes = 0
        source_attachments = {}
        row = dict(id=record['id'], name=record.get('name'),
                   slots=sorted(record.get('textures', {})),
                   channels={slot: binding.get('channel')
                             for slot, binding in record.get('textures', {}).items()},
                   source_attachments=source_attachments,
                   properties=dict(record.get('preview', {}).get('properties', {})))
        prepare_started = time.perf_counter()
        try:
            for slot, binding in record.get('textures', {}).items():
                attachment_id = binding.get('attachmentId')
                if attachment_id not in attachments:
                    entry = project.attachment(attachment_id)
                    if entry is None:
                        raise ValueError('原图缺失：' + str(attachment_id))
                    attachments[attachment_id] = entry
                    read_bytes += len(entry[2] or b'')
                    unique_attachments[attachment_id] = len(entry[2] or b'')
                    source_attachments[attachment_id] = len(entry[2] or b'')
                blob = attachments[attachment_id][2]
                if hashlib.sha256(blob).hexdigest() != binding['sha256']:
                    raise ValueError('原图校验失败：' + binding.get('name', slot))
            render_dir = Path(work_root) / record['id']
            payload = prepare_render(record, attachments, render_dir)
            prepare_ms = (time.perf_counter() - prepare_started) * 1000
            decoded_bytes += read_bytes
            decoded_slots += len(record.get('textures', {}))
            row['prepare_ms'] = round(prepare_ms, 2)
            row['read_bytes'] = read_bytes
            row['blendMode'] = payload['properties'].get('blendMode')
            row['normalConvention'] = payload['properties'].get('normalConvention')
            row['maps'] = sorted(payload['maps'])
            row['signature'] = render_signature(record)
            if renderer is not None:
                render_started = time.perf_counter()
                result = renderer.render(payload, 'record-' + record['id'],
                                         timeout_ms=timeout_ms)
                row['render_ms'] = round((time.perf_counter() - render_started) * 1000, 2)
                if 'error' in result:
                    row['error'] = str(result['error'])
                else:
                    analysis = analyze_png(result['blob'])
                    row['analysis'] = analysis
                    row['render_signature_sha256'] = analysis['pixel_sha256']
            shutil.rmtree(render_dir, ignore_errors=True)
        except Exception as error:
            row['error'] = repr(error)
        rows.append(row)
        persist(rows)
    return rows, dict(records=len(rows),
                      unique_source_images=len(unique_attachments),
                      decoded_bytes=decoded_bytes,
                      decoded_slots=decoded_slots,
                      unique_source_bytes=sum(unique_attachments.values()))


# ── 报告 ────────────────────────────────────────────────────────────────

def render_markdown(report):
    lines = []
    lines.append('# 材质功能逐项不退化基线')
    lines.append('')
    lines.append(f"- 生成时间：{report['generated_at']}")
    lines.append(f"- 状态：**{report['status']}**")
    lines.append(f"- 工程：`{report['project']['path']}`")
    lines.append(f"- 工具：`{report['tool']}`")
    lines.append(f"- 结论口径：{report['verdict_legend']}")
    lines.append('')

    lines.append('## 规模口径（记录数 / 唯一源图数 / 实际解码字节数 分项独立列出）')
    lines.append('')
    lines.append('| 项 | 值 | 说明 |')
    lines.append('| --- | --- | --- |')
    for row in report['scale']['rows']:
        lines.append(f"| {row['name']} | {row['value']} | {row['note']} |")
    lines.append('')

    lines.append('## 状态契约（离开材质：丢临时调整 / 留绑定通道标签备注原图）')
    lines.append('')
    regression = report.get('regression')
    if not regression:
        lines.append('无实测（本轮没有跑 tests/test_material_baseline.py）')
        lines.append('')
    else:
        contract = [row for row in regression.get('rows', [])
                    if row['name'] == CONTRACT_TEST]
        contract = contract[0] if contract else None
        if contract is None:
            verdict = '未验收'
        elif contract['outcome'] == 'passed':
            verdict = '通过'
        elif contract['outcome'] == 'skipped':
            verdict = '未验收'
        else:
            verdict = '失败'
        lines.append(f"- **状态契约（两半一起断言）：{verdict}**")
        lines.append(f"- 测试：`tests/test_material_baseline.py::{CONTRACT_TEST}`")
        lines.append(f"- 断言的两半：")
        lines.append('  - 丢弃：`preview.properties` 的 `slots` / `flipX` / `flipY` / `alphaCutoff`，'
                     '并置 `preview.discardedAdjustments = True`')
        lines.append('  - 保留：`textures[slot]` 绑定、`textures[slot]["channel"]` 通道、'
                     '`tags`、`notes`、`files`（按 `sha256` 逐份比对 attachmentId / name / 字节哈希）')
        lines.append(f"- pytest 退出码：{regression.get('exit_code')}，"
                     f"通过 {regression.get('passed')} / 失败 {regression.get('failed')} / "
                     f"xfail {regression.get('xfailed')} / 跳过 {regression.get('skipped')}")
        xfails = [row for row in regression.get('rows', [])
                  if row.get('outcome') == 'xfailed']
        if xfails:
            lines.append('- **已用 `pytest.mark.xfail(strict=True)` 钉住的已知缺陷**'
                         '（strict：修好后会 XPASS 并失败，提醒回来摘掉 xfail）：')
            for row in xfails:
                lines.append(f"  - `{row['name']}`")
                if row.get('message'):
                    lines.append(f"    - 原因：{row['message'][:300]}")
        lines.append(f"- junit：`{regression.get('junit')}`")
        lines.append(f"- 命令：`{regression.get('command')}`")
        lines.append('')

    lines.append('## 10 项逐项结论表')
    lines.append('')
    lines.append('每一项一条独立结论。第 10 项「自定义通道」按通道拆成 5 条，'
                 '每条单独一个结论。')
    lines.append('')
    lines.append('- 逐项结论词只取 **通过 / 失败 / 未验收**；'
                 '「部分验收」只用于整体状态词，不用于逐项结论。')
    lines.append('- 每一项的表里都分列契约层 / GPU 层 / 配对层，'
                 '哪一层没有结论一眼可见。')
    lines.append('- **只有 3 项有配对对照**（法线约定、裁切阈值 0.2、裁切阈值 0.8）：'
                 '配对层要拿「同一张图、两个相反设置」比像素指纹，'
                 '只有存在第二个可比设置的能力才配得上；其余项没有第二个设置，'
                 '表里的「—」表示**该层不适用**，不是漏测。')
    lines.append('')
    lines.append('| 项 | 能力 | 结论 | 契约层 | GPU 层 | 配对层 | 证据位置 |')
    lines.append('| --- | --- | --- | --- | --- | --- | --- |')
    for entry in report['capabilities']:
        if entry.get('role') != 'primary':
            continue
        contract = entry['layers'].get('contract', {}).get('verdict', '—')
        gpu = entry['layers'].get('gpu', {}).get('verdict', '无实测')
        pair = entry['layers'].get('pair', {}).get('verdict', '—')
        evidence = '<br>'.join('`' + item + '`' for item in entry['evidence'])
        lines.append(f"| {entry.get('group_number', '-')} | {entry['capability']} | "
                     f"**{entry['verdict']}** | {contract} | {gpu} | {pair} | {evidence} |")
    lines.append('')

    pairs = [entry for entry in report['capabilities'] if entry.get('role') == 'pair']
    if pairs:
        lines.append('### 配对探针（与主项同图、不同设置）')
        lines.append('')
        lines.append('| 配对探针 | 结论 | 证据位置 |')
        lines.append('| --- | --- | --- |')
        for entry in pairs:
            evidence = '<br>'.join('`' + item + '`' for item in entry['evidence'])
            lines.append(f"| {entry['capability']} | **{entry['verdict']}** | {evidence} |")
        lines.append('')

    lines.append('## 逐项细节')
    lines.append('')
    for entry in report['capabilities']:
        lines.append(f"### {entry['capability']}（{entry['id']}）— {entry['verdict']}")
        lines.append('')
        for layer_name, layer in entry['layers'].items():
            lines.append(f"- **{layer_name}**：{layer.get('verdict')}")
            for note in layer.get('notes', []):
                lines.append(f"  - {note}")
        if entry.get('ms') is not None:
            lines.append(f"- 耗时：{entry['ms']} ms")
        lines.append('')

    lines.append('## 真实记录批次')
    lines.append('')
    batch = report.get('record_batch', {})
    if batch.get('rows'):
        lines.append(f"- 记录数：{batch.get('records')}")
        lines.append(f"- 唯一源图数：{batch.get('unique_source_images')}")
        lines.append(f"- 唯一源图字节数：{batch.get('unique_source_bytes')}")
        lines.append(f"- 本轮实际读出并解码的字节数：{batch.get('decoded_bytes')}"
                     f"（同一张原图被多条记录重复引用时会被重复读取）")
        lines.append(f"- 本轮实际解码的槽位贴图数：{batch.get('decoded_slots')}")
        lines.append('')
        lines.append('| 指标 | count | median_ms | p95_ms | max_ms |')
        lines.append('| --- | --- | --- | --- | --- |')
        for name, value in report.get('timing', {}).items():
            lines.append(f"| {name} | {value.get('count', '无实测')} | "
                         f"{value.get('median_ms', '无实测')} | {value.get('p95_ms', '无实测')} | "
                         f"{value.get('max_ms', '无实测')} |")
        lines.append('')
        degraded = [row for row in batch.get('rows', [])
                    if row.get('analysis', {}).get('degraded')]
        errors = [row for row in batch.get('rows', []) if row.get('error')]
        lines.append(f"- 渲染失败条数：{len(errors)}")
        lines.append(f"- 渲染退化（纯色块 / 颜色数不足）条数：{len(degraded)}")
        lines.append(f"- 材质内容组数（按 `render_signature` 去重）：{batch.get('content_groups')}")
        lines.append(f"- **同一内容组内像素指纹不一致的组数：{batch.get('group_fingerprint_conflicts')}**"
                     f"（应当是 0：同一份材质重复渲染必须给出同一张图）")
        lines.append(f"- 不同内容组的像素指纹：{batch.get('distinct_fingerprints')} / "
                     f"{batch.get('content_groups')}（应当相等：不同材质不能撞图）")
        if 'group_fingerprint_collisions' in batch:
            lines.append(f"- **组间撞图数（同一张图被两个以上内容组渲染出来）："
                         f"{batch.get('group_fingerprint_collisions')}**（应当是 0）")
        lines.append(f"- 说明：本工程的 5000 条材质记录是 **300 组真实材质**复制出来的，"
                     f"所以「记录数」远大于「不同渲染数」，这不是退化，"
                     f"**记录数不能当材质数用**。")
        if degraded:
            for row in degraded:
                lines.append(f"  - 退化：`{row['id']}` {row.get('name')}")
        if errors:
            for row in errors:
                lines.append(f"  - 失败：`{row['id']}` {row.get('error')}")
        lines.append('')
    else:
        lines.append('无实测')
        lines.append('')

    merge = report.get('merge')
    if merge:
        lines.append('## 分片合并（全量批次由并发分片合成）')
        lines.append('')
        lines.append(f"- 合并工具：`{merge.get('tool')}`，合并时间 {merge.get('merged_at')}")
        lines.append(f"- **分片并发数 = {merge.get('concurrency')}**"
                     f"（分片内的耗时读数带并发争用，**不是低负载读数**）")
        lines.append(f"- 覆盖：{merge.get('covered')} / {merge.get('record_total')} 条"
                     f"，缺失记录序号 {merge.get('missing_count')} 个"
                     f"（**缺失即部分验收，不能报全量**）")
        lines.append(f"- 去重丢弃的行：{merge.get('duplicate_rows')}"
                     f"（同一记录 id 在多片出现时保留先读到的那行）")
        lines.append(f"- 工程里不存在的 id：{len(merge.get('unknown_ids') or [])}")
        lines.append('')
        lines.append('**合并规则**（不是把各片数字相加）：')
        lines.append('')
        lines.append('1. **行是唯一事实来源**：各片 `record_batch.rows` 是唯一被信任的输入；')
        lines.append('   各片自带的汇总数字（records / unique_source_images / decoded_bytes /')
        lines.append('   content_groups / group_fingerprint_conflicts / distinct_fingerprints）')
        lines.append('   **一律不采用** —— 例如「组内不一致的组数」必须**跨片重算**，')
        lines.append('   同一个内容组可能横跨两片。')
        lines.append('2. **按记录 id 去重**，先读到者优先（输入顺序即优先级，结果确定）。')
        lines.append('3. **按工程 rowid 顺序重排**（SQLite `mode=ro` 打开，绝不写回），')
        lines.append('   使合并结果与串行跑出来的行序一致。')
        lines.append('4. **统计量全部重算**：内容组数 / 组内指纹不一致组数 / 组间撞图数 /')
        lines.append('   唯一源图数 / 唯一源图字节数 / 实际解码字节数 / 实际解码槽位数 /')
        lines.append('   渲染失败条数 / 渲染退化条数 / 计时统计。')
        lines.append('')
        lines.append('| 输入分片 | 行数 | 去重后新增 | 覆盖区间 |')
        lines.append('| --- | --- | --- | --- |')
        for item in merge.get('inputs', []):
            span = (' — ' if not item.get('range')
                    else f"{item['range'][0]} … {item['range'][1]}")
            lines.append(f"| `{item.get('path')}` | {item.get('rows')} | "
                         f"{item.get('unique')} | {span} |")
        lines.append('')
        lines.append('| 被忽略的分片自带汇总（仅留痕，不参与计算） | 值 |')
        lines.append('| --- | --- |')
        for item in merge.get('inputs', []):
            ignored = item.get('summary_ignored') or {}
            if not ignored:
                continue
            lines.append(f"| `{item.get('path')}` | "
                         + '，'.join(f'{key}={value}' for key, value in ignored.items())
                         + ' |')
        lines.append('')

    write_stats = report.get('write_stats')
    if write_stats:
        lines.append('## 落盘可靠性（Windows 文件锁重试）')
        lines.append('')
        lines.append(f"- 落盘次数：{write_stats.get('writes')}")
        lines.append(f"- **靠重试才写成功：{write_stats.get('retry_successes')} 次**"
                     f"（累计重试 {write_stats.get('retries')} 次；"
                     f"上限 {WRITE_RETRY_LIMIT} 次，退避 50/100/200/400/800 ms）")
        lines.append(f"- **最终放弃落盘：{write_stats.get('failures')} 次**"
                     f"（放弃不会让整片崩掉：下一次落盘会把更新的行重新写上）")
        if write_stats.get('last_error'):
            lines.append(f"- 最后一次错误：`{write_stats.get('last_error')}`")
        by_shard = write_stats.get('by_shard')
        if by_shard:
            lines.append('- 各分片的落盘统计（`None` = 该分片跑在抗锁修复**之前**，没有计数器）：')
            for path, stats in by_shard.items():
                name = Path(path).parent.name if path else '?'
                if stats is None:
                    lines.append(f'  - `{name}`：无计数器（修复前跑的）')
                else:
                    lines.append(f"  - `{name}`：写入 {stats.get('writes')}，"
                                 f"靠重试成功 {stats.get('retry_successes')}，"
                                 f"累计重试 {stats.get('retries')}，"
                                 f"最终失败 {stats.get('failures')}")
        lines.append('- **计数口径**：`writes` 不含**最后那一遍**写入自身；'
                     '合并与 `--only-tests` 各自再写 2 次，不计入分片侧统计。')
        lines.append('')

    lines.append('## 环境与负载')
    lines.append('')
    for line in _environment_lines(report):
        lines.append(f"- {line}")
    lines.append('')
    lines.append('## 未做的检查')
    lines.append('')
    for line in report.get('not_checked', []):
        lines.append(f"- {line}")
    lines.append('')
    return '\n'.join(lines) + '\n'


def _format_bytes(value):
    if not isinstance(value, int) or value <= 0:
        return '无实测'
    return f'{value} 字节（{value / (1024 * 1024):.1f} MB）'


def _environment_lines(report):
    environment = report.get('environment') or {}
    lines = [
        f"机器：{environment.get('logical_cpus', '无实测')} 逻辑核，"
        f"内存 {environment.get('memory_gb', '无实测')} GB",
        f"OS：{environment.get('os', '无实测')}",
        f"解释器：Python {environment.get('python', '无实测')}，"
        f"PyQt6 {environment.get('pyqt', '无实测')}，Qt {environment.get('qt', '无实测')}",
        f"渲染后端：{environment.get('renderer', '无实测')}",
        f"是否真实素材：是（工程 {environment.get('project', '无实测')}）"
        if environment.get('real_assets') else '是否真实素材：否',
        f"测量时机器负载：{environment.get('load', '无实测')}",
        f"负载快照：{environment.get('load_snapshot', '无实测')}",
        f"渲染进程常驻内存：{_format_bytes(environment.get('resident_bytes'))}",
    ]
    return lines


def main():
    # The Chinese report text is UTF-8; a GBK console would mangle it.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--project', type=Path,
                        default=Path(r'E:\Prism-stress-100k\Prism-100k-v1.prism'))
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=24,
                        help='真实记录批次条数（0 = 全部）')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--scan-cache', type=Path, default=None,
                        help='material_records 扫描结果缓存文件（分片并发时省掉重复扫描）')
    parser.add_argument('--timeout-ms', type=int, default=60000)
    parser.add_argument('--no-gpu', action='store_true',
                        help='只跑契约层，不开 GPU 预览')
    parser.add_argument('--records-only', action='store_true')
    parser.add_argument('--capabilities-only', action='store_true')
    parser.add_argument('--screens', type=Path, default=None,
                        help='渲染截图落盘目录（默认临时目录）')
    parser.add_argument('--no-tests', action='store_true',
                        help='不跑 tests/test_material_baseline.py')
    parser.add_argument('--only-tests', action='store_true',
                        help='只跑测试并把结果并回已有的 material-baseline.json')
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / 'material-baseline.json'
    md_path = out_dir / 'material-baseline.md'

    if args.only_tests:
        # 只想把测试结果并回已有报告，不重跑渲染。
        if not json_path.exists():
            raise SystemExit(f'no report to merge into: {json_path}')
        report = json.loads(json_path.read_text(encoding='utf-8'))
        report['regression'] = run_contract_tests(out_dir)
        report['generated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
        environment = report.setdefault('environment', {})
        environment['load_snapshot'] = _load_snapshot()
        _refresh_derived(report)
        write_json(json_path, report)
        _atomic_write(md_path, render_markdown(report))
        print(json.dumps({'regression': {key: value for key, value
                                         in report['regression'].items() if key != 'rows'},
                          'json': str(json_path)}, ensure_ascii=False, indent=2))
        return 0

    work_root = Path(tempfile.mkdtemp(prefix='prism-material-baseline-'))
    screens = Path(args.screens) if args.screens else Path(work_root) / 'screens'
    screens.mkdir(parents=True, exist_ok=True)

    from prism.material_parameters import RANGES
    from prism.material_textures import CHANNELS, SCALARS, SLOTS

    report = {
        'tool': 'tools/material/baseline.py',
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'project': {'path': str(args.project), 'readonly': True},
        'status': STATUS_RUNNING,
        'verdict_legend': '通过 / 失败 / 未验收（结论只取这三个词）',
        'capabilities': [],
        'record_batch': {},
        'timing': {},
        'scale': {'rows': []},
        'environment': {},
        'not_checked': [
            '没有做长时间会话的内存曲线（那是 E-4 的环境矩阵范围）。',
            '没有验证 10 万条工程在材质库打开路径上的端到端耗时（那是 E-1 的范围）。',
            '没有验证材质库面板（卡片 / 详情检视器 / 槽位控件）的像素级表现，'
            '只验证了渲染载荷与预览控件这两层。',
        ],
    }

    def persist(capabilities=None, batch=None):
        if capabilities is not None:
            report['capabilities'] = capabilities
        if batch is not None:
            report['record_batch'] = batch
        # 计时永远由当前已有的行算出来，报告被中断也不会留下上一轮的数字。
        rows = report.get('record_batch', {}).get('rows', [])
        report['timing'] = {
            'prepare_ms': stats([row['prepare_ms'] for row in rows if 'prepare_ms' in row]),
            'render_ms': stats([row['render_ms'] for row in rows if 'render_ms' in row]),
        }
        _refresh_derived(report)
        report['scale']['rows'] = _scale_rows(report)
        # 落盘统计用同一个 dict 对象：JSON 落盘那一刻的内容是当时的计数，
        # markdown 在 JSON 之后写，能把 JSON 那次写入的重试也算进去。
        report['write_stats'] = _write_stats
        write_json(json_path, report)
        _atomic_write(md_path, render_markdown(report))

    # ── 环境 ──
    import platform
    from PyQt6 import QtCore
    total_memory = '无实测'
    try:
        import ctypes
        class _MemoryStatus(ctypes.Structure):
            _fields_ = [('dwLength', ctypes.c_ulong),
                        ('dwMemoryLoad', ctypes.c_ulong),
                        ('ullTotalPhys', ctypes.c_ulonglong),
                        ('ullAvailPhys', ctypes.c_ulonglong),
                        ('ullTotalPageFile', ctypes.c_ulonglong),
                        ('ullAvailPageFile', ctypes.c_ulonglong),
                        ('ullTotalVirtual', ctypes.c_ulonglong),
                        ('ullAvailVirtual', ctypes.c_ulonglong),
                        ('ullAvailExtendedVirtual', ctypes.c_ulonglong)]
        status = _MemoryStatus()
        status.dwLength = ctypes.sizeof(_MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        total_memory = round(status.ullTotalPhys / (1024 ** 3), 1)
    except Exception:
        pass
    resident = '无实测'
    try:
        from prism.performance_trace import process_memory_bytes
        resident = process_memory_bytes()
    except Exception:
        pass
    report['environment'] = {
        'logical_cpus': os.cpu_count(),
        'memory_gb': total_memory,
        'os': f'{platform.system()} {platform.release()} ({platform.version()})',
        'python': sys.version.split()[0],
        'pyqt': QtCore.PYQT_VERSION_STR,
        'qt': QtCore.QT_VERSION_STR,
        'renderer': '未启动',
        'real_assets': True,
        'project': str(args.project),
        'load': '记录本轮开始时的 python 进程数与 CPU 占用（其它组并发在跑）',
        'load_snapshot': _load_snapshot(),
        'resident_bytes': resident,
    }

    renderer = None
    renderer_reason = ''
    project = None
    try:
        if not args.capabilities_only:
            project = MaterialProject(args.project)
            from prism.material_textures import render_signature
            # The full scan of material_records is random I/O over a 55 GB
            # SQLite file; concurrent shards would each pay for it.  The scan
            # result is cached next to the report and keyed by the project's
            # identity, so a shard can reuse it.
            total, groups = _project_scale(args.project, args.scan_cache)
            report['record_total'] = total
            report['material_groups'] = groups
            manifest = _fixture_manifest()
            # The canvas-side count comes from the manifest, not from
            # "SELECT count(*) FROM items WHERE type='pixmap'": items.type has
            # no index, so that query is a full scan of 100k rows with payload
            # blobs on a 55 GB file -- minutes of random I/O per shard.
            report['project_items'] = manifest.get('images', '无实测')
            report['project_unique'] = manifest.get('original_images', '无实测')
            report['project_unique_bytes'] = manifest.get('original_image_bytes', '无实测')
            report['decoded_bytes'] = 0
            report['decoded_slots'] = 0
            report['unique_source_bytes'] = 0
            report['unique_source_images'] = 0
        # The GPU renderer is needed by *both* passes: the capability pass and
        # the record batch (the record batch is the one that renders every
        # material through the real Babylon.js path).  --records-only means
        # "skip the capability pass", it must not mean "skip the GPU".
        need_capabilities = not args.records_only
        need_records = not args.capabilities_only
        if not args.no_gpu and (need_capabilities or need_records):
            try:
                renderer = BabylonRenderer()
                report['environment']['renderer'] = 'Babylon.js (WebGL, 真实 GPU 路径)'
            except Exception as error:
                renderer_reason = repr(error)
                report['environment']['renderer'] = 'GPU 不可用：' + renderer_reason
                report['not_checked'].append(
                    'GPU 像素层无实测：预览控件无法启动（' + renderer_reason + '）')
        elif args.no_gpu:
            renderer_reason = '--no-gpu'
            report['environment']['renderer'] = '按 --no-gpu 跳过'
            report['not_checked'].append('GPU 像素层无实测：--no-gpu')
        persist()

        if not args.records_only:
            capabilities = run_capability_pass(
                work_root / 'probes', work_root / 'probe-render', renderer,
                args.timeout_ms, lambda items: persist(capabilities=items),
                renderer_reason)
            report['capabilities'] = capabilities

        if not args.capabilities_only and project is not None:
            rows, summary = run_record_pass(
                project, args.limit, args.offset, work_root / 'records', renderer,
                args.timeout_ms, lambda items: persist(batch=dict(rows=items)))
            report['record_batch'] = dict(rows=rows, **summary)
            report['decoded_bytes'] = summary['decoded_bytes']
            report['decoded_slots'] = summary['decoded_slots']
            report['unique_source_images'] = summary['unique_source_images']
            report['unique_source_bytes'] = summary['unique_source_bytes']
        persist()

        verdicts = [entry['verdict'] for entry in report['capabilities']]
        if report['capabilities'] and VERDICT_FAIL in verdicts:
            report['status'] = STATUS_PARTIAL
        elif report['capabilities'] and all(v == VERDICT_PASS for v in verdicts):
            report['status'] = STATUS_DONE
        else:
            report['status'] = STATUS_LANDED
        report['not_checked'].extend([
            '本轮只覆盖了样本批次，没有对全部 5000 条材质记录逐条做 GPU 渲染。',
            '没有做长时间会话的内存曲线（那是 E-4 的环境矩阵范围）。',
            '没有验证 10 万条工程在材质库打开路径上的端到端耗时（那是 E-1 的范围）。',
        ])
        if not args.no_tests:
            report['regression'] = run_contract_tests(out_dir)
        persist()
    finally:
        if project is not None:
            project.close()
        if renderer is not None:
            renderer.close()
        shutil.rmtree(work_root, ignore_errors=True)

    print(json.dumps({
        'status': report['status'],
        'capabilities': {entry['id']: entry['verdict'] for entry in report['capabilities']},
        'json': str(json_path),
        'markdown': str(md_path),
    }, ensure_ascii=False, indent=2))
    return 0


def run_contract_tests(out_dir):
    """Run the material baseline test file and read the verdicts back.

    The report then carries its own regression evidence instead of pointing
    at a console scrollback that nobody can check later.
    """
    import subprocess
    import xml.etree.ElementTree as ET
    junit = Path(out_dir) / 'material-baseline-junit.xml'
    command = [sys.executable, '-m', 'pytest', 'tests/test_material_baseline.py',
               '-q', '--no-header', '-p', 'no:cacheprovider', '--tb=line',
               f'--junitxml={junit}']
    try:
        completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True,
                                   encoding='utf-8', errors='replace', timeout=3600)
        exit_code = completed.returncode
        tail = (completed.stdout or '')[-4000:]
    except Exception as error:
        return dict(command=' '.join(command), exit_code=None,
                    error=repr(error), rows=[], passed=0, failed=0, skipped=0,
                    junit=str(junit))
    rows = []
    if junit.exists():
        root = ET.parse(junit).getroot()
        for suite in root.findall('testsuite') or [root]:
            for case in suite.findall('testcase'):
                failure = case.find('failure')
                error = case.find('error')
                skipped = case.find('skipped')
                outcome = 'passed'
                if failure is not None:
                    outcome = 'failed'
                elif error is not None:
                    outcome = 'error'
                elif skipped is not None:
                    # pytest reports xfail as a skip; keep them apart.
                    outcome = ('xfailed' if skipped.get('type') == 'pytest.xfail'
                               else 'skipped')
                rows.append(dict(name=case.get('name', ''), outcome=outcome,
                                 time=float(case.get('time', 0) or 0),
                                 message=(failure.get('message') if failure is not None
                                          else error.get('message') if error is not None
                                          else skipped.get('message') if skipped is not None
                                          else '')))
    return dict(command=' '.join(command), exit_code=exit_code, tail=tail,
                junit=str(junit), rows=rows,
                passed=sum(1 for row in rows if row['outcome'] == 'passed'),
                failed=sum(1 for row in rows if row['outcome'] in ('failed', 'error')),
                skipped=sum(1 for row in rows if row['outcome'] == 'skipped'),
                xfailed=sum(1 for row in rows if row['outcome'] == 'xfailed'))


CONTRACT_TEST = ('test_leaving_material_discards_adjustments_but_keeps_'
                 'bindings_channels_tags_notes_and_originals')


def _derive_batch(report):
    """Derive the batch numbers and the scale figures from the rows.

    The rows are the ground truth: deriving from them means a run that was
    interrupted still reports exactly what it measured -- never a zero that
    looks like "the material records carry no originals".
    """
    batch = report.get('record_batch') or {}
    rows = batch.get('rows') or []
    unique = {}
    decoded_bytes = 0
    decoded_slots = 0
    groups = {}
    for row in rows:
        decoded_bytes += row.get('read_bytes', 0) or 0
        decoded_slots += len(row.get('source_attachments') or {}) or len(row.get('slots') or [])
        for attachment_id, size in (row.get('source_attachments') or {}).items():
            unique[attachment_id] = size
        fingerprint = (row.get('analysis') or {}).get('fingerprint')
        if fingerprint:
            groups.setdefault(row.get('signature'), set()).add(fingerprint)
    conflicts = [signature for signature, prints in groups.items() if len(prints) > 1]
    derived = dict(records=len(rows),
                   unique_source_images=len(unique),
                   decoded_bytes=decoded_bytes,
                   decoded_slots=decoded_slots,
                   unique_source_bytes=sum(unique.values()),
                   content_groups=len(groups),
                   group_fingerprint_conflicts=len(conflicts),
                   distinct_fingerprints=len({print for prints in groups.values()
                                              for print in prints}))
    # The rows win; the summary only fills in what the rows cannot say.
    for key, value in derived.items():
        if value or not batch.get(key):
            batch[key] = value
    report['record_batch'] = batch
    report['decoded_bytes'] = batch.get('decoded_bytes', 0)
    report['decoded_slots'] = batch.get('decoded_slots', 0)
    report['unique_source_images'] = batch.get('unique_source_images', 0)
    report['unique_source_bytes'] = batch.get('unique_source_bytes', 0)
    return batch


def _refresh_derived(report):
    """Recompute the fields that depend on what was actually measured.

    Kept separate so a report can be re-derived (for example when the test
    results are merged into an existing run) without repeating the run.
    """
    report['verdict_legend'] = '通过 / 失败 / 未验收（逐项结论只取这三个词）'
    _derive_batch(report)
    rows = report.get('record_batch', {}).get('rows', [])
    covered = len(rows)
    total = report.get('record_total')
    not_checked = [
        '没有做长时间会话的内存曲线（那是 E-4 的环境矩阵范围）。',
        '没有验证 10 万条工程在材质库打开路径上的端到端耗时（那是 E-1 的范围）。',
        '没有验证材质库面板（卡片 / 详情检视器 / 槽位控件）的像素级表现，'
        '只验证了渲染载荷与预览控件这两层。',
    ]
    if isinstance(total, int) and covered < total:
        not_checked.insert(0, f'本轮只渲染了 {covered} / {total} 条材质记录，'
                              f'其余记录没有逐条做 GPU 渲染。')
    if not report.get('regression'):
        not_checked.append('状态契约两半的测试本轮没有跑（无实测）。')
    report['not_checked'] = not_checked

    verdicts = [entry['verdict'] for entry in report.get('capabilities', [])]
    if not verdicts:
        report['status'] = STATUS_RUNNING
    elif VERDICT_FAIL in verdicts:
        report['status'] = STATUS_PARTIAL
    elif not all(verdict == VERDICT_PASS for verdict in verdicts):
        report['status'] = STATUS_LANDED
    elif isinstance(total, int) and covered < total:
        report['status'] = STATUS_PARTIAL
    else:
        report['status'] = STATUS_DONE
    return report


def _scale_rows(report):
    """The scale table: 记录数 / 唯一源图数 / 实际解码字节数, listed apart.

    提取成函数是为了让分片合并（tools/material/merge_batches.py）用
    **完全同一套口径**重建这张表，而不是各写一份。
    """
    return [
        {'name': '材质记录数', 'value': report.get('record_total', '无实测'),
         'note': 'material_records 表行数（目录规模）'},
        {'name': '材质唯一内容组数', 'value': report.get('material_groups', '无实测'),
         'note': '不重复的材质内容组（按 render_signature 去重）；记录数被这一项放大'},
        {'name': '材质唯一源图数', 'value': report.get('unique_source_images', '无实测'),
         'note': '材质记录实际引用的不重复原图数（解码负载）'},
        {'name': '材质唯一源图字节数', 'value': report.get('unique_source_bytes', '无实测'),
         'note': '不重复原图的字节总数'},
        {'name': '本轮实际读出并解码的字节数', 'value': report.get('decoded_bytes', '无实测'),
         'note': '本轮真正从工程文件读出并解码的原图字节数（含同一张图被多条记录重复读取）'},
        {'name': '本轮实际解码的槽位贴图数', 'value': report.get('decoded_slots', '无实测'),
         'note': 'prepare_render 真正解码的槽位贴图张数'},
        {'name': '工程画布侧记录数', 'value': report.get('project_items', '无实测'),
         'note': 'items 表的 pixmap 数，材质库之外的规模（来源 artifacts/scale-audit/fixture.json 的 images）'},
        {'name': '工程画布侧唯一原图数', 'value': report.get('project_unique', '无实测'),
         'note': '画布侧不重复原图数，与材质库不是同一批（来源 artifacts/scale-audit/fixture.json）'},
        {'name': '工程画布侧唯一原图字节数', 'value': report.get('project_unique_bytes', '无实测'),
         'note': '同上（来源 artifacts/scale-audit/fixture.json）'},
    ]


def _project_scale(project_path, cache_path=None):
    """(记录数, 唯一内容组数) —— 扫一遍 material_records。

    扫描是 55 GB SQLite 上的随机 I/O，很贵；并发分片各扫一遍是纯浪费。
    扫描结果按工程身份（路径 + 大小 + mtime）缓存到 ``cache_path``，
    工程没变就直接复用。
    """
    from prism.material_textures import render_signature

    identity = None
    try:
        stat = Path(project_path).stat()
        identity = dict(path=str(Path(project_path).resolve()),
                        size=stat.st_size, mtime=int(stat.st_mtime))
    except Exception:
        identity = dict(path=str(project_path))
    if cache_path:
        cache = Path(cache_path)
        try:
            payload = json.loads(cache.read_text(encoding='utf-8'))
            if payload.get('identity') == identity:
                return int(payload['record_total']), int(payload['material_groups'])
        except Exception:
            pass
    connection = sqlite3.connect(f'file:{project_path}?mode=ro', uri=True)
    try:
        signatures = set()
        total = 0
        for (payload,) in connection.execute(
                'SELECT payload FROM material_records ORDER BY rowid'):
            total += 1
            signatures.add(render_signature(json.loads(payload)))
    finally:
        connection.close()
    if cache_path:
        try:
            write_json(cache_path, dict(identity=identity, record_total=total,
                                       material_groups=len(signatures),
                                       scanned_at=time.strftime('%Y-%m-%dT%H:%M:%S%z')))
        except Exception:
            pass
    return total, len(signatures)


def _fixture_manifest():
    """The scale-audit manifest: the authoritative canvas-side unique counts.

    Recomputing "unique real images" from the 53 GB sqlar table would cost a
    full scan of a busy disk; the manifest already recorded it when the
    fixture was built.
    """
    path = ROOT / 'artifacts' / 'scale-audit' / 'fixture.json'
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def _load_snapshot():
    """What else was running while this measurement was taken."""
    try:
        import subprocess
        command = ('$p=(Get-Process python -ErrorAction SilentlyContinue | Measure-Object).Count; '
                   '$c=(Get-CimInstance Win32_Processor | '
                   'Measure-Object -Property LoadPercentage -Average).Average; '
                   'Write-Output "python 进程数=$p；CPU 占用=$c%"')
        output = subprocess.run(
            ['powershell', '-NoProfile', '-Command', command],
            capture_output=True, text=True, timeout=40)
        return (output.stdout or '').strip() or '负载快照失败'
    except Exception as error:
        return '负载快照失败：' + repr(error)


if __name__ == '__main__':
    sys.exit(main())
