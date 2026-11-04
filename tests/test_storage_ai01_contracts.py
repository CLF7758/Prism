"""AI-01 contract tests for the storage batch.

Prefix ``test_storage_ai01_`` as the batch rules require.  These tests only
use their own fixtures; they never touch a user project, and they never
write into the shared product tree.
"""
from __future__ import annotations

import os
import sqlite3
import sys

import pytest

from prism.storage import (
    AckState,
    BackendKind,
    BackendRouter,
    Capability,
    CommitState,
    DurableState,
    ProjectIdentity,
    SaveReceipt,
    SourceKind,
    SourceRef,
    SourceState,
    StorageError,
    StorageErrorKind,
    Terminal,
    contract_manifest,
    identity_of,
    receipt_of,
)
from prism.storage.legacy_backend import LegacyBackend
from prism.storage.source_ref import (
    classify_stored_body,
    legacy_page_ref,
    resolve_legacy_page,
)

from prism.fileio.schema import APPLICATION_ID, SCHEMA, USER_VERSION


# ------------------------------------------------------------------ fixtures
def _make_project(path: str, *, user_version: int = USER_VERSION,
                  application_id: int = APPLICATION_ID,
                  pages=(), attachments=()):
    """Create a small .prism-shaped project with the real schema."""
    conn = sqlite3.connect(path)
    try:
        conn.execute(f"PRAGMA application_id={application_id}")
        conn.execute(f"PRAGMA user_version={user_version}")
        for statement in SCHEMA:
            conn.execute(statement)
        for page_id, content in pages:
            conn.execute(
                "INSERT INTO workspace_pages (id, kind, title, data) VALUES (?, ?, ?, ?)",
                (page_id, "document", f"page-{page_id}", content))
        for att_id, name, mime, blob in attachments:
            conn.execute(
                "INSERT INTO attachments (id, name, mime, blob) VALUES (?, ?, ?, ?)",
                (att_id, name, mime, blob))
        conn.commit()
    finally:
        conn.close()
    return path


@pytest.fixture()
def legacy_project(tmp_path):
    path = str(tmp_path / "sample.prism")
    return _make_project(
        path,
        pages=[("p1", '{"title":"kept"}'), ("p2", None)],
        attachments=[(1, "note.txt", "text/plain", b"hello-bytes")],
    )


# ------------------------------------------------------------- A01 legacy ok
def test_storage_ai01_legacy_project_opens_and_keeps_existing_contract(legacy_project):
    """A01: the current .prism still opens through the new seam."""
    backend = LegacyBackend(legacy_project)
    probe = backend.probe(legacy_project)
    assert probe.kind is BackendKind.LEGACY_SQLITE
    assert probe.readable is True
    assert probe.supported is True
    assert probe.user_version == USER_VERSION
    assert probe.application_id == APPLICATION_ID
    assert probe.supports(Capability.READ)
    assert probe.supports(Capability.PAGE_BODY)
    assert probe.supports(Capability.ATTACHMENT)

    snapshot = backend.snapshot()
    assert set(snapshot.page_ids) == {"p1", "p2"}
    assert snapshot.read_only is True
    assert snapshot.source_revision and len(snapshot.source_revision) == 64
    backend.close()


def test_storage_ai01_page_body_read_does_not_touch_the_scene(legacy_project):
    """A01: reading a never-opened body is a read of the file, not an edit."""
    backend = LegacyBackend(legacy_project)
    resolved = resolve_legacy_page(legacy_project, "p1")
    assert resolved.ok
    assert resolved.data == b'{"title":"kept"}'
    assert resolved.state is SourceState.PRESENT
    backend.close()


def test_storage_ai01_attachment_bytes_are_exact(legacy_project):
    backend = LegacyBackend(legacy_project)
    resolved = resolve_legacy_page(legacy_project, "p1")
    assert resolved.ok
    from prism.storage.source_ref import resolve_legacy_attachment
    att = resolve_legacy_attachment(legacy_project, "1")
    assert att.ok
    assert att.data == b"hello-bytes"
    backend.close()


# ------------------------------------------------------- A02 refuse unknown
def test_storage_ai01_future_version_is_refused_for_write(tmp_path):
    """A02: a newer format is readable but never written by this build."""
    path = str(tmp_path / "future.prism")
    _make_project(path, user_version=USER_VERSION + 1)
    before = open(path, "rb").read()

    backend = LegacyBackend(path)
    probe = backend.probe(path)
    assert probe.supported is False
    assert probe.writable is False
    assert probe.readable is True
    assert "newer" in (probe.unsupported_reason or "")
    assert probe.supports(Capability.READ) is True
    assert probe.supports(Capability.WRITE) is False

    with pytest.raises(StorageError) as excinfo:
        BackendRouter([backend]).open(path)
    assert excinfo.value.kind is StorageErrorKind.UNSUPPORTED_VERSION

    # the source bytes are untouched
    assert open(path, "rb").read() == before
    backend.close()


def test_storage_ai01_foreign_application_id_is_refused(tmp_path):
    path = str(tmp_path / "foreign.prism")
    _make_project(path, application_id=APPLICATION_ID + 1)
    before = open(path, "rb").read()
    backend = LegacyBackend(path)
    probe = backend.probe(path)
    assert probe.supported is False
    assert probe.writable is False
    assert open(path, "rb").read() == before
    backend.close()


def test_storage_ai01_non_database_is_not_adopted(tmp_path):
    """A02/A05: an unknown path is refused, never turned into a new db."""
    path = str(tmp_path / "notaproject.prism")
    with open(path, "wb") as fh:
        fh.write(b"this is not a database")
    before = open(path, "rb").read()
    backend = LegacyBackend(path)
    probe = backend.probe(path)
    assert probe.kind is BackendKind.UNKNOWN
    assert probe.supported is False
    assert open(path, "rb").read() == before
    backend.close()


def test_storage_ai01_missing_path_is_unknown_not_empty_db(tmp_path):
    path = str(tmp_path / "absent.prism")
    backend = LegacyBackend(path)
    probe = backend.probe(path)
    assert probe.kind is BackendKind.UNKNOWN
    assert probe.readable is False
    assert not os.path.exists(path)
    backend.close()


# ------------------------------------------------------- A04 identity rules
def test_storage_ai01_project_identity_keeps_three_identities_apart():
    ident = ProjectIdentity("p1", "inst-1", "epoch-1")
    assert ident.project_id == "p1"
    assert ident.storage_instance_id == "inst-1"
    assert ident.open_epoch == "epoch-1"
    assert ident.same_project(ProjectIdentity("p1", "inst-2", "epoch-9")) is True
    assert ident.with_epoch("epoch-2").open_epoch == "epoch-2"
    with pytest.raises(ValueError):
        ProjectIdentity("", "inst", "epoch")


def test_storage_ai01_source_ref_kinds_and_states():
    legacy = legacy_page_ref("p1")
    assert legacy.kind is SourceKind.LEGACY_ARCHIVE
    external = SourceRef(SourceKind.EXTERNAL_LINK, "e1",
                          external_uri="file:///x.png")
    assert external.external_uri == "file:///x.png"
    with pytest.raises(ValueError):
        SourceRef(SourceKind.EXTERNAL_LINK, "e2")  # no uri


def test_storage_ai01_missing_empty_notloaded_are_different_facts(tmp_path):
    """A04: 缺失 / 空内容 / 未加载 must not collapse into one another."""
    path = str(tmp_path / "states.prism")
    _make_project(path, pages=[("empty", ""), ("deferred", None)])
    backend = LegacyBackend(path)

    missing = resolve_legacy_page(path, "nope")
    assert missing.error.kind is StorageErrorKind.MISSING
    assert missing.state is SourceState.MISSING

    empty = resolve_legacy_page(path, "empty")
    assert empty.error.kind is StorageErrorKind.EMPTY_CONTENT
    assert empty.state is SourceState.EMPTY_CONTENT

    deferred = resolve_legacy_page(path, "deferred")
    assert deferred.error.kind is StorageErrorKind.NOT_LOADED
    assert deferred.state is SourceState.NOT_LOADED
    backend.close()


def test_storage_ai01_classify_stored_body_is_pure():
    class Fake:
        found = True
        content = None
        error = None
    assert classify_stored_body(Fake()) is SourceState.NOT_LOADED


def test_storage_ai01_receipt_reuses_save_outcome_fields():
    """A04: the receipt is built from AI-02's SaveOutcome, not beside it."""

    class Outcome:
        operation_id = "op-1"
        project_id = "p1"
        open_epoch = "e1"
        target = "out.prism"
        captured_revision = "rev-1"
        commit_state = "complete"
        ack_state = "confirmed"
        terminal = "confirmed"
        changed_entities = ("p1",)
        source_before = "sb"
        source_after = "sa"

    receipt = receipt_of(Outcome())
    assert isinstance(receipt, SaveReceipt)
    assert receipt.operation_id == "op-1"
    assert receipt.identity.project_id == "p1"
    assert receipt.durable_state is DurableState.COMMITTED
    assert receipt.commit_state is CommitState.COMPLETE
    assert receipt.ack_state is AckState.CONFIRMED
    assert receipt.terminal is Terminal.CONFIRMED
    assert receipt.changed_entities == ("p1",)
    assert receipt.complete is True


def test_storage_ai01_written_but_unconfirmed_is_its_own_state():
    class Outcome:
        operation_id = "op-2"
        project_id = "p1"
        open_epoch = "e1"
        target = "out.prism"
        captured_revision = "rev-1"
        commit_state = "complete"
        ack_state = "failed"
        terminal = "committed_not_confirmed"
        changed_entities = ("p1",)

    receipt = receipt_of(Outcome())
    assert receipt.durable_state is DurableState.COMMITTED_UNCONFIRMED
    assert receipt.is_durable is True
    assert receipt.complete is True


def test_storage_ai01_unknown_commit_state_never_becomes_success():
    class Outcome:
        operation_id = "op-3"
        project_id = "p1"
        open_epoch = "e1"
        target = "out.prism"
        captured_revision = "rev-1"
        commit_state = "something-else"
        ack_state = "something-else"
        terminal = "failed"
        changed_entities = ()

    receipt = receipt_of(Outcome())
    assert receipt.commit_state is CommitState.UNKNOWN
    assert receipt.durable_state is DurableState.UNKNOWN
    assert receipt.complete is False


def test_storage_ai01_identity_of_reads_existing_structures():
    class Q:
        project = "p9"
        source_revision = "sr"
        overlay_revision = "or"
    ident = identity_of(Q(), open_epoch="e5")
    assert ident.project_id == "p9"
    assert ident.storage_instance_id == "sr"
    assert ident.open_epoch == "e5"


# ------------------------------------------------------------------ A07 API
def test_storage_ai01_contract_manifest_is_publishable():
    manifest = contract_manifest()
    assert manifest["contract"] == "storage-contract-v1.1"
    assert manifest["qt_free"] is True
    assert manifest["pure_python"] is True
    # the contract must point at the real existing owners, not invent new ones
    assert manifest["reused_types"]["QueryKey"]["module"] == "prism.catalog_browser"
    assert manifest["reused_types"]["CountPublication"]["module"] == "prism.count_contract"
    assert manifest["reused_types"]["SaveOutcome"]["module"] == "prism.fileio.save_preparation"
    assert manifest["reused_types"]["layout_key"]["module"] == "prism.compact_layout"
    assert set(manifest["error_kinds"]) >= {
        "missing", "empty_content", "not_loaded", "corrupt",
        "unsupported_version", "read_only", "cancelled",
        "committed_not_confirmed", "insufficient_space", "unknown"}


def test_storage_ai01_contract_module_is_qt_free():
    """A07: contracts.py must not drag Qt in.

    Checked on the AST, not on raw text: the module's docstring is allowed
    to *mention* QApplication in prose; what must not exist is an import of
    it, or any call to it.
    """
    import ast
    import prism.storage.contracts as contracts
    tree = ast.parse(open(contracts.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported.add(node.module.split(".")[0])
    for forbidden in ("PyQt6", "PyQt5", "sqlite3"):
        assert forbidden not in imported, f"contracts.py imports {forbidden}"
    # no Qt object is ever constructed
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "QApplication"
        if isinstance(node, ast.Name):
            assert node.id != "QApplication"


def test_storage_ai01_router_refuses_unknown_paths(tmp_path):
    path = str(tmp_path / "weird.bin")
    with open(path, "wb") as fh:
        fh.write(b"nope")
    backend = LegacyBackend(path)
    router = BackendRouter([backend])
    with pytest.raises(StorageError) as excinfo:
        router.open(path)
    assert excinfo.value.kind is StorageErrorKind.UNKNOWN
