"""Effective surface replacement rules, independent of storage and rendering."""
from dataclasses import replace
import json

SURFACE_KEEPS = frozenset({'opacity', 'sss', 'displacement'})

def replace_surface(original, replacement):
    """Replace appearance while preserving geometry/coverage-related channels.

    Inputs are immutable definitions. Returned definition owns its serialized
    properties; neither source nor library material is modified.
    """
    textures = tuple(t for t in replacement.textures if t.slot not in SURFACE_KEEPS)
    textures += tuple(t for t in original.textures if t.slot in SURFACE_KEEPS)
    properties = json.loads(replacement.properties_json or '{}')
    source = json.loads(original.properties_json or '{}')
    for key in ('alpha_mode', 'alpha_cutoff', 'double_sided'):
        if key in source:
            properties[key] = source[key]
    # The shared browser uses Prism's canonical transparency fields. Keeping
    # only the source-format alias would let the replacement's "auto" win.
    if 'blendMode' in source:
        properties['blendMode'] = source['blendMode']
    elif source.get('alpha_mode') in ('OPAQUE','MASK','BLEND'):
        properties['blendMode'] = {'OPAQUE':'opaque','MASK':'masked','BLEND':'translucent'}[source['alpha_mode']]
    if 'alphaCutoff' in source:properties['alphaCutoff'] = source['alphaCutoff']
    elif 'alpha_cutoff' in source:properties['alphaCutoff'] = source['alpha_cutoff']
    for slot in SURFACE_KEEPS:
        if slot in source.get('slots', {}):
            properties.setdefault('slots', {})[slot] = source['slots'][slot]
    return replace(replacement, textures=textures, properties_json=json.dumps(properties))
