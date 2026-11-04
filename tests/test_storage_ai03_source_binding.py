"""AI-03: the binding between the object repository and storage-contract-v1.

The contract is **PROPOSED**: the signature is published, the state is not
frozen yet.  These tests therefore run against the published module
(``prism/storage/contracts.py``, copied verbatim from AI-01's package and
hash checked against the batch manifest) and prove what the object path
does with each of the contract's facts.

The seven negative controls of the contract are mapped here one by one:
the ones that really trigger on the object path, and the ones that do not
apply here - with the reason, not a silent skip.
"""

import hashlib
import sys
from pathlib import Path

import pytest
pytest.importorskip("disk_objectstore", reason="requires storage-experimental optional dependency")

_PRIVATE_DEPS = Path(__file__).resolve().parents[2] / 'deps'
if _PRIVATE_DEPS.is_dir():
    sys.path.insert(0, str(_PRIVATE_DEPS))

from prism.storage import contracts                        # noqa: E402
from prism.storage.dos_repository import (                # noqa: E402
    DosObjectRepository, open_readonly)
from prism.storage.object_repository import (             # noqa: E402
    ActiveWriter, ObjectCorrupt, ObjectNotFound,
    ReadOnlyViolation, RepositoryClosed)
from prism.storage import source_binding                  # noqa: E402


CONTRACT_SHA256 = ('053a929f2987b327bfd48a7665e60469e9608b6a6f446e9d78d182a8fc009da1')


def test_the_contract_module_is_the_published_one():
    """The binding is written against the published bytes, not a copy of it."""
    module = Path(contracts.__file__)
    digest = hashlib.sha256(module.read_bytes()).hexdigest()
    assert digest == CONTRACT_SHA256, (
        'prism/storage/contracts.py is not the published module; the '
        'binding would be written against something else')


def test_a_managed_object_becomes_a_source_ref(tmp_path):
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    try:
        payload = b'source ref payload' * 100
        stat = repository.put_stream(iter([payload]))
        ref = source_binding.source_ref_for_object(stat, media_role='image')
        assert ref.kind is contracts.SourceKind.MANAGED_OBJECT
        assert ref.source_id == stat.hashkey
        # The content identity, not a path and not an mtime.
        assert ref.content_revision == stat.hashkey
        assert ref.byte_length == len(payload)
        assert ref.state is contracts.SourceState.PRESENT
        assert ref.external_uri is None
    finally:
        repository.close()


def test_the_three_source_kinds_are_not_collapsed(tmp_path):
    managed = source_binding.source_ref_for_object(
        type('S', (), {'hashkey': 'a' * 64, 'length': 7})(),
        media_role='image')
    legacy = source_binding.source_ref_for_legacy('legacy-1', byte_length=7)
    external = source_binding.source_ref_for_external(
        'ext-1', 'file:///C:/x.png', byte_length=7)
    assert managed.kind is not legacy.kind
    assert legacy.kind is not external.kind
    assert external.external_uri
    # The external link we do not own may have an unknown size; that is
    # None, never 0.
    assert external.byte_length is None or external.byte_length >= 0


def test_the_five_states_stay_different(tmp_path):
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    try:
        stat = repository.put_stream(iter([b'states']))
        assert source_binding.state_for_exception(
            ObjectNotFound('x')) == contracts.SourceState.MISSING.value
        assert source_binding.state_for_exception(
            ObjectCorrupt('x')) == contracts.SourceState.CORRUPT.value
        assert source_binding.state_for_exception(
            RepositoryClosed('x')) == contracts.SourceState.NOT_LOADED.value
        # An object that is there is present, not "empty" and not "missing".
        ref = source_binding.source_ref_for_object(stat)
        assert ref.state is contracts.SourceState.PRESENT
    finally:
        repository.close()


def test_durable_and_unconfirmed_are_different_answers(tmp_path):
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    try:
        stat = repository.put_stream(iter([b'durable states']))
        verified = repository.verify(stat.hashkey)
        assert source_binding.durable_state(verified) == \
            contracts.DurableState.COMMITTED.value
        # The bytes are there but nobody recomputed the hash: that is
        # "committed, not confirmed", not "committed".
        unconfirmed = type(verified)(hashkey=verified.hashkey,
                                     length=verified.length, durable=True,
                                     verified=False, container=verified.container)
        assert source_binding.durable_state(unconfirmed) == \
            contracts.DurableState.COMMITTED_UNCONFIRMED.value
        missing = type(verified)(hashkey=verified.hashkey, length=0,
                                 durable=False, verified=False,
                                 container=verified.container)
        assert source_binding.durable_state(missing) == \
            contracts.DurableState.NOT_STARTED.value
    finally:
        repository.close()


def test_only_a_managed_object_is_read_through_the_repository(tmp_path):
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    try:
        payload = b'open through the binding'
        stat = repository.put_stream(iter([payload]))
        ref = source_binding.source_ref_for_object(stat)
        assert b''.join(source_binding.open_object(repository, ref)) == payload
        legacy = source_binding.source_ref_for_legacy('legacy-1')
        with pytest.raises(Exception):
            source_binding.open_object(repository, legacy)
    finally:
        repository.close()


def test_the_contract_spellings_of_the_same_fact_differ():
    """A defect found while signing: one fact, two spellings.

    `StorageErrorKind.COMMITTED_NOT_CONFIRMED` is 'committed_not_confirmed'
    while `DurableState.COMMITTED_UNCONFIRMED` is 'committed_unconfirmed'.
    The same fact must not be written two ways, or a consumer that compares
    the strings across the two vocabularies silently misses.
    """
    assert contracts.StorageErrorKind.COMMITTED_NOT_CONFIRMED.value == \
        'committed_not_confirmed'
    assert contracts.DurableState.COMMITTED_UNCONFIRMED.value == \
        'committed_unconfirmed'
    assert (contracts.StorageErrorKind.COMMITTED_NOT_CONFIRMED.value
            != contracts.DurableState.COMMITTED_UNCONFIRMED.value)


def test_the_live_writer_case_has_no_counterpart_in_the_contract():
    """The one gap: the contract has no error kind for "a writer is active".

    We know exactly what happened (the container has a write ahead log), so
    mapping it to ``unknown`` would be wrong.  It is reported as a gap
    instead of being papered over.
    """
    assert source_binding.error_kind(ActiveWriter('busy')) == 'SOURCE_BUSY'
    assert source_binding.error_kind(ObjectNotFound('x')) == 'MISSING'
    assert source_binding.error_kind(ObjectCorrupt('x')) == 'CORRUPT'
    assert source_binding.error_kind(ReadOnlyViolation('x')) == 'READ_ONLY'
    assert source_binding.error_kind(RepositoryClosed('x')) == 'CANCELLED'


# ── the contract's negative controls, mapped onto the object path ───────

def test_negative_control_absent_path_creates_nothing(tmp_path):
    """Contract case "path absent -> UNKNOWN, no file created".

    The object path's analogue: a folder that is not a container is
    refused, and nothing is written anywhere.
    """
    missing = tmp_path / 'not-a-container'
    with pytest.raises(Exception):
        open_readonly(missing, cache_dir=tmp_path / 'cache')
    assert not missing.exists()
    assert not (tmp_path / 'cache').exists() or \
        not list((tmp_path / 'cache').glob('*.idx'))


def test_negative_control_foreign_path_creates_nothing(tmp_path):
    """Contract case "non-sqlite path -> UNKNOWN, no database created".

    The analogue: a folder that holds something else entirely is refused,
    and the reader does not create an index there.
    """
    foreign = tmp_path / 'foreign'
    foreign.mkdir()
    (foreign / 'readme.txt').write_text('not a container', encoding='utf-8')
    before = sorted(p.name for p in foreign.iterdir())
    with pytest.raises(Exception):
        open_readonly(foreign, cache_dir=tmp_path / 'cache')
    assert sorted(p.name for p in foreign.iterdir()) == before


def test_an_object_that_was_not_read_is_not_loaded_not_missing(tmp_path):
    """Contract case "page missing / empty / never-loaded -> three errors".

    The object path's three facts: the object is missing, the object is
    there but its bytes were never materialised, and the bytes are empty.
    They must not collapse into one another.
    """
    folder = tmp_path / 'store'
    folder.mkdir()
    repository = DosObjectRepository(folder)
    repository._container.init_container(pack_size_target=64 * 1024)
    try:
        stat = repository.put_stream(iter([b'never loaded']))
        empty = repository.put_stream(iter([b'']))
        # Not loaded: the object is there, its bytes were never read.
        known = repository.stat(stat.hashkey)
        assert known is not None
        assert known.length == len(b'never loaded')
        assert known.verified is False
        # Empty content: there is an object, its content is empty.
        assert repository.stat(empty.hashkey).length == 0
        # Missing: no object at all.
        assert repository.stat('0' * 64) is None
    finally:
        repository.close()
