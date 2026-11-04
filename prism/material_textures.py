"""Original texture storage and deterministic render inputs (never modify originals)."""
import hashlib
import io
import json
import mimetypes
from pathlib import Path

from PIL import Image, ImageChops, ImageOps
from prism.material_parameters import slot_options, CHANNELS
from prism.performance_trace import measured
from prism.library_observability import observed, event as library_event


SLOTS = {
    'basecolor': '颜色', 'normal': '法线', 'roughness': '粗糙度',
    'metallic': '金属度', 'ao': 'AO', 'opacity': '透明遮罩 / 透明度',
    'emissive': '自发光', 'sss': 'SSS', 'displacement': '置换',
}
SCALARS = {'roughness', 'metallic', 'ao', 'opacity', 'sss', 'displacement'}


def read_original(attachments, key):
    """Use the existing project's lazy storage; don't pin every viewed original."""
    reader = getattr(attachments, 'read_uncached', attachments.get)
    return reader(key)


def display_rgba(original):
    """Normalize unsigned 16-bit scalar previews; keep source bytes untouched.

    Pillow's direct I;16 -> RGBA conversion clips values above 255 rather than
    mapping the 0..65535 range, making ordinary displacement maps turn white.
    """
    if original.mode.startswith('I;16') or (original.mode == 'I' and original.format == 'PNG'):
        return original.convert('I').point(lambda value: value/257).convert('L').convert('RGBA')
    return original.convert('RGBA')


class DecodedTextureCache:
    """Bounded content-keyed source decode cache; callers receive owned copies."""
    def __init__(self, budget=64 * 1024 * 1024, max_entries=128):
        from collections import OrderedDict
        import threading
        self.budget = budget
        self.max_entries = max_entries
        self.bytes = 0
        self.entries = OrderedDict()
        self.lock = threading.RLock()

    def open(self, blob):
        key = hashlib.sha256(blob).digest()
        with self.lock:
            image = self.entries.get(key)
            if image is None:
                image = Image.open(io.BytesIO(blob))
                try:
                    image.load()
                except BaseException:
                    image.close()
                    raise
                # Conservative decoded-pixel residency estimate, not process RSS.
                size = image.width * image.height * max(4, len(image.getbands()) * 4)
                if size <= self.budget and self.max_entries > 0:
                    while self.entries and (self.bytes + size > self.budget or len(self.entries) >= self.max_entries):
                        _, victim = self.entries.popitem(last=False)
                        self.bytes -= victim.width * victim.height * max(4, len(victim.getbands()) * 4)
                        victim.close()
                    self.entries[key] = image
                    self.bytes += size
                else:
                    return image  # caller owns oversize decode; never cached
            else:
                self.entries.move_to_end(key)
            result = image.copy()
            result.format = image.format
            result.info = dict(image.info)
            return result

    def clear(self):
        with self.lock:
            for image in self.entries.values():
                image.close()
            self.entries.clear()
            self.bytes = 0


_DECODED_TEXTURES = DecodedTextureCache()


def open_decoded_texture(blob):
    """Decode unchanged originals once; caller closes its independent image."""
    return _DECODED_TEXTURES.open(blob)


@measured('materials.texture.inspect')
@observed('material','texture_read_decode')
def prepare_texture(path):
    path = Path(path)
    blob = path.read_bytes()
    with open_decoded_texture(blob) as original:
        image = display_rgba(original)
        bands = image.split()
        extrema = image.getextrema()
        grayscale = (ImageChops.difference(bands[0], bands[1]).getbbox() is None
                     and ImageChops.difference(bands[0], bands[2]).getbbox() is None)
        info = dict(name=path.name, width=image.width, height=image.height,
                    size=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
                    mime=Image.MIME.get(original.format) or mimetypes.guess_type(path.name)[0]
                    or 'application/octet-stream',
                    grayscale=grayscale, hasAlpha='A' in original.getbands()
                    or 'transparency' in original.info,
                    extrema=extrema)
    library_event('material','texture_evidence','verified',sha256=info['sha256'],
                  size_bytes=len(blob),width=info['width'],height=info['height'])
    return info, blob


def select_decoded_channel(image, channel='RGB', edge=2048):
    if channel not in CHANNELS:
        raise ValueError('Invalid texture channel')
    selected = image.convert('RGB') if channel == 'RGB' else image.getchannel(channel)
    selected.thumbnail((edge, edge), Image.Resampling.LANCZOS)
    return selected


def selected_image(blob, channel='RGB', edge=2048):
    with open_decoded_texture(blob) as original:
        image = display_rgba(original)
    try:
        return select_decoded_channel(image, channel, edge)
    finally:
        image.close()


def png_bytes(image):
    stream = io.BytesIO()
    image.save(stream, 'PNG')
    return stream.getvalue()


def transform_image(image, slot, properties):
    options = slot_options(properties, slot, True)
    flip_x = bool(properties.get('flipX', False)) != options['flipX']
    flip_y = bool(properties.get('flipY', False)) != options['flipY']
    if flip_x:
        image = ImageOps.mirror(image)
    if flip_y:
        image = ImageOps.flip(image)
    if slot == 'normal' and (flip_x or flip_y) and image.mode == 'RGB':
        r, g, b = image.split()
        image = Image.merge('RGB', (ImageOps.invert(r) if flip_x else r,
                                   ImageOps.invert(g) if flip_y else g, b))
    if options['invert'] and slot == 'normal' and image.mode == 'RGB':
        r, g, b = image.split()
        return Image.merge('RGB', (r, ImageOps.invert(g), b))
    return ImageOps.invert(image) if options['invert'] else image


def render_signature(record):
    from prism.material_preview_profile import profile_signature
    data = dict(textures=record.get('textures', {}),
                properties=record.get('preview', {}).get('properties', {}),
                preview_profile=profile_signature())
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def texture_signature(record):
    props = record.get('preview', {}).get('properties', {})
    transforms = {key: {k: slot_options(props, key)[k]
                        for k in ('enabled', 'invert', 'flipX', 'flipY')} for key in SLOTS}
    data = [record.get('textures', {}), transforms,
            {k: props.get(k, False) for k in ('flipX', 'flipY', 'ignoreBaseAlpha')}]
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def resolved_properties(properties, maps, detected, reason):
    result = json.loads(json.dumps(properties))
    requested = result.get('blendMode', 'auto')
    from prism.material_parameters import resolved_controls, normal_convention
    result['controls'] = resolved_controls(result, maps)
    result['normalConvention'] = normal_convention(result)
    result['sourceAutoBlendMode'] = detected
    result['sourceAutoBlendReason'] = reason
    if result['controls']['opacity']['enabled'] and result['controls']['opacity']['amount'] < 1:
        detected, reason = 'translucent', '透明度强度小于 1'
    result['blendMode'] = detected if requested == 'auto' else requested
    result['autoBlendMode'] = detected
    result['autoBlendReason'] = reason
    return result


def payload_for_record(cached, record):
    """Reuse prepared texture bytes while resolving the latest scalar controls."""
    import copy
    payload = copy.deepcopy(cached)
    old = payload['properties']
    payload['properties'] = resolved_properties(record.get('preview', {}).get('properties', {}),
        payload['maps'], old['sourceAutoBlendMode'], old.get('sourceAutoBlendReason', ''))
    payload['signature'] = render_signature(record)
    payload['recordId'] = record.get('id')
    return payload


@measured('materials.texture.prepare_render')
@observed('material','render_prepare')
def prepare_render(record, attachments, directory):
    """Prepare linear scalar maps, sRGB color maps and standard packed MR inputs."""
    from contextlib import ExitStack
    with ExitStack() as cleanup:
        return _prepare_render_sources(record, attachments, directory, cleanup)


def _prepare_render_sources(record, attachments, directory, cleanup):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    maps, images, sources = {}, {}, {}
    digests, decoded = {}, {}
    properties = json.loads(json.dumps(record.get('preview', {}).get('properties', {})))
    from collections import Counter
    remaining = Counter(binding.get('sha256') for slot, binding in record.get('textures', {}).items()
        if slot in SLOTS and isinstance(binding, dict) and slot_options(properties, slot, True)['enabled'])

    for slot, binding in record.get('textures', {}).items():
        if slot not in SLOTS or not isinstance(binding, dict):
            continue
        options = slot_options(properties, slot, True)
        if not options['enabled']:
            continue
        source_id = binding.get('attachmentId')
        if source_id not in sources:
            sources[source_id] = read_original(attachments, source_id)
            entry = sources[source_id]
            if entry and isinstance(entry[2], bytes):
                digests[source_id] = hashlib.sha256(entry[2]).hexdigest()
        entry = sources[source_id]
        if not entry or not isinstance(entry[2], bytes):
            raise ValueError('原始贴图缺失：' + binding.get('name', slot))
        if digests[source_id] != binding['sha256']:
            raise ValueError('原始贴图校验失败：' + binding['name'])
        if slot == 'normal' and binding['channel'] != 'RGB':
            raise ValueError('法线需要完整 RGB 向量，请选择整张 / RGB')
        digest = digests[source_id]
        if digest not in decoded:
            with open_decoded_texture(entry[2]) as original:
                decoded[digest] = display_rgba(original)
            cleanup.callback(decoded[digest].close)
        image = select_decoded_channel(decoded[digest], binding['channel'])
        if (slot == 'basecolor' and 'opacity' not in record.get('textures', {})
                and slot_options(properties, 'opacity')['enabled'] and not properties.get('ignoreBaseAlpha')):
            alpha = select_decoded_channel(decoded[digest], 'A')
            if alpha.getextrema() != (255, 255):
                images['opacity'] = alpha
        remaining[digest] -= 1
        if remaining[digest] == 0:
            decoded.pop(digest).close()  # last consumer: free full-size source now
        colour_sss = slot == 'sss' and binding.get('channel') == 'RGB' and not binding.get('grayscale')
        if slot in SCALARS and not colour_sss:
            if image.mode != 'L':
                if not binding.get('grayscale'):
                    raise ValueError(SLOTS[slot] + '包含不同的 RGB 内容，请选择 R/G/B/A 通道')
                image = image.getchannel('R')
        elif image.mode == 'L':
            image = image.convert('RGB')
        image = transform_image(image, slot, properties)
        images[slot] = image
        path = directory / (slot + '.png')
        image.save(path)
        maps[slot] = path.as_uri()
    if 'opacity' in images and 'opacity' not in maps:
        alpha = images['opacity']
        base_options = slot_options(properties, 'basecolor', True)
        if bool(properties.get('flipX')) != base_options['flipX']:
            alpha = ImageOps.mirror(alpha)
        if bool(properties.get('flipY')) != base_options['flipY']:
            alpha = ImageOps.flip(alpha)
        images['opacity'] = alpha
        path = directory / 'opacity.png'
        alpha.save(path)
        maps['opacity'] = path.as_uri()
    detected, reason = classify_transparency(images.get('opacity'))
    properties = resolved_properties(properties, maps, detected, reason)
    # Babylon PBR metallicTexture: G roughness, B metallic. Both linear.
    if 'roughness' in images or 'metallic' in images:
        size = (max(images[k].width for k in ('roughness', 'metallic') if k in images),
                max(images[k].height for k in ('roughness', 'metallic') if k in images))
        def scalar(key, default):
            return images[key].resize(size, Image.Resampling.LANCZOS) if key in images else Image.new('L', size, default)
        packed = Image.merge('RGB', (Image.new('L', size, 255), scalar('roughness', 255), scalar('metallic', 255)))
        path = directory / 'metallicRoughness.png'
        packed.save(path)
        maps['metallicRoughness'] = path.as_uri()
    return dict(maps=maps, properties=properties,
                sssColor=bool('sss' in images and images['sss'].mode == 'RGB'),
                textureSignature=texture_signature(record), recordId=record.get('id'),
                signature=render_signature(record))


def classify_transparency(image):
    if image is None:
        return 'opaque', '没有有效透明输入'
    histogram = image.convert('L').histogram()
    total = sum(histogram)
    if sum(histogram[254:]) == total:
        return 'opaque', '透明输入完全不透明'
    middle = sum(histogram[3:253])
    if middle / total <= .08:
        return 'masked', '透明输入以黑白遮罩为主（允许边缘灰阶）'
    # Gray antialiasing confined to the boundary of a binary mask should not
    # turn foliage into blended glass. Test a reduced image's local extrema.
    sample = image.copy()
    sample.thumbnail((512, 512))
    import numpy as np
    values = np.asarray(sample.convert('L'))
    mid = (values > 2) & (values < 253)
    from PIL import ImageFilter
    minimum = np.asarray(sample.filter(ImageFilter.MinFilter(5)))
    maximum = np.asarray(sample.filter(ImageFilter.MaxFilter(5)))
    boundary = (minimum <= 2) & (maximum >= 253)
    if mid.any() and (mid & boundary).sum() / mid.sum() > .85:
        return 'masked', '灰阶主要位于遮罩轮廓边缘'
    return 'translucent', '透明输入存在连续灰阶；可在高级设置纠正'


@observed('material','original_export')
def export_originals(record, attachments, directory):
    """Exclusive output creation: never overwrite another asset or follow filenames."""
    root = Path(directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    outputs = []
    used = set()
    for file in record.get('files', []):
        name = file['name']
        if Path(name).name != name or any(c in name for c in '\\/:\x00') or name in ('.', '..'):
            raise ValueError('原始文件名无效')
        if name.casefold() in used:
            name = file['sha256'][:12] + '_' + name
        used.add(name.casefold())
        target = root / name
        if target.exists():
            raise ValueError('目标文件已存在：' + name)
        entry = read_original(attachments, file['attachmentId'])
        if not entry or not isinstance(entry[2], bytes) or hashlib.sha256(entry[2]).hexdigest() != file['sha256']:
            raise ValueError('原始文件缺失或校验失败：' + file['name'])
        outputs.append((target, entry[2]))
    created = []
    try:
        for target, blob in outputs:
            with target.open('xb') as stream:
                created.append(target)
                stream.write(blob)
    except Exception:
        for target in created:
            target.unlink()
        raise
    return [str(target) for target, _ in outputs]
