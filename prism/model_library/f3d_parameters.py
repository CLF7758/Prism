"""Prism appearance → F3D parameters, with derived preview maps.

The rule this module exists to protect: **the F3D option names are the
existing material semantics, renamed only at the boundary**. Prism stores
slots (``basecolor``, ``normal``, ``roughness``, ``metallic``, ``ao``,
``opacity``, ``emissive``) with the options defined by
:mod:`prism.material_parameters`; F3D renders ``model.color.texture``,
``model.normal.texture`` and the packed ORM in ``model.material.texture``.
There is no second set of business parameters here.

Three behaviours are deliberate:

* **No override means no override.** With no enabled slot the snapshot is
  empty and not a single ``model.*`` option is set, so the model keeps its
  imported multi-material and textures.
* **One multiply, never two.** A strength is either baked into the derived
  channel or passed as an F3D multiplier, never both.
* **The original bytes are never written.** Derived preview maps go into a
  scratch directory under names derived from their content hash; a repeated
  snapshot reuses an existing file instead of touching the source.

``model.textures_transform`` cannot be set through the F3D Python binding in
the verified build, so the per-slot and global flips are baked into the
derived maps with the existing :func:`prism.material_textures.transform_image`
semantics. Normal maps are flipped green for DirectX sources through the same
slot ``invert`` option Prism already shows.
"""
from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from prism.material_parameters import RANGES, slot_options

#: Bumped when the meaning of a snapshot field changes.
PARAMETER_SCHEMA_VERSION = 1

#: Slots the native runtime maps to a real F3D parameter.
SUPPORTED_SLOTS = ('basecolor', 'normal', 'roughness', 'metallic', 'ao',
                   'opacity', 'emissive')

#: Slots with no verified F3D equivalent. They stay visible in the inspector
#: and are reported, never silently applied.
UNSUPPORTED_SLOTS = {
    'sss': 'F3D 4.0.0 没有次表面散射参数，预览不会假装生效',
    'displacement': 'F3D 4.0.0 没有置换参数，预览不会假装生效',
}

#: Channel order of the derived ORM map, checked channel by channel on the
#: real runtime: R occlusion, G roughness, B metallic (glTF convention).
ORM_CHANNELS = ('ao', 'roughness', 'metallic')

#: F3D blending modes that are valid for a translucent preview.
BLEND_DDP = 'ddp'
BLEND_NONE = 'none'


class AppearanceError(ValueError):
    """An appearance could not be turned into a parameter snapshot."""


@dataclass(frozen=True)
class ParameterSnapshot:
    """A detached, JSON-safe description of what the native side must render.

    Every path is a derived preview file inside the caller's scratch
    directory; no project original is referenced and nothing here is mutable.
    """

    revision: int = 0
    has_override: bool = False
    color_texture: Optional[str] = None
    color_rgb: Optional[tuple] = None
    color_opacity: Optional[float] = None
    normal_texture: Optional[str] = None
    normal_scale: Optional[float] = None
    emissive_texture: Optional[str] = None
    emissive_factor: Optional[tuple] = None
    orm_texture: Optional[str] = None
    material_roughness: Optional[float] = None
    material_metallic: Optional[float] = None
    blending_mode: str = BLEND_NONE
    translucent: bool = False
    derived: tuple = ()
    warnings: tuple = ()
    unsupported: tuple = ()
    signature: str = ''
    schema: int = PARAMETER_SCHEMA_VERSION

    # -- transport -----------------------------------------------------
    def to_payload(self) -> dict:
        return {
            'schema': self.schema,
            'revision': int(self.revision),
            'has_override': bool(self.has_override),
            'color_texture': self.color_texture,
            'color_rgb': list(self.color_rgb) if self.color_rgb else None,
            'color_opacity': self.color_opacity,
            'normal_texture': self.normal_texture,
            'normal_scale': self.normal_scale,
            'emissive_texture': self.emissive_texture,
            'emissive_factor': list(self.emissive_factor) if self.emissive_factor else None,
            'orm_texture': self.orm_texture,
            'material_roughness': self.material_roughness,
            'material_metallic': self.material_metallic,
            'blending_mode': self.blending_mode,
            'translucent': bool(self.translucent),
            'derived': [list(item) for item in self.derived],
            'warnings': list(self.warnings),
            'unsupported': list(self.unsupported),
            'signature': self.signature,
        }

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]]) -> 'ParameterSnapshot':
        if not payload:
            return cls()
        return cls(
            revision=int(payload.get('revision') or 0),
            has_override=bool(payload.get('has_override')),
            color_texture=_opt_text(payload.get('color_texture')),
            color_rgb=_opt_tuple(payload.get('color_rgb')),
            color_opacity=_opt_float(payload.get('color_opacity')),
            normal_texture=_opt_text(payload.get('normal_texture')),
            normal_scale=_opt_float(payload.get('normal_scale')),
            emissive_texture=_opt_text(payload.get('emissive_texture')),
            emissive_factor=_opt_tuple(payload.get('emissive_factor')),
            orm_texture=_opt_text(payload.get('orm_texture')),
            material_roughness=_opt_float(payload.get('material_roughness')),
            material_metallic=_opt_float(payload.get('material_metallic')),
            blending_mode=str(payload.get('blending_mode') or BLEND_NONE),
            translucent=bool(payload.get('translucent')),
            derived=tuple(tuple(item) for item in (payload.get('derived') or ())),
            warnings=tuple(str(item) for item in (payload.get('warnings') or ())),
            unsupported=tuple(str(item) for item in (payload.get('unsupported') or ())),
            signature=str(payload.get('signature') or ''),
            schema=int(payload.get('schema') or PARAMETER_SCHEMA_VERSION),
        )

    # -- F3D binding ---------------------------------------------------
    def option_values(self) -> dict:
        """The F3D options this snapshot really sets.

        An option is present only when the snapshot carries a value for it, so
        "no override" cannot accidentally replace the imported materials.
        """
        values: dict = {}
        if self.color_texture:
            values['model.color.texture'] = self.color_texture
        if self.color_rgb:
            values['model.color.rgb'] = [float(c) for c in self.color_rgb]
        if self.color_opacity is not None:
            values['model.color.opacity'] = float(self.color_opacity)
        if self.normal_texture:
            values['model.normal.texture'] = self.normal_texture
        if self.normal_scale is not None:
            values['model.normal.scale'] = float(self.normal_scale)
        if self.emissive_texture:
            values['model.emissive.texture'] = self.emissive_texture
        if self.emissive_factor:
            values['model.emissive.factor'] = [float(c) for c in self.emissive_factor]
        if self.orm_texture:
            values['model.material.texture'] = self.orm_texture
        if self.material_roughness is not None:
            values['model.material.roughness'] = float(self.material_roughness)
        if self.material_metallic is not None:
            values['model.material.metallic'] = float(self.material_metallic)
        if self.translucent:
            values['render.effect.blending.mode'] = BLEND_DDP
        return values

    def option_names(self) -> tuple:
        return tuple(sorted(self.option_values()))


# ── reading the appearance ────────────────────────────────────────────────
def appearance_bindings(appearance: Any) -> dict:
    bindings = getattr(appearance, 'bindings', None)
    return dict(bindings) if isinstance(bindings, Mapping) else {}


def appearance_properties(appearance: Any) -> dict:
    properties = getattr(appearance, 'properties', None)
    return json.loads(json.dumps(dict(properties))) if isinstance(properties, Mapping) else {}


def enabled_slots(appearance: Any) -> tuple:
    """Slots whose control is switched on, in the existing slot order."""
    properties = appearance_properties(appearance)
    bindings = appearance_bindings(appearance)
    slots = []
    for slot in SUPPORTED_SLOTS:
        options = slot_options(properties, slot, bool(bindings.get(slot)))
        if options.get('enabled', True):
            slots.append(slot)
    return tuple(slots)


def _stored_slot(appearance: Any, slot: str) -> Mapping:
    properties = appearance_properties(appearance)
    stored = properties.get('slots')
    if not isinstance(stored, Mapping):
        return {}
    entry = stored.get(slot)
    return entry if isinstance(entry, Mapping) else {}


def has_override(appearance: Any) -> bool:
    """True when the appearance changes something the native side must apply.

    A slot that was merely explored and left at its stored default is not an
    override: it must not repaint a model that imports its own materials.
    """
    if appearance is None:
        return False
    bindings = appearance_bindings(appearance)
    if any(isinstance(binding, Mapping) for binding in bindings.values()):
        return True
    properties = appearance_properties(appearance)
    stored = properties.get('slots')
    if not isinstance(stored, Mapping):
        return False
    for slot, options in stored.items():
        if slot in UNSUPPORTED_SLOTS or slot not in SUPPORTED_SLOTS:
            continue
        if not isinstance(options, Mapping):
            continue
        defaults = slot_options({}, slot, False)
        if options.get('enabled') is False:
            continue
        for key in ('value', 'factor', 'color', 'midpoint'):
            if key in options and options[key] != defaults.get(key):
                return True
    return False


# ── building the snapshot ─────────────────────────────────────────────────
def build_snapshot(appearance: Any, read_texture: Optional[Callable[[Mapping], bytes]],
                   workdir: Path, *, revision: Optional[int] = None) -> ParameterSnapshot:
    """Turn one appearance into a render-ready snapshot.

    ``read_texture`` receives the stored binding and returns the original
    bytes; it is only called for slots that carry a binding. ``workdir`` is a
    scratch directory the caller owns (a derived cache, never the project).
    """
    properties = appearance_properties(appearance)
    bindings = appearance_bindings(appearance)
    revision = int(getattr(appearance, 'revision', 0) or 0) if revision is None else int(revision)
    warnings: list = []
    unsupported: list = []
    for slot, reason in UNSUPPORTED_SLOTS.items():
        options = slot_options(properties, slot, bool(bindings.get(slot)))
        if bindings.get(slot) or options.get('value') != RANGES[slot][2]:
            unsupported.append('%s: %s' % (slot, reason))

    if not has_override(appearance):
        return ParameterSnapshot(revision=revision, has_override=False,
                                 warnings=tuple(warnings),
                                 unsupported=tuple(unsupported),
                                 signature=_signature({}, revision, unsupported))

    if read_texture is None and bindings:
        raise AppearanceError('缺少贴图读取器，无法准备预览贴图')
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    images: dict = {}
    derived: list = []

    for slot in SUPPORTED_SLOTS:
        binding = bindings.get(slot)
        options = slot_options(properties, slot, bool(binding))
        if not options.get('enabled', True):
            continue
        if not binding:
            continue
        if not isinstance(binding, Mapping):
            raise AppearanceError('贴图绑定格式无效：%s' % slot)
        if slot in UNSUPPORTED_SLOTS:
            continue
        blob = read_texture(binding)
        if not isinstance(blob, (bytes, bytearray)):
            raise AppearanceError('原始贴图缺失：%s' % _binding_name(binding, slot))
        expected = str(binding.get('sha256') or '')
        if expected and hashlib.sha256(bytes(blob)).hexdigest() != expected:
            raise AppearanceError('原始贴图校验失败：%s' % _binding_name(binding, slot))
        image = _prepare_image(bytes(blob), slot, binding, properties)
        images[slot] = image
        derived.append((slot, str(binding.get('sha256') or '')[:16]))

    color_texture, opacity, translucent, alpha_note = _compose_color(images, properties, workdir)
    if alpha_note:
        warnings.append(alpha_note)
    normal_texture, normal_scale = _compose_normal(images, properties, workdir)
    emissive_texture, emissive_factor = _compose_emissive(images, properties, workdir)
    orm_texture, roughness_option, metallic_option = _compose_orm(
        images, properties, workdir, warnings)

    color_rgb = None
    base_options = slot_options(properties, 'basecolor', bool(images.get('basecolor')))
    if _color_is_override(base_options):
        color_rgb = tuple(round(float(c), 6) for c in base_options.get('color') or (1.0, 1.0, 1.0))

    snapshot = ParameterSnapshot(
        revision=revision,
        has_override=True,
        color_texture=color_texture,
        color_rgb=color_rgb,
        color_opacity=opacity,
        normal_texture=normal_texture,
        normal_scale=normal_scale,
        emissive_texture=emissive_texture,
        emissive_factor=emissive_factor,
        orm_texture=orm_texture,
        material_roughness=roughness_option,
        material_metallic=metallic_option,
        blending_mode=BLEND_DDP if translucent else BLEND_NONE,
        translucent=translucent,
        derived=tuple(derived),
        warnings=tuple(warnings),
        unsupported=tuple(unsupported),
    )
    signature = _signature(snapshot.option_values(), revision, unsupported)
    return replace(snapshot, signature=signature)


def _prepare_image(blob: bytes, slot: str, binding: Mapping, properties: Mapping):
    from prism.material_textures import SCALARS, selected_image, transform_image

    channel = str(binding.get('channel') or 'RGB')
    image = selected_image(blob, channel)
    if slot == 'normal' and channel != 'RGB':
        raise AppearanceError('法线需要完整 RGB 向量，请选择整张 / RGB')
    if slot in SCALARS:
        if image.mode != 'L':
            if not binding.get('grayscale'):
                raise AppearanceError('%s 包含不同的 RGB 内容，请选择 R/G/B/A 通道' % slot)
            image = image.getchannel('R')
    elif image.mode == 'L':
        image = image.convert('RGB')
    return transform_image(image, slot, properties)


def _compose_color(images: Mapping, properties: Mapping, workdir: Path):
    """Base colour plus the alpha of the opacity slot, as one preview map."""
    from PIL import Image

    base = images.get('basecolor')
    opacity_image = images.get('opacity')
    opacity_options = slot_options(properties, 'opacity', bool(opacity_image))
    opacity_value = float(opacity_options.get('value') if opacity_options.get('value')
                          is not None else 1.0)
    if opacity_value > 1.0:
        opacity_value = 1.0
    note = ''
    translucent = False
    if base is None:
        # No colour map. A pure opacity strength still applies to the imported
        # material, but a per-pixel alpha mask needs a colour map to live in:
        # repainting the model white just to carry alpha would be a lie.
        if opacity_image is not None:
            note = '透明贴图需要同时设置颜色贴图才能在预览中合成，本次只应用透明度数值'
        if opacity_value < 1.0:
            return None, round(opacity_value, 6), True, note
        return None, None, False, note
    if base.mode != 'RGB':
        base = base.convert('RGB')
    if opacity_image is not None:
        alpha = opacity_image
        if alpha.size != base.size:
            alpha = alpha.resize(base.size, Image.Resampling.LANCZOS)
        if opacity_value < 1.0:
            alpha = alpha.point(lambda value: int(round(value * opacity_value)))
        base = Image.merge('RGBA', (*base.split(), alpha))
        translucent = True
    elif opacity_value < 1.0:
        base = Image.merge('RGBA', (*base.split(),
                                    Image.new('L', base.size, int(round(opacity_value * 255)))))
        translucent = True
    path = _write_map(workdir, 'basecolor', base)
    return str(path), None, translucent, note


def _compose_normal(images: Mapping, properties: Mapping, workdir: Path):
    normal = images.get('normal')
    if normal is None:
        return None, None
    options = slot_options(properties, 'normal', True)
    scale = float(options.get('value') if options.get('value') is not None else 1.0)
    path = _write_map(workdir, 'normal', normal.convert('RGB'))
    return str(path), round(scale, 6)


def _compose_emissive(images: Mapping, properties: Mapping, workdir: Path):
    emissive = images.get('emissive')
    if emissive is None:
        return None, None
    options = slot_options(properties, 'emissive', True)
    strength = float(options.get('value') if options.get('value') is not None else 1.0)
    path = _write_map(workdir, 'emissive', emissive.convert('RGB'))
    return str(path), (round(strength, 6),) * 3


def _compose_orm(images: Mapping, properties: Mapping, workdir: Path, warnings: list):
    """Pack occlusion/roughness/metallic into one map, exactly one multiply.

    Measured on the pinned runtime (see the B2 evidence table):

    * R is occlusion and multiplies the lighting directly, so a strength has to
      be baked into R — F3D offers no occlusion multiplier.
    * G is roughness and B is metallic. Setting ``model.material.roughness`` /
      ``metallic`` **replaces** the texture channel instead of scaling it, so a
      map plus a multiplier would silently throw the map detail away. The
      strength is therefore baked into the channel and the multiplier stays
      unset whenever a map exists.
    * With no map at all the multiplier is the exact path (no 8-bit
      quantisation), so it is used there and nowhere else. A slot left at its
      stored default stays unwritten, because writing a default-valued
      multiplier already changes the shading.
    """
    from PIL import Image

    options = {slot: slot_options(properties, slot, bool(images.get(slot)))
               for slot in ORM_CHANNELS}
    strengths = {}
    for slot in ORM_CHANNELS:
        entry = options[slot]
        amount = entry.get('amount')
        strengths[slot] = float(amount if amount is not None else 1.0)
    has_any_map = any(images.get(slot) is not None for slot in ORM_CHANNELS)
    if not has_any_map:
        # No ORM map: the multipliers carry the strengths, and only the slots
        # the user really touched are written.
        touched = _touched_slots(properties, ORM_CHANNELS)
        roughness = round(strengths['roughness'], 6) if 'roughness' in touched else None
        metallic = round(strengths['metallic'], 6) if 'metallic' in touched else None
        return None, roughness, metallic

    size = max(images[slot].size[0] for slot in ORM_CHANNELS if images.get(slot) is not None), \
        max(images[slot].size[1] for slot in ORM_CHANNELS if images.get(slot) is not None)

    def channel(slot, neutral):
        image = images.get(slot)
        if image is None:
            return Image.new('L', size, int(round(max(0.0, min(1.0, strengths[slot])) * 255))
                             if slot in _touched_slots(properties, ORM_CHANNELS)
                             else neutral)
        if image.size != size:
            image = image.resize(size, Image.Resampling.LANCZOS)
        if image.mode != 'L':
            image = image.getchannel('R')
        strength = strengths[slot]
        if abs(strength - 1.0) > 1e-9:
            image = image.point(lambda value: int(round(value * strength)))
        return image

    packed = Image.merge('RGB', (channel('ao', 255), channel('roughness', 255),
                                 channel('metallic', 0)))
    path = _write_map(workdir, 'orm', packed)
    # The strengths are inside the map: a multiplier here would drop the map.
    return str(path), None, None


def _touched_slots(properties: Mapping, slots) -> tuple:
    """Slots whose stored options are not the documented defaults."""
    stored = properties.get('slots') if isinstance(properties, Mapping) else None
    stored = stored if isinstance(stored, Mapping) else {}
    touched = []
    for slot in slots:
        entry = stored.get(slot)
        if not isinstance(entry, Mapping):
            continue
        defaults = slot_options({}, slot, False)
        for key in ('value', 'factor'):
            if key in entry and entry[key] != defaults.get(key):
                touched.append(slot)
                break
    return tuple(touched)


def _color_is_override(options: Mapping) -> bool:
    color = options.get('color')
    if not isinstance(color, (list, tuple)) or len(color) != 3:
        return False
    return any(abs(float(c) - 1.0) > 1e-6 for c in color)


def _write_map(workdir: Path, slot: str, image) -> Path:
    """Write a derived map atomically under a content-derived name."""
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    data = buffer.getvalue()
    digest = hashlib.sha256(data).hexdigest()[:16]
    path = Path(workdir) / ('%s-%s.png' % (slot, digest))
    if path.is_file() and path.stat().st_size == len(data):
        return path
    staged = path.with_name(path.name + '.part')
    staged.write_bytes(data)
    import os
    os.replace(str(staged), str(path))
    return path


def _signature(options: Mapping, revision: int, unsupported) -> str:
    payload = {
        'schema': PARAMETER_SCHEMA_VERSION,
        'revision': int(revision),
        'options': {key: (list(value) if isinstance(value, tuple) else value)
                    for key, value in (options or {}).items()},
        'unsupported': list(unsupported or ()),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()[:32]


def _binding_name(binding: Mapping, slot: str) -> str:
    return str(binding.get('name') or slot)


def _opt_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value)
    return text or None


def _opt_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_tuple(value: Any) -> Optional[tuple]:
    if value is None:
        return None
    try:
        return tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None


def capability_report(snapshot: Optional[ParameterSnapshot] = None) -> dict:
    """What the mapping honestly supports, for the handoff report and the UI."""
    return {
        'supported_slots': list(SUPPORTED_SLOTS),
        'unsupported_slots': dict(UNSUPPORTED_SLOTS),
        'orm_channels': {'R': 'ao', 'G': 'roughness', 'B': 'metallic'},
        'one_multiply': 'texture × strength is baked; multipliers stay unset',
        'textures_transform': ('F3D 4.0.0 的 Python 绑定无法设置 model.textures_transform，'
                               '每通道翻转变换烘焙进派生贴图'),
        'normal_map': ('仅在源文件提供切线时生效（glTF/GLB 带 TANGENT）；'
                       'OBJ/FBX 无切线时 F3D 不改变外观'),
        'alpha_mask': 'F3D 没有裁切/混合模式参数；透明预览用 alpha + blending.ddp',
        'snapshot_warnings': list(snapshot.warnings) if snapshot else [],
        'snapshot_unsupported': list(snapshot.unsupported) if snapshot else [],
    }
