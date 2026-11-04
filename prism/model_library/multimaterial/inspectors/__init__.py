"""Format dispatch for the source-material catalog.

One entry point, one answer per format, no silent fallback: an extension this
package does not implement raises instead of returning an empty catalog, because
"no materials" and "not parsed" are different facts and the UI must never show
the first when the second is true.
"""
from __future__ import annotations

from pathlib import Path

from ..catalog import CatalogParseError, MaterialCatalog
from .fbx import PARSER_ID_FBX, inspect_fbx
from .gltf import PARSER_ID_GLB, PARSER_ID_GLTF, inspect_glb, inspect_gltf
from .obj import PARSER_ID_OBJ, inspect_obj

#: Extension -> (parser id, reader).
INSPECTORS = {
    '.gltf': (PARSER_ID_GLTF, inspect_gltf),
    '.glb': (PARSER_ID_GLB, inspect_glb),
    '.obj': (PARSER_ID_OBJ, inspect_obj),
    '.fbx': (PARSER_ID_FBX, inspect_fbx),
}

#: The formats a catalog can be read from, in the app's own extension list.
SUPPORTED_FORMATS = tuple(sorted(INSPECTORS))

#: What each parser does not do yet, reported to the user instead of hidden.
PARSER_LIMITS = {
    PARSER_ID_GLTF: (
        'KHR_draco_mesh_compression 只影响几何，材质清单不受影响',
        'KHR_materials_* 中除 unlit 与 pbrSpecularGlossiness 外只登记不求值',
    ),
    PARSER_ID_GLB: ('GLB 的 BIN 块只按引用读取图片区段，不改写容器',),
    PARSER_ID_OBJ: (
        'OBJ 没有节点实例，使用点用 o/g/usemtl 定位',
        'bump/map_bump 不当作 normal，map_Ks 不当作 roughness',
    ),
    PARSER_ID_FBX: (
        '几何顶点数据不载入，只保留材质索引数组',
        '程序贴图与分层贴图只登记，不展开',
    ),
}


def inspector_for(path) -> tuple:
    """``(parser id, reader)`` for one path, or an explicit failure."""
    suffix = Path(path).suffix.lower()
    entry = INSPECTORS.get(suffix)
    if entry is None:
        raise CatalogParseError(f'不支持的模型格式，无法读取材质清单：{suffix or "无扩展名"}')
    return entry


def inspect_source(main_path, *, revision: int = 1) -> MaterialCatalog:
    """Read the material catalog of one model file."""
    path = Path(main_path)
    if not path.is_file():
        raise CatalogParseError(f'模型文件不存在：{path}')
    _parser_id, reader = inspector_for(path)
    return reader(path, revision=revision)


def parser_limits(parser_id: str) -> tuple:
    """The documented limits of one parser."""
    return PARSER_LIMITS.get(parser_id, ())
