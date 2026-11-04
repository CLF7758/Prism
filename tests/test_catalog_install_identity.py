from prism.catalog_install import InstallIdentity


def test_install_rejects_each_changed_lifetime():
    identity = InstallIdentity(('g', 'p'), 'p', 1, 2)
    current = dict(token=('g', 'p'), project='p', open_epoch=1, query_generation=2)
    assert identity.matches(**current)
    for field, value in [('token', ('h', 'p')), ('project', 'q'), ('open_epoch', 2), ('query_generation', 3)]:
        changed = dict(current, **{field: value})
        assert not identity.matches(**changed)
