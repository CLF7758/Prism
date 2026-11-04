from dataclasses import replace
import pytest
from prism.model_library.multimaterial.application import ProjectMaterialService
from prism.model_library.multimaterial.contracts import MaterialEditRequest,MaterialOperation
from .conftest import make_target,chair_catalog


def test_preview_restore_uses_opening_state_and_rejects_other_source():
    service=ProjectMaterialService.__new__(ProjectMaterialService)
    catalog, _ = chair_catalog()
    base=service._initial(catalog,make_target())
    service._preview_baselines={base.target.model_id:dict(state=base,snapshots={},paths={},resources={})}
    current=replace(base,target=replace(base.target,state_revision=base.target.state_revision+1),
        materials=(replace(base.materials[0],property_patch_json='{"roughness":0.9}'),)+base.materials[1:])
    item=dict(state=current,snapshots={},paths={},resources={})
    request=MaterialEditRequest('restore','close',current.target,MaterialOperation.RESTORE_PREVIEW)
    restored=service._edit(item,request)
    assert restored==replace(base,target=current.target)
    item['state']=replace(current,target=replace(current.target,source_revision=current.target.source_revision+1))
    with pytest.raises(ValueError,match='source changed'):
        service._edit(item,replace(request,target=item['state'].target))
