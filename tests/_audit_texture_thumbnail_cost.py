"""取证：64x64 的贴图槽缩略图，为���么一张要 200~500 ms？

材质面板在选中一个材质时，会为**每个**有绑定的贴图槽各排一个任务
（``model_material_panel.py:876``），每个任务用 PIL 把**原尺寸**贴图解码成
RGBA 再缩到 64x64。真实工程里是 8K PNG：8192x8192 的一次 RGBA 转换就是
268 MB 的分配。

这个探针在真实 8K 贴图上量四条路线：

  1. 现在的做法：``convert('RGBA')`` -> 取通道 -> ``thumbnail`` -> PNG 编码
  2. 先缩后转：``thumbnail`` -> ``convert('RGBA')`` -> 取通道 -> PNG 编码
  3. Qt 缩放解码：``prism.image_decode.decode(max_side=64)``（复用项目现成管线）
  4. Qt 全尺寸解码：``decode()`` 不带 max_side，再自己缩

用��：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u tests\\_audit_texture_thumbnail_cost.py
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ARTIFACTS = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))) / 'artifacts' / 'material-tests'


def find_textures(limit=6):
    """The real textures this project works with (8K PNGs and friends)."""
    if not ARTIFACTS.exists():
        return []
    found = []
    for pattern in ('*.PNG', '*.png', '*.jpg', '*.jpeg', '*.tga'):
        found.extend(ARTIFACTS.rglob(pattern))
    found = [p for p in found if p.stat().st_size > 32 * 1024]
    found.sort(key=lambda p: -p.stat().st_size)
    return found[:limit]


def timed(function, repeat=3):
    best = None
    for _ in range(repeat):
        started = time.perf_counter()
        function()
        elapsed = (time.perf_counter() - started) * 1000
        best = elapsed if best is None else min(best, elapsed)
    return best


def current_pil(path, channels='RGB'):
    """Exactly what ``ProjectMaterialService.texture_thumbnail`` does today."""
    from PIL import Image
    with Image.open(path) as image:
        image = image.convert('RGBA')
        if channels in ('R', 'G', 'B', 'A'):
            image = image.getchannel(channels).convert('RGB')
        image.thumbnail((64, 64))
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return stream.getvalue()


def thumbnail_first_pil(path, channels='RGB'):
    """Downscale first, then convert: the big RGBA buffer is never built."""
    from PIL import Image
    with Image.open(path) as image:
        image.draft('RGB', (64, 64))
        image.thumbnail((64, 64))
        image = image.convert('RGBA')
        if channels in ('R', 'G', 'B', 'A'):
            image = image.getchannel(channels).convert('RGB')
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return stream.getvalue()


def draft_only_pil(path, channels='RGB'):
    """``draft`` plus the original order of the remaining steps."""
    from PIL import Image
    with Image.open(path) as image:
        image.draft('RGB', (64, 64))
        image = image.convert('RGBA')
        if channels in ('R', 'G', 'B', 'A'):
            image = image.getchannel(channels).convert('RGB')
        image.thumbnail((64, 64))
        stream = io.BytesIO()
        image.save(stream, format='PNG')
        return stream.getvalue()


def qt_scaled(path):
    """The project's own decode pipeline, asked for the size we actually need."""
    from prism.image_decode import decode
    from prism.thumbnail_cache import encode_preview
    result = decode(path=str(path), max_side=64)
    if not result:
        return None
    image = result[0]
    return encode_preview(image)


def qt_full(path):
    """The same pipeline without the size hint, to show what the hint buys."""
    from prism.image_decode import decode
    from prism.thumbnail_cache import encode_preview
    result = decode(path=str(path))
    if not result:
        return None
    image = result[0]
    from prism.thumbnail_cache import make_preview
    return encode_preview(make_preview(image, 64))


def main():
    textures = find_textures()
    if not textures:
        print(f'没有找到贴图样本：{ARTIFACTS}')
        return
    print(f'样本贴图 {len(textures)} 张\n', flush=True)
    for path in textures:
        size_mb = path.stat().st_size / (1024 * 1024)
        print(f'── {path.name}  ({size_mb:.1f} MB)  {path}', flush=True)
        rows = [
            ('现在的做法 (PIL 全尺寸 RGBA)', current_pil),
            ('PIL draft + 全尺寸 RGBA', draft_only_pil),
            ('PIL draft + 先缩后转', thumbnail_first_pil),
            ('Qt 缩放解码 (max_side=64)', qt_scaled),
            ('Qt 全尺寸解码 + 自己缩', qt_full),
        ]
        baseline = None
        for label, function in rows:
            try:
                started = time.perf_counter()
                blob = function(path)
                elapsed = (time.perf_counter() - started) * 1000
            except Exception as error:      # noqa: BLE001 - a probe reports, never raises
                print(f'    {label:<32} 失败：{type(error).__name__}: {error}')
                continue
            size = len(blob) if blob else 0
            if baseline is None:
                baseline = elapsed
            speed = f'{baseline / elapsed:.1f}x' if elapsed else '-'
            print(f'    {label:<32} {elapsed:9.1f} ms   {size:6d} B   {speed}')
        print(flush=True)


if __name__ == '__main__':
    main()
