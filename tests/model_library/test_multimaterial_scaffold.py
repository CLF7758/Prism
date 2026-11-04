"""Only scaffold safety; these tests do NOT accept real multi-material editing."""
import pytest

from prism.model_library.contracts import ModelCardSummary, ModelFeatureUnavailable
from prism.model_library.multimaterial.contracts import (
    MaterialEditRequest, MaterialOperation, MaterialTarget)
from prism.model_library.multimaterial.service import MultiMaterialService


def _target():
    return MaterialTarget('session', 1, 'model', 2, 3, 4)


def test_scaffold_cannot_claim_a_working_backend():
    service = MultiMaterialService()
    report = service.capabilities(_target())
    assert report.reason
    assert not any((report.read_catalog, report.edit_material, report.assign_material,
                    report.pick_material, report.normal_override, report.sss,
                    report.displacement, report.wireframe))
    request = MaterialEditRequest('request', 'gesture', _target(), MaterialOperation.PATCH)
    for call in (lambda: service.inspect(_target()), lambda: service.preview(request, 1),
                 lambda: service.commit(request), lambda: service.cancel('unknown')):
        with pytest.raises(ModelFeatureUnavailable):
            call()


def test_panel_has_no_fake_catalog_or_enabled_action(qtbot):
    from prism.widgets.model_material_panel import ModelMaterialPanel
    panel = ModelMaterialPanel()
    qtbot.addWidget(panel)
    panel.reset('model-a')
    assert panel.materials.model() is None
    assert not panel.materials.isEnabled()
    assert all(not button.isEnabled() for button in panel.actions.values())
    assert '尚未解析' in panel.heading.text()
    panel.reset()
    assert panel.model_id is None
    assert '未选择' in panel.heading.text()


def test_details_reset_forgets_previous_material_context(qtbot):
    from prism.widgets.model_details import ModelDetails
    details = ModelDetails()
    qtbot.addWidget(details)
    details.show_summary(ModelCardSummary('first', 'First', 'gltf'))
    assert details.material_panel.model_id == 'first'
    details.show_summary(ModelCardSummary('second', 'Second', 'fbx'))
    assert details.material_panel.model_id == 'second'
    details.show_summary(None)
    assert details.material_panel.model_id is None
    assert details.sections.tabText(0) == '材质'
