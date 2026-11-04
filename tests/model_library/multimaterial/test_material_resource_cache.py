"""The unpacked material originals must survive the session that unpacked them.

Opening the model library used to unpack every material texture into a
directory named after a fresh ``uuid4`` session token, so the next opening
read all 130 MB of them again -- 1.7 s measured on the reference project --
and left another copy behind on disk.

The unpacked files are addressed by their content digest, so they belong to
no session.  These tests pin that: the second session must find them where
the first one left them, and must not read the attachments again.
"""
import hashlib
import json
from types import SimpleNamespace

from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import MaterialTarget


CATALOG_BYTES = b'{}'
TEXTURE_BYTES = b'not really a png, but the bytes never matter here'
RESOURCE_ID = 'sha256:' + hashlib.sha256(TEXTURE_BYTES).hexdigest()


class CountingResources:
    """The attachment store, counting how often a blob is read."""

    def __init__(self, blobs):
        self.blobs = {int(key): value for key, value in blobs.items()}
        self.reads = {}

    def read_all(self, attachment_id):
        self.reads[attachment_id] = self.reads.get(attachment_id, 0) + 1
        return self.blobs[int(attachment_id)]


class Owner:
    """The model-library service, with only what the material service uses."""

    def __init__(self, root, resources):
        package_root = root.parent / 'package'
        package_root.mkdir(parents=True, exist_ok=True)
        self.repository = SimpleNamespace(
            context=SimpleNamespace(project_session='session', open_epoch=1),
            resources=resources,
            get_payload=lambda model_id: payload(),
            material_cache_root=root)
        self.bundles = SimpleNamespace(
            root=str(root.parent / 'session-bundles'),
            prepare=lambda model_id, revision, **kw: SimpleNamespace(
                package_root=str(package_root), main_relative_path='model.obj',
                lease_id='lease', model_id=model_id, source_revision=revision))


def build_service(owner):
    service = ProjectMaterialService(owner, SimpleNamespace(shutdown=lambda: None))
    return service


def payload():
    return {
        'source_revision': 3,
        'files': [],
        'multimaterial': {
            'catalog_ref': {
                'attachment_id': 7,
                'sha256': hashlib.sha256(CATALOG_BYTES).hexdigest(),
            },
            'resources': {
                RESOURCE_ID: {
                    'attachment_id': 9,
                    'sha256': RESOURCE_ID.removeprefix('sha256:'),
                    'locator': 'T_Body_BC.PNG',
                },
            },
        },
    }


def target():
    return MaterialTarget('session', 1, 'model-a', 3, 1, 1)


def test_the_second_session_does_not_read_the_texture_again(tmp_path):
    root = tmp_path / 'material-work'
    resources = CountingResources({7: CATALOG_BYTES, 9: TEXTURE_BYTES})

    first = build_service(Owner(root, resources))
    try:
        first._load_serial(target())
        assert resources.reads.get(9) == 1, 'the first session must unpack it'
    finally:
        first.close()

    unpacked = root / (RESOURCE_ID.replace(':', '_') + '.PNG')
    assert unpacked.exists()
    assert unpacked.read_bytes() == TEXTURE_BYTES

    second = build_service(Owner(root, resources))
    try:
        second._load_serial(target())
        assert resources.reads.get(9) == 1, \
            'the texture was already unpacked; reading it again is the waste'
    finally:
        second.close()


def test_the_unpacked_texture_is_published_atomically(tmp_path):
    """A half written file must never be mistaken for a complete one."""
    root = tmp_path / 'material-work'
    resources = CountingResources({7: CATALOG_BYTES, 9: TEXTURE_BYTES})
    service = build_service(Owner(root, resources))
    try:
        service._load_serial(target())
    finally:
        service.close()
    leftovers = [p.name for p in root.iterdir() if p.name.endswith('.tmp')]
    assert not leftovers, 'a staging file was left behind'
