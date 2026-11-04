"""The one slot vocabulary: what a source states vs. what the app may edit.

Three different things were previously one list, and that is how a viewer ends
up claiming that SSS works because a slot is named ``sss``:

``STANDARD_SLOTS``
    The slots Prism's own material controls know about (identical to
    ``prism.material_textures.SLOTS``).  A *name*, not a promise.
``SOURCE_ROLES``
    Roles a source file states that Prism has no standard slot for:
    ``bump`` is not ``normal``, ``specular`` is not ``roughness``.  They are
    kept so nothing is dropped and nothing is mislabelled.
``editable_slots(verified)``
    What may actually be **edited**, which is a capability question, not a
    naming question.  Nothing is editable until the runtime has verified it, and
    ``sss``/``displacement`` stay unverified until AI 2 proves them.

Only this module defines the vocabulary.  ``catalog`` re-exports it for the
payload, and the UI/backend must import from here instead of copying a list.
Import direction: ``catalog``/``inspectors`` -> ``vocabulary`` -> (nothing in
this package).
"""
from __future__ import annotations

#: Slots the existing material controls define.  Order is the display order.
STANDARD_SLOTS = ('basecolor', 'normal', 'roughness', 'metallic', 'ao',
                  'opacity', 'emissive', 'sss', 'displacement')

#: Roles a source file states that have no standard slot.
SOURCE_ROLES = ('bump', 'specular', 'specular_exponent', 'reflection',
                'ambient', 'decal', 'transparency')

#: Every slot name a catalog payload may carry.
CATALOG_SLOTS = STANDARD_SLOTS + SOURCE_ROLES

#: Slots whose editing path is *not* verified by the pinned native runtime yet.
#: Kept here so the UI and the runtime agree on the reason, not on a boolean.
UNVERIFIED_SLOTS = {
    'sss': '原生运行时未验证真实的次表面散射（透明/透射不算）',
    'displacement': '原生运行时未验证置换的几何策略与预算',
}

#: Slots that are never editable through a texture patch, even when the
#: runtime can display them: they are source roles, displayed read-only.
READ_ONLY_SLOTS = SOURCE_ROLES


def is_standard(slot: str) -> bool:
    return slot in STANDARD_SLOTS


def is_source_role(slot: str) -> bool:
    return slot in SOURCE_ROLES


def editable_slots(verified=()) -> tuple:
    """The slots that may be edited, given what the runtime has verified.

    ``verified`` is what the native backend reports as working *and* proven
    (see ``MaterialCapabilityReport``).  An unverified standard slot is not
    editable, and a source role is never editable: it has no semantic target in
    Prism, so editing it would silently mean something else.
    """
    confirmed = set(verified or ())
    return tuple(slot for slot in STANDARD_SLOTS
                 if slot in confirmed and slot not in UNVERIFIED_SLOTS)


def slot_policy(verified=()) -> dict:
    """``{slot: blocking reason}``; an empty reason means editable."""
    policy = {}
    editable = set(editable_slots(verified))
    for slot in STANDARD_SLOTS:
        if slot in editable:
            continue
        policy[slot] = UNVERIFIED_SLOTS.get(slot, '原生运行时未验证该槽位')
    for slot in SOURCE_ROLES:
        policy[slot] = '源文件角色，只读：' + _role_reason(slot)
    return policy


def _role_reason(slot: str) -> str:
    return {
        'bump': 'bump 是高度贴图，不是法线贴图',
        'specular': 'specular 是镜面贴图，不是粗糙度',
        'specular_exponent': '镜面指数不是粗糙度',
        'reflection': '反射贴图没有等价槽位',
        'ambient': '环境色贴图不是 AO',
        'decal': '贴花贴图没有等价槽位',
        'transparency': '透明贴图的语义未分解',
    }.get(slot, '没有等价槽位')


def classify(slot: str) -> str:
    """``'standard' | 'source_role' | 'unknown'`` for one slot name."""
    if is_standard(slot):
        return 'standard'
    if is_source_role(slot):
        return 'source_role'
    return 'unknown'
