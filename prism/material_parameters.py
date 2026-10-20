"""Shared control semantics for the inspector, render inputs and persistence."""
import copy
import math


RANGES = {
    'basecolor': (0., 2., 1.), 'roughness': (0., 1., .5),
    'metallic': (0., 1., 0.), 'normal': (0., 4., 1.),
    'ao': (0., 1., 1.), 'opacity': (0., 1., 1.),
    'emissive': (0., 20., 0.), 'sss': (0., 1., 1.),
    'displacement': (0., .2, .07),
}


def slot_options(properties, slot, has_map=False):
    stored = properties.get('slots', {}).get(slot, {})
    defaults = dict(enabled=True, invert=False, flipX=False, flipY=False,
                    value=RANGES[slot][2], factor=1., midpoint=.5,
                    color=[1., 1., 1.])
    if slot == 'emissive' and has_map:
        defaults['value'] = 1.
    defaults.update(stored)
    defaults['amount'] = defaults['factor'] if has_map and slot in ('roughness', 'metallic') else defaults['value']
    return defaults


def validate_slot_patch(slot, patch):
    if slot not in RANGES or not isinstance(patch, dict):
        raise ValueError('无效的材质槽位设置')
    result = copy.deepcopy(patch)
    for key, value in result.items():
        if key in ('enabled', 'invert', 'flipX', 'flipY'):
            if not isinstance(value, bool):
                raise ValueError('槽位开关必须是布尔值')
        elif key in ('value', 'factor', 'midpoint'):
            low, high = (0., 1.) if key in ('factor', 'midpoint') else RANGES[slot][:2]
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
