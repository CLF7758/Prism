"""Stable identity for material rows in the asset canvas (P3).

CPython recycles the address of a freed object.  A layout key built from
``id(item)`` therefore treats a deleted material and a freshly created one
with the same size and crop as the same row, and the canvas replays the
deleted material's cached layout instead of laying the new one out.  The
row map built from ``id(item)`` has the same problem: a recycled address
points the row at the previous material.

These tests pin the stable identity: one object keeps one identity, two
objects never share one, and a replacement material never inherits the
deleted material's layout key or row.
"""
import gc
from types import SimpleNamespace

from PyQt6 import QtCore, QtWidgets

from prism.widgets.asset_browser import AssetModel
from prism.widgets.asset_canvas import AssetCanvas


class Material:
    """Attribute-capable stand-in for a scene item.

    Real materials are ``QGraphicsItem`` subclasses that carry attributes
    (``save_id``, ``_canvas_id``, ...); the canvas identity is attached to
    the object, so the stand-in has to accept attributes as well.
    """

    def __init__(self, title, size=(256, 128), canvas_id='a', save_id=None):
        self.title = title
        self._image_size = QtCore.QSizeF(*size)
        self.crop = QtCore.QRectF(0, 0, *size)
        self._canvas_id = canvas_id
        if save_id is not None:
            self.save_id = save_id


def canvas(qapp, count=3):
    owner = QtWidgets.QWidget()
    owner.mode = 'all'
    owner.sort_key = 'name'
    owner.view = SimpleNamespace(scene=SimpleNamespace(project_id='a'),
                                 on_can_undo_changed=lambda _: None,
                                 on_can_redo_changed=lambda _: None)
    host = AssetCanvas(owner)
    model = AssetModel(owner)
    host.setModel(model)
    host._composites.MIN_ITEMS = 100000
    return owner, host, model


def materials(count, title='m'):
    return [Material('%s-%d' % (title, index)) for index in range(count)]


def cached(host, key):
    return host._layout_cache.get(key) is not None


def recycled_scenario(host, model, other, attempts=64):
    """Reproduce CPython's address recycling around a real layout.

    The deleted material leaves the model and releases its address; the
    replacement material is allocated right afterwards, with nothing
    allocating in between, so it takes that very address.  Returns
    ``(deleted_key, replacement)`` or ``None`` when the allocator kept
    handing out fresh addresses.
    """
    gc.collect()
    for _ in range(attempts):
        deleted = Material('deleted')
        model.replace([deleted, other])
        host.arrange()
        deleted_key = host._layout_cache_key([])
        deleted_id = id(deleted)
        model.replace([other])
        del deleted
        replacement = Material('replacement')
        if id(replacement) == deleted_id:
            return deleted_key, replacement
        del replacement
    return None


def test_recycled_address_does_not_reuse_the_deleted_layout_key(qapp, monkeypatch):
    """A replacement material must not hit the deleted material's layout key.

    This is the reported defect: delete a material, create a new one with
    the same size and crop, and the layout cache must not serve the entry
    that was computed for the deleted material.
    """
    owner, host, model = canvas(qapp)
    other = Material('other')
    scenario = recycled_scenario(host, model, other)
    assert scenario is not None, 'CPython did not recycle the freed address'
    deleted_key, replacement = scenario
    assert cached(host, deleted_key), 'the deleted layout must be cached'

    # A replayed layout never packs again; a fresh one always does.
    from prism import compact_layout
    packs = []
    original_pack = compact_layout.pack_sizes
    monkeypatch.setattr(compact_layout, 'pack_sizes',
                        lambda sizes, **kwargs: packs.append(sizes)
                        or original_pack(sizes, **kwargs))
    model.replace([replacement, other])
    host.arrange()
    replacement_key = host._layout_cache_key([])

    assert replacement_key != deleted_key, \
        'the replacement material reused the deleted layout key'
    assert packs, 'the replacement material replayed the deleted layout entry'
    assert cached(host, deleted_key), \
        'the deleted layout entry must stay untouched for other ranges'
    owner.close()


def test_replacement_material_is_mapped_to_its_own_row(qapp):
    """The row map must follow the material, not a recycled address."""
    owner, host, model = canvas(qapp)
    deleted = Material('deleted')
    other = Material('other')
    model.replace([deleted, other])
    host.arrange()
    assert host._item_rows.get(host._row_identity(deleted)) == 0

    replacement = Material('replacement')
    model.replace([replacement, other])
    host.arrange()
    assert host._item_rows.get(host._row_identity(replacement)) == 0
    assert host.display_priority(replacement) != float('inf'), \
        'a material without a row is never decoded for the viewport'
    owner.close()


def test_row_replaced_in_place_is_remapped(qapp):
    """Replacing the object in a row must refresh that row's mapping."""
    owner, host, model = canvas(qapp)
    first = Material('first')
    model.replace([first, Material('other')])
    host.arrange()

    replacement = Material('replacement')
    model.items[0] = replacement
    model.dataChanged.emit(model.index(0), model.index(0))
    assert host._item_rows.get(host._row_identity(replacement)) == 0
    assert host.display_priority(replacement) != float('inf')
    owner.close()


def test_new_material_takes_a_different_slot_than_the_deleted_one(qapp):
    """Delete a material, create one with the same size and crop.

    The new material is a different asset and must not sit in the slot the
    deleted material occupied.
    """
    owner, host, model = canvas(qapp)
    first, second, third = materials(3)
    model.replace([first, second, third])
    host.arrange()
    deleted_position = host.cards[0].pos()

    model.replace([second, third])
    replacement = Material('replacement')
    model.replace([second, third, replacement])
    host.arrange()

    assert host.cards[2].pos() != deleted_position, \
        "the new material took the deleted material's slot"
    owner.close()


def test_identity_is_not_the_python_address_and_is_unique_per_object(qapp):
    """The identity must be attached to the object, never its address."""
    owner, host, model = canvas(qapp)
    first, second = materials(2)
    model.replace([first, second])
    host.arrange()

    first_identity = host._row_identity(first)
    assert id(first) not in first_identity, \
        'the identity must not be built from the recycled address'
    assert first_identity == host._row_identity(first), \
        'the identity of one object must be stable'
    assert first_identity != host._row_identity(second), \
        'two objects must never share an identity'
    assert host._row_identity(first)[1] == 'a', \
        'the canvas grouping stays part of the identity'
    owner.close()


def test_saved_material_identity_uses_the_project_save_id(qapp):
    """A saved material is identified by its project save id."""
    owner, host, model = canvas(qapp)
    saved = Material('saved', save_id=42)
    model.replace([saved])
    host.arrange()
    assert host._row_identity(saved)[0] == ('save', 42)

    twin = Material('twin', save_id=42)
    assert host._row_identity(twin)[0] == ('save', 42), \
        'the same project row is the same material'
    owner.close()


def test_layout_key_is_stable_while_the_content_does_not_change(qapp):
    """Same content must keep the same key, otherwise cards jump around."""
    owner, host, model = canvas(qapp)
    model.replace(materials(4))
    host.arrange()
    key = host._layout_cache_key([])
    positions = [card.pos() for card in host.cards]
    host.arrange()
    assert host._layout_cache_key([]) == key
    assert [card.pos() for card in host.cards] == positions
    owner.close()


def test_legacy_id_lookup_still_resolves_the_row(qapp):
    """``catalog_browser`` looks rows up by ``id(item)``; keep that working."""
    owner, host, model = canvas(qapp)
    first, second = materials(2)
    model.replace([first, second])
    host.arrange()
    assert host._item_rows.get(id(first)) == 0
    assert host._item_rows.get(id(second)) == 1
    owner.close()
