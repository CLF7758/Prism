"""Bounded fixes: owned projections, loader proof, and inspector reuse."""
import copy
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from prism.asset_records import ImageRecord
from prism.material_service import MaterialLibraryService, material_validation_fingerprint


def test_normal_image_record_still_copies_nested_metadata(qapp):
    extra = dict(imageWidth=3, imageHeight=3, future={'list': [1]})
    normal = ImageRecord(dict(data=extra), None)
    extra['future']['list'].append(2)
    assert normal._extra_data['future']['list'] == [1]
    owned = ImageRecord(dict(data=extra), None, _owned_extra=True)
    assert owned._extra_data is extra


def test_validated_materials_transfer_once_and_mutated_proof_revalidates(qapp, monkeypatch):
    from prism import material_service
    records = []
    scene = SimpleNamespace(project_id='p', material_records=records,
        _material_validation_proof=material_validation_fingerprint(records, 'p'))
    validate = Mock()
    monkeypatch.setattr(material_service, 'validate_material_records', validate)
    service = MaterialLibraryService(scene, None)
    assert service.records is records
    validate.assert_not_called()
    assert scene._material_validation_proof is None
    scene._material_validation_proof = material_validation_fingerprint(records, 'p')
    records.append({'changed': True})
    service2 = MaterialLibraryService(scene, None)
    validate.assert_called_once()
    assert service2.records == records and service2.records is not records


def test_slot_identical_publications_skip_widgets_but_reset_after_user_edit(qapp, monkeypatch):
    from prism.widgets.material_slots import MaterialSlot
    slot = MaterialSlot('roughness', 'Roughness')
    slot.show_binding({})
    setter = Mock(wraps=slot.color.setStyleSheet)
    monkeypatch.setattr(slot.color, 'setStyleSheet', setter)
    slot.show_binding({})
    setter.assert_not_called()
    slot.amount.setValue(0.123)
    slot.show_binding({})
    setter.assert_called_once()
    assert slot.amount.value() != pytest.approx(0.123)
    slot.deleteLater()


def test_stale_material_proof_never_skips_real_validation(qapp):
    scene = SimpleNamespace(project_id='p', material_records=[])
    scene._material_validation_proof = material_validation_fingerprint([], 'p')
    scene.material_records.append({'invalid': 'must reject'})
    with pytest.raises((ValueError, KeyError)):
        MaterialLibraryService(scene, None)
