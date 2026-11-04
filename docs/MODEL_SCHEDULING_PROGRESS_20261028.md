# Material command / scene scheduling

Current implementation:
- Durable commands and catalog reads use a priority single-owner scheduler. Commit requests are never coalesced.
- Interactive native work uses a separate single-owner scheduler; render latency no longer holds the durable command executor.
- A committed material snapshot is delivered atomically with its receipt to prevent the UI exposing a new revision with old controls.
- Native rendering consumes detached item snapshots. Pending scene draws for the same model are replaceable; running draws finish before another native mutation starts.
- New drafts supersede old preview intent without inserting a committed-state restore or force-cancelling the native helper. Explicit cancellation retains its previous semantics.
- Cover rendering retains its separate controller/executor and existing revision coalescing.
- Render failures use runtime_failed, independent of commit success and draft preview tokens.

Evidence: artifacts/material-lanes-final.txt: 36 passed, including real temporary-project attachment/parameter save-reopen and blocked-render idempotent commit verification, native worker tests, UI feedback/editor, scheduler ordering. First failed attempt retained in material-lanes-test.txt.

No user project or originals modified. Not complete product-wide acceptance: startup eager construction, comprehensive panel/controller decomposition, desktop long-run workload and fault-injection of all failure orderings remain unverified/unfinished. No claim of measured desktop speedup.


## 2026-10-29 Startup and inspector responsibility follow-up

Material/model library pages now have stable deferred tab and inspector hosts. Hidden pages are not built on canvas startup or a no-op save. The model composition and periodic material diagnostics avoid forcing creation. The factory runs once at first actual use; the real library keeps the original services and controls. Material search reveal resolves the stable tab host; inspector draft writes forward to the real owner.

PreviewSession now owns pending preview identity and lease taking; CommitLedger owns registration, receipt idempotence and pending order. Both are Qt-free. ModelMaterialPanel handles widget presentation and port calls and delegates these state decisions. This is a focused extraction, not a claim that all panel responsibilities have been eliminated.

Evidence: edit-session-ledger-final.txt 100 passed (UI cases plus native temporary-project integration); deferred-libraries-retest.txt 26 passed; deferred-navigation-final.txt 47 passed; startup-session-final.txt 22 passed. Overlapping suites must not be added into a unique total. Initial deferred-libraries-regression.txt retained: search reveal identity regression repaired; test_main mocked-event-loop teardown error has not been signed off as resolved.

Desktop session 109516 opened the user's existing project through its file dialog. One startup stall measured 1276.9ms; no repeated controlled A/B, so no speedup percentage claimed. Desktop work was stopped at the user's instruction. Resource samples are partial, mixed navigation/idle, and do NOT establish leak freedom or long-duration acceptance. Window remains open; user desktop acceptance pending.

Manual acceptance: restart updated source; open the existing project; enter model/material libraries the first time and repeatedly; navigate from search to a material; verify correct inspector, selection and pending metadata; rapidly change material slots then save/reopen; ensure old preview failures do not replace current selection or alter save success. Check first-use delay separately from startup. Long-run resource/response validation is reserved for the user, not marked passed.
