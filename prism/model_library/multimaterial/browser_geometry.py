"""Immutable binary geometry for the shared Babylon viewer; no Qt or renderer."""
import hashlib
import json
from pathlib import Path
import re
import os
import tempfile

import numpy as np

from .catalog import catalog_from_payload
from .native.scene_data import read_parts
from .presentation import _publish_cache_file

GEOMETRY_VERSION = 2


def triangle_indices(points, faces):
    """Keep triangles verbatim; ear-clip planar source polygons, including concave ones."""
    result = []
    for face in faces:
        ids = list(map(int, face))
        if len(ids) < 3:
            continue
        if min(ids) < 0 or max(ids) >= len(points):
            raise ValueError('模型面引用了不存在的顶点')
        if len(ids) == 3:
            result.extend(ids)
            continue
        p = np.asarray(points)[ids]
        normal = np.cross(p, np.roll(p, -1, axis=0)).sum(axis=0)
        xy = np.delete(p, int(np.argmax(np.abs(normal))), axis=1)
        area = np.sum(xy[:, 0] * np.roll(xy[:, 1], -1) - np.roll(xy[:, 0], -1) * xy[:, 1])
        if abs(area) < 1e-15:
            raise ValueError('模型包含退化多边形，无法安全三角化')
        sign = 1 if area > 0 else -1
        eps = max(float(np.ptp(xy, axis=0).max()) ** 2 * 1e-12, 1e-18)
        remaining = list(range(len(ids)))
        face_result = []
        def cross(a, b, c):
            ab, ac = xy[b] - xy[a], xy[c] - xy[a]
            return sign * (ab[0] * ac[1] - ab[1] * ac[0])
        while len(remaining) > 3:
            for position, b in enumerate(remaining):
                a, c = remaining[position - 1], remaining[(position + 1) % len(remaining)]
                if cross(a, b, c) <= eps:
                    continue
                if any(cross(a, b, q) >= -eps and cross(b, c, q) >= -eps and cross(c, a, q) >= -eps
                       for q in remaining if q not in (a, b, c)
                       and not any(np.array_equal(xy[q],xy[v]) for v in (a,b,c))):
                    continue
                face_result.extend((ids[a], ids[b], ids[c]))
                remaining.pop(position)
                break
            else:
                raise ValueError('模型多边形自交或无法安全三角化')
        face_result.extend(ids[i] for i in remaining)
        result.extend(face_result)
    return np.asarray(result, dtype='<u4')


def prepare_geometry(artifact, root):
    """Source revision owns geometry; scalar/material edits never reparse it."""
    source = Path(artifact['model_path'])
    catalog = catalog_from_payload(artifact['catalog'])
    stat = source.stat()
    key = hashlib.sha256(json.dumps([GEOMETRY_VERSION, str(source.resolve()), stat.st_size,
        stat.st_mtime_ns, catalog.source_digest, catalog.revision], sort_keys=True).encode()).hexdigest()
    root = Path(root)
    descriptor = root / (key + '.geometry.json')
    instance_count = len({site.instance_key or site.node_path or site.geometry_key for site in catalog.binding_sites})
    if descriptor.is_file():
        value = json.loads(descriptor.read_text(encoding='utf-8'))
        if Path(value['binary_path']).is_file():
            value['statistics']['instances'] = instance_count
            value['uv_origin'] = 'top-left' if source.suffix.lower() in ('.gltf','.glb') else 'bottom-left'
            return value
    parts = read_parts(source, catalog, artifact['assimp_dll'])
    binary = root / (key + '.geometry.bin')
    meshes = []
    statistics = dict(points=0, triangles=0, polygons=0, instances=instance_count,
                      basis='source-binding+ear-clipping', backend_version='Babylon')
    sites = {b.binding_id: b for b in catalog.binding_sites}
    def write(destination):
        with destination.open('wb') as stream:
            def store(values, dtype='<f4'):
                array = np.asarray(values, dtype=dtype).reshape(-1)
                if array.dtype.kind == 'f' and not np.isfinite(array).all():
                    raise ValueError('模型包含非有限坐标')
                offset = stream.tell()
                stream.write(array.tobytes())
                return dict(offset=offset, count=len(array))
            for part in parts:
                indices = triangle_indices(part['points'], part['faces'])
                matrix = np.asarray(part['matrix'])
                if matrix.shape != (4, 4) or not np.isfinite(matrix).all():
                    raise ValueError('模型变换矩阵无效')
                site = sites[part['binding_id']]
                lod = re.search(r'(?:^|[/_])LOD(\d+)(?:$|[/_])', site.node_path, re.I)
                meshes.append(dict(binding_id=part['binding_id'],
                    positions=store(part['points']), indices=store(indices, '<u4'),
                    normals=store(part['normals']) if part.get('normals') is not None else None,
                    uvs={str(k): store(v) for k, v in part.get('uvs', {}).items()},
                    matrix=matrix.T.reshape(-1).tolist(), lod=int(lod[1]) if lod else None))
                statistics['points'] += len(part['points'])
                statistics['polygons'] += len(part['faces'])
                statistics['triangles'] += len(indices) // 3
    _publish_cache_file(binary, write)
    # A concurrently published binary still requires this caller's descriptor.
    if not meshes:
        fd, name = tempfile.mkstemp(prefix=key + '.', suffix='.descriptor.tmp', dir=root)
        os.close(fd)
        write_meshes = Path(name)
        try:
            write(write_meshes)
        finally:
            write_meshes.unlink(missing_ok=True)
    value = dict(version=GEOMETRY_VERSION, key=key, binary_path=str(binary),
                 uv_origin='top-left' if source.suffix.lower() in ('.gltf','.glb') else 'bottom-left',
                 binary_url=binary.resolve().as_uri(), meshes=meshes, statistics=statistics)
    blob = json.dumps(value, allow_nan=False, sort_keys=True).encode()
    _publish_cache_file(descriptor, lambda staged: staged.write_bytes(blob))
    return value
