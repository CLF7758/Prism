from types import SimpleNamespace
from prism.catalog_ordering import order_catalog_items


def test_ordering_preserves_input_and_visual_membership():
    a, b, c = [SimpleNamespace(identity=i, name=n) for i, n in [(1, 'Z'), (2, 'a'), (3, 'B')]]
    source = [a, b, c]
    args = dict(name=lambda x: x.name, key=lambda x: x.identity)
    assert order_catalog_items(source, mode='all', sort_key='name', **args) == [b, c, a]
    assert order_catalog_items(source, mode='visual', sort_key='name', visual_keys=(3, 1), **args) == [c, a]
    assert source == [a, b, c]
    one = order_catalog_items(source, mode='random', sort_key='name', random_seed=7, **args)
    assert one == order_catalog_items(source, mode='random', sort_key='name', random_seed=7, **args)
