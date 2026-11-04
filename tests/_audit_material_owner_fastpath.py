"""Does the content-proof check slow the owned-record fast path down again?

``MaterialLibraryService`` reuses an already validated record list instead of
copying and validating every record again.  On the 100k project that reuse is
worth seconds: validating the whole library costs seconds, reusing it costs
nothing.

The fix for the stale-proof hole made a content proof win over the ownership
proof.  A content proof is a fingerprint of the whole list, so it must not be
computed on the hot path.  This probe measures both paths on 100k records:

* owner path - the production path, no content proof on the scene
* content path - a content proof is present, the fingerprint has to be built

The owner path must stay in the millisecond range.
"""
from __future__ import annotations

import time
from types import SimpleNamespace

N = 100000


def _records(count):
    return [{'id': 'm%06d' % index, 'projectId': 'p', 'schemaVersion': 1,
             'name': 'material %d' % index, 'notes': '', 'folderId': None,
             'tags': [], 'textures': {}, 'preview': {}, 'nativePackages': [],
             'status': 'ready', 'createdAt': '2026-01-01T00:00:00.000000+00:00',
             'updatedAt': '2026-01-01T00:00:00.000000+00:00', 'deletedAt': None}
            for index in range(count)]


def _scene(records, **extra):
    scene = SimpleNamespace(project_id='p', material_records=records, **extra)
    return scene


def test_the_owner_path_stays_free_of_the_fingerprint(qapp):
    from prism.material_service import (MaterialLibraryService, rebind_material_records,
                                        material_validation_fingerprint)
    records = _records(N)
    scene = _scene(records)
    rebind_material_records(scene, records, project_id='p')

    started = time.monotonic()
    service = MaterialLibraryService(scene, None)
    owner_ms = (time.monotonic() - started) * 1000
    assert service.records is records, 'the owned list was copied'
    print('\nAUDIT: owner 路径 %d 条记录 %.1f ms（未复制、未校验）'
          % (N, owner_ms), flush=True)

    # The same list with a content proof: the fingerprint of the whole list
    # has to be built, which is what the production path must avoid.
    records = _records(N)
    scene = _scene(records, _material_validation_proof=material_validation_fingerprint(
        records, 'p'))
    rebind_material_records(scene, records, project_id='p')
    scene._material_validation_proof = material_validation_fingerprint(records, 'p')
    started = time.monotonic()
    service = MaterialLibraryService(scene, None)
    content_ms = (time.monotonic() - started) * 1000
    assert service.records is records, 'the validated list was copied'
    print('AUDIT: content 路径 %d 条记录 %.1f ms（要算整表指纹）'
          % (N, content_ms), flush=True)
    print('AUDIT: owner/content = %.3f%%' % (owner_ms / content_ms * 100), flush=True)
