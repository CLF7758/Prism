"""Structural fixtures for the D1 expectation set.

D1 asks for structures that real download corpora do not happen to contain:
three clearly different materials, four parts sharing one material, the same
material name with two different definitions, a mesh used by several nodes, one
mesh split into several primitives, a definition nothing binds to, a reference
to a definition that does not exist, and so on.

This module writes those structures as **real files** (glTF + BIN, OBJ + MTL,
PNG textures) plus, for each one, a ``declared`` block: the author's intent in
machine-readable form.  The intent is never used as the expectation.  It is
used the other way round -- the independent reader in
:mod:`material_reference` must derive the same structure from the bytes, and a
disagreement means the *reader* is wrong and has to be fixed before the
expectation set can be trusted.

FBX is deliberately absent: no fixture is synthesised for it, because a
hand-written FBX that no DCC ever opened is not evidence about what DCC output
does.  FBX coverage comes from real samples and the boundary is recorded.
"""
from __future__ import annotations

import io
import json
import os
import struct
import zlib

GENERATOR = 'prism-ai4-independent-fixture'

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__))))))
SAMPLES = os.path.join(REPO, 'tests', 'model_library', 'acceptance', 'samples')

#: The ASCII FBX used as the base for the two derived path variants.  It is a
#: real exporter file; the variants only rewrite path strings inside it, so the
#: structure that gets parsed is still the structure a DCC wrote.
ASCII_FBX_BASE = os.path.join('assimp_pbr_metal_rough',
                              'maxPbrMaterial_metalRough.fbx')


# ── texture bytes ─────────────────────────────────────────────────────────
def png_bytes(width, height, rows):
    """A deterministic PNG, so a fixture hash never drifts between runs."""
    def chunk(tag, payload):
        return (struct.pack('>I', len(payload)) + tag + payload
                + struct.pack('>I', zlib.crc32(tag + payload) & 0xFFFFFFFF))

    raw = b''.join(b'\x00' + bytes(row) for row in rows)
    header = struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def solid_png(red, green, blue, alpha=255, size=2):
    row = [red, green, blue, alpha] * size
    return png_bytes(size, size, [row for _ in range(size)])


#: The textures every fixture may refer to.  Distinct bytes per role, so a
#: wrong-slot binding cannot pass by accident.
TEXTURES = {
    'wood_diffuse.png': solid_png(150, 90, 40),
    'wood_normal.png': solid_png(128, 128, 255),
    'wood_bump.png': solid_png(96, 96, 96),
    'metal_diffuse.png': solid_png(180, 180, 190),
    'metal_orm.png': solid_png(255, 200, 40),
    'glass_diffuse.png': solid_png(200, 230, 240, 90),
    'mask_diffuse.png': solid_png(40, 200, 90, 128),
    'uv_test.png': solid_png(10, 20, 30),
    'second/wood_diffuse.png': solid_png(200, 60, 20),
}


# ── glTF builders ─────────────────────────────────────────────────────────
class Gltf:
    """A minimal but spec-valid glTF/GLB writer for structural fixtures."""

    def __init__(self, container='gltf', bin_name='geometry.bin'):
        self.container = container
        self.bin_name = bin_name
        self.blob = bytearray()
        self.document = {
            'asset': {'version': '2.0', 'generator': GENERATOR},
            'scene': 0,
            'scenes': [{'nodes': []}],
            'nodes': [],
            'meshes': [],
            'materials': [],
            'textures': [],
            'images': [],
            'samplers': [{'magFilter': 9729, 'minFilter': 9987,
                          'wrapS': 10497, 'wrapT': 10497}],
            'accessors': [],
            'bufferViews': [],
            'buffers': [],
        }
        self.textures = {}

    # -- low level --------------------------------------------------------
    def _view(self, payload, target=None):
        while len(self.blob) % 4:
            self.blob.append(0)
        offset = len(self.blob)
        self.blob.extend(payload)
        view = {'buffer': 0, 'byteOffset': offset, 'byteLength': len(payload)}
        if target:
            view['target'] = target
        self.document['bufferViews'].append(view)
        return len(self.document['bufferViews']) - 1

    def _accessor(self, values, component_type, type_name, target,
                  minimum=None, maximum=None, normalize=False):
        formats = {5126: '<f', 5123: '<H', 5125: '<I', 5121: '<B'}
        pack = formats[component_type]
        payload = b''.join(struct.pack(pack, value) for value in values)
        view = self._view(payload, target)
        accessor = {'bufferView': view, 'componentType': component_type,
                    'count': len(values) // {'SCALAR': 1, 'VEC2': 2,
                                             'VEC3': 3, 'VEC4': 4}[type_name],
                    'type': type_name}
        if minimum is not None:
            accessor['min'] = minimum
        if maximum is not None:
            accessor['max'] = maximum
        if normalize:
            accessor['normalized'] = True
        self.document['accessors'].append(accessor)
        return len(self.document['accessors']) - 1

    def add_quad(self, uvs=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)),
                 uv_layers=1, y=0.0):
        positions = [-0.5, y, -0.5, -0.5, y, 0.5, 0.5, y, 0.5, 0.5, y, -0.5]
        indices = [0, 1, 2, 0, 2, 3]
        attributes = {
            'POSITION': self._accessor(
                positions, 5126, 'VEC3', 34962,
                minimum=[-0.5, y, -0.5], maximum=[0.5, y, 0.5]),
            'NORMAL': self._accessor(
                [0.0, 1.0, 0.0] * 4, 5126, 'VEC3', 34962),
        }
        for layer in range(uv_layers):
            attributes['TEXCOORD_%d' % layer] = self._accessor(
                [value for pair in uvs for value in pair], 5126, 'VEC2', 34962)
        indices_accessor = self._accessor(indices, 5123, 'SCALAR', 34963)
        return attributes, indices_accessor

    # -- authoring helpers ------------------------------------------------
    def material(self, **payload):
        self.document['materials'].append(payload)
        return len(self.document['materials']) - 1

    def texture(self, image_name, embedded=False):
        """Register a texture and return its index (images are de-duplicated)."""
        if image_name in self.textures:
            return self.textures[image_name]
        if embedded:
            payload = TEXTURES[image_name]
            view = self._view(payload)
            image = {'bufferView': view, 'mimeType': 'image/png',
                     'name': image_name}
        else:
            image = {'uri': image_name}
        self.document['images'].append(image)
        self.document['textures'].append(
            {'source': len(self.document['images']) - 1, 'sampler': 0})
        index = len(self.document['textures']) - 1
        self.textures[image_name] = index
        return index

    def mesh(self, name, primitives):
        self.document['meshes'].append({'name': name, 'primitives': primitives})
        return len(self.document['meshes']) - 1

    def node(self, name, mesh=None, children=None, extras=None):
        node = {'name': name}
        if mesh is not None:
            node['mesh'] = mesh
        if children:
            node['children'] = children
        if extras:
            node.update(extras)
        self.document['nodes'].append(node)
        return len(self.document['nodes']) - 1

    def root(self, *node_indices):
        self.document['scenes'][0]['nodes'].extend(node_indices)

    def finish(self, directory, extra_files=()):
        """Write the fixture and return {relative path: bytes}."""
        files = {}
        if self.container == 'glb':
            document = dict(self.document)
            while len(self.blob) % 4:
                self.blob.append(0)
            document['buffers'] = [{'byteLength': len(self.blob)}]
            payload = json.dumps(document, ensure_ascii=False,
                                 sort_keys=True).encode('utf-8')
            while len(payload) % 4:
                payload += b' '
            binary = bytes(self.blob)
            total = 12 + 8 + len(payload) + (8 + len(binary) if binary else 0)
            files['model.glb'] = (
                b'glTF' + struct.pack('<II', 2, total)
                + struct.pack('<II', len(payload), 0x4E4F534A) + payload
                + (struct.pack('<II', len(binary), 0x004E4942) + binary
                   if binary else b''))
        else:
            self.document['buffers'] = [
                {'uri': self.bin_name, 'byteLength': len(self.blob)}]
            files[self.bin_name] = bytes(self.blob)
            files['model.gltf'] = json.dumps(
                self.document, ensure_ascii=False, indent=2).encode('utf-8')
        for name in extra_files:
            files[name] = TEXTURES[name]
        for path, payload in files.items():
            target = os.path.join(directory, path.replace('/', os.sep))
            os.makedirs(os.path.dirname(target) or directory, exist_ok=True)
            with open(target, 'wb') as stream:
                stream.write(payload)
        return files


def _write(directory, files):
    for path, payload in files.items():
        target = os.path.join(directory, path.replace('/', os.sep))
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, 'wb') as stream:
            if isinstance(payload, str):
                payload = payload.encode('utf-8')
            stream.write(payload)
    return sorted(files)


# ── the fixtures ──────────────────────────────────────────────────────────
def fixture_gltf_three_distinct(directory):
    """3 materials that differ in values *and* in texture, on 3 own nodes."""
    model = Gltf()
    wood = model.material(
        name='Wood', doubleSided=False,
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('wood_diffuse.png')},
            'metallicFactor': 0.0, 'roughnessFactor': 0.8},
        normalTexture={'index': model.texture('wood_normal.png'), 'scale': 1.0})
    steel = model.material(
        name='Steel',
        pbrMetallicRoughness={
            'baseColorFactor': [0.56, 0.57, 0.58, 1.0],
            'metallicFactor': 1.0, 'roughnessFactor': 0.25})
    glass = model.material(
        name='Glass', alphaMode='BLEND', doubleSided=True,
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('glass_diffuse.png')},
            'metallicFactor': 0.0, 'roughnessFactor': 0.05})
    nodes = []
    for name, material in (('Body', wood), ('Frame', steel), ('Window', glass)):
        attributes, indices = model.add_quad()
        mesh = model.mesh(name + 'Mesh', [{
            'attributes': attributes, 'indices': indices,
            'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory, extra_files=(
        'wood_diffuse.png', 'wood_normal.png', 'glass_diffuse.png'))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Wood', 'Steel', 'Glass'],
            'usage': {'Wood': 1, 'Steel': 1, 'Glass': 1},
            'distinct_values': True,
            'alpha': {'Glass': 'BLEND'},
            'double_sided': {'Glass': True},
            'slots': {'Wood': ['base_color', 'normal']},
            'texture_slots': 3,
        },
    }


def fixture_gltf_four_parts_share_one(directory):
    """Four nodes instance one mesh: one material, four usage sites."""
    model = Gltf()
    wood = model.material(
        name='Wood', pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('wood_diffuse.png')},
            'metallicFactor': 0.0, 'roughnessFactor': 0.7})
    attributes, indices = model.add_quad()
    mesh = model.mesh('LegMesh', [{'attributes': attributes,
                                   'indices': indices, 'material': wood}])
    legs = [model.node('Leg%d' % index, mesh=mesh) for index in range(1, 5)]
    model.root(*legs)
    files = model.finish(directory, extra_files=('wood_diffuse.png',))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Wood'],
            'usage': {'Wood': 4},
            'shared_mesh_instances': 4,
            'distinct_geometry': 1,
        },
    }


def fixture_gltf_same_name_two_definitions(directory):
    """Two definitions both called ``Wood``, bound to different parts."""
    model = Gltf()
    first = model.material(
        name='Wood', pbrMetallicRoughness={
            'baseColorTexture': {
                'index': model.texture('wood_diffuse.png')},
            'metallicFactor': 0.0, 'roughnessFactor': 0.9})
    second = model.material(
        name='Wood', pbrMetallicRoughness={
            'baseColorTexture': {
                'index': model.texture('second/wood_diffuse.png')},
            'metallicFactor': 0.3, 'roughnessFactor': 0.2})
    nodes = []
    for name, material in (('Chair', first), ('Table', second)):
        attributes, indices = model.add_quad()
        mesh = model.mesh(name + 'Mesh', [{'attributes': attributes,
                                           'indices': indices,
                                           'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory, extra_files=(
        'wood_diffuse.png', 'second/wood_diffuse.png'))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Wood', 'Wood'],
            'duplicate_material_names': ['Wood'],
            'usage': {'Wood#0': 1, 'Wood#1': 1},
            'same_name_different_texture': True,
        },
    }


def fixture_gltf_one_mesh_multi_primitive(directory):
    """One mesh, three primitives, three materials, one node."""
    model = Gltf()
    materials = [
        model.material(name='Base', pbrMetallicRoughness={
            'baseColorFactor': [0.2, 0.2, 0.2, 1.0]}),
        model.material(name='Trim', pbrMetallicRoughness={
            'baseColorFactor': [0.9, 0.7, 0.1, 1.0]}),
        model.material(name='Decal', pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('uv_test.png')}}),
    ]
    primitives = []
    for material in materials:
        attributes, indices = model.add_quad()
        primitives.append({'attributes': attributes, 'indices': indices,
                           'material': material})
    mesh = model.mesh('MultiPrimitive', primitives)
    model.root(model.node('Prop', mesh=mesh))
    files = model.finish(directory, extra_files=('uv_test.png',))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Base', 'Trim', 'Decal'],
            'usage': {'Base': 1, 'Trim': 1, 'Decal': 1},
            'primitives_per_mesh': 3,
            'distinct_geometry': 1,
        },
    }


def fixture_gltf_no_material_and_unused(directory):
    """A primitive with no material, a definition nothing uses, numeric-only."""
    model = Gltf()
    used_numeric_only = model.material(
        name='FlatColor', pbrMetallicRoughness={
            'baseColorFactor': [0.1, 0.6, 0.3, 1.0],
            'metallicFactor': 0.0, 'roughnessFactor': 1.0})
    unused = model.material(
        name='UnusedDefinition', pbrMetallicRoughness={
            'baseColorFactor': [1.0, 0.0, 1.0, 1.0]})
    attributes, indices = model.add_quad()
    attributes2, indices2 = model.add_quad(y=0.3)
    mesh = model.mesh('Mixed', [
        {'attributes': attributes, 'indices': indices,
         'material': used_numeric_only},
        {'attributes': attributes2, 'indices': indices2},
    ])
    model.root(model.node('Part', mesh=mesh))
    files = model.finish(directory)
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['FlatColor', 'UnusedDefinition'],
            'usage': {'FlatColor': 1},
            'unused': ['UnusedDefinition'],
            'unassigned_sites': 1,
            'numeric_only_material': 'FlatColor',
        },
    }


def fixture_gltf_missing_texture(directory):
    """One texture exists on disk, one was never delivered."""
    model = Gltf()
    material = model.material(
        name='HalfMissing', pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('wood_diffuse.png')},
            'metallicRoughnessTexture': {
                'index': model.texture('metal_orm.png')}})
    attributes, indices = model.add_quad()
    mesh = model.mesh('Panel', [{'attributes': attributes, 'indices': indices,
                                 'material': material}])
    model.root(model.node('Panel', mesh=mesh))
    files = model.finish(directory, extra_files=('wood_diffuse.png',))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['HalfMissing'],
            'usage': {'HalfMissing': 1},
            'missing_textures': ['metal_orm.png'],
            'present_textures': ['wood_diffuse.png'],
        },
    }


def fixture_gltf_corrupt_json(directory):
    """A file whose JSON stops in the middle: it must be reported as broken."""
    broken = ('{"asset": {"version": "2.0"}, "materials": [{"name": "Cut"'
              ', "pbrMetallicRoughness": {"baseColorFactor": [1, 0')
    files = _write(directory, {'model.gltf': broken})
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'readable': False,
            'reason': 'JSON 截断',
        },
    }


def fixture_gltf_multi_uv_transform(directory):
    """Two UV sets, a non-default transform, a shared RGB/A image, ORM."""
    model = Gltf()
    orm = model.texture('metal_orm.png')
    material = model.material(
        name='Panel', doubleSided=True, alphaMode='BLEND',
        extensions={'KHR_texture_transform': {}},
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('mask_diffuse.png'),
                                 'texCoord': 0},
            'metallicRoughnessTexture': {'index': orm, 'texCoord': 0},
            'metallicFactor': 1.0, 'roughnessFactor': 1.0},
        occlusionTexture={'index': orm, 'texCoord': 0, 'strength': 0.75},
        normalTexture={'index': model.texture('wood_normal.png'),
                       'texCoord': 1, 'scale': 0.5,
                       'extensions': {'KHR_texture_transform': {
                           'offset': [0.25, 0.5], 'scale': [2.0, 2.0],
                           'rotation': 0.7853981633974483, 'texCoord': 1}}})
    model.document['extensionsUsed'] = ['KHR_texture_transform']
    attributes, indices = model.add_quad(uv_layers=2)
    mesh = model.mesh('MultiUV', [{'attributes': attributes,
                                   'indices': indices, 'material': material}])
    model.root(model.node('Panel', mesh=mesh))
    files = model.finish(directory, extra_files=(
        'mask_diffuse.png', 'wood_normal.png', 'metal_orm.png'))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Panel'],
            'usage': {'Panel': 1},
            'uv_sets': 2,
            'double_sided': {'Panel': True},
            'alpha': {'Panel': 'BLEND'},
            'shared_rgb_a_image': True,
            'orm_shared_image': True,
            'normal_texcoord': 1,
            'normal_scale': 0.5,
            'texture_transform': {'offset': [0.25, 0.5], 'scale': [2.0, 2.0]},
        },
    }


def fixture_gltf_alpha_mask_overlap(directory):
    """MASK and BLEND side by side, with geometry that overlaps in space."""
    model = Gltf()
    mask = model.material(
        name='Cutout', alphaMode='MASK', alphaCutoff=0.25,
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('mask_diffuse.png')}})
    blend = model.material(
        name='Smoke', alphaMode='BLEND',
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('glass_diffuse.png')}})
    nodes = []
    for name, material in (('CutoutQuad', mask), ('SmokeQuad', blend)):
        attributes, indices = model.add_quad()          # same place on purpose
        mesh = model.mesh(name, [{'attributes': attributes, 'indices': indices,
                                  'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory, extra_files=(
        'mask_diffuse.png', 'glass_diffuse.png'))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Cutout', 'Smoke'],
            'usage': {'Cutout': 1, 'Smoke': 1},
            'alpha': {'Cutout': 'MASK', 'Smoke': 'BLEND'},
            'alpha_cutoff': {'Cutout': 0.25},
            'overlapping_geometry': True,
        },
    }


def fixture_gltf_transmission_volume(directory):
    """Three different optics: thin transmission, real volume, plain blend.

    The point of the fixture is that the three must not be counted as the same
    thing.  ``PlainBlend`` is *not* scattering -- a report that calls it SSS is
    wrong, and this file is how that claim gets caught.
    """
    model = Gltf()
    thin = model.material(
        name='ThinTransmission',
        extensions={'KHR_materials_transmission': {'transmissionFactor': 1.0}},
        pbrMetallicRoughness={'baseColorFactor': [1.0, 1.0, 1.0, 1.0]})
    volume = model.material(
        name='VolumeGlass',
        extensions={
            'KHR_materials_transmission': {'transmissionFactor': 0.9},
            'KHR_materials_volume': {'thicknessFactor': 0.5,
                                     'attenuationDistance': 0.25,
                                     'attenuationColor': [0.8, 0.9, 1.0]},
            'KHR_materials_ior': {'ior': 1.45}},
        pbrMetallicRoughness={'baseColorFactor': [0.9, 0.95, 1.0, 1.0]})
    plain = model.material(
        name='PlainBlend', alphaMode='BLEND',
        pbrMetallicRoughness={'baseColorFactor': [1.0, 1.0, 1.0, 0.4]})
    model.document['extensionsUsed'] = [
        'KHR_materials_transmission', 'KHR_materials_volume', 'KHR_materials_ior']
    nodes = []
    for name, material in (('Thin', thin), ('Volume', volume), ('Plain', plain)):
        attributes, indices = model.add_quad()
        mesh = model.mesh(name, [{'attributes': attributes, 'indices': indices,
                                  'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory)
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['ThinTransmission', 'VolumeGlass', 'PlainBlend'],
            'usage': {'ThinTransmission': 1, 'VolumeGlass': 1, 'PlainBlend': 1},
            'extensions': {
                'ThinTransmission': ['KHR_materials_transmission'],
                'VolumeGlass': ['KHR_materials_ior', 'KHR_materials_transmission',
                                'KHR_materials_volume'],
                'PlainBlend': [],
            },
            'volume_thickness': 0.5,
            'not_sss': ['PlainBlend'],
        },
    }


def fixture_gltf_embedded_glb(directory):
    """A GLB whose texture lives inside the file: no external dependency."""
    model = Gltf(container='glb')
    material = model.material(
        name='Embedded', pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('wood_diffuse.png',
                                                        embedded=True)}})
    attributes, indices = model.add_quad()
    mesh = model.mesh('Box', [{'attributes': attributes, 'indices': indices,
                               'material': material}])
    model.root(model.node('Box', mesh=mesh))
    files = model.finish(directory)
    return {
        'files': files,
        'declared': {
            'format': 'glb',
            'main_file': 'model.glb',
            'material_names': ['Embedded'],
            'usage': {'Embedded': 1},
            'embedded_media': 1,
            'external_dependencies': 0,
        },
    }


def fixture_gltf_absolute_author_path(directory):
    """A glTF that still points at the author's own machine."""
    model = Gltf()
    material = model.material(
        name='AuthorPath', pbrMetallicRoughness={
            'baseColorTexture': {
                'index': model.texture(
                    'C:/Users/author/Documents/textures/wood.png')}})
    attributes, indices = model.add_quad()
    mesh = model.mesh('Panel', [{'attributes': attributes, 'indices': indices,
                                 'material': material}])
    model.root(model.node('Panel', mesh=mesh))
    files = model.finish(directory)
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['AuthorPath'],
            'usage': {'AuthorPath': 1},
            'absolute_references': ['C:/Users/author/Documents/textures/wood.png'],
            'missing_textures': ['C:/Users/author/Documents/textures/wood.png'],
        },
    }


def fixture_gltf_version_reorder(directory):
    """The next revision of the three-material asset.

    Materials are reordered, one is renamed, one is added and one disappears.
    Any implementation that matches by index gets a different answer than any
    implementation that matches by name -- which is exactly the ambiguity D1
    asks to be recorded rather than resolved by luck.
    """
    model = Gltf()
    steel = model.material(
        name='Steel', pbrMetallicRoughness={
            'baseColorFactor': [0.56, 0.57, 0.58, 1.0],
            'metallicFactor': 1.0, 'roughnessFactor': 0.25})
    oak = model.material(
        name='Oak_Wood', doubleSided=False,
        pbrMetallicRoughness={
            'baseColorTexture': {'index': model.texture('wood_diffuse.png')},
            'metallicFactor': 0.0, 'roughnessFactor': 0.8},
        normalTexture={'index': model.texture('wood_normal.png'), 'scale': 1.0})
    rubber = model.material(
        name='Rubber', pbrMetallicRoughness={
            'baseColorFactor': [0.05, 0.05, 0.05, 1.0],
            'metallicFactor': 0.0, 'roughnessFactor': 1.0})
    nodes = []
    for name, material in (('Body', oak), ('Frame', steel), ('Gasket', rubber)):
        attributes, indices = model.add_quad()
        mesh = model.mesh(name + 'Mesh', [{'attributes': attributes,
                                           'indices': indices,
                                           'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory, extra_files=(
        'wood_diffuse.png', 'wood_normal.png'))
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['Steel', 'Oak_Wood', 'Rubber'],
            'usage': {'Steel': 1, 'Oak_Wood': 1, 'Rubber': 1},
            'version_of': 'gltf_three_distinct',
            'removed': ['Glass'],
            'renamed': {'Wood': 'Oak_Wood'},
            'added': ['Rubber'],
            'reordered': True,
            'ambiguous_match': ['Wood->Oak_Wood（按名不匹配，按值匹配到 Body 使用点）'],
        },
    }


def fixture_obj_bump_vs_normal(directory):
    """Bump and normal separated, plus an MTL statement with no texture."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'mtllib materials.mtl',
        'o Panel',
        'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'v 0.5 0 -0.5',
        'vt 0 0', 'vt 1 0', 'vt 1 1', 'vt 0 1',
        'usemtl BumpedWood',
        'f 1/1 2/2 3/3',
        'f 1/1 3/3 4/4',
        'usemtl NormalledMetal',
        'f 1/1 2/2 3/3',
        'usemtl FlatOnly',
        'f 1/1 2/2 3/3',
        '',
    ])
    mtl = '\n'.join([
        '# prism-ai4-independent-fixture',
        'newmtl BumpedWood',
        'Kd 0.55 0.35 0.15',
        'Ns 12.0',
        'map_Bump -bm 0.5 wood_bump.png',
        '',
        'newmtl NormalledMetal',
        'Kd 0.7 0.7 0.72',
        'map_Kd metal_diffuse.png',
        'norm wood_normal.png',
        '',
        'newmtl FlatOnly',
        'Kd 0.1 0.6 0.3',
        'd 1.0',
        '',
    ])
    files = _write(directory, {
        'model.obj': obj, 'materials.mtl': mtl,
        'wood_bump.png': TEXTURES['wood_bump.png'],
        'wood_normal.png': TEXTURES['wood_normal.png'],
        'metal_diffuse.png': TEXTURES['metal_diffuse.png'],
    })
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['BumpedWood', 'NormalledMetal', 'FlatOnly'],
            'usage': {'BumpedWood': 1, 'NormalledMetal': 1, 'FlatOnly': 1},
            'slots': {'BumpedWood': ['bump'], 'NormalledMetal': ['diffuse', 'normal']},
            'bump_multiplier': 0.5,
            'numeric_only_material': 'FlatOnly',
            'face_counts': {'BumpedWood': 2, 'NormalledMetal': 1, 'FlatOnly': 1},
        },
    }


def fixture_obj_dangling_and_unused(directory):
    """A ``usemtl`` with no definition, faces before any ``usemtl``, and an
    MTL entry nothing binds to."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'v 0.5 0 -0.5',
        'o Loose',
        'f 1 2 3',
        'mtllib materials.mtl',
        'g Named',
        'usemtl MissingDefinition',
        'f 1 2 3',
        'usemtl Present',
        'f 1 3 4',
        '',
    ])
    mtl = '\n'.join([
        'newmtl Present',
        'Kd 0.2 0.4 0.8',
        '',
        'newmtl NeverUsed',
        'Kd 0.9 0.1 0.1',
        'map_Kd wood_diffuse.png',
        '',
    ])
    files = _write(directory, {
        'model.obj': obj, 'materials.mtl': mtl,
        'wood_diffuse.png': TEXTURES['wood_diffuse.png'],
    })
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Present', 'NeverUsed'],
            'usage': {'Present': 1},
            'unused': ['NeverUsed'],
            'dangling_material_references': ['MissingDefinition'],
            'sites_without_material': 1,
        },
    }


def fixture_obj_absolute_author_path(directory):
    """The classic delivered-broken asset: ``map_Kd`` with a C: drive path."""
    obj = '\n'.join([
        'mtllib materials.mtl',
        'o Panel',
        'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'v 0.5 0 -0.5',
        'vt 0 0', 'vt 1 0', 'vt 1 1', 'vt 0 1',
        'usemtl AuthorAbsolute',
        'f 1/1 2/2 3/3',
        '',
    ])
    mtl = '\n'.join([
        'newmtl AuthorAbsolute',
        'Kd 0.6 0.4 0.2',
        r'map_Kd C:\Users\author\Documents\textures\wood_diffuse.png',
        '',
    ])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['AuthorAbsolute'],
            'usage': {'AuthorAbsolute': 1},
            'absolute_references': [
                r'C:\Users\author\Documents\textures\wood_diffuse.png'],
            'missing_textures': [
                r'C:\Users\author\Documents\textures\wood_diffuse.png'],
        },
    }


def fixture_obj_same_name_two_definitions(directory):
    """Two ``newmtl Wood`` blocks with different colours in one MTL."""
    obj = '\n'.join([
        'mtllib materials.mtl',
        'o ChairLeg',
        'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5',
        'usemtl Wood',
        'f 1 2 3',
        'o TableTop',
        'v -0.5 1 -0.5', 'v -0.5 1 0.5', 'v 0.5 1 0.5',
        'usemtl Wood',
        'f 4 5 6',
        '',
    ])
    mtl = '\n'.join([
        'newmtl Wood',
        'Kd 0.55 0.35 0.15',
        'Ns 10.0',
        'map_Kd wood_diffuse.png',
        '',
        'newmtl Wood',
        'Kd 0.20 0.10 0.05',
        'Ns 80.0',
        'map_Kd second/wood_diffuse.png',
        '',
    ])
    files = _write(directory, {
        'model.obj': obj, 'materials.mtl': mtl,
        'wood_diffuse.png': TEXTURES['wood_diffuse.png'],
        'second/wood_diffuse.png': TEXTURES['second/wood_diffuse.png'],
    })
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Wood', 'Wood'],
            'duplicate_material_names': ['Wood'],
            'usage': {'Wood#0': 2},
            'ambiguous': '两个同名 newmtl 都定义在同一个 MTL 里，绑定只按名字',
        },
    }


def fixture_obj_missing_texture(directory):
    """The MTL ships, the texture it names does not."""
    obj = '\n'.join([
        'mtllib materials.mtl',
        'o Panel',
        'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5',
        'vt 0 0', 'vt 1 0', 'vt 1 1',
        'usemtl WaitingForTexture',
        'f 1/1 2/2 3/3',
        '',
    ])
    mtl = '\n'.join([
        'newmtl WaitingForTexture',
        'Kd 0.5 0.5 0.5',
        'map_Kd not_delivered.png',
        '',
    ])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['WaitingForTexture'],
            'usage': {'WaitingForTexture': 1},
            'missing_textures': ['not_delivered.png'],
        },
    }


def fixture_gltf_version_unmatched(directory):
    """A new revision where nothing matches: every name and value changed.

    The point is the *unmatched* case.  A matcher that falls back to "the
    material at the same index" would silently bind the wrong surface here, so
    the expectation records that no match exists and that the ambiguity has to
    be reported rather than guessed.
    """
    model = Gltf()
    surface_a = model.material(
        name='M_A_New', pbrMetallicRoughness={
            'baseColorFactor': [0.11, 0.22, 0.33, 1.0],
            'metallicFactor': 0.4, 'roughnessFactor': 0.6})
    surface_b = model.material(
        name='M_B_New', pbrMetallicRoughness={
            'baseColorFactor': [0.77, 0.11, 0.09, 1.0],
            'metallicFactor': 0.0, 'roughnessFactor': 0.9})
    nodes = []
    for name, material in (('Body', surface_b), ('Frame', surface_a)):
        attributes, indices = model.add_quad()
        mesh = model.mesh(name + 'Mesh', [{'attributes': attributes,
                                          'indices': indices,
                                          'material': material}])
        nodes.append(model.node(name, mesh=mesh))
    model.root(*nodes)
    files = model.finish(directory)
    return {
        'files': files,
        'declared': {
            'format': 'gltf',
            'main_file': 'model.gltf',
            'material_names': ['M_A_New', 'M_B_New'],
            'usage': {'M_A_New': 1, 'M_B_New': 1},
            'version_of': 'gltf_three_distinct',
            'unmatched': '两个材质名与数值都与上一版不同，只能报告无法匹配',
            'index_order_reversed': True,
        },
    }



def fixture_obj_encoding_collision(directory):
    """Three materials whose names are different bytes that display the same.

    ``b'Wood\xff'``, ``b'Wood\xfe'`` and ``b'Wood\xef\xbf\xbd'`` all decode
    to ``Wood\ufffd`` with UTF-8 replacement.  A reader that builds identity
    from the *displayed* name collapses three materials into one and can no
    longer say which part the user is editing.
    """
    obj = b'\n'.join([
        b'# prism-ai4-independent-fixture',
        b'mtllib materials.mtl',
        b'o A', b'v -0.5 0 -0.5', b'v -0.5 0 0.5', b'v 0.5 0 0.5',
        b'usemtl Wood\xff', b'f 1 2 3',
        b'o B', b'v -0.5 1 -0.5', b'v -0.5 1 0.5', b'v 0.5 1 0.5',
        b'usemtl Wood\xfe', b'f 4 5 6',
        b'o C', b'v -0.5 2 -0.5', b'v -0.5 2 0.5', b'v 0.5 2 0.5',
        b'usemtl Wood\xef\xbf\xbd', b'f 7 8 9',
        b'',
    ])
    mtl = b'\n'.join([
        b'newmtl Wood\xff', b'Kd 0.55 0.35 0.15', b'',
        b'newmtl Wood\xfe', b'Kd 0.20 0.10 0.05', b'',
        b'newmtl Wood\xef\xbf\xbd', b'Kd 0.90 0.90 0.10', b'',
    ])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Wood\ufffd', 'Wood\ufffd', 'Wood\ufffd'],
            'duplicate_material_names': ['Wood\ufffd'],
            'usage': {'Wood\ufffd#0': 1, 'Wood\ufffd#1': 1,
                      'Wood\ufffd#2': 1},
            'distinct_byte_identities': 3,
            'has_material_statement': True,
            'encoding_evidence': ('三个 newmtl 名称的原始字节分别是 '
                                 '57 6f 6f 64 ff / 57 6f 6f 64 fe / '
                                 '57 6f 6f 64 ef bf bd，UTF-8 解码后都是 Wood\ufffd'),
        },
    }


def fixture_obj_no_usemtl(directory):
    """The file never states a material at all."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'mtllib materials.mtl',
        'o A', 'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'f 1 2 3',
        'o B', 'v -0.5 1 -0.5', 'v -0.5 1 0.5', 'v 0.5 1 0.5', 'f 4 5 6',
        '',
    ])
    mtl = '\n'.join(['newmtl Unused', 'Kd 0.3 0.3 0.3', ''])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Unused'],
            'usage': {},
            'unused': ['Unused'],
            'unassigned': ['Unused'],
            'has_material_statement': False,
            'sites_without_material': 2,
            'dangling_material_references': [],
        },
    }


def fixture_obj_missing_mtl(directory):
    """``mtllib`` names a file that was never delivered."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'mtllib materials.mtl',
        'o Panel', 'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5',
        'usemtl Wood', 'f 1 2 3',
        '',
    ])
    files = _write(directory, {'model.obj': obj})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': [],
            'usage': {},
            'missing_material_libraries': ['materials.mtl'],
            'dangling_material_references': ['Wood'],
            'has_material_statement': True,
            'sites_without_material': 0,
        },
    }


def fixture_obj_unassigned_before_usemtl(directory):
    """Faces that come before the first ``usemtl`` belong to no material."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'o Loose', 'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'f 1 2 3',
        'mtllib materials.mtl',
        'o Named', 'usemtl Present', 'f 1 2 3',
        '',
    ])
    mtl = '\n'.join(['newmtl Present', 'Kd 0.2 0.4 0.8', ''])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Present'],
            'usage': {'Present': 1},
            'has_material_statement': True,
            'sites_without_material': 1,
            'dangling_material_references': [],
        },
    }


def fixture_obj_dangling_material(directory):
    """``usemtl`` names a material the delivered MTL does not define."""
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'mtllib materials.mtl',
        'o Panel', 'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5',
        'usemtl Gone', 'f 1 2 3',
        '',
    ])
    mtl = '\n'.join(['newmtl Other', 'Kd 0.9 0.1 0.1', ''])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Other'],
            'usage': {},
            'unused': ['Other'],
            'dangling_material_references': ['Gone'],
            'has_material_statement': True,
            'sites_without_material': 0,
        },
    }



def fixture_obj_mixed_assignment(directory):
    """One file that contains an unassigned part, a dangling name and a real one.

    Counting four separate files is not the same as distinguishing the three
    states *per binding site*: a reader that collapses them still passes the
    counts.  This fixture makes the per-site question unavoidable.
    """
    obj = '\n'.join([
        '# prism-ai4-independent-fixture',
        'mtllib materials.mtl',
        'o Loose', 'v -0.5 0 -0.5', 'v -0.5 0 0.5', 'v 0.5 0 0.5', 'f 1 2 3',
        'o Normal', 'usemtl Present', 'f 1 2 3',
        'o Broken', 'usemtl Gone', 'f 1 2 3',
        '',
    ])
    mtl = '\n'.join([
        'newmtl Present', 'Kd 0.2 0.4 0.8', '',
        'newmtl NeverUsed', 'Kd 0.9 0.1 0.1', '',
    ])
    files = _write(directory, {'model.obj': obj, 'materials.mtl': mtl})
    return {
        'files': files,
        'declared': {
            'format': 'obj',
            'main_file': 'model.obj',
            'material_names': ['Present', 'NeverUsed'],
            'usage': {'Present': 1},
            'unused': ['NeverUsed'],
            'dangling_material_references': ['Gone'],
            'has_material_statement': True,
            'sites_without_material': 1,
            # 逐使用点状态：专项测试按 binding 顺序核对这一列，不进通用 compare。
            'site_states': [
                {'site': 'obj:Loose/(default)#(none)', 'kind': 'unassigned',
                 'reference': ''},
                {'site': 'obj:Normal/(default)#Present', 'kind': 'bound',
                 'reference': 'Present'},
                {'site': 'obj:Broken/(default)#Gone', 'kind': 'dangling',
                 'reference': 'Gone'},
            ],
        },
    }


def _derive_ascii_fbx(directory, replacements, extra_files):
    """Copy the real ASCII FBX and rewrite only the given path strings."""
    base = os.path.join(SAMPLES, ASCII_FBX_BASE)
    with io.open(base, encoding='utf-8', errors='replace') as stream:
        text = stream.read()
    for old, new in replacements:
        if text.count(old) < 1:
            raise AssertionError('派生夹具的替换目标不存在：%r' % (old,))
        text = text.replace(old, new, 1)
    files = {'model.fbx': text.encode('utf-8')}
    for name in extra_files:
        files[name] = TEXTURES[name]
    return _write(directory, files)


def fixture_fbx_path_conflict(directory):
    """Two declarations for one texture, both present, pointing at different bytes.

    The asset is ambiguous: adopting either silently makes "which texture is
    this" depend on the reader.  The expectation is that the conflict is
    reported and the adopted file is named.
    """
    files = _derive_ascii_fbx(
        directory,
        [('FileName: "Textures\\albedo.png"',
          'FileName: "alt\\albedo_alt.png"')],
        ('wood_diffuse.png', 'metal_diffuse.png'))
    _write(directory, {'alt/albedo_alt.png': TEXTURES['metal_diffuse.png'],
                       'Textures/albedo.png': TEXTURES['wood_diffuse.png']})
    return {
        'files': files,
        'declared': {
            'format': 'fbx',
            'main_file': 'model.fbx',
            'material_names': ['PBR Material'],
            'usage': {'PBR Material': 1},
            'path_probe': {
                'slot': 'diffuse',
                'declarations': 2,
                'expect_conflict': True,
                'expect_selected_exists': True,
                'derived_from': ASCII_FBX_BASE,
                'note': ('只改写贴图路径字符串，其余结构与真实 ASCII FBX 相同；'
                         '两个候选都存在但内容不同'),
            },
        },
    }


def fixture_fbx_candidates_all_missing(directory):
    """Both declarations are absent on disk: nothing may be adopted."""
    files = _derive_ascii_fbx(
        directory,
        [('FileName: "Textures\\albedo.png"',
          'FileName: "Z:\\author-only\\albedo.png"')],
        ())
    return {
        'files': files,
        'declared': {
            'format': 'fbx',
            'main_file': 'model.fbx',
            'material_names': ['PBR Material'],
            'usage': {'PBR Material': 1},
            'path_probe': {
                'slot': 'diffuse',
                'declarations': 2,
                'expect_conflict': False,
                'expect_selected_exists': False,
                'expect_selected_empty': True,
                'derived_from': ASCII_FBX_BASE,
                'note': '两条声明都指向本机不存在的文件；selected 必须为空',
            },
        },
    }


FIXTURES = {
    'gltf_three_distinct': fixture_gltf_three_distinct,
    'gltf_four_parts_share_one': fixture_gltf_four_parts_share_one,
    'gltf_same_name_two_definitions': fixture_gltf_same_name_two_definitions,
    'gltf_one_mesh_multi_primitive': fixture_gltf_one_mesh_multi_primitive,
    'gltf_no_material_and_unused': fixture_gltf_no_material_and_unused,
    'gltf_missing_texture': fixture_gltf_missing_texture,
    'gltf_corrupt_json': fixture_gltf_corrupt_json,
    'gltf_multi_uv_transform': fixture_gltf_multi_uv_transform,
    'gltf_alpha_mask_overlap': fixture_gltf_alpha_mask_overlap,
    'gltf_transmission_volume': fixture_gltf_transmission_volume,
    'gltf_embedded_glb': fixture_gltf_embedded_glb,
    'gltf_absolute_author_path': fixture_gltf_absolute_author_path,
    'gltf_version_reorder': fixture_gltf_version_reorder,
    'gltf_version_unmatched': fixture_gltf_version_unmatched,
    'obj_bump_vs_normal': fixture_obj_bump_vs_normal,
    'obj_dangling_and_unused': fixture_obj_dangling_and_unused,
    'obj_absolute_author_path': fixture_obj_absolute_author_path,
    'obj_same_name_two_definitions': fixture_obj_same_name_two_definitions,
    'obj_missing_texture': fixture_obj_missing_texture,
    'obj_encoding_collision': fixture_obj_encoding_collision,
    'obj_no_usemtl': fixture_obj_no_usemtl,
    'obj_missing_mtl': fixture_obj_missing_mtl,
    'obj_unassigned_before_usemtl': fixture_obj_unassigned_before_usemtl,
    'obj_dangling_material': fixture_obj_dangling_material,
    'obj_mixed_assignment': fixture_obj_mixed_assignment,
    'fbx_path_conflict': fixture_fbx_path_conflict,
    'fbx_candidates_all_missing': fixture_fbx_candidates_all_missing,
}


def build(root):
    """Write every fixture under ``root`` and return {name: record}."""
    built = {}
    for name, builder in sorted(FIXTURES.items()):
        directory = os.path.join(root, name)
        os.makedirs(directory, exist_ok=True)
        for stale in os.listdir(directory):
            target = os.path.join(directory, stale)
            if os.path.isfile(target):
                os.remove(target)
        record = builder(directory)
        record['directory'] = name
        built[name] = record
    return built
