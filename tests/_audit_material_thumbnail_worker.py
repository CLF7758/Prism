"""Measure the worker side of a material row preview, stage by stage.

The desktop process must not load the public VTK runtime next to F3D's own
build, so the row preview is measured in its own process.  The specs come
from ``tests/_audit_material_thumbnails.py``.

Every row preview of the material list is a native render: a sphere is built,
an off-screen renderer is created, the material's textures are decoded, the
sphere is rendered and the image is written.  This probe repeats exactly that
code path and times the stages, so the cost of each step is known instead of
guessed.

    $env:AUDIT_VARIANT='base|reuse|downscale|reuse-downscale'
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

SPECS = Path(os.environ.get('AUDIT_SPEC_JSON', ''))


def _sphere():
    import vtk
    from vtkmodules.util.numpy_support import vtk_to_numpy
    import numpy as np
    started = time.perf_counter()
    sphere = vtk.vtkTexturedSphereSource()
    sphere.SetThetaResolution(48); sphere.SetPhiResolution(32); sphere.Update()
    mesh = sphere.GetOutput(); faces = []; ids = vtk.vtkIdList()
    mesh.GetPolys().InitTraversal()
    while mesh.GetPolys().GetNextCell(ids):
        faces.append([ids.GetId(i) for i in range(ids.GetNumberOfIds())])
    part = dict(binding_id='sphere',
                points=vtk_to_numpy(mesh.GetPoints().GetData()),
                normals=vtk_to_numpy(mesh.GetPointData().GetNormals()),
                faces=faces, matrix=np.eye(4),
                uvs={0: vtk_to_numpy(mesh.GetPointData().GetTCoords())})
    return part, (time.perf_counter() - started) * 1000


def _downscale(source: Path, target: Path, max_side=512):
    """A small copy of one texture, and the time it cost to make it."""
    from PIL import Image
    started = time.perf_counter()
    with Image.open(source) as image:
        image = image.convert('RGBA')
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image.save(target, format='PNG')
    return (time.perf_counter() - started) * 1000


def test_the_worker_side_of_a_row_preview():
    if not SPECS.exists():
        pytest.skip(f'没有规格文件：{SPECS}')
    from prism.model_library.multimaterial.native.renderer import MaterialRenderer
    data = json.loads(SPECS.read_text(encoding='utf-8'))
    materials = data['materials']
    variant = os.environ.get('AUDIT_VARIANT', 'base')
    work = Path(tempfile.mkdtemp(prefix='prism-thumb-worker-'))
    try:
        part, sphere_ms = _sphere()
        # A down-scaled copy of every picture behind the rows.  The rows only
        # show a 128 pixel ball, so the original size is never needed.
        scaled = {}
        scale_ms = 0.0
        if 'downscale' in variant:
            for entry in materials:
                for slot, image in (entry['spec'].get('maps') or {}).items():
                    source = image.get('path')
                    if not source or source in scaled:
                        continue
                    target = work / (Path(source).stem + '.small.png')
                    scale_ms += _downscale(Path(source), target)
                    scaled[source] = str(target)

        rows = []
        shared = None
        for entry in materials:
            spec = json.loads(json.dumps(entry['spec']))
            if scaled:
                for image in spec.get('maps', {}).values():
                    source = image.get('path')
                    if source in scaled:
                        image['path'] = scaled[source]
            started = time.perf_counter()
            if variant.startswith('reuse'):
                if shared is None:
                    create_started = time.perf_counter()
                    shared = MaterialRenderer()
                    create_ms = (time.perf_counter() - create_started) * 1000
                else:
                    create_ms = 0.0
                renderer = shared
            else:
                create_started = time.perf_counter()
                renderer = MaterialRenderer()
                create_ms = (time.perf_counter() - create_started) * 1000
            renderer.window.SetSize(128, 128)
            renderer.metrics.clear()
            load_started = time.perf_counter()
            renderer.load([part], {'sphere': spec}, {'sphere': 'sphere'})
            load_ms = (time.perf_counter() - load_started) * 1000
            png_started = time.perf_counter()
            renderer.png(str(work / 'row.png'))
            png_ms = (time.perf_counter() - png_started) * 1000
            close_ms = 0.0
            if not variant.startswith('reuse'):
                close_started = time.perf_counter()
                renderer.close()
                close_ms = (time.perf_counter() - close_started) * 1000
            rows.append(dict(name=entry['name'], create_ms=create_ms, load_ms=load_ms,
                             png_ms=png_ms, close_ms=close_ms,
                             total_ms=(time.perf_counter() - started) * 1000,
                             metrics=dict(renderer.metrics)))
            print(f"AUDIT: {variant} {entry['name'][:20]:20s} create={create_ms:7.1f} "
                  f"load={load_ms:8.1f} png={png_ms:7.1f} close={close_ms:6.1f} "
                  f"total={rows[-1]['total_ms']:8.1f}", flush=True)
        if shared is not None:
            shared.close()
        total = sum(row['total_ms'] for row in rows)
        decodes = sum(row['metrics'].get('image_decodes', 0) for row in rows)
        reuses = sum(row['metrics'].get('texture_reuses', 0) for row in rows)
        decode_ms = sum(row['metrics'].get('image_read_decode_transform_ms', 0) for row in rows)
        print(f'\nAUDIT: {variant} 共 {len(rows)} 行，总计 {total:.1f}ms '
              f'（解码 {decode_ms:.1f}ms，decodes={decodes} reuses={reuses}，'
              f'球体 {sphere_ms:.1f}ms，缩图准备 {scale_ms:.1f}ms）', flush=True)
        report = os.environ.get('AUDIT_REPORT')
        if report:
            Path(report).write_text(json.dumps(dict(
                variant=variant, sphere_ms=round(sphere_ms, 1),
                scale_ms=round(scale_ms, 1), total_ms=round(total, 1),
                decode_ms=round(decode_ms, 1), decodes=decodes, reuses=reuses,
                rows=[dict(name=row['name'], create_ms=round(row['create_ms'], 1),
                           load_ms=round(row['load_ms'], 1), png_ms=round(row['png_ms'], 1),
                           close_ms=round(row['close_ms'], 1),
                           total_ms=round(row['total_ms'], 1),
                           metrics={key: round(value, 1) for key, value in row['metrics'].items()})
                      for row in rows],
            ), ensure_ascii=False, indent=1), encoding='utf-8')
            print(f'AUDIT: 报告已写入 {report}', flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def test_the_small_copy_paints_the_same_ball():
    """A row picture must look the same with the small copy of the picture.

    The ball is 128 pixels wide, so a 512 pixel copy of a 4K picture has to
    look like the original.  The mean difference of the two pictures says
    whether it does.
    """
    if not SPECS.exists():
        pytest.skip(f'没有规格文件：{SPECS}')
    from prism.model_library.multimaterial.native.renderer import MaterialRenderer
    data = json.loads(SPECS.read_text(encoding='utf-8'))
    entry = data['materials'][0]
    work = Path(tempfile.mkdtemp(prefix='prism-thumb-compare-'))
    try:
        part, _ = _sphere()
        scaled = {}
        for image in (entry['spec'].get('maps') or {}).values():
            source = image.get('path')
            if not source or source in scaled:
                continue
            target = work / (Path(source).stem + '.small.png')
            _downscale(Path(source), target)
            scaled[source] = str(target)
        pictures = []
        for name, paths in (('original', None), ('small', scaled)):
            spec = json.loads(json.dumps(entry['spec']))
            if paths:
                for image in spec.get('maps', {}).values():
                    source = image.get('path')
                    if source in paths:
                        image['path'] = paths[source]
            renderer = MaterialRenderer()
            renderer.window.SetSize(128, 128)
            renderer.load([part], {'sphere': spec}, {'sphere': 'sphere'})
            renderer.png(str(work / ('%s.png' % name)), clean=False)
            renderer.close()
            pictures.append(work / ('%s.png' % name))
        from PIL import Image, ImageChops
        import numpy as np
        with Image.open(pictures[0]) as first, Image.open(pictures[1]) as second:
            left = np.asarray(first.convert('RGB'), dtype='int16')
            right = np.asarray(second.convert('RGB'), dtype='int16')
        difference = np.abs(left - right)
        print(f'\nAUDIT: 原始小图差异 平均 {difference.mean():.2f}/255 '
              f'最大 {difference.max()} 超过 8 的像素 {int((difference.max(axis=2) > 8).sum())}'
              f'/{left.shape[0]*left.shape[1]}', flush=True)
        assert difference.mean() < 8.0, difference.mean()
    finally:
        shutil.rmtree(work, ignore_errors=True)
