"""C0-C4 UI test doubles. Production stays disabled; these exist only here."""
import json
from dataclasses import dataclass

import pytest
from PyQt6 import QtCore, QtWidgets

from prism.model_library.multimaterial.contracts import (
    BindingSite, MaterialAssignment, MaterialCatalog, MaterialCapabilityReport,
    MaterialDefinition, MaterialState, MaterialTarget, WorkingMaterial)


@dataclass(frozen=True)
class TextureRecord:
    """A texture with an explicit backend state.

    Contracts do not carry a texture state yet (RFC-3-4), so the double reports
    ``state`` the way the future contract will: the UI must never infer "missing
    file" from an empty resource id.
    """

    slot: str
    resource_id: str = ''
    channels: str = 'RGB'
    texcoord: int = 0
    color_space: str = 'srgb'
    uv_transform: tuple = ()
    normal_convention: str = ''
    source_locator: str = ''
    state: str = 'ok'


def make_target(model_id='model-a', *, session='session-1', epoch=1,
                source_revision=2, catalog_revision=3, state_revision=4):
    return MaterialTarget(session, epoch, model_id, source_revision,
                          catalog_revision, state_revision)


def chair_catalog(*, unresolved=('legacy_material_slot',)):
    """One chair: four legs share the wood, the seat uses metal."""
    definitions = (
        MaterialDefinition(
            'mat-wood', 'src-wood', '木材', 'pbr',
            textures=(TextureRecord('basecolor', 'res-wood-colour', 'RGB+A',
                                    color_space='srgb', state='ok',
                                    source_locator='wood_basecolor.png'),
                      TextureRecord('normal', 'res-wood-normal', 'RGB',
                                    color_space='linear', state='ok',
                                    source_locator='wood_normal.png'))),
        MaterialDefinition(
            'mat-metal', 'src-metal', '金属', 'pbr',
            textures=(TextureRecord('basecolor', 'res-metal', 'RGB', state='ok',
                                    source_locator='metal.png'),)),
        MaterialDefinition(
            'mat-unused', 'src-unused', '未使用的皮革', 'pbr',
            textures=(TextureRecord('basecolor', 'res-leather', 'RGB', state='ok',
                                    source_locator='leather.png'),)),
        MaterialDefinition(
            'mat-broken', 'src-broken', '缺图材质', 'pbr',
            textures=(TextureRecord('basecolor', '', 'RGB', state='missing'),)),
        MaterialDefinition(
            'mat-partial', 'src-partial', '半透布', 'pbr',
            textures=(TextureRecord('basecolor', 'res-cloth', 'RGB', state='ok',
                                    source_locator='cloth.png'),),
            unsupported=('sss',)),
    )
    sites = tuple(
        BindingSite(f'bind-{name}', f'src-{name}', name, 'geo-chair', 'prim-0',
                    'mat-wood' if name.startswith('leg') else 'mat-metal')
        for name in ('leg-fl', 'leg-fr', 'leg-bl', 'leg-br', 'seat')
    ) + (BindingSite('bind-back', 'src-back', 'back', 'geo-chair', 'prim-1', None),)
    catalog = MaterialCatalog(1, 11, 'digest', 'test-parser', definitions, sites,
                              unresolved=unresolved)
    state = MaterialState(
        target=make_target(),
        materials=(WorkingMaterial('w-wood', 'mat-wood', '木材'),
                   WorkingMaterial('w-metal', 'mat-metal', '金属'),
                   WorkingMaterial('w-broken', 'mat-broken', '缺图材质'),
                   WorkingMaterial('w-partial', 'mat-partial', '半透布')),
        assignments=(MaterialAssignment('bind-leg-fl', 'w-wood'),
                     MaterialAssignment('bind-leg-fr', 'w-wood'),
                     MaterialAssignment('bind-leg-bl', 'w-wood'),
                     MaterialAssignment('bind-leg-br', 'w-wood'),
                     MaterialAssignment('bind-seat', 'w-metal'),
                     MaterialAssignment('bind-front', 'w-broken'),
                     MaterialAssignment('bind-arm', 'w-partial')))
    return catalog, state


def modified_catalog():
    """Wood carries a local override, so restore actions become available."""
    catalog, state = chair_catalog()
    patched = WorkingMaterial(
        'w-wood', 'mat-wood', '木材',
        texture_patches=(),
        property_patch_json=json.dumps({'slots': {'roughness': {'value': .27}}}))
    materials = (patched,) + tuple(state.materials[1:])
    return catalog, MaterialState(state.target, materials, state.assignments)


class FakePort:
    """Records every port call; returns job ids, never a result."""

    def __init__(self, *, report=None, fail=(), counters=None):
        self.calls = []
        self.report = report or MaterialCapabilityReport(
            read_catalog=True, edit_material=True, assign_material=True,
            pick_material=True)
        self.fail = set(fail)
        self.counters = counters or {}
        self.thumbs = {}
        self.subscribers = []
        self.unsubscribed = []
        self.subscription_tokens = []

    # -- subscription (optional service push channel) -----------------
    def subscribe(self, callback):
        self.subscribers.append(callback)
        token = f'sub-{len(self.subscription_tokens) + 1}'
        self.subscription_tokens.append(token)
        return token

    def unsubscribe(self, token):
        self.unsubscribed.append(token)
        if self.subscribers:
            self.subscribers.pop(0)

    def deliver(self, event):
        """Push one event to the current subscribers (GUI-thread delivery)."""
        return [callback(event) for callback in list(self.subscribers)]

    def _record(self, name, *args):
        self.calls.append((name,) + args)
        if name in self.fail:
            raise RuntimeError(name + ' 失败（测试替身）')

    def calls_of(self, name):
        return [call for call in self.calls if call[0] == name]

    def capabilities(self, target, working_ids=()):
        self._record('capabilities', target, tuple(working_ids))
        return self.report

    def inspect(self, target):
        self._record('inspect', target)
        self.counters['inspect'] = self.counters.get('inspect', 0) + 1
        return f'job-inspect-{self.counters["inspect"]}'

    def preview(self, request, draft_generation):
        self._record('preview', request, draft_generation)
        self.counters['preview'] = self.counters.get('preview', 0) + 1
        return f'job-preview-{self.counters["preview"]}'

    def commit(self, request):
        self._record('commit', request)
        self.counters['commit'] = self.counters.get('commit', 0) + 1
        return f'job-commit-{self.counters["commit"]}'

    def cancel(self, job_id):
        self._record('cancel', job_id)

    def release(self, lease_id):
        self._record('release', lease_id)

    def highlight(self, target, binding_ids):
        self._record('highlight', target, tuple(binding_ids))

    def thumbnail(self, target, key):
        self._record('thumbnail', target, key)
        return self.thumbs.get(key)

    def restore(self, target):
        self._record('restore', target)
        return 'job-restore-1'


class FakeLibrarySource:
    def __init__(self, entries=()):
        self._entries = tuple(entries)
        self.reads = []

    def entries(self):
        return self._entries

    def thumbnail(self, entry_id):
        self.reads.append(entry_id)
        return 'data:image/png;base64,AAAA'


class FakeCards(QtWidgets.QWidget):
    """The part of the card surface the picker uses; no Chromium involved."""

    selection_toggled = QtCore.pyqtSignal(str, bool)
    selection_changed = QtCore.pyqtSignal(str)
    thumbnail_requested = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.assets = []
        self.selected_id = None
        self.patched = {}

    def set_display_assets(self, assets, selected_id=None, selected_ids=None,
                           bounded=False):
        self.assets = list(assets)
        self.selected_id = selected_id
        self.bounded = bounded

    def update_thumbnail(self, record, thumbnail):
        self.patched[record['id']] = thumbnail


@pytest.fixture
def port():
    return FakePort()


@pytest.fixture
def library_source():
    from prism.widgets.model_material_picker import LibraryEntry
    return FakeLibrarySource((
        LibraryEntry('lib-1', '胡桃木', tags=('木材', '室内'),
                     channels=('basecolor', 'normal'), resolution='2K',
                     snapshot_token='sha256:aa11'),
        LibraryEntry('lib-2', '拉丝金属', tags=('金属',),
                     channels=('basecolor', 'metallic'), resolution='1K',
                     snapshot_token='sha256:bb22'),
        LibraryEntry('lib-3', '布料', tags=('织物',), channels=('basecolor',),
                     snapshot_token='sha256:cc33'),
    ))


@pytest.fixture
def panel(qtbot, port, library_source):
    from prism.widgets.model_material_panel import ModelMaterialPanel
    widget = ModelMaterialPanel()
    qtbot.addWidget(widget)
    widget.set_port(port)
    widget.set_library_source(library_source)
    widget.picker_factory = _cards_picker_factory
    widget.reset(make_target())
    yield widget
    widget.shutdown()


def _cards_picker_factory(parent=None):
    from prism.widgets.model_material_picker import MaterialPicker
    return MaterialPicker(parent, card_factory=FakeCards)


@pytest.fixture
def loaded(qtbot, panel, port):
    """The panel after one successful catalog publication."""
    catalog, state = chair_catalog()
    panel.publish_catalog(make_target(), catalog, state)
    return panel


def start_preview(panel, port, value=0.33):
    """Drive one real preview and return its ``(request_id, generation)``.

    Tests need the identity of the *current* preview to exercise out-of-order,
    duplicate and post-cancel deliveries without reading panel internals.
    """
    panel.select_material('w-wood')
    slot = panel.editor.slots['roughness']
    slot.amount.setValue(value)
    request, generation = port.calls_of('preview')[-1][1], port.calls_of('preview')[-1][2]
    return request.request_id, generation


def start_commit(panel, port, value=0.44):
    """Drive one real commit and return its request."""
    panel.select_material('w-wood')
    slot = panel.editor.slots['roughness']
    slot.amount.setValue(value)
    return port.calls_of('commit')[-1][1]
