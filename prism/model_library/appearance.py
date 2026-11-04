"""Adapter between the existing material slot conventions and ModelAppearance.

The model library reuses Prism's existing texture controls: ``MaterialSlot``
from :mod:`prism.widgets.material_slots`, the parameter semantics of
:mod:`prism.material_parameters` and the channel list of
:mod:`prism.material_textures`. There is no second set of slot rules here.

The single rule that shapes this module: **a stored appearance is a snapshot,
never a live view.** Applying a material from the material library copies the
configuration at that moment. Editing the source material afterwards must not
silently change every model that used it, and the snapshot handed to a
background job must not change while that job runs.

This module is Qt-free and contains no F3D object.
"""
import copy
from dataclasses import dataclass
from typing import Dict, Iterable, Mapping, Optional, Tuple

from prism.material_parameters import RANGES, slot_options, validate_slot_patch
from prism.material_textures import SLOTS
from prism.model_library.contracts import ModelAppearance

#: Slots the native runtime has not verified yet.
#:
#: They stay visible in the inspector and carry an explicit reason. They are
#: never silently dropped: a user who opens the SSS slot must be able to see
#: that it is unavailable and why, instead of editing a control that has no
#: effect.
UNVERIFIED_SLOTS: Dict[str, str] = {
    'sss': '原生运行时未验证 SSS 通道',
    'displacement': '原生运行时未验证置换通道',
}

#: Property keys a single slot stores, in the existing convention.
SLOT_OPTION_KEYS = ('enabled', 'invert', 'flipX', 'flipY',
                    'value', 'factor', 'midpoint', 'color')


# ----------------------------------------------------------------------
# Detached snapshots
# ----------------------------------------------------------------------
def detached_mapping(value: Optional[Mapping]) -> dict:
    """Copy a mapping into plain, independent Python objects."""
    if not value:
        return {}
    return copy.deepcopy(dict(value))


def detached_appearance(appearance: Optional[ModelAppearance]) -> ModelAppearance:
    """Return an appearance that shares no mutable state with the original."""
    if appearance is None:
        return ModelAppearance()
    return ModelAppearance(
        revision=int(getattr(appearance, 'revision', 0) or 0),
        bindings=detached_mapping(getattr(appearance, 'bindings', None)),
        properties=detached_mapping(getattr(appearance, 'properties', None)),
    )


def appearance_payload(appearance: Optional[ModelAppearance]) -> dict:
    """A JSON-safe snapshot for storage or for a cross-process request."""
    snapshot = detached_appearance(appearance)
    return {
        'revision': snapshot.revision,
        'bindings': detached_mapping(snapshot.bindings),
        'properties': detached_mapping(snapshot.properties),
    }


def appearance_from_payload(payload: Optional[Mapping]) -> ModelAppearance:
    """Rebuild an appearance from a stored / transmitted payload."""
    if not payload:
        return ModelAppearance()
    return ModelAppearance(
        revision=int(payload.get('revision', 0) or 0),
        bindings=detached_mapping(payload.get('bindings')),
        properties=detached_mapping(payload.get('properties')),
    )


# ----------------------------------------------------------------------
# Reading a stored appearance
# ----------------------------------------------------------------------
def appearance_from_record(record) -> ModelAppearance:
    """Read the appearance of a complete ``ModelRecord``.

    Only a full record may be used here. A card summary is not a source of
    appearance: summaries are deliberately small and can be stale.
    """
    appearance = getattr(record, 'appearance', None)
    if appearance is None:
        return ModelAppearance()
    return detached_appearance(appearance)


def appearance_from_material_record(record: Mapping) -> ModelAppearance:
    """Snapshot a material-library record as a model appearance.

    The result is a detached copy. Later edits to the source material cannot
    reach a model that already used it.
    """
    textures = (record or {}).get('textures') or {}
    preview = (record or {}).get('preview') or {}
    bindings = {}
    for slot, binding in textures.items():
        if slot in SLOTS and isinstance(binding, Mapping):
            bindings[slot] = copy.deepcopy(dict(binding))
    properties = copy.deepcopy(dict(preview.get('properties') or {}))
    return ModelAppearance(revision=0, bindings=bindings, properties=properties)


def slot_binding(appearance: Optional[ModelAppearance], slot: str) -> Optional[dict]:
    """The stored texture binding of one slot, detached."""
    bindings = detached_mapping(getattr(appearance, 'bindings', None))
    binding = bindings.get(slot)
    return dict(binding) if isinstance(binding, Mapping) else None


def slot_state(appearance: Optional[ModelAppearance], slot: str) -> dict:
    """The effective option values of one slot, in the existing convention.

    Uses :func:`prism.material_parameters.slot_options`, so the inspector and
    the renderer read exactly the same numbers.
    """
    properties = detached_mapping(getattr(appearance, 'properties', None))
    binding = slot_binding(appearance, slot)
    return slot_options(properties, slot, bool(binding))


def appearance_bindings(appearance: Optional[ModelAppearance]) -> dict:
    """Every slot binding, detached."""
    return detached_mapping(getattr(appearance, 'bindings', None))


def appearance_properties(appearance: Optional[ModelAppearance]) -> dict:
    """Every slot property, detached."""
    return detached_mapping(getattr(appearance, 'properties', None))


# ----------------------------------------------------------------------
# Editing a stored appearance
# ----------------------------------------------------------------------
def apply_slot_patch(appearance: Optional[ModelAppearance], slot: str,
                     patch: Mapping) -> ModelAppearance:
    """Apply one validated slot patch and return a new appearance.

    The result is detached from the input: the caller keeps working on its
    own copy while the new appearance is on its way to a background job.
    """
    if slot not in SLOTS:
        raise ValueError('无效的贴图槽位')
    validated = validate_slot_patch(slot, patch)
    snapshot = detached_appearance(appearance)
    properties = snapshot.properties
    slots = properties.get('slots')
    slots = copy.deepcopy(slots) if isinstance(slots, Mapping) else {}
    current = dict(slots.get(slot) or {})
    current.update(validated)
    slots[slot] = current
    properties['slots'] = slots
    return ModelAppearance(
        revision=snapshot.revision + 1,
        bindings=snapshot.bindings,
        properties=properties,
    )


def set_slot_binding(appearance: Optional[ModelAppearance], slot: str,
                     binding: Optional[Mapping]) -> ModelAppearance:
    """Attach (or clear) the texture binding of one slot."""
    if slot not in SLOTS:
        raise ValueError('无效的贴图槽位')
    snapshot = detached_appearance(appearance)
    bindings = snapshot.bindings
    if binding:
        bindings[slot] = copy.deepcopy(dict(binding))
    else:
        bindings.pop(slot, None)
    return ModelAppearance(revision=snapshot.revision + 1,
                           bindings=bindings, properties=snapshot.properties)


def set_slot_channel(appearance: Optional[ModelAppearance], slot: str,
                     channel: str) -> ModelAppearance:
    """Change the channel of one slot's binding."""
    snapshot = detached_appearance(appearance)
    binding = snapshot.bindings.get(slot)
    if not isinstance(binding, Mapping):
        return snapshot
    updated = dict(binding)
    updated['channel'] = channel
    bindings = snapshot.bindings
    bindings[slot] = updated
    return ModelAppearance(revision=snapshot.revision + 1,
                           bindings=bindings, properties=snapshot.properties)


def appearance_signature(appearance: Optional[ModelAppearance]) -> Tuple:
    """A comparable fingerprint of the visible configuration.

    Used to tell "the user changed something" from "the same configuration
    was published again". Two appearances with the same signature render the
    same way, so a cover does not need to be regenerated.
    """
    snapshot = detached_appearance(appearance)
    bindings = {}
    for slot, binding in (snapshot.bindings or {}).items():
        if isinstance(binding, Mapping):
            bindings[slot] = (binding.get('sha256'), binding.get('channel'),
                              binding.get('attachmentId'))
        else:
            bindings[slot] = repr(binding)
    properties = snapshot.properties or {}
    slots = properties.get('slots') if isinstance(properties, Mapping) else None
    normalized = {}
    if isinstance(slots, Mapping):
        for slot, options in slots.items():
            if isinstance(options, Mapping):
                normalized[slot] = tuple(sorted(
                    (key, _hashable(options.get(key)))
                    for key in SLOT_OPTION_KEYS if key in options))
    return (tuple(sorted(bindings.items())), tuple(sorted(normalized.items())))


def _hashable(value):
    if isinstance(value, (list, tuple)):
        return tuple(value)
    if isinstance(value, dict):
        return tuple(sorted(value.items()))
    return value


# ----------------------------------------------------------------------
# Slot availability
# ----------------------------------------------------------------------
def resolve_slot_policy(overrides: Optional[Mapping] = None) -> Dict[str, str]:
    """Return ``{slot: blocking reason}``; an empty reason means usable.

    ``overrides`` lets the composition root publish the slots the native
    runtime has actually verified. It accepts:

    * ``True`` / ``''``   -> the slot is verified and usable
    * ``False``           -> unavailable, generic reason
    * a non-empty string -> unavailable, that string is the reason
    """
    policy = dict(UNVERIFIED_SLOTS)
    for slot, value in (overrides or {}).items():
        if slot not in SLOTS:
            continue
        if value is True or value == '':
            policy.pop(slot, None)
        elif value is False:
            policy[slot] = '原生运行时未验证该通道'
        elif isinstance(value, str):
            policy[slot] = value
    return policy


def usable_slots(policy: Optional[Mapping] = None) -> Tuple[str, ...]:
    """The slots that may be edited, in the existing slot order."""
    blocked = resolve_slot_policy(policy)
    return tuple(slot for slot in SLOTS if slot not in blocked)


def slot_block_reason(slot: str, policy: Optional[Mapping] = None) -> str:
    """Why a slot is unavailable, or an empty string when it is usable."""
    return resolve_slot_policy(policy).get(slot, '')


# ----------------------------------------------------------------------
# Edit sessions
# ----------------------------------------------------------------------
class AppearanceSession:
    """One editing session on one model.

    A slider drag emits dozens of small patches. They are accumulated here
    and become **one** appearance revision when the session is committed, so
    undo has one entry per real user action instead of one per pixel.

    The session never writes to the project. Committing produces a detached
    appearance; persisting it is the service's job.
    """

    def __init__(self, appearance: Optional[ModelAppearance] = None,
                 *, supported: Optional[Mapping] = None):
        self._base = detached_appearance(appearance)
        self._working = detached_appearance(appearance)
        self._supported = resolve_slot_policy(supported)
        self._dirty = False

    # -- state ---------------------------------------------------------
    @property
    def base(self) -> ModelAppearance:
        """The appearance the session started from."""
        return detached_appearance(self._base)

    @property
    def working(self) -> ModelAppearance:
        """The current working copy, detached."""
        return detached_appearance(self._working)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def supported(self) -> Dict[str, str]:
        return dict(self._supported)

    def is_supported(self, slot: str) -> bool:
        return slot not in self._supported

    def block_reason(self, slot: str) -> str:
        return self._supported.get(slot, '')

    # -- editing -------------------------------------------------------
    def patch(self, slot: str, patch: Mapping) -> bool:
        """Merge one slot patch into the working copy.

        Returns ``False`` when the slot is not available, so the caller can
        report the reason instead of pretending the edit happened.
        """
        reason = self.block_reason(slot)
        if reason:
            return False
        self._working = apply_slot_patch(self._working, slot, patch)
        self._dirty = True
        return True

    def set_binding(self, slot: str, binding: Optional[Mapping]) -> bool:
        if self.block_reason(slot):
            return False
        self._working = set_slot_binding(self._working, slot, binding)
        self._dirty = True
        return True

    def set_channel(self, slot: str, channel: str) -> bool:
        if self.block_reason(slot):
            return False
        self._working = set_slot_channel(self._working, slot, channel)
        self._dirty = True
        return True

    def apply_material(self, material_record: Mapping) -> bool:
        """Adopt the configuration of a material-library record.

        The configuration is copied at this moment. Later edits to the
        source material do not reach this model.
        """
        snapshot = appearance_from_material_record(material_record)
        unsupported = [slot for slot in snapshot.bindings
                       if self.block_reason(slot)]
        bindings = {slot: binding for slot, binding in snapshot.bindings.items()
                    if not self.block_reason(slot)}
        properties = detached_mapping(snapshot.properties)
        slots = properties.get('slots')
        if isinstance(slots, Mapping):
            properties['slots'] = {slot: options for slot, options in slots.items()
                                   if not self.block_reason(slot)}
        self._working = ModelAppearance(revision=self._working.revision,
                                        bindings=bindings, properties=properties)
        self._dirty = True
        return not unsupported

    # -- finishing -----------------------------------------------------
    def commit(self) -> ModelAppearance:
        """Return the merged appearance and close the session."""
        if not self._dirty:
            return detached_appearance(self._base)
        committed = ModelAppearance(revision=self._base.revision + 1,
                                    bindings=self._working.bindings,
                                    properties=self._working.properties)
        self._base = detached_appearance(committed)
        self._working = detached_appearance(committed)
        self._dirty = False
        return detached_appearance(committed)

    def discard(self) -> None:
        """Throw the working copy away and return to the base appearance."""
        self._working = detached_appearance(self._base)
        self._dirty = False


def collect_appearance(slots: Iterable, appearance: Optional[ModelAppearance] = None,
                        properties: Optional[Mapping] = None) -> ModelAppearance:
    """Collect a detached appearance out of live ``MaterialSlot`` controls.

    ``slots`` is an iterable of ``(key, widget)`` pairs. Each widget is read
    through its public option accessors only; nothing keeps a reference to a
    widget or to a live dictionary.
    """
    snapshot = detached_appearance(appearance)
    bindings = detached_mapping(snapshot.bindings)
    stored = detached_mapping(properties if properties is not None
                              else snapshot.properties)
    stored_slots = stored.get('slots')
    stored_slots = copy.deepcopy(stored_slots) if isinstance(stored_slots, Mapping) else {}
    for key, widget in slots or ():
        if key not in SLOTS:
            continue
        binding = bindings.get(key)
        if binding is not None and not isinstance(binding, Mapping):
            binding = None
        options = slot_options(stored, key, bool(binding))
        options = dict(options)
        options['enabled'] = bool(widget.enabled.isChecked())
        options['invert'] = bool(widget.invert.isChecked())
        options['flipX'] = bool(widget.flip_x.isChecked())
        options['flipY'] = bool(widget.flip_y.isChecked())
        options['midpoint'] = float(widget.midpoint.value())
        options['color'] = list(getattr(widget, '_color', [1., 1., 1.]))
        if binding and key in ('roughness', 'metallic'):
            options['factor'] = float(widget.amount.value())
            options['value'] = options['factor']
        else:
            options['value'] = float(widget.amount.value())
        stored_slots[key] = {name: options[name] for name in SLOT_OPTION_KEYS
                             if name in options}
    stored['slots'] = stored_slots
    return ModelAppearance(revision=snapshot.revision,
                           bindings=bindings, properties=stored)
