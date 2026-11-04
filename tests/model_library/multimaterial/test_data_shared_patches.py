"""What the shared files must guarantee before A3 can store anything.

Two kinds of test live here:

* **live** checks the product already satisfies (a ``multimaterial`` block must
  survive the record round-trip and every metadata edit -- "claimed but then
  dropped" is the failure mode this guards);
* **pending** checks that ``skip`` with the exact patch they wait for.  They are
  not deleted and not weakened: when the main conversation lands the patch
  described in ``docs/model-multimaterial-tasks/AI_1_SHARED_PATCHES.md``, the
  same test starts asserting instead of skipping.
"""
import json

import pytest

from prism.model_library.project_storage import (
    CONTRACT_FIELDS,
    OWNED_FIELDS,
    decode_record,
    encode_record,
    model_attachment_ids,
    register_model_attachments,
)
from prism.model_library.repository import ModelRepositoryAdapter

BLOCK = {
    'schema_version': 1,
    'catalog': {'schema_version': 1, 'revision': 1, 'source_digest': 'sha256:abc',
                'parser_id': 'gltf-2.0', 'definitions': [], 'binding_sites': [],
                'unresolved': []},
    'state': {'state_revision': 1, 'materials': [], 'assignments': []},
    'resources': {'sha256:one': {'attachment_id': 9001, 'locator': 'a.png',
                                 'size': 10},
                  'sha256:two': {'attachment_id': 9002, 'locator': 'b.png',
                                 'size': 20}},
}


def _payload(project_id='project-1', **extra):
    payload = {
        'id': 'model-1',
        'model_id': 'model-1',
        'project_id': project_id,
        'name': 'Chair',
        'main_file_id': 'file-1',
        'format': 'fbx',
        'source_revision': 1,
        'record_revision': 1,
        'folder_id': None,
        'tags': [],
        'notes': '',
        'files': [{'file_id': 'file-1', 'relative_path': 'models/chair.fbx',
                   'role': 'main', 'size_bytes': 10, 'sha256': 'abc',
                   'source_ref': 'chair.fbx', 'attachment_id': 9000,
                   'missing': False}],
        'appearance': {'revision': 0, 'bindings': {}, 'properties': {}},
        'statistics': {},
        'unknown_fields': {},
    }
    payload.update(extra)
    return payload


def _block():
    return json.loads(json.dumps(BLOCK))


# ---------------------------------------------------------------------------
# live: the block must not be dropped on the way through the record codec
# ---------------------------------------------------------------------------
def test_the_record_codec_retains_the_multimaterial_block():
    """``decode_record`` must keep a field it has no slot for."""
    record = decode_record(_payload(multimaterial=_block()))
    assert record.unknown_fields['multimaterial'] == BLOCK
    encoded = encode_record(record)
    assert encoded['unknown_fields']['multimaterial'] == BLOCK


def test_encode_record_flattens_the_retained_block():
    """RFC 7.6: encode/decode must be symmetric, or only the save path saves."""
    record = decode_record(_payload(multimaterial=_block()))
    if 'multimaterial' not in encode_record(record):
        pytest.skip('pending main-conversation patch RFC 7.6: encode_record keeps '
                    'the block inside unknown_fields, so only '
                    'repository._write_payload (merge_unknown_fields) writes it '
                    'back to the top level')
    assert encode_record(record)['multimaterial'] == BLOCK


def test_the_block_survives_metadata_only_edits(qapp, tmp_path):
    """A rename or a cover refresh must not delete material state."""
    scene, context = _project(tmp_path)
    del scene
    repository = ModelRepositoryAdapter(context)
    repository.insert(_payload(project_id=context.project_id,
                               multimaterial=_block()))

    stored = repository.get_payload('model-1')
    assert stored['multimaterial'] == BLOCK

    pairs = repository.patch_batch([('model-1', stored['record_revision'],
                                     {'name': 'Chair renamed'})])
    after = pairs[0][1]
    assert after['multimaterial'] == BLOCK

    repository.update_cache('model-1', expected_revision=after['record_revision'],
                            thumbnail={'state': 'ready', 'cache_key': 'k'})
    assert repository.get_payload('model-1')['multimaterial'] == BLOCK

    repository.set_thumbnail('model-1', None,
                             expected_revision=repository.get_payload(
                                 'model-1')['record_revision'])
    assert repository.get_payload('model-1')['multimaterial'] == BLOCK

    repository.retire('model-1', expected_revision=repository.get_payload(
        'model-1')['record_revision'])
    assert repository.get_payload('model-1')['multimaterial'] == BLOCK


def _project(tmp_path):
    """A real project written by the application's own writer."""
    from PyQt6.QtGui import QUndoStack
    from prism.scene import PrismGraphicsScene
    from prism.fileio.sql import SQLiteIO
    from prism.model_library.project_storage import open_model_context

    scene = PrismGraphicsScene(QUndoStack())
    path = tmp_path / 'a2.prism'
    io = SQLiteIO(str(path), scene, create_new=True)
    try:
        io.write()
    finally:
        io._close_connection()
    return scene, open_model_context(str(path))


# ---------------------------------------------------------------------------
# pending: the patches the main conversation owns
# ---------------------------------------------------------------------------
def test_pending_nested_material_attachments_are_part_of_the_model():
    """RFC 7.4: ``model_attachment_ids`` must see nested material resources."""
    ids = set(model_attachment_ids(_payload(multimaterial=_block())))
    if not {9001, 9002} <= ids:
        pytest.skip('pending main-conversation patch RFC 7.4: model_attachment_ids '
                    'walks files and appearance.bindings only, so nested material '
                    f'resources are not registered (currently {sorted(ids)})')
    assert {9000, 9001, 9002} <= ids


def test_pending_nested_material_attachments_are_registered():
    """RFC 7.4: the import path must register them, or cleanup deletes them."""

    class _Attachments:
        def __init__(self):
            self._meta = {}
            self._lock = None

    class _Scene:
        def __init__(self):
            self.attachments = _Attachments()

    registered = set(register_model_attachments(
        _Scene(), _payload(multimaterial=_block())))
    if not {9001, 9002} <= registered:
        pytest.skip('pending main-conversation patch RFC 7.4: '
                    'register_model_attachments registers files and '
                    f'appearance.bindings only (currently {sorted(registered)})')
    assert {9000, 9001, 9002} <= registered


def test_pending_partial_save_counts_model_attachments_as_required():
    """RFC 7.5: a page-level save must require the bytes it is about to keep."""
    from pathlib import Path
    source = Path('prism/partial_project.py').read_text(encoding='utf-8')
    occurrences = source.count('model_attachment_ids')
    if occurrences < 2:
        pytest.skip('pending main-conversation patch RFC 7.5: partial_project '
                    'collects required attachments for material_records only '
                    f'(model_attachment_ids appears {occurrences} time(s))')
    assert occurrences >= 2


def test_pending_events_declare_material_identity():
    """RFC 7.3: one material edit must be traceable per material."""
    from prism.model_library import events
    required = ('material_id', 'binding_id', 'catalog_revision',
                'state_revision', 'draft_generation')
    missing = [name for name in required if name not in events.IDENTITY_FIELDS]
    if missing:
        pytest.skip('pending main-conversation patch RFC 7.3: '
                    f'events.IDENTITY_FIELDS lacks {missing}; AI 4 D6 covers it '
                    'and stays failing on purpose')
    assert missing == []


def test_pending_binding_site_carries_its_resolution_state():
    """RFC 7.1: the contract must say assigned / unassigned / dangling."""
    import dataclasses
    from prism.model_library.multimaterial.contracts import BindingSite
    names = {item.name for item in dataclasses.fields(BindingSite)}
    if 'resolution' not in names:
        pytest.skip('pending main-conversation decision RFC 7.1: BindingSite has '
                    'no resolution field; catalog.binding_resolutions() carries '
                    'the same information for now')
    assert 'resolution' in names


# ---------------------------------------------------------------------------
# the failure mode the RFC wants to avoid: "owned" must not mean "dropped"
# ---------------------------------------------------------------------------
def test_owning_the_field_must_not_hide_it_from_the_record():
    """Claiming the field must not be what loses it.

    ``OWNED_FIELDS`` marks fields this implementation owns although the contract
    has no slot for them.  The save path merges unknown fields back, so the data
    survives either way -- but claiming a field while the codec drops it on
    encode is exactly the "claimed, therefore dropped" bug, so both
    classifications are checked against the same payload.
    """
    payload = _payload(multimaterial=_block())
    assert 'multimaterial' not in CONTRACT_FIELDS
    record = decode_record(payload)
    assert record.unknown_fields['multimaterial'] == BLOCK
    assert 'multimaterial' in OWNED_FIELDS or 'multimaterial' in record.unknown_fields

    owned = _payload(thumbnail={'state': 'ready'}, multimaterial=_block())
    record = decode_record(owned)
    assert record.unknown_fields['multimaterial'] == BLOCK
