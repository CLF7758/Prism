Prism development
=================

See README.md for setup, tests, screenshots and Windows installer builds.
Use Python 3.12. Preserve upstream copyright and GPL notices.
Do not commit private projects, credentials, build caches or installers.
The release must include matching source, notices and build instructions.
See packaging/README.md and docs/RELEASE_AUDIT.md before publishing binaries.

Module boundaries and reuse
---------------------------

See ``docs/ARCHITECTURE_EXTENSION_GUIDE.md`` before extending an existing feature.
Reuse project storage, source-file handling, tags, resource trees, search and export
services. Keep file IO and expensive decoding outside GUI event handlers.
Use existing rendering/editor engines through adapters and preserve original bytes.

Run ``tests/test_architecture.py`` with the focused feature regressions.
Use ``tools/run_tests_safe.py`` for bounded whole-suite runs; retain failure,
timeout and crash evidence separately from successful reruns.
