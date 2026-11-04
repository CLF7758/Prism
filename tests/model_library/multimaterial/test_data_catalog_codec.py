"""Catalog payload round-trip, retention of unknown fields, format dispatch.

The point of these tests is the promise that a catalog can leave memory, be
stored in a project record and come back without losing what this version does
not understand -- and that an unimplemented format fails instead of answering
"no materials".
"""
import json
from pathlib import Path

import pytest

from prism.model_library.multimaterial import catalog as catalog_module
from prism.model_library.multimaterial.catalog import (
    CatalogParseError,
    catalog_from_payload,
    catalog_to_payload,
    definitions_by_id,
    usage_counts,
)
from prism.model_library.multimaterial.inspectors import (
    INSPECTORS,
    PARSER_LIMITS,
    SUPPORTED_FORMATS,
    inspect_source,
    inspector_for,
    parser_limits,
)
from prism.model_library.multimaterial.inspectors.fbx import PARSER_ID_FBX
from prism.model_library.multimaterial.inspectors.gltf import PARSER_ID_GLTF
from prism.model_library.multimaterial.inspectors.obj import PARSER_ID_OBJ

PNG = b'\x89PNG\r\n\x1a\npayload'


def test_editable_slots_match_the_existing_material_convention():
    from prism.material_textures import SLOTS
    # The catalog must not invent a slot vocabulary of its own.
    assert catalog_module.EDITABLE_SLOTS == tuple(SLOTS)


def test_every_supported_format_has_a_parser_and_declared_limits():
    assert SUPPORTED_FORMATS == ('.fbx', '.glb', '.gltf', '.obj')
    assert set(INSPECTORS) == set(SUPPORTED_FORMATS)
    for _suffix, (parser_id, reader) in INSPECTORS.items():
        assert callable(reader)
        assert parser_limits(parser_id), parser_id
    assert set(PARSER_LIMITS) >= {PARSER_ID_GLTF, PARSER_ID_OBJ, PARSER_ID_FBX}
    with pytest.raises(CatalogParseError):
        inspector_for('model.usdz')


def _gltf_catalog(tmp_path):
    (tmp_path / 'wood.png').write_bytes(PNG)
    document = {
        'asset': {'version': '2.0'},
        'extensionsUsed': ['KHR_texture_transform'],
        'materials': [{
            'name': 'wood',
            'pbrMetallicRoughness': {'baseColorTexture': {'index': 0}},
            # A field this version does not map: it must survive the round trip.
            'extras': {'studio_note': 'keep me', 'batch': 42},
        }],
        'textures': [{'source': 0}],
        'images': [{'uri': 'wood.png'}],
    }
    path = tmp_path / 'wood.gltf'
    path.write_text(json.dumps(document), encoding='utf-8')
    return inspect_source(path)


def test_payload_round_trip_keeps_every_field(tmp_path):
    catalog = _gltf_catalog(tmp_path)
    payload = catalog_to_payload(catalog)
    rebuilt = catalog_from_payload(json.loads(json.dumps(payload)))

    assert rebuilt.schema_version == catalog.schema_version
    assert rebuilt.revision == catalog.revision
    assert rebuilt.parser_id == catalog.parser_id
    assert rebuilt.source_digest == catalog.source_digest
    assert rebuilt.unresolved == catalog.unresolved
    assert [item.material_id for item in rebuilt.definitions] == [
        item.material_id for item in catalog.definitions]
    original = definitions_by_id(catalog)['gltf:material:0']
    copy = definitions_by_id(rebuilt)['gltf:material:0']
    assert copy.name == original.name
    assert copy.workflow == original.workflow
    assert copy.textures == original.textures
    assert copy.properties_json == original.properties_json
    assert copy.unsupported == original.unsupported
    assert copy.unknown_json == original.unknown_json
    assert rebuilt.binding_sites == catalog.binding_sites
    assert usage_counts(rebuilt) == usage_counts(catalog)


def test_unknown_material_fields_are_retained_verbatim(tmp_path):
    catalog = _gltf_catalog(tmp_path)
    wood = definitions_by_id(catalog)['gltf:material:0']
    source = json.loads(wood.unknown_json)['source']

    assert source['extras'] == {'studio_note': 'keep me', 'batch': 42}
    rebuilt = catalog_from_payload(catalog_to_payload(catalog))
    assert json.loads(definitions_by_id(rebuilt)['gltf:material:0']
                      .unknown_json)['source'] == source


def test_unknown_envelope_fields_from_a_newer_version_survive(tmp_path):
    catalog = _gltf_catalog(tmp_path)
    payload = catalog_to_payload(catalog)
    payload['future_field'] = {'written_by': 'prism-next'}
    payload['another'] = [1, 2, 3]

    rebuilt = catalog_from_payload(payload)
    again = catalog_to_payload(rebuilt)

    assert again['future_field'] == {'written_by': 'prism-next'}
    assert again['another'] == [1, 2, 3]


def test_texture_payload_is_json_safe(tmp_path):
    catalog = _gltf_catalog(tmp_path)
    payload = catalog_to_payload(catalog)
    # No live mapping, no bytes: the payload can be written to a project record.
    json.dumps(payload, allow_nan=False)
    use = payload['definitions'][0]['textures'][0]
    assert use['slot'] == 'basecolor'
    assert use['resource_id'].startswith('sha256:')
    assert isinstance(use['uv_transform'], list)


# ---------------------------------------------------------------------------
# reading files this repository already carries (evidence, not fixtures)
# ---------------------------------------------------------------------------
REAL_SAMPLES = Path(__file__).resolve().parents[3] / '.tmp' / 'f3d_probe'
REAL_CASES = (
    ('box.fbx', PARSER_ID_FBX, 1),
    ('spider.fbx', PARSER_ID_FBX, 4),
    ('work/iso_v_vt_vn_usemtl.obj', PARSER_ID_OBJ, 0),
    ('work/g_texture.gltf', PARSER_ID_GLTF, 1),
    ('work/gltf_embedded.gltf', PARSER_ID_GLTF, None),
)


@pytest.mark.parametrize('relative,parser_id,definitions', REAL_CASES)
def test_real_sample_files_are_read_without_crashing(relative, parser_id, definitions):
    path = REAL_SAMPLES / relative
    if not path.is_file():
        pytest.skip(f'sample not present: {relative}')
    catalog = inspect_source(path)

    assert catalog.parser_id == parser_id
    assert len(catalog.definitions) >= (definitions or 0)
    # Every binding site points at a definition the catalog contains, or at no
    # material at all -- never at a name that is not there.
    known = {item.material_id for item in catalog.definitions}
    for binding in catalog.binding_sites:
        assert binding.original_material_id in known or binding.original_material_id is None
    assert catalog.source_digest.startswith('sha256:')
