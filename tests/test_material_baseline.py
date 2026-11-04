"""材质能力逐项不退化基线（E-3）。

每一项材质能力一个独立用例：退化时能点出**是哪一项**退了，而不是只得到
一个"材质整体没报错"。本文件只依赖公开 API 的行为契约
（``MaterialLibraryService.configure`` / ``update`` / ``list_records`` /
``save_preview`` / ``render_signature`` / ``prepare_render``），不��言内部实现，
所以材质模块的重构不会把它打坏。

两处重点：

* **状态契约两半**在同一个用例里一起断言 —— 离开材质时 ``preview.properties``
  里的 ``slots`` / ``flipX`` / ``flipY`` / ``alphaCutoff`` 被丢弃，而
  ``textures[slot]`` 绑定、``textures[slot]['channel']`` 通道、``tags``、
  ``notes``、``files``（原图附件）逐字段不变。
* **像素指纹**必须能区分"同一张图的不同渲染"和"不同图"，否则退化会被
  平均值掩盖。
"""
import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

import pytest
from PIL import Image

from prism.material_parameters import RANGES, slot_options
from prism.material_service import MaterialLibraryService
from prism.material_textures import (CHANNELS, SCALARS, SLOTS, prepare_render,
                                     prepare_texture, render_signature,
                                     selected_image)


# ── 基线工具（tools/material/baseline.py）里的纯函数 ────────────────────

def _load_baseline_tool():
    """Load the baseline tool by path so no package/__init__ is needed."""
    path = Path(__file__).resolve().parents[1] / 'tools' / 'material' / 'baseline.py'
    spec = importlib.util.spec_from_file_location('prism_material_baseline_tool', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


baseline_tool = _load_baseline_tool()


# ── 最小场景替身 ────────────────────────────────────────────────────────

class _Scene:
    """The service only needs a project id, the record list and the attachments."""

    def __init__(self, project_id='material-baseline'):
        self.project_id = project_id
        self.material_records = []
        self.attachments = {}


class _Folders:
    """No folders: every record lives at the root of the material library."""

    def _by_id(self, folder_id):
        return None

    def _descendants(self, folder_id):
        return ()


@pytest.fixture
def service():
    scene = _Scene()
    return MaterialLibraryService(scene, _Folders()), scene


# ── 小工具 ──────────────────────────────────────────────────────────────

def _map_path(uri):
    """prepare_render hands back file:// URIs; map one back to a real path."""
    return Path(url2pathname(urlparse(uri).path))


def _write_rgb(path, size=32, color=(30, 120, 200)):
    Image.new('RGB', (size, size), color).save(path)
    return path


def _write_gray(path, size=32, level=128):
    Image.new('L', (size, size), level).save(path)
    return path


def _write_rgba(path, size=32, color=(30, 120, 200, 255)):
    Image.new('RGBA', (size, size), color).save(path)
    return path


def _write_binary_alpha(path, size=64):
    """Black/white cut-out: classify_transparency() must call this 'masked'."""
    image = Image.new('RGBA', (size, size), (30, 120, 200, 0))
    for x in range(size // 2):
        for y in range(size):
            image.putpixel((x, y), (30, 120, 200, 255))
    image.save(path)
    return path


def _write_gradient_alpha(path, size=64):
    """Continuous grey alpha: classify_transparency() must call this 'translucent'."""
    image = Image.new('RGBA', (size, size))
    for x in range(size):
        level = round(255 * x / (size - 1))
        for y in range(size):
            image.putpixel((x, y), (30, 120, 200, level))
    image.save(path)
    return path


def _payload(service, record_id, directory):
    record = service.get(record_id)
    return prepare_render(record, service.scene.attachments, directory)


# ── 1. 不透明 ───────────────────────────────────────────────────────────

def test_capability_opaque_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    source = _write_rgb(tmp_path / 'base.png')
    record = library.create(name='不透明材质')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='RGB')
    # 自动判定：basecolor 不带 alpha 通道 -> 没有有效透明输入 -> opaque
    auto = _payload(library, record['id'], tmp_path / 'auto')
    assert auto['properties']['blendMode'] == 'opaque'
    assert auto['properties']['autoBlendMode'] == 'opaque'
    # 显式指定：显式值必须压过自动判定
    library.configure(record['id'], blend_mode='opaque')
    explicit = _payload(library, record['id'], tmp_path / 'explicit')
    assert explicit['properties']['blendMode'] == 'opaque'
    assert explicit['properties']['sourceAutoBlendMode'] == 'opaque'
    # 合法值集合
    for mode in ('auto', 'opaque', 'masked', 'translucent'):
        library.configure(record['id'], blend_mode=mode)
        assert library.get(record['id'])['preview']['properties']['blendMode'] == mode
    with pytest.raises(ValueError, match='透明方式'):
        library.configure(record['id'], blend_mode='alpha')


# ── 2. 透明裁切 ─────────────────────────────────────────────────────────

def test_capability_masked_with_alpha_cutoff_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    source = _write_binary_alpha(tmp_path / 'cutout.png')
    record = library.create(name='透明裁切材质')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='RGB')
    library.configure(record['id'], properties_patch={'alphaCutoff': 0.3})
    payload = _payload(library, record['id'], tmp_path / 'masked')
    assert payload['properties']['blendMode'] == 'masked'
    assert payload['properties']['autoBlendReason']
    assert payload['properties']['alphaCutoff'] == pytest.approx(0.3)
    assert 'opacity' in payload['maps']
    # alphaCutoff 的合法区间是 0..1
    with pytest.raises(ValueError, match='材质设置'):
        library.configure(record['id'], properties_patch={'alphaCutoff': 1.5})
    with pytest.raises(ValueError, match='材质设置'):
        library.configure(record['id'], properties_patch={'alphaCutoff': -0.1})
    library.configure(record['id'], properties_patch={'alphaCutoff': 0})
    library.configure(record['id'], properties_patch={'alphaCutoff': 1})
    assert library.get(record['id'])['preview']['properties']['alphaCutoff'] == 1


# ── 3. 半透明 ───────────────────────────────────────────────────────────

def test_capability_translucent_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    source = _write_gradient_alpha(tmp_path / 'glass.png')
    record = library.create(name='半透明材质')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='RGB')
    payload = _payload(library, record['id'], tmp_path / 'translucent')
    assert payload['properties']['blendMode'] == 'translucent'
    assert payload['properties']['autoBlendMode'] == 'translucent'
    library.configure(record['id'], blend_mode='translucent')
    explicit = _payload(library, record['id'], tmp_path / 'explicit')
    assert explicit['properties']['blendMode'] == 'translucent'


# ── 4. 金属 ─────────────────────────────────────────────────────────────

def test_capability_metallic_lands_in_the_packed_blue_channel(service, tmp_path):
    library, scene = service
    assert RANGES['metallic'] == (0., 1., 0.)
    assert slot_options({}, 'metallic')['value'] == 0.
    source = tmp_path / 'packed.png'
    Image.new('RGB', (8, 8), (32, 96, 192)).save(source)
    record = library.create(name='金属材质')
    library.configure(record['id'], slot='metallic',
                      prepared=prepare_texture(source), channel='B')
    library.configure(record['id'], slot_settings=('metallic', {'value': .9}))
    stored = library.get(record['id'])
    assert stored['preview']['properties']['slots']['metallic']['value'] == pytest.approx(.9)
    # 数值滑块的范围校验
    with pytest.raises(ValueError, match='范围'):
        library.configure(record['id'], slot_settings=('metallic', {'value': 1.4}))
    payload = _payload(library, record['id'], tmp_path / 'metallic')
    assert 'metallicRoughness' in payload['maps']
    with Image.open(_map_path(payload['maps']['metallicRoughness'])) as packed:
        # Babylon PBR metallicTexture: G = roughness, B = metallic.
        # 这里只绑了金属度，所以 G 是"没有粗糙度贴图"的默认 255，B 是金属度贴图的 B 通道 192。
        assert packed.getpixel((0, 0)) == (255, 255, 192)


# ── 5. 粗糙 ─────────────────────────────────────────────────────────────

def test_capability_roughness_lands_in_the_packed_green_channel(service, tmp_path):
    library, scene = service
    assert RANGES['roughness'] == (0., 1., .5)
    assert slot_options({}, 'roughness')['value'] == .5
    source = tmp_path / 'packed.png'
    Image.new('RGB', (8, 8), (32, 96, 192)).save(source)
    record = library.create(name='粗糙材质')
    library.configure(record['id'], slot='roughness',
                      prepared=prepare_texture(source), channel='G')
    payload = _payload(library, record['id'], tmp_path / 'roughness')
    assert 'metallicRoughness' in payload['maps']
    with Image.open(_map_path(payload['maps']['metallicRoughness'])) as packed:
        # 这里只绑了粗糙度，所以 G 是粗糙度贴图的 G 通道 96，B 是"没有金属度贴图"的默认 255。
        assert packed.getpixel((0, 0)) == (255, 96, 255)
    # 贴图存在时粗糙度滑块写的是 factor，渲染取值走 factor
    library.configure(record['id'], slot_settings=('roughness', {'factor': .2}))
    options = slot_options(library.get(record['id'])['preview']['properties'],
                            'roughness', True)
    assert options['amount'] == pytest.approx(.2)


# ── 6. 法线 ─────────────────────────────────────────────────────────────

def test_capability_normal_slot_and_convention_reach_the_render_payload(service, tmp_path):
    library, scene = service
    assert RANGES['normal'] == (0., 4., 1.)
    source = _write_rgb(tmp_path / 'normal.png', color=(128, 128, 255))
    record = library.create(name='法线材质')
    library.configure(record['id'], slot='normal',
                      prepared=prepare_texture(source), channel='RGB')
    library.configure(record['id'], normal_convention='OpenGL')
    stored = library.get(record['id'])
    assert stored['preview']['properties']['normalConvention'] == 'OpenGL'
    library.configure(record['id'], normal_convention='DirectX')
    assert library.get(record['id'])['preview']['properties']['normalConvention'] == 'DirectX'
    with pytest.raises(ValueError, match='法线格式'):
        library.configure(record['id'], normal_convention='OpenGLY')
    payload = _payload(library, record['id'], tmp_path / 'normal')
    assert 'normal' in payload['maps']
    assert payload['properties']['normalConvention'] == 'DirectX'
    # 法线必须是整张 RGB 向量，单通道必须被拒绝而不是被猜
    import copy
    before = copy.deepcopy(library.get(record['id']))
    with pytest.raises(ValueError, match='RGB'):
        library.configure(record['id'], slot='normal', channel='R')
    assert library.get(record['id']) == before
    assert 'normal' in _payload(library, record['id'], tmp_path / 'normal-single')['maps']


# ── 7. AO ───────────────────────────────────────────────────────────────

def test_capability_ao_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    assert RANGES['ao'] == (0., 1., 1.)
    source = tmp_path / 'ao.png'
    Image.new('RGB', (8, 8), (32, 96, 192)).save(source)
    record = library.create(name='AO 材质')
    library.configure(record['id'], slot='ao',
                      prepared=prepare_texture(source), channel='R')
    payload = _payload(library, record['id'], tmp_path / 'ao')
    assert 'ao' in payload['maps']
    with Image.open(_map_path(payload['maps']['ao'])) as ao:
        assert ao.mode == 'L'
        assert ao.getpixel((0, 0)) == 32


# ── 8. 自发光 ───────────────────────────────────────────────────────────

def test_capability_emissive_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    assert RANGES['emissive'] == (0., 20., 0.)
    source = _write_rgb(tmp_path / 'emissive.png', color=(200, 120, 40))
    record = library.create(name='自发光材质')
    library.configure(record['id'], slot='emissive',
                      prepared=prepare_texture(source), channel='RGB')
    payload = _payload(library, record['id'], tmp_path / 'emissive')
    assert 'emissive' in payload['maps']
    # 有自发光贴图时自发光默认强度是 1（而不是 0）
    assert slot_options(payload['properties'], 'emissive', True)['value'] == 1.
    assert slot_options(payload['properties'], 'emissive', False)['value'] == 0.


# ── 9. SSS ──────────────────────────────────────────────────────────────

def test_capability_sss_reaches_the_render_payload(service, tmp_path):
    library, scene = service
    assert RANGES['sss'] == (0., 1., 1.)
    assert 'sss' in SCALARS
    source = _write_gray(tmp_path / 'sss.png', level=200)
    record = library.create(name='SSS 材质')
    library.configure(record['id'], slot='sss',
                      prepared=prepare_texture(source), channel='R')
    payload = _payload(library, record['id'], tmp_path / 'sss')
    assert 'sss' in payload['maps']
    with Image.open(_map_path(payload['maps']['sss'])) as sss:
        assert sss.getpixel((0, 0)) == 200


def test_sss_colour_texture_preserves_rgb_and_does_not_block_preview(service, tmp_path):
    library, scene = service
    source = _write_rgb(tmp_path/'sss-color.png',color=(180,70,25))
    record = library.create(name='colour SSS')
    library.configure(record['id'],slot='sss',prepared=prepare_texture(source),channel='RGB')
    payload = _payload(library,record['id'],tmp_path/'sss-color-render')
    assert payload['sssColor'] is True
    with Image.open(_map_path(payload['maps']['sss'])) as image:
        assert image.getpixel((0,0)) == (180,70,25)


# ── 10. 自定义通道：每个通道一条独立结论 ────────────────────────────────

def test_capability_channel_rgb_picks_the_whole_colour_image(service, tmp_path):
    library, scene = service
    source = tmp_path / 'rgba.png'
    Image.new('RGBA', (8, 8), (30, 120, 200, 255)).save(source)
    blob = source.read_bytes()
    image = selected_image(blob, 'RGB')
    assert image.mode == 'RGB'
    assert image.getpixel((0, 0)) == (30, 120, 200)
    record = library.create(name='通道 RGB')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='RGB')
    payload = _payload(library, record['id'], tmp_path / 'rgb')
    with Image.open(_map_path(payload['maps']['basecolor'])) as base:
        assert base.getpixel((0, 0)) == (30, 120, 200)


def test_capability_channel_r_picks_the_red_channel(service, tmp_path):
    library, scene = service
    source = tmp_path / 'rgba.png'
    Image.new('RGBA', (8, 8), (30, 120, 200, 255)).save(source)
    blob = source.read_bytes()
    image = selected_image(blob, 'R')
    assert image.mode == 'L'
    assert image.getpixel((0, 0)) == 30
    record = library.create(name='通道 R')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='R')
    payload = _payload(library, record['id'], tmp_path / 'r')
    with Image.open(_map_path(payload['maps']['basecolor'])) as base:
        assert base.getpixel((0, 0)) == (30, 30, 30)


def test_capability_channel_g_picks_the_green_channel(service, tmp_path):
    library, scene = service
    source = tmp_path / 'rgba.png'
    Image.new('RGBA', (8, 8), (30, 120, 200, 255)).save(source)
    blob = source.read_bytes()
    image = selected_image(blob, 'G')
    assert image.mode == 'L'
    assert image.getpixel((0, 0)) == 120
    record = library.create(name='通道 G')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='G')
    payload = _payload(library, record['id'], tmp_path / 'g')
    with Image.open(_map_path(payload['maps']['basecolor'])) as base:
        assert base.getpixel((0, 0)) == (120, 120, 120)


def test_capability_channel_b_picks_the_blue_channel(service, tmp_path):
    library, scene = service
    source = tmp_path / 'rgba.png'
    Image.new('RGBA', (8, 8), (30, 120, 200, 255)).save(source)
    blob = source.read_bytes()
    image = selected_image(blob, 'B')
    assert image.mode == 'L'
    assert image.getpixel((0, 0)) == 200
    record = library.create(name='通道 B')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='B')
    payload = _payload(library, record['id'], tmp_path / 'b')
    with Image.open(_map_path(payload['maps']['basecolor'])) as base:
        assert base.getpixel((0, 0)) == (200, 200, 200)


def test_capability_channel_a_picks_the_alpha_channel(service, tmp_path):
    library, scene = service
    source = tmp_path / 'rgba.png'
    Image.new('RGBA', (8, 8), (30, 120, 200, 77)).save(source)
    blob = source.read_bytes()
    image = selected_image(blob, 'A')
    assert image.mode == 'L'
    assert image.getpixel((0, 0)) == 77
    record = library.create(name='通道 A')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(source), channel='A')
    payload = _payload(library, record['id'], tmp_path / 'a')
    with Image.open(_map_path(payload['maps']['basecolor'])) as base:
        assert base.getpixel((0, 0)) == (77, 77, 77)


def test_capability_channel_set_is_exactly_rgb_r_g_b_a():
    assert CHANNELS == ('RGB', 'R', 'G', 'B', 'A')
    assert set(SLOTS) == {'basecolor', 'normal', 'roughness', 'metallic', 'ao',
                           'opacity', 'emissive', 'sss', 'displacement'}
    assert SCALARS == {'roughness', 'metallic', 'ao', 'opacity', 'sss', 'displacement'}


# ── 状态契约：两半在同一个用例里一起断言 ────────────────────────────────

def test_leaving_material_discards_adjustments_but_keeps_bindings_channels_tags_notes_and_originals(service, tmp_path):
    """离开材质 = 丢临时滑块调整，保留绑定/通道/标签/备注/原图。

    两半放在**同一个**用例里：只测一半等于没测契约。
    """
    library, scene = service
    base = _write_rgb(tmp_path / 'asphalt_diff.png', size=32, color=(30, 120, 200))
    normal = _write_rgb(tmp_path / 'asphalt_nor.png', size=32, color=(128, 128, 255))

    record = library.create(name='沥青')
    library.update(record['id'], name='沥青 地面', notes='Poly Haven 沥青材质')
    library.configure(record['id'], slot='basecolor',
                      prepared=prepare_texture(base), channel='RGB')
    library.configure(record['id'], slot='roughness',
                      prepared=prepare_texture(base), channel='G')
    library.configure(record['id'], slot='normal',
                      prepared=prepare_texture(normal), channel='RGB')
    library.configure(record['id'], tags=['Poly Haven', 'asphalt', 'outdoor'])
    library.configure(record['id'], blend_mode='masked', normal_convention='OpenGL')

    # 另一个材质复用同一张原图：attachmentId 会被共享，断言必须按 sha256 比。
    other = library.create(name='沥青 副本')
    library.configure(other['id'], slot='basecolor',
                      prepared=prepare_texture(base), channel='RGB')

    # 临时滑块调整：滑块数值、翻转、裁切阈值
    library.configure(record['id'], slot_settings=('roughness', {'factor': .2, 'invert': True}))
    library.configure(record['id'], slot_settings=('basecolor', {'color': [1., .1, .1], 'value': .4}))
    library.configure(record['id'], properties_patch={'flipX': True, 'flipY': True,
                                                     'alphaCutoff': .3})
    before = library.get(record['id'])
    assert before['preview']['properties']['slots']
    assert before['preview']['properties']['flipX'] is True
    assert before['preview']['properties']['flipY'] is True
    assert before['preview']['properties']['alphaCutoff'] == pytest.approx(.3)

    # ── 离开材质 ──
    after = library.configure(record['id'], reset_adjustments=True)

    # ── 第一半：临时调整被丢弃 ──
    properties = after['preview']['properties']
    assert 'slots' not in properties, '临时滑块调整必须被丢弃'
    assert 'flipX' not in properties, '临时翻转必须被丢弃'
    assert 'flipY' not in properties, '临时翻转必须被丢弃'
    assert 'alphaCutoff' not in properties, '临时裁切阈值必须被丢弃'
    assert after['preview']['discardedAdjustments'] is True

    # ── 第二半：绑定 / 通道 / 标签 / 备注 / 原图 逐字段不变 ──
    # 1) 槽位绑定
    assert set(after['textures']) == set(before['textures']) == {'basecolor', 'roughness', 'normal'}
    for slot in ('basecolor', 'roughness', 'normal'):
        assert after['textures'][slot]['attachmentId'] == before['textures'][slot]['attachmentId']
        assert after['textures'][slot]['sha256'] == before['textures'][slot]['sha256']
        assert after['textures'][slot]['name'] == before['textures'][slot]['name']
    # 2) 通道
    assert after['textures']['basecolor']['channel'] == 'RGB'
    assert after['textures']['roughness']['channel'] == 'G'
    assert after['textures']['normal']['channel'] == 'RGB'
    # 3) 标签
    assert after['tags'] == before['tags'] == ['Poly Haven', 'asphalt', 'outdoor']
    # 4) 备注
    assert after['notes'] == before['notes'] == 'Poly Haven 沥青材质'
    # 5) 原图附件：按 sha256 逐份比对，不比数量
    assert len(after['files']) == len(before['files'])
    before_by_sha = {f['sha256']: f for f in before['files']}
    after_by_sha = {f['sha256']: f for f in after['files']}
    assert set(before_by_sha) == set(after_by_sha)
    for sha, original in before_by_sha.items():
        kept = after_by_sha[sha]
        assert kept['attachmentId'] == original['attachmentId']
        assert kept['name'] == original['name']
        assert kept['sha256'] == original['sha256']
        assert kept['size'] == original['size']
        blob = scene.attachments[original['attachmentId']][2]
        assert hashlib.sha256(blob).hexdigest() == sha, '原图字节必须仍然完好'
    # 共享 attachmentId 的另一个材质没有被牵连
    sibling = library.get(other['id'])
    assert sibling['textures']['basecolor']['attachmentId'] == \
        after['textures']['basecolor']['attachmentId']
    assert sibling['textures']['basecolor']['channel'] == 'RGB'
    # 耐久设置（透明方式 / 法线格式）不属于临时调整，必须保留
    assert after['preview']['properties']['blendMode'] == 'masked'
    assert after['preview']['properties']['normalConvention'] == 'OpenGL'


def test_notes_survive_switching_away_from_the_material_page(main_window, qtbot):
    """备注是材质状态契约的一半：离开材质必须留住。"""
    window = main_window
    window.show()
    window.open_material_library()
    page = window.material_library
    page.create_material()
    record_id = page.selected_id
    page.details.notes.setPlainText('离开材质也要留住备注')
    window.tabs.setCurrentWidget(window.canvas_page)
    # Navigation commits asynchronously; require completion, not synchronous timing.
    qtbot.waitUntil(lambda: window.tabs.currentWidget() is window.canvas_page)

    assert page.records.get(record_id)['notes'] == '离开材质也要留住备注'


# ── 像素指纹 / 退化判定 ────────────────────────────────────────────────

def _png(size, color, quad=None):
    image = Image.new('RGB', (size, size), color)
    if quad:
        for x in range(size // 2):
            for y in range(size // 2):
                image.putpixel((x, y), quad)
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def _gradient_png(size):
    """A tile in here carries many colours, so it is not a 纯色块."""
    image = Image.linear_gradient('L').resize((size, size)).convert('RGB')
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def test_pixel_fingerprint_distinguishes_the_same_image_rendered_differently():
    base = _png(64, (40, 90, 160))
    other = _png(64, (40, 90, 160), quad=(200, 30, 30))
    first = baseline_tool.analyze_png(base)
    second = baseline_tool.analyze_png(other)
    assert first['pixel_sha256'] != second['pixel_sha256']
    assert first['fingerprint'] != second['fingerprint']


def test_pixel_fingerprint_distinguishes_different_images():
    first = baseline_tool.analyze_png(_png(64, (40, 90, 160)))
    second = baseline_tool.analyze_png(_png(64, (160, 90, 40)))
    assert first['pixel_sha256'] != second['pixel_sha256']
    assert first['fingerprint'] != second['fingerprint']


def test_pixel_fingerprint_is_stable_for_identical_bytes():
    blob = _png(64, (40, 90, 160))
    assert baseline_tool.analyze_png(blob)['fingerprint'] == \
        baseline_tool.analyze_png(blob)['fingerprint']


def test_flat_colour_block_is_flagged_as_degraded():
    flat = baseline_tool.analyze_png(_png(64, (40, 90, 160)))
    assert flat['non_flat_block_ratio'] == pytest.approx(0.0)
    assert flat['degraded'] is True
    # 一张每个块里都有很多颜色的图不算纯色块
    textured = baseline_tool.analyze_png(_gradient_png(64))
    assert textured['non_flat_block_ratio'] > 0.0
    assert textured['degraded'] is False
    # 由若干纯色块拼起来的图**仍然是**纯色块 —— 退化检测不能被拼图骗过
    patchwork = baseline_tool.analyze_png(_png(64, (40, 90, 160), quad=(200, 30, 30)))
    assert patchwork['non_flat_block_ratio'] == pytest.approx(0.0)
    assert patchwork['degraded'] is True


def test_timing_stats_use_the_scale_acceptance_shape():
    stats = baseline_tool.stats([10.0, 20.0, 30.0, 40.0])
    assert set(stats) == {'count', 'median_ms', 'p95_ms', 'max_ms'}
    assert stats['count'] == 4
    assert stats['median_ms'] == 25.0
    assert stats['max_ms'] == 40.0
    assert baseline_tool.stats([]) == {}
