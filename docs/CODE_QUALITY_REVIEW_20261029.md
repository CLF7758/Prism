# Code quality verification — 2026-10-29

## Implemented boundaries
- presentation.material_parameters(properties, mapped_slots): pure scalar interpretation, no paths, PIL, Qt or native scene; preserves mapped-factor versus unmapped-constant semantics.
- presentation.prepare_material_maps: owns image decoding, transforms and immutable derived cache. material_spec remains the compatible orchestration entry point. Original files remain read-only.
- partial_save._apply_row_patch: scalar/JSON merge, replacement and explicit removal policy isolated from database access. capture_partial_save still owns the SAME SQLite read transaction, revision checks and attachment completeness checks. No weaker write authorization.

## Evidence
artifacts/code-quality-focused-20261029.txt: 24 passed (partial-save transaction tests, presentation pixel tests, concurrent cache publication, detached scalar rules).
artifacts/code-quality-boundaries-20261029.txt: 49 passed / 13 failed. Eleven failures occur at existing write format guard: replacement fixtures have user_version=0. Two occur on AssetGrid.item missing in purge UI tests. These failures remain recorded; not fixed by weakening format validation or adding obsolete widget APIs. Baseline attribution has NOT yet been independently established by reverting the refactor.

## Verified candidates / unfinished work
- _offer_recovery_snapshot: repository search finds only definition and commented startup invocation. Reader/writer/clear recovery APIs have live callers. Nothing in recovery deleted: dynamic integration and intended recovery product behavior require separate confirmation.
- ProjectMaterialService._edit: replacement, patch, clipboard, assignment and recovery branches still share mutable item registries. Next step is command result objects and explicit resource registration; not a mixin split. NOT complete.
- AssetBrowser._advance_catalog_rows: time-budgeted query consumption, overlay precedence, proxy cache, sorting and publication still combined. Preserve stale-install guard and bounded batches; query/install ownership refactor NOT complete.
- PrismGraphicsView: existing public entry points retained. Full responsibility split NOT complete; no evidence that line count alone causes runtime stalls.

## Limits
No desktop control, no user-project database edits, no performance improvement claimed from these structural changes. Dead-code tools are candidates, not deletion authority. Remaining hotspots are tracked rather than reported as solved.

## Follow-up implementation and retest
- Original 13 failing cases: fixtures now declare USER_VERSION and select via AssetGrid selectionModel. Original assertions retained; production supported_format unchanged. An intermediate retest missed a QtCore import (2 failures); corrected. Original-failure suites plus canvas policy: 44 passed, artifacts/code-quality-original-failures-final-20261029.txt.
- Extended save/JSON/purge/presentation/cache/catalog/canvas regressions: 93 passed in 66.11s, artifacts/code-quality-expanded-final-20261029.txt. This run precedes the isolated canvas policy extraction below.
- Surface replacement policy extracted to replacement.py: immutable definitions in/out; snapshot registration stays in service transaction owner. Real integrated runtime plus ordering regression: 6 passed, artifacts/code-quality-command-runtime-20261029.txt.
- Catalog ordering now consumes explicit mode, sort key, identity/name functions and visual ranking; returns new sequence, preserving input. Query generations/cache ownership/publication still in browser; no claim of comprehensive paging rewrite.
- Canvas drop eligibility now pure explicit-input policy in canvas_drop_policy.py. View owns event translation/hit testing; undo and scene mutation unchanged. Five threshold/selection/same-page cases pass. Existing canvas interaction regression rerun separately in artifacts/code-quality-canvas-final-20261029.txt.
- Still open: full command handler result/resource-registration boundary; full pagination state ownership; larger GraphicsView responsibilities; manually approved dead-code deletion. Bounded extractions above do NOT mean entire hotspot cleanup complete.

## Command resource, install lifetime and canvas ownership follow-up

### Implemented
- Command preparation now deep-copies the loaded item before _edit; path/snapshot/resource mutations remain private until put_batch transaction succeeds. Live item registration is published after success. Existing durable receipt and render lanes retained.
- ResourcePlan explicitly produces ordered unique uploads and validates stored digests before registry creation. Missing unregistered sources now fail rather than silently omitting a texture. Registry creation does not mutate its input.
- _resolved now reads bases from the supplied item, not the global model cache. First integration run exposed this hidden dependency: paste/reopen retained .55 instead of copied .31. Corrected; failing evidence retained in code-quality-ownership-20261029.txt (1 failed / 23 passed).
- InstallIdentity centralizes project/open-epoch/query-generation/token lifetime comparison. Qt ownership and active-pending identity stay in UI; streams/cache lifecycles remain unchanged. This is lifetime policy separation, not a full rewrite of the mutable pending dictionary.
- DragFileStore owns reference-original lookup, generated PNG paths and temporary-directory lifetime, detached from GraphicsView. View retains thin compatibility entry point. Failed PNG save removes its temporary file.

### Verification
artifacts/code-quality-ownership-unit-20261029.txt: 4 passed, resource deduplication/hash mismatch/incomplete upload/missing locator, install lifetime mismatch, original/derived file lifetime.
artifacts/code-quality-ownership-final-20261029.txt: 28 passed in 32.91s, including real material commit/retry/copy/paste/reopen, nine-slot controls/persistence, catalog partial browsing and canvas drop interactions, plus new boundary tests.
No desktop control or live user project edits. Failure after filesystem upload / process crash recovery was not newly fault-injected in this round. No measured speedup claim.

### Remaining scope
GraphicsView still owns other input, camera, selection and save orchestration. Pagination pending remains dictionary-based, with cache-owned checkpoint copies already implemented before this change. Further changes require operation-specific characterization, not arbitrary method moves. Full architecture cleanup is not signed complete by these tests.

## Pagination continuations and canvas operations follow-up
- Replaced live overlay iterator in pending with immutable OverlayProgress(entries, offset). A checkpoint and two resumed installs cannot advance each other's overlay cursor.
- Added checkpoint_install/resume_install ownership boundary: candidate and new collections copied; query predicate containers copied; stream/evidence/proxy objects borrowed intentionally. UI preview widgets/indexes excluded. CatalogResultCache get_pending/pending_view return detached containers rather than exposing cache-owned dictionaries.
- Existing mutable install metadata dictionary remains for UI counters/layout integration; this iteration removes the shared continuation and cache-container escape, not every dictionary in the browser.
- Canvas copy uses explicit scene/clipboard ports in clipboard_workflow.copy_selection. Empty selection is a no-op and preserves clipboard. View retains mode cancellation/application clipboard lookup.
- Canvas deletion classification is an immutable DeleteSelectionPlan. It owns the captured asset/annotation partition; view remains the single undo-command submitter, scene owns mutation. Existing recoverable trash behavior is preserved.
- artifacts/code-quality-checkpoint-canvas-20261029.txt: 57 passed. artifacts/code-quality-checkpoint-replay-20261029.txt: 7 passed. Includes independent two-resume/no-duplicate/no-shared-context checks, cache-read mutation isolation and existing real row replay tests.
- Selection/trash/view/browser regression output: artifacts/code-quality-selection-final-20261029.txt. See raw result; do not count pending work as passed.
- No desktop automation. Camera/input/open/save responsibilities in GraphicsView remain candidates for separate behavior characterization. No full-class rewrite or measured latency claim.

### Selection regression follow-through
- The broad selection/trash/full-view/browser run produced no result during the observed interval and was explicitly stopped (only the two verified pytest launcher/interpreter processes; running Prism untouched). artifacts/code-quality-selection-final-20261029.txt is incomplete, not a pass. Cause not determined.
- Focused selection/trash/purge run: 31 passed / 4 failed. Two old AssetGrid count/item calls and two named pixmap fixtures lacking retained originals. Kept original source-preservation product guard; updated fixtures to explicitly encoded PNG source bytes via set_source_blob and current selectionModel/rowCount interfaces. Original assertions retained.
- Retest: artifacts/code-quality-selection-retest-20261029.txt: 35 passed in 16.66s. Covers delete/trash/restore/undo/redo/save-reopen and partial-project purge protection.
- Full test_view.py and full asset browser suite remain unverified by the interrupted broad run. Existing focused browser/pagination/canvas regressions above passed. Camera, gesture arbitration, project-open and save coordination still live in view; this is a tracked remaining scope, not a claim of complete class decomposition.

## Explicit material transaction and bounded overlay batches
- commit_transaction.write_material_commit now owns SQLite record-revision check, exact resource batch count, digest registration, sparse state encoding, unknown-field-preserving payload merge, durable receipt and UPDATE. Explicit inputs; CommitResult output. No Qt, renderer, executor, UI acknowledgement or connection commit. Repository resource batch remains the outer transaction owner.
- application.commit retains preparation/scheduling and publishes memory/UI state only after the resource transaction returns. Existing retry receipt lookup stays before preparation.
- consume_overlay now takes immutable progress plus explicit match/valid/time-budget predicates and returns OverlayBatch(accepted, per-batch counters, continuation, stop). It never installs widgets or mutates pending. Browser owns scheduling and applies a batch only if its install lifetime remains valid.
- Validation: artifacts/code-quality-transaction-overlay-20261029.txt: 21 passed in 29.46s (real runtime commit/retry/copy/paste/reopen, nine-slot persistence, resource registration, partial browser, row replay/checkpoints).
- artifacts/code-quality-transaction-fault-20261029.txt: 4 passed. SQLite test verifies independent observer cannot see uncommitted writes; explicit rollback after UPDATE restores revision; stale revision and missing stored catalog reject before publication; unknown fields and input block survive. Overlay tests verify invalid/budget/limit/exhaustion and independent batch counters.
- Remaining: directory-row proxy reuse/creation still belongs to browser, as does UI installation metadata. Command _edit still dispatches patch/clipboard/assignment operations; this change specifically separates persistence ownership, not every command handler. No claim of complete subsystem rewrite or performance speedup. No desktop control.

## Shared material workflow continuation
Current implementation/evidence and unresolved scopes: docs/MATERIAL_WORKFLOW_UNIFICATION_20261029.md. Common preview intent throttle and complete source decode validation are integrated. No claim of full cache/storage/session equivalence.

Shared-workflow follow-up: focus setup corrected (8 passed), bounded common decoded-source cache and committed-render failure identity installed. Combined verification 31 passed in artifacts/shared-workflow-followup-final-20261029.txt. See MATERIAL_WORKFLOW_UNIFICATION_20261029.md for scopes and limits.

Material durable history now implemented: sparse row deltas, 100-step undo, redo branch invalidation, exact-scope validation, normal transactional receipts and resources retained. Combined regression 70 passed; history/UI 20 passed; real managed-original history chain final 5 passed. Raw evidence and prior failed test-attempt attribution in MATERIAL_WORKFLOW_UNIFICATION_20261029.md. Desktop and comprehensive fault/performance acceptance not claimed.

Six-layer semantics follow-up: shared binding validation/colour-space policy, shared normal control availability; fixed actual material notes loss during lazy-host async navigation. Regression 103 passed / 4 skipped; flag tests 3 passed. Detailed failed-run attribution and remaining limits in MATERIAL_WORKFLOW_UNIFICATION_20261029.md.
