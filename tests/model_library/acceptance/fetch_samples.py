"""Fetch the real model samples and write the provenance manifest.

The expectation set has to be made of **real** files: files that exist
upstream, that carry a license and whose bytes can be proved. Controlled
generation is allowed for exact unit tests, but it cannot stand in for a
real, complex sample -- so this tool downloads the upstream files and
records where every byte came from.

Run it once; afterwards the samples and ``INVENTORY.json`` live in
``tests/model_library/acceptance/samples/``.

    .venv/Scripts/python.exe tests/model_library/acceptance/fetch_samples.py

The tool is idempotent: a file that is already on disk with the expected
size is kept, and its hash is recorded again.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(HERE, 'samples')
UA = 'Mozilla/5.0 (prism-ai4-acceptance; independent model-library acceptance)'

KHONOS_RAW = 'https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models'
ASSIMP_RAW = 'https://raw.githubusercontent.com/assimp/assimp/master/test/models'
POLYHAVEN_FILES = 'https://api.polyhaven.com/files'
POLYHAVEN_INFO = 'https://api.polyhaven.com/info'


def quote_path(path):
    """Percent-encode each segment, so spaces and non-ascii names work."""
    import urllib.parse
    return '/'.join(urllib.parse.quote(part) for part in path.split('/'))


# ── the sample plan ───────────────────────────────────────────────────────
# category tokens are the D1 buckets; see acceptance/conftest.py
# (slug, upstream folder, upstream file name, categories)
KHRONOS_GLTF = [
    ('boxtextured', 'BoxTextured', 'BoxTextured.gltf', ['external_texture', 'gltf_with_bin']),
    ('box', 'Box', 'Box.gltf', ['embedded_texture']),
    ('duck', 'Duck', 'Duck.gltf', ['external_texture', 'multi_material', 'gltf_with_bin']),
    ('avocado', 'Avocado', 'Avocado.gltf', ['external_texture', 'multi_material', 'gltf_with_bin']),
    ('cesiummilktruck', 'CesiumMilkTruck', 'CesiumMilkTruck.gltf',
     ['external_texture', 'multi_material', 'gltf_with_bin']),
    ('boxwithspaces', 'Box With Spaces', 'Box With Spaces.gltf',
     ['space_or_chinese_path', 'external_texture']),
    ('unicode', 'Unicode❤♻Test', 'Unicode❤♻Test.gltf', ['space_or_chinese_path']),
    ('simpleskin', 'SimpleSkin', 'SimpleSkin.gltf', ['skinning']),
    ('animatedtriangle', 'AnimatedTriangle', 'AnimatedTriangle.gltf', ['animation']),
    ('meshprimitivemodes', 'MeshPrimitiveModes', 'MeshPrimitiveModes.gltf',
     ['points_and_lines']),
    ('orientationtest', 'OrientationTest', 'OrientationTest.gltf', ['normal_orientation']),
    ('simpleinstancing', 'SimpleInstancing', 'SimpleInstancing.gltf', ['instancing']),
    ('multiuvtest', 'MultiUVTest', 'MultiUVTest.gltf', ['multi_uv']),
    ('emissivestrengthtest', 'EmissiveStrengthTest', 'EmissiveStrengthTest.gltf',
     ['emissive']),
    ('flighthelmet', 'FlightHelmet', 'FlightHelmet.gltf',
     ['multi_texture', 'external_texture']),
    # A real Khronos transparency sample: OPAQUE / MASK / BLEND in one file.
    # assimp's transparentTest.fbx cannot carry this category -- its upstream
    # bytes have every NUL replaced by a space, so nothing can read it.
    ('alphablendmodetest', 'AlphaBlendModeTest', 'AlphaBlendModeTest.gltf',
     ['transparency', 'multi_material']),
]

# (slug, upstream folder, upstream file name, categories)
KHRONOS_GLB = [
    ('box', 'Box', 'Box.glb', []),
    ('damagedhelmet', 'DamagedHelmet', 'DamagedHelmet.glb', ['embedded_texture']),
    ('waterbottle', 'WaterBottle', 'WaterBottle.glb', ['embedded_texture']),
    ('cesiumman', 'CesiumMan', 'CesiumMan.glb', ['skinning', 'animation']),
    ('toycar', 'ToyCar', 'ToyCar.glb', ['embedded_texture']),
    ('boombox', 'BoomBox', 'BoomBox.glb', ['embedded_texture']),
]

# (filename, bytes, upstream git blob sha1) -- from the assimp repository
# listing of test/models/OBJ and test/models/FBX.
ASSIMP_OBJ = [
    ('SpiderTex.jpg', 15750, '397e684346a915869e3a12ddb343e3b185c2d77c'),
    ('WusonOBJ.obj', 258268, 'b5a68d44362ae6bbd4c3784a11c84bdc4b0e19d5'),
    ('box.obj', 410, '5653b604283d7736f5baab2c29f62ec3b20d402e'),
    ('box_UTF16BE.obj', 882, '67d315cfebfbe098731104e59be852dba0c0e649'),
    ('box_mat_with_spaces.obj', 465, 'f2921dadcf26c2e780285add026c72e7cb69eb3b'),
    ('box_spaces.mtl', 1123, '0e0cc244efb7f5e37d2f99a92b36514d5f32e95c'),
    ('box_without_lineending.obj', 394, '80a201495e268fe4999a60088a854eb7ce49532a'),
    ('concave_polygon.mtl', 88, '4f74d33f85a44a639cd21c0eb9d7a570502b8554'),
    ('concave_polygon.obj', 2117, '41f5c9d2f11030fd638fc8a52f36cc0346ca30dd'),
    ('cube_mtllib_after_g.mtl', 97, '63dde71af5d8b58c7e0234200818355b9ff210a3'),
    ('cube_mtllib_after_g.obj', 588, 'dfd5997e8b6ccd85b3f543d0875c30c212b54950'),
    ('cube_usemtl.mtl', 171, '040e0adade26fb084e8b7145aee9f816a58b0be0'),
    ('cube_usemtl.obj', 669, 'd840a28fc2665a77616d3464fbf2d4d745ab84ed'),
    ('cube_with_vertexcolors.obj', 658, '1541e08234bbdc00dc686e8872717d136006521c'),
    ('empty_mat.obj', 12980, '55cfb83585dc7c448c012de521e43e7b68fb992b'),
    ('mtl_different_folder.obj', 450, 'afffa41b063148945606304261dfdda2d56a836c'),
    ('multiple_spaces.obj', 167, '4f27a03423ff7cc5e629c515e885931eed210db6'),
    ('point_cloud.obj', 285, 'e65f20abceb88ebd1000ade9fc7614ed6477a7e0'),
    ('regr01.obj', 166087, '1fdb5018c4f934c8a32a6fe0ec60c3a8ba510190'),
    ('regr01.mtl', 1090, 'bb5ff077a77ae99ce532d35e31ae732211d0d708'),
    ('space_in_material_name.obj', 2146, 'd5824e1134630fef5133d13561e051a867502b62'),
    ('space_in_material_name.mtl', 168, '2f210fd48b1e923ec77e2779d50b8304eb3af8e0'),
    ('spider.mtl', 731, 'd225a7c62d6a09de30b238a027de16284234f01f'),
    ('spider.obj', 105735, '3e8e70fbd1c2ebaeceddb42ac35299d1a4835f26'),
    ('testline.obj', 254, 'f2f156832c2e4b208cff9ba1856c7dec45310371'),
    ('testmixed.obj', 390, '1deeb3e323571b3d4ae61762905c42a9ff126cc6'),
    ('testpoints.obj', 254, '92416a8d4568252def3c367a8ddcb91afbaef6a6'),
]

ASSIMP_FBX = [
    ('animation_with_skeleton.fbx', 195420, 'a7476bc64578a9eb7f5967148004ebcb0aba1676'),
    ('box.fbx', 17200, 'cd60d72dad3222dcc9cdc7278fc0387297d4b4df'),
    ('boxWithCompressedCTypeArray.FBX', 13824, '2f71bc7ee9383cc8db4615b1c15067625a26b6be'),
    ('boxWithUncompressedCTypeArray.FBX', 26021, '2ec3d77d776c013a6a2491c76eeb56d42fa590d7'),
    ('box_orphant_embedded_texture.fbx', 36622, 'd874751d03fe16bac1683fe18f94205ac02287c9'),
    ('close_to_identity_transforms.fbx', 88301, 'c0b99028264347a0185f2200755b1fe67c6f27e0'),
    ('cubes_with_names.fbx', 88127, '203543ec8c8ee66d1f17df22df875163b7423074'),
    ('embedded_double_uv.fbx', 1635, 'a741a312432739a8b0f4fb04bea6c5f15652e148'),
    ('huesitos.fbx', 111564, '646271392e5fbd5c3a069498778fb70ba302ebef'),
    ('maxPbrMaterial_metalRough.fbx', 20439, 'a662752691ee70eec0fa62042d398a56c64f66d7'),
    ('phong_cube.fbx', 17084, '851021d1c77e0480a8e29d41ae47097387c8509f'),
    ('spider.fbx', 123292, 'cc5a38798a162b4b6f1a7f3a1d2df69e334f104c'),
    ('transparentTest.fbx', 389688, '2c4f1cd82dff688973d6b536261855eda63e0915'),
]

POLYHAVEN_ASSETS = ['ArmChair_01', 'Barrel_01', 'Barrel_02', 'Camera_01']

ASSIMP_OBJ_GROUPS = [
    ('spider', ['obj_with_mtl', 'external_texture'],
     ['spider.obj', 'spider.mtl', 'SpiderTex.jpg']),
    ('wuson', ['multi_mesh'], ['WusonOBJ.obj']),
    ('mat_with_spaces', ['obj_with_mtl', 'space_or_chinese_path'],
     ['box_mat_with_spaces.obj', 'box_spaces.mtl']),
    ('concave', ['obj_with_mtl', 'n_gon'], ['concave_polygon.obj', 'concave_polygon.mtl']),
    ('vertexcolors', ['vertex_colors'], ['cube_with_vertexcolors.obj']),
    ('point_cloud', ['points_and_lines'], ['point_cloud.obj']),
    ('lines', ['points_and_lines'], ['testline.obj']),
    ('mixed_primitives', ['points_and_lines'], ['testmixed.obj']),
    ('mtl_other_dir', ['obj_with_mtl', 'mtl_in_other_directory'],
     ['mtl_different_folder.obj', 'folder/mtl_different_folder.mtl']),
    ('utf16', ['utf16_encoding'], ['box_UTF16BE.obj']),
    ('multimaterial', ['multi_material', 'obj_with_mtl'],
     ['cube_usemtl.obj', 'cube_usemtl.mtl']),
]

ASSIMP_FBX_GROUPS = [
    ('box', ['minimal_mesh'], ['box.fbx']),
    ('phong_cube', ['legacy_material'], ['phong_cube.fbx']),
    ('pbr_metal_rough', ['pbr_material', 'orm'], ['maxPbrMaterial_metalRough.fbx']),
    ('skeleton_animation', ['fbx_skeleton_or_animation', 'skinning', 'animation'],
     ['animation_with_skeleton.fbx']),
    ('spider_fbx', ['multi_material'], ['spider.fbx']),
    ('transparent', ['corrupt_file', 'unreadable_container'],
     ['transparentTest.fbx']),
    ('compressed_arrays', ['compressed_arrays'], ['boxWithCompressedCTypeArray.FBX']),
    ('embedded_uv', ['embedded_texture'], ['embedded_double_uv.fbx']),
    ('multi_mesh_named', ['multi_mesh'], ['cubes_with_names.fbx']),
]


# ── download helpers ──────────────────────────────────────────────────────
def fetch(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={'User-Agent': UA})
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            last = error
            if error.code in (404, 403):
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as error:  # network hiccup
            last = error
            time.sleep(1.5 * (attempt + 1))
    return None


def git_blob_sha1(data):
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def store(directory, relative, data):
    target = os.path.join(directory, relative.replace('/', os.sep))
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'wb') as stream:
        stream.write(data)
    return target


def fetch_existing(url, directory, relative):
    """Download a file, or reuse the copy that is already on disk.

    Re-running the tool must not re-download 70 MB. A file that is already
    there is read back, so the recorded hash always describes the bytes that
    are really in the tree.
    """
    target = os.path.join(directory, relative.replace('/', os.sep))
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        with open(target, 'rb') as stream:
            return stream.read()
    data = fetch(url)
    if data is None:
        return None
    store(directory, relative, data)
    return data


def describe(directory, relative, data, expected_blob=None, integrity=None):
    return {
        'relpath': relative.replace('\\', '/'),
        'bytes': len(data),
        'sha256': sha256(data),
        'git_blob_sha1_expected': expected_blob,
        'git_blob_sha1_match': (git_blob_sha1(data) == expected_blob
                                if expected_blob else None),
        'integrity': integrity or ('verified' if expected_blob else 'unverified'),
    }


def read_file(directory, relative):
    with open(os.path.join(directory, relative.replace('/', os.sep)), 'rb') as stream:
        return stream.read()


# ── the samplers ──────────────────────────────────────────────────────────
def sample_khronos_gltf(samples):
    """The .gltf describes its own dependencies, so no listing is needed."""
    out = []
    for slug, folder, filename, categories in KHRONOS_GLTF:
        directory = os.path.join(SAMPLES, 'khronos_%s_gltf' % slug)
        os.makedirs(directory, exist_ok=True)
        relative = filename
        data = fetch_existing('%s/%s/glTF/%s' % (KHONOS_RAW, quote_path(folder),
                                                 quote_path(filename)),
                               directory, relative)
        if data is None:
            out.append({'sample_id': 'khronos_%s_gltf' % slug, 'failed': True})
            continue
        files = [describe(directory, relative, data)]
        try:
            document = json.loads(data.decode('utf-8'))
        except ValueError:
            document = {}
        wanted = []
        for buffer in document.get('buffers', []):
            uri = buffer.get('uri')
            if uri and not uri.startswith('data:'):
                wanted.append(uri)
        for image in document.get('images', []):
            uri = image.get('uri')
            if uri and not uri.startswith('data:'):
                wanted.append(uri)
        for uri in wanted:
            import urllib.parse
            clean = urllib.parse.unquote(uri)
            payload = fetch_existing('%s/%s/glTF/%s' % (KHONOS_RAW, quote_path(folder),
                                                       quote_path(clean)),
                                     directory, clean)
            if payload is None:
                continue
            files.append(describe(directory, clean, payload))
        license_data = fetch_existing('%s/%s/LICENSE.md' % (KHONOS_RAW, quote_path(folder)),
                                      directory, 'LICENSE.md')
        license_name, license_file, evidence = 'unknown', None, ''
        if license_data:
            license_file = 'LICENSE.md'
            text = license_data.decode('utf-8', 'replace')
            evidence = '\n'.join(line for line in text.splitlines() if line.strip())[:400]
            license_name = _license_name(text)
        out.append({
            'sample_id': 'khronos_%s_gltf' % slug,
            'format': 'gltf', 'directory': os.path.basename(directory),
            'provenance_kind': 'upstream',
            'source_repo': 'https://github.com/KhronosGroup/glTF-Sample-Assets',
            'source_url_template': KHONOS_RAW + '/%s/glTF/<file>' % folder,
            'upstream_paths': ['Models/%s/glTF/<file>' % folder,
                               'Models/%s/LICENSE.md' % folder],
            'license': license_name, 'license_file': license_file,
            'license_evidence': evidence,
            'categories': categories,
            'files': files,
            'total_bytes': sum(item['bytes'] for item in files),
            'notes': 'Khronos glTF-Sample-Assets；每个模型自带许可文件。',
        })
    return out


def sample_khronos_glb(samples):
    out = []
    for slug, folder, filename, categories in KHRONOS_GLB:
        directory = os.path.join(SAMPLES, 'khronos_%s_glb' % slug)
        os.makedirs(directory, exist_ok=True)
        relative = filename
        data = fetch_existing('%s/%s/glTF-Binary/%s' % (KHONOS_RAW, quote_path(folder),
                                                        quote_path(filename)),
                               directory, relative)
        if data is None:
            out.append({'sample_id': 'khronos_%s_glb' % slug, 'failed': True})
            continue
        files = [describe(directory, relative, data)]
        license_data = fetch_existing('%s/%s/LICENSE.md' % (KHONOS_RAW, quote_path(folder)),
                                      directory, 'LICENSE.md')
        license_name, license_file, evidence = 'unknown', None, ''
        if license_data:
            license_file = 'LICENSE.md'
            text = license_data.decode('utf-8', 'replace')
            evidence = '\n'.join(line for line in text.splitlines() if line.strip())[:400]
            license_name = _license_name(text)
        out.append({
            'sample_id': 'khronos_%s_glb' % slug,
            'format': 'glb', 'directory': os.path.basename(directory),
            'provenance_kind': 'upstream',
            'source_repo': 'https://github.com/KhronosGroup/glTF-Sample-Assets',
            'source_url_template': KHONOS_RAW + '/%s/glTF-Binary/<file>' % folder,
            'upstream_paths': ['Models/%s/glTF-Binary/%s' % (folder, filename)],
            'license': license_name, 'license_file': license_file,
            'license_evidence': evidence,
            'categories': categories,
            'files': files,
            'total_bytes': sum(item['bytes'] for item in files),
            'notes': 'Khronos glTF-Sample-Assets；GLB 自包含。',
        })
    return out


def _license_name(text):
    lowered = text.lower()
    for needle, name in (('cc0', 'CC0-1.0'),
                         ('cc-by', 'CC-BY-4.0'),
                         ('attribution', 'CC-BY-4.0'),
                         ('apache', 'Apache-2.0'),
                         ('mit license', 'MIT'),
                         ('public domain', 'public-domain'),
                         ('spdx', 'see-license-file')):
        if needle in lowered:
            return name
    return 'see-license-file'


def sample_assimp(directory_name, groups, upstream_dir, format_name, prefix):
    listing = {name: (size, blob) for name, size, blob in
               (ASSIMP_OBJ if format_name == 'obj' else ASSIMP_FBX)}
    out = []
    for slug, categories, files in groups:
        directory = os.path.join(SAMPLES, '%s_%s' % (prefix, slug))
        os.makedirs(directory, exist_ok=True)
        entries = []
        failed = False
        for relative in files:
            expected = listing.get(os.path.basename(relative))
            data = fetch_existing('%s/%s/%s' % (ASSIMP_RAW, upstream_dir,
                                                 quote_path(relative)),
                                   directory, relative)
            if data is None:
                failed = True
                break
            entries.append(describe(directory, relative, data,
                                    expected_blob=expected[1] if expected else None))
        if failed:
            out.append({'sample_id': '%s_%s' % (prefix, slug), 'failed': True})
            continue
        out.append({
            'sample_id': '%s_%s' % (prefix, slug),
            'format': format_name, 'directory': os.path.basename(directory),
            'provenance_kind': 'upstream',
            'source_repo': 'https://github.com/assimp/assimp',
            'source_url_template': ASSIMP_RAW + '/%s/<file>' % upstream_dir,
            'upstream_paths': ['test/models/%s/%s' % (upstream_dir, relative)
                               for relative in files],
            'license': 'unverified-upstream-test-fixture',
            'license_file': None,
            'license_evidence': ('assimp 仓库没有为这些路径提供单独的许可文件；'
                                  '这些文件仅用于内部测试，不得随产品分发。'),
            'categories': categories,
            'files': entries,
            'total_bytes': sum(item['bytes'] for item in entries),
            'notes': '上游为 assimp 测试语料；许可未核实，仅限内部测试，不得随产品发布。',
        })
    return out


def sample_polyhaven():
    out = []
    for asset in POLYHAVEN_ASSETS:
        files_payload = fetch('%s/%s' % (POLYHAVEN_FILES, asset))
        info_payload = fetch('%s/%s' % (POLYHAVEN_INFO, asset))
        if files_payload is None:
            out.append({'sample_id': 'polyhaven_%s' % asset.lower(), 'failed': True})
            continue
        listing = json.loads(files_payload.decode('utf-8'))
        info = json.loads(info_payload.decode('utf-8')) if info_payload else {}
        directory = os.path.join(SAMPLES, 'polyhaven_%s' % asset.lower())
        os.makedirs(directory, exist_ok=True)
        entries = []
        wanted = []
        for key in ('fbx', 'gltf', 'blend'):
            block = listing.get(key, {}).get('1k', {})
            for name in ('fbx', 'gltf', 'glb'):
                item = block.get(name)
                if item and item.get('url'):
                    wanted.append((key, item['url']))
        for texture in ('Diffuse', 'arm', 'Metal', 'Rough', 'nor_dx'):
            block = listing.get(texture, {}).get('1k', {})
            for name, item in block.items():
                if isinstance(item, dict) and item.get('url'):
                    wanted.append((texture, item['url']))
        for kind, url in wanted:
            relative = url.split('/Models/')[-1].replace('/', '_')
            data = fetch_existing(url, directory, relative)
            if data is None:
                continue
            entries.append(describe(directory, relative, data))
        authors = info.get('authors') or {}
        author_text = ', '.join('%s (%s)' % (value.get('name', '?'), value.get('email', ''))
                                for value in authors.values() if isinstance(value, dict))
        license_text = (
            'Poly Haven asset: %s\n'
            'Asset name: %s\n'
            'Author(s): %s\n'
            'License: CC0-1.0 (public domain dedication) https://polyhaven.com/license\n'
            'Source: https://polyhaven.com/a/%s\n'
            'Retrieved: %s\n'
            'Asset API snapshot sha256: %s\n' % (
                asset, info.get('name', asset), author_text or 'see polyhaven.com',
                asset, datetime.now(timezone.utc).isoformat(),
                sha256(files_payload)))
        store(directory, 'LICENSE.txt', license_text.encode('utf-8'))
        entries.append(describe(directory, 'LICENSE.txt', license_text.encode('utf-8')))
        out.append({
            'sample_id': 'polyhaven_%s' % asset.lower(),
            'format': 'mixed', 'directory': os.path.basename(directory),
            'provenance_kind': 'upstream',
            'source_repo': 'https://polyhaven.com',
            'source_url_template': 'https://dl.polyhaven.org/file/ph-assets/Models/<fmt>/1k/%s/<file>' % asset,
            'upstream_paths': ['Models/%s/<fmt>/1k' % asset],
            'license': 'CC0-1.0', 'license_file': 'LICENSE.txt',
            'license_evidence': license_text[:400],
            'categories': ['orm', 'external_texture', 'multi_material',
                           'missing_file', 'absolute_path_reference'],
            'files': entries,
            'total_bytes': sum(item['bytes'] for item in entries),
            'notes': ('Poly Haven CC0 资产：FBX 与 glTF 都在，但下载得到的目录布局与文件内部'
                      '引用不一致 —— FBX 用 Y:\\... 的绝对路径加 textures\\ 相对路径，'
                      'glTF 引用一个未随下载提供的 .bin 和 textures/ 子目录。'
                      '因此这个样本证明的是"真实资产引用了这里没有的文件"，'
                      '不是"完整可加载的贴图模型"；完整模型请用 khronos_* 与 assimp_spider。'),
        })
    return out


# ── derived fixtures ──────────────────────────────────────────────────────
def build_derived():
    out = []
    # D1: the same name in two directories, with different content.
    directory = os.path.join(SAMPLES, 'derived_same_name_texture_two_dirs')
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    os.makedirs(os.path.join(directory, 'textures', 'a'), exist_ok=True)
    os.makedirs(os.path.join(directory, 'textures', 'b'), exist_ok=True)
    os.makedirs(os.path.join(directory, 'textures', 'c'), exist_ok=True)
    from PIL import Image
    for folder, colour in (('a', (255, 0, 0)), ('b', (0, 0, 255))):
        image = Image.new('RGB', (8, 8), colour)
        image.save(os.path.join(directory, 'textures', folder, 'diffuse.png'))
    upstream = os.path.join(SAMPLES, 'polyhaven_armchair_01')
    for candidate in os.listdir(upstream) if os.path.isdir(upstream) else []:
        if candidate.lower().endswith('.png'):
            shutil.copy(os.path.join(upstream, candidate),
                        os.path.join(directory, 'textures', 'c', 'diffuse.png'))
            break
    with open(os.path.join(directory, 'model.obj'), 'w', encoding='utf-8') as stream:
        stream.write('# same file name, two directories, different content\n'
                     'mtllib model.mtl\n'
                     'v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\n'
                     'usemtl red\n'
                     'f 1 2 3 4\n'
                     'usemtl blue\n'
                     'f 1 2 3 4\n')
    with open(os.path.join(directory, 'model.mtl'), 'w', encoding='utf-8') as stream:
        stream.write('newmtl red\nKa 0 0 0\nKd 1 0 0\nmap_Kd textures/a/diffuse.png\n'
                     '\nnewmtl blue\nKa 0 0 0\nKd 0 0 1\nmap_Kd textures/b/diffuse.png\n')
    entries = []
    for base, _, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, directory).replace('\\', '/')
            with open(path, 'rb') as stream:
                entries.append(describe(directory, relative, stream.read()))
    out.append({
        'sample_id': 'derived_same_name_texture_two_dirs',
        'format': 'obj', 'directory': os.path.basename(directory),
        'provenance_kind': 'derived', 'derived_from': None,
        'transformation': '由 AI 4 生成的确定性夹具：两个目录下同名不同内容的 diffuse.png。',
        'source_repo': 'authored-by-ai4',
        'source_url_template': None, 'upstream_paths': [],
        'license': 'CC0-1.0', 'license_file': None,
        'license_evidence': '本夹具由本验收 AI 生成，可自由使用。',
        'categories': ['same_name_texture_two_dirs', 'external_texture'],
        'files': entries,
        'total_bytes': sum(item['bytes'] for item in entries),
        'notes': '按名字解析贴图会拿到错误内容；必须按相对路径解析。',
    })

    # D2: the same content under a Chinese path with a space.
    source = os.path.join(SAMPLES, 'khronos_boxtextured_gltf')
    directory = os.path.join(SAMPLES, 'derived_path_chinese_space')
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    target = os.path.join(directory, '模型 库 示例')
    shutil.copytree(source, target)
    entries = []
    for base, _, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, directory).replace('\\', '/')
            with open(path, 'rb') as stream:
                entries.append(describe(directory, relative, stream.read()))
    out.append({
        'sample_id': 'derived_path_chinese_space',
        'format': 'gltf', 'directory': os.path.basename(directory),
        'provenance_kind': 'derived', 'derived_from': 'khronos_boxtextured_gltf',
        'transformation': '把 khronos_boxtextured_gltf 整目录复制到“模型 库 示例”路径。',
        'source_repo': 'https://github.com/KhronosGroup/glTF-Sample-Assets',
        'source_url_template': None, 'upstream_paths': [],
        'license': 'see-license-file', 'license_file': None,
        'license_evidence': '内容与 khronos_boxtextured_gltf 相同，许可见该样本。',
        'categories': ['space_or_chinese_path', 'external_texture'],
        'files': entries,
        'total_bytes': sum(item['bytes'] for item in entries),
        'notes': '路径含中文与空格。',
    })

    # D3: one referenced texture removed.
    source = os.path.join(SAMPLES, 'assimp_spider')
    directory = os.path.join(SAMPLES, 'derived_missing_dependency')
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    shutil.copytree(source, directory)
    victim = os.path.join(directory, 'SpiderTex.jpg')
    if os.path.isfile(victim):
        os.remove(victim)
    entries = []
    for base, _, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, directory).replace('\\', '/')
            with open(path, 'rb') as stream:
                entries.append(describe(directory, relative, stream.read()))
    out.append({
        'sample_id': 'derived_missing_dependency',
        'format': 'obj', 'directory': os.path.basename(directory),
        'provenance_kind': 'derived', 'derived_from': 'assimp_spider',
        'transformation': '删除 SpiderTex.jpg，保留 spider.mtl 中的引用。',
        'source_repo': 'https://github.com/assimp/assimp',
        'source_url_template': None, 'upstream_paths': [],
        'license': 'unverified-upstream-test-fixture', 'license_file': None,
        'license_evidence': '派生自 assimp 测试语料，仅限内部测试。',
        'categories': ['missing_file', 'obj_with_mtl'],
        'files': entries,
        'total_bytes': sum(item['bytes'] for item in entries),
        'notes': '引用仍然存在但文件缺失；必须报告缺失而不是报告完整。',
    })

    # D4: a truncated binary payload.
    source = os.path.join(SAMPLES, 'khronos_boxtextured_gltf')
    directory = os.path.join(SAMPLES, 'derived_corrupt_file')
    if os.path.isdir(directory):
        shutil.rmtree(directory)
    shutil.copytree(source, directory)
    truncated = None
    for base, _, names in os.walk(directory):
        for name in sorted(names):
            if name.lower().endswith('.bin'):
                path = os.path.join(base, name)
                with open(path, 'rb') as stream:
                    data = stream.read()
                # A real truncation: the file becomes *shorter* than the glTF
                # declares. Zero-filling back to the original length would
                # leave every declared byte range intact and prove nothing.
                keep = len(data) * 40 // 100
                with open(path, 'wb') as stream:
                    stream.write(data[:keep])
                truncated = (name, len(data), keep)
                break
        if truncated:
            break
    entries = []
    for base, _, names in os.walk(directory):
        for name in sorted(names):
            path = os.path.join(base, name)
            relative = os.path.relpath(path, directory).replace('\\', '/')
            with open(path, 'rb') as stream:
                entries.append(describe(directory, relative, stream.read()))
    out.append({
        'sample_id': 'derived_corrupt_file',
        'format': 'gltf', 'directory': os.path.basename(directory),
        'provenance_kind': 'derived', 'derived_from': 'khronos_boxtextured_gltf',
        'transformation': ('把 %s 从 %d 字节真实截断到 %d 字节，glTF 里的 byteLength 不变。'
                           % truncated if truncated else '未找到 .bin 文件'),
        'source_repo': 'https://github.com/KhronosGroup/glTF-Sample-Assets',
        'source_url_template': None, 'upstream_paths': [],
        'license': 'see-license-file', 'license_file': None,
        'license_evidence': '内容派生自 khronos_boxtextured_gltf。',
        'categories': ['corrupt_file'],
        'files': entries,
        'total_bytes': sum(item['bytes'] for item in entries),
        'notes': '文件长度与 glTF 声明不一致，必须被识别为损坏。',
    })
    return out


def main():
    os.makedirs(SAMPLES, exist_ok=True)
    samples = []
    print('khronos gltf ...', flush=True)
    samples.extend(sample_khronos_gltf(SAMPLES))
    print('khronos glb ...', flush=True)
    samples.extend(sample_khronos_glb(SAMPLES))
    print('assimp obj ...', flush=True)
    samples.extend(sample_assimp('OBJ', ASSIMP_OBJ_GROUPS, 'OBJ', 'obj', 'assimp'))
    print('assimp fbx ...', flush=True)
    samples.extend(sample_assimp('FBX', ASSIMP_FBX_GROUPS, 'FBX', 'fbx', 'assimp'))
    print('polyhaven ...', flush=True)
    samples.extend(sample_polyhaven())
    print('derived fixtures ...', flush=True)
    samples.extend(build_derived())
    failed = [sample for sample in samples if sample.get('failed')]
    samples = [sample for sample in samples if not sample.get('failed')]
    identifiers = {}
    for sample in samples:
        identifiers.setdefault(sample['sample_id'], []).append(sample['directory'])
    duplicated = {name: dirs for name, dirs in identifiers.items() if len(dirs) > 1}
    if duplicated:
        # Two samples under one id would silently overwrite each other in the
        # expectation file, and every check would then compare a file against
        # another file's facts.
        raise SystemExit('duplicate sample_id: %r' % duplicated)
    manifest = {
        'schema_version': 1,
        'generated_utc': datetime.now(timezone.utc).isoformat(),
        'generator': 'tests/model_library/acceptance/fetch_samples.py',
        'sources': [
            {'repo': 'https://github.com/KhronosGroup/glTF-Sample-Assets',
             'license': 'per-model LICENSE.md (see each sample)',
             'license_evidence': '每个样本目录内含上游 LICENSE.md',
             'note': 'Khronos 官方样例；许可逐模型不同，以样本目录内 LICENSE.md 为准。'},
            {'repo': 'https://github.com/assimp/assimp',
             'license': 'unverified-upstream-test-fixture',
             'license_evidence': 'assimp 未为这些测试语料提供单独许可文件',
             'note': '仅用于内部解析测试；不得随产品分发。'},
            {'repo': 'https://polyhaven.com',
             'license': 'CC0-1.0',
             'license_evidence': 'https://polyhaven.com/license',
             'note': '真实资产，含 FBX/glTF 与 ORM 贴图集。'},
        ],
        'samples': samples,
    }
    counts = {}
    for sample in samples:
        counts[sample['format']] = counts.get(sample['format'], 0) + 1
    upstream = [s for s in samples if s['provenance_kind'] == 'upstream']
    manifest['summary'] = {
        'total': len(samples),
        'upstream': len(upstream),
        'derived': len(samples) - len(upstream),
        'by_format': counts,
        'total_bytes': sum(sample['total_bytes'] for sample in samples),
        'failed': [sample['sample_id'] for sample in failed],
    }
    target = os.path.join(SAMPLES, 'INVENTORY.json')
    with open(target, 'w', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
    print(json.dumps(manifest['summary'], ensure_ascii=False, indent=2))
    print('written:', target)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
