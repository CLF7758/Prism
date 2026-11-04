"""Read side of a locally built Assimp, through ctypes, without post-processing.

Why this exists: the installed F3D hides source materials and node names behind
its renderer, and the pip VTK wheel has no FBX reader. A shared Assimp build gives
both back as *data* (node tree, per-mesh material index, per-face polygons, UV,
normals, material properties), which is what a bound material editor needs.

Deliberate choices:

* No post-processing flags are passed. Triangulation is not applied, so the face
  list keeps the source polygons and a later wireframe mode can tell source edges
  from triangulated ones.
* Nothing is written: the file is only read, the original bytes are never touched.
* Missing data is reported as missing. A material that only stores a blend/opacity
  channel does not become "translucent SSS", and a bump slot does not become a
  normal map.
* The DLL path is explicit. The module never searches the system, so the version
  under test is the version recorded in the evidence.
"""
from __future__ import annotations

import ctypes
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# aiPrimitiveType bits, straight from assimp's include/assimp/mesh.h
PRIMITIVE_POINT = 0x1
PRIMITIVE_LINE = 0x2
PRIMITIVE_TRIANGLE = 0x4
PRIMITIVE_POLYGON = 0x8

# aiTextureType, assimp 5.4 order
TEXTURE_TYPES = {
    1: 'diffuse', 2: 'specular', 3: 'ambient', 4: 'emissive', 5: 'height',
    6: 'normals', 7: 'shininess', 8: 'opacity', 9: 'displacement', 10: 'lightmap',
    11: 'reflection', 12: 'base_color', 13: 'normal_camera', 14: 'emission_color',
    15: 'metalness', 16: 'diffuse_roughness', 17: 'ambient_occlusion',
    18: 'unknown', 19: 'sheen', 20: 'clearcoat', 21: 'transmission',
}

# aiPropertyTypeInfo
_PTI = {0x1: 'float', 0x2: 'double', 0x3: 'string', 0x4: 'integer', 0x5: 'buffer'}


class _AiString(ctypes.Structure):
    _fields_ = [('length', ctypes.c_uint), ('data', ctypes.c_char * 1024)]

    @property
    def value(self) -> str:
        return self.data[:min(self.length, 1023)].decode('utf-8', 'replace')


class _AiVector3D(ctypes.Structure):
    _fields_ = [('x', ctypes.c_float), ('y', ctypes.c_float), ('z', ctypes.c_float)]


class _AiMatrix4x4(ctypes.Structure):
    _fields_ = [('a1', ctypes.c_float), ('a2', ctypes.c_float), ('a3', ctypes.c_float),
                ('a4', ctypes.c_float),
                ('b1', ctypes.c_float), ('b2', ctypes.c_float), ('b3', ctypes.c_float),
                ('b4', ctypes.c_float),
                ('c1', ctypes.c_float), ('c2', ctypes.c_float), ('c3', ctypes.c_float),
                ('c4', ctypes.c_float),
                ('d1', ctypes.c_float), ('d2', ctypes.c_float), ('d3', ctypes.c_float),
                ('d4', ctypes.c_float)]

    def rows(self) -> tuple:
        return ((self.a1, self.a2, self.a3, self.a4), (self.b1, self.b2, self.b3, self.b4),
                (self.c1, self.c2, self.c3, self.c4), (self.d1, self.d2, self.d3, self.d4))


class _AiFace(ctypes.Structure):
    _fields_ = [('mNumIndices', ctypes.c_uint), ('mIndices', ctypes.POINTER(ctypes.c_uint))]


class _AiMaterialProperty(ctypes.Structure):
    # assimp 5.4 order is mKey, mSemantic, mIndex, mDataLength, mType, mData.
    # (Verified against include/assimp/material.h in the built source tree; the
    # README's field list is in a different order and produced garbage values.)
    _fields_ = [('mKey', _AiString), ('mSemantic', ctypes.c_uint), ('mIndex', ctypes.c_uint),
                ('mDataLength', ctypes.c_uint), ('mType', ctypes.c_uint),
                ('mData', ctypes.c_void_p)]


class _AiMaterial(ctypes.Structure):
    _fields_ = [('mProperties', ctypes.POINTER(ctypes.POINTER(_AiMaterialProperty))),
                ('mNumProperties', ctypes.c_uint), ('mNumAllocated', ctypes.c_uint)]


class _AiMesh(ctypes.Structure):
    _fields_ = [
        ('mPrimitiveTypes', ctypes.c_uint),
        ('mNumVertices', ctypes.c_uint),
        ('mNumFaces', ctypes.c_uint),
        ('mVertices', ctypes.POINTER(_AiVector3D)),
        ('mNormals', ctypes.POINTER(_AiVector3D)),
        ('mTangents', ctypes.POINTER(_AiVector3D)),
        ('mBitangents', ctypes.POINTER(_AiVector3D)),
        ('mColors', ctypes.POINTER(ctypes.c_void_p) * 8),
        ('mTextureCoords', ctypes.POINTER(_AiVector3D) * 8),
        ('mNumUVComponents', ctypes.c_uint * 8),
        ('mFaces', ctypes.POINTER(_AiFace)),
        ('mNumBones', ctypes.c_uint),
        ('mBones', ctypes.c_void_p),
        ('mMaterialIndex', ctypes.c_uint),
        ('mName', _AiString),
        ('mNumAnimMeshes', ctypes.c_uint),
        ('mAnimMeshes', ctypes.c_void_p),
        ('mMethod', ctypes.c_uint),
        ('mAABB', ctypes.c_void_p),
    ]


class _AiNode(ctypes.Structure):
    pass


_AiNode._fields_ = [
    ('mName', _AiString),
    ('mTransformation', _AiMatrix4x4),
    ('mParent', ctypes.POINTER(_AiNode)),
    ('mNumChildren', ctypes.c_uint),
    ('mChildren', ctypes.POINTER(ctypes.POINTER(_AiNode))),
    ('mNumMeshes', ctypes.c_uint),
    ('mMeshes', ctypes.POINTER(ctypes.c_uint)),
    ('mMetaData', ctypes.c_void_p),
]


class _AiTexture(ctypes.Structure):
    _fields_ = [('mWidth', ctypes.c_uint), ('mHeight', ctypes.c_uint),
                ('achFormatHint', ctypes.c_char * 9), ('pcData', ctypes.c_void_p)]


class _AiMetadataEntry(ctypes.Structure):
    _fields_ = [('mType', ctypes.c_uint), ('mData', ctypes.c_void_p)]


class _AiMetadata(ctypes.Structure):
    _fields_ = [('mNumProperties', ctypes.c_uint),
                ('mKeys', ctypes.POINTER(_AiString)),
                ('mValues', ctypes.POINTER(_AiMetadataEntry))]


class _AiScene(ctypes.Structure):
    _fields_ = [
        ('mFlags', ctypes.c_uint),
        ('mRootNode', ctypes.POINTER(_AiNode)),
        ('mNumMeshes', ctypes.c_uint),
        ('mMeshes', ctypes.POINTER(ctypes.POINTER(_AiMesh))),
        ('mNumMaterials', ctypes.c_uint),
        ('mMaterials', ctypes.POINTER(ctypes.POINTER(_AiMaterial))),
        ('mNumAnimations', ctypes.c_uint),
        ('mAnimations', ctypes.c_void_p),
        ('mNumTextures', ctypes.c_uint),
        ('mTextures', ctypes.POINTER(ctypes.POINTER(_AiTexture))),
        ('mNumLights', ctypes.c_uint),
        ('mLights', ctypes.c_void_p),
        ('mNumCameras', ctypes.c_uint),
        ('mCameras', ctypes.c_void_p),
        ('mMetaData', ctypes.c_void_p),
    ]


@dataclass(frozen=True)
class SourceNode:
    """One node instance of the source scene graph."""

    path: str
    name: str
    depth: int
    mesh_indices: tuple[int, ...]
    local_matrix: tuple[tuple[float, ...], ...]


@dataclass(frozen=True)
class SourceMesh:
    index: int
    name: str
    material_index: int
    vertex_count: int
    face_count: int
    primitive_types: int
    has_normals: bool
    has_tangents: bool
    uv_components: tuple[int, ...]
    bone_count: int
    points: tuple[float, ...] = ()
    normals: tuple[float, ...] = ()
    uvs: tuple[float, ...] = ()
    faces: tuple[tuple[int, ...], ...] = ()

    @property
    def keeps_source_polygons(self) -> bool:
        """True when assimp did not triangulate: polygon faces survived."""
        return bool(self.primitive_types & PRIMITIVE_POLYGON)


@dataclass(frozen=True)
class SourceMaterial:
    index: int
    name: str
    properties: dict = field(default_factory=dict)
    textures: dict = field(default_factory=dict)
    unsupported: tuple[str, ...] = ()


@dataclass(frozen=True)
class AssimpScene:
    path: str
    nodes: tuple[SourceNode, ...]
    meshes: tuple[SourceMesh, ...]
    materials: tuple[SourceMaterial, ...]
    embedded_texture_count: int
    metadata: dict = field(default_factory=dict)

    def mesh_instances(self) -> dict:
        """mesh index -> node paths that display it (instance detection)."""
        usage: dict[int, list[str]] = {}
        for node in self.nodes:
            for index in node.mesh_indices:
                usage.setdefault(index, []).append(node.path)
        return usage


class AssimpLibraryError(RuntimeError):
    pass


class AssimpLibrary:
    """Thin, explicit binding to one assimp DLL."""

    def __init__(self, dll_path: str | Path):
        self.dll_path = str(Path(dll_path).resolve())
        if not Path(self.dll_path).is_file():
            raise AssimpLibraryError(f'assimp DLL 不存在：{self.dll_path}')
        self.lib = ctypes.CDLL(self.dll_path)
        self.lib.aiImportFile.restype = ctypes.POINTER(_AiScene)
        self.lib.aiImportFile.argtypes = [ctypes.c_char_p, ctypes.c_uint]
        self.lib.aiReleaseImport.argtypes = [ctypes.POINTER(_AiScene)]
        self.lib.aiGetErrorString.restype = ctypes.c_char_p
        self.lib.aiGetErrorString.argtypes = []

    def version(self) -> dict:
        """Numeric version from the DLL.

        ``aiGetVersionRevision`` is deliberately not called: on this build it
        raises an access violation inside the DLL (observed and reproducible), so
        the revision string is taken from the build record instead of guessed.
        """
        self.lib.aiGetVersionMajor.restype = ctypes.c_uint
        self.lib.aiGetVersionMajor.argtypes = []
        self.lib.aiGetVersionMinor.restype = ctypes.c_uint
        self.lib.aiGetVersionMinor.argtypes = []
        self.lib.aiGetVersionPatch.restype = ctypes.c_uint
        self.lib.aiGetVersionPatch.argtypes = []
        major = self.lib.aiGetVersionMajor()
        minor = self.lib.aiGetVersionMinor()
        patch = self.lib.aiGetVersionPatch()
        return {
            'dll': self.dll_path,
            'version': f'{major}.{minor}.{patch}',
            'major': major, 'minor': minor, 'patch': patch,
            'revision_source': '构建记录（运行时 aiGetVersionRevision 在本构建上访问冲突，未调用）',
        }

    def last_error(self) -> str:
        raw = self.lib.aiGetErrorString()
        return (raw or b'').decode('utf-8', 'replace')

    def read(self, model_path: str | Path, *, with_geometry: bool = True,
             limit_vertices: int = 2_000_000) -> AssimpScene:
        model = str(Path(model_path).resolve())
        pointer = self.lib.aiImportFile(model.encode('utf-8'), 0)
        if not pointer:
            raise AssimpLibraryError(f'Assimp 读取失败：{self.last_error() or "unknown"}（{model}）')
        try:
            return self._scene(model, pointer.contents, with_geometry, limit_vertices)
        finally:
            self.lib.aiReleaseImport(pointer)

    # ---------------------------------------------------------------- internals
    def _scene(self, model: str, scene: _AiScene, with_geometry: bool,
               limit_vertices: int) -> AssimpScene:
        nodes: list[SourceNode] = []
        self._walk(scene.mRootNode, '', 0, nodes)
        meshes = tuple(self._mesh(index, scene.mMeshes[index].contents, with_geometry,
                                  limit_vertices)
                       for index in range(scene.mNumMeshes))
        materials = tuple(self._material(index, scene.mMaterials[index].contents)
                          for index in range(scene.mNumMaterials))
        metadata = self._metadata(scene.mMetaData)
        metadata.update({'animations': int(scene.mNumAnimations),
                         'lights': int(scene.mNumLights),
                         'cameras': int(scene.mNumCameras)})
        return AssimpScene(path=model, nodes=tuple(nodes), meshes=meshes,
                           materials=materials,
                           embedded_texture_count=int(scene.mNumTextures),
                           metadata=metadata)

    def _metadata(self, pointer) -> dict:
        """Scene metadata (FBX unit scale, axis system, and friends) as plain values.

        Read through the documented ``aiMetadata`` layout; anything unexpected is
        skipped rather than interpreted.
        """
        if not pointer:
            return {}
        meta = ctypes.cast(pointer, ctypes.POINTER(_AiMetadata)).contents
        out = {}
        for index in range(min(int(meta.mNumProperties), 128)):
            key = meta.mKeys[index].value
            entry = meta.mValues[index]
            out[key] = _decode_metadata(int(entry.mType), entry.mData)
        return out

    def _walk(self, node: Optional[ctypes.POINTER(_AiNode)], path: str, depth: int,
              rows: list[SourceNode]) -> None:
        if not node:
            return
        current = node.contents
        name = current.mName.value
        here = f'{path}/{name}' if path else name
        rows.append(SourceNode(
            path=here, name=name, depth=depth,
            mesh_indices=tuple(int(current.mMeshes[i]) for i in range(current.mNumMeshes)),
            local_matrix=current.mTransformation.rows(),
        ))
        for index in range(current.mNumChildren):
            self._walk(current.mChildren[index], here, depth + 1, rows)

    def _mesh(self, index: int, mesh: _AiMesh, with_geometry: bool, limit: int) -> SourceMesh:
        points: tuple[float, ...] = ()
        normals: tuple[float, ...] = ()
        uvs: tuple[float, ...] = ()
        faces: tuple[tuple[int, ...], ...] = ()
        if with_geometry and mesh.mNumVertices <= limit:
            points = tuple(component for i in range(mesh.mNumVertices)
                           for component in (mesh.mVertices[i].x, mesh.mVertices[i].y,
                                             mesh.mVertices[i].z))
            if mesh.mNormals:
                normals = tuple(component for i in range(mesh.mNumVertices)
                                for component in (mesh.mNormals[i].x, mesh.mNormals[i].y,
                                                  mesh.mNormals[i].z))
            if mesh.mTextureCoords[0]:
                uvs = tuple(component for i in range(mesh.mNumVertices)
                            for component in (mesh.mTextureCoords[0][i].x,
                                              mesh.mTextureCoords[0][i].y))
            faces = tuple(tuple(int(mesh.mFaces[i].mIndices[k])
                                for k in range(mesh.mFaces[i].mNumIndices))
                          for i in range(mesh.mNumFaces))
        return SourceMesh(
            index=index, name=mesh.mName.value, material_index=int(mesh.mMaterialIndex),
            vertex_count=int(mesh.mNumVertices), face_count=int(mesh.mNumFaces),
            primitive_types=int(mesh.mPrimitiveTypes),
            has_normals=bool(mesh.mNormals), has_tangents=bool(mesh.mTangents),
            uv_components=tuple(int(value) for value in mesh.mNumUVComponents),
            bone_count=int(mesh.mNumBones),
            points=points, normals=normals, uvs=uvs, faces=faces,
        )

    def _material(self, index: int, material: _AiMaterial) -> SourceMaterial:
        """Every material property, decoded, plus a texture slot map.

        Property keys are assimp's ``key#semantic#index`` triple. Texture entries
        carry the aiTextureType in ``semantic``, which is what makes per-slot
        reporting possible without guessing from file names.
        """
        properties: dict = {}
        name = ''
        for position in range(material.mNumProperties):
            prop = material.mProperties[position].contents
            key = prop.mKey.value
            kind = _PTI.get(int(prop.mType), f'type{int(prop.mType)}')
            raw = ctypes.string_at(prop.mData, prop.mDataLength) if prop.mDataLength else b''
            values = _decode_value(kind, raw)
            if key == '?mat.name':
                name = values if isinstance(values, str) else ''
                continue
            properties[f'{key}#{prop.mSemantic}#{prop.mIndex}'] = {
                'type': kind, 'values': values,
            }
        textures = {}
        for key, entry in properties.items():
            base, semantic, slot_index = (key.split('#') + ['', ''])[:3]
            if base == '$tex.file':
                textures[f'{TEXTURE_TYPES.get(int(semantic), f"type{semantic}")}[{slot_index}]'] = \
                    entry['values']
        return SourceMaterial(index=index, name=name, properties=properties,
                              textures=textures, unsupported=())


def _decode_metadata(kind: int, pointer) -> object:
    """Decode one aiMetadataEntry payload by its documented type tag."""
    if not pointer:
        return None
    if kind == 0:      # AI_BOOL
        return bool(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_bool)).contents.value)
    if kind == 1:      # AI_INT32
        return int(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_int32)).contents.value)
    if kind == 2:      # AI_UINT64
        return int(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_uint64)).contents.value)
    if kind == 3:      # AI_FLOAT
        return round(float(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_float)).contents.value), 6)
    if kind == 4:      # AI_DOUBLE
        return round(float(ctypes.cast(pointer, ctypes.POINTER(ctypes.c_double)).contents.value), 6)
    if kind == 5:      # AI_AISTRING
        return ctypes.cast(pointer, ctypes.POINTER(_AiString)).contents.value
    if kind == 6:      # AI_AIVECTOR3D
        vector = ctypes.cast(pointer, ctypes.POINTER(_AiVector3D)).contents
        return [round(vector.x, 6), round(vector.y, 6), round(vector.z, 6)]
    return f'<metadata type {kind}>'


def _decode_value(kind: str, raw: bytes):
    """Decode one assimp property payload; an empty payload is an empty list, not an error."""
    if kind == 'float':
        count = len(raw) // 4
        return [round(value, 6) for value in struct.unpack(f'<{count}f', raw)] if count else []
    if kind == 'integer':
        count = len(raw) // 4
        return list(struct.unpack(f'<{count}i', raw)) if count else []
    if kind == 'double':
        count = len(raw) // 8
        return [round(value, 6) for value in struct.unpack(f'<{count}d', raw)] if count else []
    if kind == 'string':
        # aiString: uint32 length followed by the characters (no terminator).
        if len(raw) >= 4:
            length = struct.unpack('<I', raw[:4])[0]
            return raw[4:4 + min(length, len(raw) - 4)].decode('utf-8', 'replace')
        return raw.decode('utf-8', 'replace')
    if kind == 'buffer':
        # assimp stores booleans (and some enums) as a raw byte blob, not as an
        # aiPTI_Integer: $mat.twosided arrives as one byte, $mat.shadingm as four.
        # Decoding it is required, otherwise "double sided" silently reads as unknown.
        if len(raw) == 1:
            return [raw[0]]
        if len(raw) == 4:
            return list(struct.unpack('<i', raw))
        return {'bytes': len(raw)}
    return f'<{len(raw)} bytes>'
