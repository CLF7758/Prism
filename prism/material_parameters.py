"""Shared control semantics for the inspector, render inputs and persistence."""
import copy
import math


RANGES = {
    'basecolor': (0., 1000., 1.), 'roughness': (0., 1000., .5),
    'metallic': (0., 1000., 0.), 'normal': (0., 1000., 1.),
    'ao': (0., 1000., 1.), 'opacity': (0., 1000., 1.),
    'emissive': (0., 1000., 0.), 'sss': (0., 1000., 1.),
    'displacement': (0., 1000., .07),
}


def reset_texture_adjustments(properties, mapped_slots=()):
    """Reset numeric/tint controls without changing maps, channels or switches."""
    result = copy.deepcopy(properties)
    slots = result.setdefault('slots', {})
    for options in slots.values():
        for key in ('value', 'factor', 'color'):
            options.pop(key, None)
    for slot in mapped_slots:
        slots.setdefault(slot, {}).update(value=1., factor=1., color=[1., 1., 1.])
    return result


def slot_options(properties, slot, has_map=False):
    stored = properties.get('slots', {}).get(slot, {})
    defaults = dict(enabled=True, invert=False, flipX=False, flipY=False,
                    value=1. if has_map else RANGES[slot][2], factor=1., midpoint=.5,
                    color=[1., 1., 1.])
    if slot == 'emissive' and has_map:
        defaults['value'] = 1.
    defaults.update(stored)
    defaults['amount'] = defaults['factor'] if has_map and slot in ('roughness', 'metallic') else defaults['value']
    return defaults


def linear_color(color, space='srgb'):
    """UI colours are sRGB; imported physical source factors may be linear."""
    if isinstance(color, str) and color.startswith('#') and len(color) == 7:
        color = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    if not isinstance(color, (list, tuple)) or len(color) < 3:
        raise ValueError('Invalid material colour')
    if space == 'linear':
        return [float(c) for c in color[:3]]
    from prism.material_preview_profile import preview_profile
    gamma = preview_profile()['colorGamma']
    return [float(c) ** gamma for c in color[:3]]


def resolved_controls(properties, mapped_slots=()):
    """Both backends consume these same defaults, factors and UI colours."""
    controls = {slot: slot_options(properties, slot, slot in mapped_slots) for slot in RANGES}
    for slot in controls:
        space = properties.get('basecolor_color_space', 'srgb') if slot == 'basecolor' else 'srgb'
        controls[slot]['linearColor'] = linear_color(controls[slot]['color'], space)
    return controls


def normal_convention(properties, authored=''):
    names = {'directx': 'DirectX', 'opengl': 'OpenGL'}
    for value in (authored, properties.get('normalConvention')):
        if isinstance(value, str) and value.casefold() in names:
            return names[value.casefold()]
    from prism.material_preview_profile import preview_profile
    return preview_profile()['normalConvention']


def validate_slot_patch(slot, patch):
    if slot not in RANGES or not isinstance(patch, dict):
        raise ValueError('无效的材质槽位设置')
    result = copy.deepcopy(patch)
    for key, value in result.items():
        if key in ('enabled', 'invert', 'flipX', 'flipY'):
            if not isinstance(value, bool):
                raise ValueError('槽位开关必须是布尔值')
        elif key in ('value', 'factor', 'midpoint'):
            low, high = (0., 1.) if key == 'midpoint' else RANGES[slot][:2]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('槽位数值超出范围')
        elif key == 'color':
            if (not isinstance(value, list) or len(value) != 3
                    or any(not isinstance(v, (int, float)) or isinstance(v, bool)
                           or not math.isfinite(v) or not 0 <= v <= 1 for v in value)):
                raise ValueError('颜色数值无效')
        else:
            raise ValueError('未知的槽位设置')
    return result


CHANNELS = ('RGB', 'R', 'G', 'B', 'A')


def validate_texture_channel(slot, channel):
    """Editable binding policy shared by both library services and controls."""
    if slot not in RANGES or channel not in CHANNELS:
        raise ValueError('Invalid texture slot or channel')
    if slot == 'normal' and channel != 'RGB':
        raise ValueError('Normal textures require complete RGB vector channels')
    return channel


def texture_color_space(slot):
    if slot not in RANGES:
        raise ValueError('Invalid texture slot')
    return 'srgb' if slot in ('basecolor', 'emissive') else 'linear'
