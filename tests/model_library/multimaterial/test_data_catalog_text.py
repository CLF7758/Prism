"""Text encoding: certain, ambiguous, damaged, and two names that decode alike.

The rule under test is not "match the reference reader".  It is:

* the original bytes are never rewritten or dropped;
* a name's **identity** never rests on a lossy decoding;
* an encoding that cannot be decided is reported as ambiguous instead of being
  presented as a complete parse;
* two different byte strings that decode to the same replacement characters
  stay two different materials.

The last two fixtures are the ones that caught the previous implementation: a
legacy Chinese library decoded by guesswork, and two damaged names that would
otherwise collapse into one row.
"""
import json

import pytest

from prism.model_library.multimaterial.catalog import (
    binding_resolutions,
    definitions_by_id,
    dangling_bindings,
    usage_counts,
)
from prism.model_library.multimaterial.inspectors import inspect_source
from prism.model_library.multimaterial.inspectors import text as textio

#: "木材" in GB18030 — bytes that are *not* valid UTF-8.
WOOD_GB = '木材'.encode('gb18030')
#: "金属" in GB18030.
METAL_GB = '金属'.encode('gb18030')


def _write(path, data: bytes):
    path.write_bytes(data)
    return path


def test_a_utf8_file_is_certain_and_keeps_readable_names(tmp_path):
    _write(tmp_path / 'lib.mtl', 'newmtl 木材\nKd 1 0 0\n'.encode('utf-8'))
    _write(tmp_path / 'a.obj', 'mtllib lib.mtl\nusemtl 木材\nf 1 2 3\n'.encode('utf-8'))

    catalog = inspect_source(tmp_path / 'a.obj')
    flags = json.loads(catalog.unknown_json)
    assert flags['text_encoding'] == 'utf-8'
    assert flags['text_encoding_certain'] is True
    assert [item.name for item in catalog.definitions] == ['木材']
    # A certain decoding lets the text itself be the identity.
    assert catalog.definitions[0].material_id == 'obj:mtl:lib.mtl#木材'
    assert usage_counts(catalog) == {'obj:mtl:lib.mtl#木材': 1}
    assert not any('编码' in note for note in catalog.unresolved)


def test_a_utf16_container_is_read_as_text_with_the_same_identity(tmp_path):
    # A UTF-16 OBJ: splitting it on bytes would see NUL between every character.
    _write(tmp_path / 'lib.mtl', 'newmtl 木材\nKd 1 0 0\n'.encode('utf-16'))
    _write(tmp_path / 'a.obj', 'mtllib lib.mtl\nusemtl 木材\nf 1 2 3\n'.encode('utf-16'))

    catalog = inspect_source(tmp_path / 'a.obj')
    assert json.loads(catalog.unknown_json)['text_encoding'] == 'utf-16'
    assert [item.name for item in catalog.definitions] == ['木材']
    # The same characters must give the same identity as the UTF-8 file.
    assert catalog.definitions[0].material_id == 'obj:mtl:lib.mtl#木材'
    assert usage_counts(catalog) == {'obj:mtl:lib.mtl#木材': 1}


def test_a_legacy_chinese_library_is_decoded_but_reported_as_ambiguous(tmp_path):
    _write(tmp_path / 'lib.mtl', b'newmtl ' + WOOD_GB + b'\nKd 1 0 0\n')
    _write(tmp_path / 'a.obj', b'mtllib lib.mtl\nusemtl ' + WOOD_GB + b'\nf 1 2 3\n')

    catalog = inspect_source(tmp_path / 'a.obj')
    entry = catalog.definitions[0]
    # The name is shown, because in this context gb18030 is the best-supported
    # candidate -- but nothing claims the file's encoding is known.
    assert entry.name == '木材'
    assert any('编码不确定' in note for note in catalog.unresolved)
    flags = json.loads(catalog.unknown_json)
    assert flags['text_encoding_certain'] is False
    assert flags['text_encoding'] == 'gb18030'
    # The identity carries a digest of the raw bytes, so it does not depend on
    # the guess being right.
    assert entry.material_id.startswith('obj:mtl:lib.mtl#木材~')
    assert usage_counts(catalog) == {entry.material_id: 1}


def test_damaged_bytes_are_marked_lossy_and_still_identifiable(tmp_path):
    damaged = b'\x81\x20'  # valid in no candidate: gb18030/big5/cp1252 all reject
    _write(tmp_path / 'lib.mtl', b'newmtl ' + damaged + b'\nKd 1 0 0\n')
    _write(tmp_path / 'a.obj', b'mtllib lib.mtl\nusemtl ' + damaged + b'\nf 1 2 3\n')

    catalog = inspect_source(tmp_path / 'a.obj')
    entry = catalog.definitions[0]
    flags = json.loads(catalog.unknown_json)
    assert flags['text_encoding'] == 'damaged'
    assert flags['text_encoding_certain'] is False
    assert '\ufffd' in entry.name                    # the damage is visible
    assert any('编码不确定' in note for note in catalog.unresolved)
    # The bytes remain the identity, so the material is still referenceable.
    assert entry.material_id.startswith('obj:mtl:lib.mtl#')
    assert usage_counts(catalog) == {entry.material_id: 1}


def test_two_names_that_decode_alike_stay_two_materials(tmp_path):
    # Both bytes decode under *no* candidate, so both names become the same
    # replacement character: a name-keyed catalog would merge them.
    first, second = b'\x81', b'\x8d'
    _write(tmp_path / 'lib.mtl',
           b'newmtl ' + first + b'\nKd 1 0 0\nnewmtl ' + second + b'\nKd 0 1 0\n')
    _write(tmp_path / 'a.obj',
           b'mtllib lib.mtl\nusemtl ' + first + b'\nf 1 2 3\n'
           b'usemtl ' + second + b'\nf 1 2 3\n')

    catalog = inspect_source(tmp_path / 'a.obj')
    ids = [item.material_id for item in catalog.definitions]
    assert len(ids) == 2
    assert ids[0] != ids[1]
    assert catalog.definitions[0].name == catalog.definitions[1].name  # same display
    counts = usage_counts(catalog)
    assert counts[ids[0]] == 1 and counts[ids[1]] == 1
    assert not [item for item in catalog.binding_sites
                if item.original_material_id is None]
    assert dangling_bindings(catalog) == {}


def test_a_name_written_in_two_encodings_is_not_silently_merged(tmp_path):
    # The OBJ says the name in UTF-8, the library says it in GB18030.  The bytes
    # differ, so the reference cannot be resolved by bytes; the reader must say
    # so instead of guessing that both mean the same material.
    _write(tmp_path / 'lib.mtl', b'newmtl ' + WOOD_GB + b'\nKd 1 0 0\n')
    _write(tmp_path / 'a.obj', 'mtllib lib.mtl\nusemtl 木材\nf 1 2 3\n'.encode('utf-8'))

    catalog = inspect_source(tmp_path / 'a.obj')
    site = catalog.binding_sites[0]
    assert site.original_material_id is None
    assert binding_resolutions(catalog)[site.binding_id] == 'dangling'
    assert dangling_bindings(catalog)[site.binding_id]['material_name'] == '木材'
    assert any('悬空' in note for note in catalog.unresolved)


def test_the_original_bytes_are_never_rewritten(tmp_path):
    path = _write(tmp_path / 'lib.mtl', b'newmtl ' + WOOD_GB + b'\nKd 1 0 0\n')
    before = path.read_bytes()
    _write(tmp_path / 'a.obj', b'mtllib lib.mtl\nusemtl ' + WOOD_GB + b'\nf 1 2 3\n')
    inspect_source(tmp_path / 'a.obj')

    assert path.read_bytes() == before


# ---------------------------------------------------------------------------
# the decoder itself
# ---------------------------------------------------------------------------
def test_decoder_reports_certainty_per_container():
    assert textio.decode_text(b'plain ascii').certain is True
    assert textio.decode_text(b'\xef\xbb\xbf' + '木材'.encode()).encoding == 'utf-8-sig'
    assert textio.decode_text('木材'.encode('utf-16')).encoding == 'utf-16'
    legacy = textio.decode_text(WOOD_GB)
    assert legacy.certain is False and legacy.text == '木材'
    assert 'gb18030' in legacy.candidates
    broken = textio.decode_text(b'\x81\x20')
    assert broken.lossy is True and broken.certain is False


def test_identity_never_collides_for_different_bytes():
    certain = textio.decode_text(b'wood')
    ambiguous = textio.decode_text(b'\x81\x20')
    other = textio.decode_text(b'\x82\x20')

    assert certain.identity(b'wood') == 'wood'
    assert ambiguous.identity(b'\x81\x20') != other.identity(b'\x82\x20')
    assert ambiguous.identity(b'\x81\x20').startswith('\ufffd')


def test_a_utf16_container_and_a_utf8_container_share_one_identity(tmp_path):
    utf8 = _write(tmp_path / 'utf8.mtl', 'newmtl 木材\nKd 1 0 0\n'.encode('utf-8'))
    utf16 = _write(tmp_path / 'utf16.mtl', 'newmtl 木材\nKd 1 0 0\n'.encode('utf-16'))

    ids = []
    for name in ('utf8.obj', 'utf16.obj'):
        library = 'utf8.mtl' if name == 'utf8.obj' else 'utf16.mtl'
        encoding = 'utf-8' if name == 'utf8.obj' else 'utf-16'
        _write(tmp_path / name,
               f'mtllib {library}\nusemtl 木材\nf 1 2 3\n'.encode(encoding))
        catalog = inspect_source(tmp_path / name)
        ids.append(catalog.definitions[0].material_id)

    assert ids[0].endswith('#木材') and ids[1].endswith('#木材')
    # The identity is the name, which does not depend on the container.
    assert ids[0].split('#', 1)[1] == ids[1].split('#', 1)[1]
    del utf8, utf16
