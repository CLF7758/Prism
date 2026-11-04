from types import SimpleNamespace as Obj
import pytest
from prism.model_library.multimaterial.resource_plan import plan_resources


def test_plan_deduplicates_and_registers_without_mutating_source():
    definition = Obj(textures=[Obj(resource_id='sha256:abc'), Obj(resource_id='sha256:abc')])
    existing = {'old': {'attachment_id': 1}}
    plan = plan_resources([definition], existing, {'sha256:abc': '/tmp/a.png'})
    assert plan.resource_ids == ('sha256:abc',)
    resource = Obj(sha256='abc', attachment_id=2, name='a.png', size_bytes=10)
    result = plan.register(existing, [resource])
    result['old']['attachment_id'] = 99
    assert existing == {'old': {'attachment_id': 1}}
    assert result['sha256:abc']['attachment_id'] == 2
    with pytest.raises(ValueError, match='Incomplete'):
        plan.register(existing, [])
    resource.sha256 = 'changed'
    with pytest.raises(ValueError, match='changed'):
        plan.register(existing, [resource])


def test_missing_source_is_not_silently_omitted():
    with pytest.raises(ValueError, match='no source'):
        plan_resources([Obj(textures=[Obj(resource_id='missing')])], {}, {})
