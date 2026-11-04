"""Wavefront OBJ + MTL: definitions, per-group usage points, honest mapping.

Three rules shape this reader, because Wavefront material libraries are the
format most often mis-mapped by a viewer:

* ``bump`` / ``map_bump`` is **not** a normal map.  It keeps its own source role
  instead of being installed as ``normal``;
* ``map_Ks`` and ``Ns`` are specular terms, not roughness.  A viewer that shows
  them as roughness is lying about the material, so they stay source roles and
  the workflow is named ``mtl_illum_*`` instead of "PBR";
* ``usemtl`` names are resolved through the libraries the file actually names,
  in declaration order.  A name two libraries define is recorded as ambiguous
  and stays reported instead of silently picking one.

Usage points are ``(object, group, usemtl)`` runs.  Four chair legs that share
one ``usemtl`` are four usage points of **one** definition, which is exactly
the "wood, affects 4 places" the UI has to show.

Encoding: the file is parsed as **bytes**.  A name is matched by its bytes, a
display name is decoded separately, and when the encoding is not certain the
definition's identity carries a digest of the raw bytes -- two names that both
decode to replacement characters must not become one material.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..catalog import (
    CHANNEL_R,
    CHANNEL_RGB,
    COLOR_LINEAR,
    COLOR_SRGB,
    CatalogBuilder,
    CatalogParseError,
    MaterialDefinition,
    resource_id_of,
    texture_use,
)
from ..contracts import BindingSite
from . import locator as locators
from . import text as textio

PARSER_ID_OBJ = 'wavefront-obj-1.0'

#: How many options each ``map_*`` option takes before the file name.
MAP_OPTION_ARITY = {
    '-blendu': 1, '-blendv': 1, '-cc': 1, '-clamp': 1, '-imfchan': 1,
    '-mm': 2, '-o': 3, '-s': 3, '-t': 3, '-texres': 1, '-bm': 1,
    '-boost': 1, '-type': 1,
}

#: Options whose argument count is "up to N numbers"; the file name follows
#: them and must not be consumed as the fourth number.
_NUMERIC_OPTIONS = frozenset({'-o', '-s', '-t', '-mm'})

#: ``map_*`` keywords mapped onto a slot: ``keyword -> (slot, channels, colour
#: space, note)``.  A source role Prism cannot edit keeps its own slot name and
#: states why in ``note``, so nothing is dropped and nothing is mislabelled:
#: ``bump`` never becomes ``normal`` and ``map_Ks`` never becomes ``roughness``.
MAP_TO_SLOT = {
    'map_kd': ('basecolor', CHANNEL_RGB, COLOR_SRGB, ''),
    'map_ke': ('emissive', CHANNEL_RGB, COLOR_SRGB, ''),
    'map_d': ('opacity', CHANNEL_R, COLOR_LINEAR, ''),
    'norm': ('normal', CHANNEL_RGB, COLOR_LINEAR, ''),
    'disp': ('displacement', CHANNEL_R, COLOR_LINEAR, ''),
    'bump': ('bump', CHANNEL_R, COLOR_LINEAR,
             'bump 是高度贴图，不是法线贴图，未当作 normal'),
    'map_bump': ('bump', CHANNEL_R, COLOR_LINEAR,
                 'map_bump 是高度贴图，不是法线贴图，未当作 normal'),
    'map_ka': ('ambient', CHANNEL_RGB, COLOR_SRGB, 'map_Ka 是环境色贴图'),
    'map_ks': ('specular', CHANNEL_RGB, COLOR_SRGB,
               'map_Ks 是镜面贴图，不能当作粗糙度'),
    'map_ns': ('specular_exponent', CHANNEL_R, COLOR_LINEAR,
               'map_Ns 是镜面指数贴图，不能当作粗糙度'),
    'map_tr': ('transparency', CHANNEL_R, COLOR_LINEAR,
               'map_Tr 是透明贴图，语义未分解'),
    'refl': ('reflection', CHANNEL_RGB, COLOR_SRGB, 'refl 是反射贴图'),
    'decal': ('decal', CHANNEL_RGB, COLOR_SRGB, 'decal 是贴花贴图'),
}

#: Material-library fields this reader records as numbers.
NUMERIC_FIELDS = ('ka', 'kd', 'ks', 'ke', 'ns', 'ni', 'd', 'tr', 'illum',
                  'tf', 'sharpness', 'pr', 'pm', 'ps', 'pc', 'pcr', 'aniso',
                  'anisor')


@dataclass
class _ObjUsage:
    """One ``(object, group, usemtl)`` run, with the faces that used it."""

    object_key: bytes = b''
    group_key: bytes = b''
    material_key: bytes = b''
    object_name: str = ''
    group_name: str = ''
    material_name: str = ''
    faces: int = 0


@dataclass
class _MtlMaterial:
    name: str
    library: str
    name_key: bytes = b''
    name_identity: str = ''
    values: dict = field(default_factory=dict)
    maps: list = field(default_factory=list)
    unsupported: list = field(default_factory=list)
    lines: list = field(default_factory=list)


def inspect_obj(main_path, *, revision: int = 1):
    """Read a ``.obj`` file, the libraries it names and the maps they declare."""
    path = Path(main_path)
    try:
        data = path.read_bytes()
    except OSError as error:
        raise CatalogParseError(f'OBJ 文件无法读取：{error}') from error
    base_dir = path.parent
    builder = CatalogBuilder(PARSER_ID_OBJ, main_path=path)
    document = textio.decode_text(data)
    builder.note_unknown('text_encoding', document.encoding)
    builder.note_unknown('text_encoding_certain', document.certain)
    if document.candidates:
        builder.note_unknown('text_encoding_candidates', list(document.candidates))
    if document.ambiguous:
        builder.note_unresolved(
            f'OBJ 文本编码不确定（{document.encoding}）；名称按该编码显示，'
            '身份按原始字节，可能仍需人工确认')

    libraries, usages, scanned = _parse_obj(textio.lines(data, document), path)
    builder.note_unknown('object_count', scanned['objects'])
    builder.note_unknown('group_count', scanned['groups'])
    builder.note_unknown('face_count', scanned['faces'])
    if not libraries:
        builder.note_unresolved('OBJ 没有 mtllib，没有任何材质库可读')
    if scanned['usemtl_without_library']:
        builder.note_unresolved('有 usemtl 但没有 mtllib，材质名无法解析')

    definitions: list = []
    first_by_name: dict = {}
    declared: dict = {}
    for library_reference in libraries:
        resolved = locators.resolve_locator(library_reference, base_dir)
        if not resolved.resolved:
            builder.note_unresolved(f'找不到材质库：{library_reference}')
            continue
        library_path = Path(resolved.path)
        try:
            library_data = library_path.read_bytes()
        except OSError as error:
            builder.note_unresolved(f'材质库无法读取：{library_reference}（{error}）')
            continue
        builder.note_resource_digest(resolved.original, resource_id_of(library_data))
        library_text = textio.decode_text(library_data)
        if library_text.ambiguous:
            builder.note_unresolved(
                f'材质库 {library_reference} 的文本编码不确定'
                f'（{library_text.encoding}），名称身份按原始字节')
        library_key = locators.relative_locator(library_path, base_dir)
        seen_in_library: dict = {}
        for material in _parse_mtl(textio.lines(library_data, library_text),
                                   library_key, library_text):
            # Two declarations of one name are two definitions: the same name in
            # one library, or in two libraries, is not the same material.
            occurrence = seen_in_library.get(material.name_key, 0) + 1
            seen_in_library[material.name_key] = occurrence
            suffix = '' if occurrence == 1 else f'@{occurrence}'
            material_id = f'obj:mtl:{library_key}#{material.name_identity}{suffix}'
            if material.name_key in first_by_name:
                declared.setdefault(material.name_key,
                                    [first_by_name[material.name_key].library])
                declared[material.name_key].append(library_key)
                builder.note_unresolved(
                    f'材质名重复：{material.name}'
                    f'（{", ".join(declared[material.name_key])}），'
                    'usemtl 只能按名字绑定到第一条'
                    f'（{first_by_name[material.name_key].library}）')
            else:
                first_by_name[material.name_key] = material
            definitions.append((material_id, material))

    material_ids = {}
    for material_id, material in definitions:
        material_ids.setdefault(material.name_key, material_id)
        builder.add_definition(_definition(builder, material_id, material, base_dir))

    _add_bindings(builder, usages, material_ids)
    return builder.build(revision=revision)


# ---------------------------------------------------------------------------
# OBJ
# ---------------------------------------------------------------------------
def _parse_obj(lines, path: Path) -> tuple:
    libraries: list = []
    usages: list = []
    current = _ObjUsage()
    active = _ObjUsage()
    scanned = {'objects': 0, 'groups': 0, 'faces': 0, 'usemtl_without_library': False}
    for line in lines:
        keyword = line.keyword
        value = line.raw
        if keyword == 'mtllib':
            for name in _library_names(value, line.text, path):
                if name and name not in libraries:
                    libraries.append(name)
        elif keyword == 'usemtl':
            active = _ObjUsage(material_key=value, material_name=line.text)
            if not libraries:
                scanned['usemtl_without_library'] = True
        elif keyword in ('o', 'g'):
            if keyword == 'o':
                scanned['objects'] += 1
                candidate = _ObjUsage(object_key=value, object_name=line.text,
                                      material_key=active.material_key,
                                      material_name=active.material_name)
            else:
                scanned['groups'] += 1
                candidate = _ObjUsage(object_key=current.object_key,
                                      object_name=current.object_name,
                                      group_key=value, group_name=line.text,
                                      material_key=active.material_key,
                                      material_name=active.material_name)
            current = _canonical(usages, candidate)
        elif keyword == 'f':
            if not value:
                continue
            scanned['faces'] += 1
            candidate = _ObjUsage(object_key=current.object_key,
                                  object_name=current.object_name,
                                  group_key=current.group_key,
                                  group_name=current.group_name,
                                  material_key=active.material_key,
                                  material_name=active.material_name)
            current = _canonical(usages, candidate)
            current.faces += 1
        elif keyword == 's':
            # Smoothing groups do not change which material a face uses.
            continue
    return libraries, usages, scanned


def _canonical(usages: list, usage: _ObjUsage) -> _ObjUsage:
    """Return the stored usage with this identity, adding it when new."""
    for item in usages:
        if (item.object_key == usage.object_key
                and item.group_key == usage.group_key
                and item.material_key == usage.material_key):
            return item
    usages.append(usage)
    return usage


def _library_names(value: bytes, text: str, path: Path) -> tuple:
    """The library names of one ``mtllib`` line.

    A line may name several files, and a single name may contain spaces.  The
    whole remainder wins when it resolves as one file; otherwise **every** part
    is used, because a line that names two libraries must not lose the second
    one.  A name that is not valid UTF-8 is also tried through the filesystem's
    own decoding, because that is how the tool that wrote it reads it back.
    """
    if not value:
        return ()
    candidates = []
    for candidate in (text, _fsdecode(value)):
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    for candidate in candidates:
        if ' ' in candidate and locators.resolve_locator(candidate, path.parent).resolved:
            return (candidate,)
    names = []
    for candidate in candidates:
        for part in candidate.split():
            if part not in names:
                names.append(part)
    return tuple(names)


def _fsdecode(value: bytes) -> str:
    try:
        import os
        return os.fsdecode(value)
    except Exception:  # pragma: no cover - defensive
        return ''


# ---------------------------------------------------------------------------
# MTL
# ---------------------------------------------------------------------------
def _parse_mtl(lines, library_key: str, document) -> list:
    del document  # the line already carries its decoding decision
    materials: list = []
    current: Optional[_MtlMaterial] = None
    for line in lines:
        if not line.raw and not line.keyword:
            continue
        keyword = line.keyword
        value = line.raw
        if keyword.startswith('#'):
            continue
        if keyword == 'newmtl':
            current = _MtlMaterial(name=line.text, library=library_key,
                                   name_key=value, name_identity=line.identity)
            materials.append(current)
            continue
        if current is None:
            continue
        current.lines.append(line.text)
        if keyword in MAP_TO_SLOT:
            name, options = _map_argument(line.text)
            if not name:
                current.unsupported.append(f'{keyword} 行没有文件名')
                continue
            slot, _channels, _color_space, note = MAP_TO_SLOT[keyword]
            if note:
                current.unsupported.append(f'{note}：{name}')
            current.maps.append({'keyword': keyword, 'name': name,
                                 'options': options, 'mapped': slot})
        elif keyword in NUMERIC_FIELDS:
            current.values[keyword] = line.text
        else:
            current.unsupported.append(f'未解释的材质库字段：{keyword}')
    return materials


def _map_argument(value: str) -> tuple:
    """``(file name, options)`` of one ``map_*`` line.

    ``-o``/``-s``/``-t`` take *up to* three numbers (``-o u [v [w]]``), so a
    strict fixed arity would swallow the file name.  An option the format gives
    a fixed arity keeps it.
    """
    tokens = value.split()
    options = {}
    index = 0
    while index < len(tokens) and tokens[index].startswith('-'):
        option = tokens[index].lower()
        arity = MAP_OPTION_ARITY.get(option, 0)
        index += 1
        values = []
        if option in _NUMERIC_OPTIONS:
            while len(values) < arity and index < len(tokens) and _is_number(tokens[index]):
                values.append(tokens[index])
                index += 1
        else:
            while len(values) < arity and index < len(tokens):
                if tokens[index].lower() in MAP_OPTION_ARITY:
                    break
                values.append(tokens[index])
                index += 1
        options[option] = values
    if index >= len(tokens):
        return '', options
    return ' '.join(tokens[index:]).strip(), options


def _is_number(token: str) -> bool:
    try:
        float(token)
    except ValueError:
        return False
    return True


def _definition(builder: CatalogBuilder, material_id: str,
                material: _MtlMaterial, base_dir: Path) -> MaterialDefinition:
    textures = []
    for entry in material.maps:
        slot = entry['mapped']
        if not slot:
            continue
        _slot, channels, color_space, _note = MAP_TO_SLOT[entry['keyword'].lower()]
        resource, note = locators.read_reference(entry['name'], base_dir)
        if note:
            builder.note_unresolved(f'{material.name} 的 {entry["keyword"]}：{note}')
        builder.note_resource_digest(resource.locator, resource.resource_id)
        options = entry['options']
        if channels == CHANNEL_RGB and options.get('-imfchan'):
            channels = _imfchan(options.get('-imfchan'))
        textures.append(texture_use(
            slot, resource,
            channels=channels,
            color_space=color_space,
            normal_convention='unspecified' if slot == 'normal' else '',
            uv_transform=_uv_transform(options.get('-o'), options.get('-s')),
            source_locator=entry['name'],
            unknown={'keyword': entry['keyword'], 'options': options,
                     'embedded': False, 'missing': resource.missing,
                     'absolute': resource.absolute, 'remote': resource.remote}))
    # A library that states the same slot twice is ambiguous; the last line
    # wins the way every MTL reader behaves, and the ambiguity is reported.
    ordered: list = []
    index_of: dict = {}
    for use in textures:
        if use.slot in index_of:
            ordered[index_of[use.slot]] = use
            builder.note_unresolved(
                f'{material.name} 重复声明了贴图槽位 {use.slot}，'
                f'后一条（{use.source_locator}）覆盖前一条')
        else:
            index_of[use.slot] = len(ordered)
            ordered.append(use)
    textures = ordered
    return MaterialDefinition(
        material_id=material_id,
        source_key=f'{material.library}#{material.name_identity}',
        name=material.name,
        workflow=_workflow(material),
        textures=tuple(textures),
        properties_json=json.dumps(_mtl_properties(material), ensure_ascii=False,
                                   sort_keys=True),
        unsupported=tuple(dict.fromkeys(material.unsupported)),
        unknown_json=json.dumps({'lines': material.lines}, ensure_ascii=False,
                                sort_keys=True),
    )


def _mtl_properties(material: _MtlMaterial) -> dict:
    properties: dict = {}
    values = material.values
    for key in ('kd', 'ka', 'ks', 'ke'):
        if key in values:
            properties[f'{key}_color'] = _numbers(values[key])
    if 'ns' in values:
        properties['specular_exponent'] = _numbers(values['ns'])
    if 'ni' in values:
        properties['optical_density'] = _numbers(values['ni'])
    for source, target in (('pr', 'roughness_factor'), ('pm', 'metallic_factor')):
        if source in values:
            numbers = _numbers(values[source])
            if numbers:properties[target] = numbers[0]
    if 'illum' in values:
        properties['illum'] = _numbers(values['illum'])
    if 'd' in values:
        properties['opacity'] = _numbers(values['d'])
        properties['opacity_source'] = 'd'
    elif 'tr' in values:
        # Tr is the inverse of d; the derived opacity is recorded together with
        # where it came from instead of pretending the file stated one.
        tr = _numbers(values['tr'])
        properties['opacity'] = [1.0 - tr[0]] if tr else []
        properties['opacity_source'] = 'Tr'
    properties['present'] = sorted(values)
    return properties


def _numbers(text: str) -> list:
    values = []
    for token in str(text).split():
        try:
            values.append(float(token))
        except ValueError:
            continue
    return values


def _uv_transform(offset, scale) -> tuple:
    translation = (0.0, 0.0)
    scaling = (1.0, 1.0)
    if offset:
        numbers = _numbers(' '.join(offset))
        if numbers:
            translation = (numbers[0], numbers[1] if len(numbers) > 1 else 0.0)
    if scale:
        numbers = _numbers(' '.join(scale))
        if numbers:
            scaling = (numbers[0], numbers[1] if len(numbers) > 1 else numbers[0])
    if translation == (0.0, 0.0) and scaling == (1.0, 1.0):
        return ()
    return (translation[0], translation[1], 0.0, scaling[0], scaling[1])


def _imfchan(option) -> str:
    value = (option or [''])[0].lower()
    return {'r': 'R', 'g': 'G', 'b': 'B', 'a': 'A'}.get(value, CHANNEL_RGB)


def _workflow(material: _MtlMaterial) -> str:
    illum = _numbers(material.values.get('illum', ''))
    if not illum:
        return 'mtl_illum_unknown'
    return f'mtl_illum_{int(illum[0])}'


# ---------------------------------------------------------------------------
# binding sites
# ---------------------------------------------------------------------------
def _add_bindings(builder: CatalogBuilder, usages, material_ids: dict) -> None:
    """One binding site per usage run -- including the broken ones.

    A ``usemtl`` whose name no library declares is **still a usage point**: the
    file assigns something there, and dropping it would make the usage count
    wrong.  Its resolution state is ``dangling`` and the original reference is
    kept, so the UI can say "this part points at a material that is not in the
    file" instead of "this part has no material".

    The frozen :class:`BindingSite` contract has no resolution field yet, so the
    state is exposed by
    :func:`prism.model_library.multimaterial.catalog.binding_resolutions`, which
    reads the same diagnostics.  When the field lands, only this builder changes.
    """
    face_counts = {}
    dangling = {}
    for usage in usages:
        if usage.faces <= 0:
            continue
        material_id = material_ids.get(usage.material_key)
        where = f'{usage.object_name or "无 o"}/{usage.group_name or "无 g"}'
        resolution = 'assigned'
        if not usage.material_key:
            resolution = 'unassigned'
            builder.note_unresolved(f'有面组没有 usemtl：{where}（{usage.faces} 面）')
        elif material_id is None:
            resolution = 'dangling'
            builder.note_unresolved(
                f'usemtl 指向未定义的材质：{usage.material_name}（{where}，'
                f'{usage.faces} 面）；使用点保留，分配为悬空')
            dangling[_usage_id(usage)] = {
                'node_path': _node_path(usage),
                'material_name': usage.material_name,
                'material_key': usage.material_key.hex(),
                'faces': usage.faces,
            }
        binding_id = _usage_id(usage)
        key = f'usemtl:{usage.material_name}' if usage.material_key else 'usemtl:'
        builder.add_binding(BindingSite(
            binding_id=binding_id,
            source_key=key,
            node_path=_node_path(usage),
            geometry_key='obj:main',
            primitive_key=key,
            original_material_id=material_id))
        builder.note_binding_resolution(binding_id, resolution)
        face_counts[binding_id] = usage.faces
    if face_counts:
        builder.note_unknown('face_counts', face_counts)
    if dangling:
        builder.note_unknown('dangling_bindings', dangling)


def _usage_id(usage: _ObjUsage) -> str:
    """A binding id that stays distinct even when names decode alike."""
    return ('obj|object:' + _identity(usage.object_key)
            + '|group:' + _identity(usage.group_key)
            + '|usemtl:' + _identity(usage.material_key))


def _identity(raw: bytes) -> str:
    return textio.decode_snippet(raw or b'').identity(raw or b'')


def _node_path(usage: _ObjUsage) -> str:
    return '/'.join(part for part in (usage.object_name, usage.group_name) if part)
