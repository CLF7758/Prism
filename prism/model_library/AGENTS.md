# Model library implementation boundaries

Read docs/MODEL_LIBRARY_HANDOFF.md, your assigned document under
docs/model-library-tasks/, and the referenced full plan before changing this
subsystem. Core workflows are now integrated. Read
docs/MODEL_LIBRARY_INTEGRATION_20261026.md for the measured scope and remaining
gates; don't treat either the original scaffold baseline or worker reports as
current acceptance.

There are exactly four implementation roles: storage/import/export, native F3D,
UI/workflow, and logging/independent acceptance. Each has one standalone task
document specifying file ownership. The Codex in the current main conversation
owns integration and final acceptance; do not appoint another integration AI.
Shared contracts.py, ports.py, service.py and final service composition belong to
that main conversation. Submit exact interface requests instead of editing them
independently. Do not start other agents or message other chats just because the
task documents describe parallel work.

- No F3D, VTK or Qt imports in contracts.py or ports.py.
- No F3D imports or subprocess startup at Prism application import/startup.
- No fake successful import/export, empty success responses, sample records in
  production, or in-memory substitute for project persistence.
- Keep UI, project repository, import/export and native process owners separate.
- Only the main process writes project data. Native helpers receive immutable
  manifests and revision identities, never live widgets or scene objects.
- Preserve original model/dependency bytes and relative paths. Only original
  export is in scope. Do not introduce GLB conversion or edited-model export.
- Reuse existing project tags, notes and texture controls.
- The model sidebar section is intentionally UI-only. Do NOT enable folder
  creation, drops or metadata editing until full AND partial saves, snapshots,
  unknown fields, delete/restore and project switching have been integrated.
- Do not advertise F3D show_edges as pure wireframe or Cells as triangle count.
- Extend capabilities only when the operation, UI handler and independent
  acceptance checks actually exist. Keep unavailable operations fail-fast.
- Native loading/rendering must not block the Prism GUI thread.
- Instantiate the shared preview window shell and F3D widget in an isolated helper
  process. Never pass a QWidget over IPC or reparent one across processes.
- Add events to the existing performance/diagnostics pipeline, not a new logging
  system. Do not log original bytes, texture Base64 or notes.
- Preserve material-library behavior when changing shared cards.

Current focused regression entry point:
python -m pytest -q tests/test_model_library_scaffold.py tests/test_material_cards.py

For navigation/tree changes also run the resource-tree and navigation tests
listed in the handoff document. Do not weaken old behavior checks to hide a new
integration defect.
